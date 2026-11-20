"""Room for complete frozen per-objection output, without changing paid inputs."""
from __future__ import annotations

MAX_OUTPUT = 48000
TOKENS_PER_ITEM = 600
STAGES = {'implementation_spec', 'implementation_document_review'}
PROTOCOL = 'implementation_inventory_output_protocol'


def inventory_enabled(context):
    from .implementation_protocol_contracts import author_protocols
    return PROTOCOL in author_protocols(context)


def admit(context, effective_output):
    """Do not dispatch an inventory under a silently reduced output allowance."""
    if not requires_capacity(context):
        return
    required = output_limit(context, context.get('_purpose', ''), 0)
    if effective_output < required:
        from .engine import ResearchBlocked
        error = ResearchBlocked('完整冻结问题清单的输出容量不足；未派发请求，不能截断或删除义务。')
        error.implementation_output_capacity = {'version': 1, 'required_tokens': required,
            'effective_tokens': effective_output, 'action': 'capacity_admission_required',
            'scientific_approval': False}
        raise error


def admit_batch(requests, report):
    if any(requires_capacity(r.get('context', {})) for r in requests) and len(report.get('requests', [])) != len(requests):
        raise ValueError('Incomplete output inventory admission report')
    for request, admitted in zip(requests, report.get('requests', [])):
        if not admitted.get('cache_hit') and not admitted.get('native_readback'):
            admit(request.get('context', {}), admitted['max_output_tokens'])


def requires_capacity(context):
    from .implementation_whole_review import request_enabled
    if request_enabled(context):
        return True
    from .implementation_report_amendments import KEY as AMENDMENT
    from .implementation_verification_contract import FIELD, SCOPE
    return AMENDMENT in context or inventory_enabled(context) or FIELD in context.get(SCOPE, {})


def insufficient(schema, field, count):
    """Explicit legacy successor trigger; never rewrite the frozen packet."""
    maximum = schema.get('properties', {}).get(field, {}).get('maxItems')
    return type(maximum) is int and count > maximum


def inventory_array(item, inventory, *, key='issue_id', conditions=False):
    """Exact finite obligations, not a larger arbitrary array allowance."""
    import copy
    ids = [row[key] for row in inventory]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate frozen output obligation')
    variant = copy.deepcopy(item)
    variant['properties'][key] = {'enum': ids}
    if conditions and ids:
        groups = {}
        for row in inventory:
            groups.setdefault(len(row['close_when']), []).append(row[key])
        variant['properties']['conditions'].update(minItems=min(groups), maxItems=max(groups))
        # Reuse the prose/evidence schema once; group only the finite index
        # constraints, avoiding an entire schema copy for every issue.
        variant['allOf'] = [{'if': {'properties': {key: {'enum': members}}, 'required': [key]},
            'then': {'properties': {'conditions': {'minItems': count, 'maxItems': count,
                'items': {'properties': {'index': {'enum': list(range(count))}}},
                'allOf': [{'contains': {'type': 'object', 'required': ['index'],
                    'properties': {'index': {'const': index}}}, 'minContains': 1, 'maxContains': 1}
                    for index in range(count)]}}}} for count, members in groups.items()]
    result = {'type': 'array', 'minItems': len(ids), 'maxItems': len(ids),
              'items': variant if ids else False}
    # uniqueItems alone cannot reject the same ID with different prose.
    if ids:
        result['allOf'] = [{'contains': {'type': 'object', 'required': [key],
            'properties': {key: {'const': iid}}}, 'minContains': 1, 'maxContains': 1} for iid in ids]
    return result


def author_schema(schema, context):
    import copy
    if not inventory_enabled(context):
        return schema  # Frozen legacy schema remains its own contract.
    result = copy.deepcopy(schema)
    inventory = context['acceptance_issues']
    for name in ('patch_reports', 'acceptance_completion'):
        if name in result['properties']:
            result['properties'][name] = {**result['properties'][name], **inventory_array(
                result['properties'][name]['items'], inventory, conditions=name == 'acceptance_completion')}
    return result


def review_schema(schema, context, role):
    import copy
    from .implementation_acceptance import CONTEXT, VERIFY
    result = copy.deepcopy(schema)
    inventory = [i for i in context[CONTEXT]['issues'] if i['verifier'] == role and i['status'] == 'PATCHED']
    result['properties']['verifications'] = inventory_array(
        VERIFY, inventory, conditions=True)
    # A check can refer to all frozen issues plus all new discovery issues.
    checks = result['properties']['checks']['items']['properties']['issue_ids']
    checks['maxItems'] = len(context[CONTEXT]['issues']) + result['properties']['issues']['maxItems']
    return result


def output_limit(context, purpose, base):
    from .implementation_whole_review import request_enabled
    if request_enabled(context):
        from .implementation_acceptance import CONTEXT
        return max(base, 6500 + sum(TOKENS_PER_ITEM + 200 * len(i['close_when'])
            for i in context.get(CONTEXT, {}).get('issues', context.get('acceptance_issues', [])))
            + TOKENS_PER_ITEM * len(context.get('implementation_whole_document_review', {}).get('pending_findings', [])))
    from .implementation_report_amendments import KEY as AMENDMENT, output_limit as amendment_limit
    if AMENDMENT in context:
        return amendment_limit(context, base)
    from .implementation_verification_contract import output_limit as verification_limit
    base = verification_limit(context, base)
    stage = purpose.split(':', 1)[0]
    if inventory_enabled(context):
        from .implementation_acceptance import CONTEXT
        inventory = context.get('acceptance_issues', context.get(CONTEXT, {}).get('issues', []))
        # Reserve the complete inventory. The ordinary provider/budget admission
        # may reject this reservation; never silently truncate obligations.
        return max(base, 6500 + sum(TOKENS_PER_ITEM + 200 * len(i['close_when']) for i in inventory))
    if stage not in STAGES:
        return base
    from . import implementation_issue_responses as issues

    def enabled(value):
        return type(value.get(issues.PROTOCOL)) is int and value[issues.PROTOCOL] == issues.VERSION

    if not enabled(context):
        return base
    key = 'actions' if stage == 'implementation_spec' else 'requirements'
    count = len(issues._contract(context)[key])
    completion_tokens = 0
    if stage == 'implementation_spec':
        from .implementation_author_revision import contract
        revision = contract(context)
        if revision is not None:
            completion_tokens = sum(300 + 150 * len(item['fields']) for item in revision['items'])
    # This bounds the extra reservation. A higher existing alignment cap is
    # preserved; provider/account and execution budget caps still apply.
    return max(base, min(MAX_OUTPUT, base + TOKENS_PER_ITEM * count + completion_tokens))
