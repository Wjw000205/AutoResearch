"""Frozen continuations for explicit resume or confirmed empty transient failure.

Unknown, running, completed-with-output and unobserved turns never authorize
dispatch. Output-free transient failures are read back, then retried with backoff.
All normal admission gates
remain necessary. This does not convert a tool-capable request to final-only.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from datetime import datetime

from .native_final_recovery import _profile_matches, _source as interrupted_source, _disposition
from .storage import now, uid

AUTO_POLICY = 'confirmed-empty-transient-failure-v1'


class RetryScheduled(Exception):
    """Re-enter the same frozen tool turn without recursion or peer cancellation."""


def retryable(engine, intelligence, run_id, key, progress):
    """A hint to read the original turn; this never authorizes a new request."""
    normal = progress.get('normal_request', {})
    if progress.get('mode') == 'final_only' or _target(normal, progress.get('turn')) is None:
        return False
    source = _source(engine.store, intelligence, run_id, normal, require_readback=False)
    return (source is not None and source['status'] == 'failed'
            and _valid_history(engine, intelligence, run_id, key, progress)
            and _profile_matches(engine.store, intelligence, run_id, normal, source))


def _delay(number):
    return (2, 5, 15, 30)[min(number - 1, 3)]


def _authorization(engine, run_id, key, previous, audit, source, target, number):
    if audit.get('automatic_recovery'):
        try:
            not_before = datetime.fromisoformat(audit['created_at']).timestamp() + _delay(number)
        except (KeyError, TypeError, ValueError):
            return False
        return (audit['automatic_recovery'] == AUTO_POLICY and source['status'] == 'failed'
                and audit.get('resume_generation') is None
                and audit.get('automatic_retry_number') == number
                and audit.get('not_before') == not_before)
    generation = engine.store.get('operator_resumes', audit.get('resume_generation'), {})
    return (generation.get('kind') == 'explicit_start_command' and generation.get('run_id') == run_id
            and generation.get('targets', {}).get(key) == target
            and _fresh_generation(generation, engine.store.get('codex_reasoning', source['native_key'])))


async def wait_retry(engine, run_id, progress):
    audits = progress.get('native_normal_recovery_history', [])
    if not audits or not audits[-1].get('automatic_recovery'):
        return
    while time.time() < audits[-1]['not_before']:
        engine.check_active(run_id)
        await asyncio.sleep(min(1, audits[-1]['not_before'] - time.time()))
    engine.check_active(run_id)


def _history(progress):
    requests = progress.get('native_normal_request_history', [])
    audits = progress.get('native_normal_recovery_history', [])
    if (not isinstance(requests, list) or not isinstance(audits, list)
            or len(requests) != len(audits)
            or any(not isinstance(item, dict) for item in [*requests, *audits])):
        return None
    turns = [item.get('turn') for item in requests]
    operations = [item.get('operation_id') for item in requests]
    if (any(type(turn) is not int or turn < 0 for turn in turns) or turns != sorted(turns)
            or any(not isinstance(operation, str) or not operation for operation in operations)
            or len(set(operations)) != len(operations)):
        return None
    return requests, audits


def _target(normal, turn):
    from .engine import Harness
    if (normal.get('provider_id') != 'local_codex' or normal.get('turn') != turn
            or not isinstance(normal.get('operation_id'), str) or not normal['operation_id']
            or normal.get('status') not in {'blocked', 'dispatching'} or normal.get('response') is not None
            or not isinstance(normal.get('prepared'), dict)
            or any(normal.get(k) for k in ('legacy_source_input_id', 'migration_of', 'stream_retry_of'))):
        return None
    return Harness._resume_target(normal, turn)


def _source(store, intelligence, run_id, normal, *, require_readback=True):
    context = normal.get('prepared', {}).get('context')
    if not isinstance(context, dict) or not isinstance(context.get('_harness_control', {}), dict):
        return None
    if context.get('_harness_control', {}).get('mode') not in (None, 'continue'):
        return None
    source = interrupted_source(store, intelligence, run_id, normal, normal=True, require_readback=require_readback)
    if source is None:
        return None
    saved = store.get('codex_reasoning', source['native_key'])
    cp = saved['checkpoint']
    if (cp.get('version') != 1 or cp.get('model_provider') != 'openai'
            or not isinstance(saved.get('instruction'), str)
            or cp.get('instruction_sha256') != hashlib.sha256(saved['instruction'].encode()).hexdigest()
            or cp.get('output_schema_sha256') != hashlib.sha256(json.dumps(
                saved.get('transport_schema', saved['schema']), sort_keys=True, ensure_ascii=False).encode()).hexdigest()):
        return None
    return source


def _fresh_generation(generation, saved):
    try:
        failure = saved['checkpoint']['first_failure']
        failed_at = datetime.fromisoformat(failure['observed_at'])
        resumed_at = datetime.fromisoformat(generation['created_at'])
        return (failed_at.tzinfo is not None and resumed_at.tzinfo is not None and failed_at < resumed_at
                and failure.get('thread_id') == saved['checkpoint']['thread_id']
                and failure.get('turn_id') == saved['checkpoint']['turn_id'])
    except (KeyError, TypeError, ValueError):
        return False


def _prepared(previous, operation):
    result = copy.deepcopy(previous['prepared'])
    control = result['context'].get('_harness_control', {})
    result['context']['_harness_control'] = {**control, 'operation_id': operation}
    return result


def _valid_history(engine, intelligence, run_id, key, progress):
    """Recheck every frozen recovery, including repeated interruptions.

    A successful continuation may finish a tool turn and leave normal_request.
    Its next tool turn therefore has a different input. Same-turn successors
    instead retain the exact prepared request and link to the preceding audit.
    """
    from .engine import digest
    history = _history(progress)
    turn = progress.get('turn')
    if history is None or type(turn) is not int or turn < 0:
        return False
    native_keys, operations, generations, successors = set(), set(), set(), {}
    automatic_counts = {}
    for number, (previous, audit) in enumerate(zip(*history)):
        source = _source(engine.store, intelligence, run_id, previous)
        operation = audit.get('new_operation_id')
        generation_id = audit.get('resume_generation')
        source_turn = previous['turn']
        target = _target(previous, source_turn)
        if audit.get('automatic_recovery'):
            automatic_counts[source_turn] = automatic_counts.get(source_turn, 0) + 1
        if (source is None or target is None or source_turn > turn
                or source != audit.get('source') or target != audit.get('target')
                or audit.get('turn') != source_turn
                or not _authorization(engine, run_id, key, previous, audit, source, target,
                                      automatic_counts.get(source_turn, 0))
                or not isinstance(operation, str) or not operation or operation == previous['operation_id']
                or source['native_key'] in native_keys or previous['operation_id'] in operations
                or operation in operations or operation in successors
                or generation_id is not None and generation_id in generations
                or audit.get('continuation_number') != 1 or audit.get('continuation_limit') != 1
                or not _profile_matches(engine.store, intelligence, run_id, previous, source)):
            return False
        if source['status'] == 'failed' and audit.get('source_disposition') != 'superseded_without_response':
            return False
        expected = _prepared(previous, operation)
        if audit.get('prepared_sha256') != digest(expected):
            return False
        if number and history[0][number - 1]['turn'] == source_turn:
            last_operation = history[1][number - 1]['new_operation_id']
            if previous['operation_id'] != last_operation:
                return False
        predecessor = successors.get(previous['operation_id'])
        if previous.get('native_interruption_of'):
            if (predecessor is None or previous['turn'] != predecessor[0]['turn']
                    or previous['native_interruption_of'] != predecessor[0]['operation_id']
                    or previous.get('prepared') != predecessor[1]):
                return False
        elif predecessor is not None:
            return False
        native_keys.add(source['native_key'])
        operations.add(previous['operation_id'])
        if generation_id is not None:
            generations.add(generation_id)
        successors[operation] = (previous, expected)
    normal = progress.get('normal_request', {})
    if not isinstance(normal, dict):
        return False
    predecessor = successors.get(normal.get('operation_id'))
    if normal.get('native_interruption_of'):
        if (not history[0] or predecessor is None
                or normal.get('operation_id') != history[1][-1].get('new_operation_id')
                or normal.get('turn') != predecessor[0]['turn']
                or normal['native_interruption_of'] != predecessor[0]['operation_id']
                or normal.get('prepared') != predecessor[1]):
            return False
    elif predecessor is not None or (history[0] and history[0][-1]['turn'] == turn):
        return False
    return True


def advance(engine, intelligence, run_id, key, progress, *, automatic=False):
    from .engine import digest, ResearchBlocked
    history = _history(progress)
    normal = progress.get('normal_request', {})
    turn = progress.get('turn')
    if progress.get('mode') == 'final_only' or history is None or type(turn) is not int:
        return False
    if not _valid_history(engine, intelligence, run_id, key, progress):
        return False
    target = _target(normal, turn)
    run = engine.store.get('runs', run_id, {})
    generation = engine.store.get('operator_resumes', run.get('state', {}).get('resume_generation'), {})
    handoff = engine.store.get('handoffs', key, {})
    from .tuning_diagnostics import handoff_stage_matches
    if (target is None or run.get('status') != 'running'
            or not handoff_stage_matches(engine.store, run, handoff)):
        return False
    if not automatic and (generation.get('run_id') != run_id or generation.get('kind') != 'explicit_start_command'
            or generation.get('targets', {}).get(key) != target
            or any(audit.get('resume_generation') == generation.get('id') for audit in history[1])):
        return False
    source = _source(engine.store, intelligence, run_id, normal)
    if (source is None or any(previous['operation_id'] == normal['operation_id'] for previous in history[0])
            or any(audit.get('source', {}).get('native_key') == source['native_key'] for audit in history[1])
            or not _profile_matches(engine.store, intelligence, run_id, normal, source)
            or (source['status'] != 'failed' if automatic else
                not _fresh_generation(generation, engine.store.get('codex_reasoning', source['native_key'])))):
        return False
    engine.check_active(run_id)
    operation = uid()
    if operation in {value for previous, audit in zip(*history)
                     for value in (previous['operation_id'], audit.get('new_operation_id'))} or operation == normal['operation_id']:
        return False
    prepared = _prepared(normal, operation)
    preview = intelligence.preflight_batch(run_id, [prepared])
    if preview['request_count'] != 1 or preview['requests'][0]['max_output_tokens'] != source['effective_output_limit']:
        return False
    audit = {'source': source, 'target': target, 'turn': turn,
             'new_operation_id': operation, 'prepared_sha256': digest(prepared), 'created_at': now(),
             'continuation_number': 1, 'continuation_limit': 1, **_disposition(source)}
    if automatic:
        number = 1 + sum(bool(a.get('automatic_recovery')) and a['turn'] == turn for a in history[1])
        audit.update(automatic_recovery=AUTO_POLICY, automatic_retry_number=number,
                     not_before=datetime.fromisoformat(audit['created_at']).timestamp() + _delay(number))
    else:
        audit['resume_generation'] = generation['id']
    expected = copy.deepcopy(progress)

    def claim(current):
        if current != expected:
            raise ResearchBlocked('Native normal recovery changed; no new request dispatched')
        from .tool_request_recovery import prepared_successor
        current.update(native_normal_request_history=[*history[0], copy.deepcopy(normal)],
                       native_normal_recovery_history=[*history[1], audit],
                       normal_request=prepared_successor(engine.store, key,
                           {'status': 'ready', 'turn': turn, 'operation_id': operation,
                           'prepared': prepared, 'provider_id': 'local_codex', 'model': source['model'],
                           'native_interruption_of': normal['operation_id'], 'created_at': now()}))
        return current

    progress.update(engine.store.mutate('handoff_progress', key, claim))
    engine.store.event(run_id, 'handoff.native_normal_auto_retry' if automatic else 'handoff.native_normal_recovery',
        (f'本机推理临时失败已回读确认且无回复；{_delay(number)} 秒后自动重试当前请求，保留已完成工具和其他角色任务。'
         if automatic else
         '原普通推理请求已回读确认为失败且无回复；废弃旧请求并按本次恢复指令重新发起，保留工具能力、原回执及用量。'
         if source['status'] == 'failed' else
         '原普通推理请求已回读确认为中断；按本次恢复指令续答一次，保留工具能力、原回执及用量。'),
        {'handoff_id': key, **({'automatic_recovery': AUTO_POLICY} if automatic else {'resume_generation': generation['id']}), 'source': source, 'turn': turn,
         'new_operation_id': operation, **_disposition(source),
         'continuation_number': 1, 'continuation_limit': 1})
    return True


def validate(engine, intelligence, run_id, key, progress, preview):
    from .engine import digest, ResearchBlocked
    history = _history(progress)
    normal = progress.get('normal_request', {})
    valid = (history is not None and bool(history[0]) and progress.get('mode') != 'final_only'
             and _valid_history(engine, intelligence, run_id, key, progress))
    if valid:
        previous, audit = history[0][-1], history[1][-1]
        source = _source(engine.store, intelligence, run_id, previous)
        operation = normal.get('operation_id')
        expected = _prepared(previous, operation) if source is not None else None
        target = _target(previous, progress.get('turn'))
        valid = (source is not None and source == audit.get('source') and target is not None
                 and audit.get('target') == target
                 and (not audit.get('automatic_recovery') or time.time() >= audit['not_before'])
                 and normal.get('turn') == previous.get('turn') == audit.get('turn') == progress.get('turn')
                 and isinstance(operation, str) and bool(operation)
                 and operation == audit.get('new_operation_id') and operation != previous.get('operation_id')
                 and normal.get('native_interruption_of') == previous.get('operation_id')
                 and normal.get('provider_id') == 'local_codex' and normal.get('model') == source['model']
                 and normal.get('prepared') == expected and audit.get('prepared_sha256') == digest(expected)
                 and audit.get('continuation_number') == audit.get('continuation_limit') == 1
                 and _profile_matches(engine.store, intelligence, run_id, normal, source)
                 and preview and preview.get('request_count') == 1 and len(preview.get('requests', [])) == 1
                 and preview['requests'][0]['max_output_tokens'] == source['effective_output_limit'])
    if not valid:
        raise ResearchBlocked('Native normal interruption receipt or continuation changed; no new request dispatched')
