"""Explicit operator direction, separate from independent scientific approval.

Completing a frozen ablation is not a claim that an inconclusive training
diagnosis has been resolved. Raw decisions, paid inputs and plans stay intact.
"""
import copy

from .experiment_workflow import sha
from .storage import now

COLLECTION = 'experiment_progression_requests'
GUIDANCE = '''用户已明确授权继续下一批三方评审，先完成 ETTh1 冻结计划缺失的消融，再推进 Weather 验证和正式参数搜索。
这是新的工作方向，不是三方批准。保留先前分析与未定根因；本轮仍须三方独立批准具体批次。
ETTh1 两项消融使用原计划、原代码与原种子；不激活 candidate_only 诊断补丁，不重跑 baseline/proposed，不把消融当作过拟合/不稳定原因诊断。
已有真实受控检查及其三方结果评审可复用；没有新的具体失败时不要再申请补日志、重跑 CPU 检查或重建检查报告。
存在待执行冻结消融时，考虑 coverage_validation：action=diagnose，选该 ablation unit，changes=[]，保留 cause=unknown/cause_status=hypothesis，列出与同种子 proposed 的全部实际 factor_paths。这仅补模块贡献证据，仍须记录不能证明根因或稳定。
ETTh1 覆盖完成后保留全部结果与未解决告警；若没有进一步安全障碍，选择 stop 结束当前数据集探索以使宿主队列推进 Weather，不进入 held-out 测试或宣称科学验收通过。
Weather 由原队列单独配置、评审和验证；新计划默认单种子。正式参数搜索须有该数据集有效成对控制，三方批准一个真实参数变化后执行，再重新判断。不能复用 ETTh1 结论证明 Weather 稳定。
若有新的执行、安全、完整性或资源阻塞，列明具体证据；用户工作方向不能覆盖门禁。'''


def register(engine, run_id, instruction):
    from .experiment_decisions import _binding
    from .dataset_configuration_queue import current
    run = engine.store.get('runs', run_id)
    if run.get('status') not in {'paused', 'blocked'} or not instruction.strip():
        raise ValueError('Register a progression request only while paused with an explicit instruction')
    _, binding = _binding(engine, run_id)
    queue = current(engine.store, run_id)
    if not queue or queue['status'] != 'active':
        raise ValueError('An active dataset queue is required')
    body = {'instruction': instruction.strip(), 'initial_binding': binding, 'queue_id': queue['id'],
            'new_plan_seed_policy': 'single', 'preserve_frozen_plans': True,
            'rerun_completed': False, 'scientific_approval': False}
    record = {'id': run_id, 'run_id': run_id, 'body': body, 'sha256': sha(body), 'created_at': now()}
    old = engine.store.get(COLLECTION, run_id)
    if old and old['sha256'] != record['sha256']:
        raise ValueError('An existing progression request must be retained; do not overwrite it')
    engine.store.put(COLLECTION, run_id, old or record)
    return old or record


def decision_context(engine, run_id, evidence, context, instruction):
    # The later explicit architecture-first request supersedes the old
    # "coverage then Weather" direction, without rewriting its saved receipt.
    if evidence.get('dataset_optimization'):
        return context, instruction, ':architecture-first-v1'
    record = engine.store.get(COLLECTION, run_id)
    if not record:
        return context, instruction, ''
    body = record['body']
    queue = evidence.get('configuration_queue') or {}
    if record['sha256'] != sha(body) or record['run_id'] != run_id or queue.get('id') != body['queue_id']:
        raise ValueError('Progression direction identity changed')
    if evidence['binding']['cycle'] == body['initial_binding']['cycle'] and evidence['binding'] != body['initial_binding']:
        raise ValueError('Progression must retain the original frozen plan/code/configuration')
    context = copy.deepcopy(context)
    context['operator_progression'] = record
    context['operator_progression']['scope'] = 'User direction only. No approval inherited; original diagnosis remains unresolved.'
    from .experiment_analysis import _param_diff
    candidates = []
    for unit in evidence['units']:
        if unit['kind'] != 'ablation' or not unit['available']:
            continue
        jobs = []
        for name in unit['jobs']:
            params = evidence['job_parameters'][name]
            controls = [r for r in evidence['records'] if r['status'] == 'completed'
                and r['params'].get('variant') == 'proposed'
                and all(r['params'].get(k) == params.get(k) for k in ('seed','dataset','benchmark_task_id','phase'))]
            jobs.append({'job': name, 'seed': params['seed'], 'same_seed_controls': [
                {'trial_id': r['id'], 'factor_paths': [d['path'] for d in _param_diff(r['params'], params)]}
                for r in controls]})
        candidates.append({'unit_id': unit['id'], 'jobs': jobs,
            'scope': 'All frozen differences, including contract metadata; not an isolated causal factor.'})
    context['operator_progression']['frozen_ablation_candidates'] = candidates
    return context, instruction + '\n' + GUIDANCE, ':progression-' + record['sha256'][:16]


def validate_coverage(proposal, evidence):
    """A bounded contribution experiment may coexist with an unknown cause."""
    response = proposal['training_response']
    causal = evidence['training_investigation'].get('causal_followup') or {}
    outcome = causal.get('outcome') or {}
    classification = outcome.get('summary', {}).get('classification', {})
    reviews = causal.get('analyses', [])
    verdicts = classification.get('verdicts', {})
    if (causal.get('status') != 'completed' or len(reviews) != 3
            or {r.get('role') for r in reviews} != {'director', 'engineer', 'scout'}
            or not all(r.get('read_receipts') for r in reviews)
            or classification.get('complete_counterfactual') is not True
            or classification.get('checkpoint_restoration_consistent') is not True
            or any(verdicts.get(k) != 'inconclusive' for k in ('overfitting', 'update_instability'))
            or not all({f.get('hypothesis'): f.get('verdict') for f in r.get('findings', [])} == verdicts
                       for r in reviews)):
        raise ValueError('Coverage validation requires completed, independently reviewed inconclusive controlled evidence')
    if (proposal['action'] != 'diagnose' or proposal['changes']
            or response['cause'] != 'unknown' or response['cause_status'] != 'hypothesis'
            or not response['factor_paths'] or evidence.get('failure_categories')):
        raise ValueError('Coverage validation must preserve the unresolved cause and original code')
    unit = next((u for u in evidence['units'] if u['id'] == proposal['unit_id']), None)
    if not unit or unit['kind'] != 'ablation' or not unit['available'] or unit['complete']:
        raise ValueError('Coverage validation may execute only a missing frozen ablation batch')
    if not any(v.get('comparable') and v.get('task_id') == unit.get('task_id')
               for v in evidence.get('comparisons', {}).values()):
        raise ValueError('Coverage validation requires an intact same-task paired comparison')
    from .experiment_analysis import _param_diff
    for name in unit['jobs']:
        candidate = evidence['job_parameters'][name]
        controls = [r for r in evidence['records'] if r['status'] == 'completed'
                    and r['params'].get('variant') == 'proposed'
                    and all(r['params'].get(k) == candidate.get(k)
                            for k in ('seed', 'dataset', 'benchmark_task_id', 'phase'))]
        expected = [sorted(d['path'] for d in _param_diff(r['params'], candidate)) for r in controls]
        if candidate.get('phase') != 'validation' or candidate.get('variant') != 'ablation' or not any(
                set(paths) == set(response['factor_paths']) for paths in expected):
            raise ValueError('Frozen ablation requires a completed same-seed control and exact declared differences. '
                'factor_paths are parameter-relative (no /job_parameters/<job> prefix; variant/phase/seed excluded), '
                f'and include frozen contract metadata. Expected for {name}: {expected}')
