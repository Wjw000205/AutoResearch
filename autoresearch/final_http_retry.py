"""Final-only automatic HTTP retry policy and explicit-confirmation lineage.

No unknown-charge or explicitly single-dispatch operation qualifies for automatic
retry. A frozen logical operation may have at most three individually billed
attempts. This module derives evidence for the Harness and, after a later unknown
result, for the separately confirmed one-attempt recovery API. It never sends.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time
from datetime import datetime
from email.utils import parsedate_to_datetime

TRANSIENT_HTTP = frozenset({408, 429, 500, 502, 503, 504})
MAX_ATTEMPTS = 3


def epoch_now():
    return time.time()


def retry_after_seconds(value, *, now_ts=None):
    """Parse only a legal delay-seconds or HTTP date; never persist the header."""
    if not isinstance(value, str) or len(value) > 128:
        return None
    value = value.strip()
    if re.fullmatch(r'[0-9]+', value):
        return min(120, int(value))
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            return None
        delay = parsed.timestamp() - (epoch_now() if now_ts is None else now_ts)
        return min(120, max(0, math.ceil(delay))) if math.isfinite(delay) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def derive_retry(store, intelligence, run_id, key, progress):
    """Return a bounded plan only when *every* canonical receipt proves failure."""
    from .engine import Harness, digest
    from .intelligence import Intelligence, is_moderation_error, _hash as request_hash

    if not isinstance(intelligence, Intelligence):
        return None
    run = store.get('runs', run_id, {})
    final = progress.get('finalization', {})
    prepared = final.get('prepared')
    if (run.get('mode') != 'live' or store.get('run_deletions', run_id)
            or progress.get('mode') != 'final_only'
            or final.get('status') not in {'blocked', 'dispatching', 'retry_wait'}
            or final.get('response') is not None or store.get('decisions', key)
            or progress.get('finalization_history') or progress.get('normal_migrations')
            or any(final.get(field) for field in ('recovery_of', 'explicit_retry_of',
                'request_retry_authorization', 'operator_resume_generation', 'migration_of'))
            or not isinstance(prepared, dict) or not isinstance(prepared.get('context'), dict)):
        return None
    role, operation = prepared.get('role'), final.get('operation_id')
    control = prepared['context'].get('_harness_control', {})
    if (not isinstance(operation, str) or not operation or control.get('mode') != 'final_only'
            or control.get('operation_id') != operation
            or any(control.get(field) for field in ('operator_resume_generation', 'recovery', 'provider_migration'))):
        return None
    assignment, handoff = store.get('handoff_inputs', key, {}), store.get('handoffs', key, {})
    if (handoff.get('run_id') != run_id or assignment.get('run_id') != run_id
            or handoff.get('to_role') != role or prepared.get('schema') != assignment.get('schema')
            or handoff.get('input_hash') != digest({name: assignment.get(name) for name in ('instruction', 'context', 'schema')})
            or key != Harness.decision_key(run_id, role, handoff.get('purpose'), assignment.get('instruction'),
                                           assignment.get('context'), assignment.get('schema'))):
        return None
    expected = Harness.prepare(role, handoff['purpose'], assignment['instruction'], assignment['context'],
                               assignment['schema'], progress.get('tool_results', []),
                               final_control={'operation_id': operation, 'reason': final.get('reason')})
    expected['context']['current_date'] = prepared['context'].get('current_date')
    if any('output_limit' in item for item in progress.get('tool_results', [])):
        expected['context']['_output_token_limit'] *= 2
    if expected != prepared:
        return None
    provider = intelligence.provider_for(role)
    if (provider['id'], provider['model']) != (final.get('provider_id'), final.get('model')):
        return None
    identity = {name: provider.get(name) for name in ('id', 'model', 'protocol', 'base_url', 'api_key_env')}
    previous = final.get('http_retry', {})
    if not isinstance(previous, dict):
        return None
    if previous and (previous.get('prepared_sha256') != _hash(prepared)
                     or previous.get('provider_identity') != identity):
        return None
    logical = intelligence._operation_identity(role, prepared['task'], prepared['context'], prepared['schema'])
    related = lambda row: row.get('operation_id') == logical or row.get('controller_operation_id') == operation
    ledger = store.get('budgets', run_id, {})
    if ledger.get('id') != run_id or ledger.get('run_id') != run_id:
        return None
    audits = [row for row in store.list('request_audits', run_id) if related(row)]
    audit_ids = {row.get('request_id') for row in audits}
    records = [row for row in ledger.get('records', []) if related(row) or row.get('id') in audit_ids]
    if (not 1 <= len(records) < MAX_ATTEMPTS
            or any(related(row) or row.get('id') in audit_ids for row in ledger.get('pending', {}).values())):
        return None
    ids = [row.get('id') for row in records]
    if (any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != len(ids)
            or len(audits) != len(records) or audit_ids != set(ids)):
        return None
    if (any(row.get('kind') == 'output' and (related(row) or row.get('request_id') in ids)
            for row in store.list('transcripts', run_id))
            or any(row.get('review_status') == 'pending' for row in store.list('moderation_reviews', run_id))):
        return None
    initial_dispatch = previous.get('initial_dispatched_at', final.get('dispatched_at'))
    initial_ts = _timestamp(initial_dispatch)
    if initial_ts is None:
        return None
    caps, completed = set(), []
    for row in records:
        start, end = _timestamp(row.get('created_at')), _timestamp(row.get('completed_at'))
        cap, cost, details = row.get('request_output_token_limit'), row.get('cost_usd'), row.get('error_details')
        if (any(row.get(name) != value for name, value in {'run_id': run_id, 'role': role,
                'provider': provider['id'], 'model': provider['model'], 'operation_id': logical,
                'controller_operation_id': operation}.items())
                or row.get('status') != 'http_error' or type(row.get('http_status')) is not int
                or row['http_status'] not in TRANSIENT_HTTP or row.get('provenance') != 'live_provider'
                or not isinstance(details, dict) or is_moderation_error(details) or row.get('moderation_required')
                or not isinstance(row.get('usage'), dict) or type(cost) not in (int, float)
                or not math.isfinite(cost) or cost < 0 or type(cap) is not int or cap <= 0
                or start is None or end is None or not initial_ts <= start <= end):
            return None
        audit = next(item for item in audits if item.get('request_id') == row['id'])
        endpoint, _, _ = intelligence._wire(provider, role, '', '', cap)
        if (audit.get('id') != row['id'] or row.get('request_audit_id') != row['id']
                or any(audit.get(name) != row.get(name) for name in ('run_id', 'role', 'provider', 'model',
                                                                    'operation_id', 'controller_operation_id'))
                or audit.get('endpoint') != endpoint or audit.get('protocol') != provider.get('protocol')
                or audit.get('http_method') != 'POST' or audit.get('capture_stage') != 'prepared_before_http'
                or not isinstance(audit.get('body_sha256'), str)
                or re.fullmatch(r'[0-9a-f]{64}', audit['body_sha256']) is None):
            return None
        caps.add(cap)
        completed.append((end, row))
    if len(caps) != 1:
        return None
    cap = caps.pop()
    if previous and previous.get('effective_output_limit') != cap:
        return None
    if final.get('effective_output_limit', cap) != cap:
        return None
    records.sort(key=lambda row: (row['created_at'], row['id']))
    audits.sort(key=lambda row: row['id'])
    if previous:
        old_ids = previous.get('source_request_ids', [])
        if (not isinstance(old_ids, list) or any(not isinstance(value, str) for value in old_ids)
                or not old_ids or len(set(old_ids)) != len(old_ids)
                or type(previous.get('max_attempts')) is not int or previous['max_attempts'] != MAX_ATTEMPTS
                or type(previous.get('attempts_completed')) is not int
                or previous.get('attempts_completed') != len(old_ids)
                or not set(old_ids).issubset(ids)
                or previous.get('source_sha256') != _hash([
                    [row for row in records if row['id'] in old_ids],
                    [row for row in audits if row['id'] in old_ids]])):
            return None
    last_time, last = max(completed, key=lambda pair: (pair[0], pair[1]['id']))
    # A failed admission after claiming a later dispatch is not another HTTP
    # failure. Never recycle the previous receipt into an unlimited retry loop.
    dispatched = _timestamp(final.get('dispatched_at'))
    if (dispatched is None or final['status'] != 'retry_wait'
            and _timestamp(last['created_at']) < dispatched):
        return None
    retry_after = last.get('retry_after_seconds')
    retry_after = retry_after if type(retry_after) in (int, float) and math.isfinite(retry_after) and 0 <= retry_after <= 120 else 0
    delay = max((10, 30)[len(records) - 1], retry_after)
    circuit = store.get('circuits', request_hash([provider['id'], provider.get('base_url'), provider['model']]), {})
    cooldown = circuit.get('open_until', 0)
    cooldown = cooldown if type(cooldown) in (int, float) and math.isfinite(cooldown) else 0
    return {'version': 1, 'max_attempts': MAX_ATTEMPTS, 'attempts_completed': len(records),
            'source_request_ids': [row['id'] for row in records], 'source_sha256': _hash([records, audits]),
            'provider_identity': identity, 'prepared_sha256': _hash(prepared), 'effective_output_limit': cap,
            'initial_dispatched_at': initial_dispatch, 'delay_seconds': delay,
            'not_before': max(last_time + delay, cooldown)}


def verified_unknown_suffix(store, intelligence, run_id, key, progress, records, unknown_statuses):
    """Recognize only our proven HTTP prefix followed by one unknown attempt.

    This does not authorize a send. It lets the existing explicit confirmation API
    retain an auditable recovery route after this policy encounters a timeout.
    """
    final = progress.get('finalization', {})
    plan = final.get('http_retry')
    if not isinstance(plan, dict) or not 2 <= len(records) <= MAX_ATTEMPTS:
        return None
    prefix_ids = plan.get('source_request_ids')
    if (not isinstance(prefix_ids, list) or not 1 <= len(prefix_ids) <= 2
            or any(not isinstance(value, str) or not value for value in prefix_ids)
            or len(set(prefix_ids)) != len(prefix_ids)
            or type(plan.get('attempts_completed')) is not int or plan['attempts_completed'] != len(prefix_ids)):
        return None
    prefix = [row for row in records if row.get('id') in prefix_ids]
    suffix = [row for row in records if row.get('id') not in prefix_ids]
    if (len(prefix) != len(prefix_ids) or len(suffix) != 1
            or suffix[0].get('status') not in unknown_statuses or suffix[0].get('http_status') is not None):
        return None
    terminal = suffix[0]
    times = [_timestamp(row.get('completed_at')) for row in prefix]
    start, end, dispatch = (_timestamp(terminal.get('created_at')),
                            _timestamp(terminal.get('completed_at')), _timestamp(final.get('dispatched_at')))
    if (any(value is None for value in times) or start is None or end is None or dispatch is None
            or not max(*times, dispatch) <= start <= end):
        return None
    if store.get('budgets', run_id, {}).get('pending'):
        return None

    class PrefixView:
        # Read-only projection for reusing the full HTTP receipt verifier. The
        # caller still validates the terminal receipt and every channel identity.
        def get(self, collection, identifier, default=None):
            value = store.get(collection, identifier, default)
            if collection == 'budgets' and identifier == run_id:
                return {**value, 'records': [row for row in value.get('records', []) if row.get('id') != terminal['id']]}
            return value

        def list(self, collection, owner=None):
            values = store.list(collection, owner)
            if collection == 'request_audits':
                return [row for row in values if row.get('request_id') != terminal['id']]
            return values

    projected = copy.deepcopy(progress)
    projected['finalization']['status'] = 'retry_wait'
    verified = derive_retry(PrefixView(), intelligence, run_id, key, projected)
    if (verified is None or any(verified.get(field) != plan.get(field) for field in (
            'version', 'max_attempts', 'attempts_completed', 'source_request_ids', 'source_sha256',
            'provider_identity', 'prepared_sha256', 'effective_output_limit', 'initial_dispatched_at'))):
        return None
    return {'previous_http_request_ids': prefix_ids,
            'previous_http_cost_usd': round(sum(row['cost_usd'] for row in prefix), 10),
            'source_types': [{'request_id': row['id'], 'kind': 'confirmed_transient_http_failure',
                              'http_status': row['http_status'], 'cost_estimated': row.get('cost_estimated')}
                             for row in prefix] + [{'request_id': terminal['id'], 'kind': 'terminal_unknown_charge',
                                                   'http_status': None, 'cost_estimated': True}]}
