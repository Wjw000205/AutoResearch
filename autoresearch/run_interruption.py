"""Durable stop intent, observable draining, and lease release after teardown."""
from __future__ import annotations

import asyncio

from .storage import now, redact, uid
from .async_ownership import cancel as cancel_owned

OBSERVATION_SECONDS = 5


def draining(store, run_id):
    state = (store.get('runs', run_id) or {}).get('state', {}).get('interruption', {})
    return state.get('status') in {'draining', 'failed'}


def request(engine, run, *, cancel=False, cause=None, preparation=False):
    """Persist intent promptly, including while startup owns the lifecycle lock."""
    run_id = run['id']
    existing = run.get('state', {}).get('interruption', {})
    reason = cause or ('cancel' if cancel else 'pause')
    if (existing.get('status') == 'draining' and existing.get('startup_preparation')
            and existing.get('cause') == reason):
        return run, dict(existing)
    intent = {'id': uid(), 'cause': reason, 'status': 'draining',
              'requested_at': now(), 'resources_released': False}
    if preparation:
        intent['startup_preparation'] = True
    target = run['status'] if run['status'] in {'completed', 'cancelled'} else 'cancelled' if cancel else 'paused'
    updated = engine.store.mutate('runs', run_id, lambda current: {
        **current, 'status': target, 'updated_at': now(),
        'state': {**current.get('state', {}), 'interruption': dict(intent)}})
    try:
        engine.store.event(run_id, 'run.stop_requested', '停止意图已保存，正在等待已启动工作和资源清理。', intent)
    except Exception as exc:
        intent['event_error_type'] = type(exc).__name__
    return updated, intent


async def stop(engine, run, *, cancel=False, cause=None):
    run_id = run['id']
    run, intent = request(engine, run, cancel=cancel, cause=cause)

    def save(status, **fields):
        intent.update(status=status, **fields)
        return engine.store.mutate('runs', run_id, lambda current: {
            **current, 'state': {**current.get('state', {}), 'interruption': dict(intent)},
            'updated_at': now()})

    async def observe():
        await asyncio.sleep(OBSERVATION_SECONDS)
        engine.store.event(run_id, 'run.stop_pending',
            '仍在等待已启动工作收尾；未确认资源释放，未允许恢复执行。', dict(intent))

    observer = asyncio.create_task(observe(), name=f'stop-observer:{run_id}')
    committed = False
    try:
        if run_id in engine.cancellations:
            engine.cancellations[run_id].set()
        task = engine.tasks.get(run_id)
        failures = []
        if task and not task.done():
            cancel_owned(task)
            outcomes = await asyncio.gather(task, return_exceptions=True)
            failures.extend(error for error in outcomes
                            if isinstance(error, BaseException) and not isinstance(error, asyncio.CancelledError))
        if hasattr(engine.runner, 'cancel'):
            try:
                await engine.runner.cancel(run_id)
            except Exception as exc:
                failures.append(exc)
        if failures:
            raise RuntimeError('停止清理未完成：' + '; '.join(
                f'{type(error).__name__}: {redact(str(error))[:500]}' for error in failures))
        intent.update(status='stopped', finished_at=now(), resources_released=True)
        current = engine.store.get('runs', run_id)
        result = {**current, 'state': {**current.get('state', {}), 'interruption': dict(intent)},
                  'updated_at': now()}
        records = [('runs', run_id, result)]
        expected = [('runs', run_id, current)]
        lease = engine.store.get('runtime', 'worker')
        if lease and lease.get('owner_instance') == engine.instance_id and lease.get('run_id') == run_id:
            records.append(('runtime', 'worker', {**lease, 'held': False}))
            expected.append(('runtime', 'worker', lease))
        engine.store.batch(records, expected=expected)
        committed = True
        engine.store.event(run_id, 'run.cancelled' if cancel else 'run.paused',
            'Execution stopped; committed evidence and checkpoints retained.', dict(intent))
        return result
    except BaseException as exc:
        if not committed:
            try:
                save('failed', resources_released=False, error=redact(f'{type(exc).__name__}: {exc}')[:1000])
                engine.store.event(run_id, 'run.stop_failed', '停止清理未确认完成，执行租约继续保留。', dict(intent))
            except Exception as audit_error:
                exc.add_note('Stop failure audit could not be persisted: ' + type(audit_error).__name__)
        raise
    finally:
        observer.cancel()
        await asyncio.gather(observer, return_exceptions=True)
