"""Retain ownership of started work through cancellation and repeated signals."""
from __future__ import annotations

import asyncio


def cancel(task):
    """Request cancellation once; a second request can interrupt finally blocks."""
    if task.done() or getattr(task, 'cancelling', lambda: 0)():
        return False
    return task.cancel()


async def finish(task, *, propagate_cancel=True):
    """Join owned work before propagating caller cancellation.

    Shield every wait, including waits after cancellation. A second signal must
    never release a lock while a Python writer or subprocess cleanup still runs.
    A worker error is observed, but cannot replace the caller's cancellation.
    A caller already handling cancellation may set propagate_cancel=False to
    retain a real receipt before re-raising its original cancellation.
    """
    cancelled = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancelled = exc
        except BaseException:
            break
    if cancelled is not None and propagate_cancel:
        if not task.cancelled():
            task.exception()
        raise cancelled
    return task.result()


async def to_thread(function, /, *args, **kwargs):
    """Offload synchronous work without orphaning it when its await is cancelled."""
    return await finish(asyncio.create_task(asyncio.to_thread(function, *args, **kwargs)))


async def gather(*awaitables, return_exceptions=False):
    """Settle every started sibling before propagating failure or cancellation.

    Ordinary failure policy still follows return_exceptions. Shield the batch
    so repeated caller cancellation cannot interrupt a sibling's finally block.
    """
    tasks, batch = [], None
    try:
        for awaitable in awaitables:
            tasks.append(asyncio.ensure_future(awaitable, loop=asyncio.get_running_loop()))
        batch = asyncio.gather(*tasks, return_exceptions=return_exceptions)
        return await asyncio.shield(batch)
    except BaseException:
        for task in tasks:
            cancel(task)
        try:
            await finish(asyncio.gather(*tasks, return_exceptions=True))
        finally:
            if batch is not None and batch.done() and not batch.cancelled():
                batch.exception()
        raise
