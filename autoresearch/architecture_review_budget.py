"""Stage-scoped reading budgets and explicit, receipt-preserving repartitioning."""
from __future__ import annotations

import copy
import math

from .experiment_workflow import sha
from .storage import now

VERSION = 1
KEY = 'architecture_review_read_budget'
ARCHIVES = 'architecture_review_partitions'
DEFAULTS = {'max_reading_groups': 3, 'unit_char_limit': 60000,
            'document_window_chars': 72000, 'total_window_chars': 100000,
            'output_tokens': 12000, 'max_batch_calls': 64}


def profile(document):
    groups = min(DEFAULTS['max_reading_groups'], max(1, math.ceil(document['total_chars'] / DEFAULTS['unit_char_limit'])))
    cap = math.ceil(document['total_chars'] / groups)
    if cap > DEFAULTS['unit_char_limit']:
        raise ValueError('Architecture document exceeds the reviewed three-group budget; raise the stage budget explicitly')
    body = {'version': VERSION, 'stage': 'architecture_review', **DEFAULTS,
        'document_id': document['document_id'], 'document_sha256': document['sha256'],
        'document_chars': document['total_chars'], 'reading_groups': groups, 'unit_chars': cap}
    return {**body, 'sha256': sha(body)}


def checked(value, document):
    if not isinstance(value, dict) or value != profile(document):
        raise ValueError('Architecture reading budget no longer matches its frozen document/profile')
    return value


def from_context(context):
    value = context.get(KEY)
    if value is None:
        return None
    if (context.get('architecture_review_recovery_protocol') != 1
            or context.get('architecture_workflow_protocol') != 1
            or not isinstance(context.get('architecture_document'), dict)):
        raise ValueError('The large reading window is restricted to architecture document review')
    return checked(value, context['architecture_document'])


def validate_parent(engine, run_id, record):
    ref = record.get('reading_plan_parent')
    if not ref:
        return None
    row = engine.store.get(ARCHIVES, ref['id']) or {}
    prior = row.get('review') or {}
    if (row.get('run_id') != run_id or row.get('sha256') != ref['sha256'] or sha(prior) != ref['sha256']
            or prior.get('document') != record['document'] or prior.get('design_hash') != record['design_hash']):
        raise ValueError('Original architecture review partition or its source document changed')
    # The old approvals remain approvals of their exact old scopes. They are
    # never relabelled as approvals of a larger replacement batch.
    from .architecture_review_recovery import _units, _checked_entries, effective_source
    units = {unit['id']: unit for unit in _units(engine, run_id, prior)}
    keys = {(v['role'], v['unit_id']) for v in prior.get('unit_reviews', [])}
    for role, unit_id in keys:
        _checked_entries(engine, run_id, prior, role, units[unit_id])
    if any(v['disposition'] != 'approve' for v in effective_source(prior)['unit_reviews']):
        raise ValueError('A reading-budget migration cannot discard an unresolved scientific objection')
    if record.get('inherited_unit_reviews') != effective_source(prior)['unit_reviews']:
        raise ValueError('Inherited opinions differ from the authenticated original partition')
    return prior


def migrate(engine, run_id, reason):
    """Only a quiescent checkpoint may receive a new versioned reading plan."""
    from .engine import ResearchBlocked
    run = engine.store.get('runs', run_id) or {}
    worker = engine.store.get('runtime', 'worker') or {}
    if (run.get('status') not in {'paused', 'blocked'} or run.get('stage') != 'design'
            or worker.get('held') and worker.get('run_id') == run_id or not reason.strip()):
        raise ResearchBlocked('Pause and settle the current architecture assignment before changing its reading plan')
    if engine.store.active_reasoning(run_id, {'running', 'queued', 'starting', 'turn_start_dispatched',
            'thread_start_dispatched', 'thread_resume_dispatched'}):
        raise ResearchBlocked('A native request is still active; its paid input is preserved')
    rows = [r for r in engine.store.list('architecture_reviews', run_id)
            if r.get('cycle') == engine.cycle(run_id) and r.get('status') == 'reviewing']
    if len(rows) != 1:
        raise ValueError('A unique current architecture review is required')
    old = rows[0]
    if old.get(KEY):
        checked(old[KEY], old['document'])
        return old
    if old.get('io_template_review') == 1 or old.get('reviews'):
        raise ValueError('This migration is for segmented reviews before final decisions')
    if old.get('review_delivery_validation') or old.get('prepared_review_restart'):
        raise ValueError('Resolve the exact delivery recovery before repartitioning')
    from .architecture_review_recovery import effective_source
    accepted = effective_source(old)['unit_reviews']
    if any(v['disposition'] != 'approve' for v in accepted):
        raise ValueError('Resolve existing objections instead of resetting them through a budget change')
    # Keep unfinished assignments out of the migration as well as native turns.
    active = [old.get('active_review_handoff') or {}, *old.get('active_review_handoffs', {}).values()]
    for entry in active:
        hid = entry.get('handoff_id')
        if hid and not any(v.get('handoff_id') == hid for v in [*accepted, *old.get('reviews', [])]):
            raise ValueError('Finish the current document-review assignment before repartitioning')
    budget = profile(old['document']); checksum = sha(old)
    # Validate accepted source evidence before committing a replacement plan.
    from .architecture_review_recovery import _units, _checked_entries
    prior_units = {unit['id']: unit for unit in _units(engine, run_id, old)}
    for role, unit_id in {(v['role'], v['unit_id']) for v in old.get('unit_reviews', [])}:
        _checked_entries(engine, run_id, old, role, prior_units[unit_id])
    key = old['id'] + ':partition:' + checksum
    archived = {'id': key, 'run_id': run_id, 'sha256': checksum, 'review': copy.deepcopy(old), 'created_at': now()}
    new = copy.deepcopy(old)
    for field in ('active_review_handoff', 'active_review_handoffs', 'completed_at'):
        new.pop(field, None)
    new.update({KEY: budget, 'reading_plan_parent': {'id': key, 'sha256': checksum},
        'inherited_unit_reviews': copy.deepcopy(accepted), 'unit_reviews': [], 'unit_review_amendments': [],
        'reviews': [], 'final_review_amendments': [], 'unit_count': budget['reading_groups'],
        'unit_cap': budget['unit_chars'], 'reading_budget_reason': reason, 'updated_at': now()})
    engine.store.batch([(ARCHIVES, key, archived), ('architecture_reviews', old['id'], new)],
        expected=[('runs', run_id, run), ('architecture_reviews', old['id'], old), (ARCHIVES, key, None)])
    engine.store.event(run_id, 'architecture.reading_budget_migrated',
        '同一架构文档已切换大窗口阅读计划；旧交接、原文及有效意见完整归档，不授予新批次批准。',
        {'review_id': old['id'], 'reading_groups': budget['reading_groups'],
         'inherited_reviews': len(accepted), 'profile_sha256': budget['sha256'], 'parent_id': key})
    return new


def register(app, engine, task_runtime, require_run):
    from fastapi import Body
    from .async_ownership import to_thread
    @app.post('/api/runs/{run_id}/architecture-review-budget')
    @task_runtime.control
    async def apply_budget(run_id: str, payload: dict = Body(...)):
        require_run(run_id)
        if set(payload) != {'reason'} or not isinstance(payload['reason'], str):
            raise ValueError('Provide an explicit reason for the frozen stage budget migration')
        async with engine._lifecycle_lock:
            result = await to_thread(migrate, engine, run_id, payload['reason'])
        return {'review_id': result['id'], 'reading_budget': result[KEY],
                'inherited_reviews': len(result.get('inherited_unit_reviews', [])), 'status': result['status']}
