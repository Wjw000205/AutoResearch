"""Bounded final continuations after explicit resume and native readback.

The original native turn is read back first. Output-free transient failures can
be superseded by a fresh request; unknown and in-progress turns remain blocked.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime

from .codex_reasoning import key_for, profile
from .native_failure import retryable_failure as _retryable_failure
from .storage import now, uid


MAX_CONTINUATIONS = 2
HEALTH_CONTINUATIONS = 1


def _fresh_failure_resume(generation, saved):
    try:
        checkpoint = saved['checkpoint']
        failure = checkpoint['first_failure']
        failed_at = datetime.fromisoformat(failure['observed_at'])
        resumed_at = datetime.fromisoformat(generation['created_at'])
        return (failed_at.tzinfo is not None and resumed_at.tzinfo is not None and failed_at < resumed_at
            and all(failure.get(field) == checkpoint.get(field) for field in ('thread_id', 'turn_id')))
    except (KeyError, TypeError, ValueError):
        return False


def _disposition(source):
    return {'source_disposition': 'superseded_without_response'} if source['status'] == 'failed' else {}


def _unique_json(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate diagnostic marker field')
        value[key] = item
    return value


def _health_source(store, run_id, source, *, native_key=None):
    """A subsequent real diagnostic admits one extra retry, never a result."""
    from .codex_reasoning import TRANSPORT_SCHEMA
    from .engine import digest
    failed = store.get('codex_reasoning', source['native_key'], {})
    failure_time = failed.get('checkpoint', {}).get('first_failure', {}).get('observed_at')
    try:
        failure_at = datetime.fromisoformat(failure_time)
        if failure_at.tzinfo is None:
            return None
    except (TypeError, ValueError):
        return None
    ledger = store.get('budgets', run_id, {})
    candidates = []
    keys = [native_key] if native_key else store.record_keys('codex_reasoning', run_id,
        predicate="json_extract(data,'$.kind')='transport_diagnostic' "
                  "AND json_type(data,'$.diagnostic_only')='true'")
    rows = (store.get('codex_reasoning', key, {}) for key in keys)
    for saved in rows:
        if saved.get('kind') != 'transport_diagnostic' or saved.get('diagnostic_only') is not True:
            continue
        cp, reservation = saved.get('checkpoint', {}), saved.get('reservation', {})
        selection = saved.get('selection', {})
        purpose = saved.get('purpose', '')
        if (not isinstance(purpose, str) or not purpose.startswith('diagnostic:transport_generation:')
                or cp.get('status') != 'completed' or cp.get('auth_mode') != 'chatgpt'
                or cp.get('version') != 1 or cp.get('model_provider') != 'openai'
                or cp.get('reasoning_only') is not True or cp.get('public_output_truncated')
                or cp.get('native_tool_violation') or cp.get('run_id') != run_id or saved.get('run_id') != run_id
                or cp.get('operation_id') != saved.get('id') or not cp.get('thread_id') or not cp.get('turn_id')
                or cp.get('requested_model') != source['model'] or selection.get('model') != source['model']
                or cp.get('requested_effort') != failed.get('selection', {}).get('effort')
                or selection.get('effort') != cp.get('requested_effort')
                or saved.get('transport_schema') != TRANSPORT_SCHEMA
                or not isinstance(saved.get('instruction'), str)
                or cp.get('instruction_sha256') != hashlib.sha256(saved['instruction'].encode()).hexdigest()
                or cp.get('output_schema_sha256') != hashlib.sha256(json.dumps(
                    TRANSPORT_SCHEMA, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                or reservation.get('provider') != 'local_codex' or reservation.get('run_id') != run_id
                or reservation.get('model') != source['model'] or reservation.get('role') != saved.get('role')
                or reservation.get('operation_id') != saved.get('operation_id')
                or reservation.get('controller_operation_id') != purpose):
            continue
        settled = [row for row in ledger.get('records', []) if row.get('id') == reservation.get('id')]
        if (len(settled) != 1 or settled[0].get('status') != 'completed'
                or settled[0].get('diagnostic_only') is not True or settled[0].get('purpose') != purpose
                or settled[0].get('provenance') != 'local_codex_chatgpt'
                or settled[0].get('thread_id') != cp['thread_id'] or settled[0].get('turn_id') != cp['turn_id']
                or any(settled[0].get(k) != v for k, v in reservation.items())
                or reservation.get('id') in ledger.get('pending', {})):
            continue
        try:
            completed_at = datetime.fromisoformat(cp['updated_at'])
            dispatched_at = datetime.fromisoformat(cp['turn_dispatched_at'])
            admitted_at = datetime.fromisoformat(reservation['created_at'])
            if not failure_at < admitted_at <= dispatched_at <= completed_at:
                continue
            messages = [m for m in cp.get('public_messages', []) if m.get('phase') == 'final_answer']
            if not messages:
                messages = [m for m in cp.get('public_messages', []) if m.get('phase') is None]
            # The transport diagnostic is a fixed marker. No repair, prose,
            # cached result or scientific judgment can substitute for it.
            if len(messages) != 1:
                continue
            envelope = json.loads(messages[0]['text'], object_pairs_hook=_unique_json)
            if set(envelope) != {'result_json'} or json.loads(
                    envelope['result_json'], object_pairs_hook=_unique_json) != {'ok': 'ready'}:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        candidates.append({'native_key': saved['id'], 'native_sha256': digest(saved),
            'request_id': reservation['id'], 'thread_id': cp['thread_id'], 'turn_id': cp['turn_id'],
            'completed_at': cp['updated_at'], 'scope': 'short_generation_only_not_long_reply_or_research_approval'})
    return max(candidates, key=lambda item: (item['completed_at'], item['native_key'])) if candidates else None


def _chain(progress):
    old = progress.get('finalization_history', [])
    prior_audits = progress.get('native_final_recovery_history', [])
    audit = progress.get('native_final_recovery')
    final = progress.get('finalization')
    if (not isinstance(final, dict) or not isinstance(old, list)
            or not isinstance(prior_audits, list) or len(old) > MAX_CONTINUATIONS + HEALTH_CONTINUATIONS
            or any(not isinstance(item, dict) for item in [*old, *prior_audits])):
        return None
    if not old:
        return ([final], []) if not audit and not prior_audits else None
    if not isinstance(audit, dict) or len(prior_audits) != len(old) - 1:
        return None
    return [*old, final], [*prior_audits, audit]


def _target(final):
    from .engine import digest
    if (final.get('response') is not None or not final.get('operation_id')
            or final.get('provider_id') != 'local_codex'
            or final.get('status') not in {'blocked', 'dispatching'}
            or not isinstance(final.get('prepared'), dict)):
        return None
    return {'operation_id': final['operation_id'], 'request_fingerprint': digest(final['prepared'])}


def resume_target(progress, *, engine=None, intelligence=None, run_id=None, key=None):
    chain = _chain(progress)
    if progress.get('mode') != 'final_only' or chain is None:
        return None
    count = len(chain[1])
    target = _target(chain[0][-1])
    if count < MAX_CONTINUATIONS:
        return target
    if (count != MAX_CONTINUATIONS or target is None or engine is None or intelligence is None
            or not _valid_chain(engine, intelligence, run_id, key, progress)):
        return None
    source = _source(engine.store, intelligence, run_id, chain[0][-1])
    health = _health_source(engine.store, run_id, source) if source else None
    return {**target, 'health_probe': health} if health else None


def _source(store, intelligence, run_id, final, *, normal=False, require_readback=True):
    from .engine import digest
    prepared = final.get('prepared', {})
    if (not isinstance(prepared, dict) or not isinstance(prepared.get('context'), dict)
            or not {'role', 'task', 'schema'} <= prepared.keys()):
        return None
    native_key, operation = key_for(intelligence, run_id, prepared['role'], prepared['task'],
                                    prepared['context'], prepared['schema'])
    saved = store.get('codex_reasoning', native_key, {})
    checkpoint = saved.get('checkpoint', {})
    reservation = saved.get('reservation', {})
    control = prepared['context'].get('_harness_control', {})
    if (saved.get('result') is not None or saved.get('run_id') != run_id
            or saved.get('role') != prepared['role'] or saved.get('operation_id') != operation
            or saved.get('schema') != prepared['schema']
            or saved.get('selection', {}).get('model') != final.get('model')
            or (not normal and (control.get('mode') != 'final_only'
                                or control.get('operation_id') != final['operation_id']))
            or (normal and control.get('mode') not in {None, 'continue'})
            or not (checkpoint.get('status') == 'interrupted' or _retryable_failure(checkpoint))
            or require_readback and checkpoint.get('recovered_read_only') is not True
            or checkpoint.get('operation_id') != native_key or checkpoint.get('run_id') != run_id
            or checkpoint.get('requested_model') != final.get('model')
            or not saved.get('selection', {}).get('effort')
            or checkpoint.get('requested_effort') != saved['selection']['effort']
            or not checkpoint.get('thread_id') or not checkpoint.get('turn_id')
            or checkpoint.get('reasoning_only') is not True or checkpoint.get('auth_mode') != 'chatgpt'
            or checkpoint.get('public_output_truncated') or checkpoint.get('native_tool_violation')
            or reservation.get('provider') != 'local_codex' or reservation.get('operation_id') != operation
            or reservation.get('run_id') != run_id or reservation.get('role') != prepared['role']
            or reservation.get('model') != final['model'] or not reservation.get('id')
            or type(saved.get('output_limit')) is not int
            or reservation.get('request_output_token_limit') != saved['output_limit']):
        return None
    if checkpoint['status'] == 'failed' and (
            saved.get('id') != native_key or checkpoint.get('version') != 1
            or checkpoint.get('model_provider') != 'openai'
            or not isinstance(saved.get('instruction'), str)
            or checkpoint.get('instruction_sha256') != hashlib.sha256(saved['instruction'].encode()).hexdigest()
            or checkpoint.get('output_schema_sha256') != hashlib.sha256(json.dumps(
                saved.get('transport_schema', saved['schema']), sort_keys=True, ensure_ascii=False).encode()).hexdigest()):
        return None
    if any(m.get('phase') in {None, 'final_answer'} and str(m.get('text', '')).strip()
           for m in checkpoint.get('public_messages', [])):
        return None
    ledger = store.get('budgets', run_id, {})
    rows = [*ledger.get('pending', {}).values(), *ledger.get('records', [])]
    if sum(row == reservation for row in rows) != 1:
        return None
    if store.has_output_transcript(run_id, request_id=reservation['id'], operation_id=operation):
        return None
    return {'native_key': native_key, 'native_sha256': digest(saved), 'request_id': reservation['id'],
            'thread_id': checkpoint['thread_id'], 'turn_id': checkpoint['turn_id'],
            'model': final['model'], 'effective_output_limit': saved['output_limit'],
            'status': checkpoint['status'], 'usage_scope': 'original_subscription_usage_retained'}


def _profile_matches(store, intelligence, run_id, final, source):
    provider = intelligence.provider_for(final['prepared']['role'])
    saved = store.get('codex_reasoning', source['native_key'], {})
    selected = saved.get('selection', {})
    chosen = profile(store, run_id, provider, final['prepared']['context'])
    return provider['id'] == 'local_codex' and all(
        chosen.get(field) == selected.get(field) for field in ('model', 'effort', 'timeout'))


def _valid_chain(engine, intelligence, run_id, key, progress):
    from .engine import digest
    chain = _chain(progress)
    if chain is None or not chain[1] or progress.get('mode') != 'final_only':
        return False
    finals, audits = chain
    operations = [final.get('operation_id') for final in finals]
    if not all(isinstance(operation, str) and operation for operation in operations):
        return False
    if len(set(operations)) != len(operations):
        return False
    for number, (previous, final, audit) in enumerate(zip(finals, finals[1:], audits), 1):
        target = _target(previous)
        source = _source(engine.store, intelligence, run_id, previous)
        limit = MAX_CONTINUATIONS
        health = audit.get('health_probe')
        if number > MAX_CONTINUATIONS:
            if (number != MAX_CONTINUATIONS + HEALTH_CONTINUATIONS or source is None
                    or not isinstance(health, dict) or health != _health_source(
                        engine.store, run_id, source, native_key=health.get('native_key'))):
                return False
            target = {**target, 'health_probe': health} if target else None
            limit += HEALTH_CONTINUATIONS
        elif health is not None:
            return False
        generation = engine.store.get('operator_resumes', audit.get('resume_generation'), {})
        if (target is None or source is None or source != audit.get('source')
                or audit.get('target') != target
                or generation.get('run_id') != run_id or generation.get('kind') != 'explicit_start_command'
                or generation.get('native_final_targets', {}).get(key) != target
                or final.get('provider_id') != 'local_codex' or final.get('model') != source['model']
                or final.get('operation_id') != audit.get('new_operation_id')
                or final.get('native_interruption_of') != previous.get('operation_id')
                or audit.get('continuation_number', number) != number
                or audit.get('continuation_limit', MAX_CONTINUATIONS) != limit):
            return False
        if source['status'] == 'failed' and (
                audit.get('source_disposition') != 'superseded_without_response'
                or not _fresh_failure_resume(generation, engine.store.get('codex_reasoning', source['native_key']))):
            return False
        expected = copy.deepcopy(previous['prepared'])
        expected['context']['_harness_control']['operation_id'] = final['operation_id']
        if final.get('prepared') != expected or digest(expected) != audit.get('prepared_sha256'):
            return False
    return _profile_matches(engine.store, intelligence, run_id, finals[-1], audits[-1]['source'])


def advance(engine, intelligence, run_id, key, progress):
    from .engine import digest, ResearchBlocked
    target = resume_target(progress, engine=engine, intelligence=intelligence, run_id=run_id, key=key)
    run = engine.store.get('runs', run_id, {})
    generation = engine.store.get('operator_resumes', run.get('state', {}).get('resume_generation'), {})
    handoff = engine.store.get('handoffs', key, {})
    from .tuning_diagnostics import handoff_stage_matches
    if (target is None or run.get('status') != 'running'
            or generation.get('run_id') != run_id or generation.get('kind') != 'explicit_start_command'
            or generation.get('native_final_targets', {}).get(key) != target
            or not handoff_stage_matches(engine.store, run, handoff)):
        return False
    if progress.get('finalization_history') and not _valid_chain(engine, intelligence, run_id, key, progress):
        return False
    final = progress['finalization']
    source = _source(engine.store, intelligence, run_id, final)
    if source is None or not _profile_matches(engine.store, intelligence, run_id, final, source):
        return False
    if source['status'] == 'failed' and not _fresh_failure_resume(
            generation, engine.store.get('codex_reasoning', source['native_key'])):
        return False
    engine.check_active(run_id)
    prepared = copy.deepcopy(final['prepared'])
    operation = uid()
    prepared['context']['_harness_control']['operation_id'] = operation
    preview = intelligence.preflight_batch(run_id, [prepared])
    if (preview['request_count'] != 1
            or preview['requests'][0]['max_output_tokens'] != source['effective_output_limit']):
        return False
    audit = {'source': source, 'resume_generation': generation['id'], 'target': target,
             'new_operation_id': operation, 'prepared_sha256': digest(prepared), 'created_at': now(),
             'continuation_number': len(progress.get('finalization_history', [])) + 1,
             'continuation_limit': MAX_CONTINUATIONS + (HEALTH_CONTINUATIONS if target.get('health_probe') else 0),
             **({'health_probe': target['health_probe']} if target.get('health_probe') else {}),
             **_disposition(source)}
    expected = copy.deepcopy(progress)
    def claim(current):
        if current != expected:
            raise ResearchBlocked('Native final recovery changed; no new request dispatched')
        prior_audits = [*current.get('native_final_recovery_history', [])]
        if current.get('native_final_recovery'):
            prior_audits.append(current['native_final_recovery'])
        from .tool_request_recovery import prepared_successor
        current.update(finalization_history=[*current.get('finalization_history', []), copy.deepcopy(final)],
            native_final_recovery_history=prior_audits, native_final_recovery=audit,
            finalization=prepared_successor(engine.store, key,
                {'status': 'ready', 'operation_id': operation, 'reason': final['reason'],
                'prepared': prepared, 'provider_id': 'local_codex', 'model': source['model'],
                'native_interruption_of': final['operation_id'], 'created_at': now()}))
        return current
    progress.update(engine.store.mutate('handoff_progress', key, claim))
    engine.store.event(run_id, 'handoff.native_final_recovery',
        ('原最终回答请求已回读确认为失败且无回复；废弃旧请求并按本次恢复指令重新发起，原回执及用量保留。'
         if source['status'] == 'failed' else
         f"原最终回答已回读确认为中断；按本次恢复指令续答（{audit['continuation_number']}/{audit['continuation_limit']}），原回执及用量保留。"),
        {'handoff_id': key, 'resume_generation': generation['id'], 'source': source,
         'new_operation_id': operation, **_disposition(source),
         'continuation_number': audit['continuation_number'], 'continuation_limit': audit['continuation_limit'],
         **({'health_probe': audit['health_probe']} if audit.get('health_probe') else {})})
    return True


def validate(engine, intelligence, run_id, key, progress, preview):
    from .engine import ResearchBlocked
    audit = progress.get('native_final_recovery', {})
    if (not _valid_chain(engine, intelligence, run_id, key, progress)
            or not preview or preview.get('request_count') != 1
            or len(preview.get('requests', [])) != 1
            or preview['requests'][0]['max_output_tokens'] != audit['source']['effective_output_limit']):
        raise ResearchBlocked('Native interruption receipt or continuation changed; no new request dispatched')
