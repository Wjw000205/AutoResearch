"""Engineer navigation for the current experiment, its saved configs and host source."""
import hashlib
import re
from pathlib import Path

from .experiment_analysis import frozen
from .experiment_evidence_handoff import _file
from .storage import redact

TOOL = 'experiments.files'
WEIGHT_SUFFIXES = ('.pt', '.pth', '.ckpt', '.safetensors')
HOST = Path(__file__).resolve().parent
GUIDANCE = ('工程师可自行查找文件。用 experiments.files 按 area 和 query 检索源码、配置版本、验证产物或宿主实现，'
            '按返回的 read_call 读取正文和后续分页。先自行核对所需文件，再提交可表达的单因素提案。')


def _entry(path, name, args, **fields):
    return {'path': path, **fields, 'read_call': {'name': name, 'arguments': args}}


def _host_path(name):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*\.py', name):
        raise ValueError('Choose a module in the host source directory')
    path = HOST / name
    if path.is_symlink() or (path.exists() and not path.is_file()) or path.resolve().parent != HOST:
        raise ValueError('Host source is not an ordinary module in this installation')
    return path


def _reports(engine, run_id, trial, suffixes=('.json',)):
    """Enumerate permitted ordinary files published directly by this validation attempt."""
    from .deletion import DeletionError, _plain_path
    from .experiment_views import _safe_file
    root = engine.runner.root / run_id
    directory = Path(trial.get('artifact_path') or '') / 'artifacts'
    if not directory.is_absolute():
        return
    try:
        _plain_path(directory, root.resolve())
        if not directory.is_dir():
            return
        for path in directory.iterdir():
            if (path.suffix.lower() in suffixes and not path.name.startswith('.')
                    and not re.search(r'test|holdout|held[_-]?out|credential|secret', path.name, re.I)
                    and _safe_file(engine, run_id, trial, 'artifacts/' + path.name)):
                yield 'artifacts/' + path.name, path.stat().st_size
    except (DeletionError, OSError):
        return


def _host_source(path, frozen_sha=None):
    file = _host_path(path)
    limit = 2 * 1024 * 1024
    try:
        with file.open('rb') as stream:
            raw = stream.read(limit + 1)
    except FileNotFoundError:
        if not frozen_sha:
            raise ValueError('Host source is unavailable in this installation') from None
        raw = None
    digest = hashlib.sha256(raw).hexdigest() if raw is not None and len(raw) <= limit else None
    if frozen_sha and digest != frozen_sha:
        from .diagnostic_check_evidence import archived_module
        archived = archived_module(path, frozen_sha)
        if archived is None:
            raise ValueError('Frozen host source bytes are unavailable for ' + path)
        raw, digest = archived[0], frozen_sha
    if len(raw) > limit:
        raise ValueError('Host source exceeds the reading limit')
    text = raw.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
    return redact(text), digest


def _body(engine, run_id, evidence, area, path):
    if area == 'host':
        return _host_source(path)[0], 'installed_host_source'
    from .experiment_workflow import encoded
    trial_id, _, relative = path.partition('/')
    trial = next((r for r in evidence['records'] if r['id'] == trial_id and _validation_trial(r, run_id)), None)
    binary = Path(relative).suffix.lower() in WEIGHT_SUFFIXES
    if not trial or relative not in dict(_reports(engine, run_id, trial, WEIGHT_SUFFIXES if binary else ('.json',))):
        raise ValueError('Choose a report from the completed validation trial directory')
    report = _file(engine, run_id, trial, relative, json_content=not binary)
    if report['status'] != 'available':
        raise ValueError(report['reason'])
    if binary:
        return encoded({'path': relative, 'file_sha256': report['sha256'], 'bytes': report['bytes'],
                        'scope': 'Saved file bytes only; selection and loading unverified.'}), 'saved_validation_file_metadata'
    return encoded(redact(report['data'])), 'saved_validation_report'


def _validation_trial(row, run_id):
    return (row.get('run_id') == run_id and row.get('status') == 'completed'
            and not row.get('superseded') and row.get('params', {}).get('phase') == 'validation')


def replay_page(engine, run_id, context, args, saved):
    page = browse(engine, run_id, context, args)
    if saved.get('source') == 'installed_host_source' and page.get('source') == 'frozen_host_source':
        # Historical receipts authenticated the rendered page and carried no raw digest.
        if 'file_sha256' in saved or args.get('expected_sha256') not in (None, page['sha256']):
            return False
        page = {k: v for k, v in page.items() if k not in ('file_sha256', 'file_hash_scope')}
        page['source'] = 'installed_host_source'
    return page == saved


def browse(engine, run_id, context, args):
    ref = context['validation_evidence']
    evidence = frozen(engine, run_id, context, {'evidence_id': ref['id'], 'expected_sha256': ref['sha256']})
    area, path = args.get('area', 'source'), args.get('path')
    offset = args.get('offset', 0)
    if path is not None:
        if area not in {'host', 'validation'}:
            raise ValueError('Use the reader returned by the directory for this file')
        expected = args.get('expected_sha256')
        contract = context.get('experiment_decision', {}).get('cpu_behavior_check', {}).get('staged_observations', {})
        if not contract:
            contract = context.get('rapid_revision', {}).get('host_execution', {}).get('observation_delivery', {})
        frozen_sha = contract.get('host_source_sha256', {}).get(path) if area == 'host' else None
        if frozen_sha:
            text, _ = _host_source(path, frozen_sha)
            source = 'installed_host_source'
        else:
            text, source = _body(engine, run_id, evidence, area, path)
        checksum = hashlib.sha256(text.encode()).hexdigest()
        raw_read = frozen_sha is not None and expected == frozen_sha
        if (expected is not None and expected != checksum and not raw_read) or (offset and expected is None):
            from .tool_navigation import NavigationError
            raise NavigationError('File changed or continuation lacks its content SHA256',
                code='file_version_discovery_required', next_calls=[{'name': TOOL,
                    'arguments': {'area': area, 'query': path, 'offset': 0, 'limit': 12}}],
                details={'action': 'Discover the current file and read from offset zero before continuing. Prior reads retain their original version.'})
        if not 0 <= offset <= len(text):
            from .tool_navigation import NavigationError
            raise NavigationError('File offset is outside its content', code='file_window_unavailable',
                next_calls=[{'name': TOOL, 'arguments': {'area': area, 'path': path,
                    'expected_sha256': checksum, 'offset': 0, 'limit': 4000}}],
                details={'total_chars': len(text)})
        end = min(offset + args.get('limit', 4000), len(text))
        return {'path': path, 'source': 'frozen_host_source' if frozen_sha else source, 'sha256': checksum, 'hash_scope': 'displayed_text_utf8',
                **({'file_sha256': frozen_sha, 'file_hash_scope': 'original_bytes'} if frozen_sha else {}),
                'text': text[offset:end],
                'total_chars': len(text), 'offset': offset, 'end': end,
                'next_offset': end if end < len(text) else None, 'execution_authorized': False}
    entries = []
    if area == 'source':
        from .source_read_identity import source_files
        for name, row in source_files(engine.store, run_id, context).items():
            if isinstance(row.get('text'), str):
                entries.append(_entry(name, 'experiments.inspect_module', {'evidence_id': ref['id'],
                    'expected_sha256': ref['sha256'], 'path': name, 'view': 'source'}, sha256=row['sha256']))
    elif area == 'yaml':
        from .hyperparameter_yaml import COLLECTION, leaves
        if not context.get('experiment_decision', {}).get('hyperparameter_yaml'):
            raise ValueError('This assignment has no YAML contract')
        versions = {v['id']: v for v in engine.store.list(COLLECTION, run_id) if v['body']['binding'] == evidence['binding']}
        for identifier, row in versions.items():
            body = row['body']
            parent = versions.get(body['parent'])
            changes = []
            if parent:
                before, after = dict(leaves(parent['body']['document'])), dict(leaves(body['document']))
                changes = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
            entries.append(_entry(identifier, 'hyperparameters.read', {'revision_id': identifier,
                'expected_sha256': body['yaml_sha256']}, controls=body['controls'], changed_paths=changes,
                created_at=row['created_at']))
    elif area == 'validation':
        for trial in evidence['records']:
            if _validation_trial(trial, run_id):
                for relative, size in _reports(engine, run_id, trial, WEIGHT_SUFFIXES):
                    name = trial['id'] + '/' + relative
                    entries.append(_entry(name, TOOL, {'area': area, 'path': name}, bytes=size,
                                          scope='File size and digest only; no weight deserialization.'))
                for relative, size in _reports(engine, run_id, trial):
                    name = trial['id'] + '/' + relative
                    if size <= 512 * 1024:
                        entries.append(_entry(name, TOOL, {'area': area, 'path': name}, bytes=size))
                    else:
                        entries.append({'path': name, 'bytes': size, 'unavailable_reason': 'JSON report exceeds 512 KiB'})
    elif area == 'host':
        for file in HOST.glob('*.py'):
            if not file.is_symlink() and file.is_file():
                entries.append(_entry(file.name, TOOL, {'area': area, 'path': file.name}))
    else:
        raise ValueError('Unknown file directory')
    query = args.get('query', '').casefold()
    matches = sorted((e for e in entries if query in ' '.join([e['path'], *e.get('changed_paths', []),
                     str(e.get('controls', {}).get('base_unit_id', ''))]).casefold()), key=lambda e: e['path'])
    end = min(offset + min(args.get('limit', 12), 30), len(matches))
    return {'area': area, 'entries': matches[offset:end], 'total_files': len(matches),
            'next_offset': end if end < len(matches) else None, 'execution_authorized': False}
