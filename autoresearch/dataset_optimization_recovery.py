"""Explicit operator recovery of a prematurely advanced dataset.

Only the dispatch cursor is reopened. Prior receipts, plans, reviews and paid
requests remain either in place or in verified byte-exact record archives.
"""
import copy
import gzip
import hashlib
import json
from pathlib import Path

from . import record_archive
from .experiment_workflow import sha
from .storage import now
from .dataset_optimization import PROTOCOL

COLLECTION = 'dataset_optimization_recoveries'


def _retain_closeout_files(engine, run_id, cycle):
    """Report aliases can be overwritten later; retain the original file bytes."""
    from .deletion import _plain_path
    root = engine.run_dir(run_id)
    retained = []
    for name in ('report.md', 'research-history.json', 'blocked-report.md', f'cycle-{cycle}/exploration-stop.json'):
        artifact = engine.store.get('artifacts', run_id + ':' + name)
        if not artifact:
            continue
        source = Path(artifact['path'])
        _plain_path(source, root)
        raw = source.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        # Engine.artifact hashes text before Windows newline translation.
        text_digest = hashlib.sha256(raw.decode('utf-8').replace('\r\n', '\n').encode('utf-8')).hexdigest()
        if artifact.get('sha256') not in {digest, text_digest}:
            raise ValueError('Original closeout artifact identity changed')
        target = root / 'optimization-history' / digest / source.name
        _plain_path(target, root)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.read_bytes() != raw:
                raise ValueError('Immutable closeout history changed')
        else:
            target.write_bytes(raw)
        retained.append({'artifact': artifact, 'path': str(target), 'sha256': digest, 'bytes': len(raw)})
    return retained


def graph_entry(engine, run_id):
    run = engine.store.get('runs', run_id) or {}
    key = run.get('state', {}).get('dataset_optimization_recovery')
    if not key:
        return None
    saved = engine.store.get(COLLECTION, key) or {}
    body = saved.get('body', {})
    if saved.get('run_id') != run_id or saved.get('sha256') != sha(body):
        raise ValueError('Optimization recovery identity changed')
    if engine.cycle(run_id) != body['cycle']:
        return None
    return key + ':graph', body.get('entry', 'architecture_tuning')


def resume_mainline(engine, run_id, instruction):
    """Archive the premature closeout cursor, without erasing any paid result."""
    from .dataset_optimization import enabled, certificate
    from .experiment_decisions import _binding, _approved
    from .orchestration import STAGES
    run = engine.store.get('runs', run_id) or {}
    previous_key = run.get('state', {}).get('dataset_optimization_recovery')
    previous = engine.store.get(COLLECTION, previous_key) if previous_key else None
    if previous and previous.get('body', {}).get('kind') == 'remove_early_report_branch':
        graph_entry(engine, run_id)
        return previous
    if (not enabled(engine.store, run_id) or run.get('status') not in {'paused', 'blocked'}
            or not instruction.strip()
            or run_id in engine.tasks and not engine.tasks[run_id].done()
            or engine.store.active_reasoning(run_id, {'running', 'queued', 'starting', 'turn_start_dispatched',
                'thread_start_dispatched', 'thread_resume_dispatched'})):
        raise ValueError('Mainline recovery requires a stopped task, no active requests and an operator instruction')
    _, binding = _binding(engine, run_id)
    if certificate(engine.store, run_id, binding, 'complete_optimization'):
        raise ValueError('A completed optimization is not a premature stop')
    entry = 'tuning' if certificate(engine.store, run_id, binding, 'accept_architecture') else 'architecture_tuning'
    stopped = engine.store.get('steps', run_id + ':' + entry) or {}
    decision_id = (stopped.get('output') or {}).get('decision_id')
    decision = engine.store.get('experiment_decisions', decision_id) if decision_id else None
    evidence = engine.store.get('experiment_evidence', decision['evidence']['id']) if decision else None
    if (not decision or not _approved(decision) or decision.get('run_id') != run_id
            or decision['proposal']['action'] != 'stop'
            or not evidence or sha(evidence['summary']) != decision['evidence']['sha256']
            or evidence['summary']['binding'] != binding):
        raise ValueError('Recovery requires the exact reviewed premature stop and unchanged experiment binding')
    with engine.store._read_db() as db:
        if db.execute("SELECT 1 FROM records WHERE collection='experiments' AND key>=? AND key<? "
                      "AND json_extract(data,'$.status') IN ('running','interrupted') LIMIT 1",
                      (run_id + ':', run_id + ';')).fetchone():
            raise ValueError('Unfinished training must be reconciled before moving the cursor')
    stages = STAGES[STAGES.index(entry):]
    keys = [(c, run_id + ':' + s) for c in ('steps', 'stage_contexts') for s in stages]
    keys += [('research_cycles', f'{run_id}:{binding["cycle"]}'), ('graph_status', run_id)]
    refs = [record_archive.save(engine.store, run_id, f)
            for c, k in keys + [('runs', run_id)]
            if (f := record_archive.fingerprint(engine.store, c, k))]
    files = _retain_closeout_files(engine, run_id, binding['cycle'])
    body = {'kind': 'remove_early_report_branch', 'instruction': instruction.strip(),
            'cycle': binding['cycle'], 'entry': entry, 'binding': binding,
            'previous_recovery_id': previous_key, 'previous_decision_id': decision_id,
            'previous_archives': refs, 'previous_files': files, 'config_sha256': sha(run['config']),
            'preserve_frozen_plans': True, 'rerun_completed': False,
            'new_plan_seed_policy': 'single', 'scientific_approval': False}
    key = run_id + ':optimization-recovery:' + sha(body)
    value = {'id': key, 'run_id': run_id, 'body': body, 'sha256': sha(body), 'created_at': now()}
    updated = {**run, 'state': {**run['state'], 'dataset_optimization_recovery': key},
               'stage': entry, 'status': 'paused', 'error': None,
               'progress': round(STAGES.index(entry) / len(STAGES) * 100), 'updated_at': now()}
    graph = {'id': run_id, 'run_id': run_id, 'cycle': binding['cycle'],
             'node': entry, 'status': 'paused', 'updated_at': now()}
    engine.store.batch([(COLLECTION, key, value), ('runs', run_id, updated), ('graph_status', run_id, graph)],
                       deletes=[(c, k) for c, k in keys if c != 'graph_status'])
    engine.store.event(run_id, 'experiment.mainline_restored',
        '删除提前收尾执行分支；原停止意见与报告已归档，回到当前数据集优化主流程。',
        {'recovery_id': key, 'entry': entry, 'previous_decision_id': decision_id})
    return value


def reopen_first(engine, run_id, instruction):
    from .dataset_configuration_queue import current, COLLECTION as QUEUES
    from .orchestration import STAGES
    run = engine.store.get('runs', run_id) or {}
    if run.get('state', {}).get('dataset_optimization_recovery'):
        graph_entry(engine, run_id)
        return engine.store.get(COLLECTION, run['state']['dataset_optimization_recovery'])
    if (run.get('status') not in {'paused', 'blocked'} or not instruction.strip()
            or run_id in engine.tasks and not engine.tasks[run_id].done()
            or engine.store.active_reasoning(run_id, {'running', 'queued', 'starting', 'turn_start_dispatched',
                'thread_start_dispatched', 'thread_resume_dispatched'})):
        raise ValueError('Reopen only a paused task with no active requests and an explicit operator instruction')
    queue = current(engine.store, run_id)
    if not queue or len(queue['batches']) < 2:
        raise ValueError('Expected a prematurely advanced dataset queue with archived first-dataset state')
    first = queue['batches'][0]
    with engine.store._read_db() as db:
        if db.execute("SELECT 1 FROM records WHERE collection='experiments' AND key>=? AND key<? "
            "AND (json_extract(data,'$.status') IN ('running','interrupted') OR "
            "(json_extract(data,'$.research_cycle')>? AND json_extract(data,'$.params.phase') IN ('validation','test'))) LIMIT 1",
            (run_id + ':', run_id + ';', first['cycle'])).fetchone():
            raise ValueError('Later-dataset training or unfinished execution requires explicit reconciliation, never erasure')
    restored = {}
    for ref in first.get('archives', []):
        if ref['collection'] in {'programs', 'experiment_plans'} or (
                ref['collection'] == 'steps' and ref['key'] == run_id + ':experiments'):
            record_archive.verify(ref['path'], ref['sha256'], ref['bytes'])
            with gzip.open(ref['path'], 'rt', encoding='utf-8') as stream:
                restored[(ref['collection'], ref['key'])] = json.load(stream)
    if not all((name, run_id) in restored for name in ('programs', 'experiment_plans')):
        raise ValueError('Original first-dataset plan/program archive is required')
    keys = [(c, run_id) for c in ('programs', 'experiment_plans', 'selections', 'tuning_plans', 'experiment_restarts')]
    keys += [(c, run_id + ':' + s) for c in ('steps', 'stage_contexts') for s in STAGES[STAGES.index('experiments'):]]
    keys += [('research_cycles', run_id + ':' + str(first['cycle'])), ('graph_status', run_id)]
    archive_keys = keys + [('runs', run_id), (QUEUES, queue['id'])]
    archives = [record_archive.save(engine.store, run_id, f) for c, k in archive_keys
                if (f := record_archive.fingerprint(engine.store, c, k))]
    body = {'instruction': instruction.strip(), 'queue_id': queue['id'], 'cycle': first['cycle'],
        'dataset': first['dataset'], 'previous_archives': archives, 'restored_archives': first['archives'],
        'deferred_batches': copy.deepcopy(queue['batches'][1:]), 'config_sha256': sha(run['config']),
        'preserve_frozen_plans': True, 'rerun_completed': False, 'new_plan_seed_policy': 'single',
        'scientific_approval': False}
    key = run_id + ':optimization-recovery:' + sha(body)
    recovery = {'id': key, 'run_id': run_id, 'body': body, 'sha256': sha(body), 'created_at': now()}
    first = copy.deepcopy(first)
    first.update(status='optimizing', reopened_by=key)
    queue = {**queue, 'batches': [first], 'status': 'active', 'deferred_batches': body['deferred_batches']}
    state = {**run['state'], 'research_cycle': first['cycle'], PROTOCOL: 1, 'dataset_optimization_recovery': key}
    new_run = {**run, 'state': state, 'stage': 'architecture_tuning', 'status': 'paused',
               'error': None, 'progress': 55, 'updated_at': now()}
    puts = [(c, k, v) for (c, k), v in restored.items()]
    puts += [(COLLECTION, key, recovery), (QUEUES, queue['id'], queue), ('runs', run_id, new_run),
             ('graph_status', run_id, {'id': run_id, 'run_id': run_id, 'cycle': first['cycle'],
                                      'node': 'architecture_tuning', 'status': 'paused', 'updated_at': now()})]
    kept = {(c, k) for c, k, _ in puts}
    engine.store.batch(puts, deletes=[(c, k) for c, k in keys if (c, k) not in kept])
    engine.store.event(run_id, 'experiment.optimization_reopened',
        '保留所有实验与原始评审，返回首数据集的架构评估；参数稳定前不派发后续数据集。',
        {'recovery_id': key, 'dataset': first['dataset'], 'deferred_datasets': [b['dataset'] for b in body['deferred_batches']]})
    return recovery
