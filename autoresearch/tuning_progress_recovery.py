"""Explicit host-contract upgrade; retain frozen diagnostic plans and paid inputs."""
from .experiment_workflow import sha
from .storage import now

COLLECTION = 'tuning_progress_recoveries'


def current(engine, run_id):
    row = engine.store.get(COLLECTION, f'{run_id}:{engine.cycle(run_id)}')
    if not row:
        return None
    body = row.get('body', {})
    if (row.get('run_id') != run_id or sha(body) != row.get('sha256')
            or body.get('execution_authorized') is not False):
        raise ValueError('Tuning progress recovery identity changed')
    from .record_archive import fingerprint
    for source in body['retained_sources']:
        if fingerprint(engine.store, source['collection'], source['key']) != source:
            raise ValueError('Retained diagnostic source changed after explicit route recovery')
    return row


def excluded(engine, run_id, *, task_id=None, diagnostic_id=None):
    row = current(engine, run_id)
    return bool(row and (task_id in row['body']['task_ids'] or diagnostic_id in row['body']['diagnostic_ids']))


def register(engine, run_id, diagnostic_ids, instruction):
    """Local maintenance entry: only a paused, undispatched legacy route can move."""
    from .record_archive import fingerprint, save
    from .bounded_diagnostics import source_task
    from .tuning_scope import verify, COLLECTION as SCOPES
    from .bounded_diagnostic_contract import capabilities, VERSION
    run = engine.store.get('runs', run_id)
    key = f'{run_id}:{engine.cycle(run_id)}'
    old = current(engine, run_id)
    if old:
        if set(diagnostic_ids) != set(old['body']['diagnostic_ids']):
            raise ValueError('An existing recovery cannot be expanded implicitly')
        return old
    lease = engine.store.get('runtime', 'worker', {})
    if (run['status'] not in {'paused', 'blocked'} or not instruction.strip() or not diagnostic_ids
            or lease.get('held') and lease.get('run_id') == run_id and engine.owner_alive(lease)):
        raise ValueError('Pause and settle the research worker before upgrading its diagnostic route')
    records = [engine.store.get('bounded_diagnostics', identifier) for identifier in diagnostic_ids]
    for record in records:
        if (not record or record.get('run_id') != run_id or record.get('cycle') != engine.cycle(run_id)
                or record.get('host_contract_version', 0) >= VERSION or record.get('trial_id') or record.get('outcome')
                or record.get('status') not in {'implementing', 'reviewing_code', 'implementation_rejected'}):
            raise ValueError('Only undispatched legacy diagnostic implementations may be rerouted')
        source_task(engine, record)
    binding = records[0]['binding']
    if any(record['binding'] != binding for record in records):
        raise ValueError('Diagnostic route upgrade must share one current model/data binding')
    scope = next((s for s in engine.store.list(SCOPES, run_id)
                  if s.get('status') == 'accepted' and s.get('binding') == binding), None)
    if not scope:
        raise ValueError('Recovery requires the independently accepted current tuning scope')
    verify(engine, run_id, scope)
    task_ids = sorted({r['source_task_id'] for r in records})
    sources = [fingerprint(engine.store, col, identifier) for col, ids in (
        ('bounded_diagnostics', diagnostic_ids), ('parameter_resolution_tasks', task_ids)) for identifier in ids]
    archives = [save(engine.store, run_id, source) for source in sources]
    body = {'version': 1, 'binding': binding, 'scope_id': scope['id'], 'scope_plan_sha256': scope['plan_sha256'],
        'instruction': instruction, 'diagnostic_ids': sorted(diagnostic_ids), 'task_ids': task_ids,
        'retained_sources': sources, 'archives': archives, 'new_host_capabilities': capabilities(),
        'reason': 'Legacy implementation requests saw the model adapter rather than the generated diagnostic entry; '
                  'the new decision receives verified scope approvals and the supported host contract.',
        'next_action': 'Reassess the next single-factor proposal or concrete source repair using the new evidence. '
                       'Do not replay the retained legacy work items or treat their unknown responses as results.',
        'execution_authorized': False, 'scientific_approval': False}
    if engine.store.get('runs', run_id) != run or any(
            fingerprint(engine.store, s['collection'], s['key']) != s for s in sources):
        raise ValueError('Recovery sources changed during preparation')
    row = {'id': key, 'run_id': run_id, 'body': body, 'sha256': sha(body), 'created_at': now()}
    engine.store.put(COLLECTION, key, row)
    engine.store.event(run_id, 'tuning.progress_route_recovered',
        '保留旧诊断计划与付费回执；用已核验范围批准和真实宿主能力重新审议下一批，不继承执行授权。',
        {'recovery_id': key, 'diagnostic_ids': body['diagnostic_ids'], 'execution_authorized': False})
    return row


def attach(engine, run_id, evidence):
    row = current(engine, run_id)
    if not row or row['body']['binding'] != evidence['binding']:
        return
    body = row['body']
    evidence['tuning_progress_recovery'] = {
        'id': row['id'], 'sha256': row['sha256'], 'reason': body['reason'], 'next_action': body['next_action'],
        'retained_diagnostic_ids': body['diagnostic_ids'], 'execution_authorized': False}
