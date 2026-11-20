"""Validation-only, equally weighted comparison of every declared task with its own ablation."""
import math
import re

from .experiment_plan import _signature
from .evaluation import _finite, _mean

METRIC = 'val_mechanism_macro_mse'


def binding(design, tasks):
    criteria = (design.get('acceptance') or {}).get('criteria', [])
    if not any(c['metric'] == METRIC for c in criteria):
        return None
    from .benchmark_matrix import dimensions
    axis = design.get('paper_selection_axis')
    if (design.get('baseline_mode') != 'paper_reported' or not axis or design['primary_metric'] != 'val_mse'
            or design['direction'] != 'minimize' or len(criteria) != 1 or criteria[0]['direction'] != 'minimize'
            or criteria[0].get('required', True) is not True):
        raise ValueError('全矩阵机制 MSE 需要明确的验证选择轴、val_mse 最小化和独立的机制验收条款。')
    seed = tasks[0]['plan']['validation_seeds']
    groups = {}
    for task in tasks:
        plan = task['plan']
        dims = dimensions(task)
        if (len(seed) != 1 or plan['validation_seeds'] != seed or len(plan['validation_pairs']) != 1
                or len(plan['ablation_candidates']) != 1 or axis not in dims
                or type(dims[axis]) is not int):
            raise ValueError('每个机制比较任务须具有相同固定种子、一份候选和一份已声明消融。')
        value = dims.pop(axis)
        groups.setdefault(_signature(dims), set()).add(value)
    if any(values != next(iter(groups.values())) for values in groups.values()):
        raise ValueError('全矩阵等权机制比较要求每个分组使用相同的输入候选集合。')
    return {'version': 1, 'metric': METRIC, 'source_metric': 'val_mse', 'direction': 'minimize',
            'phase': 'validation', 'reduction': 'equal_mean_all_tasks_per_variant',
            'reference_variant': 'ablation', 'candidate_variant': 'proposed',
            'relative_denominator_floor': 1e-12, 'selection_axis': axis,
            'window_count_metric': 'val_window_count', 'origin_fingerprint': 'evaluation.validation_origins_sha256',
            'tie_break': 'smallest_selection_axis'}


def evaluate(plan, trials):
    from .benchmark_matrix import dimensions, jobs
    from .engine import digest
    from .split_identity import compatible
    contract = plan['matrix_acceptance']
    criterion = plan['acceptance']['criteria'][0]
    check = {**criterion, 'passed': None, 'relative_improvement': None}
    result = {'phase': 'validation', 'criteria_complete': plan['acceptance']['complete'],
              'research_outcome': 'inconclusive', 'checks': [check], 'test_reuse_for_tuning_allowed': False}
    def missing(reason):
        check['reason'] = reason
        return result

    records = [r for r in trials if not r.get('superseded') and (r.get('params') or {}).get('phase') == 'validation']
    if not records:
        return missing('等待冻结的验证任务交付。')
    expected = {_signature(p) for stage in ('experiments', 'tuning') for _, p in jobs(plan, stage)}
    plan_hash = digest(plan)
    ids, scores, origins = set(), {'proposed': [], 'ablation': []}, {}
    dataset = code = None
    groups = {}
    for task in plan['task_plans']:
        dims = dimensions(task)
        dims.pop(contract['selection_axis'])
        groups[task['task_id']] = _signature(dims)
    for row in records:
        params = row['params']
        key = _signature(params)
        metrics = row.get('metrics') or {}
        evidence = row.get('evaluation') or {}
        score = metrics.get('val_mse')
        windows = metrics.get('val_window_count')
        fingerprint = evidence.get('validation_origins_sha256')
        if (key not in expected or not row.get('id') or row['id'] in ids or row.get('status') != 'completed'
                or row.get('experiment_plan_sha256') != plan_hash or row.get('evaluation_errors')
                or not re.fullmatch('[0-9a-f]{64}', str(row.get('code_sha256', '')))
                or not evidence.get('dataset_id') or not (evidence.get('split_fingerprints') or {}).get('validation')
                or not re.fullmatch('[0-9a-f]{64}', str(fingerprint))
                or metrics.get('completed') != 1 or not _finite(score) or score < 0
                or not _finite(windows) or windows <= 0 or int(windows) != windows):
            return missing('验证回执缺失、重复、失败，或与冻结配置、度量和有效窗口合同不一致。')
        if dataset is not None and (not compatible(dataset, evidence) or code != row['code_sha256']):
            return missing('验证回执的数据划分或模型代码版本不一致。')
        dataset, code = evidence, row['code_sha256']
        group = groups[params['benchmark_task_id']]
        if origins.setdefault(group, (windows, fingerprint)) != (windows, fingerprint):
            return missing('同一组内的有效验证起点或窗口数不一致。')
        expected.remove(key)
        ids.add(row['id'])
        scores[params['variant']].append(score)
    if expected:
        return missing('冻结矩阵的验证回执尚未收齐。')
    proposed, reference = _mean(scores['proposed']), _mean(scores['ablation'])
    difference = reference - proposed
    delta = difference / max(reference, contract['relative_denominator_floor'])
    if not math.isfinite(delta):
        return missing('机制相对改善不是有限数值。')
    threshold = criterion['minimum_relative_improvement']
    passed = delta > threshold if criterion.get('comparison') == 'greater_than' else delta >= threshold
    check.update(proposed=proposed, reference=reference, difference=difference, relative_improvement=delta,
                 passed=passed, trial_ids=sorted(ids))
    if not passed or result['criteria_complete']:
        result['research_outcome'] = 'positive' if passed else 'negative'
    return result
