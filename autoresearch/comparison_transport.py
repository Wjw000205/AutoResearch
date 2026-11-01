"""Versioned, bounded transport of program-produced comparison evidence.

This module never fits a model or computes scientific comparison statistics.
Its capability description is an implementation contract, not a test receipt or
execution authorization. A transport declaration must be part of the approved
SPEC or an explicitly authorized, hash-bound host protocol repair; merely
mentioning its name in prose does not enable it.
"""
from __future__ import annotations

import base64
import asyncio
import copy
import hashlib
import json
import math
import re
import struct
from pathlib import Path

MIB = 1024 * 1024
RECORD_LIMIT = 32 * MIB
PARAMS_LIMIT = 256 * MIB
OUTPUT_LIMIT = 320 * MIB
MAX_RECORDS = 9
HEX = re.compile(r'^[0-9a-f]{64}$')
ARRAY_KEYS = {'dtype', 'shape', 'axes', 'unit', 'encoding', 'data', 'sha256'}
BINDING_KEYS = ('proposal_hash', 'design_hash', 'plan_hash', 'implementation_document_sha256', 'spec_hash')
RECORD_KEYS = {*BINDING_KEYS, 'protocol_version', 'configuration_hash', 'actual_params', 'phase', 'seed',
               'dataset_id', 'split_fingerprints', 'payload', 'evidence_sha256'}


def _object(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


async def committed_io(function, *args):
    """Finish an admitted checkpoint write before releasing its cancelled owner."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                pass
            except Exception:
                break
        try:
            task.result()
        except Exception:
            pass
        raise


CONTRACT_SCHEMA = _object({
    'protocol_version': {'const': 1},
    'validation_consumer': _object({'pair_index': {'type': 'integer', 'minimum': 0}}),
    'payload_arrays': {'type': 'array', 'minItems': 1, 'maxItems': 100, 'items': _object({
        'path': {'type': 'string', 'minLength': 1, 'maxLength': 200},
        'dtype': {'enum': ['<f8', '<i8']},
        'shape': {'type': 'array', 'maxItems': 8, 'items': {'anyOf': [
            {'type': 'integer', 'minimum': 0}, {'enum': ['N', '*']}]}},
        'axes': {'type': 'array', 'maxItems': 8, 'items': {'type': 'string', 'minLength': 1}},
        'unit': {'type': 'string', 'minLength': 1, 'maxLength': 100},
        'required': {'type': 'boolean'}})}})
CONTRACT_SCHEMA['properties']['payload_arrays']['items']['properties']['shared'] = {'type': 'boolean'}
CONTRACT_SCHEMA['properties']['payload_layout'] = _object({
    key: {'type': 'string', 'pattern': '^[a-z][a-z0-9_]{0,79}$'}
    for key in ('sample_ids', 'condition_ids', 'target', 'sample_axis')})
CONTRACT_SCHEMA['properties']['split_identity_protocol'] = {'type': 'integer', 'const': 2}
CONTRACT_SCHEMA['properties']['acceptance_phases'] = {
    'type': 'array', 'minItems': 1, 'maxItems': 64,
    'items': _object({
        'metric': {'type': 'string', 'pattern': '^val_.+'},
        'phases': {'type': 'array', 'minItems': 1, 'maxItems': 2, 'uniqueItems': True,
                   'items': {'enum': ['validation', 'test']}}})}


def acceptance_metric(criterion, phase):
    if phase not in ('validation', 'test'):
        raise ValueError('Unknown acceptance phase')
    return criterion['metric'].replace('val_', 'test_', 1) if phase == 'test' else criterion['metric']


def phase_criteria(criteria, *, phase, contract=None, primary_metric=None):
    """Resolve an explicit approved phase overlay; never infer scope from a name."""
    if phase not in ('validation', 'test'):
        raise ValueError('Unknown acceptance phase')
    declaration = (contract or {}).get('acceptance_phases')
    if declaration is None:
        return copy.deepcopy(criteria)
    if not isinstance(criteria, list) or not criteria:
        raise ValueError('Acceptance phases require the complete frozen criteria')
    metrics = [row.get('metric') for row in criteria if isinstance(row, dict)]
    if (len(metrics) != len(criteria) or any(not isinstance(metric, str) or not metric.startswith('val_') for metric in metrics)
            or len(metrics) != len(set(metrics))):
        raise ValueError('Acceptance phases require distinct frozen val_ metrics')
    if not isinstance(declaration, list) or not declaration:
        raise ValueError('Acceptance phases must declare every frozen metric exactly once')
    declared = {}
    for row in declaration:
        if (not isinstance(row, dict) or set(row) != {'metric', 'phases'}
                or not isinstance(row['metric'], str) or row['metric'] in declared
                or not isinstance(row['phases'], list) or not row['phases']
                or any(value not in ('validation', 'test') for value in row['phases'])
                or len(row['phases']) != len(set(row['phases']))
                or 'validation' not in row['phases']):
            raise ValueError('Acceptance phases must retain every validation gate without duplicate or unknown phases')
        declared[row['metric']] = row['phases']
    if set(declared) != set(metrics):
        raise ValueError('Acceptance phases must declare every frozen metric exactly once')
    test_metrics = {metric for metric, phases in declared.items() if 'test' in phases}
    if not test_metrics or primary_metric is not None and primary_metric not in test_metrics:
        raise ValueError('Acceptance phases must retain final test criteria and the frozen primary metric')
    return copy.deepcopy([row for row in criteria if phase in declared[row['metric']]])


def canonical(value):
    """Protocol JSON, with strict JSON types and no silent key coercion."""
    def check(item):
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError('Comparison JSON object keys must be strings')
            for child in item.values():
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise ValueError('Comparison transport accepts only JSON values')
        elif type(item) is float and not math.isfinite(item):
            raise ValueError('Comparison JSON must be finite')
    check(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def frozen_digest(value):
    """Existing workflow identity format; deliberately not the wire format."""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def strict_loads(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate comparison JSON key: ' + key)
            result[key] = value
        return result
    def invalid(value):
        raise ValueError('Nonfinite comparison JSON: ' + value)
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def validation_invocations(plan):
    from .benchmark_matrix import enabled
    if enabled(plan):
        raise ValueError('Comparison transport v1 is single-task; benchmark matrices use independent ordinary task runners')
    if (not isinstance(plan, dict) or not isinstance(plan.get('validation_pairs'), list)
            or not plan['validation_pairs'] or not isinstance(plan.get('ablation_candidates'), list)
            or type(plan.get('selection_seed')) is not int
            or any(not isinstance(pair, dict) or any(not isinstance(pair.get(key), dict) for key in ('baseline', 'proposed'))
                   for pair in plan['validation_pairs'])
            or any(not isinstance(item, dict) for item in plan['ablation_candidates'])):
        raise ValueError('Comparison transport requires a complete frozen validation plan')
    rows = []
    for index, pair in enumerate(plan['validation_pairs']):
        for variant in ('baseline', 'proposed'):
            name = f'initial-{variant}' if index == 0 else f'grid-{index}-{variant}'
            rows.append((name, {**copy.deepcopy(pair[variant]), 'phase': 'validation',
                               'seed': pair.get('seed', plan['selection_seed'])}))
    for index, params in enumerate(plan['ablation_candidates']):
        rows.append(('ablation' if index == 0 else f'ablation-{index}',
                     {**copy.deepcopy(params), 'phase': 'validation', 'seed': params.get('seed', plan['selection_seed'])}))
    return rows


def validate_contract(contract, *, plan=None, primary_metric=None):
    if contract is None:
        return None
    from jsonschema import ValidationError
    from .prose_schema import validate
    try:
        validate(contract, CONTRACT_SCHEMA)
    except ValidationError as exc:
        raise ValueError('Invalid comparison transport contract: ' + exc.message) from exc
    # jsonschema integer accepts 1.0; protocol identities do not.
    if type(contract['protocol_version']) is not int or type(contract['validation_consumer']['pair_index']) is not int:
        raise ValueError('Comparison protocol and pair index must be JSON integers')
    arrays = {row['path']: row for row in contract['payload_arrays']}
    layout = payload_layout(contract)
    generic = 'payload_layout' in contract
    core = {layout['target']} if generic else {'target', 'prediction', 'sequence_mse'}
    if (len(arrays) != len(contract['payload_arrays']) or not core <= arrays.keys()
            or len({layout[k] for k in ('sample_ids', 'condition_ids', 'target')}) != 3
            or {layout['sample_ids'], layout['condition_ids'], 'diagnostics'} & arrays.keys()):
        raise ValueError('Comparison payload requires distinct declared arrays and identity fields')
    for path, row in arrays.items():
        if (not generic and path not in core and not path.startswith('diagnostics.')
                or any(not part or part in {'__proto__', '..'} for part in path.split('.'))):
            raise ValueError('Invalid comparison payload array path')
        if len(row['shape']) != len(row['axes']) or len(set(row['axes'])) != len(row['axes']):
            raise ValueError('Comparison array axes must match rank and be unique')
        if row.get('shared') and layout['sample_axis'] in row['axes'] and row['axes'][0] != layout['sample_axis']:
            raise ValueError('Shared comparison arrays require the declared sample axis first')
        if any(type(v) is not int and v not in ('N', '*') for v in row['shape']):
            raise ValueError('Comparison dimensions must be integers, N or *')
        if any(type(v) is int and (v < 0 or v > RECORD_LIMIT // 8) for v in row['shape']):
            raise ValueError('Comparison dimension exceeds bounded record capacity')
    for key in core:
        row = arrays[key]
        if (not generic and row['dtype'] != '<f8' or row['required'] is not True
                or not row['shape'] or row['shape'][0] != 'N'
                or generic and row['axes'][0] != layout['sample_axis']):
            raise ValueError('Core comparison arrays require a declared sample dimension N and required=true')
    if not generic and (arrays['target']['shape'] != arrays['prediction']['shape']
            or arrays['target']['axes'] != arrays['prediction']['axes']
            or arrays['target']['unit'] != arrays['prediction']['unit']
            or len(arrays['target']['shape']) != 3 or arrays['sequence_mse']['shape'] != ['N']):
        raise ValueError('Comparison target/prediction layouts must agree and sequence_mse must have shape [N]')
    if plan is not None:
        phase_criteria(plan.get('acceptance', {}).get('criteria', []), phase='test',
                       contract=contract, primary_metric=primary_metric)
        rows = validation_invocations(plan)
        if not 2 <= len(rows) <= MAX_RECORDS + 1 or len(plan.get('grid', [])) != 1:
            raise ValueError('Comparison protocol v1 supports one frozen grid configuration and at most ten validation calls')
        if len(plan['validation_pairs']) == 1 and len(rows) != 3:
            raise ValueError('Single-seed comparison plans require exactly three branches for bounded code acceptance')
        if not 0 <= contract['validation_consumer']['pair_index'] < len(plan['validation_pairs']):
            raise ValueError('Comparison consumer is absent from the frozen validation plan')
        if len({digest(params) for _, params in rows}) != len(rows):
            raise ValueError('Comparison producer identities must be unique')
        if len(_final_seeds(plan)) != 1:
            raise ValueError('Comparison protocol v1 requires one frozen final test seed; it does not invent cross-test aggregation')
    return copy.deepcopy(contract)


def payload_layout(contract):
    # Missing layout denotes the frozen original wire contract. New SPECs
    # declare their own identity fields, target role and sample axis.
    return contract.get('payload_layout') or {'sample_ids': 'sequence_ids',
        'condition_ids': 'condition_ids', 'target': 'target', 'sample_axis': 'sequence'}


def capability_description(plan=None):
    result = {'protocol_version': 1, 'evidence_type': 'implemented_contract',
        'grants_execution': False, 'scientific_validation': False,
        'schema': copy.deepcopy(CONTRACT_SCHEMA),
        'limits': {'record_bytes': RECORD_LIMIT, 'records': MAX_RECORDS,
                   'params_bytes': PARAMS_LIMIT, 'final_output_bytes': OUTPUT_LIMIT},
        'wire_keys': ['protocol_version', 'role', 'records'],
        'roles': ['producer', 'validation_finalize', 'test_finalize'],
        'scope': 'Pure archival, integrity checks, dependency ordering and unchanged injection; all fitting and scientific statistics remain in the approved program.',
        'layout': 'Declare payload_layout with sample_ids and condition_ids field names, target array name and sample_axis. Sample IDs are unique strings; condition IDs are int64. Declare actual payload_arrays paths, dtype, shape, axes, units and shared status from the task protocol. Arrays use canonical base64 little-endian <f8 or <i8 C-order. N is sample count; * is bounded dynamic size. Target and prediction layouts may differ when declared by the task.',
        'activation': 'Only a structured comparison_transport declaration in the approved immutable SPEC enables transport; absent/null remains disabled.',
        'supported_plan': 'One frozen shared grid configuration, at most ten validation invocations; selected proposed validation call consumes every other validation record. Each final proposed consumes its same-seed baseline. A single validation seed requires exactly three branches so code acceptance remains three fixed calls; larger plans need baseline/proposed/ablation producer calls for their three smokes.',
        'resource_accounting': 'The runner reserves bounded transport memory from the admitted RAM and includes input/output archives in the same disk and wall-time ceilings. IO policy 2 charges actual serialized input and bounded stream buffers during execution; full output decoding starts only after the child and descendants exit. Both phases independently fit the original total RAM ceiling. The invocation bootstrap comparison_io carries the exact allocation.',
        'host_memory_reserve_bytes': 8 * (PARAMS_LIMIT + OUTPUT_LIMIT) + 2 * RECORD_LIMIT,
        'acceptance_scope': 'Without an explicit acceptance_phases declaration, every frozen criterion applies to validation and final test with the existing val_ to test_ mapping. The optional exhaustive acceptance_phases list assigns every frozen metric exactly once, always retains validation, and keeps the primary metric and at least one criterion on test. This immutable SPEC declaration requires independent same-version approval against the frozen scientific scope; it never changes thresholds, parameters, seeds or invocation counts. No phase scope is inferred from metric names or prose. Protocol v1 supports one final seed.',
        'record_keys': sorted(RECORD_KEYS),
        'phase_bound_identity': {
            'opt_in': 'Approved comparison_transport.split_identity_protocol=2 and host evaluation_contract_version>=2',
            'extra_record_keys': ['split_identity_protocol', 'split_materialization'],
            'validation': 'Exactly train/validation real fingerprints; test state not_materialized',
            'test': 'All three real fingerprints; all states materialized',
            'legacy': 'Without explicit opt-in, all three real fingerprints remain required'},
        'payload_keys': 'Task-declared identity fields, declared array paths and diagnostics object',
        'array_keys': sorted(ARRAY_KEYS),
        'canonical_json': {'encoding': 'UTF-8', 'ensure_ascii': False, 'sort_keys': True,
            'separators': [',', ':'], 'allow_nan': False, 'duplicate_keys': 'reject', 'non_string_object_keys': 'reject'},
        'hashes': {'evidence_sha256': 'SHA256 of canonical record JSON after removing only evidence_sha256',
            'configuration_hash': 'SHA256 of canonical record.actual_params JSON',
            'array_sha256': 'SHA256 of complete decoded little-endian C-order bytes',
            'bindings': 'Copy real proposal_hash/design_hash/plan_hash/implementation_document_sha256/spec_hash from the approved immutable workflow; never invent them or recompute frozen workflow hashes with the wire JSON format.'},
        'actual_params': {'evaluation': 'Exact full invocation input, including comparison_inputs unchanged',
            'trial_evidence': 'Exact invocation input with only the top-level comparison_inputs removed, to avoid recursive records'},
        'evaluation': {'required': ['dataset_id', 'split_fingerprints', 'phase', 'seed', 'actual_params', 'trial_evidence', 'comparison_state'],
            'split_fingerprints': 'Distinct real SHA256 strings for train/validation/test sample IDs',
            'producer': {'comparison_state': 'pending', 'records': [], 'effect': 'Archive the real own trial_evidence and primary raw metric; does not authorize held-out evaluation'},
            'consumer': {'comparison_state': 'complete', 'acceptance_inputs': '{frozen_metric_name:{reference:finite_number,candidate:finite_number,relative_improvement:finite_number}}',
                'effect': 'Must contain exactly the frozen criteria applicable to this phase under the approved declaration, with val_ to test_ mapping only on test. Missing, extra and substituted-phase acceptance_inputs are rejected. Host checks only reported arithmetic consistency and unchanged threshold/direction/comparison, never recomputes scientific statistics.'},
            'failed': 'failed or missing required fields makes the invocation fail; it never substitutes for complete'},
        'shared_payload': 'Core target values and condition IDs must match after sequence-ID alignment. Mark payload_arrays.shared=true for declared shared stratification arrays; their values must also match. Method-specific diagnostics are not forced equal.',
        'verification': 'This description is not a fixture result, host deployment verification receipt, or research result.'}
    if plan is not None:
        result['plan_hash'] = frozen_digest(plan)
        try:
            rows = validation_invocations(plan)
            if not 2 <= len(rows) <= MAX_RECORDS + 1 or len(plan.get('grid', [])) != 1:
                raise ValueError('Protocol v1 needs one shared grid configuration and at most ten validation calls')
            if len(plan['validation_pairs']) == 1 and len(rows) != 3:
                raise ValueError('Single-seed protocol needs exactly three branches for bounded code acceptance')
            if len(_final_seeds(plan)) != 1:
                raise ValueError('Protocol v1 requires one frozen final test seed')
            result['validation_consumers'] = [{'pair_index': index, 'variant': 'proposed',
                'seed': pair.get('seed', plan['selection_seed']),
                'producer_record_count': len(rows) - 1} for index, pair in enumerate(plan['validation_pairs'])]
            result['plan_compatible'] = True
        except (ValueError, TypeError) as exc:
            result.update(plan_compatible=False, validation_consumers=[], plan_unavailable_reason=str(exc))
    return result


def ordered_validation(plan, contract):
    validate_contract(contract, plan=plan)
    rows = validation_invocations(plan)
    index = contract['validation_consumer']['pair_index']
    consumer = rows[index * 2 + 1]
    return [row for row in rows if row[0] != consumer[0]] + [consumer]


def _final_seeds(plan):
    from .experiment_plan import final_test_seeds
    try:
        return final_test_seeds(plan)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('Comparison transport requires complete frozen final_test_seeds/final_pairs') from exc


def role_for(params, plan, contract):
    if params.get('phase') == 'validation':
        consumer = ordered_validation(plan, contract)[-1][1]
        return 'validation_finalize' if canonical(params) == canonical(consumer) else 'producer'
    if params.get('phase') == 'test' and params.get('variant') in ('baseline', 'proposed'):
        return 'test_finalize' if params['variant'] == 'proposed' else 'producer'
    raise ValueError('Comparison invocation phase/variant is not supported')


def _array(value, declaration, count):
    if not isinstance(value, dict) or set(value) != ARRAY_KEYS:
        raise ValueError('Comparison array fields differ from the protocol')
    if any(value.get(key) != declaration[key] for key in ('dtype', 'axes', 'unit')) or value.get('encoding') != 'base64':
        raise ValueError('Comparison array dtype, axes, unit or encoding differs from SPEC')
    shape = value['shape']
    if not isinstance(shape, list) or len(shape) != len(declaration['shape']):
        raise ValueError('Comparison array rank differs from SPEC')
    elements = 1
    for actual, expected in zip(shape, declaration['shape']):
        if type(actual) is not int or not 0 <= actual <= RECORD_LIMIT // 8:
            raise ValueError('Comparison array dimension exceeds record capacity')
        if expected != '*' and actual != (count if expected == 'N' else expected):
            raise ValueError('Comparison array shape differs from SPEC')
        elements *= actual
        if elements > RECORD_LIMIT // 8:
            raise ValueError('Comparison decoded array exceeds record capacity')
    nbytes = elements * 8
    data = value['data']
    if not isinstance(data, str) or len(data) != 4 * ((nbytes + 2) // 3):
        raise ValueError('Comparison base64 length differs from the declared decoded size')
    try:
        raw = base64.b64decode(data, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError('Invalid comparison base64') from exc
    if len(raw) != nbytes or base64.b64encode(raw).decode('ascii') != data:
        raise ValueError('Noncanonical or truncated comparison array')
    if hashlib.sha256(raw).hexdigest() != value['sha256']:
        raise ValueError('Comparison array content hash mismatch')
    if value['dtype'] == '<f8' and any(not math.isfinite(v[0]) for v in struct.iter_unpack('<d', raw)):
        raise ValueError('Comparison arrays must contain finite floating point values')
    return raw


def validate_record(record, *, bindings, params, contract):
    from . import split_identity
    phased = type(contract.get('split_identity_protocol')) is int and contract['split_identity_protocol'] == 2
    keys = RECORD_KEYS | ({'split_identity_protocol', 'split_materialization'} if phased else set())
    if not isinstance(record, dict) or set(record) != keys or len(canonical(record)) > RECORD_LIMIT:
        raise ValueError('Comparison record fields or size differ from the protocol')
    if type(record['protocol_version']) is not int or record['protocol_version'] != 1:
        raise ValueError('Comparison record protocol mismatch')
    if any(not isinstance(bindings.get(k), str) or not HEX.fullmatch(bindings[k])
           or record[k] != bindings[k] for k in BINDING_KEYS):
        raise ValueError('Comparison record belongs to a different approved version')
    if canonical(record['actual_params']) != canonical(params) or 'comparison_inputs' in record['actual_params']:
        raise ValueError('Comparison record actual_params mismatch or recursive transport')
    if record['configuration_hash'] != digest(params) or record['phase'] != params['phase'] or type(record['seed']) is not int or record['seed'] != params['seed']:
        raise ValueError('Comparison record invocation identity mismatch')
    if record['evidence_sha256'] != digest({k: v for k, v in record.items() if k != 'evidence_sha256'}):
        raise ValueError('Comparison evidence hash mismatch')
    splits = record['split_fingerprints']
    if not isinstance(record['dataset_id'], str) or not record['dataset_id'].strip():
        raise ValueError('Comparison dataset identity is invalid')
    if phased:
        if not split_identity.enabled(record) or split_identity.validate(record, params['phase'], host_version=2):
            raise ValueError('Comparison phase-bound split identity is invalid')
    elif (not isinstance(splits, dict) or set(splits) != {'train', 'validation', 'test'}
            or any(not isinstance(v, str) or not HEX.fullmatch(v) for v in splits.values()) or len(set(splits.values())) != 3):
        raise ValueError('Comparison dataset and split identity is invalid')
    payload = record['payload']
    layout = payload_layout(contract)
    identity_keys = {layout['sample_ids'], layout['condition_ids'], 'diagnostics'}
    allowed = identity_keys | {row['path'].split('.')[0] for row in contract['payload_arrays']}
    required = identity_keys | {row['path'].split('.')[0] for row in contract['payload_arrays'] if row['required']}
    if not isinstance(payload, dict) or not required <= payload.keys() or not payload.keys() <= allowed:
        raise ValueError('Comparison payload fields differ from the protocol')
    ids, conditions = payload[layout['sample_ids']], payload[layout['condition_ids']]
    if (not isinstance(ids, list) or not ids or any(not isinstance(v, str) or not v for v in ids)
            or len(set(ids)) != len(ids) or not isinstance(conditions, list) or len(conditions) != len(ids)
            or any(type(v) is not int or not -(2**63) <= v < 2**63 for v in conditions)
            or not isinstance(payload['diagnostics'], dict)):
        raise ValueError('Comparison sequence IDs or int64 condition IDs are invalid')
    declarations = {row['path']: row for row in contract['payload_arrays']}
    seen, target, shared = set(), None, {}
    def walk(value, path):
        nonlocal target
        if isinstance(value, dict) and (set(value) == ARRAY_KEYS or 'encoding' in value and 'data' in value):
            if path not in declarations:
                raise ValueError('Undeclared comparison array: ' + path)
            raw = _array(value, declarations[path], len(ids))
            seen.add(path)
            if path == layout['target']:
                target = raw
            if declarations[path].get('shared'):
                shared[path] = {'bytes': raw, 'shape': value['shape'], 'sequence_axis': value['axes'].index(layout['sample_axis']) if layout['sample_axis'] in value['axes'] else None}
        elif isinstance(value, dict):
            for key, child in value.items():
                walk(child, path + '.' + key if path else key)
    walk(payload, '')
    if any(row['required'] and row['path'] not in seen for row in declarations.values()):
        raise ValueError('A required comparison payload array is missing')
    return {'ids': ids, 'conditions': conditions, 'target': target,
            'target_stride': len(target) // len(ids), 'target_shape': payload[layout['target']]['shape'][1:],
            'dataset_id': record['dataset_id'], 'splits': splits, 'shared': shared}


def _same_members(first, second):
    if (first['dataset_id'] != second['dataset_id'] or first['splits'] != second['splits']
            or set(first['ids']) != set(second['ids']) or first['target_shape'] != second['target_shape']):
        raise ValueError('Comparison records have different dataset, splits or sequence membership')
    a, b = first['target_stride'], second['target_stride']
    reference = {key: (condition, first['target'][i*a:(i+1)*a])
                 for i, (key, condition) in enumerate(zip(first['ids'], first['conditions']))}
    if any(reference[key] != (condition, second['target'][i*b:(i+1)*b])
           for i, (key, condition) in enumerate(zip(second['ids'], second['conditions']))):
        raise ValueError('Comparison records disagree on aligned conditions or target values')
    if set(first['shared']) != set(second['shared']):
        raise ValueError('Comparison records disagree on shared stratification availability')
    for key, left in first['shared'].items():
        right = second['shared'][key]
        if left['shape'] != right['shape'] or left['sequence_axis'] != right['sequence_axis']:
            raise ValueError('Comparison shared stratification shape differs')
        if left['sequence_axis'] is None:
            matches = left['bytes'] == right['bytes']
        elif left['sequence_axis'] == 0:
            stride = len(left['bytes']) // len(first['ids'])
            left_rows = {sid: left['bytes'][i*stride:(i+1)*stride] for i, sid in enumerate(first['ids'])}
            matches = all(left_rows[sid] == right['bytes'][i*stride:(i+1)*stride] for i, sid in enumerate(second['ids']))
        else:
            raise ValueError('Shared comparison arrays require sequence as their first axis or no sequence axis')
        if not matches:
            raise ValueError('Comparison records disagree on aligned shared stratification values')


def prepare_inputs(transport, params, trials, *, baseline_params=None):
    contract, plan, bindings = transport['contract'], transport['plan'], transport['bindings']
    validate_contract(contract, plan=plan)
    if 'comparison_inputs' in params:
        raise ValueError('Frozen scientific parameters already contain comparison_inputs')
    role = role_for(params, plan, contract)
    allowed = [p for _, p in validation_invocations(plan)]
    if params['phase'] == 'validation':
        if not any(canonical(params) == canonical(p) for p in allowed):
            raise ValueError('Comparison parameters are absent from the frozen plan')
        expected = [p for _, p in ordered_validation(plan, contract)[:-1]] if role == 'validation_finalize' else []
    else:
        if type(params['seed']) is not int or params['seed'] not in _final_seeds(plan):
            raise ValueError('Comparison final seed is not frozen')
        scientific = lambda p: {k: v for k, v in p.items() if k not in {'phase', 'seed'}}
        if not any(canonical(scientific(params)) == canonical(scientific(p)) for p in allowed):
            raise ValueError('Comparison final configuration is absent from the frozen plan')
        if role == 'test_finalize':
            if baseline_params is None:
                raise ValueError('Comparison final consumer needs the frozen baseline selection')
            expected = [{**baseline_params, 'phase': 'test', 'variant': 'baseline', 'seed': params['seed']}]
        else:
            expected = []
    records, reference = [], None
    for producer in expected:
        matches = [t for t in trials if not t.get('superseded') and t.get('status') == 'completed'
                   and t.get('comparison_transport', {}).get('bindings') == bindings
                   and canonical(scientific_params(t)) == canonical(producer)]
        if len(matches) != 1:
            raise ValueError('Comparison requires exactly one completed matching producer for each frozen invocation')
        trial = matches[0]
        evidence = trial.get('evaluation') or trial.get('experiment_metadata', {}).get('evaluation') or {}
        record = evidence.get('trial_evidence')
        checked = validate_record(record, bindings=bindings, params=producer, contract=contract)
        if reference is not None:
            _same_members(reference, checked)
        reference = checked
        records.append(record)
    full = {**params, 'comparison_inputs': {'protocol_version': 1, 'role': role, 'records': records}}
    if len(canonical(full)) > PARAMS_LIMIT:
        raise ValueError('Comparison parameter file exceeds 256 MiB')
    return full


def validate_result(transport, params, result):
    contract = validate_contract(transport['contract'], plan=transport['plan'],
                                 primary_metric=transport.get('primary_metric'))
    evidence = result.get('evaluation') or result.get('experiment_metadata', {}).get('evaluation')
    if not isinstance(evidence, dict) or canonical(evidence.get('actual_params')) != canonical(params):
        raise ValueError('Comparison result must echo the complete exact actual_params')
    wire = params.get('comparison_inputs')
    if not isinstance(wire, dict) or set(wire) != {'protocol_version', 'role', 'records'} or wire['protocol_version'] != 1:
        raise ValueError('Invalid comparison_inputs wire fields')
    role = wire['role']
    clean = {k: v for k, v in params.items() if k != 'comparison_inputs'}
    if role != role_for(clean, transport['plan'], transport['contract']):
        raise ValueError('Comparison input role differs from the frozen dependency plan')
    expected_count = len(validation_invocations(transport['plan'])) - 1 if role == 'validation_finalize' else 1 if role == 'test_finalize' else 0
    if not isinstance(wire['records'], list) or len(wire['records']) != expected_count:
        raise ValueError('Comparison input record count differs from its frozen role')
    expected_state = 'pending' if role == 'producer' else 'complete'
    if evidence.get('comparison_state') != expected_state:
        raise ValueError('Comparison producer must be pending; consumer must finish complete')
    own = validate_record(evidence.get('trial_evidence'), bindings=transport['bindings'], params=clean,
                          contract=transport['contract'])
    record = evidence['trial_evidence']
    if any(evidence.get(k) != record[k] for k in ('dataset_id', 'split_fingerprints', 'phase', 'seed')):
        raise ValueError('Comparison record differs from its enclosing evaluation identity')
    if (type(evidence.get('split_identity_protocol', 1)) is not int
            or evidence.get('split_identity_protocol', 1) != record.get('split_identity_protocol', 1)
            or record.get('split_identity_protocol') == 2
            and evidence.get('split_materialization') != record.get('split_materialization')):
        raise ValueError('Comparison record differs from its enclosing phase identity protocol')
    for prior in wire['records']:
        checked = validate_record(prior, bindings=transport['bindings'], params=prior['actual_params'], contract=transport['contract'])
        _same_members(own, checked)
    criteria = transport['plan'].get('acceptance', {}).get('criteria')
    applicable = phase_criteria(criteria or [], phase=clean['phase'], contract=contract,
                                primary_metric=transport.get('primary_metric'))
    if criteria is not None and role != 'producer':
        _validate_acceptance_inputs(evidence, applicable, clean['phase'])
    marker = {'protocol_version': 1, 'bindings': copy.deepcopy(transport['bindings']),
            'contract_sha256': digest(transport['contract']), 'input_sha256': digest(params),
            'role': role, 'comparison_state': expected_state,
            'record_hashes': [row['evidence_sha256'] for row in wire['records']]}
    if criteria is not None:
        marker['acceptance_scope'] = {'version': 1, 'contract': contract,
                                      'criteria_sha256': digest(criteria)}
    return marker


def scientific_params(trial):
    params = trial.get('params', {})
    marker = trial.get('comparison_transport')
    if (isinstance(params, dict) and isinstance(marker, dict) and marker.get('protocol_version') == 1
            and marker.get('input_sha256') == digest(params)
            and isinstance(marker.get('contract_sha256'), str) and HEX.fullmatch(marker['contract_sha256'])
            and isinstance(marker.get('bindings'), dict) and all(HEX.fullmatch(str(marker['bindings'].get(k, ''))) for k in BINDING_KEYS)):
        return {k: v for k, v in params.items() if k != 'comparison_inputs'}
    return params


def runtime_transport(engine, run_id, *, program=None, spec=None):
    """Resolve only a real, same-version approved specification; never infer prose."""
    if spec is not None and spec.get('comparison_transport') is None:
        return None
    program = program or engine.store.get('programs', run_id) or engine.outputs(run_id).get('implementation', {})
    if program.get('dataset_runtime_id'):
        from .codex_workflow import validate_program
        validate_program(engine, run_id, program)
        return None  # The new data adapter supplies its own reviewed evaluation contract.
    if spec is None and program.get('implementation_backend') != 'local_codex':
        return None
    workflow = engine.store.get('codex_workflows', program.get('delivery_id', ''))
    workflows = [workflow] if workflow else engine.store.list('codex_workflows', run_id)
    wanted = frozen_digest(spec) if spec is not None else program.get('spec_hash')
    matches = [(w, s) for w in workflows for s in w.get('specifications', [])
               if s.get('approved') is True and s.get('spec_hash') == wanted and frozen_digest(s.get('spec')) == wanted]
    if not matches:
        if spec is not None and spec.get('comparison_transport') is not None:
            raise ValueError('Comparison transport requires the approved same-version SPEC')
        return None
    if len(matches) != 1:
        raise ValueError('Ambiguous approved comparison specification')
    workflow, row = matches[0]
    plan = engine.experiment_plan(run_id)
    common = workflow['common']
    primary_metric = common.get('design', {}).get('primary_metric')
    contract = validate_contract(row['spec'].get('comparison_transport'), plan=plan, primary_metric=primary_metric)
    if contract is None:
        return None
    from .codex_workflow import _validate_approved_spec
    _validate_approved_spec(row, workflow['proposal_hash'])
    reviewed_definition = None
    if workflow.get('document_protocol') in (1, 2):
        from .implementation_documents import validate_review
        validate_review(engine, run_id, workflow, row)
        if row.get('implementation_definition_repair_protocol') == 1:
            reviewed_definition = row
    bindings = {'proposal_hash': workflow['proposal_hash'], 'design_hash': frozen_digest(common['design']),
                'plan_hash': frozen_digest(plan), 'spec_hash': row['spec_hash'],
                'implementation_document_sha256': row.get('document', {}).get('sha256')}
    if (workflow.get('run_id') != run_id or workflow.get('cycle') != engine.cycle(run_id)
            or any(not isinstance(v, str) or not HEX.fullmatch(v) for v in bindings.values())
            or frozen_digest(common.get('experiment_plan')) != bindings['plan_hash']):
        raise ValueError('Comparison transport frozen plan or document binding changed')
    from .comparison_protocol_repair import apply
    return apply(engine, run_id, workflow,
        {'contract': contract, 'plan': plan, 'bindings': bindings, 'primary_metric': primary_metric},
        reviewed_definition=reviewed_definition)


IO_POLICY_VERSION = 2


def io_limits(limits, *, params_size=None):
    """Reserve host JSON overhead within existing admitted RAM/disk, never raise them."""
    # Conservative bounded allowance for input/result objects, encoders and decoded
    # records. The child receives only the remaining originally admitted memory.
    if params_size is None:
        params_size = PARAMS_LIMIT
    if type(params_size) is not int or not 0 <= params_size <= PARAMS_LIMIT:
        raise ValueError('Comparison parameters exceed their archive capacity')
    # Output is spooled to disk while the child is alive. Full decoding begins
    # only after process.wait() and closing the job/descendants. Reserve each
    # phase separately instead of charging a decoded 320 MiB JSON tree to the
    # child that must already have exited before that tree can exist.
    reserve = 8 * params_size + 2 * RECORD_LIMIT + 8 * MIB
    decode_reserve = 8 * (params_size + OUTPUT_LIMIT) + 2 * RECORD_LIMIT
    if (int(limits['memory_mb']) * MIB <= reserve + 64 * MIB
            or int(limits['memory_mb']) * MIB < decode_reserve):
        raise ValueError('Admitted RAM cannot accommodate bounded comparison transport plus the program')
    if int(limits['disk_mb']) * MIB < PARAMS_LIMIT + OUTPUT_LIMIT + 2 * MIB:
        raise ValueError('Admitted disk cannot accommodate bounded comparison input and output archives')
    return {'policy_version': IO_POLICY_VERSION, 'host_memory_reserve_bytes': reserve,
            'post_exit_decode_reserve_bytes': decode_reserve, 'actual_params_bytes': params_size,
            'params_bytes': PARAMS_LIMIT,
            'output_bytes': OUTPUT_LIMIT, 'child_memory_mb': int(limits['memory_mb']) - math.ceil(reserve / MIB)}


def read_final_output(path, maximum=OUTPUT_LIMIT):
    """Parse the complete last nonempty UTF-8 line from a bounded disk spool."""
    path = Path(path)
    if path.stat().st_size > maximum:
        raise ValueError('Comparison output exceeds its archive capacity')
    last = b''
    with path.open('rb') as stream:
        for line in stream:
            if line.strip():
                last = line
    return strict_loads(last)


def acceptance_value(evidence, metric, criterion):
    """Check supplied arithmetic consistency; do not recompute scientific statistics."""
    values = evidence.get('acceptance_inputs') if isinstance(evidence, dict) else None
    row = values.get(metric) if isinstance(values, dict) else None
    if not isinstance(row, dict) or set(row) != {'reference', 'candidate', 'relative_improvement'}:
        raise ValueError('Required program-reported acceptance_inputs missing: ' + metric)
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in row.values()) or row['reference'] == 0:
        raise ValueError('Acceptance inputs must be finite with a nonzero reference: ' + metric)
    difference = row['reference'] - row['candidate'] if criterion['direction'] == 'minimize' else row['candidate'] - row['reference']
    expected = difference / abs(row['reference'])
    if not math.isfinite(expected) or not math.isclose(expected, row['relative_improvement'], rel_tol=1e-12, abs_tol=1e-12):
        raise ValueError('Acceptance relative_improvement disagrees with its supplied reference/candidate: ' + metric)
    return row


def _validate_acceptance_inputs(evidence, criteria, phase):
    values = evidence.get('acceptance_inputs') if isinstance(evidence, dict) else None
    expected = {acceptance_metric(criterion, phase) for criterion in criteria}
    if not isinstance(values, dict) or set(values) != expected:
        raise ValueError('Program-reported acceptance_inputs must contain exactly the applicable ' + phase + ' metrics')
    for criterion in criteria:
        acceptance_value(evidence, acceptance_metric(criterion, phase), criterion)


def reported_outcome(trials, criteria, *, phase, complete, expected_seeds):
    """Consume one validated consumer's scientific result, without raw-MSE recomputation."""
    transported = [t for t in trials if isinstance(t, dict) and t.get('params', {}).get('phase') == phase
                   and t.get('status') == 'completed' and not t.get('superseded')
                   and ('comparison_transport' in t or 'comparison_inputs' in t.get('params', {}))]
    if not transported:
        return None
    marked = [t for t in transported if scientific_params(t) is not t.get('params')]
    scope_issues, contract = [], None
    if len(marked) != len(transported):
        scope_issues.append('Invalid host comparison marker; raw-metric fallback is prohibited')
    contracts = {t['comparison_transport']['contract_sha256'] for t in marked}
    if len(contracts) != 1:
        scope_issues.append('Mixed or missing comparison contract bindings')
    scopes = [t['comparison_transport'].get('acceptance_scope') for t in marked]
    if any(scope is not None for scope in scopes):
        try:
            if any(not isinstance(scope, dict) or set(scope) != {'version', 'contract', 'criteria_sha256'}
                   or type(scope['version']) is not int or scope['version'] != 1
                   or scope['criteria_sha256'] != digest(criteria) for scope in scopes):
                raise ValueError('Acceptance phase scope differs from the frozen criteria or host marker')
            for trial, scope in zip(marked, scopes):
                candidate = validate_contract(scope['contract'])
                if candidate is None or digest(candidate) != trial['comparison_transport']['contract_sha256']:
                    raise ValueError('Acceptance phase scope differs from the validated contract fingerprint')
                phase_criteria(criteria, phase=phase, contract=candidate)
            if not scope_issues:
                contract = scopes[0]['contract']
        except (ValueError, TypeError, KeyError) as exc:
            scope_issues.append(str(exc))
    applicable = phase_criteria(criteria, phase=phase, contract=contract)
    role = 'validation_finalize' if phase == 'validation' else 'test_finalize'
    consumers = [t for t in marked if t['comparison_transport']['role'] == role
                 and t['comparison_transport']['comparison_state'] == 'complete']
    expected = set(expected_seeds or [])
    if not expected:
        expected = {t['params']['seed'] for t in marked if t['params']['variant'] in ('baseline', 'proposed')}
    bindings = {canonical(t['comparison_transport']['bindings']) for t in marked}
    pairs = [(t['params']['variant'], t['params']['seed']) for t in marked if t['params']['variant'] in ('baseline', 'proposed')]
    coverage = (not scope_issues and bool(expected) and len(bindings) == 1 and len(pairs) == len(set(pairs))
                and set(pairs) == {(variant, seed) for variant in ('baseline', 'proposed') for seed in expected}
                and len(consumers) == 1)
    if coverage:
        try:
            _validate_acceptance_inputs(consumers[0].get('evaluation') or consumers[0].get('experiment_metadata', {}).get('evaluation'), applicable, phase)
        except ValueError as exc:
            scope_issues.append(str(exc))
            coverage = False
    checks = []
    for criterion in applicable:
        metric = acceptance_metric(criterion, phase)
        row, issues = None, []
        if coverage:
            try:
                row = acceptance_value(consumers[0].get('evaluation') or consumers[0].get('experiment_metadata', {}).get('evaluation'), metric, criterion)
            except ValueError as exc:
                issues.append(str(exc))
        else:
            issues.extend(scope_issues or ['Incomplete, duplicated or mixed-version comparison producers/consumer'])
        relative = row['relative_improvement'] if row else None
        threshold = criterion['minimum_relative_improvement']
        passed = (relative > threshold if criterion.get('comparison') == 'greater_than' else relative >= threshold) if row else None
        checks.append({**criterion, 'metric': metric, 'relative_improvement': relative, 'passed': passed,
            'complete_seed_coverage': coverage, 'expected_pairs': len(expected), 'n_pairs': len(expected) if coverage else 0,
            'baseline_mean': None, 'proposed_mean': None, 'comparison_id': None,
            'acceptance_inputs': row, 'source': 'program_reported_comparison', 'issues': issues})
    from .performance_policy import outcome
    status = outcome(checks, complete)
    applicable_metrics = {criterion['metric'] for criterion in applicable}
    return {'research_outcome': status, 'phase': phase, 'checks': checks, 'criteria_complete': complete,
            'acceptance_phases': copy.deepcopy((contract or {}).get('acceptance_phases')),
            'excluded_criteria': copy.deepcopy([criterion for criterion in criteria if criterion['metric'] not in applicable_metrics]),
            'scope': 'Checks of frozen thresholds against the approved program reported comparison; transport integrity is not independent scientific verification.',
            'test_reuse_for_tuning_allowed': False}
