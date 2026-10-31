"""One structured extraction retry after an output-free native overload.

The original turn must first be read back as terminal. Its receipt, reservation,
task, schema and source pages remain intact. Unknown/policy/partial-output faults
cannot authorize a new request. The retry still uses normal admission gates.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime

from .context_compiler import fingerprint
from .storage import now


def _source(intelligence, run_id, plan):
    from .codex_reasoning import TRANSPORT_SCHEMA, key_for, profile
    from .context_reduction import saved_identity, supported
    prepared = plan.get('prepared', {})
    context = prepared.get('context', {})
    try:
        identity = saved_identity(plan)
        if (plan.get('run_id') != run_id or plan.get('id') != f'{run_id}:context-evidence:{identity}'
                or not supported(prepared) or context.get('source_pages') != plan.get('pages')
                or context.get('_purpose') != 'context_evidence:' + identity
                or context.get('_harness_control') != {'mode': 'final_only', 'operation_id': identity}):
            return None
        provider = intelligence.provider_for(prepared['role'])
        if provider.get('id') != 'local_codex' or provider.get('protocol') != 'local_codex':
            return None
        key, operation = key_for(intelligence, run_id, **prepared)
        native = intelligence.store.get('codex_reasoning', key, {})
        cp, reservation = native.get('checkpoint', {}), native.get('reservation', {})
        selected = native.get('selection', {})
        chosen = profile(intelligence.store, run_id, provider, context)
        errors = cp.get('server_errors')
        if (native.get('id') != key or native.get('run_id') != run_id or native.get('role') != prepared['role']
                or native.get('operation_id') != operation or native.get('schema') != prepared['schema']
                or native.get('transport_schema') != TRANSPORT_SCHEMA or native.get('result') is not None
                or any(chosen.get(field) != selected.get(field) for field in ('model', 'effort', 'timeout', 'purpose'))
                or cp.get('status') != 'failed' or cp.get('error_code') != 'codex_turn_failed'
                or cp.get('recovered_read_only') is not True or cp.get('version') != 1
                or cp.get('model_provider') != 'openai' or cp.get('auth_mode') != 'chatgpt'
                or cp.get('reasoning_only') is not True or cp.get('run_id') != run_id
                or cp.get('operation_id') != key or not cp.get('thread_id') or not cp.get('turn_id')
                or cp.get('workspace') != str((intelligence.store.path.parent / 'codex-reasoning' / key / prepared['role']).resolve())
                or cp.get('requested_model') != selected.get('model')
                or cp.get('requested_effort') != selected.get('effort')
                or cp.get('public_output_truncated') or cp.get('native_tool_violation')
                or cp.get('public_messages') != [] or cp.get('activities')
                or not isinstance(errors, list) or not errors
                or any(not isinstance(error, dict) or error.get('category') != 'serverOverloaded'
                       or error.get('will_retry') is not False for error in errors)
                or not isinstance(native.get('instruction'), str)
                or cp.get('instruction_sha256') != hashlib.sha256(native['instruction'].encode()).hexdigest()
                or cp.get('output_schema_sha256') != hashlib.sha256(json.dumps(
                    TRANSPORT_SCHEMA, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                or reservation.get('provider') != 'local_codex' or reservation.get('run_id') != run_id
                or reservation.get('role') != prepared['role'] or reservation.get('model') != selected.get('model')
                or reservation.get('operation_id') != operation or not reservation.get('id')
                or type(native.get('output_limit')) is not int
                or reservation.get('request_output_token_limit') != native['output_limit']):
            return None
        failure = cp.get('first_failure', {})
        if (failure.get('thread_id') != cp['thread_id'] or failure.get('turn_id') != cp['turn_id']
                or datetime.fromisoformat(failure['observed_at']).tzinfo is None
                or datetime.fromisoformat(cp['turn_dispatched_at']).tzinfo is None
                or intelligence.store.get('transcripts', key + ':output') is not None):
            return None
        ledger = intelligence.store.get('budgets', run_id, {})
        entries = [*ledger.get('pending', {}).values(), *ledger.get('records', [])]
        if sum(entry == reservation for entry in entries) != 1:
            return None
        return {'native_key': key, 'native_sha256': fingerprint(native), 'operation_id': operation,
            'request_id': reservation['id'], 'thread_id': cp['thread_id'], 'turn_id': cp['turn_id'],
            'model': selected['model'], 'effort': selected['effort'], 'timeout': selected['timeout'],
            'output_limit': native['output_limit'], 'instruction_sha256': cp['instruction_sha256'],
            'prepared_sha256': fingerprint(prepared), 'status': 'failed', 'category': 'serverOverloaded'}
    except (KeyError, TypeError, ValueError):
        return None


def _prepared(plan):
    result = copy.deepcopy(plan['prepared'])
    operation = fingerprint([plan['id'], 'native-evidence-overload-retry', 1])
    result['context']['_harness_control']['operation_id'] = operation
    return result


def load(intelligence, run_id, plan):
    """Resume the same retry when it already exists; never allocate a second."""
    from .intelligence import ProviderError
    key = plan['id'] + ':native-overload-recovery:1'
    saved = intelligence.store.get('stage_contexts', key)
    if saved is None:
        return None
    source = _source(intelligence, run_id, plan)
    expected = _prepared(plan)
    if (source is None or saved.get('id') != key or saved.get('run_id') != run_id
            or saved.get('kind') != 'context_evidence_native_recovery'
            or saved.get('extraction_id') != plan['id'] or saved.get('source') != source
            or saved.get('prepared') != expected or saved.get('prepared_sha256') != fingerprint(expected)
            or saved.get('attempt_number') != 1 or saved.get('attempt_limit') != 1):
        raise ProviderError('Native evidence recovery identity changed; no new extraction dispatched')
    return copy.deepcopy(expected)


def claim(intelligence, run_id, plan, parent, *, hard_exceeded=False):
    """Called only after the original ask failed following native readback."""
    from .intelligence import ProviderError
    if load(intelligence, run_id, plan) is not None:
        return None  # An existing retry never grants another attempt on failure.
    source = _source(intelligence, run_id, plan)
    if source is None:
        return None
    prepared = _prepared(plan)
    preview = intelligence.preflight_batch(run_id, [prepared] + ([] if hard_exceeded else [parent]))
    if preview['requests'][0]['max_output_tokens'] != source['output_limit']:
        raise ProviderError('Native evidence recovery output allowance changed; no new extraction dispatched')
    if intelligence.store.get('runs', run_id, {}).get('status') != 'running':
        raise ProviderError('Research is not active; evidence recovery was not dispatched')
    key = plan['id'] + ':native-overload-recovery:1'
    audit = {'id': key, 'run_id': run_id, 'kind': 'context_evidence_native_recovery',
        'extraction_id': plan['id'], 'source': source, 'prepared': prepared,
        'prepared_sha256': fingerprint(prepared), 'attempt_number': 1, 'attempt_limit': 1,
        'created_at': now(), 'policy': 'Retry the same structured evidence task once after terminal readback. '
            'Original source, receipt and charged attempt retained; no raw-context bypass or scientific approval.'}
    def create(current):
        if current is not None:
            raise ProviderError('Evidence recovery already claimed; no concurrent duplicate dispatched')
        return audit
    intelligence.store.mutate('stage_contexts', key, create)
    intelligence.store.event(run_id, 'context.evidence_native_recovery',
        '原证据整理请求回读确认为服务繁忙且无输出；保持原任务、模型、原文和结构，继续整理一次。',
        {'extraction_id': plan['id'], 'source_request_id': source['request_id'],
         'source_native_key': source['native_key'], 'attempt_number': 1, 'attempt_limit': 1,
         'new_model_attempt': True, 'raw_context_bypass': False, 'scientific_claims_verified': False})
    return prepared
