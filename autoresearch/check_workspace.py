"""Compact, plain directories for explicitly reviewed rapid CPU checks."""
import hashlib
import json
import os
import re
import stat

LAYOUT = 'rapid-v1'
STAGED_LAYOUT = 'rapid-v2'


class DiagnosticPreparationTimeout(TimeoutError):
    pass


def stage_invocation(root, bundle, locks):
    """Exclusively publish the two host receipts before starting a diagnostic process."""
    snapshot = root / '_delivery_snapshot'
    if not snapshot.is_dir() or snapshot.is_symlink() or snapshot.is_junction():
        raise ValueError('Missing ordinary diagnostic delivery directory')
    files = {}
    protected = []
    binding = {}
    order = snapshot / 'work_order.json'
    params = json.loads((root / 'params.json').read_bytes())
    if order.exists() or 'rapid_revision_id' in params:
        raw_order = order.read_bytes()
        expected = json.loads(raw_order)['check_input_contract']['params']
        if expected != params or params.get('phase') != 'code_check' or not params.get('rapid_revision_id'):
            raise ValueError('Diagnostic invocation does not match the frozen work order')
        binding['work_order_sha256'] = hashlib.sha256(raw_order).hexdigest()
    for name in ('params.json', 'environment.json'):
        original = root / name
        if original.is_symlink() or original.is_junction():
            raise ValueError('Linked diagnostic input')
        raw = original.read_bytes()
        target = snapshot / name
        with target.open('xb') as stream:
            stream.write(raw)
        target.chmod(stat.S_IREAD)
        if target.read_bytes() != raw:
            raise ValueError('Diagnostic staged receipt differs from its original')
        files[name] = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        protected.extend((original, target))
    for entry in bundle['files']:
        if entry['path'].startswith('_delivery_snapshot/'):
            target = root / entry['path']
            target.chmod(stat.S_IREAD)
            protected.append(target)
    if os.name == 'nt':
        import _winapi
        for target in protected:
            # FILE_SHARE_READ allows the worker to read these exact files but denies write/delete opens.
            handle = _winapi.CreateFile(str(target), 0x80000000, 1, 0, 3, 0x80, 0)
            locks.callback(_winapi.CloseHandle, handle)
    return {**binding, 'layout': STAGED_LAYOUT, 'staged_before': 'process_launch', 'files': files,
            'code_bundle_sha256': bundle['sha256'], 'read_only_snapshot_files': len(protected) - len(files),
            'input_protection': 'read_sharing_only' if os.name == 'nt' else 'read_only_mode_bits'}


def attempt_path(root, run_id, trial_id, attempt, params, limits):
    layout=limits.get('check_workspace_layout')
    if layout is None:
        return root/run_id/trial_id/f'attempt-{attempt:03d}'
    if (layout not in (LAYOUT, STAGED_LAYOUT) or params.get('phase')!='code_check'
            or not re.fullmatch(r'rapid-check-[a-f0-9]{24}',trial_id)):
        raise ValueError('Compact workspace requires a rapid CPU check')
    # Keep the full existing 96-bit check ID and run directory. Shorten only
    # redundant labels; no junction, drive alias, relocation or result reuse.
    return root/run_id/'_c'/trial_id.removeprefix('rapid-check-')/str(attempt)
