"""One confirmed buffered-524 to Responses-stream migration; no network or writes.

The only accepted lineage is the existing normal gateway recovery followed by
one failed buffered final request. HTTP failure does not prove absence of charges.
Old credentials are not inferred: the saved explicit-resume channel is preserved.
"""
from __future__ import annotations

import copy
import hashlib
import math
import re

from .normal_http_recovery import derive_normal_http_source, provider_identity, _timestamp, HTTP_RECOVERY_REASON


KIND = 'streaming_gateway_timeout'


def derive_streaming_source(store, intelligence, run_id, key, progress):
    """Return a fully checked source chain or None, without sending/preflighting."""
    from .intelligence import ProviderError
    try:
        return _derive(store, intelligence, run_id, key, progress)
    except (ProviderError, KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return None


def _derive(store, intelligence, run_id, key, progress):
    from .engine import Harness, digest
    from .intelligence import Intelligence, _json, is_moderation_error

    if not isinstance(intelligence, Intelligence):
        return None
    final = progress.get('finalization', {})
    history = progress.get('finalization_history', [])
    assignment = store.get('handoff_inputs', key, {})
    handoff = store.get('handoffs', key, {})
    role = handoff.get('to_role')
    if (progress.get('mode') != 'final_only' or final.get('status') not in {'blocked', 'dispatching'}
            or len(history) != 1 or not isinstance(history[0], dict)
            or final.get('response') is not None
            or any(final.get(name) for name in ('streaming_migration', 'request_retry_authorization',
                                               'confirmed_retry_of', 'recovery_of', 'http_retry', 'migration_of'))
            or any(name in final for name in ('received_at', 'completed_at', 'recovered_at'))
            or progress.get('normal_migrations') or progress.get('pending_tool_calls')
            or final.get('reason') != HTTP_RECOVERY_REASON):
        return None
    old = history[0]
    archived_source = old.get('gateway_timeout_source')
    if (not isinstance(archived_source, dict) or old.get('streaming_migration')
            or final.get('explicit_retry_of') != old.get('operation_id')
            or progress.get('normal_request') != {k: v for k, v in old.items() if k != 'gateway_timeout_source'}):
        return None
    provider = intelligence.provider_for(role)
    if (provider.get('protocol') != 'responses' or provider.get('responses_streaming') is not True
            or provider_identity(provider) != archived_source.get('provider_identity')):
        return None
    original = copy.deepcopy(progress)
    for field in ('mode', 'finalization', 'finalization_history'):
        original.pop(field, None)
    original['normal_request'] = copy.deepcopy(old)
    source = derive_normal_http_source(store, intelligence, run_id, key, original)
    if not source or source != archived_source:
        return None
    prepared = final.get('prepared')
    cap = source['effective_output_limit']
    generation = store.get('operator_resumes', final.get('operator_resume_generation', ''), {})
    if (generation.get('kind') != 'explicit_start_command' or generation.get('run_id') != run_id
            or generation.get('targets', {}).get(key) != Harness._resume_target(old, progress['turn'])
            or final.get('provider_id') != provider['id'] or final.get('model') != provider['model']
            or final.get('effective_output_limit') != cap or not isinstance(prepared, dict)
            or not isinstance(final.get('operation_id'), str) or not final['operation_id']):
        return None
    expected = Harness.prepare(role, handoff['purpose'], assignment['instruction'], assignment['context'],
        assignment['schema'], progress['tool_results'], final_control={'operation_id': final['operation_id'],
            'reason': HTTP_RECOVERY_REASON, 'operator_resume_generation': generation['id']})
    expected['context']['_output_token_limit'] = cap
    expected['context']['current_date'] = prepared['context'].get('current_date')
    if expected != prepared:
        return None
    operation = intelligence._operation_identity(role, prepared['task'], prepared['context'], prepared['schema'])
    controller = final['operation_id']
    related = lambda row: row.get('operation_id') == operation or row.get('controller_operation_id') == controller
    budget = store.get('budgets', run_id, {})
    candidates = [row for row in budget['records'] if related(row)]
    if len(candidates) != 1:
        return None
    row = candidates[0]
    request_id = row['id']
    associated = lambda item: related(item) or item.get('id') == request_id or item.get('request_id') == request_id
    for other in store.list('budgets'):
        if other.get('id') != run_id and any(associated(item) for item in
                [*other.get('records', []), *other.get('pending', {}).values()]):
            return None
    mirrors = [item for item in store.list('usage') if associated(item)]
    audits = [item for item in store.list('request_audits') if associated(item)]
    if (mirrors != [row] or len(audits) != 1 or audits[0].get('id') != request_id
            or audits[0].get('request_id') != request_id
            or any(item.get('kind') == 'output' and associated(item) for item in store.list('transcripts'))):
        return None
    dispatched, start, end = (_timestamp(final.get('dispatched_at')), _timestamp(row.get('created_at')),
                              _timestamp(row.get('completed_at')))
    cost = row.get('cost_usd')
    if (any(row.get(field) != value for field, value in {'run_id': run_id, 'role': role,
            'provider': provider['id'], 'model': provider['model'], 'operation_id': operation,
            'controller_operation_id': controller, 'request_output_token_limit': cap}.items())
            or row.get('status') != 'http_error' or type(row.get('http_status')) is not int or row['http_status'] != 524
            or row.get('provenance') != 'live_provider' or row.get('cache_hit') is not False
            or not isinstance(row.get('error_details'), dict) or is_moderation_error(row['error_details'])
            or row.get('moderation_required') or not isinstance(row.get('usage'), dict)
            or type(row.get('cost_estimated')) is not bool or type(cost) not in (int, float)
            or not math.isfinite(cost) or cost < 0 or dispatched is None or start is None or end is None
            or not dispatched <= start <= end):
        return None
    audit = audits[0]
    endpoint, _, _ = intelligence._wire(provider, role, '', '', cap)
    if (row.get('request_audit_id') != request_id
            or any(audit.get(field) != row.get(field) for field in ('run_id', 'role', 'provider', 'model',
                                                                  'operation_id', 'controller_operation_id'))
            or audit.get('endpoint') != endpoint or audit.get('protocol') != 'responses'
            or audit.get('http_method') != 'POST' or audit.get('capture_stage') != 'prepared_before_http'
            or audit.get('hash_format') != 'sha256-canonical-json-v1' or audit.get('redacted') is not True
            or audit.get('truncated') is not False or not isinstance(audit.get('body'), dict)
            or not isinstance(audit.get('body_sha256'), str) or not re.fullmatch('[0-9a-f]{64}', audit['body_sha256'])
            or type(audit.get('original_body_bytes')) is not int or audit['original_body_bytes'] <= 0):
        return None
    body = audit['body']
    encoded = _json(body).encode('utf-8')
    if (body.get('model') != provider['model'] or body.get('max_output_tokens') != cap
            or body.get('stream', False) is not False
            or audit.get('redacted_body_sha256') != hashlib.sha256(encoded).hexdigest()
            or type(audit.get('redacted_body_bytes')) is not int or audit['redacted_body_bytes'] != len(encoded)):
        return None
    # The whole chain must prove buffered transport, including the old normal attempts.
    if any(store.get('request_audits', item, {}).get('body', {}).get('stream', False) is not False
           for item in source['request_ids']):
        return None
    prior_cost = source['estimated_cost_usd']
    return {'recovery_kind': KIND, 'source_transport': 'buffered', 'destination_transport': 'stream',
            'request_ids': [request_id], 'source_request_ids': [*source['request_ids'], request_id],
            'previous_http_request_ids': source['request_ids'], 'previous_http_cost_usd': prior_cost,
            'estimated_cost_usd': round(prior_cost + cost, 10), 'unknown_cost_usd': 0.0,
            'provider_identity': provider_identity(provider), 'responses_streaming': True,
            'effective_output_limit': cap, 'previous_operation_id': controller,
            'source_sha256': digest([source, row, audit, final]),
            'credential_binding_scope': 'explicit_resume_snapshot'}


def validate_streaming_dispatch(store, run_id, key, final, provider, target):
    """Validate the archived buffered chain without changing any existing receipt."""
    from .intelligence import Intelligence
    source = final.get('streaming_migration')
    if (not source or source != target.get('streaming_migration')
            or provider.get('responses_streaming') is not True or provider.get('protocol') != 'responses'):
        return False
    progress = copy.deepcopy(store.get('handoff_progress', key, {}))
    history = progress.get('finalization_history', [])
    if len(history) != 2 or history[-1].get('operation_id') != source.get('previous_operation_id'):
        return False
    progress['finalization'] = history[-1]
    progress['finalization_history'] = history[:-1]
    role = progress['finalization']['prepared']['role']
    intelligence = Intelligence(store, {'providers': [provider], 'roles': {role: provider['id']}})
    return derive_streaming_source(store, intelligence, run_id, key, progress) == source
