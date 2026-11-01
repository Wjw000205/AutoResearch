"""Typed, scope-authorized single-factor candidates, including method-local knobs."""
import copy
import math

from .experiment_workflow import sha
from .tuning_scope import factor, get, set_value, value_key, configured_value


def targets(spec, params):
    return spec.get('variant', 'shared') in ('shared', params['variant'])


def changed(params, spec, value):
    result = copy.deepcopy(params)
    if targets(spec, params):
        if params['variant'] in spec.get('missing_control_variants', []) and value_key(value) == value_key(spec['control_value']):
            parts = spec['factor_path'].strip('/').split('/')
            parent = result
            for part in parts[:-1]:
                parent = parent[part]
            parent.pop(parts[-1], None)
            return result
        try:
            original = get(params, spec['factor_path'])
        except KeyError:
            original = spec['control_value']
            if value_key(original) == value_key(value):
                return result  # A missing source-declared default is an exact legacy control.
        # Preserve exact old control bytes, including 0 versus 0.0.
        set_value(result, spec['factor_path'], original if value_key(original) == value_key(value) else value)
    return result


def propose(spec, evidence):
    from .parameter_search import SCHEMA
    from .parameter_grid import check_budget
    from .fair_comparison import assert_same_task
    from .prose_schema import validate
    validate(spec, SCHEMA)
    f = factor(spec, evidence)
    leaf, value = spec['factor_path'].split('/')[-1], spec['value']
    if leaf in {'batch_size', 'max_epochs', 'max_steps', 'patience', 'depth', 'width', 'embedding_dim'} and (type(value) is not int or value < 1):
        raise ValueError('A discrete capacity/training count must be a positive integer')
    if leaf in {'lr', 'learning_rate', 'grad_clip', 'clip_grad_norm', 'max_grad_norm', 'optimizer_eps'} and (type(value) not in (int, float) or value <= 0):
        raise ValueError('Learning rate, clipping and optimizer epsilon must be positive')
    if leaf in {'weight_decay', 'min_delta'} and (type(value) not in (int, float) or value < 0):
        raise ValueError('Regularization/stopping scalar must be nonnegative')
    if leaf == 'betas' and (not isinstance(value, list) or len(value) != 2 or any(type(v) not in (int, float) or not 0 <= v < 1 for v in value)):
        raise ValueError('Adam betas require two values in [0,1)')
    base = next((u for u in evidence['units'] if u['id'] == spec['base_unit_id']), None)
    comparison = evidence['comparisons'].get(spec['base_unit_id'], {})
    if not base or not base['complete'] or base['kind'] not in {'pilot', 'grid'} or not comparison.get('comparable'):
        raise ValueError('Broad tuning requires a completed paired control')
    originals = [(n, evidence['job_parameters'][n]) for n in base['jobs']
                 if evidence['job_parameters'][n]['seed'] == spec['validation_seed']]
    if sorted(p['variant'] for _, p in originals) != ['baseline', 'proposed']:
        raise ValueError('One fixed control seed and both methods are required')
    controls = [configured_value(p, spec['factor_path'], evidence) for _, p in originals if targets(spec, p)]
    if not controls or len({value_key(v) for v in controls}) != 1:
        raise ValueError('Shared factor controls must have the same semantic value; use a method-local factor otherwise')
    control = controls[0]
    keys = {value_key(v) for v in spec['grid_values']}
    if value_key(control) not in keys or value_key(spec['value']) == value_key(control):
        raise ValueError('Grid must retain this control and select a distinct candidate')
    if f['value_kind'] in {'number', 'integer'}:
        lo, hi = spec['lower_bound'], spec['upper_bound']
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in (lo, hi)) or lo >= hi:
            raise ValueError('Numeric factors require finite ordered bounds')
        if any(not lo <= v <= hi for v in spec['grid_values']):
            raise ValueError('Every candidate/control must fit the reviewed bounds')
    elif spec['lower_bound'] is not None or spec['upper_bound'] is not None:
        raise ValueError('Categorical/vector/boolean factors use explicit values and null numeric bounds')
    for grid in evidence.get('parameter_grids', []):
        if grid['task_id'] == base['task_id'] and grid['validation_seed'] != spec['validation_seed']:
            raise ValueError('Keep the same fixed model seed across current-dataset tuning')
    params = [changed(p, {**spec, 'control_value': control}, spec['value']) for _, p in originals]
    for (_, old), new in zip(originals, params):
        assert_same_task(old, new)
    identity = 'search-' + sha({'binding': evidence['binding'], 'jobs': params})[:24]
    by_params = {sha(p): n for n, p in evidence['job_parameters'].items()}
    jobs, fresh = [], []
    for index, param in enumerate(params):
        name = by_params.get(sha(param))
        if name is None:
            name = identity + '-' + str(index)
            fresh.append(name)
        jobs.append((name, param))
    if not fresh:
        raise ValueError('This exact configuration is already recorded; reuse it without a new execution')
    grid = {k: copy.deepcopy(spec[k]) for k in ('base_unit_id', 'factor_path', 'validation_seed',
             'grid_values', 'lower_bound', 'upper_bound', 'grid_rationale', 'scope_sha256')}
    grid.update(variant=spec.get('variant', 'shared'), value_kind=f['value_kind'], binding=copy.deepcopy(evidence['binding']),
                control_value=control, task_id=base['task_id'], metric=comparison['metric'], direction=comparison['direction'],
                selection_rule='best_same_seed_proposed_validation_control_first_on_tie')
    grid['source_default'] = next((copy.deepcopy(c) for c in evidence.get('parameter_capabilities', [])
                                  if c['path'] == spec['factor_path'] and c['variant'] in ('shared', spec.get('variant'))), None)
    grid['missing_control_variants'] = []
    for _, params0 in originals:
        if targets(spec, params0):
            try:
                get(params0, spec['factor_path'])
            except KeyError:
                grid['missing_control_variants'].append(params0['variant'])
    grid['grid_values'] = [control if value_key(v) == value_key(control) else v for v in grid['grid_values']]
    grid['id'] = 'parameter-grid-' + sha(grid)[:24]
    check_budget(evidence, grid)
    return {'id': identity, 'kind': 'grid', 'task_id': base['task_id'], 'jobs': jobs, 'new_job_names': fresh,
            'factor_path': spec['factor_path'], 'base_unit_id': base['id'], 'parameter_grid': grid}
