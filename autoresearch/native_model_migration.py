"""Explicit model changes after a read-back, output-free native service failure.

Changing settings alone never authorizes dispatch. A subsequent start binds the
old request and destination, keeps effort/timeouts, and permits one successor.
"""
from __future__ import annotations

import copy

from . import codex_reasoning
from .native_failure import retryable_failure
from .native_normal_recovery import _fresh_generation, _prepared, _source, _target
from .storage import now, uid


def _selection(store, ai, run_id, normal):
    provider = ai.provider_for(normal['prepared']['role'])
    if provider.get('id') != 'local_codex':
        return None
    selected = codex_reasoning.profile(store, run_id, provider, normal['prepared']['context'])
    return {field: selected[field] for field in ('model', 'effort', 'timeout')}


def resume_target(store, ai, run_id, progress):
    """Only a first failed normal request is eligible for model migration."""
    normal = progress.get('normal_request') or {}
    target = _target(normal, progress.get('turn'))
    if (target is None or progress.get('mode') == 'final_only'
            or any(progress.get(k) for k in ('native_model_migrations', 'native_normal_request_history',
                                             'finalization_history'))
            or any(normal.get(k) for k in ('native_interruption_of', 'native_model_migration_of',
                                           'json_syntax_correction_of'))):
        return None
    prepared = normal['prepared']
    native_key, operation = codex_reasoning.key_for(ai, run_id, prepared['role'],
        prepared['task'], prepared['context'], prepared['schema'])
    saved = store.get('codex_reasoning', native_key, {})
    cp, previous = saved.get('checkpoint', {}), saved.get('selection', {})
    destination = _selection(store, ai, run_id, normal)
    if (not destination or destination['model'] == normal.get('model')
            or any(destination[k] != previous.get(k) for k in ('effort', 'timeout'))
            or saved.get('id') != native_key or saved.get('operation_id') != operation
            or saved.get('run_id') != run_id or saved.get('role') != prepared['role']
            or saved.get('schema') != prepared['schema'] or saved.get('result') is not None
            or previous.get('model') != normal.get('model') or not saved.get('reservation')
            or cp.get('operation_id') != native_key or cp.get('run_id') != run_id
            or not cp.get('thread_id') or not cp.get('turn_id') or not retryable_failure(cp)):
        return None
    return {'target': target, 'destination': destination, 'native_key': native_key}


async def recover(harness, run_id, key, progress):
    """Read the old turn using its original model, then archive and supersede it."""
    from .engine import ResearchBlocked, digest
    from .intelligence import ProviderError
    engine, ai = harness.engine, harness.intelligence
    store = engine.store
    run = store.get('runs', run_id, {})
    generation = store.get('operator_resumes', run.get('state', {}).get('resume_generation'), {})
    binding = resume_target(store, ai, run_id, progress)
    if (binding is None or run.get('status') != 'running'
            or generation.get('kind') != 'explicit_start_command' or generation.get('run_id') != run_id
            or generation.get('native_model_targets', {}).get(key) != binding
            or generation.get('targets', {}).get(key) != binding['target']
            or store.get('handoffs', key, {}).get('stage') != run.get('stage')
            or not _fresh_generation(generation, store.get('codex_reasoning', binding['native_key']))):
        return False
    original_progress = copy.deepcopy(progress)
    normal, prepared = progress['normal_request'], progress['normal_request']['prepared']
    provider = {**ai.provider_for(prepared['role']), 'model': normal['model']}
    engine.check_active(run_id)
    # The verified existing checkpoint has both native IDs: this call can only
    # read the original turn. It cannot create a thread or reserve another call.
    try:
        response = await codex_reasoning.ask(ai, run_id, provider=provider, **prepared)
    except ProviderError:
        response = None
    if response is not None:
        normal.update(status='response_saved', response=response, recovered_at=now())
        harness._save_progress(run_id, key, progress)
        return True
    source = _source(store, ai, run_id, normal)
    if source is None or source['status'] != 'failed':
        raise ResearchBlocked('模型切换前未能确认原请求已失败且无回复；保留原回执，未发送新请求。')
    if resume_target(store, ai, run_id, progress) != binding:
        raise ResearchBlocked('模型切换目标已改变；未发送新请求。')
    operation = uid()
    successor = _prepared(normal, operation)
    preview = ai.preflight_batch(run_id, [successor])
    if preview['request_count'] != 1 or preview['requests'][0]['max_output_tokens'] != source['effective_output_limit']:
        raise ResearchBlocked('模型切换后的输出额度发生变化；未发送新请求。')
    audit = {'previous': copy.deepcopy(normal), 'source': source, 'binding': binding,
        'resume_generation': generation['id'], 'new_operation_id': operation,
        'prepared_sha256': digest(successor), 'tool_results_sha256': digest(progress['tool_results']),
        'created_at': now(), 'source_disposition': 'superseded_without_response'}

    def claim(current):
        if current != original_progress:
            raise ResearchBlocked('模型切换时任务进度已改变；未发送新请求。')
        from .tool_request_recovery import prepared_successor
        current.update(native_model_migrations=[audit], normal_request=prepared_successor(store, key, {
            'status': 'ready', 'turn': progress['turn'], 'operation_id': operation, 'prepared': successor,
            'provider_id': 'local_codex', 'model': binding['destination']['model'],
            'native_model_migration_of': normal['operation_id'], 'created_at': now()}))
        return current

    engine.check_active(run_id)
    progress.update(store.mutate('handoff_progress', key, claim))
    store.event(run_id, 'handoff.native_model_migration',
        '原请求已回读确认为过载等临时故障且无回复；按本次恢复指令切换模型，保持推理强度、工具进度和原始回执。',
        {'handoff_id': key, 'resume_generation': generation['id'], 'source': source,
         'destination': binding['destination'], 'new_operation_id': operation})
    return True


def validate(engine, ai, run_id, key, progress, preview):
    from .engine import ResearchBlocked, digest
    audits = progress.get('native_model_migrations', [])
    valid = isinstance(audits, list) and len(audits) == 1 and isinstance(audits[0], dict)
    if valid:
        audit, normal = audits[0], progress['normal_request']
        previous, binding = audit['previous'], audit['binding']
        source = _source(engine.store, ai, run_id, previous)
        generation = engine.store.get('operator_resumes', audit.get('resume_generation'), {})
        operation = normal.get('operation_id')
        expected = _prepared(previous, operation)
        valid = (progress.get('mode') != 'final_only' and source is not None and source['status'] == 'failed'
            and source == audit.get('source') and binding['native_key'] == source['native_key']
            and generation.get('kind') == 'explicit_start_command' and generation.get('run_id') == run_id
            and generation.get('native_model_targets', {}).get(key) == binding
            and generation.get('targets', {}).get(key) == binding['target']
            and binding['target'] == _target(previous, progress['turn'])
            and _fresh_generation(generation, engine.store.get('codex_reasoning', source['native_key']))
            and binding['destination'] == _selection(engine.store, ai, run_id, normal)
            and binding['destination']['model'] != previous['model']
            and all(binding['destination'][field] == engine.store.get('codex_reasoning',
                source['native_key'])['selection'].get(field) for field in ('effort', 'timeout'))
            and isinstance(operation, str) and bool(operation) and operation != previous['operation_id']
            and operation == audit.get('new_operation_id')
            and normal.get('native_model_migration_of') == previous['operation_id']
            and normal.get('turn') == previous['turn'] == progress['turn']
            and normal.get('provider_id') == 'local_codex' and normal.get('model') == binding['destination']['model']
            and normal.get('prepared') == expected and audit.get('prepared_sha256') == digest(expected)
            and audit.get('tool_results_sha256') == digest(progress['tool_results'])
            and preview.get('request_count') == 1 and len(preview.get('requests', [])) == 1
            and preview['requests'][0]['max_output_tokens'] == source['effective_output_limit'])
    if not valid:
        raise ResearchBlocked('模型切换的原回执、目标或冻结任务已改变；未发送新请求。')
