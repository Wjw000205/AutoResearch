"""Readable code workspaces, with task identity in metadata instead of names."""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

OUTPUTS = '代码输出'
CODE = '代码'
OWNER = 'workspace-owner.json'
KINDS = {'implementation': '代码实现', 'runtime_repair': '运行修复'}


def _plain(path):
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part, 'is_junction', lambda: False)():
            raise ValueError('Codex工作目录及父目录不能使用链接或junction')


def task_name(title):
    value = unicodedata.normalize('NFKC', str(title or '研究任务'))
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '-', value)
    value = re.sub(r'\s+', ' ', value).strip(' .')[:32].rstrip(' .') or '研究任务'
    if re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', value, re.I):
        value = '任务-' + value
    return value


def checked(project_root, run_id, workspace, *, create=False):
    project, workspace = Path(project_root), Path(workspace)
    if not project.is_absolute() or not workspace.is_absolute() or not project.is_dir():
        raise ValueError('Codex项目必须是已存在的绝对目录')
    _plain(project)
    _plain(workspace)
    project, workspace = project.resolve(), workspace.resolve()
    legacy = project / '.autoresearch' / run_id
    if workspace.is_relative_to(legacy):
        parts = workspace.relative_to(legacy).parts
        if len(parts) != 2 or parts[-1] != 'workspace' or not re.fullmatch(r'[0-9a-f]{64}', parts[0]):
            raise ValueError('Codex工作目录缺少任务工作流隔离标识')
    else:
        base = project / OUTPUTS
        if not workspace.is_relative_to(base):
            raise ValueError('Codex工作目录不属于当前项目和任务')
        parts = workspace.relative_to(base).parts
        if (len(parts) != 3 or parts[-1] != CODE or task_name(parts[0]) != parts[0]
                or not re.fullmatch(r'第[1-9]\d*轮-(代码实现|运行修复)-版本[1-9]\d*', parts[1])):
            raise ValueError('Codex工作目录缺少任务工作流隔离标识')
        marker = workspace.parent / OWNER
        _plain(marker)
        try:
            owner = json.loads(marker.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise ValueError('Codex可读工作目录缺少有效归属记录') from exc
        if (not isinstance(owner, dict) or type(owner.get('version')) is not int or owner['version'] != 1
                or owner.get('run_id') != run_id or not isinstance(owner.get('workflow_id'), str)
                or not owner['workflow_id'] or type(owner.get('cycle')) is not int or owner['cycle'] < 1
                or owner.get('kind') not in KINDS
                or not parts[1].startswith(f"第{owner['cycle']}轮-{KINDS[owner['kind']]}-版本")):
            raise ValueError('Codex可读工作目录归属与当前研究不符')
    if create:
        workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def allocate(project_root, run_id, workflow_id, title, cycle, kind):
    project = Path(project_root)
    if not project.is_absolute() or not project.is_dir():
        raise ValueError('Codex项目必须是已存在的绝对目录')
    _plain(project)
    if type(cycle) is not int or cycle < 1 or kind not in KINDS or not workflow_id:
        raise ValueError('Codex工作目录需要有效任务、轮次和阶段')
    parent = project.resolve() / OUTPUTS / task_name(title)
    _plain(parent)
    parent.mkdir(parents=True, exist_ok=True)
    owner = {'version': 1, 'run_id': run_id, 'workflow_id': workflow_id, 'cycle': cycle, 'kind': kind}
    for version in range(1, 10001):
        directory = parent / f'第{cycle}轮-{KINDS[kind]}-版本{version}'
        try:
            directory.mkdir()
        except FileExistsError:
            _plain(directory)
            marker = directory / OWNER
            _plain(marker)
            try:
                existing = json.loads(marker.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue  # Never claim a pre-existing or incompletely created directory.
            if existing != owner:
                continue
        else:
            with (directory / OWNER).open('x', encoding='utf-8') as stream:
                json.dump(owner, stream, ensure_ascii=False, indent=2)
        return checked(project, run_id, directory / CODE, create=True)
    raise ValueError('Codex可读工作目录版本数量超限')
