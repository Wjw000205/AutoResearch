"""Read-only eligibility for one explicit final continuation after normal HTTP 524.

This is not automatic HTTP retry. The original assignment, tool results and
charges remain intact; Engine requires a fresh explicit-start generation. Old
audits establish provider/model/endpoint/protocol, not the historical credential
environment name. That name is bound only at the explicit-resume snapshot.
"""
from __future__ import annotations

import copy
import hashlib
import math
import re
from datetime import datetime

HTTP_RECOVERY_REASON = 'One operator-authorized final continuation after HTTP 524; original charges remain unverified'


def provider_identity(provider):
    return {field: provider.get(field) for field in ('id', 'model', 'protocol', 'base_url', 'api_key_env')}


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
        return stamp.timestamp() if stamp.tzinfo else None
    except (ValueError, OverflowError):
        return None


def derive_normal_http_source(store, intelligence, run_id, key, progress):
    """Return canonical 524 evidence, or None; never write, preflight or send."""
    from .intelligence import ProviderError
    try:
        return _derive(store, intelligence, run_id, key, progress)
    except (ProviderError, KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return None


def _derive(store, intelligence, run_id, key, progress):
    from .engine import Harness, digest
    from .intelligence import Intelligence, is_moderation_error, _json

    if not isinstance(intelligence, Intelligence):
        return None
    run, handoff = store.get('runs', run_id, {}), store.get('handoffs', key, {})
    assignment = store.get('handoff_inputs', key, {})
    normal, context = progress.get('normal_request', {}), assignment.get('context')
    prepared, role = normal.get('prepared'), handoff.get('to_role')
    if (run.get('mode') != 'live' or run.get('status') not in {'blocked', 'paused', 'running'}
            or store.get('run_deletions', run_id) or store.get('decisions', key)
            or any(row.get('run_id') != run_id or row.get('id') != key for row in (handoff, assignment, progress))
            or handoff.get('status') not in {'blocked', 'interrupted', 'running'}
            or handoff.get('stage') != run.get('stage') or role not in {'director', 'engineer', 'scout'}
            or progress.get('mode') == 'final_only' or progress.get('finalization')
            or progress.get('finalization_history') or progress.get('normal_migrations')
            or progress.get('pending_tool_calls') or normal.get('migration_of') or normal.get('legacy_source_input_id')
            or normal.get('status') not in {'blocked', 'dispatching'} or normal.get('response') is not None
            or any(normal.get(field) for field in ('received_at', 'completed_at', 'recovered_at'))
            or type(progress.get('turn')) is not int or progress['turn'] < 0
            or type(normal.get('turn')) is not int or normal['turn'] != progress['turn']
            or not isinstance(progress.get('tool_results'), list)
            or not isinstance(normal.get('operation_id'), str) or not normal['operation_id']
            or not isinstance(context, dict) or context.get('research_cycle', 1) != run.get('state', {}).get('research_cycle', 1)
            or not isinstance(assignment.get('instruction'), str) or not isinstance(assignment.get('schema'), dict)
            or not isinstance(prepared, dict) or prepared.get('role') != role or not isinstance(prepared.get('context'), dict)):
        return None
    if (key != Harness.decision_key(run_id, role, handoff['purpose'], assignment['instruction'], context, assignment['schema'])
            or handoff.get('input_hash') != digest({field: assignment[field] for field in ('instruction', 'context', 'schema')})
            or progress['turn'] >= Harness._tool_turn_limit(handoff['purpose'], context, progress)):
        return None
    expected = Harness.prepare(role, handoff['purpose'], assignment['instruction'], context,
                               assignment['schema'], progress['tool_results'])
    if any('output_limit' in item for item in progress['tool_results']):
        expected['context']['_output_token_limit'] *= 2
    expected['context']['current_date'] = prepared['context'].get('current_date')
    if expected != prepared:
        return None
    dispatched = _timestamp(normal.get('dispatched_at'))
    if dispatched is None:
        return None
    operation = intelligence._operation_identity(role, prepared['task'], prepared['context'], prepared['schema'])
    controller = prepared['context'].get('_harness_control', {}).get('operation_id')
    controllers = {normal['operation_id'], *([controller] if controller else [])}
    related = lambda row: row.get('operation_id') == operation or row.get('controller_operation_id') in controllers
    budget = store.get('budgets', run_id, {})
    records = budget.get('records')
    if (budget.get('id') != run_id or budget.get('run_id') != run_id or not isinstance(records, list)
            or not isinstance(budget.get('pending'), dict) or budget['pending']
            or any(not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id'] for row in records)
            or len({row['id'] for row in records}) != len(records)):
        return None
    candidates = [row for row in records if related(row)]
    if not 1 <= len(candidates) <= 3:
        return None
    ids = {row['id'] for row in candidates}
    associated = lambda row: related(row) or row.get('id') in ids or row.get('request_id') in ids
    # Inspect all owners for matching identities, never silently filter a
    # contradictory foreign-role/run receipt out of the evidence set.
    for other in store.list('budgets'):
        if other.get('id') != run_id and any(associated(row) for row in
                [*other.get('records', []), *other.get('pending', {}).values()]):
            return None
    mirrors = [row for row in store.list('usage') if associated(row)]
    audits = [row for row in store.list('request_audits') if associated(row)]
    if (len(mirrors) != len(candidates) or {row.get('id') for row in mirrors} != ids
            or any(store.get('usage', row['id']) != row for row in candidates)
            or len(audits) != len(candidates) or {row.get('id') for row in audits} != ids
            or {row.get('request_id') for row in audits} != ids
            or any(row.get('id') != row.get('request_id') for row in audits)):
        return None
    if (any(row.get('kind') == 'output' and associated(row) for row in store.list('transcripts'))
            or any(row.get('review_status') == 'pending' for row in store.list('moderation_reviews', run_id))
            or any(is_moderation_error(row.get('error_details')) or row.get('moderation_required')
                   for row in [*records, *store.list('usage', run_id)])):
        return None
    provider = intelligence.provider_for(role)
    if (provider['id'], provider['model']) != (normal.get('provider_id'), normal.get('model')):
        return None
    caps = set()
    for row in candidates:
        start, end = _timestamp(row.get('created_at')), _timestamp(row.get('completed_at'))
        cap, cost = row.get('request_output_token_limit'), row.get('cost_usd')
        if (any(row.get(field) != value for field, value in {'run_id': run_id, 'role': role,
                'provider': provider['id'], 'model': provider['model'], 'operation_id': operation,
                'controller_operation_id': controller}.items())
                or row.get('status') != 'http_error' or type(row.get('http_status')) is not int or row['http_status'] != 524
                or row.get('provenance') != 'live_provider' or row.get('cache_hit') is not False
                or not isinstance(row.get('error_details'), dict) or not isinstance(row.get('usage'), dict)
                or type(row.get('cost_estimated')) is not bool or type(cost) not in (int, float)
                or not math.isfinite(cost) or cost < 0 or type(cap) is not int or cap <= 0
                or start is None or end is None or not dispatched <= start <= end):
            return None
        audit = store.get('request_audits', row['id'], {})
        endpoint, _, wire = intelligence._wire(provider, role, '', '', cap)
        if (row.get('request_audit_id') != row['id'] or audit not in audits
                or any(audit.get(field) != row.get(field) for field in ('run_id', 'role', 'provider', 'model',
                                                                     'operation_id', 'controller_operation_id'))
                or audit.get('protocol') != provider.get('protocol') or audit.get('endpoint') != endpoint
                or audit.get('http_method') != 'POST' or audit.get('capture_stage') != 'prepared_before_http'
                or audit.get('hash_format') != 'sha256-canonical-json-v1' or audit.get('redacted') is not True
                or audit.get('truncated') is not False or not isinstance(audit.get('body'), dict)
                or not isinstance(audit.get('body_sha256'), str) or re.fullmatch('[0-9a-f]{64}', audit['body_sha256']) is None
                or type(audit.get('original_body_bytes')) is not int or audit['original_body_bytes'] <= 0):
            return None
        cap_field = 'max_output_tokens' if provider['protocol'] == 'responses' else 'max_tokens'
        if audit['body'].get('model') != wire['model'] or audit['body'].get(cap_field) != cap:
            return None
        encoded = _json(audit['body']).encode('utf-8')
        if (audit.get('redacted_body_sha256') != hashlib.sha256(encoded).hexdigest()
                or type(audit.get('redacted_body_bytes')) is not int or audit['redacted_body_bytes'] != len(encoded)):
            return None
        caps.add(cap)
    if len(caps) != 1:
        return None
    candidates.sort(key=lambda row: (row['created_at'], row['id']))
    return {'kind': 'normal_gateway_timeout', 'request_ids': [row['id'] for row in candidates],
            'provider_id': provider['id'], 'model': provider['model'], 'effective_output_limit': caps.pop(),
            'request_operation_id': operation, 'linkage': 'canonical_operation_id',
            'charge_status': 'charge_unverified', 'response_confirmed': False,
            'http_statuses': [524] * len(candidates), 'estimated_cost_usd': round(sum(row['cost_usd'] for row in candidates), 10),
            'provider_identity': provider_identity(provider), 'credential_binding_scope': 'explicit_resume_snapshot',
            'receipt_sha256': digest([candidates, sorted(audits, key=lambda row: row['id'])]),
            'prepared_sha256': digest(prepared)}


def validate_gateway_dispatch(store, intelligence, run_id, key, progress, provider, preview):
    """Revalidate archived source and channel before the one allowed send."""
    final = progress.get('finalization', {})
    old = next((row for row in progress.get('finalization_history', [])
                if row.get('operation_id') == final.get('explicit_retry_of')), None)
    source = (old or {}).get('gateway_timeout_source')
    if not source:
        return False
    original = copy.deepcopy(progress)
    for field in ('mode', 'finalization', 'finalization_history'):
        original.pop(field, None)
    original['normal_request'] = copy.deepcopy(old)
    current = derive_normal_http_source(store, intelligence, run_id, key, original)
    return (current == source and provider_identity(provider) == source['provider_identity']
            and bool(preview) and len(preview.get('requests', [])) == 1
            and preview['requests'][0]['max_output_tokens'] == source['effective_output_limit'])
