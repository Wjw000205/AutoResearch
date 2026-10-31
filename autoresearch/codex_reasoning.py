"""ChatGPT-authenticated Codex as the research roles' JSON reasoning backend.

Each bounded request has an isolated native thread and a durable receipt. Role
history and RAG stay under the harness's context budget; native thread history
is only used to recover the exact original turn. No HTTP-provider fallback.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time

from .local_codex import LocalCodexClient, LocalCodexBlocked, close_after_operation
from .storage import now, redact
from .codex_accounting import capture_usage
from . import native_timeout_readback


DEFAULT_PROVIDER = {
    'id': 'local_codex', 'name': '本机 Codex · ChatGPT 套餐', 'protocol': 'local_codex',
    'base_url': '', 'api_key_env': '', 'model': 'gpt-6-astra',
    'context_window': 256000, 'max_output_tokens': 16000,
    'reasoning_effort': 'high', 'timeout_seconds': 1800,
    'deep_reasoning_effort': 'max', 'deep_timeout_seconds': 3600,
    'input_price': None, 'output_price': None, 'cached_input_price': None,
    'cache_write_price': None, 'price_verified': False,
    'prompt_cache_enabled': False, 'responses_streaming': False,
}

# Native Structured Outputs requires closed objects. Research tool arguments
# and experiment parameters intentionally have open dictionaries, so transport
# them as JSON text and validate the decoded object against the original schema.
TRANSPORT_SCHEMA = {'type': 'object', 'properties': {'result_json': {'type': 'string'}},
                    'required': ['result_json'], 'additionalProperties': False}
ENVELOPE_INSTRUCTION = ('\n\nTRANSPORT CONTRACT: Your final output must be an object with exactly one '
    'string field result_json. Put the complete JSON response requested above, including tool_calls '
    'if needed, INSIDE that string using JSON escaping. The inner JSON must satisfy the original '
    'task schema. Before responding, check every required field and data type in the inner JSON. '
    'Example transport: {"result_json":"{\\"answer\\":\\"ready\\"}"}. '
    'Do not add a result_json field to the inner task object. This envelope is only for transport.')


def profile(store, run_id, provider, context):
    stage = store.get('runs', run_id, {}).get('stage')
    purpose = str(context.get('_purpose', '')).split(':')[0]
    # A literature subtask keeps its own profile even inside innovation/design.
    # The parent stage is only a fallback for requests without a known purpose.
    supporting = purpose in {'literature', 'literature_reading', 'literature_query_plan', 'literature_screen',
        'evidence', 'related_work_queries', 'paper_visual', 'context_evidence', 'deep_paper'}
    deep = not supporting and (stage in {'innovation', 'design'} or purpose in {
        'innovation_candidate', 'innovation_compare', 'design'})
    effort = provider.get('deep_reasoning_effort', 'max') if deep else provider.get('reasoning_effort', 'high')
    if supporting and context.get('literature_reasoning_effort') in {'low', 'medium', 'high', 'xhigh', 'max'}:
        effort = context['literature_reasoning_effort']
    return {'model': provider['model'],
            'effort': effort,
            'timeout': provider.get('deep_timeout_seconds', 3600) if deep else provider.get('timeout_seconds', 1800),
            'stage': stage, 'purpose': context.get('_purpose', ''),
            'profile': 'deep' if deep else 'standard'}


def key_for(intelligence, run_id, role, task, context, schema):
    operation = intelligence._operation_identity(role, task, context, schema)
    key = hashlib.sha256(json.dumps([run_id, role, operation, 'codex-reasoning-v1']).encode()).hexdigest()
    return key, operation


def completed(intelligence, run_id, role, task, context, schema, model=None):
    key, _ = key_for(intelligence, run_id, role, task, context, schema)
    saved = intelligence.store.get('codex_reasoning', key, {})
    if saved.get('result') and (model is None or saved['selection']['model'] == model):
        return copy.deepcopy(saved['result'])
    if saved and (model is None or saved['selection']['model'] == model):
        from .native_retained_response import recover
        return recover(intelligence, saved)
    return None


async def connection(settings):
    blocked = [p.get('api_key_env', '') for p in settings.get('providers', [])]
    return await LocalCodexClient(blocked_env_names=[*blocked, 'OPENAI_API_KEY', 'CODEX_API_KEY']).account_status()


async def _prepare_context(intelligence, run_id, role, task, context, schema, provider, images):
    """Compile and archive input without blocking the application event loop."""
    from .intelligence import ProviderError, token_estimate
    from .image_inputs import tokens

    def prepare():
        # Store opens each SQLite connection in the calling thread and guards
        # writes with its RLock. Keep the original transcript/snapshot writes;
        # compilation neither reserves budget nor sends a native request.
        prepared_context = intelligence._context(run_id, role, task, context, provider, schema)
        system, user, cap = prepared_context
        limit = int(intelligence.store.get('runs', run_id, {}).get('config', {}).get('context_tokens', 256000))
        instruction = system + '\n\n' + user + ENVELOPE_INSTRUCTION
        if token_estimate(instruction) + tokens(images) > min(limit, int(provider['context_window']) - cap - 256):
            raise ProviderError('Task and JSON schema exceed the configured context budget')
        from . import implementation_indexed_review as indexed
        if context.get(indexed.PROTOCOL) == indexed.VERSION and indexed.CONTRACT in context:
            indexed.assert_input(context, instruction,
                prepared={'role': role, 'task': task, 'context': context, 'schema': schema})
        from .memory_capture import PreparedInstruction
        return PreparedInstruction(instruction, cap, getattr(prepared_context, 'memory_capture_binding', None))

    preparation = asyncio.create_task(asyncio.to_thread(prepare))
    try:
        return await asyncio.shield(preparation)
    except asyncio.CancelledError:
        # Cancelling an await cannot stop its Python worker. Retain the caller's
        # per-request lock until these audit writes finish so a resumed request
        # cannot compile the same input concurrently. Cancellation still wins
        # over a worker error and no reservation/dispatch follows it.
        while not preparation.done():
            try:
                await asyncio.shield(preparation)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not preparation.cancelled():
            preparation.exception()
        raise


async def ask(intelligence, run_id, role, task, context, schema, provider):
    from .intelligence import ProviderError, normalize_usage, parse_provider_json
    store = intelligence.store
    from .image_inputs import resolve
    images = resolve(store, run_id, context)
    key, operation = key_for(intelligence, run_id, role, task, context, schema)
    lock = intelligence._locks.setdefault('codex:' + key, asyncio.Lock())

    def active():
        current = store.get('runs', run_id)
        if not current or current.get('status') != 'running':
            raise asyncio.CancelledError()

    async with lock:
        saved = store.get('codex_reasoning', key)
        chosen = profile(store, run_id, provider, context)
        if saved:
            # Continue the original model/effort and frozen input after interruption.
            if saved['selection']['model'] != provider['model']:
                raise ProviderError('本机 Codex 原请求模型与当前配置不同；保留原回执，未重复提交。')
            if saved.get('result'):
                return copy.deepcopy(saved['result'])
            from .native_retained_response import recover
            retained = recover(intelligence, saved)
            if retained is not None:
                saved['result'] = retained
                store.put('codex_reasoning', key, saved)
                store.event(run_id, 'provider.retained_response_recovered',
                    '已从原完成回执恢复结构化回复；未重发模型请求，原始输出和费用保持不变。',
                    {'request_id': retained['request_id'], 'transcript_id': retained['transcript_id'],
                     'json_format_repair': retained.get('json_format_repair'),
                     'recovery': retained['retained_response_recovery']})
                return retained
            chosen = saved['selection']
        else:
            active()
            prepared_instruction = await _prepare_context(
                intelligence, run_id, role, task, context, schema, provider, images)
            instruction, cap = prepared_instruction
            # Pause/cancel can now run while preparation is in the worker. Its
            # audit snapshots are not authority to commit or submit a request.
            active()
            saved = {'id': key, 'run_id': run_id, 'role': role, 'operation_id': operation,
                     'selection': chosen, 'instruction': instruction,
                     'output_limit': cap, 'schema': schema, 'transport_schema': TRANSPORT_SCHEMA, 'created_at': now()}
            binding = getattr(prepared_instruction, 'memory_capture_binding', None)
            if binding:
                saved['memory_capture_binding'] = copy.deepcopy(binding)
            store.put('codex_reasoning', key, saved)
        # One short-lived process per request. Long reasoning waits keep consuming
        # events and checking cancellation rather than using the HTTP read timeout.
        blocked = [p.get('api_key_env', '') for p in intelligence.settings.get('providers', [])]
        client = LocalCodexClient(timeout=chosen['timeout'], request_timeout=60,
            blocked_env_names=[*blocked, 'OPENAI_API_KEY', 'CODEX_API_KEY'])
        workspace = (store.path.parent / 'codex-reasoning' / key / role).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        native_images = []
        if images:
            from .evidence_tools import _plain_path
            for image in images:
                path = workspace / (image['sha256'] + '.png')
                _plain_path(path, store.path.parent.resolve())
                if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != image['sha256']:
                    raise ProviderError('Native vision image hash changed')
                if not path.exists():
                    path.write_bytes(image['data'])
                native_images.append({'path': str(path.resolve()), 'sha256': image['sha256']})

        def save_record():
            saved['updated_at'] = now()
            store.put('codex_reasoning', key, saved)

        manifest_status = None
        def persist(receipt):
            nonlocal manifest_status
            native_timeout_readback.observe(saved, receipt)
            saved['checkpoint'] = copy.deepcopy(receipt)
            save_record()
            from .context_manifest import record_submission
            acknowledged = (receipt.get('run_id') == run_id and receipt.get('operation_id') == key
                and bool(receipt.get('thread_id')) and bool(receipt.get('turn_id')))
            status = (('completed' if receipt.get('status') == 'completed' else 'submitted')
                if acknowledged else 'submission_unknown' if receipt.get('turn_dispatched_at') else 'prepared')
            if status != manifest_status:
                record_submission(store, saved.get('memory_capture_binding'), saved['reservation']['id'],
                    status=status, wire_sha256=hashlib.sha256(saved['instruction'].encode('utf-8')).hexdigest(),
                    thread_id=receipt.get('thread_id'), turn_id=receipt.get('turn_id'))
                manifest_status = status
            usage = capture_usage(store, run_id, receipt)
            if usage:
                usage.update(role=role, purpose=context.get('_purpose'), backend='reasoning')
                store.put('codex_usage', usage['id'], usage)

        active()
        if not saved.get('reservation'):
            # The operation id allows a crash between admission and its mirror
            # write to reuse the same pending reservation, never count it twice.
            budget = store.get('budgets', run_id, {})
            previous = next((r for r in [*budget.get('pending', {}).values(), *budget.get('records', [])]
                if r.get('provider') == 'local_codex' and r.get('operation_id') == operation), None)
            saved['reservation'] = previous or intelligence._reserve(run_id, provider, role,
                saved['instruction'], saved['output_limit'], operation_id=operation)
            store.put('codex_reasoning', key, saved)
        from .context_manifest import record_submission
        record_submission(store, saved.get('memory_capture_binding'), saved['reservation']['id'],
            status='prepared', wire_sha256=hashlib.sha256(saved['instruction'].encode('utf-8')).hexdigest())
        manifest_status = 'prepared'
        started = time.monotonic()
        try:
            async def read_original(remaining):
                nonlocal client
                # The original timed-out process is closed before its replacement.
                # Only the actual completed readback client may own later cleanup.
                await client._close()
                client = LocalCodexClient(timeout=remaining, request_timeout=min(60, remaining),
                    blocked_env_names=[*blocked, 'OPENAI_API_KEY', 'CODEX_API_KEY'])
                active()
                return await client.run(run_id, workspace, saved['instruction'], operation_id=key,
                    checkpoint=copy.deepcopy(saved['checkpoint']), on_progress=persist, check_active=active,
                    model=chosen['model'], effort=chosen['effort'], reasoning_only=True,
                    output_schema=saved.get('transport_schema', saved['schema']), keep_open=True,
                    **({'image_inputs': native_images} if native_images else {}))

            try:
                if native_timeout_readback.FIELD in saved:
                    native_timeout_readback.validate(saved, store=store)
                receipt = await client.run(run_id, workspace, saved['instruction'], operation_id=key,
                    checkpoint=saved.get('checkpoint'), on_progress=persist, check_active=active,
                    model=chosen['model'], effort=chosen['effort'], reasoning_only=True,
                    output_schema=saved.get('transport_schema', saved['schema']), keep_open=True,
                    **({'image_inputs': native_images} if native_images else {}))
                persist(receipt)
                receipt = await native_timeout_readback.recover(saved, store=store,
                    readback=read_original, save=save_record, check_active=active)
            except LocalCodexBlocked as exc:
                raise ProviderError('本机 Codex 请求未完成：' + exc.code + '；未回退到 API。') from exc
            if receipt.get('status') != 'completed' or receipt.get('public_output_truncated'):
                from .local_codex import LocalCodexClient as NativeClient
                if NativeClient._unsubmitted_initialization(receipt, True):
                    raise ProviderError('本机 Codex 初始化尚未完成：' + str(receipt.get('error_code'))
                        + '；尚未提交模型回合，恢复时继续原请求的首次初始化。')
                raise ProviderError('本机 Codex 请求尚无完整结果：' + str(receipt.get('error_code') or receipt.get('status'))
                    + '；原回执已保存，恢复时读取原会话，不回退到 API。')
            messages = receipt.get('public_messages', [])
            final = [m for m in messages if m.get('phase') == 'final_answer']
            if not final:
                final = [m for m in messages if m.get('phase') is None]
            text = str(redact('\n'.join(m.get('text', '') for m in final)))
            metadata = {'provider_status': 'completed', 'output_truncated': False,
                        'output_limit_reached': False, 'source': 'local_codex_app_server'}
            if saved.get('transport_schema'):
                envelope, _ = parse_provider_json(text, TRANSPORT_SCHEMA, metadata)
                text = envelope['result_json']
                metadata['transport_envelope'] = 'result_json_v1'
            output_id = key + ':output'
            store.put('transcripts', output_id, {'id': output_id, 'run_id': run_id, 'role': role,
                'kind': 'output', 'text': text, 'request_id': saved['reservation']['id'], 'created_at': now(),
                'provider': provider['id'], 'model': chosen['model'], 'reasoning_effort': chosen['effort'],
                'operation_id': operation, 'request_scope': intelligence._request_scope(context),
                'request_output_token_limit': saved['output_limit'], 'response_metadata': metadata})
            raw = (receipt.get('usage') or {}).get('last') or {}
            usage = normalize_usage('chat', {'prompt_tokens': raw.get('inputTokens', 0),
                'completion_tokens': raw.get('outputTokens', 0),
                'prompt_tokens_details': {'cached_tokens': raw.get('cachedInputTokens', 0)}})
            intelligence._settle(saved['reservation'], usage, 0, verified=False, status='completed',
                provenance='local_codex_chatgpt', billing_scope='chatgpt_subscription',
                cost_usd=None, cost_estimated=False, cost_status='not_reported_by_local_codex',
                response_metadata=metadata, thread_id=receipt.get('thread_id'), turn_id=receipt.get('turn_id'))
            value, repair = parse_provider_json(text, schema, metadata) if schema is not None else (None, None)
            result = {'text': text, 'json': value, 'provider': provider['id'], 'model': chosen['model'],
                'reasoning_effort': chosen['effort'], 'timeout_seconds': chosen['timeout'], 'usage': usage,
                'cost_usd': None, 'cost_estimated': False, 'price_verified': False, 'cache_hit': False,
                'request_id': saved['reservation']['id'], 'transcript_id': output_id,
                'latency_ms': round((time.monotonic() - started) * 1000),
                'provenance': 'local_codex_chatgpt', 'response_metadata': metadata,
                'thread_id': receipt.get('thread_id'), 'turn_id': receipt.get('turn_id'),
                **({'json_format_repair': repair} if repair else {})}
            saved['result'] = result
            store.put('codex_reasoning', key, saved)
            store.event(run_id, 'provider.completed', f'{role} 本机 Codex {chosen["effort"]} 输出已保存',
                {'provider': 'local_codex', 'request_id': result['request_id'], 'billing_scope': 'chatgpt_subscription'})
            from .codex_cleanup import cleanup_record
            await cleanup_record(store, key, client=client)
            return result
        finally:
            await close_after_operation(client)
