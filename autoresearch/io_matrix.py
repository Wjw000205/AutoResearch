"""One complete executable template and a source-bound table of input/output combinations."""
import copy

from .benchmark_matrix import PLAN_FIELDS, metadata
from .fair_comparison import strict_equal, task_signature


def author_schema():
    names = {'type': 'string', 'pattern': '^[a-z][a-z0-9_]{0,79}$'}
    values = {'type': 'object', 'minProperties': 1, 'maxProperties': 64, 'propertyNames': names,
              'additionalProperties': {'type': ['string', 'number', 'boolean']}}
    row = {'type': 'object', 'properties': {
        'task_id': {'type': 'string', 'pattern': '^[a-z][a-z0-9_-]{0,39}$'},
        'inputs': values, 'outputs': values}, 'required': ['task_id', 'inputs', 'outputs'],
        'additionalProperties': False}
    return {'type': 'object', 'properties': {'protocol_id': {'type': 'string', 'minLength': 1},
            'protocol_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
            'combinations': {'type': 'array', 'minItems': 1, 'maxItems': 64, 'items': row}},
            'required': ['protocol_id', 'protocol_sha256', 'combinations'], 'additionalProperties': False}


def instantiate(template, meta, dimensions):
    fields = {key: copy.deepcopy(template[key]) for key in PLAN_FIELDS}
    for role in ('baseline', 'proposed', 'ablation'):
        profile = fields[role + '_params']
        for item in [profile, *profile.get('configurations', {}).values()]:
            if item is not profile and not item.get('comparison_task'):
                continue  # Named overrides can inherit dimensions from their profile.
            for name, value in dimensions.items():
                _set_axis(item, name, value)
    return {**fields, **meta}


def materialize(draft, *, dataset_catalog=None):
    """Adapt a compact author submission to the existing frozen plan/compiler interface."""
    from .dataset_contract import materialize as resolve_datasets
    draft = resolve_datasets(draft, dataset_catalog)
    matrix = draft.get('benchmark_io_matrix')
    if matrix is None:
        return draft
    if 'benchmark_tasks' in draft:
        raise ValueError('Supply one I/O template or explicit tasks, not both')
    template = {k: v for k, v in draft.items() if k != 'benchmark_io_matrix'}
    datasets = [task_signature(template[role + '_params']).get('dataset')
                for role in ('baseline', 'proposed', 'ablation')]
    if (not all(isinstance(value, str) and value.strip() for value in datasets)
            or any(not strict_equal(value, datasets[0]) for value in datasets[1:])):
        raise ValueError('I/O template branches must bind the same concrete dataset through their actual parameter paths')
    rows = []
    if not matrix['combinations']:
        raise ValueError('An I/O matrix requires at least one combination')
    first = matrix['combinations'][0]
    axes = {side: sorted(first[side]) for side in ('inputs', 'outputs')}
    if not all(axes.values()) or set(axes['inputs']) & set(axes['outputs']):
        raise ValueError('Input and output dimension names must be distinct and nonempty')
    for item in matrix['combinations']:
        if any(sorted(item[side]) != names for side, names in axes.items()):
            raise ValueError('All combinations must bind the same input/output dimensions')
        values = {**item['inputs'], **item['outputs']}
        if 'dataset' in values:
            raise ValueError('A template binds one common dataset')
        meta = {k: matrix[k] for k in ('protocol_id', 'protocol_sha256')}
        meta.update(dataset=datasets[0], task_id=item['task_id'], task_dimensions=values)
        rows.append(instantiate(template, meta, values))
    return {**template, 'benchmark_tasks': rows, 'benchmark_io_axes': axes}


def review_layout(design):
    """Reconstruct every row; a shared-template approval covers no other changes."""
    rows = design.get('benchmark_tasks') or []
    if not rows:
        return None
    axes = design.get('benchmark_io_axes')
    if axes is None:
        if not all('input_length' in row and 'output_length' in row for row in rows):
            return None  # Legacy time-series rows already name both I/O dimensions.
        axes = {'inputs': ['input_length'], 'outputs': ['output_length']}
    if (not isinstance(axes, dict) or set(axes) != {'inputs', 'outputs'}
            or not all(isinstance(group, list) and group and all(isinstance(k, str) for k in group)
                       for group in axes.values())):
        return None
    names = [*axes['inputs'], *axes['outputs']]
    if len(set(names)) != len(names) or 'dataset' in names:
        return None
    first = rows[0]
    table = []
    for row in rows:
        meta = metadata(row)
        if meta.keys() != metadata(first).keys() or any(
                not strict_equal(v, first[k]) for k, v in meta.items()
                if k not in {*names, 'task_dimensions', 'task_id'}):
            return None
        try:
            from .benchmark_matrix import dimensions
            if row.get('task_dimensions', {}).keys() != first.get('task_dimensions', {}).keys():
                return None
            dims = dimensions(row)
            values = {name: dims[name] for name in names}
            if any(not strict_equal(value, first.get('task_dimensions', {}).get(k))
                   for k, value in row.get('task_dimensions', {}).items() if k not in names):
                return None
            expected = instantiate(first, meta, values)
        except (KeyError, ValueError, TypeError, AttributeError, IndexError):
            return None
        if not strict_equal(row, expected):
            return None
        table.append({'task_id': row['task_id'], **{side: {k: values[k] for k in keys} for side, keys in axes.items()}})
    return table


def protocol_plan(plan):
    """Review the jobs compiled from the separately supplied complete template."""
    from .engine import digest
    from .python_training import jobs
    final_variants = ['proposed'] if plan.get('baseline_mode') == 'paper_reported' else ['baseline', 'proposed']
    return {'plan_sha256': digest(plan), 'planned_trials': plan['planned_trials'],
            **({'metric_contract': plan['metric_contract']} if plan.get('metric_contract') else {}),
            **({'matrix_acceptance': plan['matrix_acceptance']} if plan.get('matrix_acceptance') else {}),
            'final_trials': plan['final_pairs'] * len(final_variants), 'final_variants': final_variants,
            'protocol': plan['protocol'],
            'selection_axis': plan.get('paper_selection_axis'),
            'heldout_seeds': {row['task_id']: row['plan']['final_test_seeds'] for row in plan['task_plans']},
            'validation_jobs': [{'name': name, 'task_id': params['benchmark_task_id'],
                                 'variant': params['variant'], 'seed': params['seed'],
                                 'params_sha256': digest(params)}
                                for stage in ('experiments', 'tuning') for name, params in jobs(plan, stage)]}


def _set_axis(params, name, value):
    from .fair_comparison import _resolve
    rows = [d for d in params['comparison_task']['dimensions'] if d['name']==name]
    if len(rows)!=1 or not rows[0]['paths']:
        raise ValueError('Template requires actual parameter paths for '+name)
    item=rows[0]
    for path in item['paths']:
        if not strict_equal(_resolve(params,path),item['value']):
            raise ValueError('Template pointer and declared dimension disagree: '+path)
        tokens=[p.replace('~1','/').replace('~0','~') for p in path[1:].split('/')]
        parent=params
        for token in tokens[:-1]:
            parent=parent[int(token) if isinstance(parent,list) else token]
        parent[int(tokens[-1]) if isinstance(parent,list) else tokens[-1]]=value
    item['value']=value
    if name in params.get('task_dimensions',{}):
        params['task_dimensions'][name]=value


