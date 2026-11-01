"""Reuse execution evidence by runtime bytes, inputs and admitted resources.

Delivery README/test reports and validator revisions are not execution inputs.
Every reused output is revalidated; a previous approval is never copied.
"""
import copy
import hashlib
import json
import sys
from pathlib import Path

from .comparison_transport import canonical, digest, io_limits, read_final_output
from .delivery_files import identity
from .lab import _digest

RESOURCE_KEYS = ('memory_mb', 'cpu_cores', 'timeout_seconds', 'cpu_seconds', 'disk_mb',
    'gpu_allowed', 'gpu_memory_mb', 'torch_memory_cap', 'gpu_uuid', 'gpu_index',
    'execution_mode', 'python_executable', 'required_packages')


def normalized_limits(limits, params):
    values = {key: copy.deepcopy(limits.get(key)) for key in RESOURCE_KEYS}
    for key, default in [('memory_mb', 1024), ('cpu_cores', 1), ('cpu_seconds', 120), ('disk_mb', 128)]:
        values[key] = int(limits.get(key, default))
    values['timeout_seconds'] = float(limits.get('timeout_seconds', 120))
    values['required_packages'] = sorted(limits.get('required_packages', []))
    values['torch_memory_cap'] = bool(limits.get('torch_memory_cap')) or any(
        str(p).split('=')[0].split('>')[0].split('<')[0] in {'torch', 'pytorch'} for p in values['required_packages'])
    if limits.get('execution_mode') == 'local':
        values['python_executable'] = str(Path(limits.get('python_executable') or sys.executable).resolve()).casefold()
    if limits.get('comparison_transport'):
        values['comparison_io'] = io_limits(limits, params_size=len(canonical(params)))
        values['memory_mb'] = values['comparison_io']['child_memory_mb']
    return values


def fingerprint(result, params):
    value = {'code': result['code'], 'params': params}
    if result.get('runtime_bundle'):
        value['code_bundle_sha256'] = result['runtime_bundle']['sha256']
    return _digest(value)


def execution_identity(result, params, limits):
    transport = limits.get('comparison_transport') or {}
    return digest({'fingerprint': fingerprint(result, params), 'resources': normalized_limits(limits, params),
        'bindings': transport.get('bindings'),
        # Additive diagnostic decoder repairs do not change executable inputs.
        'original_contract_sha256': transport.get('protocol_repair', {}).get('original_contract_sha256')
            or (digest(transport['contract']) if transport else None)})


def _inside(path, root):
    path, root = Path(path), Path(root).resolve()
    if not path.resolve().is_relative_to(root):
        raise ValueError('Saved acceptance evidence escaped the task directory')
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part, 'is_junction', lambda: False)():
            raise ValueError('Saved acceptance evidence contains a link')
        if part == root:
            break
    return path


def _compatible(record, result, params, limits):
    previous = record.get('limits', {})
    expected = normalized_limits(limits, params)
    actual = {key: copy.deepcopy(previous.get(key)) for key in expected}
    actual['required_packages'] = sorted(actual.get('required_packages') or [])
    if actual.get('python_executable'):
        actual['python_executable'] = str(Path(actual['python_executable']).resolve()).casefold()
    historical = limits.get('historical_execution_policy') or {}
    if historical and record.get('storage_key') in historical.get('source_keys', []):
        # A read-only replay checks the original admission. Today's fluctuating
        # free VRAM is not a new resource grant or an input to a past execution.
        if actual.get('gpu_memory_mb') == historical.get('original_gpu_memory_mb'):
            expected['gpu_memory_mb'] = actual['gpu_memory_mb']
    if actual != expected:
        return False
    transport = limits.get('comparison_transport') or {}
    old = previous.get('comparison_transport') or {}
    allowed = {digest(transport.get('contract'))}
    if transport.get('protocol_repair'):
        allowed.add(transport['protocol_repair']['original_contract_sha256'])
    return (old.get('bindings') == transport.get('bindings')
        and old.get('plan') == transport.get('plan') and digest(old.get('contract')) in allowed
        and record.get('fingerprint') == fingerprint(result, params))


def find(engine, run_id, result, params, limits, *, relocated=False):
    """Return one authenticated completed execution, without touching its receipt."""
    if not limits.get('comparison_transport'):
        return None
    with engine.store._read_db() as db:
        keys = [row[0] for row in db.execute("""SELECT key FROM records
            WHERE collection='codex_checks' AND json_extract(data,'$.run_id')=?
              AND json_extract(data,'$.fingerprint')=? AND json_extract(data,'$.status')='completed'
            ORDER BY updated_at DESC,key DESC""", (run_id, fingerprint(result, params)))]
    for key in keys:
        policy = limits.get('historical_execution_policy')
        if policy and key not in policy['source_keys']:
            continue
        record = engine.store.get('codex_checks', key)
        if not _compatible(record, result, params, limits):
            continue
        if record.get('provenance') != 'real_experiment_execution' or record.get('exit_code') != 0:
            continue
        root = engine.run_dir(run_id) / 'codex-acceptance'
        path = _inside(record['artifact_path'], root)
        bundle = result.get('runtime_bundle')
        if bundle:
            from .code_bundle import validate
            validate({**bundle, 'root': str(path)}, result['code'])
        elif identity(_inside(path / 'experiment.py', root))['sha256'] != result['code_sha256']:
            raise ValueError('Saved acceptance code bytes changed')
        original_params = json.loads(_inside(path / 'params.json', root).read_bytes())
        bootstrap = json.loads(_inside(path / 'bootstrap.json', root).read_bytes())
        environment = json.loads(_inside(path / 'environment.json', root).read_bytes())
        if (canonical(original_params) != canonical(params)
                or any(bootstrap.get(k) != v for k, v in record['limits'].items())
                or environment.get('code_sha256') != result['code_sha256']
                or environment.get('params_sha256') != _digest(params)):
            raise ValueError('Saved execution input, environment or resource receipt changed')
        archive = record.get('output_archive') or {}
        output = _inside(archive.get('path', ''), root)
        if (not archive.get('complete') or output.resolve() != (path / 'stdout.txt').resolve()
                or identity(output) != {k: archive.get(k) for k in ('bytes', 'sha256')}):
            raise ValueError('Saved acceptance output failed its original SHA256 verification')
        parsed = read_final_output(output)
        if parsed.get('metrics') != record.get('metrics'):
            raise ValueError('Saved metrics differ from the archived output')
        restored = {**record, 'experiment_metadata': {k: v for k, v in parsed.items() if k not in {'metrics', 'artifacts'}}}
        restored['execution_reuse'] = {'source_check_id': record['id'], 'source_storage_key': key,
            'output_sha256': archive['sha256'], 'runtime_sha256': (bundle or {}).get('sha256') or result['code_sha256'],
            'execution_identity': execution_identity(result, params, limits),
            'source_finished_at': record.get('finished_at'), 'source_environment_path': record.get('environment_path'),
            'scope': 'Original execution reused; current delivery and output contract rechecked; no new model fit'}
        return restored
    if not relocated:
        from .code_layout_relocation import predecessor
        relocation = predecessor(engine, run_id, result)
        if relocation:
            old = relocation['previous']
            restored = find(engine, run_id, old, params, {**limits, 'code_bundle': old['runtime_bundle']}, relocated=True)
            if restored:
                restored['execution_reuse']['source_relocation'] = {
                    'id': relocation['id'], 'sha256': relocation['sha256'],
                    'current_runtime_sha256': result['runtime_bundle']['sha256'], 'scope': relocation['scope']}
                return restored
    return None


def replay_policy(engine, run_id, result):
    """Read an operator-authorized, delivery/config-bound no-execution replay."""
    sha = result.get('runtime_sha256') or (result.get('runtime_bundle') or {}).get('sha256')
    row = engine.store.get('codex_acceptance_replays', run_id + ':' + str(sha))
    if not row:
        return None
    config = engine.store.get('runs', run_id)['config']
    if (row.get('sha256') != digest({k: v for k, v in row.items() if k != 'sha256'})
            or row.get('run_id') != run_id or row.get('runtime_sha256') != sha
            or row.get('config_sha256') != digest(config) or row.get('mode') != 'reuse_only'
            or row.get('original_gpu_memory_mb', 0) > config.get('vram_limit_gb', 0) * 1024):
        raise ValueError('Historical acceptance replay no longer matches authorized inputs')
    return row
