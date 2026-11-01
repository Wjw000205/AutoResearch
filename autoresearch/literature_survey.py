"""Bounded, restartable metadata acquisition; a completed plan is not an exhaustive review."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone

from .lab import _doi, _search_relevance
from .storage import RunDeletedError, now
from .literature_rounds import recent as is_recent, publication_date


SURVEY_VERSION = 1
SOURCES = ('semantic_scholar', 'crossref', 'openalex', 'arxiv', 'pubmed', 'europe_pmc', 'paper_rag')


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _verified(paper):
    return paper.get('verified') is True and paper.get('verification', {}).get('status') == 'verified'


def _year(value):
    return value if type(value) is int and 1800 <= value <= 9999 else None


def _aliases(paper):
    aliases = set()
    doi = _doi(paper.get('doi'))
    if doi:
        aliases.add('doi:' + doi)
    arxiv = str(paper.get('arxiv_id') or '')
    if not arxiv and str(paper.get('id', '')).startswith('arxiv:'):
        arxiv = str(paper['id'])[6:]
    if re.fullmatch(r'(?:\d{4}\.\d{4,5}|[a-zA-Z.-]+/\d{7})(?:v\d+)?', arxiv):
        aliases.add('arxiv:' + re.sub(r'v\d+$', '', arxiv))
    title = re.sub(r'[^\w]', '', unicodedata.normalize('NFKC', str(paper.get('title') or '')).casefold())
    if title:
        aliases.add('title:' + title)
    return aliases


def compact_paper(paper):
    """Full originals remain in query checkpoints; never send 12k abstracts per record."""
    keys = ('id', 'paper_id', 'doi', 'pmid', 'pmcid', 'arxiv_id', 'semantic_scholar_id', 'journal',
            'citation_count', 'oa_pdf_url', 'fulltext_locations', 'fulltext_access', 'relevance_score', 'ranking',
            'retrieval_relations', 'url', 'fulltext_url', 'source', 'verified', 'checked_at',
            'provenance', 'identity_aliases', 'matched_queries', 'sources', 'metadata_conflicts', 'publication_date',
            'rag_entry_id', 'rag_version_id', 'rag_score', 'rag_analysis_at', 'retrieval_origin', 'venue', 'ccf',
            'identity_kind', 'source_reference_id', 'uploaded_source_id', 'uploaded_content_sha256',
            'content_sha256', 'bibliographic_identity_verified')
    result = {key: copy.deepcopy(paper[key]) for key in keys if key in paper}
    result.update(title=str(paper.get('title') or '')[:400], year=_year(paper.get('year')),
                  date_granularity='day' if publication_date(paper) else 'year' if _year(paper.get('year')) else 'unknown',
                  authors=[str(a)[:100] for a in (paper.get('authors') or [])[:5]],
                  abstract_excerpt=str(paper.get('abstract') or paper.get('abstract_excerpt') or '')[:320],
                  abstract_truncated=bool(paper.get('abstract_truncated')) or len(str(paper.get('abstract') or '')) > 320,
                  full_text_status='not_retrieved')
    verification = paper.get('verification') or {}
    result['verification'] = {key: copy.deepcopy(verification[key]) for key in
        ('status', 'method', 'evidence_url', 'metadata_sha256', 'canonical_year', 'title_match', 'identifier_match',
         'author_match', 'year_match', 'venue_metadata', 'content_sha256', 'bibliographic_identity_verified') if key in verification}
    return result


class LiteratureSurvey:
    def __init__(self, store, *, max_batches=10, per_query_limit=12, target_distinct=30, phase=None, date_policy=None, proposal_hash=None, module_protocol=0, search_profile=None):
        for name, value, low, high in [('max_batches', max_batches, 1, 10),
                ('per_query_limit', per_query_limit, 1, 12), ('target_distinct', target_distinct, 1, 120)]:
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f'{name} must be an integer between {low} and {high}')
        self.store = store
        self.max_batches, self.per_query_limit, self.target_distinct = max_batches, per_query_limit, target_distinct
        self.phase, self.date_policy, self.proposal_hash = phase, date_policy, proposal_hash
        self.module_protocol = module_protocol
        if search_profile not in (None, 'novelty'):
            raise ValueError('search_profile must be novelty when supplied')
        self.search_profile = search_profile

    def _guard(self, run_id):
        if self.store.get('run_deletions', run_id):
            raise RunDeletedError('Research has been deleted')
        if not self.store.get('runs', run_id):
            raise ValueError('Research not found')

    @staticmethod
    def _queries(queries):
        if not isinstance(queries, list) or not 1 <= len(queries) <= 3:
            raise ValueError('Provide one to three academic search queries')
        if any(not isinstance(q, str) or not q.strip() for q in queries):
            raise ValueError('Each search query must contain 1 to 400 characters')
        return list(queries)  # Preserve the supplied direction verbatim in the frozen plan.

    def _plan(self, run_id, queries):
        self._guard(run_id)
        inputs = {'run_id': run_id, 'queries': self._queries(queries), 'version': SURVEY_VERSION,
                  'max_batches': self.max_batches, 'per_query_limit': self.per_query_limit,
                  'target_distinct': self.target_distinct}
        if self.phase:
            inputs.update(phase=self.phase, date_policy=self.date_policy, proposal_hash=self.proposal_hash, version=2)
        if self.module_protocol == 1:
            inputs['codex_literature_protocol'] = 1
        if self.search_profile:
            # New explicit acquisition identity; never reinterpret old plans.
            inputs['search_profile'] = self.search_profile
        from . import venue_policy
        if venue_policy.enabled(self.store.get('runs', run_id)):
            inputs['venue_policy'] = venue_policy.policy(self.store.get('runs', run_id))
            inputs['retrieval_protocol'] = 2
        anchor = f'{run_id}:survey:plan:{_hash(inputs)}'

        def create(existing):
            if existing:
                if existing.get('inputs') != inputs or existing.get('run_id') != run_id:
                    raise ValueError('Survey checkpoint ownership or input mismatch')
                return existing
            date = self.date_policy['as_of_date'] if self.date_policy else datetime.now(timezone.utc).date().isoformat()
            year, schedule = int(date[:4]), []

            def add(base, facet, *, suffix='', recent=False):
                schedule.append({'index': len(schedule), 'base_query': base,
                    'query': (base.strip() + (' ' + suffix if suffix else '')),
                    'facet': facet, 'sort': 'recent' if recent else 'relevance',
                    'year_from': year - 2 if recent else None, 'year_to': year if recent else None,
                    'limit': self.per_query_limit})

            # First cover every user query with both established and recent records.
            for query in queries:
                add(query, 'domain_relevance')
                add(query, 'recent_progress', recent=True)
            # These are retrieval facets, not assertions that a paper is a baseline or a limitation.
            add(queries[0], 'foundational_baselines', suffix='baseline')
            add(queries[-1], 'recent_methods', suffix='method', recent=True)
            add(queries[(len(queries) - 1) // 2], 'alternatives_limitations', suffix='limitations')
            add(queries[0], 'reviews', suffix='review')
            if self.phase == 'idea':
                schedule = []
                for query in queries:
                    add(query, 'latest_idea_methods', recent=True)
                    add(query, 'latest_idea_architectures', suffix='architecture', recent=True)
                    add(query, 'latest_idea_limitations', suffix='limitations', recent=True)
                for item in schedule:
                    item.update(date_from=self.date_policy['date_from'], date_to=self.date_policy['date_to'],
                        year_from=int(self.date_policy['date_from'][:4]), year_to=int(date[:4]))
            elif self.phase == 'related_work' and self.search_profile != 'novelty':
                for item in schedule:
                    # Relevance and mechanism overlap take priority, with no age exclusion.
                    item.update(sort='relevance', year_from=None, year_to=None)
            if inputs.get('venue_policy', {}).get('enabled'):
                # Broaden over-constrained conjunctive queries while retaining
                # the quoted research subject. The same hard venue/date checks
                # still apply; this spends existing batches, not extra budget.
                for index, query in enumerate(queries):
                    from .literature_relevance import subject_query
                    subject = subject_query(query)
                    if subject != query:
                        slot = min(len(schedule) - 1, index * 3 + 2)
                        schedule[slot].update(query=subject, base_query=subject, facet='ccf_subject_recall')
            schedule = schedule[:self.max_batches]
            frozen = {**inputs, 'as_of_date': date, 'batches': schedule}
            return {'id': anchor, 'run_id': run_id, 'kind': 'survey_plan', 'inputs': inputs,
                    'plan_id': _hash(frozen), 'query_plan': frozen, 'created_at': now()}

        record = self.store.mutate('literature_inputs', anchor, create)
        if record.get('plan_id') != _hash(record['query_plan']):
            raise ValueError('Frozen survey plan digest mismatch')
        return record['query_plan'], record['plan_id']

    async def collect(self, run_id, queries, source_service):
        """Acquire metadata only. source_service is the existing LiteratureService."""
        plan, plan_id = self._plan(run_id, queries)
        checkpoints = []
        for item in plan['batches']:
            self._guard(run_id)
            key = f'{run_id}:survey:{plan_id}:query:{item["index"]}'
            checkpoint = self.store.get('literature_inputs', key)
            if checkpoint:
                if (checkpoint.get('run_id') != run_id or checkpoint.get('plan_id') != plan_id
                        or checkpoint.get('query_plan_item') != item or checkpoint.get('status') != 'completed'
                        or checkpoint.get('result_sha256') != _hash([checkpoint.get('papers'), checkpoint.get('diagnostics')])):
                    raise ValueError('Survey query checkpoint is inconsistent')
            else:
                try:
                    report = await source_service.search_with_report(item['query'], limit=item['limit'],
                        sort=item['sort'], year_from=item['year_from'], year_to=item['year_to'],
                        **({'ccf_ab_only': True} if plan.get('venue_policy', {}).get('enabled') else {}),
                        **({k: item[k] for k in ('date_from', 'date_to')} if 'date_from' in item else {}))
                    papers = report['papers']
                    if not isinstance(papers, list) or any(not isinstance(p, dict) for p in papers):
                        raise ValueError('Literature provider returned invalid metadata')
                    papers = papers[:item['limit']]
                    diagnostics = report['diagnostics']
                except (RunDeletedError, KeyboardInterrupt, SystemExit):
                    raise
                except Exception as exc:
                    # A completed failed batch is durable too. Resuming does not silently hammer it.
                    papers = []
                    diagnostics = {'errors': [{'source': 'search', 'error': type(exc).__name__,
                        'status_code': getattr(getattr(exc, 'response', None), 'status_code', None)}], 'sources': {}}
                self._guard(run_id)
                checkpoint = {'id': key, 'run_id': run_id, 'kind': 'survey_query', 'plan_id': plan_id,
                    'query_plan_item': item, 'papers': papers, 'diagnostics': diagnostics,
                    'result_sha256': _hash([papers, diagnostics]), 'status': 'completed', 'completed_at': now()}
                self.store.put('literature_inputs', key, checkpoint)
            checkpoints.append(checkpoint)
        self._guard(run_id)
        return self._summarize(plan, plan_id, checkpoints)

    def _summarize(self, plan, plan_id, checkpoints):
        as_of_year = int(plan['as_of_date'][:4])
        groups, query_rows, errors = [], [], []
        sources = {s: {'queries_attempted': 0, 'queries_succeeded': 0, 'queries_failed': 0,
                       'returned_count': 0, 'distinct_count': 0} for s in SOURCES}
        for checkpoint in checkpoints:
            item, diagnostic = checkpoint['query_plan_item'], checkpoint['diagnostics']
            row = {k: item[k] for k in ('index', 'query', 'facet', 'sort', 'year_from', 'year_to')}
            row.update(status='completed', result_count=len(checkpoint['papers']), accepted_count=0,
                       rejected_irrelevant_count=0, rejected_date_count=0, rejected_venue_count=0, cache_hit=bool(diagnostic.get('cache_hit')))
            if 'rag' in diagnostic:
                row.update(rag=diagnostic['rag'], network_searched=diagnostic.get('network_searched', False))
            for source, status in diagnostic.get('sources', {}).items():
                if source in sources:
                    sources[source]['queries_attempted'] += 1
                    sources[source]['queries_failed' if status.get('status') == 'error' else 'queries_succeeded'] += 1
            for error in diagnostic.get('errors', []):
                errors.append({'query_index': item['index'], **{k: error.get(k) for k in ('source', 'error', 'status_code')}})
            if diagnostic.get('errors'):
                row['status'] = 'partial_failure' if checkpoint['papers'] else 'failed'
            for paper in checkpoint['papers']:
                if plan.get('venue_policy', {}).get('enabled'):
                    from .venue_policy import assess
                    if not assess(paper)['eligible']:
                        row['rejected_venue_count'] += 1
                        continue
                rag_match = paper.get('rag_version_id') and paper.get('rag_score', 0) >= .38
                if not paper.get('id') or not paper.get('title') or not (rag_match or _search_relevance(item['base_query'], paper)['accepted']):
                    row['rejected_irrelevant_count'] += 1
                    continue
                year = _year(paper.get('year'))
                if plan.get('phase') == 'idea' and not is_recent(paper, plan['date_policy']):
                    row['rejected_date_count'] += 1
                    continue
                if item['sort'] == 'recent' and (year is None or not item['year_from'] <= year <= item['year_to']):
                    row['rejected_date_count'] += 1
                    continue
                row['accepted_count'] += 1
                source = paper.get('source')
                if source in sources:
                    sources[source]['returned_count'] += 1
                aliases = _aliases(paper)
                matched = [group for group in groups if group['aliases'] & aliases]
                merged = {'aliases': set(aliases), 'records': [(paper, item['index'])]}
                for group in matched:
                    merged['aliases'].update(group['aliases'])
                    merged['records'].extend(group['records'])
                    groups.remove(group)
                groups.append(merged)
            query_rows.append(row)
        if plan.get('codex_literature_protocol') == 1:
            from .codex_literature.retrieval.dedup import deduplicate
            merged_records = deduplicate([{**p, 'matched_queries': [i]} for g in groups for p, i in g['records']])
        else:
            # Frozen surveys retain their original alias-merging policy.
            merged_records = []
            for group in groups:
                records = group['records']
                selected = max((p for p, _ in records), key=lambda p: (_verified(p), bool(p.get('fulltext_url')),
                    len(str(p.get('abstract') or '')), str(p.get('id'))))
                merged = copy.deepcopy(selected)
                merged['identity_aliases'] = sorted({str(p['id']) for p, _ in records})
                merged['matched_queries'] = sorted({i for _, i in records})
                merged['sources'] = sorted({str(p.get('source')) for p, _ in records if p.get('source')})
                years = sorted({_year(p.get('year')) for p, _ in records if _year(p.get('year')) is not None})
                merged['metadata_conflicts'] = {'publication_years': years} if len(years) > 1 else {}
                merged_records.append(merged)
        papers = []
        for merged in merged_records:
            for source in merged['sources']:
                if source in sources:
                    sources[source]['distinct_count'] += 1
            papers.append(compact_paper(merged))
        if plan.get('codex_literature_protocol') == 1:
            from .codex_literature.retrieval.ranking import rank_papers
            papers = rank_papers(papers, plan['queries'], mode='recent' if plan.get('phase') == 'idea' else 'relevance',
                                 as_of_year=as_of_year)
        elif plan.get('phase') == 'related_work':
            # Preserve query/rank order rather than promoting newer but less
            # relevant records. The subsequent reading analysis judges overlap.
            ranks = {}
            for checkpoint in checkpoints:
                for rank, paper in enumerate(checkpoint['papers']):
                    ranks.setdefault(paper.get('id'), (checkpoint['query_plan_item']['index'], rank))
            papers.sort(key=lambda p: (not _verified(p), min(
                (ranks[alias] for alias in p.get('identity_aliases', []) if alias in ranks), default=(999, 999))))
        else:
            papers.sort(key=lambda p: (not _verified(p), -(p['year'] or 0), p['title'], str(p['id'])))
        if plan.get('phase') == 'idea':
            papers = sorted([p for p in papers if is_recent(p, plan['date_policy'])], key=lambda p: p['publication_date'], reverse=True)
        verified = [p for p in papers if _verified(p)]
        recent = [p for p in papers if p['year'] and as_of_year - 2 <= p['year'] <= as_of_year]
        recent_verified = [p for p in recent if _verified(p)]
        known_years = [p['year'] for p in papers if p['year'] and p['year'] <= as_of_year]
        verified_years = [p['year'] for p in verified if p['year'] and p['year'] <= as_of_year]
        gaps = []

        def gap(code, message):
            gaps.append({'code': code, 'message': message})

        if len(papers) < self.target_distinct:
            gap('below_target', f'{len(papers)} distinct relevant records retrieved; target is {self.target_distinct}. Do not pad with irrelevant work.')
        if len(verified) < len(papers):
            gap('unverified_metadata', f'{len(papers) - len(verified)} records did not pass identifier/title/author/year verification.')
        if not recent_verified:
            gap('no_verified_recent_records', 'No verified relevant records in the recent window; latest progress remains unestablished.')
        if not any(p['year'] == as_of_year for p in verified):
            gap('no_verified_current_year_records', 'No verified current-year records retrieved; this does not establish that no newer work exists.')
        if errors:
            gap('source_failures', 'Some metadata queries failed or were partially unavailable; coverage is incomplete.')
        if any(not row['accepted_count'] for row in query_rows):
            gap('empty_query_facets', 'Some planned query facets returned no relevant records.')
        if any(p['year'] is None for p in papers):
            gap('unknown_dates', 'Some records lack a valid publication year and cannot establish recency.')
        if any(p['year'] and p['year'] > as_of_year for p in papers):
            gap('future_dates', 'Future-dated metadata is retained but excluded from latest/recent coverage counts.')
        if any(p['metadata_conflicts'] for p in papers):
            gap('conflicting_dates', 'Deduplicated source records disagree on publication year; inspect original metadata.')
        gap('year_precision_only', 'Publication years cannot establish the newest day/month or complete current-year coverage.')
        gap('full_text_not_reviewed', 'This survey retrieves metadata only; full-text acquisition and critical reading are separate, auditable steps.')
        coverage = {'plan_id': plan_id, 'as_of_date': plan['as_of_date'],
            'recent_window': {'year_from': as_of_year - 2, 'year_to': as_of_year, 'date_granularity': 'year'},
            'target_distinct': self.target_distinct, 'distinct_count': len(papers), 'verified_count': len(verified),
            'verified_ids': [p['id'] for p in verified], 'recent_count': len(recent),
            'recent_verified_count': len(recent_verified), 'latest_publication_year': max(known_years, default=None),
            'latest_verified_publication_year': max(verified_years, default=None),
            'date_granularity': 'year', 'query_count': len(plan['batches']), 'completed_query_count': len(checkpoints),
            'queries': query_rows, 'sources': sources, 'retrieval_errors': errors, 'gaps': gaps,
            'exhaustive': False, 'full_text_read_count': 0,
            'coverage_note': 'Bounded multi-source metadata search with lexical relevance and identity checks. Reaching the numerical target is not evidence of exhaustive coverage, novelty, or validated scientific claims.'}
        if plan.get('venue_policy'):
            coverage['venue_policy'] = plan['venue_policy']
        if plan.get('phase'):
            coverage.update(phase=plan['phase'], proposal_hash=plan.get('proposal_hash'))
        if plan.get('phase') == 'idea':
            coverage.update(recent_window={**plan['date_policy'], 'year_from': int(plan['date_policy']['date_from'][:4]),
                'year_to': as_of_year, 'date_granularity': 'day'}, date_granularity='day',
                recent_count=len(papers), recent_verified_count=len(verified),
                latest_publication_date=max((p['publication_date'] for p in papers), default=None))
            coverage['gaps'] = [g for g in gaps if g['code'] != 'year_precision_only']
            coverage['gaps'].append({'code': 'bounded_recent_search', 'message': 'First-round core papers require a verified calendar publication date within the last 12 months. No older or unknown-date substitutes; the search is not exhaustive.'})
        return {'papers': papers, 'coverage': coverage, 'query_plan': {**plan, 'plan_id': plan_id}}
