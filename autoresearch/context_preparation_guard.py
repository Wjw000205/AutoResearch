"""Bound local read-only preparation, never model calls or experiment runtime."""
import asyncio
import time

TIMEOUT_SECONDS = 120


async def run(awaitable, *, store, run_id, operation, handoff_id=None):
    start = time.monotonic()
    details = {'operation': operation, 'handoff_id': handoff_id,
        'timeout_seconds': TIMEOUT_SECONDS, 'model_dispatched': False, 'experiment_dispatched': False}
    store.event(run_id, 'context.preparation_started', '正在本地准备下一轮上下文', details)
    deadline = asyncio.timeout(TIMEOUT_SECONDS)
    try:
        async with deadline:
            result = await awaitable
    except TimeoutError as exc:
        if not deadline.expired():
            raise
        if (store.get('runs', run_id) or {}).get('status') != 'running':
            raise asyncio.CancelledError() from exc
        from .intelligence import ProviderError
        message = (f'本地上下文准备超过 {TIMEOUT_SECONDS} 秒，续接已停止；本轮尚未发送模型请求或启动实验。'
                   '原答复和工具回执已保留，恢复时从保存位置继续。')
        store.event(run_id, 'context.preparation_stalled', message,
            {**details, 'elapsed_seconds': round(time.monotonic()-start, 3)})
        raise ProviderError(message) from exc
    store.event(run_id, 'context.preparation_completed', '本地上下文准备完成，尚未派发本轮请求',
        {**details, 'elapsed_seconds': round(time.monotonic()-start, 3)})
    return result
