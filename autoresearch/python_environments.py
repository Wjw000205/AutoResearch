"""Bounded local interpreter discovery and isolated, credential-free inspection."""
from __future__ import annotations

import concurrent.futures
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

from .config import ROOT
from .storage import now, redact

_CACHE = {}
_LOCK = threading.Lock()


def validate_python(value: str) -> str:
    if not value.strip():
        return ''
    path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        raise ValueError('Python 路径必须是本机绝对路径')
    if path.is_dir():
        path = next((p for p in (path / 'Scripts/python.exe', path / 'python.exe', path / 'bin/python') if p.is_file()), path)
    # Keep the venv launcher path; resolving its symlink can switch to the base interpreter.
    if not path.is_file() or not re.fullmatch(r'python(?:3(?:\.\d+)?)?(?:\.exe)?', path.name, re.I):
        raise ValueError('请选择已存在的 Python 解释器或虚拟环境目录')
    return str(path.absolute())


def interpreter_environment(executable: str, base: dict | None = None) -> dict:
    env = dict(base) if base is not None else {k: v for k, v in os.environ.items()
        if k.upper() in {'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'LANG'}}
    parent = Path(executable).parent
    prefix = parent.parent if parent.name.lower() in {'scripts', 'bin'} else parent
    paths = [parent, prefix, prefix / 'Library/bin', prefix / 'Library/usr/bin', prefix / 'Scripts', prefix / 'bin']
    env['PATH'] = os.pathsep.join([*(str(p) for p in paths if p.is_dir()), env.get('PATH', '')])
    env.update(OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', PYTORCH_NVML_BASED_CUDA_CHECK='1')
    return env


def _probe(executable: str, code: str, timeout: int = 5) -> dict:
    result = subprocess.run([executable, '-I', '-X', 'utf8', '-c', code],
        env=interpreter_environment(executable), capture_output=True, text=True, encoding='utf-8',
        timeout=timeout, check=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return json.loads(result.stdout.splitlines()[-1])


_IDENTITY = "import json,sys,platform; print(json.dumps({'python_version':platform.python_version(),'python_prefix':sys.prefix,'is_virtualenv':sys.prefix!=sys.base_prefix}))"


def inspect_python(executable: str) -> dict:
    executable = validate_python(executable) or sys.executable
    stamp = Path(executable).stat().st_mtime_ns
    key = (os.path.normcase(executable), stamp)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached and time.monotonic() < cached[0]:
            return json.loads(json.dumps(cached[1]))
    result = {'python_executable': executable, 'provenance': 'selected_experiment_interpreter', 'checked_at': now()}
    try:
        result.update(_probe(executable, _IDENTITY))
        packages = _probe(executable, "import importlib.metadata as m,json,re; print(json.dumps({re.sub(r'[-_.]+','-',d.metadata['Name']).lower():d.version for d in m.distributions() if d.metadata.get('Name')}))")
        result.update(installed_distributions=packages, inventory_verified=True, inventory_error=None)
        torch = {'installed': 'torch' in packages, 'cuda_available': False, 'probe_status': 'not_installed'}
        if 'torch' in packages:
            try:
                torch.update(_probe(executable, "import json,torch; print(json.dumps({'version':torch.__version__,'cuda_available':bool(torch.cuda.is_available()),'cuda_build':torch.version.cuda}))", 8))
                torch['probe_status'] = 'checked_in_isolated_process'
            except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
                torch.update(cuda_available=None, probe_status='unavailable', error=type(exc).__name__)
        result['gpu_runtime'] = {'torch': torch}
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
        result.update(inventory_verified=False, inventory_error=type(exc).__name__, installed_distributions={})
    with _LOCK:
        _CACHE[key] = (time.monotonic() + 60, result)
        while len(_CACHE) > 64:
            _CACHE.pop(next(iter(_CACHE)))
    return json.loads(json.dumps(result))


def discover_candidates() -> list[dict]:
    """Inspect known env registries and shallow locations, never walk entire disks."""
    candidates = {}

    def add(path, source):
        if len(candidates) >= 100:
            return
        try:
            executable = validate_python(str(path))
            if executable:
                candidates.setdefault(os.path.normcase(executable), {'executable': executable, 'source': source})
        except (ValueError, OSError):
            pass

    def children(path):
        try:
            # Bound both discovery work and response size even in large project folders.
            from itertools import islice
            return [p for p in islice(path.iterdir(), 300) if p.is_dir()]
        except OSError:
            return []

    add(sys.executable, '当前服务')
    add(sys.prefix, '当前环境')
    add(sys.base_prefix, '基础环境')
    home = Path.home()
    for key in ('VIRTUAL_ENV', 'CONDA_PREFIX'):
        if os.environ.get(key):
            add(os.environ[key], key)
    registry = home / '.conda/environments.txt'
    try:
        for line in registry.read_text(encoding='utf-8').splitlines()[:200]:
            if line.strip():
                add(line.strip(), 'Conda 注册环境')
    except OSError:
        pass
    roots = {Path(sys.base_prefix), home / 'anaconda3', home / 'miniconda3', home / 'miniforge3',
             Path(os.environ.get('PROGRAMDATA', str(home))) / 'anaconda3'}
    if os.name == 'nt':
        for drive in 'CDEFGHIJKLMNOPQRSTUVWXYZ':
            roots.update(Path(f'{drive}:/') / name for name in ('Anaconda3', 'Miniconda3', 'Miniforge3'))
    for entry in os.environ.get('PATH', '').split(os.pathsep):
        if entry:
            folder = Path(entry)
            add(folder / ('python.exe' if os.name == 'nt' else 'python3'), 'PATH')
            if folder.name.lower() in {'scripts', 'condabin'}:
                roots.add(folder.parent)
    for root in sorted(roots):
        add(root, 'Conda / Python 安装')
        for child in children(root / 'envs'):
            add(child, 'Conda envs')
    for folder in (home / '.virtualenvs', home / '.cache/pypoetry/virtualenvs',
                   Path(os.environ.get('LOCALAPPDATA', str(home))) / 'pypoetry/Cache/virtualenvs',
                   Path(os.environ.get('LOCALAPPDATA', str(home))) / 'Programs/Python'):
        for child in children(folder):
            add(child, '用户环境目录')
    projects = [ROOT, *children(ROOT.parent)]
    for project in projects:
        for name in ('.venv', 'venv', 'env', '.env'):
            add(project / name, '项目虚拟环境')
    return list(candidates.values())


def discover_python_environments() -> dict:
    def inspect(item):
        item = dict(item)
        executable = item['executable']
        parent = Path(executable).parent
        prefix = parent.parent if parent.name.lower() in {'scripts', 'bin'} else parent
        item.update(name=prefix.name, kind='conda' if (prefix / 'conda-meta').is_dir() else 'venv' if (prefix / 'pyvenv.cfg').is_file() else 'python',
                    current=os.path.normcase(executable) == os.path.normcase(sys.executable))
        try:
            item.update(_probe(executable, _IDENTITY, 3), available=True)
            if tuple(int(n) for n in item['python_version'].split('.')[:2]) < (3, 8):
                item.update(available=False, error='实验环境需要 Python 3.8 或更高版本')
        except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
            item.update(available=False, error=type(exc).__name__)
        return item
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        environments = list(pool.map(inspect, discover_candidates()))
    return {'environments': environments, 'current_executable': sys.executable, 'checked_at': now(),
        'scope': 'Conda 注册与安装目录、PATH、用户虚拟环境目录、当前及相邻项目的常用环境目录；支持 Python 3.8+，其他位置可填写绝对路径。',
        'provenance': 'local_interpreter_scan'}


def apply_execution_preferences(config: dict, hardware: dict) -> dict:
    """Attach per-task choices without rewriting the physical host measurement."""
    hardware = dict(hardware)
    docker = config.get('execution_mode') == 'docker'
    executable = config.get('python_executable', '')
    if executable and not docker:
        try:
            hardware['software'] = inspect_python(executable)
        except (ValueError, OSError) as exc:
            hardware['software'] = {'python_executable': executable, 'inventory_verified': False,
                'inventory_error': redact(str(exc)), 'installed_distributions': {}}
    hardware['execution_preferences'] = {'execution_mode': config.get('execution_mode', 'local'),
        'python_executable': '' if docker else executable,
        'gpu_index': config.get('gpu_index'), 'vram_limit_gb': config.get('vram_limit_gb'),
        'cpu_limit_cores': config.get('cpu_limit_cores'), 'ram_limit_gb': config.get('ram_limit_gb'),
        'disk_limit_gb': config.get('disk_limit_gb'),
        'vram_policy': 'min(user budget, 80% of currently free memory on one GPU); no memory reservation'}
    return hardware
