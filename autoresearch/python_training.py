"""Agent-callable execution of one host-frozen validation job, never arbitrary code."""
from __future__ import annotations

import copy
import hashlib

from .training_context import KEY

TOOL = 'python.run'
GUIDANCE = ('执行当前冻结的验证训练任务。数据集、参数、随机种子和资源限制见上下文。'
    '调用 python.run，宿主从保存的已批准版本加载 Python 代码并运行。不得修改参数、读取留出集结果，'
    '也不得把运行完成解释为科学验收通过。根据真实工具回执返回 trial_id 和 status。')
FINAL = {'type': 'object', 'properties': {'trial_id': {'type': 'string'},
    'status': {'const': 'completed'}}, 'required': ['trial_id', 'status'], 'additionalProperties': False}


def code_hash(program):
    return hashlib.sha256(program['code'].encode('utf-8')).hexdigest()


def jobs(plan, stage, transport=None):
    """The same frozen ordering used by the controller, including producer/finalizer jobs."""
    from . import benchmark_matrix
    if benchmark_matrix.enabled(plan):
        if transport:
            raise ValueError('Comparison transport v1 does not support a benchmark matrix')
        return benchmark_matrix.jobs(plan, stage)
    if transport:
        from .comparison_transport import ordered_validation
        rows = ordered_validation(plan, transport['contract'])
        return rows[:2] if stage == 'experiments' else rows[2:]
    rows = []
    pairs = enumerate(plan['validation_pairs'])
    for index, pair in pairs:
        for variant in ('baseline', 'proposed'):
            name = f'initial-{variant}' if index == 0 else f'grid-{index}-{variant}'
            rows.append((name, {**pair[variant], 'phase': 'validation',
                                'seed': pair.get('seed', plan['selection_seed'])}))
    for index, params in enumerate(plan.get('ablation_candidates', [plan['ablation_params']])):
        rows.append(('ablation' if index == 0 else f'ablation-{index}',
            {**params, 'phase': 'validation', 'seed': params.get('seed', plan['selection_seed'])}))
    return rows[:2] if stage == 'experiments' else rows[2:]


def packet(engine, run_id, trial_name, params):
    from .engine import digest
    from .execution_contract import constraints
    run, outputs = engine.store.get('runs', run_id), engine.outputs(run_id)
    design = outputs['design']
    program = engine.store.get('programs', run_id) or outputs['implementation']
    from . import benchmark_matrix
    plan = engine.experiment_plan(run_id)
    task = benchmark_matrix.task_for(plan, params) if benchmark_matrix.enabled(plan) else None
    from .experiment_decisions import session
    from .experiment_memory import recall
    from .rapid_continuation import trial_limit, effective_policy
    from .performance_policy import context_fields, effective
    from .dataset_configuration_queue import budget as dataset_budget
    budget = dataset_budget(engine.store, run_id)
    pending = (session(engine, run_id) or {}).get('pending') or {}
    return {KEY: 1, 'experiment_workflow_protocol': 1, 'stage': run['stage'], 'research_cycle': engine.cycle(run_id),
        'experiment_memory': recall(engine, run_id),
        **({'experiment_batch': {k: pending[k] for k in ('unit_id', 'decision_id', 'proposal_sha256')}} if pending else {}),
        **{k: copy.deepcopy(design[k]) for k in ('dataset', 'evaluation', 'primary_metric',
            'direction', 'resource_plan', 'limitations') if k in design},
        **context_fields(engine.store, run_id),
        'primary_metric': effective(engine.store, run_id, design)[0],
        'direction': effective(engine.store, run_id, design)[1],
        'execution_constraints': constraints(run['config'], dataset_budget=budget),
        'dataset_budget': budget, 'trial_budget': trial_limit(engine.store, run_id),
        'iteration_policy': effective_policy(engine.store, run_id, plan.get('iteration_policy')),
        **({'benchmark_task': benchmark_matrix.metadata(task)} if task else {}),
        'plan_sha256': digest(plan),
        'program': {**{k: program[k] for k in ('artifact_id', 'delivery_id', 'delivery_hash',
            'spec_hash', 'implementation_document_sha256', 'dependencies') if k in program},
            'code_sha256': code_hash(program), 'revision': program.get('revision', 0)},
        'trial': {'name': trial_name, 'params': copy.deepcopy(params)}}


def arguments(context):
    return {'trial_name': context['trial']['name'], 'plan_sha256': context['plan_sha256'],
            'code_sha256': context['program']['code_sha256']}


async def execute(harness, run_id, role, handoff_id, args):
    from .engine import ResearchBlocked, digest
    from .comparison_transport import runtime_transport
    from .tool_authorization import authorize
    authorize(harness, run_id, role, handoff_id, TOOL, args)
    engine, store = harness.engine, harness.engine.store
    assignment = store.get('handoffs', handoff_id) or {}
    context = (store.get('handoff_inputs', handoff_id) or {}).get('context', {})
    run = store.get('runs', run_id)
    if (role != 'engineer' or context.get(KEY) != 1
            or not str(assignment.get('purpose', '')).startswith('experiment_train:')
            or run['stage'] not in {'experiments', 'tuning'} or context.get('stage') != run['stage']
            or context.get('research_cycle') != engine.cycle(run_id) or args != arguments(context)):
        raise ResearchBlocked('Python training requires the exact current frozen validation assignment')
    engine.check_active(run_id)
    await engine._read_consensus_check(run_id, engine._execution_gate, run_id)
    current = packet(engine, run_id, context['trial']['name'], context['trial']['params'])
    # Harness preparation may carry internal protocol metadata, but cannot
    # broaden the execution contract beyond this persisted packet.
    if any(current[key] != context.get(key) for key in current
           if key != 'experiment_workflow_protocol' or key in context):
        raise ResearchBlocked('Python training dataset, code, plan or resource binding changed')
    plan = engine.experiment_plan(run_id)
    if not plan or digest(plan) != args['plan_sha256']:
        raise ResearchBlocked('Python training requires a frozen experiment plan')
    transport = await engine._read_consensus_check(run_id, runtime_transport, engine, run_id)
    candidates = dict(jobs(plan, run['stage'], transport))
    params = context['trial']['params']
    from .experiment_decisions import session, authorize_job
    if session(engine, run_id):
        authorize_job(engine, run_id, args['trial_name'], params)
        candidates = dict(jobs(plan, 'experiments', transport) + jobs(plan, 'tuning', transport))
        from .parameter_search import jobs as search_jobs
        candidates.update(search_jobs(engine, run_id, plan))
    if candidates.get(args['trial_name']) != params or params.get('phase') != 'validation':
        raise ResearchBlocked('Python training parameters are outside the approved stage plan')
    result = await engine._trial(run_id, harness, args['trial_name'], copy.deepcopy(params))
    # Keep the full runner record and logs in experiments. Only measured validation
    # scalars and identifiers enter the next model request (never arrays/stdout).
    return {'trial_id': result['id'], 'status': result['status'],
        'research_cycle': result.get('research_cycle', engine.cycle(run_id)),
        'code_revision': result.get('code_revision'), 'plan_sha256': args['plan_sha256'],
        'metrics': {k: v for k, v in result.get('metrics', {}).items() if k.startswith('val_')},
        'dataset': {k: v for k, v in (result.get('evaluation') or {}).items()
                    if k in {'dataset_id', 'phase', 'seed', 'identity_consistent'}},
        'duration_seconds': result.get('duration_seconds'), 'provenance': result.get('provenance'),
        'scientific_approval': False}


def completed(engine, run_id, key, reply):
    """A model declaration cannot commit a successful training handoff."""
    from .engine import ResearchBlocked
    from .comparison_transport import scientific_params
    context = engine.store.get('handoff_inputs', key)['context']
    progress = engine.store.get('handoff_progress', key) or {}
    receipts = [row['result'] for row in progress.get('tool_results', [])
        if row.get('call') == {'name': TOOL, 'arguments': arguments(context)} and 'result' in row]
    result = engine.store.get('experiments', f'{run_id}:{reply.get("trial_id")}') or {}
    if (not any(r.get('trial_id') == reply.get('trial_id') and r.get('status') == 'completed' for r in receipts)
            or reply.get('status') != 'completed' or result.get('run_id') != run_id
            or result.get('status') != 'completed' or result.get('superseded')
            or result.get('research_cycle', 1) != engine.cycle(run_id)
            or result.get('code_revision') != context['program']['revision']
            or scientific_params(result) != context['trial']['params']):
        raise ResearchBlocked('Agent completion has no matching successful Python training receipt')
    return result


async def run(engine, harness, run_id, trial_name, params):
    """Live agents request the tool; demo retains its explicitly simulated controller."""
    if engine.store.get('runs', run_id)['mode'] != 'live':
        return await engine._trial(run_id, harness, trial_name, params)
    context = packet(engine, run_id, trial_name, params)
    purpose = f'experiment_train:{context["research_cycle"]}:{context["stage"]}:{trial_name}'
    key = harness.decision_key(run_id, 'engineer', purpose, GUIDANCE, context, FINAL)
    reply = await harness.ask(run_id, 'engineer', purpose, GUIDANCE, context, FINAL)
    result = completed(engine, run_id, key, reply)
    from .training_adjustment import after_trial
    from .experiment_decisions import session
    if not session(engine, run_id):
        await after_trial(engine, harness, run_id, result)
    return result
