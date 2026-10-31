"""Read-only recovery from an exact completed native receipt and settlement.

No native process, model dispatch, billing change or transcript rewrite occurs.
Derived JSON still passes the original schema and subsequent scientific gates.
"""
from __future__ import annotations
from .prose_schema import validator as response_validator

import copy
import hashlib
import json


def recover(intelligence, saved, *, schema_repair=None, strict_source=False):
    from .codex_reasoning import TRANSPORT_SCHEMA
    from .intelligence import ProviderError, parse_provider_json
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError
    from .json_format_recovery import _unique_object, completed_output
    from .storage import redact

    checkpoint = saved.get('checkpoint', {})
    if (checkpoint.get('status') != 'completed' or checkpoint.get('public_output_truncated')
            or checkpoint.get('error_code') or checkpoint.get('native_tool_violation')):
        return None
    store = intelligence.store
    transcript = store.get('transcripts', saved['id'] + ':output')
    reservation = saved.get('reservation', {})
    matches = [r for r in store.get('budgets', saved['run_id'], {}).get('records', [])
               if r.get('id') == reservation.get('id')]
    if not transcript or len(matches) != 1:
        return None
    ledger = matches[0]
    model = saved['selection']['model']
    fields = {'run_id': saved['run_id'], 'role': saved['role'], 'operation_id': saved['operation_id'],
              'provider': 'local_codex', 'model': model}
    if (any(transcript.get(k) != v or ledger.get(k) != v for k, v in fields.items())
            or transcript.get('kind') != 'output' or ledger.get('status') != 'completed'
            or transcript.get('request_id') != reservation.get('id')
            or ledger.get('thread_id') != checkpoint.get('thread_id')
            or ledger.get('turn_id') != checkpoint.get('turn_id')):
        return None
    metadata = transcript.get('response_metadata', {})
    if not completed_output(metadata) or ledger.get('response_metadata') != metadata:
        return None
    def verified_source():
        budget = store.get('budgets', saved['run_id'], {})
        operations = [r for r in budget.get('records', [])
                      if r.get('operation_id') == saved['operation_id']]
        pending = budget.get('pending', {})
        return (isinstance(ledger.get('usage'), dict)
            and store.get('usage', reservation.get('id')) == ledger
            and len(operations) == 1 and operations[0] == ledger
            and not any(r.get('id') == reservation.get('id')
                        or r.get('operation_id') == saved['operation_id'] for r in pending.values())
            and not ledger.get('moderation_required') and not ledger.get('error')
            and ledger.get('provenance') == 'local_codex_chatgpt'
            and ledger.get('billing_scope') == 'chatgpt_subscription'
            and ledger.get('cost_usd') is None
            and checkpoint.get('run_id') == saved['run_id']
            and checkpoint.get('operation_id') == saved['id']
            and checkpoint.get('version') == 1
            and checkpoint.get('requested_model') == model
            and checkpoint.get('auth_mode') == 'chatgpt'
            and checkpoint.get('reasoning_only') is True
            and bool(checkpoint.get('thread_id')) and bool(checkpoint.get('turn_id'))
            and transcript.get('id') == saved['id'] + ':output')
    if strict_source and not verified_source():
        return None
    messages = checkpoint.get('public_messages', [])
    final = [m for m in messages if m.get('phase') == 'final_answer']
    if not final:
        final = [m for m in messages if m.get('phase') is None]
    if not final or any(not isinstance(m.get('text'), str) for m in final):
        return None
    text = str(redact('\n'.join(m['text'] for m in final)))
    try:
        if saved.get('transport_schema'):
            envelope, _ = parse_provider_json(text, TRANSPORT_SCHEMA, metadata)
            text = envelope['result_json']
        if text != transcript.get('text'):
            return None
        try:
            value, repair = parse_provider_json(text, saved['schema'], metadata)
            if (repair and repair.get('kind') == 'extra_tool_call_closing_brace'
                    and not verified_source()):
                return None
        except ProviderError:
            # A callback may derive a narrowly audited value from a valid JSON
            # object, never finish malformed syntax or infer a model outcome.
            if not callable(schema_repair):
                return None
            Draft202012Validator.check_schema(saved['schema'])
            original = json.loads(text, object_pairs_hook=_unique_object,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
            if not isinstance(original, dict) or not verified_source():
                return None
            candidate = schema_repair(text, copy.deepcopy(metadata))
            if (not isinstance(candidate, tuple) or len(candidate) != 2
                    or not isinstance(candidate[0], dict) or not isinstance(candidate[1], dict)
                    or not response_validator(saved['schema']).is_valid(candidate[0])):
                return None
            value, repair = copy.deepcopy(candidate)
    except (ProviderError, ValueError, KeyError, TypeError, SchemaError):
        return None
    result = {'text': text, 'json': value, 'provider': 'local_codex', 'model': model,
        'reasoning_effort': saved['selection']['effort'], 'timeout_seconds': saved['selection']['timeout'],
        'usage': copy.deepcopy(ledger.get('usage', {})), 'cost_usd': None,
        'cost_estimated': False, 'price_verified': False, 'cache_hit': True,
        'request_id': reservation['id'], 'transcript_id': transcript['id'], 'latency_ms': None,
        'provenance': 'local_codex_chatgpt', 'response_metadata': copy.deepcopy(metadata),
        'thread_id': checkpoint.get('thread_id'), 'turn_id': checkpoint.get('turn_id'),
        'retained_response_recovery': {'version': 1, 'transcript_id': transcript['id'],
            'original_sha256': hashlib.sha256(text.encode()).hexdigest(), 'charges_added': 0,
            'scope': '原完成回执、原始输出和唯一费用结算匹配；未重发模型请求或修改原文。'}}
    if repair:
        result['json_format_repair'] = repair
    return result
