"""Persist local gate work separately from agent/model/experiment execution."""
import uuid
import asyncio
import time
from contextvars import ContextVar

from .storage import now

COLLECTION = 'harness_local_activity'
_validation_progress = ContextVar('harness_validation_progress', default=None)


def checkpoint(phase, completed, total, unit, role=None):
    """Report real work through the live owner, never through a read snapshot."""
    callback = _validation_progress.get()
    if callback is not None:
        callback(phase, completed, total, unit, role)


async def validate(engine, run_id, checker, *args, label=None, phase='review_validation',
                   completed_exceptions=(), _activity_handoff_id=None, _activity_role=None, **kwargs):
    """Keep a read-only worker visible and owned until all cancellation settles."""
    from .async_ownership import finish as join, to_thread
    value = begin(engine, run_id, checker, label=label, concurrent=True,
                  handoff_id=_activity_handoff_id, role=_activity_role)
    last = [0.0, None]
    owner_role = _activity_role

    def active():
        if hasattr(engine, 'check_active'):
            engine.check_active(run_id)

    def progress(current, completed, total, unit, role):
        stamp, identity = time.monotonic(), (current, role, total)
        if identity != last[1] or completed == total or stamp - last[0] >= .5:
            active()
            report(engine, value, phase=current, completed=completed, total=total, unit=unit, role=owner_role or role)
            last[:] = [stamp, identity]

    def worker():
        token = _validation_progress.set(progress)
        try:
            progress(phase, 0, 0, '项', None)
            return checker(*args, **kwargs)
        finally:
            _validation_progress.reset(token)
            active()  # A live pause wins over a frozen snapshot or successor request.

    async def pulse():
        while value is not None:
            await asyncio.sleep(1)
            await to_thread(heartbeat, engine, value)

    status = 'completed'
    timer = asyncio.create_task(pulse())
    try:
        result = await to_thread(worker)
        active()
        return result
    except BaseException as exc:
        if not isinstance(exc, completed_exceptions):
            status = 'interrupted' if isinstance(exc, asyncio.CancelledError) else 'failed'
        raise
    finally:
        timer.cancel()
        try:
            await join(asyncio.gather(timer, return_exceptions=True))
            active()
        except BaseException as exc:
            status = 'interrupted' if isinstance(exc, asyncio.CancelledError) else 'failed'
            raise
        finally:
            finish(engine, value, status)


def begin(engine, run_id, checker, *, label=None, concurrent=False, handoff_id=None, role=None):
    if getattr(engine.store,'is_read_snapshot',False):
        return None
    run = engine.store.get('runs', run_id) or {}
    if run.get('stage') != 'implementation':
        return None
    module, name = getattr(checker,'__module__','local'),getattr(checker,'__name__',type(checker).__name__)
    key = run_id + ':' + module + '.' + name
    operation_id = uuid.uuid4().hex
    if concurrent:
        key += ':' + operation_id
    value = {'id': key, 'run_id': run_id, 'operation_id': operation_id,
        'handoff_id': handoff_id, 'role': role,
        'owner_instance': getattr(engine,'instance_id',None), 'status': 'running', 'step': name,
        'label': label or '核验已保存回执与冻结来源', 'started_at': now(), 'updated_at': now()}
    engine.store.put(COLLECTION, key, value)
    return value


def heartbeat(engine, value):
    if value is None:
        return
    def update(old):
        if (old and old.get('status') == 'running'
                and old.get('operation_id') == value['operation_id']
                and old.get('owner_instance') == value.get('owner_instance')):
            return {**old, 'heartbeat_at': now()}
        return old
    engine.store.mutate(COLLECTION, value['id'], update)


def finish(engine, value, status):
    if value is None:
        return
    def update(old):
        if (old and old.get('operation_id') == value['operation_id']
                and old.get('owner_instance') == value.get('owner_instance')):
            return {**old, 'status': status, 'updated_at': now()}
        return old
    engine.store.mutate(COLLECTION, value['id'], update)


def report(engine, value, *, phase, completed, total, unit, role=None):
    """Update actual phase counts; an old worker cannot overwrite its successor."""
    if value is None:
        return
    if (type(completed) is not int or type(total) is not int or not 0 <= completed <= total
            or not isinstance(unit, str) or not isinstance(phase, str)):
        raise ValueError('Local preparation requires actual typed phase counts')
    labels = {'source_inventory': '核对历史冻结材料版本', 'read_receipts': '核验历史读取记录与投递凭证',
        'historical_handoffs': '复用已认证的历史阅读', 'capability_materials': '准备运行能力复核材料',
        'capability_validation': '核验已有能力版本与原始材料回执',
        'review_validation': '核验审核意见与原始投递回执',
        'source_validation': '核验冻结来源与代码交付',
        'validation_handoffs': '核验历史交接与原始阅读来源',
        'validation_documents': '核验批准文档的真实读取',
        'validation_code': '核验交付源码与阅读回执',
        'validation_alignment': '核验完整对齐意见',
        'validation_manifests': '核验已提交的上下文清单'}
    def update(old):
        if (old and old.get('status') == 'running' and old.get('operation_id') == value['operation_id']
                and old.get('owner_instance') == value.get('owner_instance')):
            return {**old, 'phase': phase, 'completed': completed, 'total': total, 'unit': unit,
                'role': role, 'label': labels.get(phase, value['label']),
                'model_dispatched': False, 'updated_at': now()}
        return old
    engine.store.mutate(COLLECTION, value['id'], update)


def view(store, run):
    from .run_views import _one
    fields = {key: "json_extract(data,'$." + key + "')" for key in
        ['operation_id','step','label','started_at','updated_at','heartbeat_at','phase','completed','total','unit','role']}
    fields['model_dispatched'] = ("CASE json_type(data,'$.model_dispatched') "
        "WHEN 'false' THEN json('false') WHEN 'true' THEN json('true') END")
    active = _one(store, COLLECTION, fields,
        run_id=run['id'], where="json_extract(data,'$.status')='running' AND json_extract(data,'$.owner_instance')="
            "(SELECT json_extract(owner.data,'$.owner_instance') FROM records owner WHERE owner.collection='runs' AND owner.key=?)",
        args=(run['id'],), order_by='updated_at DESC,key DESC') if run.get('status') == 'running' else None
    from .code_evidence_queue import public_queue
    tasks = public_queue(store, run)
    if not active and not tasks and not (run.get('stage') == 'implementation' and run.get('error')):
        return None
    return {'local_validation': active, 'tasks': tasks, 'serial': True,
        'block_reason': run.get('error') if run.get('status') in {'blocked','failed'} else None}
