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
import logging

from .async_ownership import cancel as cancel_owned, finish as finish_owned


class TaskRuntimeUnavailable(RuntimeError):
    """The owning loop cannot accept work; never reroute it or imply success."""


class TaskRuntime:
    def __init__(self):
        self.loop = None
        self._thread = None
        self._closed = False
        self._ever_started = False
        self._pending = set()
        self._pending_lock = threading.Lock()

    async def start(self):
        if self._thread is not None:
            raise RuntimeError('Research runtime already started')
        ready = Future()
        self._closed = False
        self._ever_started = True

        def serve():
            try:
                # Runner retains Windows' Proactor loop for native subprocesses.
                with asyncio.Runner() as runner:
                    self.loop = runner.get_loop()
                    # Publish readiness only while the owner is processing
                    # callbacks, not in the gap before run_forever starts.
                    owner_loop = self.loop
                    def announce():
                        ready.set_result(None)
                        if self._closed:
                            owner_loop.stop()
                    owner_loop.call_soon(announce)
                    self.loop.run_forever()
            except BaseException as exc:
                if not ready.done():
                    ready.set_exception(exc)
                else:
                    logging.getLogger(__name__).error('Research owner loop stopped unexpectedly: %s', type(exc).__name__)
            finally:
                self.loop = None
                with self._pending_lock:
                    pending = list(self._pending)
                for future in pending:
                    if not future.done():
                        future.set_exception(TaskRuntimeUnavailable('Research owner loop stopped; operation outcome must be checked'))

        self._thread = threading.Thread(target=serve, name='research-control', daemon=True)
        try:
            self._thread.start()
        except BaseException:
            self._thread = None
            self._closed = True
            raise
        startup = asyncio.wrap_future(ready)
        try:
            await asyncio.shield(startup)
            if self._closed:
                raise TaskRuntimeUnavailable('Research runtime was closed during startup')
        except BaseException:
            # Even a cancelled startup must not leave an owner loop behind.
            try:
                await finish_owned(startup, propagate_cancel=False)
            except BaseException:
                pass  # Preserve the caller's original startup exception.
            await finish_owned(asyncio.create_task(self.close()), propagate_cancel=False)
            raise

    async def call(self, function, *args, **kwargs):
        loop = self.loop
        if self._closed:
            raise TaskRuntimeUnavailable('Research runtime is closed')
        if loop is None:
            # Direct, lifespan-free embedding/tests retain the original API.
            if not self._ever_started:
                return await function(*args, **kwargs)
            raise TaskRuntimeUnavailable('Research owner loop is not running')
        if asyncio.get_running_loop() is loop:
            return await function(*args, **kwargs)
        if loop.is_closed() or not loop.is_running():
            raise TaskRuntimeUnavailable('Research owner loop is not running')

        completed = Future()
        with self._pending_lock:
            if self._closed or self.loop is not loop:
                raise TaskRuntimeUnavailable('Research owner loop is stopping')
            self._pending.add(completed)
        def forget(done):
            with self._pending_lock:
                self._pending.discard(done)
        completed.add_done_callback(forget)
        task = None

        def finish(value):
            if completed.done():
                return
            try:
                completed.set_result(value.result())
            except BaseException as exc:
                completed.set_exception(exc)

        def launch():
            nonlocal task
            if completed.done():
                return
            if self._closed or self.loop is not loop:
                completed.set_exception(TaskRuntimeUnavailable('Research owner loop is stopping; operation was not started'))
                return
            work = None
            try:
                work = function(*args, **kwargs)
                task = loop.create_task(work)
                task.add_done_callback(finish)
            except BaseException as exc:
                if inspect.iscoroutine(work):
                    work.close()
                completed.set_exception(exc)

        try:
            loop.call_soon_threadsafe(launch)
        except RuntimeError as exc:
            completed.set_exception(TaskRuntimeUnavailable('Research owner loop stopped before dispatch'))
        waiter = asyncio.wrap_future(completed)
        try:
            return await asyncio.shield(waiter)
        except asyncio.CancelledError:
            # run_coroutine_threadsafe().cancel() completes its proxy before
            # the remote finally blocks drain. Explicitly wait for those blocks
            # so no lifecycle lock/persistence worker escapes cancellation.
            if not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(lambda: cancel_owned(task) if task is not None else None)
                except RuntimeError:
                    pass  # The owner's finally block settles pending proxies.
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
        if self._thread is threading.current_thread():
            raise TaskRuntimeUnavailable('Research owner loop cannot join its own thread')
        self._closed = True
        thread, loop = self._thread, self.loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass  # Concurrent owner exit still needs the thread join.
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
