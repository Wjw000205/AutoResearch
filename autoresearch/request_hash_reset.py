"""Explicit, archived replacement of one settled malformed source-hash request.

This is an operator repair, never an unbounded retry or a silent hash repair.
The model must return a fresh response; valid reads, notes and billing survive.
"""
from __future__ import annotations

import copy
import gzip
import hashlib
import json
import re
from pathlib import Path

from jsonschema import Draft202012Validator

from .storage import now, uid


def corrected_calls(store, run_id, prepared, value):
    """Only malformed source expected_sha256 fields may differ from valid JSON."""
    if not isinstance(value, dict) or set(value) != {'tool_calls'} or not isinstance(value['tool_calls'], list):
        return None
    candidate = copy.deepcopy(value)
    from .evidence_tools import SourceEvidenceService
    sources = SourceEvidenceService(store, store.path.parent)
    changed = False
    try:
        for call in candidate['tool_calls']:
            args = call['arguments']
            sha = args.get('expected_sha256')
            if sha is not None and (not isinstance(sha, str) or not re.fullmatch('[0-9a-fA-F]{64}', sha)):
                args['expected_sha256'] = sources.document(run_id, args['source_id'])['text_sha256']
                changed = True
        if not changed or not Draft202012Validator(prepared['schema']).is_valid(candidate):
            return None
        from .request_hash_policy import validate_batch
        from types import SimpleNamespace
        validate_batch(SimpleNamespace(sources=sources), run_id, candidate['tool_calls'])
        return candidate['tool_calls']
    except (KeyError, TypeError, ValueError):
        return None


def reset(engine, intelligence, run_id, key, *, expected_operation_id):
    """Call with the service stopped, while this exact assignment is blocked."""
    from .engine import digest
    from .codex_reasoning import key_for
    from .schema_recovery import derive_source
    from .tool_request_recovery import prepared_successor
    from .deletion import _plain_path
    store = engine.store
    run, progress = store.get('runs', run_id), store.get('handoff_progress', key)
    normal = progress['normal_request']
    worker = store.get('runtime', 'worker', {})
    if (run['status'] != 'blocked' or normal.get('status') != 'blocked'
            or normal.get('operation_id') != expected_operation_id or normal.get('response') is not None
            or progress.get('pending_tool_calls') or normal.get('turn') != progress.get('turn')
            or normal.get('prepared_sha256') != digest(normal['prepared'])
            or worker.get('held') and worker.get('run_id') == run_id):
        raise ValueError('Exact inactive blocked request required; nothing reset')
    source = derive_source(store, intelligence, run_id, key, normal, tool_hashes=True)
    if not source:
        raise ValueError('Cannot authenticate a settled hash-only failure; nothing reset')
    calls = corrected_calls(store, run_id, normal['prepared'], json.loads(source['original_text']))
    if not calls:
        raise ValueError('Hash-only repair no longer matches exact task-owned sources')
    native_key, _ = key_for(intelligence, run_id, **normal['prepared'])
    generation = uid()
    archive = engine.data_dir / 'diagnostics' / 'request-hash-reset' / run_id / generation
    _plain_path(archive, engine.data_dir)
    archive.mkdir(parents=True)
    # Original paid inputs/output remain immutable audit evidence. Only the new
    # active packet changes; no bad response is inserted into its fresh context.
    snapshot = {'progress': progress, 'source': source,
        'native': store.get('codex_reasoning', native_key),
        'input': store.get('handoff_inputs', key),
        'transcript': store.get('transcripts', source['transcript_id'])}
    raw = json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode('utf-8')
    path = archive / 'failed-request.json.gz'
    path.write_bytes(gzip.compress(raw))
    if gzip.decompress(path.read_bytes()) != raw:
        raise ValueError('Failed request archive verification failed')
    prepared = copy.deepcopy(normal['prepared'])
    prepared['context']['_harness_control'] = {
        **prepared['context'].get('_harness_control', {}), 'operation_id': generation}
    prepared['context']['verified_pending_tool_calls'] = calls
    prepared['task'] += ('\nFresh request after a host-rejected hash format error. The failed native context '
        'and output are superseded. Return exactly verified_pending_tool_calls using their exact '
        'host-verified hashes after checking every character. No call in that rejected batch executed. '
        'Preserve all earlier completed reading and notes. Do not repeat completed work.')
    prepared['schema'] = {'allOf': [prepared['schema'], {'type': 'object',
        'properties': {'tool_calls': {'type': 'array', 'items': {'enum': calls},
            'minItems': len(calls), 'maxItems': len(calls), 'enum': [calls]}},
        'required': ['tool_calls'], 'additionalProperties': False}]}
    successor = prepared_successor(store, key, {'status': 'dispatching', 'turn': progress['turn'],
        'operation_id': generation, 'prepared': prepared, 'provider_id': normal['provider_id'],
        'model': normal['model'], 'created_at': now(), 'dispatched_at': now(),
        'hash_reset_of': expected_operation_id, 'prepared_sha256': digest(prepared)})
    receipt = {'id': generation, 'run_id': run_id, 'handoff_id': key,
        'source_operation_id': expected_operation_id, 'source_native_key': native_key,
        'source_request_id': source['request_id'], 'source_receipt_sha256': source['receipt_sha256'],
        'archive_path': str(path), 'archive_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        'original_prepared_sha256': digest(normal['prepared']), 'prepared_sha256': digest(prepared),
        'retained_tool_results': len(progress.get('tool_results', [])),
        'tool_state_sha256': digest([progress.get('seen'), progress.get('tool_results'), progress.get('working_context_commit')]),
        'created_at': now()}
    def claim(current):
        if current != progress or store.get('runs', run_id)['status'] != 'blocked':
            raise ValueError('Request changed before reset; nothing replaced')
        current['normal_request'] = successor
        current.pop('tool_request', None)
        current.setdefault('hash_request_resets', []).append(receipt)
        return current
    store.mutate('handoff_progress', key, claim)
    store.put('request_hash_resets', generation, receipt)
    store.event(run_id, 'handoff.hash_request_reset',
        'Archived one rejected hash request and prepared a fresh bounded request; prior readings and accounting retained', receipt)
    (archive / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    return receipt
