"""Authenticate a short hash omission in one frozen bounded-diagnostic review.

The original reviewer must correct the field. A valid different digest, missing
reading, modified files, or any second schema defect is not this recovery.
"""
from __future__ import annotations

import hashlib
import re
from types import SimpleNamespace

from jsonschema import Draft202012Validator


def omission(store, run_id, key, value):
    from .engine import Harness, ResearchBlocked, digest
    from .bounded_diagnostics import COLLECTION
    from .code_bundle import validate as validate_bundle
    from .experiment_analysis import inspect_module
    from .experiment_workflow import sha
    from .rapid_experiment_revision import review_receipts

    owner = store.get('handoffs', key) or {}
    if not owner.get('purpose', '').startswith('rapid_review:bounded:'):
        return None
    inputs = store.get('handoff_inputs', key) or {}
    context, schema = inputs.get('context', {}), inputs.get('schema', {})
    scope = context.get('rapid_revision') or {}
    record = store.get(COLLECTION, scope.get('id', '')) or {}
    role = owner.get('to_role')
    if (not isinstance(value, dict) or 'tool_calls' in value
            or record.get('run_id') != run_id or record.get('status') != 'reviewing_code'
            or role not in {'director', 'engineer', 'scout'} or not record.get('rounds')):
        return None
    request = record['rounds'][-1].get('votes', {}).get(role) or {}
    request = request.get('read_correction') or request
    payload = {k: inputs.get(k) for k in ('instruction', 'context', 'schema')}
    if (inputs.get('id') != key or inputs.get('run_id') != run_id
            or owner.get('id') != key or owner.get('run_id') != run_id
            or owner.get('input_hash') != digest(payload)
            or key != Harness.decision_key(run_id, role, owner['purpose'], **payload)
            or request.get('purpose') != owner['purpose']
            or any(request.get(k) != v for k, v in payload.items())):
        return None
    candidate = record.get('candidate') or {}
    hashes = {'amendment_sha256': candidate.get('amendment_sha256'),
              'candidate_sha256': candidate.get('bundle', {}).get('sha256'),
              'checks_sha256': candidate.get('checks_sha256')}
    errors = list(Draft202012Validator(schema).iter_errors(value))
    if len(errors) != 1 or errors[0].validator not in {'pattern', 'const'}:
        return None
    path = list(errors[0].path)
    if len(path) != 1 or path[0] not in hashes:
        return None
    field = path[0]
    bad, expected = value.get(field), hashes[field]
    if (not isinstance(bad, str) or not re.fullmatch('[a-f0-9]{60,63}', bad)
            or not isinstance(expected, str) or not re.fullmatch('[a-f0-9]{64}', expected)
            or any(scope.get(k) != v or (k != field and value.get(k) != v) for k, v in hashes.items())):
        return None
    remaining = iter(expected)
    if not all(char in remaining for char in bad):
        return None
    if (not isinstance(record.get('code'), str)
            or hashlib.sha256(record['code'].encode()).hexdigest() != hashes['checks_sha256']):
        return None
    engine = SimpleNamespace(store=store)
    try:
        validate_bundle(candidate['bundle'], record['code'])
        ref = context['validation_evidence']
        archive = store.get('experiment_evidence', ref['id']) or {}
        sources = store.get('experiment_sources', candidate['code_sources']['id']) or {}
        if (archive.get('run_id') != run_id or sha(archive.get('summary')) != ref['sha256']
                or context.get('evidence_sha256') != ref['sha256']
                or sources.get('run_id') != run_id or sha(sources.get('files')) != candidate['code_sources']['sha256']):
            return None
        proof = review_receipts(engine, run_id, record, {'role': role, **value}, context,
            purpose=owner['purpose'], instruction=inputs['instruction'], schema=schema)
        # Reauthenticate the actual page bytes, in addition to the complete
        # changed-range coverage checked by the ordinary reviewer gate.
        seen = set()
        for row in (store.get('handoff_progress', key) or {}).get('tool_results', []):
            call, result = row.get('call') or {}, row.get('result') or {}
            if call.get('name') != 'experiments.inspect_module' or result.get('receipt_sha256') not in value['read_receipts']:
                continue
            body = {k: v for k, v in result.items() if k != 'receipt_sha256'}
            if inspect_module(engine, run_id, context, call['arguments']) != body:
                return None
            seen.add(result['receipt_sha256'])
        if not seen:
            return None
    except (ResearchBlocked, ValueError, KeyError, IndexError, TypeError, OSError):
        return None
    return {'version': 1, 'kind': 'bounded_diagnostic_review', 'field': field,
            'original_value': bad, 'expected_value': expected, 'omitted_hex_digits': 64 - len(bad),
            'revision_id': record['id'], 'review_handoff_id': key,
            'original_vote_sha256': digest(value), 'read_receipts_sha256': digest(value['read_receipts']),
            'coverage_sha256': digest(proof), 'execution_authorized': False, 'scientific_approval': False}
