"""Harness adapter for a terminal, unaccepted document-review delivery."""
import copy

from .storage import now


def current(engine, run_id):
    run = engine.store.get('runs', run_id) or {}
    if run.get('stage') != 'design':
        return None
    rows = [r for r in engine.store.list('architecture_reviews', run_id)
            if r.get('status') == 'reviewing' and (r.get('review_delivery_validation') or {}).get('status') == 'repair_failed']
    if len(rows) != 1:
        return None
    return rows[0]


def prepared(engine, run_id):
    rows = [r for r in engine.store.list('architecture_reviews', run_id) if r.get('prepared_review_restart')]
    if not rows:
        return None
    if len(rows) != 1:
        raise ValueError('Multiple prepared review restarts require explicit reconciliation')
    ref = rows[0]['prepared_review_restart']
    owner = engine.store.get('stage_contexts', ref['owner_key']) or {}
    task = assignment(engine, run_id, rows[0], ref['role'], owner.get('assignment', {}).get('context', {}).get('review_unit'))
    return engine.store.get('task_restarts', owner['task_restarts'][-1]['id']) if task else None


def prepare(harness, run_id, reason):
    from . import task_restart as restart
    from .engine import Harness, digest
    from .architecture_review_recovery import _check_repair
    from .architecture_read_batch import attach_review_ranges, RANGE_GUIDANCE
    engine, store = harness.engine, harness.engine.store
    review = current(engine, run_id)
    if not review:
        raise ValueError('No unique terminal architecture review delivery to restart')
    failure = review['review_delivery_validation']
    hid, role = failure['repair_handoff_id'], failure['role']
    frozen = store.get('handoff_inputs', hid) or {}
    source = store.get('handoff_inputs', failure['source_handoff_id']) or {}
    unit = frozen.get('context', {}).get('review_unit')
    _check_repair(engine, run_id, review, role, unit, failure)
    active = review.get('active_review_handoffs', {}).get(role) or review.get('active_review_handoff') or {}
    if active.get('handoff_id') != hid or any(v.get('handoff_id') == hid for v in [*review.get('unit_reviews', []), *review.get('reviews', [])]):
        raise ValueError('Restart must target the current unaccepted delivery')
    assignment = {k: copy.deepcopy(frozen[k]) for k in ('instruction', 'context', 'schema')}
    assignment['purpose'] = store.get('handoffs', hid)['purpose']
    context = copy.deepcopy(source['context'])
    if unit:
        context = attach_review_ranges(context)
    replacement = {**assignment, 'context': context,
        'instruction': source['instruction'] + (RANGE_GUIDANCE if unit else '')}
    key = run_id + ':review-restart-owner:' + digest(hid)
    owner = {'id': key, 'run_id': run_id, 'status': 'prepared', 'assignment': assignment,
        'evidence_issue': {'handoff_id': hid, 'error': failure.get('repair_diagnostic')},
        'review_id': review['id'], 'role': role, 'scope_id': failure['scope_id'],
        'source_validation_sha256': digest(failure), 'created_at': now(),
        'controller_owner': {'collection': 'architecture_reviews', 'key': review['id']}}
    existing = store.get('stage_contexts', key)
    if existing and existing != owner:
        raise ValueError('A restart owner already exists; inspect its receipt instead of repeating the reset')
    if not existing:
        store.put('stage_contexts', key, owner)
    updated = copy.deepcopy(review)
    updated.pop('review_delivery_validation')
    updated['prepared_review_restart'] = {'owner_key': key,
        'role': role, 'scope_id': failure['scope_id'], 'source_handoff_id': hid}
    updated.setdefault('review_task_restarts', []).append(copy.deepcopy(updated['prepared_review_restart']))
    # The ordinary Harness creates the new handoff when this same scope resumes.
    plan = restart.preview(engine, run_id, hid, key, assignment_pointer='/assignment',
        replacement_assignment=replacement, clear_events=True, controller_transition={
            'collection': 'architecture_reviews', 'key': review['id'], 'before': review, 'after': updated})
    return restart.apply(engine, plan, reason=reason)


def assignment(engine, run_id, record, role, unit):
    ref = record.get('prepared_review_restart')
    scope = unit['id'] if unit else 'final'
    if not ref or ref['role'] != role or ref['scope_id'] != scope:
        return None
    from .engine import Harness, digest
    owner = engine.store.get('stage_contexts', ref['owner_key']) or {}
    receipt_id = (owner.get('task_restarts') or [{}])[-1].get('id')
    receipt = engine.store.get('task_restarts', receipt_id) or {}
    task = owner.get('assignment') or {}
    if (owner.get('run_id') != run_id or owner.get('review_id') != record['id']
            or receipt.get('run_id') != run_id or receipt.get('source_handoff_id') != ref['source_handoff_id']
            or receipt.get('assignment_sha256') != digest(task)
            or receipt.get('new_handoff_id') != Harness.decision_key(run_id, role, **task)
            or task['context'].get('review_unit') != unit
            or task['context'].get('architecture_document') != record['document']):
        raise ValueError('Prepared review restart lost its exact owner or frozen scope')
    return task
