"""Recover malformed read-reference metadata from the exact frozen evidence.

Only the settled native-response transaction may consume this derived value.
The selected source, read arguments, original response and charges stay intact.
"""
from __future__ import annotations
from .prose_schema import validator as response_validator

import copy
import hashlib
import json
import re

from jsonschema import SchemaError, ValidationError

from .experiment_tools import comparison_reference
from .experiment_workflow import sha
from .json_format_recovery import _unique_object, completed_output

READS = frozenset({'experiments.compare', 'experiments.analyze', 'experiments.inspect_module'})
HASH = re.compile(r'^[0-9a-fA-F]{64}$')
KIND = 'experiment_reference_hash_from_frozen_evidence'
YAML_KIND = 'parameter_yaml_hash_from_verified_revision'


def _matches_copy(value, expected):
    """Match a prefix, an explicitly elided digest, or a near-complete copy."""
    if not isinstance(value, str) or len(value) > 66:
        return False
    value = value.lower()
    elided = re.fullmatch(r'([0-9a-f]+)(?:\.\.\.|…)([0-9a-f]*)', value)
    if elided:
        left, right = elided.groups()
        return 8 <= len(left) + len(right) < 64 and expected.startswith(left) and expected.endswith(right)
    if not re.fullmatch(r'[0-9a-f]{8,64}', value):
        return False
    if expected.startswith(value):
        return True
    remaining = iter(expected)
    return 60 <= len(value) < 64 and all(digit in remaining for digit in value)


def _verified_archive(store, run_id, ref):
    record = store.get('experiment_evidence', ref['id'], {})
    return (record.get('id') == ref['id'] and record.get('run_id') == run_id
            and isinstance(ref.get('sha256'), str) and HASH.fullmatch(ref['sha256'])
            and record.get('sha256') == ref['sha256'] and 'summary' in record
            and sha(record['summary']) == ref['sha256'])


def _yaml_edit(engine, run_id, key, ref, progress, proposal):
    from .hyperparameter_yaml import read_version, _view_with_schema
    spec = proposal['parameter_search']
    revision = spec['yaml_revision_id']
    row = read_version(engine, run_id, revision)
    body = row['body']
    evidence = engine.store.get('experiment_evidence', ref['id'])['summary']
    old, expected = spec.get('yaml_sha256'), body['yaml_sha256']
    if (row['id'] != revision or revision != run_id + ':hyperparameters:' + row['sha256']
            or not body['parent'] or body['binding'] != evidence['binding']
            or body['controls'] != {k: spec[k] for k in ('base_unit_id', 'validation_seed')}
            or not isinstance(old, str) or old == expected):
        return None
    # Also recognize a long copy with one omitted middle span. Both ends
    # must identify the exact, independently authenticated revision.
    partial = old.lower()
    middle_omitted = (re.fullmatch(r'[0-9a-f]{32,63}', partial)
        and any(expected.startswith(partial[:i]) and expected.endswith(partial[i:])
                for i in range(8, len(partial) - 7)))
    if not _matches_copy(old, expected) and not middle_omitted:
        return None
    for receipt in progress['tool_results']:
        call, result = receipt.get('call', {}), receipt.get('result', {})
        args = call.get('arguments', {})
        if result.get('revision_id') != revision or result.get('sha256') != expected:
            continue
        if call.get('name') == 'hyperparameters.revise':
            if (body['owner_handoff_id'] != key or body['source_evidence'] != ref
                    or args.get('revision_id') != body['parent']):
                continue
            read_version(engine, run_id, body['parent'], args['expected_sha256'])
        elif (call.get('name') != 'hyperparameters.read' or args.get('revision_id') != revision
              or args.get('expected_sha256', expected) != expected):
            continue
        page = _view_with_schema(row, evidence, args)
        checksum = sha({'call': call, 'result': page})
        if result == {**page, 'receipt_sha256': checksum}:
            spec['yaml_sha256'] = expected
            return {'path': '/parameter_search/yaml_sha256', 'old': old, 'new': expected,
                    'yaml_revision_id': revision, 'receipt_sha256': checksum}
    return None


def _normalize(h, run_id, role, key, progress, prepared, text, metadata):
    from .engine import Harness, digest
    store = h.engine.store
    inputs = store.get('handoff_inputs', key, {})
    assignment = store.get('handoffs', key, {})
    run = store.get('runs', run_id, {})
    frozen, context = inputs.get('context', {}), prepared.get('context', {})
    final_only = progress.get('mode') == 'final_only'
    request = progress.get('finalization' if final_only else 'normal_request', {})
    ref = frozen.get('validation_evidence', {})
    if (not completed_output(metadata) or not isinstance(text, str) or not 0 < len(text) <= 24_000
            or not isinstance(ref, dict) or not isinstance(ref.get('sha256'), str)
            or not HASH.fullmatch(ref['sha256']) or not ref.get('id')
            or frozen.get('experiment_workflow_protocol') != 1 or context.get('experiment_workflow_protocol') != 1
            or context.get('validation_evidence') != ref or context.get('research_cycle') != frozen.get('research_cycle')
            or frozen.get('research_cycle') != h.engine.cycle(run_id)
            or role not in {'director', 'engineer', 'scout'} or prepared.get('role') != role
            or any(row.get('id') != key or row.get('run_id') != run_id for row in (inputs, assignment, progress))
            or assignment.get('to_role') != role or assignment.get('status') == 'completed'
            or run.get('stage') != assignment.get('stage')
            or store.get('decisions', key) is not None or store.get('run_deletions', run_id)
            or progress.get('pending_tool_calls')
            or store.get('handoff_progress', key) != progress
            or request.get('status') not in {'blocked', 'dispatching'} or request.get('response') is not None
            or request.get('prepared') != prepared
            or context.get('_purpose') != assignment.get('purpose')
            or key != Harness.decision_key(run_id, role, assignment.get('purpose'),
                inputs.get('instruction'), frozen, inputs.get('schema'))
            or assignment.get('input_hash') != digest({k: inputs.get(k) for k in ('instruction', 'context', 'schema')})):
        return None
    if final_only:
        control = context.get('_harness_control', {})
        if (control.get('mode') != 'final_only' or not request.get('operation_id')
                or control.get('operation_id') != request['operation_id']
                or request.get('completed_result_hashes') != [digest(item) for item in progress['tool_results']]):
            return None
    elif request.get('turn') != progress.get('turn') or request.get('prepared_sha256') != digest(prepared):
        return None
    value = json.loads(text, object_pairs_hook=_unique_object,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))
    if not isinstance(value, dict):
        return None
    tool_reply = not final_only and set(value) == {'tool_calls'} and isinstance(value['tool_calls'], list)
    proposal = (role == 'engineer' and assignment['purpose'].startswith('experiment_decide:')
                and 'tool_calls' not in value and value.get('action') == 'search_parameters'
                and value.get('evidence_sha256') == ref['sha256']
                and isinstance(value.get('parameter_search'), dict))
    if not tool_reply and not proposal:
        return None
    if not _verified_archive(store, run_id, ref):
        return None
    verified = {ref['id']}
    result, edits = copy.deepcopy(value), []
    if proposal:
        edit = _yaml_edit(h.engine, run_id, key, ref, progress, result)
        if not edit:
            return None
        edits.append(edit)
    for i, call in enumerate(result.get('tool_calls', [])):
        if not isinstance(call, dict) or call.get('name') not in READS:
            continue
        args = call.get('arguments', {})
        if not isinstance(args, dict):
            return None
        evidence_id = args.get('evidence_id')
        target = ref
        if call['name'] == 'experiments.compare':
            target = comparison_reference(frozen, evidence_id)
            if not target or target != comparison_reference(context, evidence_id):
                return None
        if evidence_id != target['id']:
            return None
        if evidence_id not in verified:
            if not _verified_archive(store, run_id, target):
                return None
            verified.add(evidence_id)
        expected = target['sha256']
        old = args.get('expected_sha256')
        missing = 'expected_sha256' not in args
        if not missing and not _matches_copy(old, expected):
            return None
        if old != expected:
            args['expected_sha256'] = expected
            edits.append({'path': f'/tool_calls/{i}/arguments/expected_sha256',
                'old': old, 'missing': missing, 'new': expected, 'evidence_id': evidence_id})
    if not edits:
        return None
    # The full frozen tool allowlist and all other fields must still validate.
    response_validator(prepared['schema']).validate(result)
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return result, {'kind': YAML_KIND if proposal else KIND, 'version': 1, 'source_handoff_id': key, 'role': role,
        'original_sha256': hashlib.sha256(text.encode()).hexdigest(),
        'repaired_sha256': hashlib.sha256(encoded.encode()).hexdigest(), 'schema_sha256': digest(prepared['schema']),
        'validation_evidence_id': ref['id'], 'edits': edits,
        **({'evidence_id': edits[0]['evidence_id'], 'evidence_sha256': edits[0]['new']}
           if tool_reply and len({edit['evidence_id'] for edit in edits}) == 1 else {}),
        'charges_added': 0, 'scientific_claims_verified': False, 'scientific_approval': False}


def normalize(*args):
    try:
        return _normalize(*args)
    except (ValueError, TypeError, KeyError, AttributeError, OSError, RecursionError, SchemaError, ValidationError):
        return None
