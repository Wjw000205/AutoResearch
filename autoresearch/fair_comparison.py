"""Validate frozen task conditions independently of model hyperparameters.

Only explicit JSON pointers and a small set of known task-parameter locations
are inspected. This does not infer model behaviour or prove reported execution.
Validation is pure: it never normalizes or rewrites a paid design or invocation.
"""
from __future__ import annotations

import json
import math
import re


_CONTRACT = 'comparison_task'
_ALIASES = {
    'input_length': ('input_length', 'input_len', 'seq_len', 'lookback', 'lookback_length', 'context_length'),
    'output_length': ('output_length', 'output_len', 'pred_len', 'prediction_length', 'horizon', 'forecast_horizon'),
    'channels': ('channels', 'n_channels', 'num_channels'),
}
_CONTAINERS = ((), ('common',), ('data',), ('common', 'data'))
_SHORT = {'L': 'input_length', 'H': 'output_length', 'C': 'channels'}
_STRUCTURAL = {*_ALIASES, 'input_channels', 'output_channels', 'target_channels',
               *[name for names in _ALIASES.values() for name in names]}
_RESERVED = {_CONTRACT, 'comparison_inputs', 'description'}
_RUNNER_FIELDS = {'variant', 'phase', 'seed'}


def _finite_json(value, active=None):
    if value is None or type(value) in (str, bool, int):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) not in (dict, list):
        return False
    active = set() if active is None else active
    if id(value) in active:
        return False
    active.add(id(value))
    try:
        if type(value) is dict:
            return all(type(key) is str and _finite_json(item, active) for key, item in value.items())
        return all(_finite_json(item, active) for item in value)
    finally:
        active.remove(id(value))


def strict_equal(left, right):
    """Compare finite JSON values without equating booleans, integers or floats."""
    def equal(a, b):
        if type(a) is not type(b):
            return False
        if type(a) is dict:
            return a.keys() == b.keys() and all(equal(a[key], b[key]) for key in a)
        if type(a) is list:
            return len(a) == len(b) and all(equal(a, b) for a, b in zip(a, b))
        return a == b
    try:
        return _finite_json(left) and _finite_json(right) and equal(left, right)
    except RecursionError:
        return False


def _show(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)[:300]
    except (ValueError, TypeError, RecursionError):
        return repr(value)[:300]


def _error(message):
    raise ValueError('Fair comparison task: ' + message)


def _pointer(parts):
    return '/' + '/'.join(part.replace('~', '~0').replace('/', '~1') for part in parts)


def _resolve(params, pointer):
    if (not isinstance(pointer, str) or not pointer.startswith('/')
            or re.search(r'~(?![01])', pointer)):
        _error(f'invalid exact JSON pointer {_show(pointer)}')
    parts = [part.replace('~1', '/').replace('~0', '~') for part in pointer[1:].split('/')]
    if parts[0] in _RUNNER_FIELDS or any(part in _RESERVED for part in parts):
        _error(f'{pointer} cannot reference task metadata, runner fields or dynamic comparison inputs')
    value = params
    for part in parts:
        if type(value) is dict and part in value:
            value = value[part]
        elif type(value) is list and re.fullmatch(r'0|[1-9][0-9]*', part) and int(part) < len(value):
            value = value[int(part)]
        else:
            _error(f'{pointer} is missing from the concrete invocation')
    return value


def _dimension(name, value, pointer):
    if name in _STRUCTURAL and (type(value) is not int or value <= 0):
        _error(f'{pointer} ({name}) expected a positive integer, actual {_show(value)}')


def _task(params):
    if type(params) is not dict:
        _error('concrete parameters must be a JSON object')
    try:
        valid = _finite_json(params)
    except RecursionError:
        valid = False
    if not valid:
        _error('concrete parameters must contain only finite JSON values')
    signature, locations = {}, {}

    def add(name, value, pointer):
        _dimension(name, value, pointer)
        if name in signature and not strict_equal(signature[name], value):
            _error(f'{pointer} ({name}) expected {_show(signature[name])} from {locations[name]}, actual {_show(value)}')
        signature[name] = value
        locations.setdefault(name, pointer)

    if 'dataset_assets' in params:
        from .datasets import bindings
        add('dataset_assets', bindings(params), '/dataset_assets')
    for name in ('benchmark_task_id', 'benchmark_source'):
        if name in params:
            add(name, params[name], '/' + name)

    contract = params.get(_CONTRACT)
    explicit_names = ({row['name'] for row in contract['dimensions']
                       if type(row) is dict and type(row.get('name')) is str}
                      if type(contract) is dict and type(contract.get('dimensions')) is list else set())
    short_values, channel_values = [], []
    for parts in _CONTAINERS:
        container = params
        for part in parts:
            container = container.get(part) if type(container) is dict else None
        if type(container) is not dict:
            continue
        for name, aliases in _ALIASES.items():
            if _CONTRACT in params and name not in explicit_names:
                continue
            for alias in aliases:
                if alias in container:
                    item = (name, container[alias], _pointer((*parts, alias)))
                    if name == 'channels':
                        channel_values.append(item)
                    else:
                        add(*item)
        if parts in ((), ('common',)):
            for alias, name in _SHORT.items():
                if _CONTRACT in params and name not in explicit_names:
                    continue
                if alias in container:
                    short_values.append((alias, name, container[alias], _pointer((*parts, alias))))
    # A bare C is often an SVM regularizer; L/H may also be model choices.
    # Interpret short names only after the task has temporal-length evidence.
    temporal = (bool({'input_length', 'output_length'} & (signature.keys() | explicit_names))
                or {'L', 'H'} <= {alias for alias, _, _, _ in short_values})
    explicit_channels = 'channels' in explicit_names
    # Outside a temporal task, channels may mean a CNN hidden width. Only an
    # explicit external-channel declaration gives this otherwise generic name
    # task semantics; model hyperparameters remain independently selectable.
    if temporal or explicit_channels:
        for item in channel_values:
            add(*item)
    if temporal:
        for _, name, value, pointer in short_values:
            add(name, value, pointer)

    explicit = _CONTRACT in params
    if explicit:
        contract = params[_CONTRACT]
        if (type(contract) is not dict or set(contract) != {'version', 'dimensions'}
                or type(contract['version']) is not int or contract['version'] != 1
                or type(contract['dimensions']) is not list or not 1 <= len(contract['dimensions']) <= 64):
            _error('/comparison_task must be {version:1, dimensions:[{name,value,paths}]}')
        seen = set()
        for index, row in enumerate(contract['dimensions']):
            location = f'/comparison_task/dimensions/{index}'
            if (type(row) is not dict or set(row) != {'name', 'value', 'paths'}
                    or not isinstance(row['name'], str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,79}', row['name'])
                    or type(row['paths']) is not list or not 1 <= len(row['paths']) <= 32):
                _error(f'{location} must contain a dimension name, finite value and exact paths')
            name, value = row['name'], row['value']
            if name in seen:
                _error(f'{location}/name repeats dimension {name}')
            seen.add(name)
            if any(not isinstance(path, str) for path in row['paths']) or len(set(row['paths'])) != len(row['paths']):
                _error(f'{location}/paths must contain distinct exact JSON pointers')
            add(name, value, location + '/value')
            for path in row['paths']:
                actual = _resolve(params, path)
                if not strict_equal(actual, value):
                    _error(f'{path} ({name}) expected {_show(value)}, actual {_show(actual)}')
    return signature, locations, explicit


def task_signature(params):
    """Return canonical known/explicit task dimensions, never model internals."""
    signature, _, _ = _task(params)
    # Return detached JSON data: callers cannot mutate the input through values.
    return json.loads(json.dumps(signature, ensure_ascii=False, allow_nan=False))


def assert_same_task(left, right):
    """Reject missing, contradictory or different task conditions between calls."""
    expected, left_paths, explicit_left = _task(left)
    actual, right_paths, explicit_right = _task(right)
    if explicit_left != explicit_right:
        _error('/comparison_task must be present for every compared configuration or absent for all legacy configurations')
    for name in sorted(expected.keys() | actual.keys()):
        if name not in expected:
            _error(f'{right_paths[name]} ({name}) is declared only by the compared configuration; expected the same dimension in the reference')
        if name not in actual:
            _error(f'{name} expected {_show(expected[name])} from {left_paths[name]}, actual missing in compared configuration')
        if not strict_equal(expected[name], actual[name]):
            _error(f'{right_paths[name]} ({name}) expected {_show(expected[name])} from {left_paths[name]}, actual {_show(actual[name])}')


def _candidates(plan):
    if type(plan) is not dict or type(plan.get('validation_pairs')) is not list or not plan['validation_pairs']:
        _error('plan requires nonempty validation_pairs')
    for index, pair in enumerate(plan['validation_pairs']):
        if type(pair) is not dict:
            _error(f'validation_pairs[{index}] must be an object')
        for role in ('baseline', 'proposed'):
            if type(pair.get(role)) is not dict:
                _error(f'validation_pairs[{index}].{role} must be concrete parameters')
            yield f'validation_pairs[{index}].{role}', role, pair[role]
    ablations = plan.get('ablation_candidates')
    if ablations is None and type(plan.get('ablation_params')) is dict:
        ablations = [plan['ablation_params']]
    if type(ablations) is not list or not ablations:
        _error('plan requires nonempty ablation_candidates (or legacy ablation_params)')
    for index, params in enumerate(ablations):
        if type(params) is not dict:
            _error(f'ablation_candidates[{index}] must be concrete parameters')
        yield f'ablation_candidates[{index}]', 'ablation', params


def validate_plan(plan, design=None):
    """Check every expanded configuration; one plan has one comparison task."""
    from . import benchmark_matrix
    if benchmark_matrix.enabled(plan):
        return benchmark_matrix.validate(plan, design)
    candidates = list(_candidates(plan))
    reference_label, _, reference = candidates[0]
    for label, role, params in candidates:
        try:
            current, _, has_contract = _task(params)
            if design is not None:
                fixed = design.get(role + '_params')
                if type(fixed) is not dict:
                    _error(f'design.{role}_params must be an object')
                fixed_signature, fixed_paths, fixed_contract = _task(fixed)
                if ('dataset_assets' in fixed_signature and
                        not strict_equal(current.get('dataset_assets'), fixed_signature['dataset_assets'])):
                    _error(f'design.{role}_params/dataset_assets is frozen; shared search cannot replace dataset files')
                for name in ('benchmark_task_id', 'benchmark_source'):
                    if name in fixed_signature and not strict_equal(current.get(name), fixed_signature[name]):
                        _error(f'design.{role}_params/{name} is frozen; shared search cannot replace task identity')
                # A shared grid may explicitly override values. It must not
                # erase task dimensions/contract by replacing a whole object.
                for name in fixed_signature.keys() - current.keys():
                    _error(f'design.{role}_params{fixed_paths[name]} ({name}) was lost during grid expansion; actual missing')
                if fixed_contract and not has_contract:
                    _error(f'design.{role}_params/comparison_task was lost during grid expansion')
                if fixed_contract:
                    # Unlike legacy alias-only parameters, an explicit task
                    # declaration freezes these values before grid expansion.
                    # Replacing both its value and target must not redefine the
                    # task while appearing internally self-consistent.
                    for index, row in enumerate(fixed[_CONTRACT]['dimensions']):
                        name, value = row['name'], row['value']
                        if not strict_equal(current[name], value):
                            _error(f'design.{role}_params/comparison_task/dimensions/{index}/value '
                                   f'({name}) is frozen at {_show(value)}; expanded configuration '
                                   f'has actual {_show(current[name])}')
            assert_same_task(reference, params)
        except ValueError as exc:
            raise ValueError(f'{label} compared with {reference_label}: {exc}') from exc
    return task_signature(reference)


def validate_invocation(plan, params):
    """Recheck a frozen plan and the concrete invocation before execution."""
    validate_plan(plan)
    from . import benchmark_matrix
    if benchmark_matrix.enabled(plan):
        task = benchmark_matrix.task_for(plan, params)
        validate_invocation(task['plan'], params)
        if params.get('phase') == 'validation':
            from .python_training import jobs
            allowed = [p for stage in ('experiments', 'tuning') for _, p in jobs(task['plan'], stage)]
            if not any(strict_equal(params, p) for p in allowed):
                _error('validation invocation differs from the exact task configuration/seed inventory')
        return
    label, _, reference = next(_candidates(plan))
    try:
        assert_same_task(reference, params)
    except ValueError as exc:
        raise ValueError(f'invocation compared with {label}: {exc}') from exc
