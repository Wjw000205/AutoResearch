"""Task-derived, independently reviewed attempt budgets for dataset rounds."""
from __future__ import annotations

import copy

from jsonschema import ValidationError
from .prose_schema import validate

from .experiment_workflow import sha
from .storage import now

COUNTS = ('configuration_checks', 'validation', 'ablations', 'final_tests', 'optimization', 'diagnostics', 'retries')
SCHEMA = {'type': 'object', 'additionalProperties': False,
    'required': [*COUNTS, 'rationale', 'requirement_refs'],
    'properties': {**{k: {'type': 'integer', 'minimum': 1 if k == 'configuration_checks' else 0} for k in COUNTS},
        'rationale': {'type': 'string', 'minLength': 1, 'maxLength': 1600},
        'requirement_refs': {'type': 'array', 'minItems': 1, 'maxItems': 12,
            'items': {'type': 'string', 'minLength': 1, 'maxLength': 500}}}}


def total(proposal):
    try:
        validate(proposal, SCHEMA)
    except ValidationError as exc:
        raise ValueError('数据集预算方案需说明任务依据及各类执行次数：' + exc.message) from exc
    return sum(proposal[k] for k in COUNTS)


def check(proposal, plan):
    limit = total(proposal)
    expected = {'validation': 2 * len(plan['validation_pairs']),
        'ablations': len(plan['ablation_candidates']), 'final_tests': 2 * plan['final_pairs']}
    if any(proposal[k] != v for k, v in expected.items()):
        raise ValueError('预算中的验证、消融与最终测试次数必须匹配配置实际声明的任务和全部种子：' + str(expected))
    return limit


def check_supplements(store, run_id, active):
    """Human-authorized failed-interface retries; never scientific trial credit."""
    keys = active.get('configuration_retry_authorizations', [])
    if len(keys) != len(set(keys)):
        raise ValueError('Duplicate configuration retry authorization')
    for key in keys:
        row = store.get('dataset_configuration_retry_authorizations', key) or {}
        body = row.get('body', {})
        if (row.get('sha256') != sha(body) or body.get('run_id') != run_id
                or body.get('setup_id') != active['setup_id'] or body.get('cycle') != active['cycle']
                or body.get('dataset') != active['dataset'] or body.get('extra_configuration_checks') != 1
                or body.get('frozen_budget_sha256') != sha(active.get('frozen_budget_plan'))):
            raise ValueError('Configuration retry authorization binding changed')
    return len(keys)


def authorize_configuration_retry(engine, run_id, trial_id, *, instruction):
    """Persist one explicit operator-authorized retry against an actual failed check."""
    from . import dataset_configuration_queue as queue
    run = engine.store.get('runs', run_id) or {}
    if run.get('status') not in {'paused', 'blocked', 'failed'} or not instruction.strip():
        raise ValueError('Configuration retry needs a stopped run and explicit instruction')
    record = queue.current(engine.store, run_id)
    if not record or not queue.task_budgeted(record) or record['status'] != 'active':
        raise ValueError('Configuration retry needs an active dataset budget')
    active = record['batches'][-1]
    result = engine.store.get('experiments', run_id + ':' + trial_id) or {}
    if (active.get('budget_status') != 'approved' or not active.get('frozen_budget_plan')
            or result.get('run_id') != run_id or result.get('research_cycle') != active['cycle']
            or result.get('status') != 'failed' or result.get('params', {}).get('phase') != 'code_check'
            or result.get('provenance') != 'real_experiment_execution'):
        raise ValueError('Configuration retry requires a real failed check of this approved dataset')
    key = run_id + ':configuration-retry:' + sha({'setup_id':active['setup_id'],'trial_id':trial_id})
    existing = engine.store.get('dataset_configuration_retry_authorizations', key)
    if existing:
        if existing['body']['result_sha256'] != sha(result):
            raise ValueError('Failed configuration check changed after authorization')
        return existing
    body = {'run_id':run_id, 'setup_id':active['setup_id'], 'cycle':active['cycle'],
        'dataset':active['dataset'], 'trial_id':trial_id, 'result_sha256':sha(result),
        'frozen_budget_sha256':sha(active['frozen_budget_plan']), 'extra_configuration_checks':1,
        'instruction':instruction.strip(), 'scientific_trial_allowance_changed':False}
    row = {'id':key,'body':body,'sha256':sha(body),'created_at':now()}
    active.setdefault('configuration_retry_authorizations', []).append(key)
    active['attempt_limit'] += 1
    engine.store.batch([('dataset_configuration_retry_authorizations',key,row),(queue.COLLECTION,record['id'],record)])
    engine.store.event(run_id,'experiment.configuration_retry_authorized',
        '按用户继续修复指令，为本数据集增加一次 CPU 接口复查；失败消耗及科学实验额度保留。',
        {'authorization_id':key,'trial_id':trial_id,'attempt_limit':active['attempt_limit']})
    return row


def propose(engine, run_id, saved):
    from .experiment_attempt_policy import unlimited
    if unlimited(engine.store, run_id):
        return
    from . import dataset_configuration_queue as queue
    record = queue.current(engine.store, run_id)
    if not queue.task_budgeted(record):
        return
    active = record['batches'][-1]
    proposal = saved['draft'].get('budget_plan')
    limit = check(proposal, saved['plan']) + check_supplements(engine.store, run_id, active)
    binding = {'setup_id': saved['id'], 'plan_sha256': sha(saved['plan']),
        'amendment_sha256': saved['candidate']['amendment_sha256'], 'budget_plan_sha256': sha(proposal)}
    if active.get('budget_status') == 'approved':
        if active.get('budget_binding') == binding and active['attempt_limit'] == limit:
            return
        if (saved.get('repair_budget_binding') != active.get('budget_binding')
                or proposal != active.get('budget_proposal') or limit != active['attempt_limit']):
            raise ValueError('已冻结的数据集预算不能被新提案覆盖。')
    if active.get('frozen_budget_plan') and proposal != active['frozen_budget_plan']:
        raise ValueError('接口修复必须保留本数据集已批准的预算方案与任务覆盖。')
    used = queue.attempts(engine.store, run_id, record).get(active['cycle'], 0)
    if limit < used + 1 + saved['plan']['planned_trials']:
        from .engine import ResearchBlocked
        raise ResearchBlocked('本数据集剩余预算不足以完成接口检查与已声明实验；已有消耗保留。')
    active.update(budget_status='proposed', budget_proposal=copy.deepcopy(proposal), budget_binding=binding,
        proposed_attempt_limit=limit)
    engine.store.put(queue.COLLECTION, record['id'], record)


def approve(engine, run_id, saved):
    from .experiment_attempt_policy import unlimited
    if unlimited(engine.store, run_id):
        return
    from . import dataset_configuration_queue as queue
    from .dataset_experiments import _reviews
    record = queue.current(engine.store, run_id)
    if not queue.task_budgeted(record):
        return
    _reviews(engine, run_id, saved)
    active = record['batches'][-1]
    binding = {'setup_id': saved['id'], 'plan_sha256': sha(saved['plan']),
        'amendment_sha256': saved['candidate']['amendment_sha256'], 'budget_plan_sha256': sha(saved['draft']['budget_plan'])}
    if active.get('budget_binding') != binding or active.get('budget_proposal') != saved['draft']['budget_plan']:
        raise ValueError('数据集预算与独立评审的配置版本不一致。')
    limit = check(saved['draft']['budget_plan'], saved['plan']) + check_supplements(engine.store, run_id, active)
    if active.get('budget_status') == 'approved':
        if active['attempt_limit'] != limit:
            raise ValueError('已冻结的数据集预算发生变化。')
        return
    active.update(budget_status='approved', attempt_limit=limit, budget_approved_at=now(),
        frozen_budget_plan=copy.deepcopy(saved['draft']['budget_plan']))
    engine.store.put(queue.COLLECTION, record['id'], record)
    engine.store.event(run_id, 'experiment.dataset_budget_approved', '本数据集预算已按任务配置核算并通过独立评审。',
        {'dataset': active['dataset'], 'attempt_limit': limit, 'binding': binding})


def prepare(engine, run_id, *, instruction):
    """Explicitly migrate a stopped configuration without altering paid inputs."""
    from . import dataset_configuration_queue as queue
    from .dataset_experiments import current as setup, COLLECTION as SETUPS, source_evidence
    from .experiment_decisions import _archive
    run = engine.store.get('runs', run_id) or {}
    if (run.get('status') not in {'paused', 'blocked', 'failed'}
            or run_id in engine.tasks and not engine.tasks[run_id].done()
            or engine.store.active_reasoning(run_id, {'running', 'queued', 'starting', 'turn_start_dispatched',
                'thread_start_dispatched', 'thread_resume_dispatched'})):
        raise ValueError('需要已停止且没有活动请求的实验任务。')
    record = queue.current(engine.store, run_id)
    if not record or record['status'] != 'active' or not queue.batch(engine.store, run_id):
        raise ValueError('需要已绑定当前轮次且尚未结束的数据集队列。')
    if not isinstance(instruction, str) or not 1 <= len(instruction.strip()) <= 4000:
        raise ValueError('需要明确的数据集预算修订指令。')
    if queue.task_budgeted(record):
        return queue.view(engine.store, run_id)
    saved = setup(engine, run_id)
    if saved and (saved.get('candidate') or saved.get('check')):
        raise ValueError('当前配置已进入独立核验或执行，不能更换其已派发预算方案。')
    evidence = source_evidence(engine, run_id, saved) if saved else None
    previous = _archive(engine, run_id, {'queue': record, 'setup': saved})
    body = {'queue_id': record['id'], 'config_sha256': record['body']['config_sha256'],
        'scope': 'per_dataset_task', 'instruction': instruction.strip(), 'previous': previous,
        'scientific_approval': False}
    key = run_id + ':dataset-budget:' + sha(body)
    policy = {'id': key, 'run_id': run_id, 'body': body, 'sha256': sha(body), 'created_at': now()}
    record['budget_policy'] = policy
    record['batches'][-1].update(attempt_limit=None, budget_status='pending')
    puts = [('dataset_budget_policies', key, policy), (queue.COLLECTION, record['id'], record)]
    if saved:
        evidence['task_requirements'] = run.get('objective', '')
        ref = _archive(engine, run_id, evidence)
        request = copy.deepcopy(saved['request'])
        request.update(trial_budget=None, validation_evidence=ref, evidence_sha256=ref['sha256'],
            dataset_budget={'dataset': record['batches'][-1]['dataset'], 'scope': 'per_dataset_task',
                'status': 'pending', 'attempt_limit': None, 'policy_id': key})
        request['dataset_configuration'].update(budget_schema=SCHEMA, task_requirements_path='/task_requirements',
            budget_instruction='依据本数据集的任务、对照、种子、消融、优化与诊断需要提出 budget_plan；不继承其他数据集额度。')
        fresh = {k: v for k, v in saved.items() if k not in {'draft', 'configuration_repairs', 'error', 'blockers'}}
        fresh.update(request=request, status='configuring', previous_setup=previous, updated_at=now())
        puts.append((SETUPS, saved['id'], fresh))
        puts.append(('runs', run_id, {**run, 'status': 'paused', 'error': None, 'updated_at': now()}))
    engine.store.batch(puts)
    engine.store.event(run_id, 'experiment.dataset_budget_policy_updated',
        '各数据集将依据自身任务配置提出预算并独立核验，已有消耗与付费请求保留。',
        {'policy_id': key, 'previous': previous})
    return queue.view(engine.store, run_id)
