"""Authenticate and align derived review requests with their immutable materials.

This adapter changes no frozen input, paid request, read ledger or opinion.
Native successors carry an explicit, independently reproducible v1 audit.
"""
from __future__ import annotations

import copy

from . import independent_full_review as full

AUDIT = 'full_review_material_contract_repair'
VERSION = 1


def _frozen(engine, run_id, role, key, prepared):
    from .engine import Harness, digest
    hand = engine.store.get('handoffs', key, {})
    frozen = engine.store.get('handoff_inputs', key, {})
    context = frozen.get('context', {})
    view = prepared.get('context', {})
    if full.KEY not in context and full.KEY not in view:
        return None
    payload = {k: frozen.get(k) for k in ('instruction', 'context', 'schema')}
    if (role not in full.REVIEWERS or prepared.get('role') != role
            or hand.get('id') != key or hand.get('run_id') != run_id or hand.get('to_role') != role
            or frozen.get('id') != key or frozen.get('run_id') != run_id
            or not isinstance(hand.get('purpose'), str)
            or hand.get('input_hash') != digest(payload)
            or Harness.decision_key(run_id, role, hand['purpose'], **payload) != key
            or full.KEY not in context or view.get(full.KEY) != context[full.KEY]):
        raise ValueError('full_review_material_contract_invalid: exact frozen handoff/role/policy changed')
    full.validate_binding(context[full.KEY])
    if type(context[full.KEY]['version']) is not int:
        raise ValueError('full_review_material_contract_invalid: unknown policy version')
    return frozen


def project(engine, run_id, role, key, prepared):
    """Set the host index before the ordinary typed-schema compiler runs."""
    frozen = _frozen(engine, run_id, role, key, prepared)
    if frozen is None:
        return prepared
    from . import implementation_definition_review_scope as definitions
    if definitions.enabled(frozen['context']) or definitions.citation(frozen['context']):
        return prepared
    result = copy.deepcopy(prepared)
    result['context']['review_material'] = full.project(frozen['context'], role=role)['review_material']
    return result


def _schema(schema, context):
    """Replace only existing owned material rules; retain all other permissions.

    Unlike recompiling an old paid schema against today's entire tool registry,
    this deterministic adapter cannot refresh unrelated tool contracts. Local
    $defs and exact call enums retain their original scope and bytes.
    """
    from .tool_contracts import _conditions, _CONDITION_MARK
    replacement = _conditions([full.TOOL], context)[0]
    prefix = _CONDITION_MARK + full.TOOL + ':'
    result = copy.deepcopy(schema)

    def resolve(node, resource):
        from urllib.parse import unquote
        seen = set()
        while isinstance(node, dict) and '$ref' in node:
            if '$id' in node:
                resource = node
            ref = node['$ref']
            if not isinstance(ref, str) or not ref.startswith('#/') or (id(resource), ref) in seen:
                raise ValueError('full_review_material_contract_invalid: unsupported/cyclic tool reference')
            seen.add((id(resource), ref))
            target = resource
            try:
                for token in unquote(ref[2:]).split('/'):
                    target = target[token.replace('~1', '/').replace('~0', '~')]
            except (KeyError, TypeError):
                raise ValueError('full_review_material_contract_invalid: unresolved tool reference') from None
            node = target
        return node

    def visit(node, resource):
        if isinstance(node, list):
            for child in node:
                visit(child, resource)
        elif isinstance(node, dict):
            if '$id' in node:
                resource = node
            # Exact calls are permissions, not a schema to broaden. An old
            # conflicting exact call needs its own explicit recovery contract.
            calls = node.get('properties', {}).get('tool_calls', {})
            items = calls.get('items', {}) if isinstance(calls, dict) else {}
            items = resolve(items, items if isinstance(items, dict) and '$id' in items else resource)
            for call in items.get('enum', []) if isinstance(items, dict) else []:
                if isinstance(call, dict) and call.get('name') == full.TOOL:
                    from .tool_contracts import validate_arguments
                    validate_arguments(full.TOOL, call.get('arguments'), context)
            if str(node.get('$comment', '')).startswith(prefix):
                if node.get('if') != replacement['if'] or set(node) != set(replacement):
                    raise ValueError('full_review_material_contract_invalid: unsupported material condition')
                node.clear()
                node.update(copy.deepcopy(replacement))
                return
            for name, child in node.items():
                if name not in {'enum', 'const', 'default', 'examples'}:
                    visit(child, resource)

    visit(result, result)
    return result


def successor(engine, run_id, key, previous, operation):
    """Derive a new request only after the caller's normal recovery gates."""
    from .engine import digest
    result = copy.deepcopy(previous['prepared'])
    control = result['context'].get('_harness_control', {})
    result['context']['_harness_control'] = {**control, 'operation_id': operation}
    role = result['role']
    frozen = _frozen(engine, run_id, role, key, result)
    if frozen is None:
        return result, None
    canonical = project(engine, run_id, role, key, result)
    if 'review_material' not in canonical['context']:
        return canonical, None
    canonical['schema'] = _schema(result['schema'], canonical['context'])
    if canonical == result:
        return result, None
    return canonical, {'version': VERSION, 'handoff_id': key, 'run_id': run_id, 'role': role,
        'frozen_input_sha256': digest({k: frozen[k] for k in ('instruction', 'context', 'schema')}),
        'policy_sha256': frozen['context'][full.KEY]['sha256'],
        'material_index_sha256': digest(canonical['context']['review_material']),
        'original_prepared_sha256': digest(previous['prepared']),
        'derived_prepared_sha256': digest(canonical)}


def replay(engine, run_id, key, previous, operation, marker):
    if not isinstance(marker, dict) or type(marker.get('version')) is not int or marker['version'] != VERSION:
        raise ValueError('full_review_material_contract_invalid: unknown repair version')
    expected, audit = successor(engine, run_id, key, previous, operation)
    if audit is None or audit != marker:
        raise ValueError('full_review_material_contract_invalid: derived repair audit changed')
    return expected
