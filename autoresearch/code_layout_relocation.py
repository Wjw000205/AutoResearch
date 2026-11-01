"""Audited src relocation with exact byte transformations, not arbitrary equivalence."""
import copy
from pathlib import Path

from .comparison_transport import digest
from .storage import now

COLLECTION = 'code_layout_relocations'


def destination(name):
    return 'src/' + name if name.endswith('.py') and name != 'experiment.py' else name


def relocated_bytes(name, content):
    if name == 'experiment.py':
        before, after = b'Path(__file__).resolve().parent)', b'Path(__file__).resolve().parent / "src")'
    elif name == 'configs/frozen.py':
        before, after = b'Path(__file__).parent / "method_configs.json"', b'Path(__file__).resolve().parents[2] / "configs" / "method_configs.json"'
    elif name == 'utils/host.py':
        before, after = b'Path(__file__).resolve().parents[1] / "experiment.py"', b'Path(__file__).resolve().parents[2] / "experiment.py"'
    else:
        return content
    if content.count(before) != 1:
        raise ValueError('Unsupported src relocation entry/config loader')
    return content.replace(before, after, 1)


def verify(previous, current):
    from .code_bundle import validate
    old, new = previous['runtime_bundle'], current['runtime_bundle']
    validate(old, previous['code'])
    validate(new, current['code'])
    mapped = {destination(row['path']) for row in old['files']}
    if mapped != {row['path'] for row in new['files']}:
        raise ValueError('Src relocation added or removed runtime files')
    if previous['dependencies'] != current['dependencies']:
        raise ValueError('Src relocation changed dependencies')
    for row in old['files']:
        name = row['path']
        if relocated_bytes(name, (Path(old['root']) / name).read_bytes()) != (Path(new['root']) / destination(name)).read_bytes():
            raise ValueError('Src relocation changed implementation bytes: ' + name)


def install(engine, run_id, previous, current, *, workflow_id, verification):
    if engine.store.get('runs', run_id, {}).get('status') not in {'paused', 'blocked', 'failed'}:
        raise ValueError('Pause before recording a source relocation')
    if engine.store.get('runtime', 'worker', {}).get('held'):
        raise ValueError('Execution lease is still held')
    verify(previous, current)
    if not verification.get('passed'):
        raise ValueError('Isolated import/development checks are required for relocation')
    receipt = {'id': run_id + ':src-layout:' + current['runtime_bundle']['sha256'],
        'run_id': run_id, 'workflow_id': workflow_id, 'profile': 'src-layout-v2',
        'previous': {k: copy.deepcopy(previous[k]) for k in ('code', 'code_sha256', 'runtime_bundle', 'dependencies')},
        'runtime_sha256': current['runtime_bundle']['sha256'], 'verification': verification,
        'created_at': now(), 'scope': 'Exact module moves plus entry/import and config-location changes only; original execution remains historical; independently review final layout.'}
    receipt['sha256'] = digest(receipt)
    old = engine.store.get(COLLECTION, receipt['id'])
    if old:
        predecessor(engine, run_id, current)
        return old
    engine.store.put(COLLECTION, receipt['id'], receipt)
    return receipt


def predecessor(engine, run_id, current):
    sha = (current.get('runtime_bundle') or {}).get('sha256')
    receipt = engine.store.get(COLLECTION, run_id + ':src-layout:' + str(sha))
    if not receipt:
        return None
    if (receipt.get('sha256') != digest({k: v for k, v in receipt.items() if k != 'sha256'})
            or receipt.get('run_id') != run_id or receipt.get('runtime_sha256') != sha
            or receipt.get('profile') != 'src-layout-v2'):
        raise ValueError('Source relocation receipt changed')
    verify(receipt['previous'], current)
    return receipt
