"""Durable, idempotent browser controls; HTTP disconnects never own the work."""
from __future__ import annotations

import asyncio
import os

import psutil

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .storage import now, redact


COLLECTION = 'run_control_requests'


class ControlRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(pattern=r'^[a-zA-Z0-9-]{16,80}$')
    action: str = Field(pattern=r'^(start|resume|pause|cancel)$')


class RunControls:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store
        self.tasks = {}
        self.closing = False

    def read(self, run_id, request_id):
        return self.store.get(COLLECTION, f'{run_id}:{request_id}')

    def recover(self):
        # A restarted host cannot prove whether an uncommitted control finished.
        # Preserve that uncertainty and require a new explicit command, never replay.
        for key in self.store.record_keys(COLLECTION,
                predicate="json_extract(data,'$.status') IN ('accepted','running')"):
            row = self.store.get(COLLECTION, key)
            if self.engine.owner_alive(row):
                continue
            self.store.put(COLLECTION, key, {**row, 'status': 'interrupted',
                'updated_at': now(), 'error': '服务在操作回执完成前退出；请核对当前任务状态后重新操作，原请求不会自动重发。'})

    def submit(self, run_id, request):
        if self.closing:
            raise HTTPException(503, '服务正在退出，请稍后核对操作回执。')
        key = f'{run_id}:{request.request_id}'
        created = False
        run = self.store.get('runs', run_id)

        def claim(existing):
            nonlocal created
            if existing:
                if existing.get('action') != request.action:
                    raise HTTPException(409, '同一操作标识不能用于不同操作。')
                return existing
            created = True
            return {'id': key, 'request_id': request.request_id, 'run_id': run_id,
                'action': request.action, 'status': 'accepted', 'created_at': now(),
                'updated_at': now(), 'owner_pid': os.getpid(),
                'owner_started': psutil.Process().create_time(), 'owner_instance': self.engine.instance_id,
                'prior_status': run['status']}

        receipt = self.store.mutate(COLLECTION, key, claim)
        if created:
            # No await between the durable claim and registering the detached task.
            task = asyncio.create_task(self.execute(key), name=f'run-control:{key}')
            self.tasks[key] = task
            task.add_done_callback(lambda done: self.finished(key, done))
        return receipt

    def finished(self, key, task):
        self.tasks.pop(key, None)
        if not task.cancelled():
            task.exception()  # persistence errors must not become unobserved task errors

    async def execute(self, key):
        row = self.store.get(COLLECTION, key)
        try:
            self.store.put(COLLECTION, key, {**row, 'status': 'running', 'updated_at': now()})
            if row['action'] in {'start', 'resume'}:
                run = await self.engine.start(row['run_id'])
            else:
                run = await self.engine.stop(row['run_id'], cancel=row['action'] == 'cancel')
            result = {field: run.get(field) for field in ('id', 'status', 'stage', 'updated_at')}
            self.store.put(COLLECTION, key, {**row, 'status': 'completed',
                'result': result, 'updated_at': now()})
        except asyncio.CancelledError:
            self.store.put(COLLECTION, key, {**row, 'status': 'interrupted', 'updated_at': now(),
                'error': '操作在服务退出时中断；请核对当前任务状态后重新操作。'})
            raise
        except Exception as exc:
            from .intelligence import PolicyReviewRequired
            status = 'rejected' if isinstance(exc, (ValueError, PolicyReviewRequired)) else 'interrupted'
            self.store.put(COLLECTION, key, {**row, 'status': status,
                'error': redact(str(exc))[:2000], 'updated_at': now()})

    async def close(self):
        self.closing = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def register(app, controls, task_runtime, require_run):
    @app.post('/api/runs/{run_id}/control-requests', status_code=202)
    @task_runtime.control
    async def submit(run_id: str, payload: ControlRequest):
        require_run(run_id)
        return controls.submit(run_id, payload)

    @app.get('/api/runs/{run_id}/control-requests/{request_id}')
    def read(run_id: str, request_id: str):
        require_run(run_id)
        receipt = controls.read(run_id, request_id)
        if receipt is None:
            raise HTTPException(404, '操作请求尚未登记。')
        return receipt
