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

from jsonschema import Draft202012Validator, SchemaError, ValidationError

from .experiment_workflow import sha
from .json_format_recovery import _unique_object, completed_output

READS = frozenset({'experiments.compare', 'experiments.analyze', 'experiments.inspect_module'})
HASH = re.compile(r'^[0-9a-fA-F]{64}$')
KIND = 'experiment_reference_hash_from_frozen_evidence'


def _normalize(h, run_id, role, key, progress, prepared, text, metadata):
    from .engine import Harness, digest
    store = h.engine.store
    inputs = store.get('handoff_inputs', key, {})
    assignment = store.get('handoffs', key, {})
    run = store.get('runs', run_id, {})
    frozen, context = inputs.get('context', {}), prepared.get('context', {})
    normal = progress.get('normal_request', {})
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
            or progress.get('mode') == 'final_only' or progress.get('pending_tool_calls')
            or store.get('handoff_progress', key) != progress
            or normal.get('status') not in {'blocked', 'dispatching'} or normal.get('response') is not None
            or normal.get('turn') != progress.get('turn') or normal.get('prepared') != prepared
            or normal.get('prepared_sha256') != digest(prepared)
            or context.get('_purpose') != assignment.get('purpose')
            or key != Harness.decision_key(run_id, role, assignment.get('purpose'),
                inputs.get('instruction'), frozen, inputs.get('schema'))
            or assignment.get('input_hash') != digest({k: inputs.get(k) for k in ('instruction', 'context', 'schema')})):
        return None
    value = json.loads(text, object_pairs_hook=_unique_object,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))
    if not isinstance(value, dict) or set(value) != {'tool_calls'} or not isinstance(value['tool_calls'], list):
        return None
    record = store.get('experiment_evidence', ref['id'], {})
    if (record.get('id') != ref['id'] or record.get('run_id') != run_id
            or record.get('sha256') != ref['sha256'] or sha(record.get('summary')) != ref['sha256']):
        return None
    result, edits = copy.deepcopy(value), []
    for i, call in enumerate(result['tool_calls']):
        if not isinstance(call, dict) or call.get('name') not in READS:
            continue
        args = call.get('arguments', {})
        if not isinstance(args, dict):
            return None
        old = args.get('expected_sha256')
        missing = 'expected_sha256' not in args
        # A syntactically valid different hash denotes a different version;
        # never reinterpret it as a copying error or silently select a source.
        if (args.get('evidence_id') != ref['id'] or not missing and (
                not isinstance(old, str) or not 1 <= len(old) <= 128
                or HASH.fullmatch(old) and old != ref['sha256'])):
            return None
        if old != ref['sha256']:
            args['expected_sha256'] = ref['sha256']
            edits.append({'path': f'/tool_calls/{i}/arguments/expected_sha256',
                'old': old, 'missing': missing, 'new': ref['sha256'], 'evidence_id': ref['id']})
    if not edits:
        return None
    # The full frozen tool allowlist and all other fields must still validate.
    response_validator(prepared['schema']).validate(result)
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return result, {'kind': KIND, 'version': 1, 'source_handoff_id': key, 'role': role,
        'original_sha256': hashlib.sha256(text.encode()).hexdigest(),
        'repaired_sha256': hashlib.sha256(encoded.encode()).hexdigest(), 'schema_sha256': digest(prepared['schema']),
        'evidence_id': ref['id'], 'evidence_sha256': ref['sha256'], 'edits': edits,
        'charges_added': 0, 'scientific_claims_verified': False, 'scientific_approval': False}


def normalize(*args):
    try:
        return _normalize(*args)
    except (ValueError, TypeError, KeyError, AttributeError, OSError, RecursionError, SchemaError, ValidationError):
        return None
