"""Read-only authorization against the exact owner of a tool invocation.

Role capabilities remain a separate outer gate. This module never adds a role
catalog to a frozen request, upgrades an older request's argument contract, or
creates a controller task on behalf of a model-requested invocation.
"""
from __future__ import annotations

import copy
import json
from functools import lru_cache

from jsonschema import Draft202012Validator, SchemaError


class ToolAuthorizationError(ValueError):
    """No authenticated frozen task contract authorizes this invocation."""


def _fail(message):
    raise ToolAuthorizationError(message)


def _digest(value):
    # Keep the same canonical encoding used for persisted handoff input hashes.
    from .engine import digest
    return digest(value)


def _version(record):
    value = record.get('tool_contract_version', 0)
    if type(value) is not int or value not in (0, 1):
        _fail('Tool owner has an unsupported frozen contract version')
    return value


def _handoff(store, run_id, role, owner):
    assignment = store.get('handoffs', owner)
    frozen = store.get('handoff_inputs', owner)
    if (not isinstance(assignment, dict) or not isinstance(frozen, dict)
            or assignment.get('id') != owner or frozen.get('id') != owner
            or assignment.get('run_id') != run_id or frozen.get('run_id') != run_id
            or assignment.get('to_role') != role):
        _fail('Tool owner must be this run and role\'s exact frozen handoff')
    if (not isinstance(assignment.get('purpose'), str) or not assignment['purpose']
            or not isinstance(frozen.get('instruction'), str)
            or not isinstance(frozen.get('context'), dict)
            or not isinstance(frozen.get('schema'), dict)):
        _fail('Tool owner has incomplete frozen handoff inputs')
    if assignment.get('status') not in {'queued', 'running', 'blocked', 'interrupted'}:
        _fail('Completed or inactive handoff does not authorize new tool calls')
    try:
        actual = _digest({key: frozen[key] for key in ('instruction', 'context', 'schema')})
    except (TypeError, ValueError):
        _fail('Tool owner frozen inputs cannot be authenticated')
    if assignment.get('input_hash') != actual:
        _fail('Tool owner frozen handoff input hash changed')
    version = _version(frozen)
    if _version(assignment) != version:
        _fail('Tool owner handoff contract versions do not match')
    return assignment, frozen, version


def _progress(store, run_id, owner):
    value = store.get('handoff_progress', owner)
    if value is None:
        return {}
    if (not isinstance(value, dict) or value.get('id', owner) != owner
            or value.get('run_id', run_id) != run_id):
        _fail('Tool progress belongs to a different frozen handoff')
    if value.get('mode') == 'final_only':
        _fail('Final-only handoff does not authorize tool calls')
    return value


def _controller(store, run_id, role, owner, record, seen):
    if owner in seen or len(seen) >= 64:
        _fail('Controller tool owner parent chain contains a cycle')
    seen = {*seen, owner}
    if (not isinstance(record, dict) or record.get('id') != owner
            or record.get('run_id') != run_id or record.get('role') != role
            or record.get('authority') != 'controller' or _version(record) != 1
            or not isinstance(record.get('purpose'), str) or not record['purpose']
            or not isinstance(record.get('schema'), dict)
            or not isinstance(record.get('context'), dict)):
        _fail('Controller tool owner identity or frozen authority is invalid')
    try:
        actual = _digest({key: value for key, value in record.items() if key != 'contract_sha256'})
    except (TypeError, ValueError):
        _fail('Controller tool contract cannot be authenticated')
    if record.get('contract_sha256') != actual:
        _fail('Controller tool frozen contract hash changed')
    control = record['context'].get('_harness_control', {})
    if not isinstance(control, dict):
        _fail('Controller tool owner controller state is invalid')
    if control.get('mode') == 'final_only':
        _fail('Final-only controller task does not authorize tool calls')
    parent = record.get('parent_handoff_id')
    if parent is not None:
        if not isinstance(parent, str) or not parent:
            _fail('Controller tool parent identity is invalid')
        parent_record = store.get('tool_tasks', parent)
        if parent_record is not None:
            _controller(store, run_id, role, parent, parent_record, seen)
        else:
            _, frozen, _ = _handoff(store, run_id, role, parent)
            _progress(store, run_id, parent)
            control = frozen['context'].get('_harness_control', {})
            if not isinstance(control, dict) or control.get('mode') == 'final_only':
                _fail('Controller parent frozen state does not authorize tool calls')
    return record


def _request(assignment, frozen, progress, version):
    frozen_control = frozen['context'].get('_harness_control', {})
    if not isinstance(frozen_control, dict):
        _fail('Frozen handoff controller state is invalid')
    if frozen_control.get('mode') == 'final_only':
        _fail('Final-only handoff inputs do not authorize tool calls')
    turn = progress.get('turn', 0)
    if type(turn) is not int or turn < 0:
        _fail('Tool owner has an invalid frozen request turn')
    for slot in ('normal_request', 'tool_request'):
        request = progress.get(slot)
        if request is None:
            continue
        if not isinstance(request, dict):
            _fail('Tool owner request checkpoint is invalid')
        if request.get('turn') != turn:
            continue
        if 'prepared' not in request:
            continue
        prepared = request['prepared']
        if (not isinstance(prepared, dict) or prepared.get('role') != assignment['to_role']
                or not isinstance(prepared.get('context'), dict)
                or prepared['context'].get('_purpose') != assignment['purpose']
                or not isinstance(prepared.get('schema'), dict)):
            _fail('Tool prepared request identity does not match its frozen handoff')
        marker = prepared['context'].get('_tool_contract_version', 0)
        if type(marker) is not int or marker != version:
            _fail('Tool prepared request contract version changed')
        if version == 1:
            try:
                actual = _digest(prepared)
            except (TypeError, ValueError):
                _fail('Tool prepared request cannot be authenticated')
            expected = request.get('prepared_sha256')
            if expected is None and slot == 'normal_request':
                checkpoint = progress.get('tool_request')
                if (isinstance(checkpoint, dict) and checkpoint.get('turn') == turn
                        and checkpoint.get('prepared') == prepared):
                    expected = checkpoint.get('prepared_sha256')
            if expected != actual:
                _fail('Tool prepared request frozen hash is missing or changed')
        control = prepared['context'].get('_harness_control', {})
        if not isinstance(control, dict):
            _fail('Tool prepared request controller state is invalid')
        if control.get('mode') == 'final_only':
            _fail('Final-only request does not authorize tool calls')
        return prepared, slot
    # An explicit tool-only assignment is already a frozen tool contract. Never
    # wrap it in prepare(), which would otherwise append ordinary role tools.
    if _project(frozen['schema'], items=False) is not None:
        return {'role': assignment['to_role'], 'context': frozen['context'],
                'schema': frozen['schema']}, 'handoff_tool_contract'
    if version != 0:
        _fail('Current handoff has no frozen prepared tool request')
    return None, 'legacy_frozen_inputs'


def _legacy(harness, assignment, frozen, progress):
    results = progress.get('tool_results', [])
    if not isinstance(results, list):
        _fail('Legacy tool receipts are invalid')
    results = copy.deepcopy(results)
    pending = progress.get('pending_tool_calls', [])
    if not isinstance(pending, list):
        _fail('Legacy pending tool batch is invalid')
    if pending:
        cursor = progress.get('tool_cursor', 0)
        if type(cursor) is not int or not 0 <= cursor <= len(pending):
            _fail('Legacy pending tool cursor is invalid')
        # Reconstruct the request that produced this batch, not a continuation
        # influenced by the batch's own already-drained reads or writes.
        if cursor:
            if (len(results) < cursor or any(not isinstance(row, dict) for row in results[-cursor:])
                    or [row.get('call') for row in results[-cursor:]] != pending[:cursor]):
                _fail('Legacy batch receipts do not match its frozen tool cursor')
            results = results[:-cursor]
    try:
        return harness.prepare(assignment['to_role'], assignment['purpose'], frozen['instruction'],
                               copy.deepcopy(frozen['context']), copy.deepcopy(frozen['schema']),
                               results, tool_contract_version=0)
    except (TypeError, ValueError, KeyError):
        _fail('Legacy frozen handoff tool contract could not be reconstructed')


def _combine(parts, operator='allOf'):
    parts = [part for part in parts if part is not None]
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else {operator: parts}


def _project(schema, *, items):
    """Project only explicit top-level tool branches, excluding final JSON.

    The complete frozen item is retained, including exact-call enums and
    conditional argument schemas. Final-output branches that happen to accept
    arbitrary objects must never become tool authorization.
    """
    if not isinstance(schema, dict):
        return None
    parts = []
    properties = schema.get('properties', {})
    if isinstance(properties, dict) and 'tool_calls' in properties:
        array = properties['tool_calls']
        if not isinstance(array, dict) or array.get('type') != 'array' or 'items' not in array:
            _fail('Frozen tool_calls contract must contain a bounded item schema')
        if 'prefixItems' in array:
            _fail('Frozen tool_calls tuple contracts require an explicit positional owner')
        item = array['items']
        if not isinstance(item, dict):
            _fail('Frozen tool_calls item contract is invalid')
        parts.append(copy.deepcopy(item if items else array))
    for operator in ('anyOf', 'oneOf', 'allOf'):
        if operator not in schema:
            continue
        branches = schema[operator]
        if not isinstance(branches, list):
            _fail('Frozen tool request schema branches are invalid')
        projected = [_project(branch, items=items) for branch in branches]
        # One call may match multiple array branches with different batch
        # bounds. Per-item authorization is a union; batch validation retains
        # the actual oneOf semantics below.
        effective = 'anyOf' if operator == 'oneOf' and items else operator
        parts.append(_combine(projected, effective))
    projected = _combine(parts)
    if projected is not None:
        for key in ('$defs', 'definitions'):
            if key in schema:
                projected = {**projected, key: copy.deepcopy(schema[key])}
    return projected


def _reference(schema, root, visiting):
    reference = schema.get('$ref')
    if reference is None:
        return None, visiting
    if not isinstance(reference, str) or not reference.startswith('#/') or reference in visiting:
        _fail('Frozen tool contract has an unsupported or cyclic schema reference')
    current = root
    for part in reference[2:].split('/'):
        key = part.replace('~1', '/').replace('~0', '~')
        if not isinstance(current, dict) or key not in current:
            _fail('Frozen tool contract schema reference is missing')
        current = current[key]
    if not isinstance(current, dict):
        _fail('Frozen tool contract schema reference is invalid')
    return current, {*visiting, reference}


def _names(schema, root, visiting=frozenset(), *, value=False):
    """Find a finite upper bound on names admitted by the frozen item schema."""
    if not isinstance(schema, dict):
        return None
    limits = []
    if 'enum' in schema:
        options = schema['enum']
        if not isinstance(options, list):
            _fail('Frozen tool name enum is invalid')
        names = set()
        for option in options:
            name = option if value else option.get('name') if isinstance(option, dict) else None
            if not isinstance(name, str) or not name:
                _fail('Frozen tool name enum is invalid')
            names.add(name)
        limits.append(names)
    if 'const' in schema:
        option = schema['const']
        name = option if value else option.get('name') if isinstance(option, dict) else None
        if not isinstance(name, str) or not name:
            _fail('Frozen tool name constant is invalid')
        limits.append({name})
    if not value:
        properties = schema.get('properties', {})
        if isinstance(properties, dict) and 'name' in properties:
            names = _names(properties['name'], root, visiting, value=True)
            if names is not None:
                limits.append(names)
    referenced, next_visiting = _reference(schema, root, visiting)
    if referenced is not None:
        names = _names(referenced, root, next_visiting, value=value)
        if names is not None:
            limits.append(names)
    for operator in ('allOf', 'anyOf', 'oneOf'):
        if operator not in schema:
            continue
        branches = schema[operator]
        if not isinstance(branches, list):
            _fail('Frozen tool item schema branches are invalid')
        branch_limits = [_names(branch, root, visiting, value=value) for branch in branches]
        if operator == 'allOf':
            bounded = [names for names in branch_limits if names is not None]
            if bounded:
                limits.append(set.intersection(*bounded))
        elif all(names is not None for names in branch_limits):
            limits.append(set().union(*branch_limits))
    return set.intersection(*limits) if limits else None


def _local_references(schema):
    """Validation must not resolve remote schema URLs or inspect enum data."""
    if not isinstance(schema, dict):
        return
    for keyword in ('$ref', '$dynamicRef'):
        if keyword in schema:
            reference = schema[keyword]
            if not isinstance(reference, str) or not reference.startswith('#'):
                _fail('Frozen tool contract cannot reference an external schema')
    for keyword in ('$defs', 'definitions', 'properties', 'patternProperties', 'dependentSchemas'):
        values = schema.get(keyword, {})
        if isinstance(values, dict):
            for child in values.values():
                _local_references(child)
    for keyword in ('anyOf', 'oneOf', 'allOf', 'prefixItems'):
        values = schema.get(keyword, [])
        if isinstance(values, list):
            for child in values:
                _local_references(child)
    for keyword in ('items', 'additionalItems', 'contains', 'additionalProperties',
                    'unevaluatedProperties', 'unevaluatedItems', 'propertyNames', 'if', 'then', 'else', 'not'):
        _local_references(schema.get(keyword))


def _supported_projection(schema, *, required=False):
    """Reject output-level constraints whose tool effect cannot be preserved.

    Item-level conditionals remain intact. A final-only alternative need not
    be projected, but every constraint-only allOf branch of a tool alternative
    must be supported; silently dropping it could widen the frozen scope.
    """
    if not isinstance(schema, dict):
        if required and schema is False:
            _fail('Frozen tool request contains an unsupported output constraint')
        return
    active = required or _project(schema, items=False) is not None
    if not active:
        return
    unsupported = {'not', 'if', 'then', 'else', 'dependentSchemas', 'dependentRequired',
                   'patternProperties', '$ref', '$dynamicRef', 'unevaluatedProperties',
                   'propertyNames', 'minProperties', 'maxProperties', 'const', 'enum'}
    if unsupported.intersection(schema):
        _fail('Frozen tool request contains unsupported output-level tool constraints')
    if 'type' in schema and schema['type'] != 'object':
        _fail('Frozen tool request contains an unsupported output type constraint')
    if set(schema.get('required', [])) - {'tool_calls', 'working_notes'}:
        _fail('Frozen tool request contains unsupported output field constraints')
    if schema.get('additionalProperties', True) is not True and 'tool_calls' not in schema.get('properties', {}):
        _fail('Frozen tool request contains unsupported output property constraints')
    if 'oneOf' in schema and any(_project(branch, items=False) is None for branch in schema['oneOf']):
        _fail('Frozen exclusive output alternatives cannot discard final-only branches')
    for branch in schema.get('allOf', []):
        _supported_projection(branch, required=True)
    # A tool-bearing alternative already identifies which disjuncts can
    # authorize tools. An unrelated final-only disjunct stays excluded even
    # when its parent is an allOf branch. Constraint-only disjunctions of an
    # allOf branch, by contrast, must not silently lose restrictive disjuncts.
    branch_required = required and _project(schema, items=False) is None
    for operator in ('anyOf', 'oneOf'):
        for branch in schema.get(operator, []):
            _supported_projection(branch, required=branch_required)


@lru_cache(maxsize=128)
def _compiled(encoded, version):
    """Cache only immutable schema-derived validators, never owner decisions."""
    schema = json.loads(encoded)
    from .prose_schema import relax
    schema = relax(schema)
    Draft202012Validator.check_schema(schema)
    _local_references(schema)
    _supported_projection(schema)
    item = _project(schema, items=True)
    if item is None:
        _fail('Frozen request does not authorize any tool_calls')
    names = _names(item, item)
    if names is None:
        _fail('Frozen tool contract must contain a finite authorized tool scope')
    validator = Draft202012Validator
    if version == 1:
        # Apply strict runtime value types to the already-frozen item schema.
        # Do not fetch today's registry argument schema for an older packet.
        from .tool_contracts import StrictToolValidator
        validator = StrictToolValidator
    return frozenset(names), validator(item), validator(_project(schema, items=False))


def _validate(schema, name, arguments, pending=None, *, version=0):
    try:
        encoded = json.dumps(schema, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False)
        names, item_validator, batch_validator = _compiled(encoded, version)
    except ToolAuthorizationError:
        raise
    except (SchemaError, TypeError, ValueError):
        _fail('Frozen tool request has an invalid JSON schema')
    if name not in names:
        _fail('Tool is outside this assignment\'s frozen tool scope')
    call = {'name': name, 'arguments': arguments}
    try:
        valid = (item_validator.is_valid(call)
                 and batch_validator.is_valid(pending if pending else [call]))
    except Exception:
        # Schema-reference errors must fail closed without exposing source data
        # or argument values in a provider-visible diagnostic.
        _fail('Frozen tool contract could not be validated')
    if not valid:
        _fail('Tool call arguments or batch do not match this assignment\'s frozen contract')
    if version == 1:
        from .tool_contracts import validate_value_semantics
        try:
            validate_value_semantics(name, arguments)
        except ValueError:
            _fail('Tool call values do not match the frozen contract\'s execution semantics')


def authorize(harness, run_id, role, owner, name, arguments):
    """Authenticate the owner and validate a call without journal mutations.

    A current durable normal request takes priority over the scripted request
    checkpoint. Only truly legacy handoffs may reconstruct a missing prepared
    packet, using their original frozen inputs and the legacy contract version.
    Controller tasks carry their own exact, content-bound contract; ancestry
    authenticates identity and never contributes additional tools.
    """
    if (not isinstance(owner, str) or not owner or not isinstance(run_id, str) or not run_id
            or not isinstance(role, str) or not role):
        _fail('A frozen handoff or controller tool owner is required')
    if not isinstance(name, str) or not name or not isinstance(arguments, dict):
        _fail('Tool invocation requires a name and an arguments object')
    store = harness.engine.store
    controller = store.get('tool_tasks', owner)
    if controller is not None:
        record = _controller(store, run_id, role, owner, controller, set())
        schema, context, version = record['schema'], record['context'], 1
        source, pending, purpose = 'controller_tool_task', None, record['purpose']
    else:
        assignment, frozen, version = _handoff(store, run_id, role, owner)
        progress = _progress(store, run_id, owner)
        prepared, source = _request(assignment, frozen, progress, version)
        if prepared is None:
            prepared = _legacy(harness, assignment, frozen, progress)
        schema, context, purpose = prepared['schema'], prepared['context'], assignment['purpose']
        pending = progress.get('pending_tool_calls')
        if pending is not None and not isinstance(pending, list):
            _fail('Frozen pending tool batch is invalid')
    _validate(schema, name, arguments, pending, version=version)
    return {'handoff_id': owner, 'run_id': run_id, 'role': role, 'purpose': purpose,
            'source': source, 'tool_contract_version': version, 'context': copy.deepcopy(context)}
