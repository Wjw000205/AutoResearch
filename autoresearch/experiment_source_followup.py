"""Resolve an engineer's named implementation questions against the assessed source."""
import ast
import copy
import re

from . import experiment_evidence_handoff as handoff
from .experiment_workflow import sha

KEY = 'source_call_review'


def excerpts(engine, run_id, context, proposal):
    from .source_read_identity import source_files, resolve
    from .architecture_experiment_contract import module_paths
    names = set(re.findall(r'(?<![A-Za-z0-9_])[A-Za-z_]\w*', proposal['verification'], re.ASCII))
    files = source_files(engine.store, run_id, context)
    parsed, symbols = {}, set()
    for reference in sorted(module_paths(proposal)):
        identity = resolve(files, reference)
        path = identity['path']
        if path in parsed or not path.endswith('.py'):
            continue
        source = files[path]['text']
        tree = ast.parse(source)
        parsed[path] = (tree, source)
        symbols.update(n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and n.name in names)
    pages = []
    ref = context['validation_evidence']
    for path, (tree, source) in parsed.items():
        offsets = [0]
        for line in source.splitlines(keepends=True):
            offsets.append(offsets[-1] + len(line))
        ranges = set()
        call_names = symbols | {entry.asname for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                   for entry in node.names if entry.asname and entry.name in symbols}
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in symbols:
                ranges.add((n.lineno, n.end_lineno))
            elif isinstance(n, ast.Call):
                if ((isinstance(n.func, ast.Name) and n.func.id in call_names)
                        or (isinstance(n.func, ast.Attribute) and n.func.attr in symbols)):
                    ranges.add((n.lineno, n.end_lineno))
        last = 0
        for first, end in sorted(ranges, key=lambda p: (p[0], -p[1])):
            if end <= last:
                continue
            start = offsets[first-1]
            stop = min(start + 6000, offsets[end])
            args = {'evidence_id': ref['id'], 'expected_sha256': ref['sha256'], 'path': path, 'view': 'source',
                    'snapshot': 'current', 'offset': stop if stop < offsets[end] else start,
                    'limit': min(6000, offsets[end] - stop if stop < offsets[end] else stop - start)}
            pages.append({'path': path, 'source_sha256': files[path]['sha256'], 'first_line': first, 'last_line': end,
                'offset': start, 'end': stop, 'range_end': offsets[end], 'text': source[start:stop],
                'read_with': {'name': 'experiments.inspect_module', 'arguments': args}})
            last = end
    return {'evidence': ref, 'symbols': sorted(symbols), 'source_pages': pages} if pages else None


async def prepare(engine, run_id, record, proposal, assignment, checkpoint):
    from .diagnostic_planning_recovery import authenticate
    current = record[handoff.KEY]
    review = current['independent_review']
    if (review.get(KEY) or not assignment or not proposal.get('training_response', {}).get('module_paths')
            or not handoff.requested(proposal)):
        return None
    owner = await engine._read_consensus_check(run_id, authenticate, engine, run_id, 'engineer',
        assignment['purpose'], {k: assignment[k] for k in ('instruction', 'context', 'schema')}, proposal)
    material = await engine._read_consensus_check(run_id, excerpts, engine, run_id, assignment['context'], proposal)
    if not material:
        return None
    ctx = copy.deepcopy(assignment['context'])
    ctx['experiment_decision'][KEY] = {'source_handoff': owner, 'previous_assessment': proposal,
        'source': material, 'execution_authorized': False}
    new = {**assignment, 'context': ctx, 'purpose': assignment['purpose'] + ':source-calls:' + sha(material),
        'instruction': assignment['instruction'] + '\n你是负责这项静态核对的工程师。source_call_review 提供本次请求点名的函数定义'
            '及同名调用位置原文。它们按拼写定位，需由你核对导入和调用关系；截取未到 range_end 时用 read_with 续读。'
            '本轮直接在 verification 和修改单中写出核实的签名、每个实参、返回值及进入评分的路径。'
            '区分代码可见事实、拟议夹具和历史执行未知。现有交接回执可引用；宿主在冻结你的提案后将评审材料和备注绑定到该版独立投票。'
            '按静态核对的实际结论提交具体下一步或说明明确冲突，不将本人可以用现有源码回答的事项退回待办。'}
    await checkpoint(**{handoff.KEY: {**current, 'status': 'synthesizing',
        'independent_review': {**review, KEY: new}}}, status='proposing')
    return new
