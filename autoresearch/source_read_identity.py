"""Resolve source references only inside an authenticated frozen source archive.

Static symbol identity is not an execution claim. Aliases earn reading credit
only when exact, same-assignment source pages cover the whole resolved file.
"""
from __future__ import annotations

import ast
import hashlib

from .experiment_workflow import sha


def source_files(store, run_id, context):
    ref = context['validation_evidence']
    evidence = store.get('experiment_evidence', ref['id']) or {}
    if (evidence.get('run_id') != run_id or context['evidence_sha256'] != ref['sha256']
            or sha(evidence.get('summary')) != ref['sha256']):
        raise ValueError('Source reference evidence changed')
    source_ref = evidence['summary'].get('code_sources') or {}
    archive = store.get('experiment_sources', source_ref.get('id', '')) or {}
    if archive.get('run_id') != run_id or sha(archive.get('files')) != source_ref.get('sha256'):
        raise ValueError('Source reference archive changed')
    return archive['files']


def _definitions(node, prefix=()):
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            qualified = (*prefix, child.name)
            yield '.'.join(qualified), child
            yield from _definitions(child, qualified)
        else:
            yield from _definitions(child, prefix)


def resolve(files, reference):
    """No basename/suffix guessing or imports; ambiguous symbols fail closed."""
    if not isinstance(reference, str) or not reference:
        raise ValueError('Required source reference must be nonempty text')
    matches = []
    for path, item in files.items():
        module = path[:-3].replace('/', '.') if path.endswith('.py') else None
        if module and module.endswith('.__init__'):
            module = module[:-9]
        symbol = None
        if reference == path:
            pass
        elif module and reference == module:
            pass
        elif module and reference.startswith(module + '.'):
            symbol = reference[len(module) + 1:]
        elif path.endswith('.py') and reference.startswith(path + ':'):
            symbol = reference[len(path) + 1:]
        else:
            continue
        text = item.get('text')
        if not isinstance(text, str) or hashlib.sha256(text.encode()).hexdigest() != item.get('sha256'):
            raise ValueError('Required source reference has no intact archived text: ' + path)
        identity = {'reference': reference, 'path': path, 'source_sha256': item['sha256']}
        if symbol is None:
            matches.append(identity)
            continue
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError) as exc:
            raise ValueError('Required source symbol cannot be parsed: ' + reference) from exc
        matches.extend({**identity, 'symbol': name, 'start_line': node.lineno, 'end_line': node.end_lineno}
                       for name, node in _definitions(tree) if name == symbol)
    if len(matches) != 1:
        raise ValueError('Required source reference must resolve uniquely in the frozen archive: ' + reference)
    return matches[0]


def verified_alias(store, run_id, context, rows, reference, *, files=None):
    files = source_files(store, run_id, context) if files is None else files
    identity = resolve(files, reference)
    text = files[identity['path']]['text']
    ref = context['validation_evidence']
    ranges, receipts = [], []
    for row in rows:
        call, result = row.get('call', {}), row.get('result', {})
        args = call.get('arguments', {})
        start, end = result.get('offset'), result.get('end')
        if (call.get('name') != 'experiments.inspect_module' or args.get('view') != 'source'
                or result.get('view') != 'source'
                or args.get('snapshot', 'current') != 'current' or result.get('source_read') is not True
                or args.get('path') != identity['path'] or result.get('path') != identity['path']
                or args.get('evidence_id') != ref['id'] or args.get('expected_sha256') != ref['sha256']
                or result.get('evidence_sha256') != ref['sha256']
                or result.get('source_sha256') != identity['source_sha256']
                or result.get('receipt_sha256') != sha({'call': call,
                    'result': {k: v for k, v in result.items() if k != 'receipt_sha256'}})
                or type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)
                or result.get('total_chars') != len(text) or result.get('text') != text[start:end]):
            continue
        ranges.append((start, end))
        receipts.append(result['receipt_sha256'])
    cursor = 0
    for start, end in sorted(ranges):
        if start > cursor:
            break
        cursor = max(cursor, end)
    if not receipts or cursor != len(text):
        raise ValueError('Required source reference lacks complete authenticated source reads: ' + reference)
    return {**identity, 'ranges': [list(r) for r in sorted(ranges)],
            'receipt_sha256s': sorted(set(receipts)), 'resolution': 'frozen_source_symbol_identity',
            'execution_authorized': False}


def credit_existing(store, run_id, context, reads, references):
    """Reuse full source pages only from the same completed role assignment."""
    import copy
    from .engine import Harness, digest
    result = copy.deepcopy(reads)
    files = None
    for read in result:
        for reference in sorted(set(references) - set(read.get('source_paths', []))):
            for owner in dict.fromkeys([read.get('handoff_id'), read.get('supplemental_handoff_id')]):
                if not owner:
                    continue
                h = store.get('handoffs', owner) or {}
                inputs = store.get('handoff_inputs', owner) or {}
                payload = {k: inputs.get(k) for k in ('instruction', 'context', 'schema')}
                if (h.get('run_id') != run_id or h.get('to_role') != read['role']
                        or h.get('status') != 'completed' or inputs.get('run_id') != run_id
                        or h.get('input_hash') != digest(payload)
                        or (payload['context'] or {}).get('validation_evidence') != context['validation_evidence']
                        or Harness.decision_key(run_id, read['role'], h['purpose'], **payload) != owner):
                    continue
                files = source_files(store, run_id, context) if files is None else files
                # Invalid/ambiguous source names must fail; incomplete actual reads
                # remain missing and are dispatched through the normal page gate.
                resolve(files, reference)
                try:
                    proof = verified_alias(store, run_id, context,
                        (store.get('handoff_progress', owner) or {}).get('tool_results', []), reference, files=files)
                except ValueError:
                    continue
                read['source_paths'] = sorted(set(read.get('source_paths', [])) | {reference, proof['path']})
                read.setdefault('source_reference_resolutions', []).append({**proof, 'handoff_id': owner})
                break
    return result
