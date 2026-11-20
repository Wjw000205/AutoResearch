"""Durable execution intent and unattended host/checkpoint continuation.

Resume the existing graph, never infer scientific approval or replay uncertain
HTTP requests. Original native readback and completed-trial reuse still apply.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from datetime import datetime

from .async_ownership import to_thread as owned_to_thread
from .storage import now, uid, redact, RunDeletedError

COLLECTION = 'execution_intents'
POLL_SECONDS = 5


def running_intent(store, run_id, *, source='start_command'):
    previous = store.get(COLLECTION, run_id)
    if (isinstance(previous, dict) and previous.get('desired') == 'running'
            and previous.get('run_id') == run_id and previous.get('id') == run_id
            and isinstance(previous.get('authorization_id'), str) and previous['authorization_id'].strip()):
        candidate = {**previous, 'continuation_status': 'running', 'retry_at': 0, 'updated_at': now()}
        if _intent_error(run_id, candidate) is None:
            return candidate
    return {'id': run_id, 'run_id': run_id, 'desired': 'running',
            'authorization_id': uid(), 'source': source, 'created_at': now(),
            'updated_at': now(), 'continuation_status': 'running', 'retry_at': 0}


def stopped_intent(store, run_id, *, cancel=False):
    return store.mutate(COLLECTION, run_id, lambda old: {
        **(old if isinstance(old, dict) else {'created_at': now()}),
        'id': run_id, 'run_id': run_id,
        'desired': 'cancelled' if cancel else 'paused', 'updated_at': now(),
        'continuation_status': 'disabled', 'retry_at': 0})


def authorize_generation(store, run_id, generation):
    intent = store.get(COLLECTION, run_id)
    if _intent_error(run_id, intent) or intent.get('desired') != 'running':
        raise ValueError('No durable execution intent authorizes automatic continuation')
    if generation:
        generation.update(automatic_continuation=True,
                          execution_authorization_id=intent['authorization_id'])
        generation['native_model_targets'] = {}


def recover_intent(store, run):
    """Migrate only execution observed running at host loss, never manual pause."""
    if store.get(COLLECTION, run['id']) is None:
        store.put(COLLECTION, run['id'], running_intent(store, run['id'], source='interrupted_running_checkpoint'))


def after_native_readback(engine, run_id, key, target, source, *, field):
    """Freeze a later controller command after verified native readback.

    The native caller has already checked the exact original request, empty
    terminal response and profile. Preserve the original generation: a failure
    first observed after startup needs a later command, never a backdated one.
    """
    run = engine.store.get('runs', run_id)
    generation = engine.store.get('operator_resumes', run.get('state', {}).get('resume_generation'), {})
    if not generation.get('automatic_continuation') or source is None:
        return generation
    intent = engine.store.get(COLLECTION, run_id, {})
    saved = engine.store.get('codex_reasoning', source['native_key'], {})
    failure = saved.get('checkpoint', {}).get('first_failure', {})
    if (intent.get('desired') != 'running' or run.get('status') != 'running'
            or intent.get('authorization_id') != generation.get('execution_authorization_id')
            or generation.get(field, {}).get(key) != target):
        return generation
    try:
        observed = datetime.fromisoformat(failure['observed_at'])
        created = datetime.fromisoformat(generation['created_at'])
        if observed < created:
            return generation
    except (KeyError, TypeError, ValueError):
        return generation
    successor = {**generation, 'id': uid(), 'created_at': now(),
                 'readback_parent_generation': generation['id'],
                 'readback_handoff_id': key, 'readback_source': source}
    engine.store.batch([('operator_resumes', successor['id'], successor),
        ('runs', run_id, {**run, 'state': {**run.get('state', {}), 'resume_generation': successor['id']}, 'updated_at': now()})],
        expected=[('runs', run_id, run), (COLLECTION, run_id, intent)])
    return successor


def _intent_error(key, intent):
    if not isinstance(intent, dict):
        return '持续执行记录不是对象。'
    if intent.get('run_id') != key or intent.get('id') != key:
        return '持续执行记录与任务标识不一致。'
    if not isinstance(intent.get('desired'), str) or intent['desired'] not in {'running', 'paused', 'cancelled'}:
        return '持续执行意图无效。'
    if intent['desired'] != 'running':
        return None  # Inactive counters cannot authorize work or block a stop.
    deadline = intent.get('retry_at', 0)
    try:
        valid_deadline = type(deadline) in (int, float) and math.isfinite(deadline) and deadline >= 0
    except OverflowError:
        valid_deadline = False
    if not valid_deadline:
        return '自动接续的重试时间无效。'
    if any(type(intent.get(field, 0)) is not int or intent.get(field, 0) < 0
           for field in ('admission_failures', 'native_readback_waits')):
        return '自动接续的重试计数无效。'
    if (intent['desired'] == 'running' and (not isinstance(intent.get('authorization_id'), str)
                                           or not intent['authorization_id'].strip())):
        return '持续执行记录缺少原执行授权标识。'
    return None


def _record_invalid_intent(store, key, raw, revision, error):
    """Keep the original untouched; record one diagnostic per bad revision."""
    digest = hashlib.sha256(raw.encode('utf-8')).hexdigest()
    with store._read_db() as db:
        last = db.execute("SELECT json_extract(data,'$.source_updated_at') FROM events "
            "WHERE run_id=? AND kind='run.continuation_invalid' ORDER BY id DESC LIMIT 1", (key,)).fetchone()
    if last and last[0] == revision:
        return
    try:
        store.event(key, 'run.continuation_invalid', '本任务的自动接续记录未通过校验，原记录与检查点已保留。',
            {'intent_key': key, 'source_sha256': digest, 'source_updated_at': revision, 'error': error})
    except RunDeletedError:
        pass  # Deletion won the race; never recreate audit data or stop other runs.


def diagnostic_view(store, run_id):
    with store._read_db() as db:
        row = db.execute("SELECT json_extract(e.data,'$.error') FROM events e JOIN records r "
            "ON r.collection='execution_intents' AND r.key=e.run_id "
            "WHERE e.run_id=? AND e.kind='run.continuation_invalid' "
            "AND json_extract(e.data,'$.source_updated_at')=r.updated_at ORDER BY e.id DESC LIMIT 1", (run_id,)).fetchone()
    return {'continuation_status': 'invalid_metadata', 'error': row[0]} if row else None


def _candidates(engine):
    rows = []
    for key in engine.store.record_keys(COLLECTION):
        if engine.store.get('run_deletions', key):
            continue
        with engine.store._read_db() as db:
            head = db.execute('SELECT data,updated_at FROM records WHERE collection=? AND key=?', (COLLECTION, key)).fetchone()
        if head is None:
            continue
        try:
            intent = json.loads(head[0])
        except json.JSONDecodeError:
            _record_invalid_intent(engine.store, key, head[0], head[1], '持续执行记录不是有效 JSON。')
            continue
        error = _intent_error(key, intent)
        if error:
            _record_invalid_intent(engine.store, key, head[0], head[1], error)
            continue
        if intent.get('desired') != 'running' or intent.get('retry_at', 0) > time.time():
            continue
        run = engine.store.get('runs', intent['run_id'])
        if not run or engine.store.get('run_deletions', intent['run_id']):
            continue
        if (run.get('status') == 'paused' or run.get('status') == 'blocked'
                and intent.get('continuation_status') == 'waiting_for_native_receipt'):
            rows.append(intent['run_id'])
    return rows


def wait_for_native_receipt(engine, intelligence, run_id):
    """Schedule further readback of dispatched native turns, never new inference."""
    from .codex_reasoning import key_for
    from .implementation_activity import records
    from .native_timeout_readback import TRANSIENT
    from .tuning_diagnostics import handoff_stage_matches
    intent = engine.store.get(COLLECTION, run_id)
    run = engine.store.get('runs', run_id)
    if not intent or intent.get('desired') != 'running':
        return False
    if not callable(getattr(intelligence, '_operation_identity', None)):
        return False
    sources = []
    for handoff in records(engine.store, 'handoffs', run_id):
        if (handoff.get('status') == 'completed' or not handoff_stage_matches(engine.store, run, handoff)
                or engine.store.get('decisions', handoff['id'])):
            continue
        progress = engine.store.get('handoff_progress', handoff['id'], {})
        for slot in ('normal_request', 'finalization'):
            request = progress.get(slot, {})
            prepared = request.get('prepared')
            if (request.get('provider_id') != 'local_codex' or request.get('response') is not None
                    or not isinstance(prepared, dict)):
                continue
            native_key, operation = key_for(intelligence, run_id, prepared['role'],
                                           prepared['task'], prepared['context'], prepared['schema'])
            saved = engine.store.get('codex_reasoning', native_key, {})
            cp = saved.get('checkpoint', {})
            if (saved.get('run_id') != run_id or saved.get('operation_id') != operation
                    or cp.get('run_id') != run_id or cp.get('operation_id') != native_key
                    or not cp.get('thread_id') or not cp.get('turn_id')
                    or cp.get('reasoning_only') is not True or cp.get('auth_mode') != 'chatgpt'
                    or cp.get('status') not in {'running', 'unknown'}
                    or cp.get('error_code') not in TRANSIENT | {None}):
                continue
            sources.append({'handoff_id': handoff['id'], 'slot': slot, 'native_key': native_key,
                            'thread_id': cp['thread_id'], 'turn_id': cp['turn_id']})
    if not sources:
        return False
    sources.sort(key=lambda row: (row['handoff_id'], row['slot'], row['native_key']))
    number = intent.get('native_readback_waits', 0) + 1 if intent.get('native_readback_sources') == sources else 1
    delay = min(300, 5 * 2 ** min(number - 1, 6))
    engine.store.mutate(COLLECTION, run_id, lambda current: {
        **current, 'continuation_status': 'waiting_for_native_receipt',
        'native_readback_waits': number, 'native_readback_sources': sources,
        'retry_at': time.time() + delay, 'updated_at': now()})
    engine.store.event(run_id, 'run.native_receipt_pending',
        '原生长任务尚无完整回执；后台退避后回读原 thread/turn，不重新派发原推理请求。',
        {'sources': sources, 'retry_seconds': delay})
    return True


class Continuations:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    async def tick(self):
        if getattr(self.engine, '_shutting_down', False):
            return
        for run_id in await owned_to_thread(_candidates, self.engine):
            # Share manual controls' writer lock, including slow preparation.
            async with self.engine._lifecycle_lock:
                run = self.store.get('runs', run_id)
                intent = self.store.get(COLLECTION, run_id)
                if (not run or _intent_error(run_id, intent) or intent.get('desired') != 'running'
                        or self.store.get('run_deletions', run_id)):
                    continue
                if (run.get('status') != 'paused' and not (run.get('status') == 'blocked'
                        and intent.get('continuation_status') == 'waiting_for_native_receipt')):
                    continue
                interruption = run.get('state', {}).get('interruption', {})
                if interruption.get('status') in {'draining', 'failed'}:
                    continue
                if any(not task.done() for task in self.engine.tasks.values()):
                    continue
                try:
                    await self.engine._start(run_id, automatic=True)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Failed admission spends no model budget. Foreign leases,
                    # unavailable resources and existing gates remain binding.
                    previous = intent.get('admission_failures', 0)
                    delay = min(300, 5 * 2 ** min(previous, 6))
                    self.store.mutate(COLLECTION, run_id, lambda current: {
                        **current, 'continuation_status': ('waiting_for_native_receipt' if run['status'] == 'blocked'
                                                         else 'waiting_for_admission'),
                        'admission_failures': previous + 1, 'retry_at': time.time() + delay,
                        'last_error': redact(f'{type(exc).__name__}: {exc}')[:1000], 'updated_at': now()})
                else:
                    self.store.event(run_id, 'run.auto_continued',
                        '按已保存的持续执行意图自动回到原检查点；原请求、审批和实验收据继续核对。',
                        {'authorization_id': intent['authorization_id']})

    async def maintain(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A temporary DB outage must not kill the lifetime controller.
                # Nothing dispatches without durable admission checks.
                pass
            await asyncio.sleep(POLL_SECONDS)
