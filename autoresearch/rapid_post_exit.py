"""Host-owned evidence verification after the bounded check worker exits.

Read JSON and bytes only. Never import candidate code, deserialize checkpoints,
rerun checks or let a worker's own probe claim a real process timeout.
"""
import hashlib
import json
from pathlib import Path

import psutil

from .experiment_workflow import sha
from .storage import now
from .delivery_files import io_path

COLLECTION = 'rapid_post_exit_verifications'


def safe(root, relative):
    root = io_path(root)
    path = Path(relative)
    if path.is_absolute() or '..' in path.parts or ':' in str(relative):
        raise ValueError('Evidence path escapes its attempt')
    target = root / path
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError('Evidence path escapes its attempt')
    for part in (target, *target.parents):
        if part.is_symlink() or getattr(part, 'is_junction', lambda: False)():
            raise ValueError('Linked evidence path')
        if part == root:
            break
    return target


def verify_after_exit(trial, record):
    if trial.get('status') not in {'completed','failed','timeout','timed_out','interrupted','resource_exceeded'} or not trial.get('finished_at'):
        raise ValueError('No terminal runner receipt')
    try:
        process = psutil.Process(trial['pid'])
        if abs(process.create_time() - trial['process_started']) < .01 and process.is_running():
            raise ValueError('Check worker is still alive')
    except (KeyError, psutil.NoSuchProcess):
        pass
    candidate = record['candidate']
    if (trial.get('code_sha256') != candidate['checks_sha256']
            or trial.get('limits', {}).get('code_bundle') != candidate['check_bundle']):
        raise ValueError('Post-exit receipt does not match the reviewed check')
    root = io_path(trial['artifact_path'])
    snapshot = safe(root, '_delivery_snapshot')
    invocation_verified = False
    if '__rapid_prepare_delivery_invocation()' in candidate.get('check_code',''):
        for name in ('params.json', 'environment.json'):
            if safe(snapshot, name).read_bytes() != safe(root, name).read_bytes():
                raise ValueError('Relocated host invocation receipt changed: ' + name)
        invocation_verified = True
    bundle = {r['path']:r for r in candidate['check_bundle']['files']}
    for name in ('work_order.json','_delivery/manifest.json','_delivery/evidence.py','_delivery/checks.py'):
        raw = safe(snapshot, name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != bundle['_delivery_snapshot/'+name]['sha256']:
            raise ValueError('Post-exit source identity changed: ' + name)
    order_sha = hashlib.sha256(safe(snapshot,'work_order.json').read_bytes()).hexdigest()
    manifest = json.loads(safe(snapshot,'_delivery/manifest.json').read_bytes())
    manifest_sha = hashlib.sha256(safe(snapshot,'_delivery/manifest.json').read_bytes()).hexdigest()
    parent = safe(snapshot, '_delivery/check_runs')
    if not parent.is_dir():
        raise ValueError('Worker left no durable evidence directory')
    directories = sorted(parent.iterdir())
    if not directories or len(directories)>32:
        raise ValueError('Invalid evidence directory count')
    verified, total_bytes, main_count = [], 0, 0
    for directory in directories:
        directory = safe(snapshot, directory.relative_to(snapshot))
        progress = safe(directory, 'progress')
        records = sorted(progress.glob('*.json'))
        if not records or len(records)>4096:
            raise ValueError('Missing or excessive progress history')
        prior, seen, last = [], set(), None
        for i, path in enumerate(records):
            path = safe(directory, path.relative_to(directory))
            if path.stat().st_size > 4*1024*1024:
                raise ValueError('Oversized progress record')
            raw = path.read_bytes(); last = json.loads(raw)
            if last['sequence'] != i or last['artifacts'][:len(prior)] != prior:
                raise ValueError('Evidence history changed or has a gap')
            binding = last['binding']
            if (binding.get('work_order_sha256') != order_sha
                    or binding.get('manifest_sha256') != manifest_sha
                    or binding.get('candidate_runtime_sha256') != candidate['bundle']['sha256']
                    or binding.get('control_manifest_sha256') != manifest['control_manifest_sha256']):
                raise ValueError('Durable evidence belongs to a different candidate')
            for row in last['artifacts']:
                identity = (row['path'],row['sha256'],row['bytes'])
                if identity in seen:
                    continue
                target = safe(directory,row['path'])
                if target.stat().st_size != row['bytes']:
                    raise ValueError('Evidence artifact size mismatch')
                total_bytes += row['bytes']
                if total_bytes > trial['limits'].get('disk_mb',256)*1024*1024:
                    raise ValueError('Evidence exceeds the admitted disk limit')
                with target.open('rb') as f:
                    digest = hashlib.file_digest(f,'sha256').hexdigest()
                if digest != row['sha256']:
                    raise ValueError('Evidence artifact hash mismatch')
                seen.add(identity)
            prior = last['artifacts']
        is_probe = set(last['stages']) == {'retention_probe'}
        if not is_probe:
            main_count += 1
            if trial['status']=='completed' and (last['status']!='passed' or not last['completed']
                    or not last['stages'] or any(s!='passed' for s in last['stages'].values())):
                raise ValueError('Completed worker has incomplete durable checks')
        verified.append({'directory':directory.relative_to(root).as_posix(),
            'last_record_sha256':hashlib.sha256(raw).hexdigest(), 'verified_artifacts':len(seen),
            'worker_reported_status':last['status'], 'incomplete_stages':[k for k,v in last['stages'].items() if v!='passed'],
            'scope':'in_process_retention_probe' if is_probe else 'main_check',
            'actual_worker_exit_observed':True})
    if main_count != 1:
        raise ValueError('Expected exactly one main CPU evidence run')
    return {'post_exit_verified':True, 'host_observed_outcome':trial['status'], 'directories':verified,
        'invocation_receipts_verified':invocation_verified,
        'verified_artifact_bytes':total_bytes, 'candidate_behavior_passed':trial['status']=='completed',
        'controlled_probe_is_real_process_timeout':False, 'scientific_approval':False}


def capture(engine, run_id, record, trial):
    body = {'run_id':run_id,'revision_id':record['id'],'trial_id':trial['id'],'runner_receipt_sha256':sha(trial),
            'checks_sha256':record['candidate']['checks_sha256'],'verifier_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    key = run_id + ':post-exit:' + sha(body)
    saved = engine.store.get(COLLECTION,key)
    if saved:
        return {'id':key,'sha256':sha(saved)}
    try:
        result = verify_after_exit(trial,record)
    except (OSError, ValueError, KeyError, TypeError, psutil.Error) as exc:
        result = {'post_exit_verified':False,'error':str(exc),'scientific_approval':False}
    saved = {'id':key, 'run_id':run_id, 'binding':body, 'result':result, 'created_at':now()}
    engine.store.put(COLLECTION,key,saved)
    return {'id':key,'sha256':sha(saved)}


def passed(store, record, trial):
    ref = record.get('post_exit_verification') or {}
    saved = store.get(COLLECTION,ref.get('id','')) or {}
    return (sha(saved)==ref.get('sha256') and saved.get('binding',{}).get('runner_receipt_sha256')==sha(trial)
            and saved['result'].get('post_exit_verified') is True
            and saved['result'].get('candidate_behavior_passed') is True)
