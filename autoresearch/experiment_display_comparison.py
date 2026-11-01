"""Descriptive saved validation pairs; never a review, selection or admission."""
import math

from .fair_comparison import assert_same_task
from .research_cycle import _data_conflict, _finite, _mean, _seed_key


def contracts(store, run_id, cycles, fallback):
    """Recover each cycle's metric without loading archived programs or reports."""
    from .run_views import _rows, _field
    found = {}
    for collection, prefix, where in (
        ('dataset_experiment_setups', 'design', "json_extract(data,'$.status')='ready'"),
        ('cycle_steps', 'output', "json_extract(data,'$.stage')='design'"),
        ('steps', 'output', "json_extract(data,'$.stage')='design'"),
        ('research_cycles', 'design', ''),
    ):
        fields = {'cycle': _field('cycle'), 'metric': _field(prefix + '.primary_metric'),
                  'direction': _field(prefix + '.direction')}
        for row in _rows(store, collection, fields, run_id=run_id, where=where):
            metric, direction = row['metric'], row['direction']
            if (row['cycle'] in cycles and isinstance(metric, str) and metric.startswith('val_')
                    and direction in ('minimize', 'maximize')):
                found.setdefault(row['cycle'], set()).add((metric, direction))
    # Caller fallback is useful for legacy single-cycle records only. Never use
    # the currently active dataset's design to label another cycle's results.
    if len(cycles) == 1 and not found and fallback[0] and fallback[1]:
        found[next(iter(cycles))] = {fallback}
    return {cycle: next(iter(values)) for cycle, values in found.items() if len(values) == 1}


def comparisons(values, groups, metric_contracts, truncated):
    if truncated:
        return []
    by_id = {r['id']: r for r in values}
    output = []
    for baseline in (g for g in groups if g['variant'] == 'baseline'):
        contract = metric_contracts.get(baseline['cycle'])
        if not contract:
            continue
        metric, direction = contract
        for proposed in (g for g in groups if g['variant'] == 'proposed'):
            pair = (baseline, proposed)
            if (any(g['issues'] or g['phase'] != 'validation' or g['completed'] != g['total'] for g in pair)
                    or any(baseline[k] != proposed[k] for k in
                           ('dataset_key', 'cycle', 'code_revision', 'code_sha256', 'plan_sha256', 'provenance'))):
                continue
            rows = [[by_id[tid] for tid in g['trial_ids']] for g in pair]
            if any(r.get('evaluation_errors') or r.get('evaluation_errors_complete', True) == False
                   or not r.get('params_complete') or not _finite(r.get('metrics', {}).get(metric))
                   for members in rows for r in members):
                continue
            keyed = [{_seed_key(r['params'].get('seed')): r for r in members} for members in rows]
            if (not keyed[0] or None in keyed[0] or None in keyed[1]
                    or keyed[0].keys() != keyed[1].keys()
                    or any(len(k) != len(r) for k, r in zip(keyed, rows))):
                continue
            pairs = []
            try:
                for key, first in keyed[0].items():
                    second = keyed[1][key]
                    assert_same_task(first['params'], second['params'])
                    if _data_conflict(first, second):
                        raise ValueError('Conflicting split declarations')
                    b, p = first['metrics'][metric], second['metrics'][metric]
                    delta = b - p if direction == 'minimize' else p - b
                    if not _finite(delta):
                        raise ValueError('Nonfinite difference')
                    pairs.append({'seed': first['params']['seed'], 'baseline': b, 'proposed': p,
                                  'improvement': delta, 'baseline_trial_id': first['id'],
                                  'proposed_trial_id': second['id']})
            except ValueError:
                continue
            bmean, pmean = (_mean([p[k] for p in pairs]) for k in ('baseline', 'proposed'))
            relative = _mean([p['improvement'] for p in pairs]) / abs(bmean) if bmean else None
            if relative is None or not math.isfinite(relative):
                continue
            output.append({'source': 'saved_validation_measurements', 'display_only': True,
                'scientific_approval': False, 'metric': metric, 'direction': direction,
                'cycle': baseline['cycle'], 'baseline_group': baseline['id'], 'proposed_group': proposed['id'],
                'baseline_mean': bmean, 'proposed_mean': pmean, 'relative_improvement': relative,
                'aggregation': 'paired_seed_mean_then_relative_improvement', 'n_pairs': len(pairs),
                'pairs': pairs})
    return output
