"""Read the shared literature before proposing; reject overlapping ideas durably.

These are prospective, bounded literature judgments, never proof of novelty.
All inference uses the existing role harness, receipt recovery and budget gates.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from .storage import now

ROLES = ('director', 'engineer', 'scout')
TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 2400}


def enabled(run):
    return run.get('config', {}).get('innovation_review_protocol') == 1


def attempt(store, run_id):
    run = store.get('runs', run_id)
    cycle = run.get('state', {}).get('research_cycle', 1)
    return (store.get('innovation_sessions', f'{run_id}:{cycle}') or {}).get('attempt', 1)


def suffix(store, run_id):
    return f':idea-attempt:{attempt(store, run_id)}' if enabled(store.get('runs', run_id)) else ''


def load_index(engine, run_id, index):
    from .engine import ResearchBlocked
    record = engine.store.get('artifacts', (index or {}).get('artifact_id', ''))
    if not record or record.get('run_id') != run_id:
        raise ResearchBlocked('创新讨论所需的文献索引尚未保存')
    path = Path(record['path']).resolve()
    if not path.is_relative_to(engine.run_dir(run_id)):
        raise ResearchBlocked('文献索引路径不属于当前任务')
    content = path.read_text(encoding='utf-8')
    if hashlib.sha256(content.encode('utf-8')).hexdigest() != index.get('sha256'):
        raise ResearchBlocked('文献索引内容已变化，需要重新生成索引')
    return json.loads(content), len(content)


async def seed_reading(engine, harness, run_id, purpose, instruction, context, schema, *, roles=ROLES,
                       index=None, unstarted_only=False):
    """Persist actual role-scoped tool receipts before any candidate/review call.

    Only the manifest travels between agents. New compiler-enabled assignments
    receive a hash-bound catalog directory; legacy assignments receive pages.
    An explicit index preserves the caller's assignment identity. With
    unstarted_only, a restart never appends to an existing request or receipt.
    """
    explicit_index = index is not None
    index = index if explicit_index else context.get('required_literature_index')
    if index is None:
        return
    if unstarted_only:
        from .engine import Harness
        roles = tuple(role for role in roles if not engine.store.get('decisions',
            Harness.decision_key(run_id, role, purpose, instruction, context, schema))
            and engine.store.get('handoff_progress',
                Harness.decision_key(run_id, role, purpose, instruction, context, schema)) is None)
        if not roles:
            return  # Never append navigation to a started or frozen request.
    await _seed_index(engine, harness, run_id, purpose, instruction, context, schema, index, roles=roles)
    if explicit_index:
        return
    if context.get('rejection_index'):
        await _seed_index(engine, harness, run_id, purpose, instruction, context, schema, context['rejection_index'], roles=roles)
    if context.get('innovation_repair_index'):
        await _seed_index(engine, harness, run_id, purpose, instruction, context, schema, context['innovation_repair_index'], roles=roles)


async def _seed_index(engine, harness, run_id, purpose, instruction, context, schema, index, *, roles=ROLES):
    from .engine import Harness, ResearchBlocked, digest
    document, total = load_index(engine, run_id, index)
    for role in roles:
        key = Harness.decision_key(run_id, role, purpose, instruction, context, schema)
        harness._handoff(run_id, role, purpose, instruction, context, schema)
        progress = engine.store.get('handoff_progress', key, {'seen': {}, 'turn': 0, 'tool_results': []})
        # New assignments receive a directory, not automatically replayed pages
        # of every paper/note. Paid or already seeded assignments stay immutable.
        is_catalog = isinstance(document, dict) and {'analysis', 'reading_gate', 'papers', 'notes'} <= document.keys()
        compiled = [item for item in progress['tool_results'] if item.get('call', {}).get('name') == 'evidence.catalog'
                    and item.get('call', {}).get('arguments', {}).get('artifact_id') == index['artifact_id']]
        if compiled:
            if compiled[0]['result']['catalog']['sha256'] != index['sha256']:
                raise ResearchBlocked('目录版本已变化；原分工读取记录保留')
            continue
        if (is_catalog and engine.store.get('runs', run_id)['config'].get('context_compiler_version') in (1, 2, 3)
                and not progress['tool_results'] and not progress.get('normal_request')
                and not engine.store.get('decisions', key)):
            call = {'name': 'evidence.catalog', 'arguments': {'artifact_id': index['artifact_id'],
                'expected_sha256': index['sha256'], 'purpose': purpose,
                'query': json.dumps(context.get('selected_idea', context.get('proposal', context.get('objective', ''))), ensure_ascii=False)}}
            result = await harness.execute_controller_tool(run_id, role, call['name'], call['arguments'],
                purpose=f'innovation-catalog:{purpose}', parent_handoff_id=key)
            progress['tool_results'].append({'call': call, 'result': result})
            progress['seen'][digest(call)] = 1
            harness._save_progress(run_id, key, progress)
            receipt_key = key if index == context.get('required_literature_index') else f'{key}:{index["sha256"]}'
            engine.store.put('innovation_readings', receipt_key, {'id': receipt_key, 'handoff_id': key,
                'run_id': run_id, 'role': role, 'purpose': purpose, 'artifact_id': index['artifact_id'],
                'sha256': index['sha256'], 'total_chars': total, 'delivered_chars': 0, 'completed': True,
                'delivery': 'catalog_directory', 'primary_source_reading': False, 'created_at': now()})
            harness._sync_tool_sources(key, progress['tool_results'])
            continue
        coverage = 0
        for item in progress['tool_results']:
            result = item.get('result', {})
            if (item.get('call', {}).get('name') == 'artifacts.read'
                    and result.get('artifact_id') == index['artifact_id']
                    and result.get('sha256') == index['sha256'] and result.get('offset') == coverage):
                coverage += len(result.get('text', ''))
        if coverage < total and (progress.get('normal_request') or engine.store.get('decisions', key)):
            raise ResearchBlocked('已有请求未完成指定文献读取，保留原始请求等待核验')
        while coverage < total:
            engine.check_active(run_id)
            call = {'name': 'artifacts.read', 'arguments': {'id': index['artifact_id'],
                'offset': coverage, 'limit': 16000, 'expected_sha256': index['sha256']}}
            result = await harness.execute_controller_tool(run_id, role, call['name'], call['arguments'],
                purpose=f'innovation-index-page:{purpose}', parent_handoff_id=key)
            if not result.get('text'):
                raise ResearchBlocked('文献索引读取未产生有效内容')
            progress['tool_results'].append({'call': call, 'result': result})
            progress['seen'][digest(call)] = 1
            coverage += len(result['text'])
            harness._save_progress(run_id, key, progress)
        harness._sync_tool_sources(key, progress['tool_results'])
        receipt_key = key if index == context.get('required_literature_index') else f'{key}:{index["sha256"]}'
        receipt = {'id': receipt_key, 'handoff_id': key, 'run_id': run_id, 'role': role, 'purpose': purpose,
            'artifact_id': index['artifact_id'], 'sha256': index['sha256'], 'total_chars': total,
            'delivered_chars': coverage, 'completed': coverage == total, 'created_at': now()}
        if not engine.store.get('innovation_readings', receipt_key):
            engine.store.put('innovation_readings', receipt_key, receipt)


def candidate_schema(base, context):
    schema = copy.deepcopy(base)
    ids = context.get('recent_paper_ids', [])
    schema['properties']['research_progress'] = {
        'type': 'object', 'additionalProperties': False,
        'properties': {'summary': TEXT, 'remaining_gap': TEXT,
            'paper_ids': {'type': 'array', 'minItems': 1, 'uniqueItems': True,
                          'items': {'enum': ids}}},
        'required': ['summary', 'remaining_gap', 'paper_ids']}
    schema['required'].append('research_progress')
    return schema


async def review(engine, harness, run_id, result):
    from .innovation_repair import enabled as constructive
    if constructive(engine.store.get('runs', run_id)):
        from .innovation_evidence import review_with_supplements
        return await review_with_supplements(engine, harness, run_id, result, _review_once)
    return await _review_once(engine, harness, run_id, result)


def review_route(gate, result):
    """An unresolved overlap risk is not a grounded finding of duplication."""
    if gate['approved']:
        return 'continue'
    ids = result.get('reading_gate', {}).get('noted_core_ids', [])
    locators = {}
    for note in result.get('notes', []):
        if note['paper_id'] in ids:
            locators.setdefault(note['paper_id'], set()).update(e['locator'] for e in note.get('excerpts', []))
    for alias in gate.get('validated_locator_aliases', []):
        if alias['current_locator'] in locators.get(alias['paper_id'], set()):
            locators[alias['paper_id']].add(alias['original_locator'])
    votes = gate['votes']
    grounded_rejection = any(v['proposal_hash'] == gate['proposal_hash']
        and v['conclusion'] in {'duplicate_found', 'high_overlap'}
        and v['closest_work']
        and all(w['paper_id'] in ids and w['evidence_locator'] in locators.get(w['paper_id'], set())
                for w in v['closest_work']) for v in votes)
    if grounded_rejection:
        return 'rethink'
    if (gate.get('evidence_issues')
            or any(v['conclusion'] in {'insufficient_evidence', 'duplicate_found', 'high_overlap'}
                   or any(w['risk'] in {'duplicate', 'high'} for w in v['closest_work']) for v in votes)):
        return 'supplement_evidence'
    return 'rethink'


def comparison_evidence(engine, run_id, result):
    """Accept original RAG locators only through a revalidated adoption receipt.

    Original paid replies keep their original source IDs. A same-hash shared
    note has a new task-local source ID; this is an alias, not a new citation.
    Unrelated or tampered foreign documents never acquire an alias.
    """
    ids = set(result.get('reading_gate', {}).get('noted_core_ids', []))
    locators = {}
    for note in result.get('notes', []):
        if note.get('paper_id') in ids:
            locators.setdefault(note['paper_id'], set()).update(e['locator'] for e in note.get('excerpts', []))
    aliases = []
    for note in engine.paper_rag.reused_notes(run_id, engine.sources.document):
        if note['paper_id'] not in ids:
            continue
        try:
            version = note['reused_from']['rag_version_id']
            old = engine.paper_rag.load(version)['note']
            for original, current in zip(old['excerpts'], note['excerpts']):
                if (original['quote'] == current['quote']
                        and current['locator'] in locators.get(note['paper_id'], set())
                        and original['locator'].replace(old['source_id'] + '#', note['source_id'] + '#', 1)
                        == current['locator']):
                    locators[note['paper_id']].add(original['locator'])
                    aliases.append({'paper_id': note['paper_id'], 'original_locator': original['locator'],
                        'current_locator': current['locator'], 'rag_version_id': version,
                        'text_sha256': note['text_sha256']})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return locators, aliases


def comparison_issues(result, votes, locators):
    skipped = result.get('reading_gate', {}).get('skipped_core_ids', [])
    required_ids = {p['paper_id'] for p in result.get('analysis', {}).get('related_papers', [])} - set(skipped)
    issues = []
    if len(set(result.get('queries', []))) < 2:
        issues.append('缺少针对当前创新点执行的至少两条检索式')
    for vote in votes:
        work = vote.get('closest_work', [])
        if not work or required_ids - {w['paper_id'] for w in work}:
            issues.append(vote['role'] + '未覆盖本轮最相关论文')
        if any(w['evidence_locator'] not in locators.get(w['paper_id'], set()) for w in work):
            issues.append(vote['role'] + '的对比尚未引用已保存笔记中的有效原文定位')
    return issues


def apply_scope_policy(engine, run_id, result, gate):
    """Host routing under explicit search-scope criteria; paid votes stay intact."""
    from . import innovation_policy
    if gate.get('approval_basis') == 'no_high_similarity_in_reviewed_scope':
        return gate
    run = engine.store.get('runs', run_id)
    if (run['config'].get('innovation_policy_version') != innovation_policy.VERSION
            and gate.get('innovation_policy_version') != innovation_policy.VERSION):
        return gate
    session = engine.store.get('innovation_sessions', f'{run_id}:{engine.cycle(run_id)}') or {}
    votes = gate.get('votes', [])
    if (not session.get('proposal_hash') or gate.get('approved') is not False
            or gate.get('proposal_hash') != session['proposal_hash']
            or result.get('selected_idea_hash') != session['proposal_hash']
            or gate.get('attempt') != session.get('attempt', 1)
            or len(votes) != len(ROLES) or {v.get('role') for v in votes} != set(ROLES)
            or any(v.get('proposal_hash') != session['proposal_hash'] for v in votes)
            or result.get('reading_gate', {}).get('approved') is not True
            or not result['reading_gate'].get('noted_core_ids')):
        return gate
    # A reported high similarity needs resolution even if its citation is bad.
    # The policy does not reinterpret an actual duplication finding as absence.
    if any(v.get('conclusion') not in {'differentiated', 'insufficient_evidence'}
           or any(w.get('risk') not in {'low', 'medium'} for w in v.get('closest_work', [])) for v in votes):
        return gate
    if run.get('mode') == 'live':
        from .literature_notes import valid_paper_ids
        if not set(result['reading_gate']['noted_core_ids']) <= valid_paper_ids(engine, run_id):
            return gate
    locators, aliases = comparison_evidence(engine, run_id, result)
    if comparison_issues(result, votes, locators):
        return gate
    limitations = [{'role': v['role'], 'limitations': v.get('limitations', ''),
                    'follow_up_questions': copy.deepcopy(v.get('required_changes', []))} for v in votes]
    return {**copy.deepcopy(gate), 'approved': True, 'route': 'continue', 'routing_version': 3,
        'innovation_policy_version': innovation_policy.VERSION,
        'approval_basis': 'no_high_similarity_in_reviewed_scope', 'approval_provenance': 'host_novelty_policy',
        'model_unanimous_differentiated': all(v['conclusion'] == 'differentiated' for v in votes),
        'previous_host_assessment': {k: copy.deepcopy(gate.get(k)) for k in
            ('approved', 'route', 'routing_version', 'evidence_issues', 'innovation_policy_version')},
        'evidence_issues': [], 'validated_locator_aliases': aliases, 'policy_limitations': limitations,
        'scope': '已执行检索并完成核心片段阅读，在此范围内未发现高度相似完整机制，按用户排重标准放行。'
                 '原始评审意见、未读来源与未验证能力保留；后续仍须三方共识、资源审批和真实实验，不保证全网无重复。',
        'route_correction': {'previous_route': gate.get('route'), 'corrected_at': now(),
            'reason': '已检索范围内没有高度相似完整机制，按用户标准通过新颖性门禁；原始模型意见与局限保留。'}}


def correct_cached_route(engine, run_id, result, gate):
    """Repair only the legacy host routing, preserving paid votes and history."""
    scoped = apply_scope_policy(engine, run_id, result, gate)
    if scoped != gate:
        return scoped
    if gate.get('routing_version') or gate.get('approved') is not False or gate.get('route') != 'rethink':
        return gate
    session = engine.store.get('innovation_sessions', f'{run_id}:{engine.cycle(run_id)}') or {}
    votes = gate.get('votes', [])
    if (not session.get('proposal_hash')
            or gate.get('proposal_hash') != session['proposal_hash']
            or result.get('selected_idea_hash') != session['proposal_hash']
            or gate.get('attempt') != session.get('attempt', 1)
            or len(votes) != len(ROLES) or {v.get('role') for v in votes} != set(ROLES)
            or any(v.get('proposal_hash') != session['proposal_hash'] for v in votes)
            or review_route(gate, result) != 'supplement_evidence'):
        return gate
    return {**copy.deepcopy(gate), 'route': 'supplement_evidence', 'routing_version': 2,
        'route_correction': {'previous_route': gate['route'], 'corrected_at': now(),
            'reason': '旧分流把未解决的高风险当成实质重复；保留原始评审，先定向补证据，不批准实验。'}}


async def _review_once(engine, harness, run_id, result, *, supplement=0):
    from . import innovation
    from .engine import ResearchBlocked
    session = engine.store.get('innovation_sessions', f'{run_id}:{engine.cycle(run_id)}')
    if not session or result.get('selected_idea_hash') != session['proposal_hash']:
        raise ResearchBlocked('相关论文分析与当前创新点版本不一致')
    ids = result.get('reading_gate', {}).get('noted_core_ids', [])
    if not ids or result.get('reading_gate', {}).get('approved') is not True:
        raise ResearchBlocked('相似性审查需要已完成的相关论文阅读记录')
    skipped = result['reading_gate'].get('skipped_core_ids', [])
    schema = {'type': 'object', 'additionalProperties': False, 'properties': {
        'proposal_hash': {'const': session['proposal_hash']},
        'conclusion': {'enum': ['differentiated', 'duplicate_found', 'high_overlap', 'insufficient_evidence']},
        'rationale': TEXT, 'required_changes': {'type': 'array', 'items': TEXT, 'maxItems': 8},
        'closest_work': {'type': 'array', 'minItems': 1, 'maxItems': 12, 'items': {
            'type': 'object', 'additionalProperties': False, 'properties': {
                'paper_id': {'enum': ids}, 'overlap': TEXT, 'difference': TEXT,
                'risk': {'enum': ['duplicate', 'high', 'medium', 'low']}, 'evidence_locator': TEXT},
            'required': ['paper_id', 'overlap', 'difference', 'risk', 'evidence_locator']}},
        'limitations': TEXT},
        'required': ['proposal_hash', 'conclusion', 'rationale', 'required_changes', 'closest_work', 'limitations']}
    context = {'innovation_review_protocol': 1, 'literature_protocol': 2,
        'selected_idea': session['proposal'], 'proposal_hash': session['proposal_hash'],
        'required_literature_index': result['literature_index'],
        'related_work_index': result['literature_index'], 'research_cycle': engine.cycle(run_id),
        'source_tool_protocol': 1, 'pdf_tools_protocol': 1,
        'literature_access_policy': {'skipped_paper_ids': skipped,
            'skipped_papers': result['reading_gate'].get('skipped_papers', []),
            'scope': '来源不可用的论文保留为覆盖局限，不计为已读证据，也不单独阻断流程。'}}
    instruction = (
        '独立审查选定创新点是否与已发表论文实质重复。指定相关论文索引已有持久读取记录，'
        '上下文可能仅展示工作目录、笔记结论和定位，详细摘录保存在RAG与原文存储。'
        '先检查tool_results中的目录和展示范围，再通过memory.search、sources.read或可用的RAG工具按需核对关键原文，'
        '不把目录定位当成本次已读原文，也不例行重载全部论文。逐篇比较研究问题、机制、架构、训练目标与评价条件，'
        '认可针对具体痛点、机制明确且可检验的组合创新与架构改进；不能仅用改名、叙事包装或无实质作用的堆叠证明创新。'
        'closest_work的overlap区分组件已有与整体机制重合，difference说明组合关系或改进对痛点的作用和验证办法；'
        'risk评价整体贡献的重复风险，不能仅因采用已有组件就标high/duplicate。'
        'closest_work必须覆盖analysis.related_papers中除系统已记录跳过外的论文，'
        '来源不可用的论文列入limitations，不要求原文定位，不把访问失败本身当作否决或反复补读的理由；'
        '只能在已读材料范围内判断，不能把跳过推断成不存在重复。'
        '只引用已读论文；evidence_locator必须逐字复制该论文已保存notes.excerpts中的locator，不得自造或改写定位。'
        'duplicate_found/high_overlap须有具体机制对照与原文依据，实质重复才退回重想；insufficient_evidence'
        '表示尚有判断局限，不能因为未检索完整就断言重复。已完成检索与核心阅读且没有发现高度相似完整机制时，'
        '按排重标准选differentiated继续推进；能力增量与未读材料写入limitations。'
        '有具体原文依据的完整机制高度相似才阻断。required_changes给出具体改动方向，保留反证和局限。'
        'required_changes只列阻断当前相似性判断的未解决问题；后续实现细节、尚未授权的实验结果、'
        '执行前的资源和共识审批放入limitations交给相应阶段，不要求文献角色用补读完成实验或设计验收。'
        '这是检索范围内的创新审查，不批准实验，也不保证不存在相同论文。使用中文。')
    purpose = f'innovation_review:{engine.cycle(run_id)}:{session.get("attempt", 1)}'
    if supplement:
        purpose += f':evidence:{supplement}'
    from . import innovation_policy
    run = engine.store.get('runs', run_id)
    if run['config'].get('innovation_policy_version') == innovation_policy.VERSION:
        purpose += f':policy:{innovation_policy.VERSION}'
        context['user_novelty_criteria'] = run['config'].get('innovation_policy_clarification', '')
    from .innovation_routing import assignment
    instruction, context, schema = assignment(engine, run_id, purpose, instruction, context, schema)
    replies = await innovation._batch(engine, harness, run_id, purpose, instruction, context, schema)
    votes = [{'role': role, **reply} for role, reply in zip(ROLES, replies)]
    locators, aliases = comparison_evidence(engine, run_id, result)
    evidence_issues = comparison_issues(result, votes, locators)
    approved = all(v['proposal_hash'] == session['proposal_hash'] and v['conclusion'] == 'differentiated'
        and v['closest_work'] and not v['required_changes']
        and all(w['paper_id'] in ids and w['risk'] not in {'duplicate', 'high'} for w in v['closest_work'])
        for v in votes) and not evidence_issues
    record = {'approved': approved, 'votes': votes, 'proposal_hash': session['proposal_hash'],
        'attempt': session.get('attempt', 1), 'reviewed_at': now(), 'evidence_issues': evidence_issues,
        'access_excluded_paper_ids': skipped, 'validated_locator_aliases': aliases,
        'scope': '基于已检索和实际阅读材料的独立审查；不保证全网无重复，不授予实验权限。'}
    record['route'] = review_route(record, result)
    record['routing_version'] = 2
    if context.get('innovation_policy_version'):
        record['innovation_policy_version'] = context['innovation_policy_version']
    record = apply_scope_policy(engine, run_id, result, record)
    engine.store.put('innovation_sessions', session['id'], {**session, 'novelty_gate': record})
    engine.store.event(run_id, 'innovation.reviewed', '创新审查通过' if record['approved'] else
        '创新审查待补证据，保留当前候选' if record['route'] == 'supplement_evidence' else '创新审查未通过，退回重新思考', record)
    return record


def refresh_stale_literature(engine, run_id):
    """A new day's candidates must not silently use yesterday's date window."""
    from datetime import date
    from .literature_rounds import policy
    store = engine.store
    run = store.get('runs', run_id)
    if not enabled(run):
        return False
    session = store.get('innovation_sessions', f'{run_id}:{engine.cycle(run_id)}')
    if session and session.get('status') == 'completed':
        return False
    window = policy(store, run_id)
    if window['as_of_date'] == date.today().isoformat():
        return False
    old = store.get('steps', f'{run_id}:literature')
    key = f'{run_id}:recent-material:{window["as_of_date"]}'
    store.batch([('innovation_material_archives', key, {'id': key, 'run_id': run_id,
        'date_policy': window, 'literature_step': old, 'archived_at': now()})], deletes=[
        ('literature_inputs', f'{run_id}:literature-date-policy'), ('literature_inputs', run_id),
        ('literature_inputs', f'{run_id}:reading-analysis'), ('steps', f'{run_id}:literature'),
        ('stage_contexts', f'{run_id}:literature'), ('stage_contexts', f'{run_id}:innovation')])
    store.event(run_id, 'innovation.literature_refresh', '创新讨论前更新截至今天的近期论文检索与阅读材料')
    return True


def return_for_rethinking(engine, harness, run_id, output):
    """Archive all original evidence and route a new attempt within the same cycle."""
    from .engine import ResearchBlocked
    from .literature_rounds import persist_index
    store = engine.store
    if output['novelty_gate'].get('route') == 'supplement_evidence':
        raise ResearchBlocked('相似性审查仍需定向补证据；保留当前候选，不计为创新否决或批准实验。')
    session = store.get('innovation_sessions', f'{run_id}:{engine.cycle(run_id)}')
    current = session.get('attempt', 1)
    archive = {**session, 'id': f'{session["id"]}:{current}', 'status': 'rejected',
               'related_work': output, 'novelty_gate': output['novelty_gate']}
    store.put('innovation_attempts', archive['id'], archive)
    if current - session.get('quality_start_attempt', 1) >= 2:
        raise ResearchBlocked('当前候选未通过相似性审查，创新候选尝试额度已用尽（包含前期研究价值审查）；'
                              '已保存候选、相似论文及退回原因，需要调整研究范围后再继续。')
    rejection = persist_index(engine, harness.intelligence, run_id, f'innovation-rejection-{engine.cycle(run_id)}-{current}', archive)
    store.batch([('innovation_sessions', session['id'], {'id': session['id'], 'run_id': run_id,
        'cycle': engine.cycle(run_id), 'attempt': current + 1, 'status': 'rethinking',
        **{k: session[k] for k in ('quality_protocol', 'quality_start_attempt', 'research_brief') if k in session},
        'rejection_index': rejection, 'created_at': session['created_at']})],
        deletes=[('steps', f'{run_id}:innovation'), ('stage_contexts', f'{run_id}:innovation'),
                 ('stage_contexts', f'{run_id}:related_work')])
    store.event(run_id, 'innovation.rethinking', '三位角色将依据相似论文与退回意见重新提出创新点',
                {'attempt': current + 1, 'rejection_index': rejection})


def upgrade_feedback(archive, run):
    """The migration archive stays local; only relevant correction facts are read."""
    return {'reason': archive['upgrade_reason'], 'previous_proposal': archive.get('proposal'),
        'previous_proposal_hash': archive.get('proposal_hash'),
        'literature_index': archive.get('literature_index'),
        'user_resource_limits': {key: run['config'].get(key) for key in
            ('vram_limit_gb', 'cpu_limit_cores', 'ram_limit_gb', 'disk_limit_gb', 'experiment_timeout')},
        'required_changes': ['先读取更新后的近期论文进展，再独立提出创新点并接受相似性审查。',
            '原方案缺少数值资源字段，旧评审误将默认2核/2GB/0.1GB及CPU-only当作用户限制；按真实配置重新声明资源需求。'],
        'scope': '旧讨论完整保存在本地历史中；此索引只交付本次修正所需的材料，不重新打包历史上下文。'}


def upgrade_pending(engine, intelligence, run_id):
    """Explicit maintenance upgrade of an inactive, unapproved existing task.

    Original decisions, paid receipts, papers and notes are never rewritten.
    New assignments start only when the user resumes the paused research.
    """
    from .engine import ResearchBlocked
    from .literature_rounds import persist_index
    store = engine.store
    run = store.get('runs', run_id)
    if enabled(run):
        return run
    if (run['status'] not in {'draft', 'paused', 'blocked', 'failed'}
            or run['stage'] not in {'intake', 'literature', 'innovation', 'related_work', 'debate'}
            or store.get('consensus', run_id, {}).get('unanimous') is True
            or store.list('experiments', run_id)):
        raise ResearchBlocked('只能升级尚未批准实验且当前未运行的研究任务')
    cycle = engine.cycle(run_id)
    session = store.get('innovation_sessions', f'{run_id}:{cycle}')
    updates, deletes = [], []
    if session:
        stages = {stage: store.get('steps', f'{run_id}:{stage}') for stage in ('innovation', 'related_work', 'debate')}
        archive = {**session, 'id': f'{run_id}:{cycle}:legacy', 'attempt': 0, 'status': 'legacy',
            'stage_records': stages, 'related_work': (stages['related_work'] or {}).get('output'),
            'consensus': store.get('consensus', run_id),
            'stage_contexts': {stage: store.get('stage_contexts', f'{run_id}:{stage}') for stage in stages},
            'upgrade_reason': '用户要求三方先阅读近期进展、相似性审查未通过自动重想，并按用户配置声明数值资源计划。'}
        index = persist_index(engine, intelligence, run_id, f'innovation-upgrade-{cycle}', upgrade_feedback(archive, run))
        updates.extend([('innovation_attempts', archive['id'], archive),
            ('innovation_sessions', session['id'], {'id': session['id'], 'run_id': run_id, 'cycle': cycle,
                'attempt': 1, 'status': 'preparing', 'review_protocol': 1, 'rejection_index': index,
                'created_at': now(), 'literature_index': session.get('literature_index')})])
        deletes.extend([('steps', f'{run_id}:{stage}') for stage in stages])
        deletes.append(('consensus', run_id))
    deletes.extend([('stage_contexts', f'{run_id}:{stage}') for stage in ('innovation', 'related_work', 'debate')])
    updated = {**run, 'config': {**run['config'], 'innovation_review_protocol': 1}, 'error': None,
        'stage': 'innovation' if session else run['stage'], 'updated_at': now()}
    updates.append(('runs', run_id, updated))
    store.batch(updates, deletes=deletes)
    store.event(run_id, 'innovation.protocol_upgraded',
        '已启用近期材料同步、三方相似性审查及数值资源计划；旧研究记录保留，恢复后执行新流程。')
    return updated
