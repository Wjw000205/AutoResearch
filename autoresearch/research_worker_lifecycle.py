"""Consume worker failures and never leave a finished task labelled running."""
from .storage import redact, now, RunDeletedError


class _SupersededWorker(Exception):
    """Rollback a late callback instead of overwriting a newer owner."""


def finished(engine, run_id, task):
    cancelled = task.cancelled()
    error = None if cancelled else task.exception()
    # A late callback from an old worker cannot affect a resumed generation.
    if engine.tasks.get(run_id) is not task:
        return
    run = engine.store.get('runs', run_id) or {}
    if run.get('status') != 'running':
        return
    reason = ('后台研究任务意外取消' if cancelled else
              f'后台研究任务异常退出：{type(error).__name__}: {redact(str(error))[:1000]}' if error else
              '后台研究任务已结束，但未保存终态')
    message = reason + '；原实验、请求和回执保留，未自动重试。'
    def block_current(current):
        # Another host can take over after the preliminary read. Recheck under
        # SQLite's write transaction, and recheck the local task generation too.
        if (not current or current.get('status') != 'running'
                or engine.tasks.get(run_id) is not task
                or current.get('owner_instance') not in (None, engine.instance_id)):
            raise _SupersededWorker()
        return {**current, 'status': 'blocked', 'error': message, 'updated_at': now()}
    try:
        engine.store.mutate('runs', run_id, block_current)
    except (_SupersededWorker, RunDeletedError):
        return
    engine.store.mutate('runtime', 'worker', lambda lease:
        {**lease, 'held': False} if lease and lease.get('owner_instance') == engine.instance_id
        and lease.get('run_id') == run_id else lease or {'id': 'worker', 'held': False})
    try:
        engine.store.event(run_id, 'run.worker_exited', message,
            {'cancelled': cancelled, 'exception_type': type(error).__name__ if error else None,
             'owner_instance': engine.instance_id, 'automatic_retry': False})
    except RunDeletedError:
        pass  # Deletion won after finalization; never recreate its audit data.
