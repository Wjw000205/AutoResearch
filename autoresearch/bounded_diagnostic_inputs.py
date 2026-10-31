"""Copy only authenticated validation inputs into a new diagnostic bundle."""
import hashlib
import json
from pathlib import Path

from .experiment_workflow import sha


def prepare(engine, run_id, evidence):
    source = engine.store.get('experiment_sources', evidence['code_sources']['id'])
    if sha(source['files']) != evidence['code_sources']['sha256']:
        raise ValueError('Original runtime source archive changed')
    files = {p: r['text'].encode('utf-8') for p, r in source['files'].items() if 'text' in r}
    trial_ids = {r['id'] for r in evidence['records']}
    diagnostic_seeds = {}
    # Reuse previously reviewed diagnostic utilities, with their original bytes.
    for col in ('controlled_training_checks', 'causal_training_followups', 'fixed_state_diagnostics'):
        old = engine.store.get(col, f'{run_id}:{engine.cycle(run_id)}')
        if not old or old.get('status') != 'completed' or not old.get('candidate'):
            continue
        if (old.get('outcome') or {}).get('trial_id'):
            trial_ids.add(old['outcome']['trial_id'])
            seeds = old.get('protocol', {}).get('seeds', [])
            if len(seeds) == 1:
                diagnostic_seeds[old['outcome']['trial_id']] = seeds[0]
        bundle = old['candidate']['bundle']
        from .code_bundle import validate
        validate(bundle, old['code'])
        for row in bundle['files']:
            name = row['path']
            if name not in files and name.endswith('.py'):
                files[name] = (Path(bundle['root']) / name).read_bytes()
    completed = [r for r in engine.store.list('bounded_diagnostics', run_id) if r.get('status') == 'completed']
    for old in sorted(completed, key=lambda r: r.get('completed_at', ''))[-4:]:
        trial_id = old['outcome']['trial_id']
        trial_ids.add(trial_id)
        diagnostic_seeds[trial_id] = old['protocol']['seed']
        bundle = old['candidate']['bundle']
        from .code_bundle import validate
        validate(bundle, old['code'])
        for row in bundle['files']:
            if row['path'].startswith('bounded_diagnostic') and row['path'].endswith('.py'):
                files['prior_diagnostics/' + sha(old['id'])[:16] + '/' + row['path']] = (Path(bundle['root']) / row['path']).read_bytes()
    from .experiment_views import _safe_file
    catalog, originals = [], {}
    # Enumerate only bounded, saved output files; never stage held-out results.
    with engine.store._read_db() as db:
        rows = db.execute("SELECT json_object('id',json_extract(data,'$.id'),"
            "'artifact_path',json_extract(data,'$.artifact_path'),'params',"
            "json(json_remove(json_extract(data,'$.params'),'$.comparison_inputs'))) "
            "FROM records WHERE collection='experiments' AND json_extract(data,'$.run_id')=? "
            "AND json_extract(data,'$.status')='completed' AND json_extract(data,'$.params.phase') IN ('validation','code_check')", (run_id,)).fetchall()
    for row in rows:
        trial = json.loads(row[0])
        if trial['id'] not in trial_ids:
            continue
        root = Path(trial.get('artifact_path') or '') / 'artifacts'
        if not root.is_dir() or root.is_symlink() or getattr(root, 'is_junction', lambda: False)():
            continue
        for path in sorted(root.glob('*')):
            if path.suffix not in {'.pt', '.json'} or not path.is_file() or path.stat().st_size > 256 * 1024 * 1024:
                continue
            relative = 'artifacts/' + path.name
            seed = trial['params'].get('seed', diagnostic_seeds.get(trial['id']))
            if path.suffix == '.pt' and seed is None:
                continue  # Do not infer a model seed from an unbound filename.
            actual = _safe_file(engine, run_id, trial, relative)
            if actual is None:
                continue
            raw = actual.read_bytes(); digest = hashlib.sha256(raw).hexdigest()
            name = 'diagnostic_inputs/' + sha(trial['id'])[:16] + '/' + path.name
            catalog.append({'trial_id': trial['id'], 'original_path': relative, 'path': name,
                            'sha256': digest, 'bytes': len(raw), 'seed': seed,
                            'params': {k: v for k, v in trial['params'].items() if k != 'comparison_inputs'}})
            originals[str(actual.resolve())] = digest
            if len(catalog) > 128:
                raise ValueError('Diagnostic input catalog exceeds 128 saved artifacts; narrow the source scope')
    return files, catalog, originals


def selected(engine, record, proposal):
    catalog = {x['path']: x for x in record['input_catalog']}
    names = proposal['input_paths']
    if len(names) != len(set(names)) or not set(names) <= set(catalog):
        raise ValueError('Select only exact host-catalogued diagnostic inputs')
    selected = [catalog[n] for n in names]
    if any(x['seed'] not in (None, proposal['seed']) for x in selected):
        raise ValueError('A new diagnostic must use only its one declared model seed')
    if sum(x['bytes'] for x in selected) > 256 * 1024 * 1024:
        raise ValueError('Diagnostic inputs exceed the bounded staging limit')
    assets = []
    files = {}
    from .experiment_views import _safe_file
    for item in selected:
        trial = engine.store.get('experiments', record['run_id'] + ':' + item['trial_id'])
        path = _safe_file(engine, record['run_id'], trial, item['original_path'])
        if path is None or hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
            raise ValueError('Selected diagnostic input changed')
        files[item['path']] = path.read_bytes()
        asset = item['params'].get('dataset_assets')
        if asset is not None:
            assets.append(asset)
    if not assets or any(a != assets[0] for a in assets):
        raise ValueError('Diagnostic inputs need one consistent original dataset binding')
    return files, selected, assets[0]
