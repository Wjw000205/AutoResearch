"""Small public activity descriptions, never shell bodies or tool output."""
import re
from pathlib import Path


def describe(item, workspace, clean):
    kind = item.get('type')
    result = {}
    if kind == 'commandExecution':
        # Classify locally, but do not persist command strings, inline scripts,
        # environment values, stdout/stderr, or arbitrary command arguments.
        command = str(item.get('command', ''))
        categories = [
            (r'\bpytest\b|\bunittest\b|\btest_[\w.-]+\.py\b', '运行开发测试'),
            (r'\bpy_compile\b|\bcompileall\b', '检查 Python 语法'),
            (r'\b(?:git)\s+(?:diff|status)\b', '核对代码改动'),
            (r'\b(?:Get-Content|Select-String|rg|cat)\b', '读取或搜索项目文件'),
            (r'\b(?:apply_patch|Set-Content)\b', '修改项目文件'),
            (r'\b(?:python|python3|python\.exe)\b', '运行 Python 检查或复验'),
        ]
        result['summary'] = next((label for pattern, label in categories
                                  if re.search(pattern, command, re.I)), '执行本地命令')
        duration = item.get('durationMs')
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration >= 0:
            result['duration_seconds'] = round(duration / 1000, 2)
        actions = item.get('commandActions')
        candidates = [action.get('path') for action in actions if isinstance(action, dict)] if isinstance(actions, list) else []
    elif kind == 'fileChange':
        result['summary'] = '修改代码文件'
        changes = item.get('changes')
        candidates = [change.get('path') for change in changes if isinstance(change, dict)] if isinstance(changes, list) else []
    else:
        return {'summary': '整理执行上下文'}
    paths = []
    root = Path(workspace).absolute()
    for value in candidates:
        if not isinstance(value, str) or len(value) > 2000:
            continue
        path = Path(value)
        path = path if path.is_absolute() else root / path
        if '..' in path.parts or not path.is_relative_to(root):
            continue
        relative = path.relative_to(root).as_posix()
        if relative and relative not in paths:
            paths.append(clean(relative)[:240])
    if paths:
        result['paths'] = paths[:8]
    return result
