"""Discover installed Windows editors and open only this task's code workspace."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .codex_workspace import checked
from .implementation_progress import _field, _latest

EDITORS = {'vscode': ('Visual Studio Code', 'Code.exe'),
           'vscode-insiders': ('VS Code Insiders', 'Code - Insiders.exe'),
           'pycharm': ('PyCharm', 'pycharm64.exe')}


def _registry_candidates():
    if os.name != 'nt':
        return []
    import winreg
    found = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            for _, exe in EDITORS.values():
                try:
                    with winreg.OpenKey(hive, rf'Software\Microsoft\Windows\CurrentVersion\App Paths\{exe}', 0, winreg.KEY_READ | view) as key:
                        found.append(Path(winreg.QueryValueEx(key, '')[0].strip('"')))
                except OSError:
                    pass
            try:
                with winreg.OpenKey(hive, r'Software\Microsoft\Windows\CurrentVersion\Uninstall', 0, winreg.KEY_READ | view) as root:
                    for index in range(winreg.QueryInfoKey(root)[0]):
                        try:
                            with winreg.OpenKey(root, winreg.EnumKey(root, index)) as key:
                                name = winreg.QueryValueEx(key, 'DisplayName')[0]
                                if not any(word in name.lower() for word in ('visual studio code', 'pycharm')):
                                    continue
                                try:
                                    folder = Path(winreg.QueryValueEx(key, 'InstallLocation')[0])
                                    found.extend([folder / 'Code.exe', folder / 'Code - Insiders.exe', folder / 'bin' / 'pycharm64.exe'])
                                except OSError:
                                    pass
                                try:
                                    found.append(Path(winreg.QueryValueEx(key, 'DisplayIcon')[0].rsplit(',', 1)[0].strip('"')))
                                except OSError:
                                    pass
                        except OSError:
                            continue
            except OSError:
                pass
    return found


def discover_editors():
    if os.name != 'nt':
        return []
    candidates = []
    for command in ('code', 'code-insiders', 'pycharm64', 'pycharm'):
        located = shutil.which(command)
        if located:
            path = Path(located)
            candidates.extend([path, path.parent.parent / 'Code.exe', path.parent.parent / 'Code - Insiders.exe', path.parent / 'pycharm64.exe'])
    candidates.extend(_registry_candidates())
    local = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData' / 'Local')))
    for base in (local / 'Programs', Path(os.environ.get('ProgramFiles', r'C:\Program Files'))):
        candidates.extend([base / 'Microsoft VS Code' / 'Code.exe', base / 'Microsoft VS Code Insiders' / 'Code - Insiders.exe'])
        for parent in (base / 'JetBrains', local / 'JetBrains' / 'Toolbox' / 'apps'):
            if parent.is_dir():
                candidates.extend(sorted(parent.glob('PyCharm*/bin/pycharm64.exe'), reverse=True))
    found = {}
    for path in candidates:
        for key, (name, executable) in EDITORS.items():
            if key not in found and path.name.lower() == executable.lower() and path.is_file():
                found[key] = {'id': key, 'name': name, 'executable': str(path.resolve())}
    return list(found.values())


def current_workspace(store, run):
    state = run.get('state', {})
    row = _latest(store, 'codex_workflows', run['id'], state.get('research_cycle', 1),
        {'id': 'key', 'workspace': _field('workspace'), 'project_root': _field('project_root')},
        generation=state.get('implementation_generation'))
    if not row or not row.get('workspace') or not row.get('project_root'):
        raise ValueError('当前任务尚未创建代码目录。')
    workspace = checked(row['project_root'], run['id'], row['workspace'])
    if not workspace.is_dir():
        raise ValueError('当前任务的代码目录不存在，请检查本机路径。')
    # Existing readable links are display aliases only. The owned canonical
    # workspace is rechecked before every launch; never trust a client path.
    alias = Path(row['project_root']) / '代码实现' / f"第{state.get('research_cycle', 1)}轮"
    display = alias if alias.is_dir() and alias.resolve() == workspace else workspace
    return {'workflow_id': row['id'], 'workspace': str(workspace), 'display_path': str(display),
            'entry_file': 'experiment.py' if (workspace / 'experiment.py').is_file() else None}


def launch_editor(editor, workspace, relative_path=None):
    root = Path(workspace).resolve(strict=True)
    target = root
    if relative_path:
        relative = Path(relative_path)
        if relative.is_absolute() or relative.drive or '..' in relative.parts or ':' in relative_path or '\x00' in relative_path:
            raise ValueError('只能打开当前代码目录内的文件。')
        target = (root / relative).resolve(strict=True)
        if not target.is_relative_to(root) or not target.is_file():
            raise ValueError('所选文件不属于当前代码目录。')
    # Explicit executable + argv, never shell interpolation or a client-supplied
    # executable/command. Opening does not run the research or change its gates.
    subprocess.Popen([editor['executable'], str(target)], cwd=str(root),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, shell=False, close_fds=True)
    return str(target)


class OpenCode(BaseModel):
    model_config = ConfigDict(extra='forbid')
    workflow_id: str = Field(min_length=1, max_length=512)
    editor_id: str = Field(pattern='^(vscode|vscode-insiders|pycharm)$')
    relative_path: str | None = Field(default=None, max_length=2048)


def register(app, store, require_run):
    @app.get('/api/runs/{run_id}/code-workspace')
    def workspace_view(run_id: str):
        run = require_run(run_id)
        try:
            value = current_workspace(store, run)
        except (ValueError, OSError) as exc:
            return {'available': False, 'reason': str(exc), 'editors': []}
        return {'available': True, **value, 'editors': discover_editors()}

    @app.post('/api/runs/{run_id}/code-workspace/open')
    def open_code(run_id: str, payload: OpenCode):
        run = require_run(run_id)
        try:
            value = current_workspace(store, run)
            if value['workflow_id'] != payload.workflow_id:
                raise ValueError('代码任务已更新，请刷新目录后重新打开。')
            editor = next((row for row in discover_editors() if row['id'] == payload.editor_id), None)
            if editor is None:
                raise ValueError('未检测到所选编辑器，请重新检测本机软件。')
            target = launch_editor(editor, value['workspace'], payload.relative_path)
            return {'status': 'launched', 'editor': editor['name'], 'target': target}
        except (OSError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
