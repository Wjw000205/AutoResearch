"""Resolve host control facts before assigning research document or code work.

This receipt reports existing authority. It is not a scientific vote, a new
execution permit, or a replacement for any frozen request or experiment.
"""
from __future__ import annotations

import copy

from . import code_repair_contract as repair
from .storage import now

FIELD = 'host_constraint_reconciliation'
COLLECTION = 'host_constraint_reconciliations'
FOLLOWUP = 'host_constraint_followup'


def owns(issue):
    """A host ledger dependency is not an implementation definition defect."""
    binding = issue.get('binding') or issue
    if binding.get('disposition') not in {'needs_review', 'needs_definition', 'spec_conflict'}:
        return False
    text = (issue.get('issue', '') + '\n' + '\n'.join(
        str(f.get(k, '')) for f in issue.get('fixes', []) for k in ('location', 'change'))).lower()
    return (any(k in text for k in ('execution_constraints', 'trial_accounting'))
            and any(k in text for k in ('账本', 'ledger'))
            and any(k in text for k in ('宿主', 'host')))


def controls(engine, run_id):
    from .experiment_attempt_policy import current as attempts
    from .research_iteration import current as iterations, effective_config
    from .dataset_configuration_queue import budget
    from .execution_contract import constraints
    run = engine.store.get('runs', run_id)
    attempt_policy, iteration_policy = attempts(engine.store, run_id), iterations(engine.store, run_id)
    config = effective_config(engine.store, run_id, run['config'])
    effective = constraints(config, dataset_budget=budget(engine.store, run_id))
    keys = ('max_calls', 'max_trials', 'max_research_cycles', 'experiment_timeout', 'budget_usd')
    return {'task_configuration': {k: run['config'].get(k) for k in keys},
        'attempt_policy': copy.deepcopy(attempt_policy), 'iteration_policy': copy.deepcopy(iteration_policy),
        'effective_execution_constraints': effective,
        'precedence': ['explicit task operator policy', 'task configuration fallback',
                       'historical frozen observations (evidence only)'],
        'grants_experiment_authorization': False}


def validate(engine, run_id, ref, *, source=None):
    row = engine.store.get(COLLECTION, ref.get('id', ''), {})
    body = row.get('body', {})
    if (row.get('run_id') != run_id or row.get('sha256') != ref.get('sha256')
            or repair.sha(body) != row.get('sha256') or body.get('run_id') != run_id
            or source is not None and (body.get('source_sha256') != repair.sha(source)
                or body.get('spec_hash') != source['spec_hash']
                or body.get('delivery_hash') != source['delivery_hash'])):
        repair.fail('宿主有效约束回执的身份、来源或内容发生变化。')
    return row


def reconcile(engine, run_id, state, attempt, original, evaluated):
    items = [copy.deepcopy(i) for i in evaluated['issues'] if owns(i)]
    if not items:
        return None
    active = sorted(i['issue_id'] for i in evaluated['issues']
        if not owns(i) and (i.get('binding') or {}).get('disposition') == 'needs_review')
    current = controls(engine, run_id)
    identity = {'workflow_id': state['id'], 'attempt': attempt['index'],
        'source_sha256': repair.sha(original), 'evaluated_source_sha256': repair.sha(evaluated),
        'controls': current, 'host_issue_ids': sorted(i['issue_id'] for i in items),
        'supplement_issue_ids': active}
    key = run_id + ':host-constraints:' + repair.sha(identity)
    row = engine.store.get(COLLECTION, key)
    if row:
        validate(engine, run_id, {'id': key, 'sha256': row['sha256']}, source=original)
        return {'id': key, 'sha256': row['sha256']}
    # Do not deserialize the large paid request/billing archive just to read
    # two accounting operands. Counts are a frozen observation, not a permit.
    with engine.store._read_db() as db:
        ledger = db.execute("SELECT json_extract(data,'$.calls'),json_extract(data,'$.spent_usd') "
            "FROM records WHERE collection='budgets' AND key=?", (run_id,)).fetchone()
    body = {'version': 1, 'run_id': run_id, **identity,
        'spec_hash': original['spec_hash'], 'delivery_hash': original['delivery_hash'],
        'host_issues': items, 'observed_at': now(),
        'historical_observations': {role: {k: ctx.get('execution_constraints', {}).get(k)
            for k in ('max_trials_per_cycle', 'max_research_cycles', 'trial_accounting')}
            for role, ctx in original['source_contexts'].items()},
        'field_resolutions': {
            'runner_attempt_limit': {
                'configuration_fallback': current['task_configuration']['max_trials'],
                'effective': current['effective_execution_constraints']['trial_accounting'].get(
                    'attempt_limit', current['task_configuration']['max_trials']),
                'operator_policy_id': (current['attempt_policy'] or {}).get('id'),
                'operator_policy_sha256': (current['attempt_policy'] or {}).get('sha256')},
            'research_cycle_limit': {
                'configuration_fallback': current['task_configuration']['max_research_cycles'],
                'effective': current['effective_execution_constraints']['max_research_cycles'],
                'operator_policy_id': (current['iteration_policy'] or {}).get('id'),
                'operator_policy_sha256': (current['iteration_policy'] or {}).get('sha256')}},
        'call_budget_observation': {'calls': ledger[0] if ledger else None,
            'spent_usd': ledger[1] if ledger else None,
            'configured_max_calls': current['task_configuration']['max_calls'],
            'scope': 'actual host task ledger; not a reservation or a scientific approval'},
        'resolution': 'An authenticated operator policy, when present, supersedes its corresponding legacy configuration field. '
            'Frozen requests remain historical observations. Model calls and runner invocations are separate. '
            'Scientific coverage, code/data checks, current resource admission and individual dispatch gates remain required.',
        'scientific_approval': False, 'code_dispatch_authorized': False,
        'grants_experiment_authorization': False}
    row = {'id': key, 'run_id': run_id, 'body': body, 'sha256': repair.sha(body), 'created_at': now()}
    engine.check_active(run_id)
    engine.store.put(COLLECTION, key, row)
    engine.store.event(run_id, 'implementation.host_constraints_reconciled',
        'Harness 已按既有授权澄清有效约束；宿主账本问题不再派给文档作者或三方评审。',
        {'workflow_id': state['id'], 'receipt_id': key, 'host_issue_ids': body['host_issue_ids'],
         'supplement_issue_ids': active, 'scientific_approval': False})
    return {'id': key, 'sha256': row['sha256']}


def report_only(engine, run_id, state, attempt, source):
    """Host-only dependencies must never pay for a mapper or scientific vote."""
    if not source['issues'] or not all(owns(i) for i in source['issues']):
        return
    ref = reconcile(engine, run_id, state, attempt, source, source)
    from .implementation_failures import ImplementationCodeBlocked
    raise ImplementationCodeBlocked(
        'Harness 已提供有效约束及授权来源；当前仅有宿主依赖，未发现可派发的代码修改项。原意见及回执保留。',
        kind='codex_verification_pending', details={'workflow_id': state['id'], 'host_constraint_receipt': ref,
            'host_issue_ids': [i['issue_id'] for i in source['issues']]})


def context(engine, run_id, ref, original):
    row = validate(engine, run_id, ref, source=original)
    body = row['body']
    return {**ref, 'version': 1, 'workflow_id': body['workflow_id'],
        'source_sha256': body['source_sha256'], 'controls': copy.deepcopy(body['controls']),
        'call_budget_observation': copy.deepcopy(body['call_budget_observation']),
        'field_resolutions': copy.deepcopy(body['field_resolutions']),
        'host_issue_ids': body['host_issue_ids'], 'supplement_issue_ids': body['supplement_issue_ids'],
        'scientific_approval': False, 'code_dispatch_authorized': False}


async def recover_definition(engine, run_id, state, save):
    """Retain misrouted drafts/reviews and leave their active authoring chain."""
    from . import implementation_definition_repair as definitions
    record = state.get(definitions.FIELD)
    if not record or record.get('kind') != 'engineering_definition':
        return False
    requests = [engine.store.get('implementation_definition_requests', k, {}) for k in record['request_ids']]
    if not requests or not all(owns(r.get('issue', {})) for r in requests):
        return False
    evaluated = await engine._read_consensus_check(run_id, definitions._source, engine, run_id, state, record)
    spec = definitions.base_row(state, record)
    attempt = next(a for a in state['attempts'] if a['index'] == record['attempt_index'])
    original = await engine._read_consensus_check(run_id, repair.build_source,
        engine, run_id, state, spec, attempt)
    ref = reconcile(engine, run_id, state, attempt, original, evaluated)
    engine.check_active(run_id)
    state.setdefault('definition_repair_history', []).append(copy.deepcopy(record))
    state.setdefault('host_constraint_definition_routes', {})[record['id']] = {
        'receipt': ref, 'base_index': record['base_index'], 'archived_at': now(),
        'reason': 'host_control_dependency; original draft and independent receipts retained unchanged'}
    state.pop(definitions.FIELD)
    save(status='code_review', current_revision=record['base_index'], blocked_kind=None, reason=None)
    return True


