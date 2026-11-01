"""Check-worker compatibility for pathlib.Path.is_junction on Python 3.10/3.11.

Match Python 3.12's Windows junction test using the existing lstat reparse tag.
Do not replace a native implementation and never silently disable the guard.
This module is copied only into a newly reviewed CPU-check bundle.
"""
import os
import stat
import hashlib
import json
import sys
from pathlib import Path

CHECK_WORKSPACE_LAYOUT = 'rapid-v1'


def is_junction(path):
    if os.name != 'nt':
        os.fspath(path)
        return False
    try:
        result = os.lstat(path)
    except (OSError, ValueError):
        return False
    if not hasattr(result, 'st_reparse_tag'):
        raise RuntimeError('Windows junction detection is unavailable')
    return result.st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT


def install():
    if not hasattr(Path, 'is_junction'):
        Path.is_junction = is_junction
    if os.name == 'nt':
        current = os.getcwd()
        if not current.startswith('\\\\?\\'):
            # The physical directory is unchanged. Absolute paths derived by
            # the check now retain Win32 extended syntax, including atomic
            # .json.<uuid>.pending writes deeper than MAX_PATH.
            extended = '\\\\?\\UNC\\' + current[2:] if current.startswith('\\\\') else '\\\\?\\' + current
            os.chdir(extended)


def prepare_delivery_invocation():
    """Carry the runner's exact receipts into its relocated check directory."""
    root = Path.cwd()
    snapshot = root / '_delivery_snapshot'
    if not snapshot.exists():
        return
    if snapshot.is_symlink() or snapshot.is_junction() or not snapshot.is_dir():
        raise ValueError('Invalid delivery invocation directory')
    params_path = root / 'params.json'
    try:
        argument=Path(sys.argv[1])
        actual=(argument.name=='params.json' and not argument.is_symlink()
                and not argument.is_junction() and argument.parent.samefile(root)
                and argument.samefile(params_path))
    except (IndexError,OSError,ValueError):
        actual=False
    if not actual:
        raise ValueError('Delivery check requires the actual runner params path')
    originals = {}
    for name in ('params.json', 'environment.json'):
        path = root / name
        if path.is_symlink() or path.is_junction():
            raise ValueError('Linked invocation receipt')
        originals[name] = path.read_bytes()
        if (snapshot / name).exists() or (snapshot / name).is_symlink():
            raise ValueError('Candidate occupied a host invocation receipt')
    params = json.loads(originals['params.json'])
    environment = json.loads(originals['environment.json'])
    digest = hashlib.sha256(json.dumps(params, sort_keys=True, ensure_ascii=False,
                                       allow_nan=False).encode('utf-8')).hexdigest()
    if (params.get('phase') != 'code_check' or environment.get('params_sha256') != digest
            or environment.get('code_sha256') != hashlib.sha256((root / 'experiment.py').read_bytes()).hexdigest()
            or environment.get('check_workspace_layout') != CHECK_WORKSPACE_LAYOUT):
        raise ValueError('Invocation receipts do not bind this check launcher')
    for name, raw in originals.items():
        with (snapshot / name).open('xb') as stream:
            stream.write(raw)
        if (snapshot / name).read_bytes() != raw:
            raise ValueError('Invocation receipt copy changed')
