"""Consume worker failures and never leave a finished task labelled running."""
from .storage import redact


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
    engine.update_run(run_id, status='blocked', error=message)
    engine.store.mutate('runtime', 'worker', lambda lease:
        {**lease, 'held': False} if lease and lease.get('owner_instance') == engine.instance_id
        and lease.get('run_id') == run_id else lease or {'id': 'worker', 'held': False})
    engine.store.event(run_id, 'run.worker_exited', message,
        {'cancelled': cancelled, 'exception_type': type(error).__name__ if error else None,
         'automatic_retry': False})
