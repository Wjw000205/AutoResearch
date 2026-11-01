"""Operator recovery of a completed delivery rejected only during collection.

Retain exact historical records and resume the original reviewed work order.
This neither dispatches its completed native turn again nor approves code.
"""
import copy
import hashlib
import json
from pathlib import Path

from .experiment_workflow import sha
from .storage import now

COLLECTION = 'scoped_delivery_recoveries'
STATE_KEY = 'scoped_delivery_recovery'


def graph_entry(engine, run_id):
    run = engine.store.get('runs', run_id)
    key = run.get('state', {}).get(STATE_KEY)
    if not key:
        return None
    saved = engine.store.get(COLLECTION, key) or {}
    body = saved.get('body', {})
    if saved.get('run_id') != run_id or saved.get('sha256') != sha(body):
        raise ValueError('Delivery recovery identity changed')
    if body['cycle'] != engine.cycle(run_id):
        return None
    return key + ':graph', 'architecture_tuning'


def prepare(engine, run_id, revision_id, *, host_fix_sha256, instruction):
    from . import candidate_repair_source as source, rapid_experiment_revision as rapid
    from . import record_archive
    from .experiment_decisions import revision_context
    from .scoped_codex_recovery import current, _bound
    run = engine.store.get('runs', run_id) or {}
    if (run.get('status') not in {'paused', 'blocked'} or not instruction.strip()
            or run_id in engine.tasks and not engine.tasks[run_id].done()
            or engine.store.active_reasoning(run_id, {'running', 'queued', 'starting',
                'turn_start_dispatched', 'thread_start_dispatched', 'thread_resume_dispatched'})):
        raise ValueError('Delivery recovery requires a stopped task and explicit instruction')
    if len(host_fix_sha256) != 64 or any(c not in '0123456789abcdef' for c in host_fix_sha256):
        raise ValueError('Host repair source SHA required')
    prior = engine.store.get(COLLECTION, run.get('state', {}).get(STATE_KEY, ''))
    if prior and prior['body']['revision_id'] == revision_id:
        graph_entry(engine, run_id)
        return prior
    record = engine.store.get(rapid.COLLECTION, revision_id) or {}
    if (record.get('run_id') != run_id or record.get('cycle') != engine.cycle(run_id)
            or record.get('id') != revision_id
            or revision_id != run_id + ':rapid:' + sha(record.get('decision_id'))
            or record.get('status') != 'invalid' or not record.get('yaml_migration')
            or record.get('native_delivery_protocol') != 2
            or record.get('candidate') or record.get('patch') or record.get('votes') or record.get('check')
            or not str(record.get('error', '')).startswith((
                'Candidate repair file is absent or oversized:', 'Missing or oversized Codex delivery:'))):
        raise ValueError('Recovery requires an uncollected YAML delivery, not a rejected code review')
    review = {'failure': {'kind': 'experiment_decision_revision', 'details': {
        'decision_id': record['decision_id'], 'proposal_sha256': record['decision_sha256']}}}
    approved = revision_context(engine, run_id, review)
    if not approved or approved['proposal'] != record['decision'] or approved['evidence'] != record['evidence']:
        raise ValueError('Original approved work order changed')
    rapid._validate_base(engine, run_id, record)
    job = record.get('codex_work_item') or {}
    work = current(job)
    receipt = work.get('checkpoint') or {}
    config = run['config']
    if (receipt.get('status') != 'completed' or not _bound(receipt, run_id, work,
            Path(job['workspace']), engine.run_dir(run_id).resolve(),
            config.get('codex_model') or None, config.get('codex_reasoning_effort') or None)):
        raise ValueError('Original native delivery needs a bound completed receipt')
    order_raw = source.read_file(job['workspace'], 'work_order.json')
    order = json.loads(order_raw)
    if (hashlib.sha256(order_raw).hexdigest() != job['input_sha256']
            or any(order.get(k) != record[k] for k in ('decision', 'binding', 'evidence'))):
        raise ValueError('Original native work order changed')
    bundle = rapid._bundle(engine, run_id, record['base_program'])
    control = {f['path']: source.read_file(bundle['root'], f['path']) for f in bundle['files']}
    runtime, support, receipt_sha = source._native_delivery(engine, run_id, record, control)
    for name, raw in control.items():
        if source.read_file(job['workspace'], '_control/' + name) != raw:
            raise ValueError('Original control source changed')
    # Re-read every declared file before touching the cursor, including support.
    changes = source.read_file(job['workspace'], '_delivery/changes.json')
    delivered = {n: source.read_file(job['workspace'], n) for n in
        {*runtime, *support, *(r['path'] for r in json.loads(changes))}}
    delivered.update({'work_order.json': order_raw, '_delivery/changes.json': changes})
    loop_key = f'{run_id}:{record["cycle"]}'
    keys = [(rapid.COLLECTION, revision_id), ('runs', run_id), ('graph_status', run_id),
            ('optimization_loops', loop_key)]
    cursors = [(col, run_id + ':' + stage) for col in ('steps', 'stage_contexts')
               for stage in ('tuning', 'architecture_tuning')]
    archives = [record_archive.save(engine.store, run_id, f) for col, key in keys + cursors
                if (f := record_archive.fingerprint(engine.store, col, key))]
    body = {'revision_id': revision_id, 'cycle': record['cycle'], 'binding': record['binding'],
        'host_fix_sha256': host_fix_sha256, 'instruction': instruction.strip(),
        'original_records': archives, 'native_receipt_sha256': receipt_sha,
        'delivery_files': {n: hashlib.sha256(raw).hexdigest() for n, raw in sorted(delivered.items())},
        'approval_inherited': False, 'training_authorized': False, 'scientific_approval': False}
    key = run_id + ':delivery-recovery:' + sha(body)
    value = {'id': key, 'run_id': run_id, 'body': body, 'sha256': sha(body), 'created_at': now()}
    restored = copy.deepcopy(record)
    restored.update(status='implementing', delivery_collection_recovery=key, updated_at=now())
    for field in ('error', 'completed_at'):
        restored.pop(field, None)
    loop = engine.store.get('optimization_loops', loop_key) or {}
    loop = {**loop, 'id': loop_key, 'run_id': run_id, 'cycle': record['cycle'],
        'status': 'architecture_pending', 'decision_id': record['decision_id'], 'review': review,
        'result': None, 'scientific_approval': False, 'stable': False, 'updated_at': now()}
    updated = {**run, 'state': {**run.get('state', {}), STATE_KEY: key},
        'stage': 'architecture_tuning', 'status': 'paused', 'error': None, 'updated_at': now()}
    engine.store.batch([(COLLECTION, key, value), (rapid.COLLECTION, revision_id, restored),
        ('optimization_loops', loop_key, loop), ('runs', run_id, updated),
        ('graph_status', run_id, {'id': run_id, 'run_id': run_id, 'cycle': record['cycle'],
            'node': 'architecture_tuning', 'status': 'paused', 'updated_at': now()})], deletes=cursors)
    engine.store.event(run_id, 'codex.delivery_collection_recovered',
        '宿主交付收集已修复；保留原回执，复用原工作项进入独立代码审核。',
        {'recovery_id': key, 'revision_id': revision_id, 'native_turn_replayed': False})
    return value
