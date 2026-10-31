"""Independent Windows server lifetime, with identity-bound graceful shutdown.

Start-Process/DETACHED_PROCESS alone can retain the caller's enclosing job.
WMI creates the server from its system broker; BREAKAWAY also removes the WMI
provider job. The virtualenv redirector may then own its *own* worker job.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import ctypes
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

import psutil

ROOT = Path(__file__).resolve().parent.parent
HOST, PORT = '127.0.0.1', 8000


def runtime_dir(root: Path) -> Path:
    return root / '.autoresearch' / 'server'


def write_record(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def in_job(pid: int) -> bool:
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.IsProcessInJob.argtypes = [w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        result = w.BOOL()
        if not kernel.IsProcessInJob(handle, None, ctypes.byref(result)):
            raise ctypes.WinError(ctypes.get_last_error())
        return bool(result.value)
    finally:
        kernel.CloseHandle(handle)


def broker_launch(argv: list[str], cwd: Path) -> int:
    """No shell parsing of argv or paths; never fall back to an attached child."""
    payload = base64.b64encode(json.dumps({
        'command': subprocess.list2cmdline(argv), 'cwd': str(cwd),
    }).encode('utf-8')).decode('ascii')
    script = """$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'
$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('PAYLOAD')) | ConvertFrom-Json
$s=New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]0;CreateFlags=[uint32]0x09000000}
Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine=$p.command;CurrentDirectory=$p.cwd;ProcessStartupInformation=$s} | Select-Object ReturnValue,ProcessId | ConvertTo-Json -Compress
""".replace('PAYLOAD', payload)
    powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    result = subprocess.run([
        str(powershell), '-NoProfile', '-NonInteractive', '-EncodedCommand',
        base64.b64encode(script.encode('utf-16le')).decode('ascii'),
    ], capture_output=True, text=True, timeout=25, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError('Windows 进程代理启动失败：' + result.stderr[-1500:])
    receipt = json.loads(result.stdout.strip())
    if receipt.get('ReturnValue') != 0 or not receipt.get('ProcessId'):
        raise RuntimeError(f'Windows 进程代理拒绝启动：{receipt}')
    return int(receipt['ProcessId'])


def worker_args(instance: str) -> list[str]:
    return ['-m', 'autoresearch.server_runtime', 'serve', '--instance', instance]


def owned_process(root: Path, record: dict):
    """A PID alone is not identity; never signal a reused PID or another checkout."""
    if record.get('root') != str(root.resolve()) or not record.get('instance'):
        return None
    try:
        process = psutil.Process(int(record['pid']))
        if abs(process.create_time() - float(record['create_time'])) > .001:
            return None
        if process.cmdline()[1:] != worker_args(record['instance']):
            return None
        if Path(process.cwd()).resolve() != root.resolve():
            return None
        return process if process.is_running() else None
    except (KeyError, ValueError, TypeError, psutil.NoSuchProcess):
        return None


def status(root: Path) -> dict:
    path = runtime_dir(root) / 'runtime.json'
    if not path.exists():
        return {'status': 'stopped', 'url': f'http://{HOST}:{PORT}'}
    record = json.loads(path.read_text(encoding='utf-8'))
    return {**record, 'status': record.get('status', 'starting') if owned_process(root, record) else 'stopped'}


def port_open() -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=.5):
            return True
    except OSError:
        return False


def start(root: Path, *, timeout: float = 45) -> dict:
    current = status(root)
    if current['status'] != 'stopped':
        return current
    if port_open():
        raise RuntimeError('8000 端口已被其他进程占用；未启动第二个后端。')
    executable = root / '.venv/Scripts/python.exe'
    if not executable.is_file():
        raise RuntimeError('缺少 .venv\\Scripts\\python.exe，请先运行 setup.cmd。')
    directory = runtime_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    instance = uuid.uuid4().hex
    launcher_pid = broker_launch([str(executable), *worker_args(instance)], root)
    try:
        launcher = psutil.Process(launcher_pid)
    except psutil.NoSuchProcess:
        launcher = None
    # Per-launch receipts preserve failures and concurrent start attempts.
    write_record(directory / f'launch-{instance}.json', {
        'instance': instance, 'launcher_pid': launcher_pid, 'created_at': time.time(),
        'mechanism': 'Win32_Process.Create + CREATE_BREAKAWAY_FROM_JOB + CREATE_NO_WINDOW',
    })
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = status(root)
        if current['status'] == 'running':
            return current
        if current.get('instance') == instance and current['status'] == 'stopped':
            raise RuntimeError('后端启动失败，请查看 ' + str(directory / 'server.log'))
        if launcher is None or not launcher.is_running():
            raise RuntimeError('独立启动器已退出，请查看 ' + str(directory / 'server.log'))
        time.sleep(.25)
    raise RuntimeError('启动检查尚未完成，请运行 start.cmd status；日志：' + str(directory / 'server.log'))


def stop(root: Path, *, timeout: float = 30) -> dict:
    current = status(root)
    if current['status'] == 'stopped':
        return current
    # The request is scoped to one instance and only that worker observes it.
    request = runtime_dir(root) / f'stop-{current["instance"]}.json'
    write_record(request, {'instance': current['instance']})
    deadline = time.monotonic() + timeout
    while owned_process(root, current) and time.monotonic() < deadline:
        time.sleep(.25)
    observed = status(root)
    if observed['status'] != 'stopped':
        observed['message'] = '正在正常停止并保存检查点；未强制结束进程。'
    return observed


async def run_server(root: Path, record: dict) -> None:
    import uvicorn
    from .config import load_env
    from .codex_delivery import codex_status
    from .local_codex import LocalCodexError
    directory = runtime_dir(root)
    load_env()
    # Read-only executable preflight, not account verification or a model call.
    # Keep the web UI available to fix configuration even when Codex is missing.
    try:
        readiness = await codex_status(str(root))
    except (LocalCodexError, OSError, asyncio.TimeoutError):
        readiness = {'available': False, 'version': None, 'error_code': 'codex_probe_failed',
                     'error': '本机 Codex 检查未完成，网页服务继续运行；请查看本机 Codex 状态。'}
    record['local_codex'] = {k: readiness[k] for k in (
        'available', 'version', 'executable', 'discovery_source', 'error_code', 'error') if k in readiness}
    write_record(directory / 'runtime.json', record)
    request = directory / f'stop-{record["instance"]}.json'
    server = uvicorn.Server(uvicorn.Config('autoresearch.api:app', host=HOST, port=PORT))

    async def observe():
        while True:
            if server.started and record['status'] == 'starting':
                record['status'] = 'running'
                write_record(directory / 'runtime.json', record)
            if request.exists():
                try:
                    value = json.loads(request.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    value = {}
                if value.get('instance') == record['instance']:
                    record['status'] = 'stopping'
                    write_record(directory / 'runtime.json', record)
                    server.should_exit = True
                    return
            await asyncio.sleep(.25)

    monitor = asyncio.create_task(observe())
    try:
        await server.serve()
    finally:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        request.unlink(missing_ok=True)


def serve(root: Path, instance: str) -> int:
    import msvcrt
    directory = runtime_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    # File locking is atomic across sessions, unlike a Local\\ named mutex.
    with (directory / 'server.lock').open('a+b') as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return 2
        with (directory / 'server.log').open('a', encoding='utf-8', buffering=1) as log, \
                contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            record = {
                'root': str(root.resolve()), 'instance': instance, 'pid': os.getpid(),
                'create_time': psutil.Process().create_time(), 'launcher_pid': os.getppid(),
                'url': f'http://{HOST}:{PORT}', 'status': 'starting',
            }
            try:
                # The venv worker has its own redirector job; its launcher must be
                # outside all caller/provider jobs before importing the application.
                record['launcher_in_job'] = in_job(os.getppid())
                if record['launcher_in_job']:
                    raise RuntimeError('启动器仍绑定进程作业，拒绝以依附宿主的方式运行。')
                write_record(directory / 'runtime.json', record)
                print(json.dumps({'event': 'server.starting', **record}, ensure_ascii=False))
                asyncio.run(run_server(root, record))
                return 0
            except BaseException as exc:
                import traceback
                record['error_type'] = type(exc).__name__
                traceback.print_exc()
                return 1
            finally:
                record.update(status='stopped', stopped_at=time.time())
                write_record(directory / 'runtime.json', record)
                print(json.dumps({'event': 'server.stopped', **record}, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description='AutoResearch 独立后台服务')
    parser.add_argument('action', choices=['start', 'status', 'stop', 'serve'], nargs='?', default='start')
    parser.add_argument('--instance', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if os.name != 'nt':
        parser.error('此启动器用于 Windows；其他系统请使用 uvicorn。')
    if args.action == 'serve':
        if not args.instance or uuid.UUID(args.instance).hex != args.instance:
            parser.error('无效的服务实例。')
        return serve(ROOT, args.instance)
    try:
        result = {'start': start, 'status': status, 'stop': stop}[args.action](ROOT)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, psutil.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
