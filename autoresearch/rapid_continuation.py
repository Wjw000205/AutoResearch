"""Explicit forward-only continuation after a frozen negative validation.

The operator changes the next-step policy, not historical measurements or votes.
The graph starts at diagnosis and uses the existing bounded amendment gates.
"""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from .experiment_workflow import sha, encoded
from .storage import now

COLLECTION = 'rapid_continuations'
SCOPE = 'bounded_architecture_feasibility'
AUTHORIZATION = '修正一下规则，进入快速通道'
PURPOSES = {'experiment_diagnose', 'experiment_decide', 'experiment_vote',
            'rapid_implement', 'rapid_review', 'experiment_train', 'tune',
            'loss_diagnose', 'loss_propose', 'loss_vote', 'report'}
HOST_FILES = {'rapid_continuation.py', 'runtime_training_policy.py', 'codex_workflow.py',
    'engine.py', 'lab.py', 'rapid_experiment_revision.py', 'experiment_decisions.py',
    'experiment_analysis.py', 'experiment_workflow.py', 'python_training.py',
    'training_context.py', 'experiment_tools.py', 'experiment_memory.py', 'validation_hold.py',
    'orchestration.py', 'record_archive.py', 'storage.py', 'experiment_views.py', 'experiment_cleanup.py',
    'performance_policy.py', 'experiment_plan.py', 'research_cycle.py', 'comparison_transport.py', 'experiment_summary.py',
    'benchmark_matrix.py', 'comparison_task_policy.py', 'fair_comparison.py', 'experiment_protocols.py',
    'evaluation.py', 'training_convergence.py', 'literature_relevance.py', 'datasets.py',
    'comparison_protocol_repair.py', 'code_verification.py', 'deadline_contract_checks.py', 'hrp_host_timing.py',
    'optimization_loop.py'}


def host_identity():
    root = Path(__file__).resolve().parent
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in sorted(HOST_FILES)}


def current(store, run_id):
    run = store.get('runs', run_id) or {}
    key = run.get('state', {}).get('rapid_continuation')
    return store.get(COLLECTION, key) if key else None


def validated(store, run_id, *, check_host=True):
    """Fail closed on stale authority; no reads of unbounded execution history."""
    record = current(store, run_id)
    if record is None:
        return None
    run = store.get('runs', run_id) or {}
    body = record.get('policy_amendment', {})
    sources = record.get('sources', {})
    from .execution_contract import REPAIR_ATTEMPT_ALLOWANCE
    if (record.get('run_id') != run_id or body.get('run_id') != run_id
            or record.get('scope') != SCOPE or body.get('scope') != SCOPE
            or record.get('sha256') != sha(body) or not str(body.get('operator_instruction') or '').strip()
            or body.get('sources_sha256') != sha(sources)
            or body.get('cycle') != run.get('state', {}).get('research_cycle', 1)
            or body.get('config_sha256') != sha(run.get('config'))
            or record.get('status') not in {'running', 'interrupted', 'blocked', 'completed'}
            or body.get('scientific_approval') is not False
            or body.get('final_tests_allowed') is not False
            or body.get('attempt_limit') != run.get('config', {}).get('max_trials', 0) + REPAIR_ATTEMPT_ALLOWANCE
            or sha(store.get('experiment_plans', body.get('plan_id', ''))) != body.get('plan_sha256')):
        raise ValueError('快速通道授权的计划、配置、宿主代码或来源已变化，请核对后重新授权。')
    runtime_key = run.get('state', {}).get('runtime_training_policy')
    if (runtime_key != body.get('runtime_policy_id')
            or sha(store.get('runtime_training_policies', runtime_key) if runtime_key else None)
                != body.get('runtime_policy_sha256')):
        raise ValueError('快速通道依赖的原运行策略已变化。')
    if check_host and body.get('host_files') != host_identity():
        from .performance_policy import current as performance_policy
        if not performance_policy(store, run_id):
            raise ValueError('快速通道授权的宿主代码已变化，需要明确绑定本轮规则修订。')
    return record


def effective_policy(store, run_id, original):
    record = validated(store, run_id)
    if record is None:
        return original
    from .performance_policy import current as performance_policy, compact
    performance = compact(performance_policy(store, run_id))
    return {**(original or {}), 'on_validation_failure': 'revise',
        **({'performance_policy': performance} if performance else {}),
        'rationale': '用户已显式修订本轮后续规则，授权有界局部优化；旧负结果和原停止策略保留。',
        'authorization_id': record['id'], 'authorization_sha256': record['sha256'],
        'scope': SCOPE, 'historical_outcome_unchanged': True,
        'goal': '快速验证局部架构可行性；先诊断再局部修改，每批对照后重新决策。',
        'final_tests_allowed': False,
        'stop_rule': '证据不支持继续、三方有阻塞异议、资源不允许或剩余次数不足即停止派发。'}


def trial_limit(store, run_id):
    run = store.get('runs', run_id)
    record = validated(store, run_id)
    from .dataset_configuration_queue import trial_limit as queue_limit
    return queue_limit(store, run_id, record['policy_amendment']['attempt_limit'] if record else run['config']['max_trials'])


def snapshot(engine, run_id):
    from .validation_hold import current as hold
    from .training_review_recovery import saved_basis
    from .execution_contract import REPAIR_ATTEMPT_ALLOWANCE
    run = engine.store.get('runs', run_id)
    if not run or engine.store.get('run_deletions', run_id):
        raise ValueError('任务不存在或正在删除。')
    existing = validated(engine.store, run_id)
    if existing:
        return {'snapshot_id': existing['snapshot_id'], 'policy_amendment': existing['policy_amendment'],
                'sources': existing['sources']}
    if run['status'] not in {'paused', 'blocked'} or not hold(engine.store, run):
        raise ValueError('快速继续入口需要已暂停或阻塞的验证负结果及原停止规则。')
    basis = saved_basis(engine, run_id)
    # Read only admission operands; transport arrays stay in their source rows.
    import json
    from .record_archive import fingerprint
    with engine.store._read_db() as db:
        heads = db.execute("SELECT key,json_object('status',json_extract(data,'$.status'),"
            "'attempt',coalesce(json_extract(data,'$.attempt'),1)) FROM records "
            "WHERE collection='experiments' AND key>=? AND key<? "
            "AND coalesce(json_extract(data,'$.research_cycle'),1)=? ORDER BY key",
            (run_id + ':', run_id + ';', engine.cycle(run_id))).fetchall()
    trials = [json.loads(row[1]) for row in heads]
    if any(t.get('status') in {'running', 'interrupted'} for t in trials):
        raise ValueError('仍有运行中或未确认终态的实验，先核对执行现场。')
    from .experiment_decisions import units
    pilot = next(u for u in units(basis['plan']) if u['kind'] == 'pilot')
    used = sum(t.get('attempt', 1) for t in trials)
    limit = run['config']['max_trials'] + REPAIR_ATTEMPT_ALLOWANCE
    from .rapid_experiment_revision import count
    from .research_iteration import allows as cycle_allowed, limit as cycle_limit
    if (used + 1 + len(pilot['jobs']) > limit
            or not cycle_allowed(engine.store, run_id, engine.cycle(run_id) + count(engine, run_id))):
        raise ValueError('原总次数上限内已无一次局部检查及完整成对试验的空间。')
    key = run.get('state', {}).get('runtime_training_policy')
    runtime = engine.store.get('runtime_training_policies', key) if key else None
    if runtime:
        # Old host hashes are intentionally not rewritten. New host authority
        # is separate; original independent source approvals remain mandatory.
        from .runtime_training_policy import validate_record
        from .code_bundle import validate
        p = validate_record(engine.store, run_id, runtime, basis['program'], files=False)
        validate(p['base_bundle'], basis['program']['code'])
        validate(p['bundle'], basis['program']['code'])
    checkpoints = [fingerprint(engine.store, collection, f'{run_id}:{s}')
        for collection in ('steps', 'stage_contexts') for s in
        ('tuning', 'revision', 'validation_review', 'final_tests', 'report')]
    sources = {'review': engine.store.get('research_cycles', f'{run_id}:{engine.cycle(run_id)}'),
        'program': basis['program'], 'trials': [fingerprint(engine.store, 'experiments', row[0]) for row in heads],
        'checkpoints': [ref for ref in checkpoints if ref]}
    body = {'run_id': run_id, 'cycle': engine.cycle(run_id), 'scope': SCOPE,
        'operator_instruction': AUTHORIZATION, 'sources_sha256': sha(sources),
        'config_sha256': sha(run['config']), 'plan_id': basis['plan'].get('id') or run_id,
        'plan_sha256': sha(basis['plan']), 'runtime_policy_id': key,
        'runtime_policy_sha256': sha(runtime), 'host_files': host_identity(),
        'attempt_limit': limit, 'attempts_already_used': used,
        'amendments_remaining': (None if cycle_limit(engine.store, run_id) is None else
            cycle_limit(engine.store, run_id) - engine.cycle(run_id) - count(engine, run_id)),
        'rule': '允许证据驱动的局部修订和诊断；每次仅执行获三方批准的一个批次，再重新决策。',
        'unchanged': ['dataset_and_splits', 'baseline', 'acceptance_thresholds', 'resource_caps',
                      'total_attempt_ceiling', 'independent_reviews', 'original_negative_results'],
        'scientific_approval': False, 'final_tests_allowed': False}
    return {'snapshot_id': sha(body), 'policy_amendment': body, 'sources': sources}


def view(store, run_id):
    record = current(store, run_id)
    if not record:
        return None
    run = store.get('runs', run_id)
    return {k: record.get(k) for k in ('id', 'scope', 'sha256', 'snapshot_id',
        'created_at', 'updated_at', 'completed_at')} | {'policy_amendment': record['policy_amendment'],
        'status': run['status'], 'phase': run['stage'], 'error': run.get('error')}


def resume_filter(engine, run_id):
    record = validated(engine.store, run_id)
    if not record:
        return None
    def belongs(handoff):
        if handoff.get('purpose', '').split(':', 1)[0] not in PURPOSES:
            return False
        context = (engine.store.get('handoff_inputs', handoff['id']) or {}).get('context', {})
        from .performance_policy import current as performance_policy
        performance = performance_policy(engine.store, run_id)
        return (not performance or (context.get('performance_policy') or {}).get('sha256') == performance['sha256']) and (context.get('experiment_workflow_protocol') == 1
                and context.get('research_cycle') == record['policy_amendment']['cycle'])
    return belongs


async def start(engine, run_id, snapshot_id, authorization):
    """Authorize a new graph entry, then use the normal worker/start lifecycle."""
    if not isinstance(authorization, str) or not authorization.strip():
        raise ValueError('此入口需要明确的本轮规则变更和快速通道授权。')
    async with engine._lifecycle_lock:
        run = engine.store.get('runs', run_id)
        if run_id in engine.tasks and not engine.tasks[run_id].done():
            old = validated(engine.store, run_id)
            if old and old['snapshot_id'] == snapshot_id:
                return view(engine.store, run_id)
            raise ValueError('任务已有活动 worker。')
        if not run or run['status'] not in {'paused', 'blocked'}:
            raise ValueError('启动快速通道前任务必须处于暂停或阻塞状态。')
        if any(not task.done() for task in engine.tasks.values()):
            raise ValueError('另一个研究任务正在执行。')
        def prepare():
            frozen = snapshot(engine, run_id)
            if frozen['snapshot_id'] != snapshot_id or current(engine.store, run_id):
                return frozen, []
            from .record_archive import save
            references = frozen['sources']['trials'] + frozen['sources']['checkpoints']
            return frozen, [save(engine.store, run_id, ref) for ref in references]
        preparation = asyncio.create_task(asyncio.to_thread(prepare))
        try:
            frozen, archives = await asyncio.shield(preparation)
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
        if (snapshot_id != frozen['snapshot_id'] or engine.store.get('runs', run_id) != run
                or engine.store.get('run_deletions', run_id)):
            raise ValueError('授权前任务、证据或配置已变化，请重新读取快照。')
        old = current(engine.store, run_id)
        if not old:
            body = {**frozen['policy_amendment'], 'operator_instruction': authorization.strip()}
            identifier = f'{run_id}:rapid-continuation:{sha(body)}'
            record = {'id': identifier, 'run_id': run_id, 'scope': SCOPE,
                'snapshot_id': snapshot_id, 'policy_amendment': body, 'sources': frozen['sources'],
                'sha256': sha(body), 'status': 'running', 'created_at': now(), 'updated_at': now(), 'error': None}
            puts = [(COLLECTION, identifier, record), ('runs', run_id,
                {**run, 'updated_at': now(), 'state': {**run.get('state', {}), 'rapid_continuation': identifier}})]
            deletes = []
            # Frozen source checkpoints are retained in the authorization archive.
            # Clear only downstream cached results so the new graph starts at diagnosis.
            for archive in archives:
                collection, key = archive['collection'], archive['key']
                archived = f'{identifier}:{collection}:{key}'
                puts.append(('rapid_continuation_archives', archived,
                    {'id': archived, 'run_id': run_id, 'source': archive}))
                if collection in {'steps', 'stage_contexts'}:
                    deletes.append((collection, key))
            engine.store.batch(puts, deletes)
            engine.artifact(run_id, f'rapid-continuation/{sha(body)}/authorization.json', encoded(record), 'review')
            engine.store.event(run_id, 'experiment.rapid_continuation_authorized',
                '本轮停止规则已显式修订，正式工作流从诊断进入局部修订和成对验证。',
                {'authorization_id': identifier, 'sha256': record['sha256'], 'attempt_limit': body['attempt_limit']})
        await engine._start(run_id)
        return view(engine.store, run_id)


def graph_entry(engine, run_id):
    record = validated(engine.store, run_id)
    from .performance_policy import current as performance_policy
    performance = performance_policy(engine.store, run_id)
    return ((performance or record)['id'], 'tuning') if record else None


def sync_status(store, run_id):
    record = current(store, run_id)
    if record:
        run = store.get('runs', run_id)
        record.update(status='interrupted' if run['status'] in {'paused', 'cancelled'} else
                      'blocked' if run['status'] == 'failed' else run['status'],
                      phase=run.get('stage'), error=run.get('error'), updated_at=now())
        if run['status'] == 'completed':
            record['completed_at'] = now()
        store.put(COLLECTION, record['id'], record)
