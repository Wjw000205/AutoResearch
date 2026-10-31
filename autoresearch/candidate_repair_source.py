"""Freeze an unapproved delivery as a repair input, never as an active model."""
import copy
import hashlib
import json
from pathlib import Path

from .delivery_files import io_path
from .experiment_workflow import sha

KEY = 'candidate_repair_source'
COLLECTION = 'candidate_repair_sources'
SUPPORT = {'.py', '.json', '.yaml', '.yml', '.txt', '.diff'}
RESERVED = {'_delivery/changes.json', '_delivery/checks.py', '_delivery/rationale.txt'}
SUPPORT_TOTAL_BYTES = 16 * 1024 * 1024


class DeliveryCollectionError(ValueError):
    """A saved delivery cannot be collected; another proposal cannot fix it."""


def file_limit(name):
    # Constraint inventories/examples are review data, not executable source.
    # Preserve the source limit and keep both individual and aggregate bounds.
    return (4 if name.startswith('_delivery/') and name.endswith('.json')
            and name not in RESERVED else 1) * 1024 * 1024


def review_turn_capacity(context):
    scope = context.get('rapid_revision') or {}
    if scope.get('review_read_protocol') not in (2, 3):
        return None
    pages = sum((r['end'] - r['start'] + 5999) // 6000 for r in scope.get('changed_ranges', []))
    return 8 + 2 * pages


def read_file(root, name):
    from .code_bundle import relative_file
    relative_file(name)
    root, path = io_path(Path(root)), io_path(Path(root) / name)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Candidate repair file escaped its workspace')
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part, 'is_junction', lambda: False)():
            raise ValueError('Candidate repair source contains a linked path')
        if part == root:
            break
    if not path.is_file() or path.stat().st_size > file_limit(name):
        raise DeliveryCollectionError('Candidate repair file is absent or oversized: ' + name)
    return path.read_bytes()


def support_files(root):
    folder = io_path(Path(root) / '_delivery')
    if not folder.is_dir():
        return {}
    result = {}
    for path in folder.rglob('*'):
        if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
            raise ValueError('Candidate check support contains a linked path')
        if path.is_file() and path.suffix in SUPPORT:
            name = path.relative_to(io_path(Path(root))).as_posix()
            if name not in RESERVED:
                result[name] = read_file(root, name)
                if sum(map(len, result.values())) > SUPPORT_TOTAL_BYTES:
                    raise DeliveryCollectionError('Candidate check support exceeds total byte limit')
    if len(result) > 64:
        raise DeliveryCollectionError('Candidate has too many check support files')
    return result


def _native_delivery(engine, run_id, previous, control):
    from .scoped_codex_recovery import current
    job = previous.get('codex_work_item') or {}
    if not job or current(job).get('checkpoint', {}).get('status') != 'completed':
        return None
    root = job['workspace']
    order = read_file(root, 'work_order.json')
    if hashlib.sha256(order).hexdigest() != job['input_sha256']:
        raise ValueError('Previous candidate work order changed')
    entries = json.loads(read_file(root, '_delivery/changes.json'))
    names = [x['path'] for x in entries]
    if not entries or len(names) != len(set(names)) or len(names) > 128:
        raise ValueError('Previous candidate manifest is invalid')
    runtime_names = {n for n in names if not n.startswith('_delivery/')}
    if 'work_order.json' in runtime_names:
        raise ValueError('Work order cannot become a runtime file')
    author_base = read(engine, run_id, previous)[0] if previous.get(KEY) else control
    runtime = {n: read_file(root, n) for n in set(author_base) | runtime_names}
    if any(runtime[n] != old and n not in runtime_names for n, old in author_base.items()):
        raise ValueError('Previous candidate contains an undeclared runtime edit')
    if previous.get('candidate'):
        from .rapid_experiment_revision import _candidate_unchanged
        _candidate_unchanged(previous)
        bundle = previous['candidate']['bundle']
        expected = {f['path']: read_file(bundle['root'], f['path']) for f in bundle['files']}
        if runtime != expected:
            raise ValueError('Previous native workspace differs from its reviewed candidate snapshot')
    support = support_files(root)
    support['_delivery/checks.py'] = read_file(root, '_delivery/checks.py')
    support['_delivery/rationale.txt'] = read_file(root, '_delivery/rationale.txt')
    return runtime, support, sha(current(job)['checkpoint'])


def prepare(engine, run_id, record, control):
    """A fresh work item may use its exact predecessor with the same control."""
    if record.get(KEY):
        return read(engine, run_id, record)
    previous_id = record.get('source_basis', {}).get('previous_candidate', {}).get('id')
    previous = engine.store.get('rapid_experiment_revisions', previous_id or '', {})
    if not previous or not previous.get('yaml_migration'):
        return None
    if previous.get('status') == 'activated':
        if (previous.get('run_id') != run_id
                or previous.get('candidate', {}).get('program') != record['base_program']
                or record['base_program'].get('rapid_revision_id') != previous_id):
            raise ValueError('Activated predecessor is not the exact current base program')
        # Its reviewed runtime is already the control, not a failed candidate.
        return None
    if (previous.get('run_id') != run_id or previous.get('binding') != record['binding']
            or previous.get('base_program') != record['base_program']
            or previous.get('status') not in {'rejected', 'check_failed', 'invalid'}):
        raise ValueError('Previous YAML candidate is not bound to the same repair control and evidence')
    delivered = _native_delivery(engine, run_id, previous, control)
    if delivered is None:
        raise ValueError('Previous YAML delivery has no completed receipt; reconcile it before another repair')
    runtime, support, receipt_sha = delivered
    # The delivered runtime must really be the YAML candidate that is being
    # repaired. A partial/missing migration stays on the normal creation path.
    needed = {'configs/hyperparameters.yaml', 'configs/hyperparameters.schema.json',
              'configs/hyperparameters.contract.json'}
    if not needed.issubset(runtime):
        return None
    contract = json.loads(runtime['configs/hyperparameters.contract.json'])
    if contract.get('loader') not in runtime:
        raise ValueError('Previous YAML candidate has no declared runtime loader')
    from .rapid_experiment_revision import _write_files
    identity = sha({'parent': previous_id, 'runtime': {n: hashlib.sha256(v).hexdigest() for n,v in runtime.items()},
        'support': {n: hashlib.sha256(v).hexdigest() for n,v in support.items()}, 'receipt': receipt_sha})
    root = engine.run_dir(run_id).resolve() / 'candidate-repair-sources' / identity
    runtime_bundle = _write_files(root / 'runtime', runtime)
    support_bundle = _write_files(root / 'support', support)
    body = {'version': 1, 'parent_revision_id': previous_id, 'parent_decision_sha256': previous['decision_sha256'],
        'binding': copy.deepcopy(record['binding']), 'runtime': runtime_bundle, 'support': support_bundle,
        'native_receipt_sha256': receipt_sha, 'approval_inherited': False}
    ref = {'id': run_id + ':candidate-repair-source:' + sha(body), 'sha256': sha(body)}
    engine.store.mutate(COLLECTION, ref['id'], lambda old: old or {'id': ref['id'], 'run_id': run_id, 'body': body})
    record[KEY] = ref
    record['source_basis'].update(inherits_previous_candidate=True, repair_source=copy.deepcopy(ref),
        inheritance_scope='Unapproved isolated repair input; active control and independent approval unchanged')
    return read(engine, run_id, record)


def read(engine, run_id, record):
    ref = record[KEY]
    saved = engine.store.get(COLLECTION, ref['id'], {})
    body = saved.get('body', {})
    if (saved.get('run_id') != run_id or sha(body) != ref['sha256']
            or body.get('binding') != record['binding'] or body.get('approval_inherited') is not False):
        raise ValueError('Candidate repair source lost its frozen binding')
    def files(bundle):
        result = {}
        for entry in bundle['files']:
            raw = read_file(bundle['root'], entry['path'])
            if len(raw) != entry['bytes'] or hashlib.sha256(raw).hexdigest() != entry['sha256']:
                raise ValueError('Candidate repair source file changed')
            result[entry['path']] = raw
        from .code_bundle import digest
        if digest(bundle['files']) != bundle['sha256']:
            raise ValueError('Candidate repair source manifest changed')
        return result
    return files(body['runtime']), files(body['support'])


def tests_only(record):
    targets = [c['target'] for c in record['decision']['changes']]
    return bool(targets) and all('_delivery/' in t for t in targets)


def inherited_paths(engine, run_id, record, patch):
    rows = [*patch.get('edits', []), *patch.get('new_files', [])]
    inherited = {r['path'] for r in rows if r['change_index'] is None}
    if not record.get(KEY):
        if inherited or patch.get('repair_source_sha256') or patch.get('retained_runtime'):
            raise ValueError('Inherited candidate edits require a frozen repair source')
        return set()
    if patch.get('repair_source_sha256') != record[KEY]['sha256']:
        raise ValueError('Patch changed its candidate repair source')
    runtime, _ = read(engine, run_id, record)
    seen = set()
    for item in patch.get('retained_runtime', []):
        if (item['path'] in seen or item['path'] not in runtime
                or hashlib.sha256(runtime[item['path']]).hexdigest() != item['sha256']):
            raise ValueError('Retained implementation does not match the frozen repair source')
        seen.add(item['path'])
    for row in rows:
        if row['change_index'] is None or tests_only(record):
            if row['path'] not in runtime or row.get('new', row.get('source', '')).encode() != runtime[row['path']]:
                raise ValueError('Test-only repair changed retained candidate runtime')
    return inherited
