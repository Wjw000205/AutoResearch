"""Pure candidate selection from frozen validation evidence. The harness owns dispatch."""
from math import sqrt

import numpy as np
from scipy.linalg import solve_triangular
from scipy.special import ndtr

from .experiment_workflow import sha
from .tuning_candidates import changed, targets
from .tuning_scope import configured_value, value_key


def step(evidence, factor, base, seed, value):
    values = factor['values']
    numeric = factor['value_kind'] in {'number', 'integer'}
    return {'base_unit_id': base, 'factor_path': factor['path'], 'variant': factor['variant'],
            'validation_seed': seed, 'scope_sha256': evidence['tuning_scope']['plan_sha256'],
            'grid_values': values, 'lower_bound': min(values) if numeric else None,
            'upper_bound': max(values) if numeric else None, 'pending_values': [value]}


def draft(engine, run_id, evidence, ref):
    """Build the exact reviewable candidate, including the ordinary versioned YAML."""
    from .hyperparameter_yaml import control_version, revise
    state = evidence.get('combination_validation', {})
    if state.get('version') not in (2, 3) or not state.get('next_step'):
        return None
    point = dict(state['next_step'])
    point['value'], = point.pop('pending_values')
    f = next(f for f in evidence['tuning_scope']['plan']['factors']
             if f['path'] == point['factor_path'] and f['variant'] == point['variant'])
    point.update(grid_rationale=f['rationale'][:600], rationale=(
        'Next unmeasured value in the reviewed single-factor grid.' if state['status'] == 'waiting_single_factor'
        else 'Selected by the reviewed dataset Bayesian script.' if state['version'] == 3
        else 'Maximum expected improvement among eligible joint configurations.'))
    if evidence.get('hyperparameter_yaml', {}).get('ready'):
        base = control_version(engine, run_id, evidence, ref, point['base_unit_id'], point['validation_seed'])
        ops = [{'op': 'set', 'path': '/profiles/' + v + point['factor_path'], 'value': point['value']}
               for v in base['body']['document']['profiles'] if point['variant'] in ('shared', v)]
        version = revise(engine, run_id, base, {'action': 'edit', 'operations': ops}, owner=None)
        point.update(yaml_revision_id=version['id'], yaml_sha256=version['body']['yaml_sha256'])
    return point


def grid_schedule(evidence, seed):
    """Every factor uses the exact same reference pair and declared value order."""
    from .combination_validation import _controls
    scope = evidence['tuning_scope']
    base = evidence['dataset_optimization']['architecture_certificate']['body']['assessment']['criteria']['reference_unit_id']
    comp = evidence['comparisons'][base]
    controls = _controls(evidence, seed, comp['task_id'], comp['metric'], comp['direction'])
    if base not in controls:
        raise ValueError('The scripted grid requires a measured reference pair at its reviewed seed')
    measured = {variant: {sha(pair[variant]['params']) for pair in controls.values()}
                for variant in ('baseline', 'proposed')}
    ledger = {j['name']: j['state'] for j in evidence['ledger']['jobs']}
    states = {}
    for name, params in evidence['job_parameters'].items():
        states.setdefault(sha(params), []).append(ledger.get(name))
    coverage, next_step = [], None
    first_pending = True
    for f in sorted(scope['plan']['factors'], key=lambda f: f['priority']):
        pending, completed = [], 0
        for value in f['values'] if f['disposition'] == 'search' else []:
            point = step(evidence, f, base, seed, value)
            hashes, is_control = [], True
            for variant, result in controls[base].items():
                original = result['params']
                if not targets(point, original):
                    continue
                control = configured_value(original, f['path'], evidence)
                is_control &= value_key(value) == value_key(control)
                candidate = changed(original, {**point, 'control_value': control}, value)
                hashes.append((variant, sha(candidate)))
            if all(digest in measured[v] for v, digest in hashes):
                completed += int(not is_control)
            else:
                pending.append(value)
                if first_pending:
                    first_pending = False
                    if all(digest in measured[v] or states.get(digest, ['missing']) == ['missing']
                           for v, digest in hashes):
                        next_step = point
        coverage.append({'path': f['path'], 'variant': f['variant'], 'disposition': f['disposition'],
                         'pending_values': pending, 'completed_new_points': completed,
                         'complete': f['disposition'] != 'requires_implementation' and not pending})
    return coverage, next_step


def posterior(observations, candidates, direction):
    """Fixed GP with a product categorical kernel on complete discrete configurations."""
    x = np.asarray(list(observations))
    y = np.asarray(list(observations.values()), dtype=float)
    y = (y - np.mean(y)) / (np.std(y) or 1.0)
    if direction == 'minimize':
        y = -y
    def kernel(a, b):
        # Accumulate dimensions without allocating an n-by-m-by-d tensor.
        distance = np.zeros((len(a), len(b)))
        for dimension in range(a.shape[1]):
            distance += a[:, dimension, None] != b[:, dimension]
        return np.exp(-0.5 * distance)
    gram = kernel(x, x)
    gram.flat[::len(x) + 1] += 1e-6
    lower = np.linalg.cholesky(gram)
    weights = solve_triangular(lower, y, lower=True)
    result = []
    for start in range(0, len(candidates), 256):
        cross = kernel(x, np.asarray(candidates[start:start + 256]))
        features = solve_triangular(lower, cross, lower=True)
        gain = weights @ features - max(y)
        sigma = np.sqrt(np.maximum(1e-12, 1.0 - np.sum(features * features, axis=0)))
        z = gain / sigma
        ei = gain * ndtr(z) + sigma * np.exp(-0.5 * z*z) / sqrt(2 * np.pi)
        result.extend(zip(gain.tolist(), sigma.tolist(), ei.tolist()))
    return result


def expected_improvement(observations, candidates, direction):
    return [ei for _, _, ei in posterior(observations, candidates, direction)]


def _coordinates(params, factors, anchor, evidence):
    """Drop observations with different non-search parameters or out-of-grid values."""
    reconstructed = anchor
    coordinates = []
    for f in factors:
        control = configured_value(anchor, f['path'], evidence)
        value = configured_value(params, f['path'], evidence)
        keys = [value_key(v) for v in f['values']]
        if value_key(value) not in keys:
            return None
        coordinates.append(keys.index(value_key(value)))
        reconstructed = changed(reconstructed, {'factor_path': f['path'], 'variant': f['variant'],
                                'control_value': control}, value)
    return tuple(coordinates) if sha(reconstructed) == sha(params) else None


def bayesian_progress(plan, evidence, plan_sha, controls, *, choose=None):
    from .combination_validation import _best
    seed, direction = plan['validation_seed'], plan['direction']
    job_states = {j['name']: j['state'] for j in evidence['ledger']['jobs']}
    states = {}
    for name, params in evidence['job_parameters'].items():
        states.setdefault(sha(params), []).append(job_states.get(name))
    measured = {sha(r['params']) for pair in controls.values() for r in pair.values()}
    batches = [u for u in evidence['units'] if u.get('bayesian_plan_sha256') == plan_sha]
    used = sum(u['admitted_trials'] for u in batches)
    remaining = plan['max_new_trials'] - used
    completed = [u for u in batches if u['id'] in controls]
    state = {'steps': [], 'completed_steps': sum(u['admitted_trials'] for u in completed),
             'total_steps': plan['max_new_trials'], 'completed_batches': len(completed),
             'new_trials_admitted': used, 'next_step': None}
    if remaining < 0:
        raise ValueError('Bayesian admissions exceed the reviewed budget')
    if len(completed) != len(batches):
        return {**state, 'status': 'waiting_measured_batch'}
    budget = evidence.get('budget', {})
    unreserved = remaining
    if budget.get('trial_limit') is not None:
        reserved = sum(j['state'] == 'missing' for j in evidence['ledger']['jobs'])
        unreserved = budget['trial_limit'] - budget['attempts_used'] - reserved
    needs_budget = False
    unresolved = False
    anchors, best_candidate, best_ei = {}, None, -1.0
    options, proposals = [], []
    for variant in plan['initial_controls']:
        factors = [f for f in plan['factors'] if f['variant'] in ('shared', variant)]
        initial = plan['initial_controls'][variant]
        anchor = controls[initial][variant]['params']
        eligible, observed, coordinates = {}, {}, {}
        for name in sorted(controls):
            result = controls[name][variant]
            coords = _coordinates(result['params'], factors, anchor, evidence)
            if coords is not None:
                eligible[name] = controls[name]
                observed[coords] = result['score']
                coordinates[name] = coords
        anchors[variant] = _best(eligible, variant, direction, initial)
        if not remaining:
            continue
        candidates = {}
        for name, coords in coordinates.items():
            pair = controls[name]
            for axis, f in enumerate(factors):
                for index, value in enumerate(f['values']):
                    target = (*coords[:axis], index, *coords[axis+1:])
                    if target in observed or target in candidates:
                        continue
                    point = step(evidence, f, name, seed, value)
                    control = f['values'][coords[axis]]
                    affected = [r['params'] for r in pair.values() if targets(point, r['params'])]
                    if any(value_key(configured_value(p, f['path'], evidence)) != value_key(control) for p in affected):
                        continue
                    hashes = [sha(changed(p, {**point, 'control_value': control}, value)) for p in affected]
                    needed = [h for h in hashes if h not in measured]
                    if not needed or len(needed) > remaining:
                        continue
                    if any(states.get(h, ['missing']) != ['missing'] for h in needed):
                        unresolved = True
                    elif sum(h not in states for h in needed) > unreserved:
                        needs_budget = True
                    else:
                        candidates[target] = (point, len(needed), axis)
        if candidates:
            points = sorted(candidates)
            stats = posterior(observed, points, direction)
            if plan['version'] == 3:
                for target, (gain, sigma, ei) in zip(points, stats):
                    point, cost, axis = candidates[target]
                    options.append(dict(index=float(len(options)), ei=ei, gain=gain, sigma=sigma,
                                        cost=float(cost), baseline=float(variant == 'baseline'), axis=float(axis)))
                    proposals.append(point)
                continue
            scores = np.round([ei for _, _, ei in stats], 12)
            winner = int(np.argmax(scores))
            if scores[winner] > best_ei:
                best_candidate, best_ei = candidates[points[winner]][0], scores[winner]
    state['selected_controls'] = anchors
    if options:
        from .bayesian_policy import select
        expected = select(plan['selection_script'], options, used, plan['max_new_trials'])
        if choose is not None and choose(options, used, plan['max_new_trials'], expected) != expected:
            raise ValueError('Launched script differs from the reviewed Bayesian selection')
        if expected >= 0:
            best_candidate = proposals[expected]
        else:
            needs_budget = unresolved = False
            state['script_stopped'] = True
    if best_candidate:
        return {**state, 'status': 'validating_combinations', 'next_step': best_candidate}
    if needs_budget:
        return {**state, 'status': 'waiting_trial_budget'}
    if unresolved:
        return {**state, 'status': 'waiting_measured_batch'}
    final = next((name for name, pair in sorted(controls.items()) if all(
        pair[v]['params'] == controls[anchors[v]][v]['params'] for v in anchors)), None)
    return {**state, 'status': 'complete', 'stop_reason': 'script_stop' if state.get('script_stopped') else 'budget_spent' if not remaining else 'no_affordable_frontier',
            'final_pair': {**{v+'_unit_id':unit for v,unit in anchors.items()},
                           'existing_unit_id': final}}
