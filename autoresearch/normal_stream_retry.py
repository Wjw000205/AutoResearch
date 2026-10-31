"""One durable normal-tool continuation after a confirmed upstream SSE failure.

The failed response is not an answer and may have incurred charges. Only the
explicit upstream_stream_read_error terminal qualifies; EOF, unknown charges,
moderation, invalid output and unrelated provider errors never qualify here.
"""
from __future__ import annotations

import copy
import hashlib
import math
import re

from .normal_http_recovery import _timestamp, provider_identity


def derive_source(store, intelligence, run_id, key, progress):
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
    run = store.get('runs', run_id, {})
    normal = progress.get('normal_request', {})
    prepared = normal.get('prepared', {})
    assignment = store.get('handoff_inputs', key, {})
    handoff = store.get('handoffs', key, {})
    role = handoff.get('to_role')
    if (run.get('mode') != 'live' or run.get('status') not in {'running', 'blocked', 'paused'}
            or store.get('run_deletions', run_id) or store.get('decisions', key)
            or progress.get('mode') == 'final_only' or progress.get('finalization')
            or progress.get('normal_stream_history') or progress.get('finalization_history')
            or progress.get('normal_migrations') or progress.get('pending_tool_calls')
            or normal.get('status') not in {'blocked', 'dispatching'} or normal.get('response') is not None
            or any(normal.get(name) for name in ('stream_retry_of', 'migration_of', 'legacy_source_input_id', 'received_at', 'completed_at'))
            or type(progress.get('turn')) is not int or normal.get('turn') != progress['turn']
            or not isinstance(progress.get('tool_results'), list)
            or any(row.get('run_id') != run_id or row.get('id') != key for row in (progress, assignment, handoff))
            or handoff.get('stage') != run.get('stage') or prepared.get('role') != role
            or not isinstance(prepared.get('context'), dict)
            or handoff.get('input_hash') != digest({field: assignment.get(field) for field in ('instruction', 'context', 'schema')})
            or key != Harness.decision_key(run_id, role, handoff.get('purpose'), assignment.get('instruction'),
                                            assignment.get('context'), assignment.get('schema'))):
        return None
    expected = Harness.prepare(role, handoff['purpose'], assignment['instruction'], assignment['context'],
                               assignment['schema'], progress['tool_results'])
    expected['context']['current_date'] = prepared['context'].get('current_date')
    if any('output_limit' in item for item in progress['tool_results']):
        expected['context']['_output_token_limit'] *= 2
    if expected != prepared:
        return None
    provider = intelligence.provider_for(role)
    if (provider.get('protocol') != 'responses' or provider.get('responses_streaming') is not True
            or (normal.get('provider_id'), normal.get('model')) != (provider['id'], provider['model'])):
        return None
    operation = intelligence._operation_identity(role, prepared['task'], prepared['context'], prepared['schema'])
    controller = prepared['context'].get('_harness_control', {}).get('operation_id')
    related = lambda row: row.get('operation_id') == operation
    ledger = store.get('budgets', run_id, {})
    records = [row for row in ledger.get('records', []) if related(row)]
    if len(records) != 1 or any(related(row) for row in ledger.get('pending', {}).values()):
        return None
    row = records[0]
    metadata, stream = row.get('response_metadata', {}), row.get('stream_metadata', {})
    cap, cost = row.get('request_output_token_limit'), row.get('cost_usd')
    dispatched, started, ended = map(_timestamp, (normal.get('dispatched_at'), row.get('created_at'), row.get('completed_at')))
    if (any(row.get(field) != value for field, value in {'run_id': run_id, 'role': role,
            'provider': provider['id'], 'model': provider['model'], 'controller_operation_id': controller}.items())
            or row.get('status') != 'provider_error' or type(row.get('http_status')) is not int or row['http_status'] != 200
            or row.get('error_details', {}).get('code') != 'upstream_stream_read_error'
            or is_moderation_error(row.get('error_details')) or row.get('moderation_required')
            or metadata.get('provider_status') != 'failed' or metadata.get('output_truncated') is not False
            or metadata.get('output_limit_reached') is not False
            or stream.get('transport') != 'responses_stream' or stream.get('terminal_received') is not True
            or stream.get('terminal_status') != 'failed' or not stream.get('response_id')
            or row.get('provider_response_id') != stream['response_id']
            or row.get('provenance') != 'live_provider' or not isinstance(row.get('usage'), dict)
            or type(cap) is not int or cap <= 0 or type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0
            or dispatched is None or started is None or ended is None or not dispatched <= started <= ended
            or store.get('usage', row['id']) != row):
        return None
    audit = store.get('request_audits', row['id'], {})
    endpoint, _, _ = intelligence._wire(provider, role, '', '', cap)
    body = audit.get('body')
    if (row.get('request_audit_id') != row['id'] or audit.get('id') != row['id'] or audit.get('request_id') != row['id']
            or any(audit.get(field) != row.get(field) for field in ('run_id', 'role', 'provider', 'model', 'operation_id', 'controller_operation_id'))
            or audit.get('protocol') != 'responses' or audit.get('endpoint') != endpoint
            or audit.get('capture_stage') != 'prepared_before_http' or audit.get('http_method') != 'POST'
            or audit.get('truncated') is not False or audit.get('redacted') is not True or not isinstance(body, dict)
            or body.get('stream') is not True or body.get('model') != provider['model'] or body.get('max_output_tokens') != cap
            or audit.get('redacted_body_sha256') != hashlib.sha256(_json(body).encode()).hexdigest()
            or any(item.get('kind') == 'output' and (related(item) or item.get('request_id') == row['id'])
                   for item in store.list('transcripts', run_id))
            or any(item.get('review_status') == 'pending' for item in store.list('moderation_reviews', run_id))):
        return None
    return {'request_id': row['id'], 'receipt_sha256': digest([row, audit]),
            'prepared_sha256': digest(prepared), 'tool_results_sha256': digest(progress['tool_results']),
            'provider_identity': provider_identity(provider), 'effective_output_limit': cap,
            'cost_usd': cost, 'cost_estimated': row.get('cost_estimated'),
            'not_before': ended + max(5, min(120, row.get('retry_after_seconds') or 0))}


def retry_prepared(previous, operation_id, source):
    prepared = copy.deepcopy(previous['prepared'])
    context = prepared['context']
    context['_output_token_limit'] = source['effective_output_limit']
    context['_harness_control'] = {**context.get('_harness_control', {}),
        'operation_id': operation_id, 'stream_retry_of': previous['operation_id']}
    # The old receipt remains immutable. Present equivalent short handles for
    # its saved PDF reads in this new continuation, keeping every canonical ID
    # and all exact text, ranges and hashes. Note validation still proves reads.
    from .pdf_tools import citation_alias
    for item in context.get('tool_results', []):
        result = item.get('result', {})
        if item.get('call', {}).get('name') != 'pdf.read' or not isinstance(result, dict):
            continue
        page = result.get('pdf_page')
        if type(page) is not int or page < 1:
            continue
        for citation in result.get('citations', []):
            cid = citation.get('citation_id')
            if (isinstance(cid, str) and re.fullmatch(r'[a-zA-Z0-9]+:pdf-citation:[0-9a-f]{64}', cid)
                    and type(citation.get('start')) is int and type(citation.get('end')) is int):
                citation.update(canonical_citation_id=cid,
                    citation_id=citation_alias({'id': cid, 'page': page, 'start': citation['start'], 'end': citation['end']}))
    return prepared


def validate_dispatch(store, intelligence, run_id, key, progress):
    normal = progress.get('normal_request', {})
    history = progress.get('normal_stream_history', [])
    if len(history) != 1 or normal.get('stream_retry_of') != history[0].get('operation_id'):
        return False
    previous = copy.deepcopy(progress)
    previous['normal_request'] = history[0]
    previous.pop('normal_stream_history')
    source = derive_source(store, intelligence, run_id, key, previous)
    return bool(source and source == normal.get('stream_source')
                and normal.get('prepared') == retry_prepared(history[0], normal['operation_id'], source))
