"""Recover the producer's configuration and runtime for a named continuation."""
import hashlib
import json

from .experiment_views import _safe_file
from .experiment_workflow import sha


def bind(engine, rid, next_task, catalog, files, originals):
    producer = engine.store.get('causal_training_followups', f'{rid}:{engine.cycle(rid)}')
    if not producer or producer.get('status') != 'completed':
        return None
    outcome = producer['outcome']
    tid = outcome['trial_id']
    instruction = json.dumps(next_task, ensure_ascii=False)
    matches = [row for row in catalog if row['trial_id'] == tid and tid in instruction
               and row['original_path'].startswith('artifacts/continuation-')
               and row['original_path'] in instruction and row['sha256'] in instruction]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError('Continuation plan must identify one exact checkpoint')
    checkpoint, = matches
    trial = engine.store.get('experiments', rid + ':' + tid) or {}
    protocol = producer['protocol']
    bundle = producer['candidate']['bundle']
    if (producer.get('run_id') != rid or sha(trial) != outcome['receipt_sha256']
            or trial.get('run_id') != rid or trial.get('id') != tid or trial.get('status') != 'completed'
            or trial['params'].get('phase') != 'code_check' or trial['limits'].get('code_bundle') != bundle):
        raise ValueError('Continuation producer or execution receipt changed')

    def read(relative, digest):
        path = _safe_file(engine, rid, trial, relative)
        if path is None or path.stat().st_size > 8 * 1024**2:
            raise ValueError('Missing or oversized continuation source: ' + relative)
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('Continuation source hash changed: ' + relative)
        originals[str(path.resolve())] = digest
        return raw

    report = json.loads(read('artifacts/controlled_diagnostic.json', outcome['report_sha256']))
    if (report['protocol'] != protocol or report.get('input_hashes_unchanged') is not True
            or trial['experiment_metadata'].get('diagnostic_report_sha256') != outcome['report_sha256']):
        raise ValueError('Continuation report does not authenticate its protocol')
    commits = [(t, b['continuation']) for t in report['trials'] for b in t['branches']
               if (b.get('continuation') or {}).get('path') == checkpoint['original_path']]
    if len(commits) != 1:
        raise ValueError('Missing unique continuation commit')
    source, commit = commits[0]
    seed = checkpoint['seed']
    if (source['seed'] != seed or protocol['seeds'] != [seed] or commit['sha256'] != checkpoint['sha256']
            or commit.get('resume_eligible') is not True):
        raise ValueError('Continuation checkpoint does not match the committed state')
    inputs = [i for i in protocol['inputs'] if i['seed'] == seed and i['trial_id'] == source['trial_id']]
    if len(inputs) != 1 or inputs[0]['config_path'] != f'controlled_inputs/{seed}/config.json':
        raise ValueError('Missing unique seed-bound configuration')
    item = inputs[0]
    relative, digest = item['config_path'], item['config_sha256']
    manifest = {r['path']: r for r in bundle['files']}
    if manifest.get(relative, {}).get('sha256') != digest:
        raise ValueError('Configuration differs from the frozen producer bundle')
    raw = read(relative, digest)
    config = json.loads(raw)
    original = engine.store.get('experiments', rid + ':' + item['trial_id']) or {}
    if (original.get('run_id') != rid or original.get('id') != item['trial_id']
            or original.get('status') != 'completed' or config != original.get('params')
            or config.get('phase') != 'validation' or config.get('seed') != seed
            or config.get('dataset_assets') != trial['params']['dataset_assets']):
        raise ValueError('Original validation configuration or dataset binding changed')

    # Build this isolated diagnostic from the actual producer's modules. Never
    # resolve a missing historical import using a newer active model module.
    roots = ('src/', 'dataset_runtime/', 'configs/')
    for name in list(files):
        if name.startswith(roots):
            del files[name]
    for name, row in manifest.items():
        if (name.startswith(roots) and name.endswith(('.py', '.json'))
                or name in ('diagnostic_probe.py', 'diagnostic_verdicts.py', 'diagnostic_persistence.py')):
            files[name] = read(name, row['sha256'])
    staged = 'diagnostic_inputs/' + sha(tid)[:16] + '/' + relative
    catalog.append({'trial_id': tid, 'original_path': relative, 'path': staged, 'sha256': digest,
                    'bytes': len(raw), 'seed': seed, 'params': checkpoint['params']})
    return {'producer_trial_id': tid, 'producer_receipt_sha256': outcome['receipt_sha256'],
            'producer_bundle_sha256': bundle['sha256'], 'producer_report_sha256': outcome['report_sha256'],
            'original_validation_trial_id': item['trial_id'], 'original_input': item,
            'checkpoint': commit, 'checkpoint_path': checkpoint['path'], 'config_path': staged,
            'config_sha256': digest, 'config_text': raw.decode('utf-8'),
            'dataset_assets_sha256': sha(config['dataset_assets'])}
