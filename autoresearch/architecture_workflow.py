"""Scoped, durable architecture work. Original assignments and votes survive."""
from __future__ import annotations

import copy
import math

from .storage import now

VERSION = 1


def enabled(context):
    return context.get('architecture_workflow_protocol') == VERSION


def tool_limit(context, progress):
    """Continue new source coverage; three unproductive batches end the work."""
    if not enabled(context):
        return None
    state = progress.get('architecture_evidence_progress', {})
    return progress['turn'] if state.get('stagnant_batches', 0) >= 3 else math.inf


def track(context, progress):
    if not enabled(context):
        return
    sources = {d['document_id']: (d.get('physical_memory_document_id', d['document_id']), d['sha256'])
               for d in context['indexed_documents'] if d['kind'] not in {
                   'document_working_notes', 'architecture_author_response', 'architecture_definition_feedback'}}
    spans = {}
    for row in progress.get('tool_results', []):
        if not isinstance(row, dict):
            continue
        call = row.get('call')
        p = row.get('result', {})
        if not isinstance(call, dict) or call.get('name') != 'documents.read' or not isinstance(p, dict):
            continue
        source = sources.get(p.get('document_id'))
        if (source and p.get('sha256') == source[1] and isinstance(p.get('text'), str) and p['text']):
            spans.setdefault(source, []).append((p['char_start'], p['char_end']))
    chars = 0
    for ranges in spans.values():
        end = -1
        for a, b in sorted(ranges):
            chars += max(0, b - max(a, end))
            end = max(end, b)
    old = progress.get('architecture_evidence_progress', {})
    progress['architecture_evidence_progress'] = {'unique_source_chars': chars,
        'stagnant_batches': 0 if chars > old.get('unique_source_chars', 0)
        else old.get('stagnant_batches', 0) + 1}


def prepare(engine, run_id):
    """Explicit stopped-run migration. Keep paid work as immutable predecessors."""
    from . import architecture_definitions as definitions, architecture_revisions as revisions
    from .architecture_definition_completion import PROTOCOL, _directories, check_unit
    from .engine import ResearchBlocked, digest
    run, revision = engine.store.get('runs', run_id), revisions.state(engine, run_id)
    if (run['status'] not in {'paused', 'blocked', 'failed'} or run['mode'] != 'live'
            or run['stage'] != 'design' or not revision or revision['status'] != 'needs_consensus'):
        raise ResearchBlocked('架构流程修复须先暂停当前真实架构任务。')
    engine._consensus_gate(run_id)
    revisions._available(engine, run_id)
    if any((r.get('checkpoint') or {}).get('status') in definitions.TRANSIENT
           for r in engine.store.list('codex_reasoning', run_id)):
        raise ResearchBlocked('仍有活动请求，不能迁移架构流程。')
    old = definitions._checked(engine, run_id, revision['definition_handoff_id'])
    if old.get('architecture_workflow_protocol') == VERSION:
        return definitions.view(engine, run_id)
    if old.get('authoring_protocol') != PROTOCOL:
        raise ResearchBlocked('先准备逐项定义交接，再迁移架构流程。')
    for unit in old.get('issue_drafts', []):
        check_unit(engine, run_id, old, unit)
    hids = [u['handoff_id'] for u in old.get('issue_drafts', [])]
    hids += [v['handoff_id'] for v in old['votes']]
    active = old.get('active_issue_handoff') or {}
    if active.get('handoff_id'):
        hids.append(active['handoff_id'])
    progress = {hid: copy.deepcopy(engine.store.get('handoff_progress', hid, {})) for hid in hids}
    payload = {'record': copy.deepcopy(old), 'progress': progress,
               'completion_policy': {'version': 4, 'architecture_workflow_protocol': VERSION}}
    key = f'{run_id}:architecture-workflow:{engine.cycle(run_id)}:{digest(payload)}'
    docs = [*old['input_documents'], *[u['document'] for u in old.get('issue_drafts', [])]]
    if old.get('document'):
        docs.append(old['document'])
    docs += [p['document_working_notes']['document'] for p in progress.values()
             if p.get('document_working_notes', {}).get('document')]
    record = {field: copy.deepcopy(old[field]) for field in ('run_id', 'version', 'cycle', 'proposal_hash',
        'parent_design_hash', 'source_author_handoff_id', 'source_author_response_sha256',
        'original_author_payload', 'original_author_payload_sha256', 'issues')}
    record.update(id=key, status='prepared', authoring_protocol=PROTOCOL, review_protocol=3,
        architecture_workflow_protocol=VERSION, architecture_progress_protocol=1, issue_ledger_protocol=1,
        design_review_policy_version=1, definition_feedback_protocol=2,
        predecessor_id=old['id'], predecessor_audit_id=key + ':predecessor',
        predecessor_sha256=digest(payload), completion_policy=payload['completion_policy'],
        input_documents=_directories(engine, run_id, docs), issue_drafts=[], votes=[],
        unit_reviews=[], review_amendments=[], created_at=now())
    audit = {'id': record['predecessor_audit_id'], 'run_id': run_id,
        'kind': 'architecture_definition_predecessor', 'sha256': digest(payload),
        'payload': payload, 'created_at': now()}
    engine.store.batch([('stage_contexts', audit['id'], audit), ('architecture_definitions', key, record),
        ('architecture_revisions', revision['id'], {**revision, 'definition_handoff_id': key,
            'definition_handoffs': [*revision.get('definition_handoffs', []), key]}),
        ('runs', run_id, {**run, 'config': {**run['config'], 'architecture_workflow_protocol': VERSION,
                                          'architecture_progress_protocol': 1}})])
    engine.store.event(run_id, 'architecture.workflow_prepared',
        '已保留原交付和读取检查点，安排定向定义与证据恢复流程。',
        {'id': key, 'predecessor_id': old['id'], 'saved_items': len(old.get('issue_drafts', [])),
         'scientific_approval': False})
    return definitions.view(engine, run_id)


async def plan(engine, h, run_id, record):
    """One attributed grouping decision, never heuristic scientific approval."""
    from .engine import STR, STRS, obj, digest, ResearchBlocked
    if record.get('work_plan'):
        check_plan(engine, run_id, record)
        return
    shape = obj({'groups': {'type': 'array', 'minItems': 1, 'maxItems': len(record['issues']),
        'items': obj({'issue_ids': {**copy.deepcopy(STRS), 'minItems': 1, 'maxItems': 3},
                      'reason': STR})}})
    context = {'definition_issues': record['issues'], 'definition_completion_id': record['id'],
               'proposal_hash': record['proposal_hash'], 'scientific_approval': False,
               'indexed_document_context': True, 'document_index_protocol': 1, 'indexed_documents': []}
    instruction = ('仅整理这些已保存意见的处理依赖，不做科学评议或新增设计。'
        '把涉及同一个规范定义、重复要求或必须共同检查的意见归为一组，最多三项一组。'
        '每项原 ID 恰好出现一次，每组按原顺序排列。组中第一项负责唯一规范定义；'
        '其他项只补充未覆盖要求，并引用第一项实际保存的定义，避免重复编写互相竞争的合同。'
        '独立问题单独成组。返回 groups，不读取全文，不登记批准。')
    purpose = 'architecture_definition_work_plan:' + digest(record['id'])
    hid = h._handoff(run_id, 'director', purpose, instruction, context, shape)['id']
    result = await h.ask(run_id, 'director', purpose, instruction, context, shape)
    ids = [i for g in result['groups'] for i in g['issue_ids']]
    expected = [i['id'] for i in record['issues']]
    if len(ids) != len(set(ids)) or set(ids) != set(expected):
        raise ResearchBlocked('定义工作分组必须恰好覆盖原意见，不能添加、遗漏或重复。')
    ordered = sorted(result['groups'], key=lambda g: min(expected.index(i) for i in g['issue_ids']))
    if any(g['issue_ids'] != sorted(g['issue_ids'], key=expected.index) for g in ordered):
        raise ResearchBlocked('定义工作分组的规范定义必须先于引用它的补充项。')
    record['work_plan'] = {'handoff_id': hid, 'result': copy.deepcopy(result), 'groups': ordered}
    engine.store.put('architecture_definitions', record['id'], record)


def check_plan(engine, run_id, record):
    from .engine import ResearchBlocked
    p = record['work_plan']
    decision = engine.store.get('decisions', p['handoff_id'], {})
    frozen = engine.store.get('handoff_inputs', p['handoff_id'], {}).get('context', {})
    expected = [i['id'] for i in record['issues']]
    groups = sorted(p['result']['groups'], key=lambda g: min(expected.index(i) for i in g['issue_ids']))
    ids = [i for g in groups for i in g['issue_ids']]
    if (decision.get('run_id') != run_id or decision.get('role') != 'director'
            or decision.get('result') != p['result'] or frozen.get('definition_completion_id') != record['id']
            or frozen.get('indexed_document_context') is not True or frozen.get('document_index_protocol') != 1
            or frozen.get('definition_issues') != record['issues'] or groups != p['groups']
            or len(ids) != len(set(ids)) or set(ids) != set(expected)):
        raise ResearchBlocked('定义依赖分组缺少原意见绑定的实际交付。')


def group_context(record, issue_id):
    group = next(g for g in record['work_plan']['groups'] if issue_id in g['issue_ids'])
    primary = next((u for u in record['issue_drafts'] if u['issue_id'] == group['issue_ids'][0]), None)
    return {'definition_issue_group': copy.deepcopy(group),
            'canonical_definition_document': copy.deepcopy(primary['document']) if primary else None}
