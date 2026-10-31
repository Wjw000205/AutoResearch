"""Bounded automatic reads of an already dispatched, timed-out reasoning turn.

This allowance does not authorize generation or replace official resume. Its
source and deadline survive restarts; later official reads may still inspect
the original turn without renewing this automatic allowance.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
from datetime import datetime, timedelta, timezone


FIELD = 'operation_timeout_readback'
VERSION = 1
LIMIT = 3
GRACE_SECONDS = 30
DELAYS = (0, 2, 5)
IDENTITY_FIELDS = ('version', 'run_id', 'operation_id', 'workspace', 'project_root',
    'instruction_sha256', 'requested_model', 'requested_effort', 'reasoning_only',
    'output_schema_sha256', 'image_inputs_sha256', 'thread_id', 'turn_id',
    'turn_dispatched_at', 'auth_mode', 'model_provider')
TRANSIENT = {'codex_operation_timeout', 'codex_original_turn_still_in_progress',
    'codex_transport_closed', 'codex_transport_eof', 'codex_read_timeout'}


def _sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        allow_nan=False).encode()).hexdigest()


def _now():
    return datetime.now(timezone.utc)


async def _wait(seconds):
    await asyncio.sleep(seconds)


async def _call(function, *args):
    value = function(*args)
    return await value if inspect.isawaitable(value) else value


def _fail():
    from .intelligence import ProviderError
    raise ProviderError('本机 Codex 原超时读回来源、绑定或额度已变化；未发送新的读回或模型请求。')


def _identity(receipt):
    return {key: receipt.get(key) for key in IDENTITY_FIELDS}


def _binding(saved, receipt):
    return {'native_key': saved.get('id'), 'run_id': saved.get('run_id'), 'role': saved.get('role'),
        'operation_id': saved.get('operation_id'), 'selection': copy.deepcopy(saved.get('selection')),
        'instruction_sha256': hashlib.sha256(saved['instruction'].encode()).hexdigest(),
        'schema_sha256': _sha(saved.get('schema')), 'transport_schema_sha256': _sha(saved.get('transport_schema')),
        'output_limit': saved.get('output_limit'), 'reservation': copy.deepcopy(saved.get('reservation')),
        'checkpoint_identity': _identity(receipt)}


def _source_valid(saved, receipt):
    """Only a durable authenticated, post-dispatch timeout admits this policy."""
    from .local_codex import RECEIPT_FIELDS
    if not isinstance(receipt, dict) or not isinstance(saved.get('instruction'), str):
        return False
    failure = receipt.get('first_failure') or {}
    selection, reservation = saved.get('selection') or {}, saved.get('reservation') or {}
    schema = saved.get('transport_schema', saved.get('schema'))
    expected_key = hashlib.sha256(json.dumps([saved.get('run_id'), saved.get('role'),
        saved.get('operation_id'), 'codex-reasoning-v1']).encode()).hexdigest()
    return (isinstance(saved.get('instruction'), str) and isinstance(receipt, dict)
        and not set(receipt) - RECEIPT_FIELDS and type(receipt.get('version')) is int
        and receipt['version'] == 1 and receipt.get('status') == 'unknown'
        and receipt.get('error_code') == 'codex_operation_timeout'
        and receipt.get('reasoning_only') is True and receipt.get('auth_mode') == 'chatgpt'
        and receipt.get('model_provider') == 'openai' and receipt.get('run_id') == saved.get('run_id')
        and saved.get('id') == expected_key and receipt.get('operation_id') == saved.get('id')
        and isinstance(receipt.get('thread_id'), str) and bool(receipt['thread_id'])
        and isinstance(receipt.get('turn_id'), str) and bool(receipt['turn_id'])
        and isinstance(receipt.get('turn_dispatched_at'), str) and bool(receipt['turn_dispatched_at'])
        and receipt.get('instruction_sha256') == hashlib.sha256(saved['instruction'].encode()).hexdigest()
        and receipt.get('output_schema_sha256') == _sha(schema)
        and receipt.get('requested_model') == selection.get('model')
        and receipt.get('requested_effort') == selection.get('effort')
        and type(selection.get('timeout')) in (int, float) and 1 <= selection['timeout'] <= 86400
        and reservation.get('provider') == 'local_codex' and bool(reservation.get('id'))
        and reservation.get('run_id') == saved.get('run_id')
        and reservation.get('role') == saved.get('role') and reservation.get('model') == selection.get('model')
        and reservation.get('operation_id') == saved.get('operation_id')
        and type(saved.get('output_limit')) is int
        and reservation.get('request_output_token_limit') == saved['output_limit']
        and failure.get('status') == 'unknown' and failure.get('error_code') == 'codex_operation_timeout'
        and failure.get('thread_id') == receipt['thread_id'] and failure.get('turn_id') == receipt['turn_id']
        and isinstance(failure.get('observed_at'), str) and not receipt.get('native_tool_violation'))


def _parse_time(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            _fail()
        return parsed
    except (TypeError, ValueError):
        _fail()


def validate(saved, *, store=None):
    """Authenticate the immutable timeout source and the latest real snapshot."""
    policy = saved.get(FIELD)
    if not isinstance(policy, dict) or set(policy) != {'version', 'binding', 'binding_sha256',
            'source_checkpoint', 'source_checkpoint_sha256', 'current_checkpoint_sha256',
            'started_at', 'deadline_at', 'retry_limit', 'grace_seconds', 'attempts'}:
        _fail()
    source, current = policy['source_checkpoint'], saved.get('checkpoint')
    if (type(policy['version']) is not int or policy['version'] != VERSION
            or type(policy['retry_limit']) is not int or policy['retry_limit'] != LIMIT
            or type(policy['grace_seconds']) is not int or policy['grace_seconds'] != GRACE_SECONDS
            or not isinstance(source, dict) or not isinstance(current, dict)
            or not _source_valid(saved, source)
            or policy['source_checkpoint_sha256'] != _sha(source)
            or policy['binding_sha256'] != _sha(policy['binding'])
            or _sha(_binding(saved, source)) != policy['binding_sha256']
            or _sha(_identity(current)) != _sha(_identity(source))
            or _sha(current.get('first_failure')) != _sha(source.get('first_failure'))
            or policy['current_checkpoint_sha256'] != _sha(current)):
        _fail()
    started, deadline = _parse_time(policy['started_at']), _parse_time(policy['deadline_at'])
    if (started != _parse_time(source['first_failure']['observed_at'])
            or deadline != started + timedelta(seconds=GRACE_SECONDS)):
        _fail()
    attempts = policy['attempts']
    if not isinstance(attempts, list) or len(attempts) > LIMIT:
        _fail()
    for number, attempt in enumerate(attempts, 1):
        if (not isinstance(attempt, dict) or set(attempt) - {'number', 'started_at', 'finished_at',
                'status', 'receipt_sha256', 'error_code'}
                or type(attempt.get('number')) is not int or attempt['number'] != number
                or attempt.get('status') not in {'started', 'finished', 'grace_exhausted'}
                or not started <= _parse_time(attempt.get('started_at')) < deadline):
            _fail()
    if store is not None:
        reservation = saved['reservation']
        budget = store.get('budgets', saved['run_id'], {})
        rows = [*budget.get('pending', {}).values(), *budget.get('records', [])]
        matches = [row for row in rows if row.get('id') == reservation['id']]
        if len(matches) != 1 or any(matches[0].get(k) != v for k, v in reservation.items()):
            _fail()
    return policy


def observe(saved, receipt):
    """Bind actual native callbacks, including later official original reads."""
    if FIELD not in saved:
        return
    policy = validate(saved)
    source = policy['source_checkpoint']
    if (_sha(_identity(receipt)) != _sha(_identity(source))
            or _sha(receipt.get('first_failure')) != _sha(source.get('first_failure'))):
        _fail()
    policy['current_checkpoint_sha256'] = _sha(receipt)


def _retryable(receipt):
    if receipt.get('status') != 'unknown' or receipt.get('native_tool_violation'):
        return False
    code = receipt.get('error_code')
    if code in TRANSIENT:
        return True
    if code == 'codex_rpc_error':
        errors = receipt.get('rpc_errors') or []
        last = errors[-1] if errors else {}
        return (last.get('method') in {'initialize', 'account/read', 'thread/read', 'thread/turns/list'}
            and last.get('category') in {'internal_error', 'server_error'}
            and last.get('failure_category') not in {'authentication_required', 'request_rejected'})
    return False


async def recover(saved, *, store, readback, save, check_active):
    """Use at most three automatic original reads; never a generation request.

    ``readback(remaining_seconds)`` must pass the saved checkpoint to the native
    client. Its real on_progress callbacks go through :func:`observe` before
    saving. A later official read can run independently, but cannot renew this
    policy or extend its original absolute grace deadline.
    """
    receipt = saved.get('checkpoint') or {}
    if FIELD not in saved:
        if not _source_valid(saved, receipt):
            return receipt
        await _call(check_active)
        started = _parse_time(receipt['first_failure']['observed_at'])
        binding = _binding(saved, receipt)
        saved[FIELD] = {'version': VERSION, 'binding': binding, 'binding_sha256': _sha(binding),
            'source_checkpoint': copy.deepcopy(receipt), 'source_checkpoint_sha256': _sha(receipt),
            'current_checkpoint_sha256': _sha(receipt), 'started_at': started.isoformat(),
            'deadline_at': (started + timedelta(seconds=GRACE_SECONDS)).isoformat(),
            'retry_limit': LIMIT, 'grace_seconds': GRACE_SECONDS, 'attempts': []}
        validate(saved, store=store)
        await _call(save)
    policy = validate(saved, store=store)
    if any(attempt['status'] == 'grace_exhausted' for attempt in policy['attempts']):
        return copy.deepcopy(saved['checkpoint'])
    deadline = _parse_time(policy['deadline_at'])
    loop = asyncio.get_running_loop()
    initial_remaining = (deadline - _now()).total_seconds()
    monotonic_deadline = loop.time() + max(0, min(GRACE_SECONDS, initial_remaining))

    def remaining():
        now = _now()
        if now < _parse_time(policy['started_at']):
            _fail()  # A backwards clock cannot grant another grace window.
        return min((deadline - now).total_seconds(), monotonic_deadline - loop.time())

    while len(policy['attempts']) < LIMIT and _retryable(saved['checkpoint']):
        await _call(check_active)
        validate(saved, store=store)
        delay = DELAYS[len(policy['attempts'])]
        if remaining() < max(1, delay):
            break
        if delay:
            await _wait(delay)
            await _call(check_active)
        if remaining() < 1:
            break
        attempt = {'number': len(policy['attempts']) + 1, 'started_at': _now().isoformat(), 'status': 'started'}
        policy['attempts'].append(attempt)
        await _call(save)  # An unsaved attempt never opens a connection.
        await _call(check_active)
        budget = remaining()
        if budget < 1:
            break
        try:
            async with asyncio.timeout(budget):
                observed = await readback(budget)
        except TimeoutError:
            attempt.update(status='grace_exhausted', finished_at=_now().isoformat(),
                receipt_sha256=_sha(saved['checkpoint']), error_code='automatic_readback_grace_exhausted')
            await _call(save)
            break
        validate(saved, store=store)
        if _sha(observed) != _sha(saved['checkpoint']):
            _fail()  # Only an actually persisted native callback is a result.
        attempt.update(status='finished', finished_at=_now().isoformat(),
            receipt_sha256=_sha(observed), error_code=observed.get('error_code'))
        await _call(save)
    return copy.deepcopy(saved['checkpoint'])
