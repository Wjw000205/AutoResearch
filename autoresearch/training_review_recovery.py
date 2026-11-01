"""Explicit, resumable review of saved losses while a scientific stop stays in force.

This worker has no graph, runner, code editor or execution-authorization path.
The ordinary resume and scientific gates remain unchanged.
"""
from __future__ import annotations

import asyncio
import copy
import os

import psutil

from .storage import now, redact
from .training_convergence import read_evidence, sha
from .validation_hold import current as validation_hold

COLLECTION = 'training_review_recoveries'
SCOPE = 'saved_validation_loss_review_only'


def saved_basis(engine, run_id):
    """Bind the evaluated release, without replaying pre-execution approvals.

    This is evidence selection for advisory review, never code admission. The
    original scientific gates still revalidate their full chains for execution.
    """
    from .engine import ResearchBlocked
    review = engine.store.get('research_cycles', f'{run_id}:{engine.cycle(run_id)}') or {}
    design, plan = review.get('design'), review.get('experiment_plan')
    if not isinstance(design, dict) or not isinstance(plan, dict):
        raise ResearchBlocked('原验证评审未保存完整设计和计划，不能推测历史实验范围。')
    current_plan = engine.store.get('experiment_plans', plan.get('id') or run_id)
    if current_plan != plan:
        raise ResearchBlocked('原验证评审的冻结计划与持久化计划不同。')
    program = engine.store.get('programs', run_id)
    if not program:
        program = (engine.store.get('steps', run_id + ':implementation') or {}).get('output')
    if not program:
        raise ResearchBlocked('原验证程序身份缺失。')
    return {'design': design, 'plan': plan, 'program': program,
            'source_review_sha256': sha(review), 'source': 'saved_validation_review'}


def snapshot(engine, run_id):
    from .experiment_views import trials
    from .training_adjustment import _binding
    run = engine.store.get('runs', run_id)
    if not run or engine.store.get('run_deletions', run_id):
        raise ValueError('任务不存在或正在删除。')
    hold = validation_hold(engine.store, run)
    if not hold:
        raise ValueError('此入口只复核被验证停止规则阻塞的任务。')
    rows, truncated = trials(engine.store, run_id)
    if truncated:
        raise ValueError('当前实验记录超过复核上限，不能遗漏证据后启动。')
    basis = saved_basis(engine, run_id)
    saved = []
    for trial in rows:
        if (trial.get('superseded') or trial.get('params', {}).get('phase') != 'validation'
                or trial.get('research_cycle', 1) not in (None, engine.cycle(run_id))
                or trial.get('status') not in {'completed', 'failed'}):
            continue
        saved.append({'trial_id': trial['id'], 'binding': _binding(engine, run_id, trial,
                      basis=basis, saved_trials=rows),
                      'diagnosis': read_evidence(engine, run_id, trial)})
    if not saved:
        raise ValueError('没有可复核的当前周期训练/验证记录。')
    packet = {'run_id': run_id, 'cycle': engine.cycle(run_id), 'scope': SCOPE,
              'validation_hold': hold, 'trials': saved,
              'source_review_sha256': basis['source_review_sha256']}
    return {'snapshot_id': sha(packet), 'packet': packet}


def current(engine, run_id):
    run = engine.store.get('runs', run_id) or {}
    key = run.get('state', {}).get('training_review_recovery')
    row = engine.store.get(COLLECTION, key) if key else None
    return row if row and row.get('run_id') == run_id and row.get('scope') == SCOPE else None


def view(engine, run_id):
    run = engine.store.get('runs', run_id) or {}
    row = current(engine, run_id)
    return {'available': bool(validation_hold(engine.store, run)),
        'scope': SCOPE, 'training_authorized': False,
        'record': {k: copy.deepcopy(row.get(k)) for k in (
            'id', 'status', 'snapshot_id', 'completed_trial_ids', 'adjustment_ids', 'error',
            'created_at', 'updated_at', 'completed_at')} if row else None}


def verify_current(engine, run_id, context):
    from .engine import ResearchBlocked
    row = current(engine, run_id)
    if (not row or row['snapshot_id'] != context.get('snapshot_id')
            or row['scope'] != context.get('scope') or sha(row['packet']) != row['snapshot_id']
            or snapshot(engine, run_id)['snapshot_id'] != row['snapshot_id']):
        raise ResearchBlocked('loss 复核期间原验收、计划、代码或实验日志变化；已保存意见保留。')


def _prepare(engine, run_id):
    from .config import get_settings
    frozen = snapshot(engine, run_id)
    settings = get_settings(engine.store)
    intelligence = engine.intelligence_factory(engine.store, settings)
    moderation = getattr(intelligence, 'assert_moderation_clear', None)
    if callable(moderation):
        moderation(run_id)
    # Authorize recovery of only this review's normal requests, never an old
    # implementation/revision assignment that happens to be blocked as well.
    def belongs(handoff):
        if handoff.get('purpose', '').split(':', 1)[0] not in {'loss_diagnose', 'loss_propose', 'loss_vote'}:
            return False
        inputs = engine.store.get('handoff_inputs', handoff['id']) or {}
        return inputs.get('context', {}).get('loss_review_context', {}).get('snapshot_id') == frozen['snapshot_id']
    generation = engine._operator_resume_generation(run_id, handoff_filter=belongs)
    return frozen, settings, intelligence, generation


async def start(engine, run_id, snapshot_id):
    async with engine._lifecycle_lock:
        return await start_locked(engine, run_id, snapshot_id)


async def start_locked(engine, run_id, snapshot_id):
    from .config import get_settings
    run = engine.store.get('runs', run_id)
    existing = current(engine, run_id)
    if run_id in engine.tasks and not engine.tasks[run_id].done():
        if existing and existing['snapshot_id'] == snapshot_id:
            return view(engine, run_id)
        raise ValueError('该任务正在执行其他工作，先暂停后再复核。')
    if not run or run['status'] not in {'paused', 'blocked'}:
        raise ValueError('只可从已暂停或阻塞的任务启动证据复核。')
    if any(not task.done() for task in engine.tasks.values()):
        raise ValueError('另一个任务正在占用研究 worker。')
    # Draining preparation keeps cancellation from leaving a thread reading a
    # mutable task after the lifecycle lock has been released.
    preparation = asyncio.create_task(asyncio.to_thread(_prepare, engine, run_id))
    try:
        frozen, settings, intelligence, generation = await asyncio.shield(preparation)
    except asyncio.CancelledError:
        while not preparation.done():
            try:
                await asyncio.shield(preparation)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not preparation.cancelled():
            preparation.exception()
        raise
    if frozen['snapshot_id'] != snapshot_id:
        raise ValueError('复核快照已变化，请重新读取现有证据。')
    if engine.store.get('runs', run_id) != run or get_settings(engine.store) != settings:
        raise ValueError('准备期间任务或模型配置变化，请重新读取。')
    identifier = f'{run_id}:loss-review:{snapshot_id}'
    old = engine.store.get(COLLECTION, identifier)
    if old and old.get('status') == 'completed':
        return view(engine, run_id)  # No new model calls on repeated confirmation.
    owner = {'owner_pid': os.getpid(), 'owner_started': psutil.Process().create_time(),
             'owner_instance': engine.instance_id}
    def acquire(lease):
        if lease and lease.get('held') and engine.owner_alive(lease):
            raise ValueError('另一个活动 worker 持有执行租约。')
        return {'id': 'worker', 'held': True, 'run_id': run_id, **owner}
    engine.store.mutate('runtime', 'worker', acquire)
    record = {**(old or {}), 'id': identifier, 'run_id': run_id, 'scope': SCOPE,
        'snapshot_id': snapshot_id, 'packet': frozen['packet'], 'status': 'running',
        'completed_trial_ids': (old or {}).get('completed_trial_ids', []),
        'adjustment_ids': (old or {}).get('adjustment_ids', []),
        'created_at': (old or {}).get('created_at', now()), 'updated_at': now(),
        'original_error': (old or {}).get('original_error', run.get('error')),
        'training_authorized': False, 'scientific_approval': False, 'error': None}
    engine.cancellations[run_id] = asyncio.Event()
    try:
        engine.store.batch([
            (COLLECTION, identifier, record), ('operator_resumes', generation['id'], generation),
            ('runs', run_id, {**run, 'status': 'running', 'error': None, **owner, 'updated_at': now(),
                'state': {**run.get('state', {}), 'training_review_recovery': identifier,
                          'resume_generation': generation['id']}})])
        work = _work(engine, run_id, identifier, intelligence)
        try:
            engine.tasks[run_id] = asyncio.create_task(work, name=f'loss-review:{run_id}')
        except BaseException:
            work.close()
            raise
    except BaseException:
        engine.store.batch([('runs', run_id, run)] + ([(COLLECTION, identifier, old)] if old else []),
            [('operator_resumes', generation['id'])] + ([] if old else [(COLLECTION, identifier)]))
        _release(engine, run_id)
        engine.cancellations.pop(run_id, None)
        raise
    engine.store.event(run_id, 'training.review_recovery_authorized',
        '已授权复核既有验证 loss；原停止规则、验收与实验记录保留，不新增训练。',
        {'recovery_id': identifier, 'snapshot_id': snapshot_id, 'training_authorized': False})
    return view(engine, run_id)


def _release(engine, run_id):
    engine.store.mutate('runtime', 'worker', lambda lease:
        {**lease, 'held': False} if lease and lease.get('run_id') == run_id
        and lease.get('owner_instance') == engine.instance_id else lease or {'id': 'worker', 'held': False})


async def _work(engine, run_id, identifier, intelligence):
    from .engine import Harness
    from .experiment_views import trials
    from .training_adjustment import after_trial
    record = engine.store.get(COLLECTION, identifier)
    context = {'scope': SCOPE, 'snapshot_id': record['snapshot_id'],
        'validation_hold': record['packet']['validation_hold'], 'training_authorized': False}
    harness = Harness(engine, intelligence)
    try:
        for item in record['packet']['trials']:
            await engine._read_consensus_check(run_id, verify_current, engine, run_id, context)
            if item['trial_id'] in record['completed_trial_ids']:
                continue
            rows, _ = await engine._read_consensus_check(run_id, trials, engine.store, run_id)
            trial = next(row for row in rows if row['id'] == item['trial_id'])
            result = await after_trial(engine, harness, run_id, trial, review_context=context)
            engine.check_active(run_id)
            record.update(completed_trial_ids=[*record['completed_trial_ids'], trial['id']],
                adjustment_ids=[*record['adjustment_ids'], result['id']], updated_at=now())
            engine.store.put(COLLECTION, identifier, record)
        await engine._read_consensus_check(run_id, verify_current, engine, run_id, context)
        record.update(status='completed', completed_at=now(), updated_at=now())
        engine.store.put(COLLECTION, identifier, record)
        engine.update_run(run_id, status='blocked', error=record['original_error'])
        engine.store.event(run_id, 'training.review_recovery_completed',
            '既有 loss 复核与告警三方讨论已保存；训练停止规则保持有效，未重跑实验。',
            {'recovery_id': identifier, 'adjustment_ids': record['adjustment_ids'], 'training_authorized': False})
    except asyncio.CancelledError:
        record.update(status='interrupted', updated_at=now())
        engine.store.put(COLLECTION, identifier, record)
        if engine.store.get('runs', run_id)['status'] == 'running':
            engine.update_run(run_id, status='paused')
    except Exception as exc:
        error = redact(f'{type(exc).__name__}: {exc}')[:2000]
        record.update(status='blocked', error=error, updated_at=now())
        engine.store.put(COLLECTION, identifier, record)
        engine.update_run(run_id, status='blocked', error=error)
        engine.store.event(run_id, 'training.review_recovery_blocked', error, {'recovery_id': identifier})
    finally:
        _release(engine, run_id)
