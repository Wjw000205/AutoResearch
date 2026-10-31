"""Repair literature selection without confusing identity with date eligibility.

Original model decisions remain immutable. A scout corrects its own analysis in
bounded, durable child assignments; a correction never substitutes for reading.
"""
from __future__ import annotations

import copy
from urllib.parse import unquote

from . import literature_rounds, venue_policy, literature_relevance
from .storage import now


def current_papers(engine, run_id, papers):
    from .literature_reading import _run_papers, _verified
    from .reference_papers import is_uploaded_paper, is_shared_uploaded_paper
    merged = {p['id']: p for p in papers if _verified(p) and
              (p.get('identity_kind') != 'uploaded_document' or is_uploaded_paper(engine.store, run_id, p)
               or is_shared_uploaded_paper(engine.store, p))}
    # Prefer the task's current verified metadata over stale frozen catalog rows.
    merged.update(_run_papers(engine.store, run_id))
    return list(merged.values())


def references(analysis):
    result = list(analysis.get('paper_ids', []))
    plan = analysis.get('reading_plan') or {}
    for key in ('core_paper_ids', 'pending_paper_ids'):
        result.extend(plan.get(key, []))
    result.extend(row['paper_id'] for row in analysis.get('related_papers', []))
    return list(dict.fromkeys(result))


def _identity(value):
    value = unquote(str(value)).strip().lower()
    for prefix in ('https://doi.org/', 'http://doi.org/', 'https://dx.doi.org/', 'doi:'):
        if value.startswith(prefix):
            return value[len(prefix):].strip()
    return value


def metadata_refresh_candidates(known, references, current_ids, query, limit=60):
    cited = set(references)
    ordered = sorted(known, key=lambda p: (p['id'] in cited,
        literature_relevance.domain_match(query, p), p['id'] in current_ids,
        p.get('publication_date') or ''), reverse=True)
    return [p for p in ordered if not p.get('verification', {}).get('venue_metadata')][:limit]


def inspect(engine, run_id, analysis, papers):
    known = {p['id']: p for p in current_papers(engine, run_id, papers)}
    in_date_venue = literature_rounds.filter_papers(engine.store, run_id, list(known.values()))
    eligible = {p['id']: p for p in literature_relevance.filter_task(engine, run_id, in_date_venue)}
    aliases = {}
    for p in known.values():
        for value in (p['id'], p.get('doi'), p.get('url')):
            if value:
                aliases.setdefault(_identity(value), set()).add(p['id'])
    mapping, issues = {}, []
    for cited in references(analysis):
        matches = {cited} if cited in known else aliases.get(_identity(cited), set())
        if len(matches) != 1:
            issues.append({'paper_id': cited, 'reason': 'identity_unverified_or_ambiguous'})
            continue
        canonical = next(iter(matches))
        if canonical not in eligible:
            p = known[canonical]
            venue = venue_policy.assess(p)
            issues.append({'paper_id': cited, 'canonical_id': canonical,
                'publication_date': p.get('publication_date'),
                'ccf': venue,
                'reason': 'outside_research_domain' if not literature_relevance.domain_match(literature_relevance.task_query(engine, run_id), p) else
                    venue['reason'] if venue_policy.enabled(engine.store.get('runs', run_id)) and not venue['eligible'] else
                    'publication_date_missing' if not literature_rounds.publication_date(p) else 'outside_recent_window'})
        elif cited != canonical:
            mapping[cited] = canonical
    corrected = copy.deepcopy(analysis)
    for owner, key in [(corrected, 'paper_ids'), (corrected.get('reading_plan', {}), 'core_paper_ids'),
                       (corrected.get('reading_plan', {}), 'pending_paper_ids')]:
        if key in owner:
            owner[key] = list(dict.fromkeys(mapping.get(v, v) for v in owner[key]))
    for row in corrected.get('related_papers', []):
        row['paper_id'] = mapping.get(row['paper_id'], row['paper_id'])
    return corrected, list(eligible.values()), issues, mapping


def scoped_evidence(engine, run_id, snapshot):
    eligible = {p['id'] for p in literature_relevance.filter_task(engine, run_id,
        literature_rounds.filter_papers(engine.store, run_id, current_papers(engine, run_id, [])))}
    result = copy.deepcopy(snapshot)
    result['verified_papers'] = [p for p in result.get('verified_papers', []) if p.get('id') in eligible]
    result['literature_notes'] = [n for n in result.get('literature_notes', []) if n.get('paper_id') in eligible]
    source_ids = {n['source_id'] for n in result['literature_notes'] if n.get('source_id')}
    for row in engine.store.list('run_evidence', run_id):
        if row.get('paper', {}).get('id') in eligible and row.get('reading', {}).get('source_id'):
            source_ids.add(row['reading']['source_id'])
    result['sources'] = [s for s in result.get('sources', []) if s.get('source_id') in source_ids]
    result['scope_note'] = ('列出符合本轮范围的注册身份已核验论文及用户选定上传原文。上传仅核验字节身份与来源，'
                            '不代表DOI、发表出处或科研结论已核验，旧seed不计近期工作。')
    return result


INSTRUCTION = (
    'outside_research_domain表示与研究领域无关，必须替换；不能仅因期刊等级合格而凑入核心。'
    '你先前的文献分析有范围或编号问题，请作为负责文献的角色自行修正完整分析与reading_plan。'
    'selection_issues给出确切原因：publication_date_missing是身份已核验但日期缺失，'
    'outside_recent_window是已核验但不在本轮近期范围，identity_unverified_or_ambiguous是尚无可靠身份对应。'
    '不要把这些不同问题统称为未核验。第一轮的联网近期论文只能使用日期明确且在literature_date_policy窗口内的论文；'
    'eligible_papers中用户明确上传的identity_kind=uploaded_document论文须保留并逐篇阅读，不受年代/出处/领域筛选限制；'
    '上传原文只证明字节身份，不能写成发表身份已核验或近期进展。'
    '不要从年份猜月份，也不能给旧论文补造近期日期。已核验但日期不明/过旧的论文可留待第二轮相关工作，'
    '不得作为本轮paper_ids或核心/待阅读计划。优先从eligible_papers按研究问题重选，至少min(5,合格论文数)篇相关核心。'
    '确实需要核实身份/日期时可用literature.verify或近期检索；补入的论文仍须通过本任务核验与本轮日期校验。'
    '重写摘要和selection_rationale，清除“所有日期合格”等错误说法，不能仅删除编号而保留不受支持的结论。'
    '复用持久RAG及已保存笔记，按索引读取缺失证据，不能把修正名单说成完成阅读。'
    '正文、笔记仍未读全就如实标记pending，随后系统会继续分配补读任务；不要求用户修复编号。'
    '返回原schema要求的完整结构，后续阅读与证据门禁继续生效。'
)


async def reconcile(engine, harness, run_id, analysis, papers, *, source_handoff_id, context, schema):
    from .engine import ResearchBlocked, digest
    store = engine.store
    run = store.get('runs', run_id)
    scope = venue_policy.policy(run)
    key = f'{run_id}:literature-scope:{digest([source_handoff_id, scope, literature_relevance.VERSION])}'
    saved = store.get('stage_contexts', key)
    candidate = saved['analysis'] if saved and saved.get('status') == 'completed' else analysis
    corrected, eligible, issues, aliases = inspect(engine, run_id, candidate, papers)
    if not issues:
        if aliases and not saved:
            saved = {'id': key, 'run_id': run_id, 'kind': 'literature_scope_repair',
                'source_handoff_id': source_handoff_id, 'effective_handoff_id': source_handoff_id,
                'original_hash': digest(analysis), 'analysis': corrected, 'aliases': aliases,
                'status': 'completed', 'created_at': now(), 'attempts': []}
            store.put('stage_contexts', key, saved)
        return corrected, eligible, (saved or {}).get('effective_handoff_id', source_handoff_id)
    if not saved:
        if scope['enabled']:
            # Upgrade historical task/RAG metadata using registry lookups. Never
            # turn a supplied rank into verification, or discard original notes.
            import asyncio
            known = current_papers(engine, run_id, papers)
            pending = metadata_refresh_candidates(known, references(candidate), {p['id'] for p in papers},
                                                  literature_relevance.task_query(engine, run_id))
            for offset in range(0, len(pending), 5):
                engine.check_active(run_id)
                refreshed = await asyncio.gather(*(engine.literature.verify(p) for p in pending[offset:offset + 5]))
                for p in refreshed:
                    if p.get('verified'):
                        evidence_id = f'{run_id}:{p["id"]}'
                        store.mutate('run_evidence', evidence_id, lambda old, p=p, eid=evidence_id:
                            {**(old or {}), 'id': eid, 'run_id': run_id, 'paper': p})
            queries = engine.outputs(run_id).get('intake', {}).get('search_queries')
            if queries:
                survey = await engine.collect_literature(run_id, queries[:3])
                papers = [*papers, *survey['papers']]
            corrected, eligible, issues, aliases = inspect(engine, run_id, candidate, papers)
            if not issues:
                return corrected, eligible, source_handoff_id
        saved = {'id': key, 'run_id': run_id, 'kind': 'literature_scope_repair',
            'source_handoff_id': source_handoff_id, 'original_hash': digest(analysis),
            'initial_issues': issues, 'venue_policy': scope, 'status': 'repairing', 'created_at': now(), 'attempts': []}
        store.put('stage_contexts', key, saved)
        store.event(run_id, 'literature.scope_repair_started', '文献研究员正在自动核对编号、发表出处与日期，并修正本轮阅读名单。',
                    {'repair_id': key, 'issues': issues})
    for number in range(2):
        engine.check_active(run_id)
        if len(saved['attempts']) <= number:
            child = {k: copy.deepcopy(v) for k, v in context.items() if k.endswith('_protocol') or k.endswith('_index')
                     or k in {'objective', 'research_brief', 'research_state', 'execution_constraints', 'hardware', 'literature_date_policy', 'selected_idea'}}
            child.update(literature_scope_protocol=2, original_analysis=candidate, selection_issues=issues,
                literature_venue_policy=scope,
                eligible_papers=[{k: p.get(k) for k in ('id', 'title', 'doi', 'publication_date', 'year', 'verified', 'venue', 'ccf',
                    'identity_kind', 'source_reference_id', 'bibliographic_identity_verified', 'verification')}
                                 for p in eligible], evidence=scoped_evidence(engine, run_id, engine.evidence_snapshot(run_id)))
            instruction = INSTRUCTION if literature_rounds.phase(run) == 'idea' else (
                '修正第二轮完整分析与reading_plan和related_papers，解决selection_issues中的编号或发表出处问题。'
                '第二轮不限制年代；按与idea实质相关性重选核心论文，缺失阅读须标记pending，不能编造结论。')
            if scope['enabled']:
                instruction += '\n' + venue_policy.INSTRUCTION + ' venue_unverified/venue_not_listed表示出处未核实或未收录；ccf_rank_excluded表示C类；excluded_publication_track表示非合格发表类型。'
            assignment = {'purpose': f'literature:scope-repair:{digest(key)[:24]}:{number+1}',
                'instruction': instruction, 'context': child, 'schema': schema}
            saved['attempts'].append(assignment)
            store.put('stage_contexts', key, saved)
        assignment = saved['attempts'][number]
        candidate = await harness.ask(run_id, 'scout', assignment['purpose'], assignment['instruction'], assignment['context'], assignment['schema'])
        corrected, eligible, issues, aliases = inspect(engine, run_id, candidate, papers)
        if not issues:
            handoff_id = harness.decision_key(run_id, 'scout', assignment['purpose'], assignment['instruction'], assignment['context'], assignment['schema'])
            saved.update(status='completed', analysis=corrected, aliases=aliases,
                         effective_handoff_id=handoff_id, completed_at=now())
            store.put('stage_contexts', key, saved)
            store.event(run_id, 'literature.scope_repaired', '文献编号与本轮时间范围已核对，继续读取并验证核心论文笔记。',
                        {'repair_id': key, 'source_handoff_id': source_handoff_id, 'handoff_id': handoff_id})
            return corrected, eligible, handoff_id
    saved.update(status='unresolved', remaining_issues=issues)
    store.put('stage_contexts', key, saved)
    raise ResearchBlocked('文献研究员已自动修正两次，仍有编号、发表出处或日期范围问题；原回复与逐篇原因已保存，停止重复消耗预算。')


def prior_assignment(engine, run_id, purpose, common, schema):
    """Reuse a paid analysis of this same brief; scope corrections are children."""
    rows = sorted(engine.store.list('handoffs', run_id), key=lambda r: r.get('created_at', ''), reverse=True)
    for row in rows:
        if row.get('purpose') != purpose or row.get('status') != 'completed':
            continue
        saved = engine.store.get('handoff_inputs', row['id'])
        if not saved or saved.get('schema') != schema:
            continue
        old = saved.get('context', {})
        if all(old.get(k) == v for k, v in common.items() if k not in {'evidence', 'literature_venue_policy'}):
            return saved
    return None
