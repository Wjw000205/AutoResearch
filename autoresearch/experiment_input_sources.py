"""Show the original input pages and source locations behind a disputed configuration."""
import ast
import re

from .experiment_workflow import sha


def cited_pages(engine, run_id, record):
    from .diagnostic_planning_recovery import authenticate
    from .experiment_evidence_handoff import KEY
    from .experiment_review_reads import submitted_pages
    from .experiment_tools import compare
    proposal = record.get('proposal')
    saved = record.get('recovery_answer') or {}
    if not proposal or saved.get('proposal') != proposal:
        return []
    cited = set(re.findall(r'(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])', str(proposal)))
    review = record.get(KEY, {}).get('independent_review', {})
    for assignment in review.values():
        keys = ('purpose', 'instruction', 'context', 'schema')
        if not isinstance(assignment, dict) or not all(k in assignment for k in keys):
            continue
        if sha({k: assignment[k] for k in keys}) != saved['assignment_sha256']:
            continue
        ctx = assignment['context']
        if ctx['validation_evidence'] != record['evidence']:
            raise ValueError('Cited author pages use a different validation snapshot')
        owner = authenticate(engine, run_id, 'engineer', assignment['purpose'], {k: assignment[k] for k in keys[1:]}, proposal)
        delivered = submitted_pages(engine.store, run_id, 'engineer', owner, cited)
        pages = []
        for row in engine.store.get('handoff_progress', owner, {}).get('tool_results', []):
            call, page = row.get('call', {}), row.get('result')
            if call.get('name') != 'experiments.compare' or not isinstance(page, dict) or page.get('receipt_sha256') not in delivered:
                continue
            if compare(engine, run_id, ctx, call['arguments']) != {k:v for k,v in page.items() if k != 'receipt_sha256'}:
                raise ValueError('Cited author comparison differs from its frozen source')
            pages.append({'role': 'engineer', 'handoff_id': owner, 'proposal_sha256': record['proposal_sha256'],
                'submitted_request_id': delivered[page['receipt_sha256']]['request_id'], 'call': call, 'result': page})
        return pages
    return []


def field_sources(engine, run_id, evidence, ref, selected, issues):
    from .source_read_identity import source_files
    from .hyperparameter_yaml import leaves
    from .fair_comparison import strict_equal
    descriptions = ' '.join(issues)
    names = set(re.findall(r'[A-Za-z_]\w*', descriptions, re.ASCII))
    rows = {row['id']: row for row in evidence['records']}
    keys, fields = set(), []
    for trial in selected['trials']:
        row = rows[trial['trial_id']]
        receipt = trial['yaml_receipt']
        from .hyperparameter_yaml import read_version
        profile = read_version(engine, run_id, receipt['revision_id'], receipt['yaml_sha256'])['body']['document']['profiles'][receipt['profile']]
        yaml_values = dict(leaves(profile))
        cited = []
        for path, value in leaves(row['params']):
            parts = path.strip('/').split('/')
            if parts[-1] not in names:
                continue
            keys.update((parts[0], parts[-1]))
            cited.append({'admitted_path': trial['record_path'] + '/params' + path, 'admitted_value': value,
                'yaml_path': '/profiles/' + receipt['profile'] + path if path in yaml_values else None,
                'yaml_matches': strict_equal(yaml_values[path], value) if path in yaml_values else None})
        if cited:
            fields.append({'trial_id': trial['trial_id'], 'attempt': trial['attempt'], 'fields': cited})
    if not fields:
        return None
    files = source_files(engine.store, run_id, {'evidence_sha256': ref['sha256'], 'validation_evidence': ref})
    matches, definitions, callers = {}, {}, {}
    functions = set()
    for path, file in files.items():
        if not path.endswith('.py') or not isinstance(file.get('text'), str):
            continue
        try:
            tree = ast.parse(file['text'])
        except (SyntaxError, ValueError):
            continue  # Non-parsed modules remain in the original source archive.
        lines = {n.lineno for n in ast.walk(tree) if isinstance(n, ast.Constant)
                 and isinstance(n.value, str) and n.value in keys}
        matches[path] = lines
        definitions[path] = sorted((n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))),
                                   key=lambda n: n.end_lineno - n.lineno)
        for fn in definitions[path]:
            if any(fn.lineno <= i <= fn.end_lineno for i in lines):
                functions.add(fn.name)
                lines.add(fn.lineno)
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                name = getattr(n.func, 'id', getattr(n.func, 'attr', None))
                if name:
                    callers.setdefault(name, []).append((path, n.lineno))
    todo = list(functions)
    while todo:
        for path, line in callers.get(todo.pop(), []):
            matches[path].add(line)
            fn = next((n for n in definitions[path] if n.lineno <= line <= n.end_lineno), None)
            if fn:
                matches[path].add(fn.lineno)
                if fn.name not in functions:
                    functions.add(fn.name)
                    todo.append(fn.name)
    locations = []
    remaining = 6000
    for path, matched in matches.items():
        source = files[path]['text'].splitlines(keepends=True)
        spans = []
        for line in sorted(matched):
            start, stop = max(0, line-2), min(len(source), line+2)
            if spans and start <= spans[-1][1]:
                spans[-1][1] = max(spans[-1][1], stop)
            else:
                spans.append([start, stop])
        if spans:
            excerpts = []
            offset = [0]
            for line in source:
                offset.append(offset[-1] + len(line))
            for a, b in spans:
                body = ''.join(source[a:b])
                item = {'first': a+1, 'last': b, 'offset': offset[a], 'end': offset[b]}
                if len(body) <= remaining:
                    item['text'] = body
                    remaining -= len(body)
                else:
                    item['read_with'] = {'name': 'experiments.inspect_module', 'arguments': {
                        'evidence_id': ref['id'], 'expected_sha256': ref['sha256'], 'path': path, 'snapshot': 'current',
                        'view': 'source', 'offset': offset[a], 'limit': min(6000, len(body))}}
                excerpts.append(item)
            locations.append({'path': path, 'source_sha256': files[path]['sha256'],
                'lines': excerpts})
    return {'trials': fields, 'evidence': ref, 'source_locations': locations,
            'scope': '路径和值来自冻结输入。源码按字段字面量和同名调用定位；工程师核对实际加载、覆盖顺序和下游用途，区分静态行为与历史实载证明。'}
