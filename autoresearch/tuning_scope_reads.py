"""Authenticate same-role scope reads across revisions without inheriting votes."""
import copy

from .experiment_workflow import sha


class ReadEvidenceError(ValueError):
    def __init__(self, issues):
        self.issues = issues
        super().__init__('Tuning scope reading evidence needs repair: ' + '; '.join(
            f'{x["kind"]}: {x.get("path", x.get("receipt_sha256", ""))}'
            + (f' missing ranges {x["missing_ranges"]}' if 'missing_ranges' in x else '')
            + (f' ({x["detail"]})' if x.get('detail') else '') for x in issues))


def active_vote(request):
    current = request.get('operator_review') or request.get('contract_clarification') or request
    return current.get('read_correction') or current


def assignments(record, role):
    """Only requests explicitly owned by this scope, never an arbitrary task."""
    result = []
    for round_ in record.get('rounds', []):
        if role == 'engineer':
            result += [round_.get('author'), round_.get('correction')]
        vote = round_.get('votes', {}).get(role) or {}
        result += [vote, vote.get('read_correction')]
        clarified = vote.get('contract_clarification') or {}
        result += [clarified, clarified.get('read_correction')]
        result += [vote.get('operator_review')]
    return [r for r in result if r and r.get('answer')]


def _snapshot(engine, run_id, context):
    ref = context.get('validation_evidence') or {}
    value = engine.store.get('experiment_evidence', ref.get('id', '')) or {}
    if (value.get('run_id') != run_id or sha(value.get('summary')) != ref.get('sha256')
            or context.get('evidence_sha256') != ref.get('sha256')):
        raise ValueError('Tuning read evidence snapshot changed')
    return value['summary']


def _replay(engine, run_id, context, row):
    call, result = row.get('call', {}), row.get('result') or {}
    body = {k: v for k, v in result.items() if k != 'receipt_sha256'}
    def fail(kind, detail):
        raise ReadEvidenceError([{'kind': kind, 'receipt_sha256': result.get('receipt_sha256'),
            'tool': call.get('name'), 'path': call.get('arguments', {}).get('path'), 'detail': detail}])
    if result.get('error') or result.get('receipt_sha256') != sha({'call': call, 'result': body}):
        fail('invalid_receipt', 'Returned error or receipt hash mismatch')
    from .experiment_tools import compare
    from .experiment_analysis import inspect_module
    from .hyperparameter_yaml import replay_read
    try:
        if call.get('name') == 'experiments.read':
            from .experiment_tools import read
            evidence = _snapshot(engine, run_id, context)
            expected = read(engine, run_id, evidence['binding']['cycle'], call['arguments'])
        elif call.get('name') == 'experiments.analyze':
            from .experiment_analysis import analyze
            expected = analyze(engine, run_id, context, call['arguments'], persist=False)
            archived = engine.store.get('experiment_analyses', body.get('analysis_id', '')) or {}
            if (archived.get('run_id') != run_id or sha(archived.get('result')) != body.get('content_sha256')
                    or archived.get('source_evidence_id') != call['arguments'].get('evidence_id')
                    or archived.get('source_evidence_sha256') != call['arguments'].get('expected_sha256')):
                fail('analysis_archive_mismatch', 'Saved analysis no longer matches its source evidence')
        else:
            function = {'experiments.compare': compare, 'experiments.inspect_module': inspect_module,
                        'hyperparameters.read': replay_read}.get(call.get('name'))
            if function is None:
                fail('unsupported_receipt_tool', 'Use read/compare/analyze/inspect_module/hyperparameters.read receipts; notes are not source coverage')
            expected = function(engine, run_id, context, call['arguments'])
        if expected != body:
            fail('replayed_content_mismatch', 'Saved page differs from its exact source replay')
    except ReadEvidenceError:
        raise
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        fail('source_replay_failed', str(exc))
    return body


def _current_content(engine, run_id, context, row, body):
    """An old page is current only when its exact selected content is unchanged."""
    call = row['call']; name = call['name']
    if name == 'experiments.read':
        from .experiment_tools import read
        evidence = _snapshot(engine, run_id, context)
        try:
            return read(engine, run_id, evidence['binding']['cycle'], call['arguments']) == body
        except (ValueError, KeyError, TypeError):
            return False
    if name == 'experiments.analyze':
        from .experiment_analysis import analysis_result
        previous = (engine.store.get('experiment_analyses', body['analysis_id']) or {}).get('result') or {}
        evidence = _snapshot(engine, run_id, context)
        current = analysis_result(evidence, {**call['arguments'], 'expected_sha256': context['evidence_sha256']})
        without_ref = lambda value: {k: v for k,v in value.items() if k != 'evidence_sha256'}
        return without_ref(previous) == without_ref(current)
    if name == 'hyperparameters.read':
        from .hyperparameter_yaml import replay_read
        return replay_read(engine, run_id, context, call['arguments']) == body
    from .experiment_tools import compare
    from .experiment_analysis import inspect_module
    ref = context['validation_evidence']
    args = {**call['arguments'], 'evidence_id': ref['id'], 'expected_sha256': ref['sha256']}
    if args.get('snapshot', 'current') not in {'current', 'candidate'}:
        return False
    try:
        current = (compare if name == 'experiments.compare' else inspect_module)(engine, run_id, context, args)
    except (ValueError, KeyError, TypeError):
        return False
    identity = 'content_sha256' if name == 'experiments.compare' else 'source_sha256'
    return (current.get(identity) == body.get(identity) and current.get(identity) is not None
        and all(current.get(k) == body.get(k) for k in ('text', 'offset', 'end', 'total_chars', 'view', 'source_read')))


def check(engine, run_id, role, request, plan, *, voting=False, scope_record=None):
    from .parameter_resolution import owner
    from .engine import Harness
    hid = owner(engine, run_id, role, request)
    context = request['context']; evidence = _snapshot(engine, run_id, context)
    refs = set(request['answer']['read_receipts'])
    rows = (engine.store.get('handoff_progress', hid) or {}).get('tool_results', [])
    located = {r['result']['receipt_sha256']: (hid, context, r) for r in rows
        if isinstance(r.get('result'), dict) and r['result'].get('receipt_sha256') in refs}
    if scope_record is not None:
        candidates = assignments(scope_record, role)
        if (scope_record.get('run_id') != run_id or scope_record.get('binding') != evidence.get('binding')
                or not any(r == request for r in candidates)):
            raise ValueError('Tuning read reuse is outside this scope and role assignment')
        # Stop at the current assignment: a future review cannot retroactively
        # supply reading credit to an earlier paid decision.
        for previous in candidates:
            if previous == request:
                break
            if refs <= located.keys():
                break
            previous_rows = (engine.store.get('handoff_progress',
                Harness.decision_key(
                    run_id, role, previous['purpose'], previous['instruction'], previous['context'], previous['schema'])) or {}).get('tool_results', [])
            relevant = [r for r in previous_rows if (r.get('result') or {}).get('receipt_sha256') in refs-located.keys()]
            if not relevant:
                continue
            prior_id = owner(engine, run_id, role, previous)
            old = _snapshot(engine, run_id, previous['context'])
            if old.get('binding') != evidence.get('binding') or old.get('code_sources') != evidence.get('code_sources'):
                continue
            for row in relevant:
                body = _replay(engine, run_id, previous['context'], row)
                if _current_content(engine, run_id, context, row, body):
                    located[row['result']['receipt_sha256']] = (prior_id, previous['context'], row)
    from .tuning_scope_operator_review import retained_rows
    located.update({ref: value for ref, value in retained_rows(engine, run_id, role, request).items() if ref in refs})
    from .tuning_scope_context import cited_check_summary
    supplemental = cited_check_summary(engine, run_id, context, refs)
    proof = verify_pages(engine, run_id, hid, context, plan, located,
                         refs - {r['receipt_sha256'] for r in supplemental}, voting=voting)
    proof['receipts'].extend({**row, 'handoff_id': hid} for row in supplemental)
    return {**proof, 'role': role}


def verify_pages(engine, run_id, hid, context, plan, located, refs, *, voting):
    """Pure coverage check, also used before authorizing a response-only review."""
    wanted = {'/tuning_inventory'} | ({'/tuning_scope_plan'} if voting else set())
    runtime = context.get('experiment_decision', {}).get('tuning_scope_design', {})
    if runtime.get('runtime_audit_path'):
        wanted.add(runtime['runtime_audit_path'])
    files = {p for factor in plan['factors'] for p in factor['source_paths']}
    files.update(runtime.get('runtime_route_paths', []))
    windows = {path: [] for path in wanted | files}
    issues = [{'kind': 'receipt_not_found_in_current_or_same_role_history', 'receipt_sha256': ref}
              for ref in sorted(refs-located.keys())]
    proof = []
    for ref in sorted(refs & located.keys()):
        source_id, source_context, row = located[ref]
        body = _replay(engine, run_id, source_context, row)
        name, args = row['call']['name'], row['call']['arguments']
        path = args.get('path')
        coverage_credit = ((name == 'experiments.compare' and path in wanted)
                or (name == 'experiments.inspect_module' and body.get('source_read') and path in files
                    and args.get('snapshot', 'current') == 'current'))
        if coverage_credit:
            windows[path].append(body)
        proof.append({'receipt_sha256': ref, 'handoff_id': source_id, 'path': path,
            'tool': name, 'coverage_credit': bool(coverage_credit),
            'inherited_read': source_id != hid})
    coverage = {}
    for path, pages in sorted(windows.items()):
        from .experiment_tools import compare
        from .experiment_analysis import inspect_module
        ref = context['validation_evidence']
        read_args = {'evidence_id': ref['id'], 'expected_sha256': ref['sha256'], 'path': path,
                     'offset': 0, 'limit': 1}
        if path in files:
            read_args.update(view='source', snapshot='current')
        metadata = (inspect_module if path in files else compare)(engine, run_id, context, read_args)
        cursor, gaps = 0, []
        for page in sorted(pages, key=lambda p: p['offset']):
            if page['offset'] > cursor:
                gaps.append([cursor, page['offset']])
            cursor = max(cursor, page['end'])
        total = metadata['total_chars']
        if cursor < total:
            gaps.append([cursor, total])
        if not pages or gaps:
            issues.append({'kind': 'missing_source_pages' if path in files else 'missing_evidence_pages',
                'path': path, 'missing_ranges': gaps, 'expected_total_chars': total,
                'read': {'name': 'experiments.inspect_module' if path in files else 'experiments.compare',
                    'arguments': {**read_args, 'offset': gaps[0][0] if gaps else 0,
                                  'limit': min(6000, (gaps[0][1]-gaps[0][0]) if gaps else max(1,total))}}})
        coverage[path] = {'covered_end': cursor, 'total_chars': total, 'complete': bool(pages) and not gaps}
    if issues:
        raise ReadEvidenceError(issues)
    return {'version': 1, 'handoff_id': hid, 'evidence': copy.deepcopy(context['validation_evidence']),
        'receipts': proof, 'coverage': coverage, 'approval_inherited': False}


def retained_index(engine, run_id, role, record, context, plan):
    """New requests get a compact directory of actually reusable same-role pages."""
    from .parameter_resolution import owner
    evidence = _snapshot(engine, run_id, context)
    wanted = {'/tuning_inventory', '/tuning_scope_plan'} | {p for f in plan['factors'] for p in f['source_paths']}
    runtime = context.get('experiment_decision', {}).get('tuning_scope_design', {})
    wanted.update(runtime.get('runtime_route_paths', []))
    if runtime.get('runtime_audit_path'):
        wanted.add(runtime['runtime_audit_path'])
    pages = {}
    for request in reversed(assignments(record, role)):
        old = _snapshot(engine, run_id, request['context'])
        if old.get('binding') != evidence.get('binding') or old.get('code_sources') != evidence.get('code_sources'):
            continue
        hid = owner(engine, run_id, role, request)
        for row in (engine.store.get('handoff_progress', hid) or {}).get('tool_results', []):
            call, result = row.get('call', {}), row.get('result') or {}
            args = call.get('arguments', {}); path = args.get('path')
            if (path not in wanted or result.get('error') or not result.get('receipt_sha256')
                    or call.get('name') not in {'experiments.compare', 'experiments.inspect_module'}
                    or call['name']=='experiments.inspect_module' and not result.get('source_read')):
                continue
            identity = (path, result.get('offset'), result.get('end'), result.get('content_sha256', result.get('source_sha256')))
            if identity in pages:
                continue
            body = _replay(engine, run_id, request['context'], row)
            if _current_content(engine, run_id, context, row, body):
                pages[identity] = {'path': path, 'offset': body['offset'], 'end': body['end'],
                    'total_chars': body['total_chars'], 'receipt_sha256': result['receipt_sha256'], 'handoff_id': hid}
    return {'version': 1, 'role': role, 'pages': list(pages.values()), 'approval_inherited': False}
