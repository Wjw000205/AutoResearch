"""Explicit shared parameter searches and predeclared outcome checks.

The search is fixed before any measurements. Each grid entry is applied to both
methods; model-specific fixed parameters remain separate. A program must consume
these exact values and must not perform an unrecorded inner parameter search.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math

from .evaluation import evaluate_trials


PLANNER_VALIDATION_VERSION = 'named-ablation-v1'


def build_for_engine(engine, run_id, design, max_trials):
    from .experiment_protocols import resolve_design
    return build_plan(design, max_trials, protocols=resolve_design(engine, run_id, design))


def _signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _seed_values(source, field, default):
    values = source.get(field, default)
    if (not isinstance(values, list) or not 1 <= len(values) <= 10
            or any(type(seed) is not int or not 0 <= seed < 2**32 for seed in values)
            or len(set(values)) != len(values)):
        raise ValueError(f'{field} must contain one to ten unique integer model seeds in [0, 2**32)')
    return list(values)


def validation_seeds(plan):
    return _seed_values(plan, 'validation_seeds', [plan['selection_seed']])


def final_test_seeds(plan):
    seeds = _seed_values(plan, 'final_test_seeds', [101 * (i + 1) for i in range(plan['final_pairs'])])
    if len(seeds) != plan['final_pairs']:
        raise ValueError('final_test_seeds does not match the frozen final_pairs count')
    return seeds


def _named_ablation(design, point):
    """Compile an explicit legacy selector into one concrete model invocation.

    ``variant`` remains the runner's baseline/proposed/ablation phase grouping.
    A named configuration is only accepted when its complete object already
    exists in the same frozen design; this never invents or resolves references.
    """
    fixed = design['ablation_params']
    configurations = fixed.get('configurations')
    name = point.get('variant')
    if (set(point) != {'variant'} or not isinstance(name, str) or not name.strip()
            or not isinstance(configurations, dict) or name not in configurations):
        raise ValueError('ablation_search_space variant is reserved; a named selector must contain only variant and match an existing ablation_params.configurations object')
    selected = configurations[name]
    if (not isinstance(selected, dict) or not selected
            or not isinstance(selected.get('algorithm'), str) or not selected['algorithm'].strip()):
        raise ValueError('Named ablation configuration must be a nonempty concrete object with an explicit algorithm')
    inherited = {k: v for k, v in fixed.items() if k != 'configurations'}
    for source in (inherited, selected):
        if any(k in source for k in ('variant', 'phase', 'seed', 'ablation_configuration', 'configurations')):
            raise ValueError('Named ablation configuration conflicts with reserved runner or selector fields')

    def check_references(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str) or key in {'ref', '$ref'} or key.endswith('_ref'):
                    raise ValueError('Named ablation configuration contains an unresolved reference; freeze its exact concrete values before planning')
                check_references(item)
        elif isinstance(value, list):
            for item in value:
                check_references(item)

    def merge(parent, child):
        result = copy.deepcopy(parent)
        for key, value in child.items():
            result[key] = (merge(result[key], value) if isinstance(result.get(key), dict) and isinstance(value, dict)
                           else copy.deepcopy(value))
        return result

    check_references(inherited)
    check_references(selected)
    candidate = merge(inherited, selected)
    candidate.update(ablation_configuration=name, variant='ablation')
    try:
        json.dumps(candidate, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('Named ablation configuration must contain finite JSON hyperparameters') from exc
    return candidate


def build_plan(design, max_trials, *, protocols=None):
    if 'benchmark_tasks' in design:
        from .benchmark_matrix import build
        return build(design, max_trials, protocols)
    seeded = 'validation_seeds' in design or 'final_test_seeds' in design
    seeds = _seed_values(design, 'validation_seeds', [17])
    finals = _seed_values(design, 'final_test_seeds', [101]) if 'final_test_seeds' in design else None
    grid = design.get('search_space') or [{}]
    points, seen = [], set()
    for point in grid:
        if not isinstance(point, dict) or any(k in point for k in ('phase', 'variant', 'seed', 'description')):
            raise ValueError('search_space entries must contain only concrete shared hyperparameters')
        encoded = json.dumps(point, sort_keys=True, allow_nan=False)
        if encoded not in seen:
            seen.add(encoded)
            points.append(copy.deepcopy(point))
    ablation_grid = design.get('ablation_search_space', [points[0]])
    if not isinstance(ablation_grid, list) or not ablation_grid:
        raise ValueError('ablation_search_space must be a nonempty list of concrete hyperparameter objects')
    named_ablations = {}
    for index, point in enumerate(ablation_grid):
        if isinstance(point, dict) and 'variant' in point:
            named_ablations[index] = _named_ablation(design, point)
        elif not isinstance(point, dict) or any(not isinstance(k, str) or k in ('phase', 'seed', 'description') for k in point):
            raise ValueError('ablation_search_space entries must contain only concrete hyperparameters, without reserved fields')
        try:
            json.dumps(point, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError('ablation_search_space entries must contain finite JSON hyperparameters') from exc
    def params(variant, point):
        fixed = {k: v for k, v in design[f'{variant}_params'].items()
                 if k not in {'description', 'phase', 'seed', 'variant'}}
        return copy.deepcopy({**fixed, **point, 'variant': variant})
    pairs = [{'baseline': params('baseline', point), 'proposed': params('proposed', point)} for point in points]
    signatures = [json.dumps(pair, sort_keys=True, allow_nan=False) for pair in pairs]
    if len(set(signatures)) != len(signatures):
        raise ValueError('Grid entries collapse to identical complete parameter pairs after applying fixed parameters; revise the grid before running duplicate seed trials')
    if named_ablations and len(named_ablations) != len(ablation_grid):
        raise ValueError('Named ablation configurations require one explicit selector for every ablation grid entry')
    ablations = [named_ablations[index] if index in named_ablations else params('ablation', point)
                 for index, point in enumerate(ablation_grid)]
    signatures = [json.dumps({key: value for key, value in candidate.items()
                              if not named_ablations or key != 'ablation_configuration'},
                             sort_keys=True, allow_nan=False) for candidate in ablations]
    if len(set(signatures)) != len(signatures):
        raise ValueError('Ablation grid entries collapse to identical complete parameters; revise the grid before running duplicate seed trials')
    if seeded:
        pairs = [{**copy.deepcopy(pair), 'seed': seed} for pair in pairs for seed in seeds]
        ablations = [{**copy.deepcopy(params), 'seed': seed} for params in ablations for seed in seeds]
    minimum = 2 * len(pairs) + len(ablations) + 2 * (len(finals) if finals is not None else 1)
    if max_trials is None and finals is None:
        raise ValueError('An uncapped plan requires explicit final_test_seeds; experiment count is not a seed-selection rule')
    if max_trials is not None and max_trials < minimum:
        raise ValueError(f'Approved grid needs at least {minimum} trials ({len(pairs)} validation calls per method, {len(ablations)} ablation calls and final pairs); max_trials={max_trials}. Increase the limit or obtain an explicitly revised design; no grid points or seeds were discarded.')
    final_pairs = len(finals) if finals is not None else min(10, (max_trials - 2 * len(pairs) - len(ablations)) // 2)
    plan = {'version': 2 if seeded else 1, 'grid': points, 'validation_pairs': pairs,
            'ablation_candidates': ablations, 'ablation_params': copy.deepcopy(ablations[0]),
            'final_pairs': final_pairs,
            'selection_seed': seeds[0], 'validation_trials_per_method': len(pairs),
            'protocol': 'Apply every shared grid point to both methods and execute every ablation_candidates entry; one invocation uses the exact recorded params. No inner grid search, implicit parameter selection or test-based tuning. Freeze the best validation configuration separately for each method before paired final tests.'}
    if named_ablations:
        plan['ablation_configuration_protocol'] = 1
        plan['planner_validation_version'] = PLANNER_VALIDATION_VERSION
        plan['ablation_configuration_bindings'] = [{
            'grid_index': index, 'selector': ablation_grid[index]['variant'],
            'source_pointer': '/ablation_params/configurations/' + ablation_grid[index]['variant'].replace('~', '~0').replace('/', '~1'),
            'source_configuration_sha256': _signature(design['ablation_params']['configurations'][ablation_grid[index]['variant']]),
            'invocation_params_sha256': _signature(candidate),
            'signature_scope': 'Concrete invocation before host phase and model seed; each declared validation seed is applied separately.'}
            for index, candidate in named_ablations.items()]
        plan['protocol'] += (' Each named ablation selector refers only to its matching object in the frozen design. '
            'Inherited common fields and contracts are recursively merged with that one configuration, with explicit configuration fields taking precedence; '
            'arrays and scalars replace whole values and null is not deletion. The invocation has variant=ablation and '
            'ablation_configuration records the original selector. Run exactly its algorithm once; the other configurations are not execution parameters. '
            'No unresolved references are expanded or guessed by the planner.')
    if seeded:
        plan.update(validation_seeds=seeds, final_test_seeds=finals if finals is not None else [101 * (i + 1) for i in range(final_pairs)],
            planned_trials=2 * len(pairs) + len(ablations) + 2 * final_pairs,
            protocol=plan['protocol'] + ' Replicate every validation and ablation configuration across every declared validation model seed. '
                'Select configurations by all declared validation seeds, never the most favorable seed. Final tests use only the frozen final_test_seeds list.')
    from .fair_comparison import validate_plan
    validate_plan(plan, design=design)
    return plan


def check_outcome(trials, criteria, *, phase='test', complete=True, expected_seeds=None, use_reported=True):
    """Check declared criteria without tuning on test scores.

    Only host-validated comparison markers may narrow a criterion's phase scope.
    Unmarked trials retain the full frozen inventory for both phases.
    """
    from .comparison_transport import reported_outcome
    transported = reported_outcome(trials, criteria, phase=phase, complete=complete, expected_seeds=expected_seeds) if use_reported else None
    if transported is not None:
        return transported
    checks = []
    if expected_seeds is None:
        expected_seeds = [t.get('params', {}).get('seed') for t in trials if not t.get('superseded')
                          and t.get('params', {}).get('phase') == phase
                          and t.get('params', {}).get('variant') in {'baseline', 'proposed'}]
    seed_key = lambda seed: json.dumps([type(seed).__name__, seed])
    expected = {seed_key(seed) for seed in expected_seeds}
    for criterion in criteria:
        metric = criterion['metric']
        metric = metric.replace('val_', 'test_', 1) if phase == 'test' else metric
        # evaluate_trials only analyzes records designated test. For validation
        # this local copy changes phase/names, without touching stored records.
        records = trials
        if phase == 'validation':
            records = [{**t, 'params': {**t.get('params', {}), 'phase': 'test'},
                        'metrics': {k.replace('val_', 'test_', 1): v for k, v in t.get('metrics', {}).items()}}
                       for t in trials if t.get('params', {}).get('phase') == 'validation']
        report = evaluate_trials(records, metric, criterion['direction'])
        comparisons = report['paired_comparisons']
        comparable = len(comparisons) == 1 and comparisons[0].get('n_pairs', 0) > 0
        comparison = comparisons[0] if comparable else {}
        coverage = bool(expected) and {seed_key(seed) for seed in comparison.get('seeds', [])} == expected
        baseline = comparison.get('baseline_mean')
        relative = comparison.get('improvement', 0) / abs(baseline) if baseline else None
        if relative is not None and not math.isfinite(relative):
            relative = None
        threshold = criterion['minimum_relative_improvement']
        passed = relative is not None and (relative > threshold if criterion.get('comparison') == 'greater_than' else relative >= threshold)
        checks.append({**criterion, 'metric': metric, 'relative_improvement': relative,
                       'passed': passed if relative is not None and coverage else None,
                       'complete_seed_coverage': coverage, 'expected_pairs': len(expected),
                       'baseline_mean': baseline, 'proposed_mean': comparison.get('proposed_mean'),
                       'n_pairs': comparison.get('n_pairs', 0),
                       'comparison_id': comparison.get('comparison_id'), 'issues': report['issues']})
    from .performance_policy import outcome
    status = outcome(checks, complete)
    return {'research_outcome': status, 'phase': phase, 'checks': checks, 'criteria_complete': complete,
            'scope': 'Descriptive checks of predeclared numeric criteria on recorded paired measurements; positive does not establish statistical significance, novelty, or publication readiness.',
            'test_reuse_for_tuning_allowed': False}
