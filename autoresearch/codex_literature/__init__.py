"""SEARCH PAPERS + READ PAPERS, with orchestration kept in the existing harness."""
from .models import Paper
from .retrieval import LiteratureRetriever, FullTextResolver
from .pdf import CodexPDFReader


class CodexLiterature:
    def __init__(self, engine):
        self.engine = engine
        self.retriever = LiteratureRetriever(engine.literature)
        self.reader = CodexPDFReader(engine)
        self.fulltext = FullTextResolver(engine.literature)

    async def search(self, query, *, year_from=None, top_k=20, **options):
        return await self.retriever.search(query, top_k=top_k, year_from=year_from, **options)

    async def prepare(self, run_id, harness, *, question, queries, context):
        from .codex.query_planner import plan, compile_queries
        from .codex.screening import screen
        expanded = await plan(self.engine, harness, run_id, question, queries, context)
        planned_queries = compile_queries(expanded)
        survey = await self.engine.collect_literature(run_id, planned_queries)
        if not survey['coverage']['verified_count']:
            return survey
        selection = await screen(self.engine, harness, run_id, question, survey['papers'], context)
        seeds = {p['id']: p for p in survey['papers']}
        neighbors, reports = [], []
        for pid in selection['seed_ids']:
            reusable = False
            if seeds[pid].get('rag_version_id'):
                try:
                    reusable = self.engine.paper_rag.load(seeds[pid]['rag_version_id']).get('provenance') == 'validated_live_analysis'
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            if reusable:
                # Existing validated analysis is the first source. References
                # can still be requested explicitly when it leaves a gap.
                reports.append({'seed_paper_id': pid, 'directions': [], 'errors': [],
                    'exhaustive': False, 'provenance': 'shared_paper_rag',
                    'skipped_reason': 'validated_rag_analysis_available'})
                continue
            report = await self.engine.expand_literature_citations(run_id, seeds[pid], limit=10)
            neighbors.extend(report['papers'])
            reports.append({k: v for k, v in report.items() if k != 'papers'})
        from .. import literature_rounds, venue_policy
        candidates = literature_rounds.filter_papers(self.engine.store, run_id, neighbors)
        if venue_policy.enabled(self.engine.store.get('runs', run_id)):
            candidates = venue_policy.filter_papers(candidates)
        from ..lab import _search_relevance
        candidates = [p for p in candidates if any(_search_relevance(q, p)['accepted'] for q in planned_queries)]
        papers = self.retriever.merge_and_rank([p for p in survey['papers'] if p['id'] in selection['selected_ids']] + candidates,
            planned_queries, mode='recent' if literature_rounds.phase(self.engine.store.get('runs', run_id)) == 'idea' else 'relevance')
        from ..literature_survey import compact_paper
        papers = [compact_paper(p) for p in papers]
        result = {**survey, 'papers': papers, 'query_expansion': {**expanded, 'compiled_queries': planned_queries}, 'screening': selection,
            'citation_expansion': reports, 'coverage': {**survey['coverage'],
                'broad_distinct_count': survey['coverage']['distinct_count'],
                'reading_candidate_count': sum(p.get('verified') is True for p in papers),
                'reading_candidate_ids': [p['id'] for p in papers if p.get('verified') is True],
                'screened_count': len(selection['selected_ids']), 'citation_candidate_count': len(candidates),
                'citation_errors': [e for r in reports for e in r['errors']],
                'retrieval_stages': ['broad_search', 'metadata_screening', 'citation_expansion', 'deep_read_selection']}}
        from ..storage import now
        from .codex.query_planner import fingerprint
        errors = result['coverage']['citation_errors']
        if errors:
            result['coverage']['gaps'] = [*result['coverage']['gaps'], {'code': 'citation_expansion_incomplete',
                'message': 'Some references/citations requests failed; the retained expansion is partial.'}]
        key = f'{run_id}:survey-result:module:{fingerprint([survey["coverage"]["plan_id"], selection, reports])}'
        self.engine.check_active(run_id)
        self.engine.store.put('literature_inputs', key, {'id': key, 'run_id': run_id, 'kind': 'survey_result',
            **result, 'completed_at': now()})
        return result

    async def analyze(self, run_id, harness, papers, *, task, effort='high', context=None):
        """The existing scout owns retrieval/read tools and validated reading notes."""
        from ..engine import Harness, LITERATURE_READING
        from .codex.query_planner import fingerprint
        if effort not in {'low', 'medium', 'high', 'xhigh', 'max'}:
            raise ValueError('Unsupported literature reasoning effort')
        if not isinstance(papers, list) or not 1 <= len(papers) <= 120:
            raise ValueError('Analyze requires 1..120 paper records')
        self.engine.check_active(run_id)
        instruction = (task + '\n从提供论文中选择5–15篇制定reading_plan，数量不足则只选现有相关论文；优先遵循preferred_deep_read，'
            '仍须满足当前任务核心阅读门禁。先literature.rag_search检索本地共享库，'
            'shared_rag_indexes中的合格历史分析先literature.rag_read查看并评判当前相关性；'
            '可以复用已导入的有效笔记，缺少或存疑的材料才papers.download获取全文，'
            '再pdf.manifest/pdf.search定位、pdf.read引用、pdf.analyze_figure查看关键图表，保存literature.record_notes。'
            '明确矛盾证据、因果/相关证据和未查看内容，不得将摘要或下载标为阅读。')
        supplied_ids = [p.get('id') for p in papers]
        purpose = 'literature:module:' + fingerprint([supplied_ids, task, effort])[:20]
        derived = {'papers', 'literature_reasoning_effort', 'source_tool_protocol', 'literature_notes_protocol',
                   'pdf_tools_protocol', 'codex_literature_protocol', 'paper_rag_protocol', 'shared_rag_indexes'}
        explicit_context = {k: v for k, v in (context or {}).items() if k not in derived}
        input_key = f'{run_id}:literature-analysis-input:' + fingerprint([supplied_ids, task, effort, explicit_context])
        frozen = self.engine.store.get('literature_inputs', input_key)
        from .retrieval.dedup import same_paper
        def matches(saved):
            saved_context = saved.get('context', {})
            originals = saved_context.get('papers', [])
            return (saved.get('instruction') == instruction and saved.get('schema') == LITERATURE_READING
                and {k: v for k, v in saved_context.items() if k not in derived} == explicit_context
                and [p.get('id') for p in originals] == supplied_ids
                and all(same_paper(a, b) for a, b in zip(originals, papers)))
        if frozen and not matches(frozen):
            frozen = None
        if not frozen:
            # Older module calls already froze these inputs in the Harness.
            # Recover that exact assignment before mutable retrieval metadata
            # (scores, provenance, checked_at) can create a second paid task.
            previous = [row for row in self.engine.store.list('handoff_inputs', run_id)
                if row.get('id', '').startswith(f'{run_id}:{purpose}:scout:') and matches(row)
                and row['id'] == Harness.decision_key(run_id, 'scout', purpose, instruction, row['context'], LITERATURE_READING)]
            previous.sort(key=lambda row: not bool(self.engine.store.get('decisions', row['id'])))
            if previous:
                frozen = {**previous[0], 'id': input_key, 'kind': 'codex_literature_analysis_input'}
                self.engine.store.put('literature_inputs', input_key, frozen)
        if frozen:
            return await harness.ask(run_id, 'scout', purpose, instruction, frozen['context'], LITERATURE_READING)
        from ..literature_reading import _run_papers
        from ..lab import _digest
        owned = _run_papers(self.engine.store, run_id)
        checked, rag_imports = [], []
        for paper in papers:
            trusted = owned.get(paper.get('id')) or self.engine.store.get('literature', _digest(paper.get('id')))
            if not (trusted and trusted.get('verified') is True and same_paper(trusted, paper)):
                trusted = await self.engine.literature.verify(paper)
            if trusted.get('verified') is not True:
                raise ValueError('Analyze requires identifier-verified papers')
            key = f'{run_id}:{trusted["id"]}'
            self.engine.store.mutate('run_evidence', key, lambda old, p=trusted, k=key:
                {**(old or {}), 'id': k, 'run_id': run_id, 'paper': p})
            if trusted.get('rag_version_id'):
                try:
                    imported = self.engine.paper_rag.adopt(self.engine, harness.intelligence, run_id, trusted)
                    if imported:
                        rag_imports.append({k: imported.get(k) for k in ('paper_id', 'source_id', 'rag_version_id')})
                except (OSError, ValueError, KeyError, TypeError):
                    pass  # Invalid shared evidence must follow ordinary acquisition.
            checked.append(trusted)
        from ..literature_survey import compact_paper
        papers = [compact_paper(p) for p in checked]
        identifiers = [p['id'] for p in papers]
        request_context = {**(context or {}), 'papers': papers, 'literature_reasoning_effort': effort,
             'source_tool_protocol': 1, 'literature_notes_protocol': 1,
             'pdf_tools_protocol': 1, 'codex_literature_protocol': 1, 'paper_rag_protocol': 1,
             'shared_rag_indexes': rag_imports}
        self.engine.store.put('literature_inputs', input_key, {'id': input_key, 'run_id': run_id,
            'kind': 'codex_literature_analysis_input', 'instruction': instruction, 'context': request_context,
            'schema': LITERATURE_READING})
        return await harness.ask(run_id, 'scout', 'literature:module:' + fingerprint([identifiers, task, effort])[:20],
            instruction, request_context, LITERATURE_READING)

    async def research(self, run_id, harness, *, question, top_k=10, deep_read=5, effort='high', context=None):
        if type(top_k) is not int or not 1 <= top_k <= 120 or type(deep_read) is not int or not 1 <= deep_read <= 15:
            raise ValueError('top_k must be 1..120 and deep_read 1..15')
        if effort not in {'low', 'medium', 'high', 'xhigh', 'max'}:
            raise ValueError('Unsupported literature reasoning effort')
        survey = await self.prepare(run_id, harness, question=question, queries=[question[:400]],
            context={**(context or {}), 'literature_reasoning_effort': effort})
        papers = survey['papers'][:top_k]
        analysis = await self.analyze(run_id, harness, papers, task=question, effort=effort,
            context={**(context or {}), 'preferred_deep_read': min(deep_read, len(papers))})
        return {'retrieval': survey, 'analysis': analysis}


__all__ = ['CodexLiterature', 'LiteratureRetriever', 'CodexPDFReader', 'FullTextResolver', 'Paper']
