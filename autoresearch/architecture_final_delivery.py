"""Recover genuine final deliveries without rewriting decisions or candidates."""
from __future__ import annotations

import copy

from .storage import now


def pending(record):
    vote = record.get('invalid_review') or {}
    return (record.get('review_protocol') == 3 and bool(vote.get('handoff_id'))
            and not any(v.get('handoff_id') == vote['handoff_id'] for v in record.get('votes', [])))


def check_scope(engine, run_id, record, vote):
    from .engine import ResearchBlocked
    ids = vote['reviewed_issue_ids']
    current = {i['id'] for i in record['issues']}
    ctx = engine.store.get('handoff_inputs', vote['handoff_id'], {}).get('context', {})
    if len(ids) != len(set(ids)):
        raise ResearchBlocked('终审已审意见 ID 重复；应逐项列出本版 definition_issues.id。')
    if not current <= set(ids):
        raise ResearchBlocked('终审遗漏本版已审意见 ID；缺少：' + ', '.join(sorted(current - set(ids))))
    extra = set(ids) - current
    if not extra:
        return {'legacy_history_ids': []}
    # Older frozen requests exposed two ID domains with an unconstrained string
    # array. Accept only known, explicitly closed historical objections; retain
    # the actual response unchanged. New assignments use a strict current enum.
    prior = ctx.get('previous_architecture_review') or {}
    known = {i['id'] for i in prior.get('objections', []) if i.get('role') == vote['role']}
    resolved = set((vote.get('progress') or {}).get('resolved_objection_ids', []))
    if (record.get('review_protocol') != 3 or ctx.get('definition_final_scope_protocol')
            or ctx.get('definition_issues') != record['issues']
            or not extra <= known or not extra <= resolved):
        raise ResearchBlocked('终审已审意见 ID 含本版范围外的 ID；历史闭合 ID 应放入 progress.resolved_objection_ids。')
    return {'legacy_history_ids': sorted(extra), 'history_source_id': prior.get('source_id')}


def diagnostic(engine, run_id, record):
    if not pending(record):
        return None
    from .engine import ResearchBlocked
    try:
        scope = check_scope(engine, run_id, record, record['invalid_review'])
        reason = '终审实际答复已保存；恢复前将核验原版本、原交付及本角色全部分组阅读。'
    except (ResearchBlocked, KeyError, TypeError) as exc:
        return {'pending': True, 'can_recover': False, 'reason': str(exc),
                'role': record['invalid_review'].get('role')}
    return {'pending': True, 'can_recover': True, 'reason': reason,
            'role': record['invalid_review']['role'], **scope}


def constrain(schema, context):
    """Keep current reviewed IDs separate from predecessor closure IDs."""
    ids = [i['id'] for i in context['definition_issues']]
    schema['properties']['reviewed_issue_ids'] = {
        'type': 'array', 'items': {'type': 'string', 'enum': ids},
        'uniqueItems': True, 'minItems': len(ids), 'maxItems': len(ids)}
    history = [i['id'] for i in (context.get('previous_architecture_review') or {}).get('objections', [])]
    if 'progress' in schema['properties']:
        field = {'type': 'array', 'items': {'type': 'string'}, 'uniqueItems': True}
        if history:
            field['items']['enum'] = history
        else:
            field['maxItems'] = 0
        schema['properties']['progress']['properties']['resolved_objection_ids'] = field


def recover(engine, run_id):
    from . import architecture_definitions as definitions, architecture_revisions as revisions
    from .architecture_definition_completion import check_candidate
    from .engine import Harness, ResearchBlocked, digest
    revision = revisions.state(engine, run_id) or {}
    record = definitions._checked(engine, run_id, revision.get('definition_handoff_id', ''))
    if not pending(record):
        if record.get('final_delivery_recovery'):
            return definitions.view(engine, run_id)
        raise ResearchBlocked('没有待恢复的实际终审交付。')
    run = engine.store.get('runs', run_id)
    if (run['mode'] != 'live' or run['stage'] != 'design'
            or run['status'] not in {'blocked', 'paused', 'failed'} or record['status'] != 'blocked'):
        raise ResearchBlocked('只能在架构任务停止后恢复已保存的终审交付。')
    engine._consensus_gate(run_id)
    revisions._available(engine, run_id)
    if any((row.get('checkpoint') or {}).get('status') in definitions.TRANSIENT
           for row in engine.store.list('codex_reasoning', run_id)):
        raise ResearchBlocked('仍有活动请求，不能恢复终审交付。')
    check_candidate(engine, run_id, record)
    vote = record['invalid_review']
    hid = vote['handoff_id']
    assignment = engine.store.get('handoffs', hid, {})
    frozen = engine.store.get('handoff_inputs', hid, {})
    if (assignment.get('run_id') != run_id or assignment.get('to_role') != vote['role']
            or assignment.get('status') != 'completed'
            or assignment.get('purpose') != 'architecture_definitions_review:' + record['document']['sha256']
            or assignment.get('input_hash') != digest({k: frozen.get(k) for k in ('instruction', 'context', 'schema')})
            or Harness.decision_key(run_id, vote['role'], assignment['purpose'],
                frozen.get('instruction'), frozen.get('context'), frozen.get('schema')) != hid):
        raise ResearchBlocked('终审冻结交接发生变化，不能恢复原交付。')
    if any(v['role'] == vote['role'] for v in record['votes']):
        raise ResearchBlocked('本角色已经有另一份终审，不能重复计票。')
    definitions._check_vote(engine, run_id, record, vote)
    scope = check_scope(engine, run_id, record, vote)
    audit = {'handoff_id': hid, 'original_result_sha256': digest(
        engine.store.get('decisions', hid)['result']), 'recovered_at': now(), **scope,
        'candidate_changed': False, 'model_calls': 0}
    record['votes'].append(copy.deepcopy(vote))
    record.update(status='reviewing', reason='已恢复原始终审交付；继续收集同版本剩余角色意见。',
                  final_delivery_recovery=audit, updated_at=now())
    engine.store.put('architecture_definitions', record['id'], record)
    engine.store.event(run_id, 'architecture.definition_vote_recovered',
        '已核验并恢复原始终审；候选、分组意见、原答复及阅读记录保留，未调用模型。', audit)
    return definitions.view(engine, run_id)
