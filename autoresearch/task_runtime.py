"""One research/control loop, independent of the HTTP/data event loop.

The engine's locks, cancellation events and workers stay on one owning loop.
SQLite remains the source of truth; this does not admit additional research
workers or turn a read into dispatch authority.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import Future
from functools import wraps
import inspect
import threading


class TaskRuntime:
    def __init__(self):
        self.loop = None
        self._thread = None
        self._closed = False

    async def start(self):
        if self._thread is not None:
            raise RuntimeError('Research runtime already started')
        ready = Future()
        self._closed = False

        def serve():
            try:
                # Runner retains Windows' Proactor loop for native subprocesses.
                with asyncio.Runner() as runner:
                    self.loop = runner.get_loop()
                    ready.set_result(None)
                    self.loop.run_forever()
            except BaseException as exc:
                if not ready.done():
                    ready.set_exception(exc)
                else:
                    raise
            finally:
                self.loop = None

        self._thread = threading.Thread(target=serve, name='research-control', daemon=True)
        self._thread.start()
        try:
            await asyncio.shield(asyncio.wrap_future(ready))
        except BaseException:
            # Even a cancelled startup must not leave an owner loop behind.
            await asyncio.shield(asyncio.wrap_future(ready))
            await self.close()
            raise

    async def call(self, function, *args, **kwargs):
        loop = self.loop
        if self._closed:
            raise RuntimeError('Research runtime is closed')
        if loop is None or asyncio.get_running_loop() is loop:
            # Direct, lifespan-free embedding/tests retain the original API.
            return await function(*args, **kwargs)

        completed = Future()
        task = None

        def finish(value):
            try:
                completed.set_result(value.result())
            except BaseException as exc:
                completed.set_exception(exc)

        def launch():
            nonlocal task
            try:
                task = loop.create_task(function(*args, **kwargs))
                task.add_done_callback(finish)
            except BaseException as exc:
                completed.set_exception(exc)

        loop.call_soon_threadsafe(launch)
        waiter = asyncio.wrap_future(completed)
        try:
            return await asyncio.shield(waiter)
        except asyncio.CancelledError:
            # run_coroutine_threadsafe().cancel() completes its proxy before
            # the remote finally blocks drain. Explicitly wait for those blocks
            # so no lifecycle lock/persistence worker escapes cancellation.
            loop.call_soon_threadsafe(lambda: task.cancel() if task is not None else None)
            while not waiter.done():
                try:
                    await asyncio.shield(waiter)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not waiter.cancelled():
                waiter.exception()
            raise

    def control(self, function):
        @wraps(function)
        async def invoke(*args, **kwargs):
            return await self.call(function, *args, **kwargs)
        # Resolve postponed API annotations in their original module. FastAPI
        # otherwise tries to resolve model names in this wrapper's globals.
        invoke.__signature__ = inspect.signature(function, eval_str=True)
        invoke.execution_lane = 'research-control'
        return invoke

    async def close(self):
        self._closed = True
        thread, loop = self._thread, self.loop
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            # Runner cancels remaining owned tasks and drains its executor.
            joined = asyncio.create_task(asyncio.to_thread(thread.join))
            try:
                await asyncio.shield(joined)
            except asyncio.CancelledError:
                while not joined.done():
                    try:
                        await asyncio.shield(joined)
                    except asyncio.CancelledError:
                        continue
                raise
            finally:
                self._thread = None
