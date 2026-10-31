"""Readable experiment results from bounded saved records, never execution gates."""
from collections import defaultdict
from datetime import datetime
import hashlib
import json
import math


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _seed(value):
    return type(value) is int or isinstance(value, str) and bool(value.strip())


def _dataset(trial):
    params, evidence = trial['params'], trial.get('evaluation') or {}
    common = params.get('common') if isinstance(params.get('common'), dict) else {}
    data = common.get('data') if isinstance(common.get('data'), dict) else {}
    identity = evidence.get('dataset_id')
    identity = identity if isinstance(identity, str) else None
    counts = data.get('counts')
    from .fair_comparison import task_signature
    try:
        dimensions = task_signature(params)
    except ValueError:
        dimensions = {}
    counts = counts if isinstance(counts, list) and len(counts) == 3 and all(type(n) is int and n > 0 for n in counts) else None
    return {'id': identity, 'name': params.get('dataset') or dimensions.get('dataset') or identity or '数据集未记录',
            'kind': data.get('kind', 'recorded'), 'phase': params.get('phase'),
            'split_fingerprints': evidence.get('split_fingerprints'),
            'input_length': dimensions.get('input_length'), 'horizon': dimensions.get('output_length'), 'channels': dimensions.get('channels'),
            'task_id': params.get('benchmark_task_id'), 'paper_protocol': params.get('benchmark_source'),
            'task_dimensions': {k: v for k, v in dimensions.items() if k not in {'dataset_assets', 'benchmark_source', 'benchmark_task_id'}},
            'configured_counts': dict(zip(('train', 'validation', 'test'), counts)) if counts else {},
            'split_materialization': evidence.get('split_materialization') or {},
            'data_seeds': data.get('data_seeds') if isinstance(data.get('data_seeds'), list) else [],
            'configuration_source': 'saved_experiment_params',
            'schema': data.get('summary_schema_version')}


def _review(store, run_id, values, groups, truncated):
    """Only display a saved validation verdict for the exact still-current trials.

    The stored program-reported comparison is authoritative for its aggregation;
    Raw metric means must not replace normalized inputs or confidence criteria.
    """
    from .run_views import _one, _field
    from .experiment_views import _bounded
    cycles = {r.get('research_cycle') for r in values}
    if truncated or len(cycles) != 1 or type(next(iter(cycles), None)) is not int:
        return None
    cycle = next(iter(cycles))
    fields = {k: _field(k) for k in ('run_id', 'cycle', 'revision_hash', 'completed_at')}
    fields.update(acceptance=_bounded('validation_summary.acceptance_checks', 65536),
                  baseline_ids=_bounded('validation_summary.best_baseline.trial_ids', 8192),
                  proposed_ids=_bounded('validation_summary.best_proposed.trial_ids', 8192))
    saved = _one(store, 'research_cycles', fields, key=f'{run_id}:{cycle}')
    if not saved or saved['run_id'] != run_id or saved['cycle'] != cycle:
        return None
    acceptance = saved.get('acceptance')
    if not isinstance(acceptance, dict) or acceptance.get('phase') != 'validation':
        return None
    by_id = {row['id']: row for row in values}
    matched = []
    try:
        completed = datetime.fromisoformat(saved['completed_at'])
        for variant, field in [('baseline', 'baseline_ids'), ('proposed', 'proposed_ids')]:
            ids = saved[field]
            if not isinstance(ids, list) or not ids or len(set(ids)) != len(ids):
                return None
            for tid in ids:
                row = by_id[tid]
                if (row['status'] != 'completed' or row['params'].get('variant') != variant
                        or row['params'].get('phase') != 'validation'
                        or datetime.fromisoformat(row['source_updated_at']) > completed):
                    return None
            matches = [g for g in groups if set(g['trial_ids']) == set(ids) and not g['issues']]
            if len(matches) != 1:
                return None
            matched.append(matches[0])
    except (KeyError, TypeError, ValueError):
        return None
    if (any(matched[0][key] != matched[1][key] for key in ('dataset_key', 'code_revision', 'provenance'))
            or {_json(s) for s in matched[0]['seeds']} != {_json(s) for s in matched[1]['seeds']}):
        return None
    checks = acceptance.get('checks')
    if not isinstance(checks, list) or any(not isinstance(c, dict) for c in checks):
        return None
    return {'source': 'saved_validation_review', 'record_id': f'{run_id}:{cycle}',
            'revision_hash': saved['revision_hash'], 'phase': 'validation', 'cycle': cycle,
            'baseline': matched[0]['name'], 'proposed': matched[1]['name'],
            'baseline_group': matched[0]['id'], 'proposed_group': matched[1]['id'],
            'checks': checks, 'criteria_complete': acceptance.get('criteria_complete'),
            'research_outcome': acceptance.get('research_outcome')}


def build(store, run_id, values, *, primary_metric, direction, truncated=False):
    active = [row for row in values if not row.get('superseded')]
    grouped, datasets = defaultdict(list), {}
    for trial in active:
        params, evidence = trial['params'], trial.get('evaluation') or {}
        dataset = _dataset(trial)
        dataset_key = hashlib.sha256(_json(dataset).encode()).hexdigest()[:16]
        datasets.setdefault(dataset_key, dict(dataset, key=dataset_key))
        phase = params.get('phase')
        splits = evidence.get('split_fingerprints') or {}
        known = (trial.get('params_complete') and isinstance(dataset['id'], str) and dataset['id']
                 and phase in ('validation', 'test') and evidence.get('phase') == phase
                 and isinstance(splits, dict) and all(isinstance(splits.get(k), str) and splits[k] for k in ('train', phase))
                 and type(trial.get('research_cycle')) is int and trial.get('code_revision') is not None)
        config = {k: v for k, v in params.items() if k not in ('seed', 'phase', 'comparison_inputs')}
        key = _json([dataset_key, phase, trial.get('research_cycle'), trial.get('code_revision'),
                     trial.get('code_sha256'), trial.get('experiment_plan_sha256'),
                     trial.get('provenance'), evidence.get('split_identity_protocol'), config,
                     None if known else trial['id']])
        grouped[key].append((trial, dataset_key, bool(known)))
    groups = []
    for key, entries in grouped.items():
        rows = [entry[0] for entry in entries]
        row = rows[0]
        completed = [r for r in rows if r['status'] == 'completed']
        seeds = [r['params'].get('seed') for r in completed]
        valid_seeds = all(_seed(s) for s in seeds) and len({_json(s) for s in seeds}) == len(seeds)
        issues = []
        if completed and not valid_seeds:
            issues.append('种子缺失或重复，未合并为重复实验均值。')
        if not entries[0][2]:
            issues.append('数据划分、代码版本或参数不完整，仅展示单次记录。')
        metrics = {}
        for metric in sorted({k for r in completed for k in r.get('metrics', {})}):
            numbers = [r['metrics'].get(metric) for r in completed]
            valid = [_finite(n) for n in numbers]
            mean = None
            if all(valid) and (len(numbers) == 1 or valid_seeds):
                # Scale first to avoid overflow for finite large measurements.
                scale = max(abs(n) for n in numbers)
                mean = math.fsum(n / scale for n in numbers) / len(numbers) * scale if scale else 0.0
            metrics[metric] = {'mean': mean, 'count': sum(valid), 'total': len(completed)}
        method = row['params'].get('algorithm') or row['params'].get('method') or '未记录方法'
        method = method if isinstance(method, str) else '未记录方法'
        groups.append({'id': hashlib.sha256(key.encode()).hexdigest()[:16], 'name': str(method),
            'algorithm': method, 'loss': row['params'].get('loss'), 'variant': row['params'].get('variant'),
            'phase': row['params'].get('phase'), 'cycle': row.get('research_cycle'), 'code_revision': row.get('code_revision'),
            'code_sha256': row.get('code_sha256'), 'plan_sha256': row.get('experiment_plan_sha256'),
            'dataset_key': entries[0][1], 'seeds': seeds, 'metrics': metrics,
            'completed': len(completed), 'total': len(rows), 'issues': issues,
            'trial_ids': [r['id'] for r in rows], 'provenance': row.get('provenance')})
    groups.sort(key=lambda g: ({'baseline': 0, 'proposed': 1}.get(g['variant'], 2), g['name'], g['id']))
    from .performance_policy import current, compact, assess, reference
    policy_status = None
    try:
        policy = current(store, run_id)
    except ValueError as exc:
        # A stale execution authority must not hide saved measurements. Keep
        # current() strict for execution and do not assess under an invalid rule.
        policy = None
        policy_status = {'state': 'unavailable', 'policy_id': reference(store, run_id),
            'message': str(exc), 'assessment_available': False, 'scientific_approval': False}
    assessment = None
    if policy:
        primary_metric, direction = policy['policy']['primary_metric'], policy['policy']['direction']
        assessment = assess(values, store.get('experiment_plans', policy['policy']['plan_id']), policy)
        if truncated:
            assessment['research_outcome'] = 'inconclusive'
            assessment['integrity_issues'].append('Display projection truncated; retrieve full evidence before deciding')
            for check in assessment['checks']:
                check['passed'] = None
    from .experiment_display_comparison import contracts, comparisons
    cycles = {g['cycle'] for g in groups if type(g['cycle']) is int}
    metric_contracts = contracts(store, run_id, cycles, (primary_metric, direction))
    observed = comparisons(active, groups, metric_contracts, truncated)
    reviews = [review for cycle in sorted(cycles)
               if (review := _review(store, run_id, [r for r in active if r.get('research_cycle') == cycle],
                                     [g for g in groups if g['cycle'] == cycle], truncated))]
    return {'display_only': True, 'source': 'saved_experiment_records', 'primary_metric': primary_metric,
            'observed_comparisons': observed, 'reviews': reviews,
            **({'performance_policy': compact(policy), 'performance_assessment': assessment} if policy else {}),
            **({'performance_policy_status': policy_status} if policy_status else {}),
            'direction': direction, 'datasets': list(datasets.values()), 'groups': groups,
            'completed': sum(r['status'] == 'completed' for r in active), 'total': len(active),
            'superseded_count': len(values) - len(active), 'truncated': truncated,
            'review': _review(store, run_id, active, groups, truncated)}
