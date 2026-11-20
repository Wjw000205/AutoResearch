"""SHA-bound edit locations and executable rework instructions, not vague vetoes."""
import ast
import copy
import hashlib
import json
from pathlib import Path

from .experiment_workflow import sha
from .training_adjustment import obj

GUIDANCE = '''返修意见必须包含明确修改位置与改法：从 payload 的 location_catalog 选择准确 location_id，
填写 problem（当前问题）、change（具体如何改）、acceptance（如何验证）。位置由宿主绑定当前文件SHA、符号和行号，
不得猜测位置、引用旧版行号或只写“补证/修复/完善”。source_excerpt 只是定位材料，不等于完整源码审查。
每个修改点单独列项；同一个问题涉及生产者和消费者时分别给出两处修改。
所需修改不在已授权范围时明确指出范围冲突，不能指示作者越权或在相同权限下反复返修。
批准时required_changes=[]，否决时每项都必须有具体位置、问题、改法和验收条件。'''


READ_ONLY_CONTEXT = {'autoresearch/bounded_evidence_followup.py', 'autoresearch/controlled_training_checks.py',
                     'autoresearch/tuning_diagnostics.py'}


def locations(root, allowed, *, include_readonly=False):
    root = Path(root)
    result = []
    for name in sorted(set(allowed) | (READ_ONLY_CONTEXT if include_readonly else set())):
        path = root / name
        if not path.is_file() or path.suffix != '.py':
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError('Edit location escapes candidate workspace')
        raw = path.read_bytes(); text = raw.decode('utf-8'); lines = text.splitlines()
        digest = hashlib.sha256(raw).hexdigest()
        def add(symbol, start, end):
            anchor = '\n'.join(lines[start-1:min(end,start+2)])
            body = {'path': name, 'symbol': symbol, 'start_line': start, 'end_line': end,
                    'source_sha256': digest, 'source_excerpt': anchor, 'new_file': False,
                    'symbol_sha256': sha('\n'.join(lines[start-1:end])), 'editable': name in allowed}
            result.append({'location_id': sha(body), **body})
        add('<module>', 1, max(1, len(lines)))
        def walk(nodes, prefix=''):
            for node in nodes:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    symbol = prefix + node.name
                    add(symbol, node.lineno, node.end_lineno)
                    walk(node.body, symbol + '.')
        walk(ast.parse(text).body)
    for name in ('tests/test_bounded_host_completion.py', 'autoresearch/diagnostic_repair_support.py'):
        if not (root / name).exists():
            body = {'path': name, 'symbol': '<new file>', 'start_line': 0, 'end_line': 0,
                    'source_sha256': None, 'source_excerpt': '', 'new_file': True,
                    'symbol_sha256': None, 'editable': True}
            result.append({'location_id': sha(body), **body})
    return result


def vote_schema(catalog, text_schema, hash_schema):
    edit = obj({'location_id': {'enum': [r['location_id'] for r in catalog]},
                'problem': text_schema, 'change': text_schema, 'acceptance': text_schema})
    return obj({'packet_sha256': hash_schema, 'approve': {'type': 'boolean'}, 'rationale': text_schema,
                'required_changes': {'type': 'array', 'maxItems': 12, 'items': edit}})


def bind(changes, catalog, *, role):
    index = {r['location_id']: r for r in catalog}
    result = []
    for change in changes:
        if not isinstance(change, dict) or change.get('location_id') not in index:
            raise ValueError('Repair feedback needs a current, authorized edit location')
        for field in ('problem', 'change', 'acceptance'):
            if not isinstance(change.get(field), str) or not change[field].strip():
                raise ValueError('Repair feedback omitted the problem, change, or acceptance condition')
        body = {'reviewer': role, 'location': copy.deepcopy(index[change['location_id']]),
                **{k: change[k] for k in ('problem', 'change', 'acceptance')}}
        result.append({'task_id': sha(body), **body})
    return result


def verify_tasks(tasks, manifest, allowed):
    if not tasks:
        raise ValueError('Located repair work order has no edit tasks')
    for task in tasks:
        location = task['location']; name = location['path']
        if task['task_id'] != sha({k:v for k,v in task.items() if k != 'task_id'}):
            raise ValueError('Located edit task changed')
        if location['new_file']:
            if name in manifest or name not in ('tests/test_bounded_host_completion.py', 'autoresearch/diagnostic_repair_support.py'):
                raise ValueError('New edit location is not authorized')
        elif name not in allowed or manifest.get(name) != location['source_sha256']:
            raise ValueError('Required edit is outside scope or bound to stale source')


def render(tasks):
    lines = ['# 本轮精确修改单', '', '以下位置绑定交付前源码；修改后行号可以变化，原始SHA和任务ID保持可追溯。', '']
    for i, task in enumerate(tasks, 1):
        loc = task['location']
        lines += [f"## {i}. {loc['path']}::{loc['symbol']}", '',
                  f"- 原位置：{loc['start_line']}–{loc['end_line']} 行；SHA256：{loc['source_sha256']}",
                  f"- 任务ID：{task['task_id']}", f"- 当前问题：{task['problem']}",
                  f"- 具体修改：{task['change']}", f"- 验收条件：{task['acceptance']}", '']
    return '\n'.join(lines)


def delivery_responses(root, order, declared, allowed):
    root = Path(root)
    tasks = {t['task_id']: t for t in order.get('edit_tasks', [])}
    if not tasks:
        return None
    path = root / '_delivery/repair_responses.json'
    if not path.is_file():
        raise ValueError('Missing task-by-task located repair responses')
    rows = json.loads(path.read_bytes())
    if not isinstance(rows, list) or len(rows) != len(tasks) or {r.get('task_id') for r in rows} != set(tasks):
        raise ValueError('Repair responses omitted or duplicated an edit task')
    reported = {loc['path'] for r in rows for loc in r.get('locations', [])}
    if not reported <= set(allowed) | set(declared):
        raise ValueError('Repair response points outside authorized source')
    index = {(r['path'], r['symbol']): r for r in locations(root, reported) if not r['new_file']}
    result = []
    for row in rows:
        if row.get('status') not in ('changed', 'retained', 'blocked') or not str(row.get('explanation', '')).strip():
            raise ValueError('Repair response needs a truthful disposition and explanation')
        resolved = []
        for loc in row.get('locations', []):
            item = index.get((loc.get('path'), loc.get('symbol')))
            if item is None:
                raise ValueError('Reported modification symbol does not exist')
            if row['status'] == 'changed' and item['path'] not in declared:
                raise ValueError('Changed repair location is absent from the change manifest')
            resolved.append(item)
        if not resolved and row['status'] != 'blocked':
            raise ValueError('Repair delivery must name its actual modification locations')
        result.append({**copy.deepcopy(row), 'verified_locations': resolved})
    return result
