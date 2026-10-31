"""One reviewed optimizer candidate at a time, appended to the frozen plan.

Original plans, trials and approvals are never rewritten. The supplemental
manifest binds an exact proposal, three independent votes, and source evidence.
"""
import copy
import math

from .experiment_workflow import sha
from .storage import now
from .training_adjustment import obj

COLLECTION = 'parameter_search_batches'
SCHEMA = obj({'base_unit_id': {'type': 'string', 'minLength': 1},
    'factor_path': {'type': 'string', 'pattern': '^/'},
    'value': {'type': 'number'}, 'lower_bound': {'type': 'number'},
    'upper_bound': {'type': 'number'}, 'rationale': {'type': 'string', 'minLength': 1, 'maxLength': 600}})
# Optional only for replay of older approved manifests. New proposal schemas
# require an explicit single control seed; the frozen plan is never rewritten.
SCHEMA['properties']['validation_seed'] = {'type': 'integer', 'minimum': 0}
from .parameter_grid import VALUES
from .tuning_scope import VALUE
SCHEMA['properties']['value'] = VALUE
SCHEMA['properties']['lower_bound'] = {'type': ['number', 'null']}
SCHEMA['properties']['upper_bound'] = {'type': ['number', 'null']}
SCHEMA['properties']['scope_sha256'] = {'type': 'string', 'pattern': '^[a-f0-9]{64}$'}
SCHEMA['properties']['variant'] = {'enum': ['shared', 'baseline', 'proposed']}
SCHEMA['properties']['yaml_revision_id'] = {'type': 'string', 'minLength': 1}
SCHEMA['properties']['yaml_sha256'] = {'type': 'string', 'pattern': '^[a-f0-9]{64}$'}
SCHEMA['properties']['grid_values'] = {**VALUES, 'items': VALUE}
SCHEMA['properties']['grid_rationale'] = {'type': 'string', 'minLength': 1, 'maxLength': 600}
ALLOWED = {'lr', 'learning_rate', 'weight_decay', 'grad_clip', 'clip_grad_norm', 'max_grad_norm'}
PARENTS = {'optimizer', 'training', 'train', 'optimization', 'optimizer_params', 'training_params', 'common'}


def relative_path_hint(spec, evidence):
    """Identify an evidence-qualified control path; never change an invocation."""
    parts = spec.get('factor_path', '').split('/')
    if len(parts) < 4 or parts[1] != 'job_parameters':
        return None
    name = parts[2].replace('~1', '/').replace('~0', '~')
    base = next((u for u in evidence['units'] if u['id'] == spec.get('base_unit_id')), {})
    original = evidence['job_parameters'].get(name)
    relative = parts[3:]
    if (name not in base.get('jobs', []) or not original
            or original.get('seed') != spec.get('validation_seed')
            or len(relative) > 3 or relative[-1] not in ALLOWED
            or any(p not in PARENTS for p in relative[:-1])):
        return None
    target = original
    try:
        for token in relative:
            target = target[token]
    except (KeyError, TypeError):
        return None
    return '/' + '/'.join(relative)


def propose(spec, evidence):
    if spec.get('scope_sha256'):
        from .tuning_candidates import propose as broad_candidate
        return broad_candidate(spec, evidence)
    if spec.get('variant', 'shared') != 'shared':
        raise ValueError('Method-local candidates require an independently reviewed broad tuning scope')
    from .prose_schema import validate
    from .fair_comparison import assert_same_task
    validate(spec, SCHEMA)
    parts = spec['factor_path'][1:].split('/')
    if (len(parts) > 3 or parts[-1] not in ALLOWED or any(p not in PARENTS for p in parts[:-1])):
        hint = relative_path_hint(spec, evidence)
        raise ValueError('Search may change one existing optimizer scalar only; task, seeds, loss and budget are frozen. '
            f"Rejected parameter_search.factor_path={spec['factor_path']!r} before candidate construction. "
            'factor_path and training_response.factor_paths are parameter-relative; '
            'do not include /job_parameters/<job>. Evidence citations keep that prefix.'
            + (f' This control resolves to {hint!r}; correct the proposal and submit it for independent review.' if hint else ''))
    value, lower, upper = (spec[k] for k in ('value', 'lower_bound', 'upper_bound'))
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in (value, lower, upper)) or not 0 <= lower <= value <= upper or lower == upper:
        raise ValueError('Search requires a finite nonnegative bounded candidate')
    if parts[-1] in {'lr', 'learning_rate'} and value <= 0:
        raise ValueError('Learning rate must be positive')
    if spec.get('grid_values') is not None:
        value = float(value)  # New grid points use one numeric representation; legacy replay is unchanged.
    base = next((u for u in evidence['units'] if u['id'] == spec['base_unit_id']), None)
    comparison = evidence['comparisons'].get(spec['base_unit_id'], {})
    if not base or base['kind'] not in {'pilot', 'grid'} or not base['complete'] or not comparison.get('comparable'):
        raise ValueError('Parameter search requires a completed same-task paired control batch')
    known = {sha(p) for p in evidence['job_parameters'].values()}
    jobs, original_values = [], []
    selected_seed = spec.get('validation_seed')
    if selected_seed is not None and not any(evidence['job_parameters'][n].get('seed') == selected_seed for n in base['jobs']):
        raise ValueError('Search seed must have an existing completed paired control')
    for name in base['jobs']:
        original = evidence['job_parameters'][name]
        if selected_seed is not None and original.get('seed') != selected_seed:
            continue
        candidate = copy.deepcopy(original)
        target = candidate
        try:
            for part in parts[:-1]:
                target = target[part]
            before = target[parts[-1]]
        except (KeyError, TypeError) as exc:
            raise ValueError('The optimizer scalar must exist in both control variants') from exc
        if type(before) not in (int, float) or not math.isfinite(before) or before == value:
            raise ValueError('A parameter search must change a real scalar; seed replication is not search')
        if not lower <= before <= upper:
            raise ValueError('Search bounds must include the control and candidate values')
        original_values.append(before)
        target[parts[-1]] = value
        assert_same_task(original, candidate)
        if candidate.get('phase') != 'validation' or candidate.get('variant') not in {'baseline', 'proposed'}:
            raise ValueError('Only paired validation jobs may be appended')
        if sha(candidate) in known:
            raise ValueError('This parameter/seed configuration is already frozen; do not rerun it')
        jobs.append(candidate)
    if len(set(original_values)) != 1 or {p['variant'] for p in jobs} != {'baseline', 'proposed'}:
        raise ValueError('Both variants must share the same controlled optimizer change')
    for seed in {p['seed'] for p in jobs}:
        if sorted(p['variant'] for p in jobs if p['seed'] == seed) != ['baseline', 'proposed']:
            raise ValueError('Every existing control seed requires exactly one baseline/candidate pair')
    identity = 'search-' + sha({'binding': evidence['binding'], 'jobs': jobs})[:24]
    unit = {'id': identity, 'kind': 'grid', 'task_id': base['task_id'],
        'jobs': [(identity + '-' + str(i), p) for i, p in enumerate(jobs)],
        'factor_path': spec['factor_path'], 'base_unit_id': base['id']}
    from .parameter_grid import definition, check_budget
    grid = definition(spec, evidence, original_values[0], base['task_id'])
    if grid:
        check_budget(evidence, grid)
        unit['parameter_grid'] = grid
    return unit


def response_evidence(proposal, evidence):
    """Run existing causal-training gates on the exact proposed extra batch."""
    unit = propose(proposal['parameter_search'], evidence)
    augmented = copy.deepcopy(evidence)
    augmented['units'].append({**unit, 'jobs': [n for n, _ in unit['jobs']], 'available': True, 'complete': False})
    augmented['job_parameters'].update(dict(unit['jobs']))
    if proposal['parameter_search'].get('scope_sha256'):
        augmented['parameter_intervention_variant'] = proposal['parameter_search'].get('variant', 'shared')
    return {**proposal, 'action': 'grid_search', 'unit_id': unit['id']}, augmented


def control_records(spec, evidence):
    """Resolve controls from the declared completed pair, not the alert focus.

    Comparison IDs and exact frozen parameters must both match. An unrelated
    completed run, another seed, or a failed/mutated receipt supplies no control.
    """
    unit = propose(spec, evidence)
    base = next(u for u in evidence['units'] if u['id'] == spec['base_unit_id'])
    pairs = evidence['comparisons'][base['id']]['pairs']
    records = {r['id']: r for r in evidence['records']}
    controls = []
    for _, candidate in unit['jobs']:
        originals = [evidence['job_parameters'][n] for n in base['jobs']
                     if evidence['job_parameters'][n].get('seed') == candidate['seed']
                     and evidence['job_parameters'][n].get('variant') == candidate['variant']]
        matching = [p for p in pairs if p['seed'] == candidate['seed']]
        if len(originals) != 1 or len(matching) != 1:
            raise ValueError('Parameter search requires one exact completed control per seed and variant')
        record = records.get(matching[0].get(candidate['variant'] + '_trial_id'), {})
        if record.get('status') != 'completed' or sha(record.get('params')) != sha(originals[0]):
            raise ValueError('Parameter search control receipt differs from its frozen paired configuration')
        controls.append(record)
    return controls


def _authorized(engine, run_id, record, plan):
    from .experiment_decisions import DECISIONS, _approved
    from .python_training import code_hash
    from .training_adjustment import _basis
    body = record['body']
    if record['sha256'] != sha(body):
        raise ValueError('Supplemental search manifest changed')
    binding = body['binding']
    run = engine.store.get('runs', run_id)
    basis = _basis(engine, run_id)
    if (record['run_id'] != run_id or binding['cycle'] != engine.cycle(run_id)
            or binding['plan_sha256'] != sha(plan) or binding['code_sha256'] != code_hash(basis['program'])
            or binding['revision'] != basis['program'].get('revision', 0)
            or binding['config_sha256'] != sha(run['config'])):
        raise ValueError('Supplemental search code/plan/configuration binding changed')
    decision = engine.store.get(DECISIONS, body['decision_id']) or {}
    if not _approved(decision) or decision.get('run_id') != run_id or decision['proposal_sha256'] != body['proposal_sha256']:
        raise ValueError('Supplemental search requires three unchanged independent approvals')
    archive = engine.store.get('experiment_evidence', decision['evidence']['id']) or {}
    evidence = archive.get('summary')
    if sha(evidence) != decision['evidence']['sha256'] or evidence['binding'] != binding:
        raise ValueError('Supplemental search evidence binding changed')
    if sha(propose(decision['proposal']['parameter_search'], evidence)) != sha(body['unit']):
        raise ValueError('Supplemental jobs differ from the reviewed proposal')
    if evidence.get('hyperparameter_yaml', {}).get('ready'):
        from .hyperparameter_yaml import validate_candidate
        validate_candidate(engine, run_id, decision['proposal']['parameter_search'], evidence, body['unit'])
    return body['unit']


def catalog(engine, run_id, plan=None):
    rows = engine.store.list(COLLECTION, run_id)
    # Initial delivery checks precede activation of the first program. With no
    # supplemental searches there is no candidate source binding to validate.
    if not rows:
        return []
    plan = plan or engine.experiment_plan(run_id)
    from .training_adjustment import _basis
    from .python_training import code_hash
    program = _basis(engine, run_id)['program']
    return [_authorized(engine, run_id, r, plan) for r in rows
            if r['body']['binding']['cycle'] == engine.cycle(run_id)
            and r['body']['binding']['plan_sha256'] == sha(plan)
            and r['body']['binding']['revision'] == program.get('revision', 0)
            and r['body']['binding']['code_sha256'] == code_hash(program)]


def jobs(engine, run_id, plan=None):
    return [job for unit in catalog(engine, run_id, plan) for job in unit['jobs']
            if 'new_job_names' not in unit or job[0] in unit['new_job_names']]


def register(engine, run_id, decision, evidence):
    from .dataset_optimization import validate_proposal
    validate_proposal(decision['proposal'], evidence)
    unit = propose(decision['proposal']['parameter_search'], evidence)
    if evidence.get('hyperparameter_yaml', {}).get('ready'):
        from .hyperparameter_yaml import validate_candidate
        validate_candidate(engine, run_id, decision['proposal']['parameter_search'], evidence, unit)
    body = {'binding': evidence['binding'], 'unit': unit, 'decision_id': decision['id'],
            'proposal_sha256': decision['proposal_sha256']}
    record = {'id': run_id + ':' + unit['id'], 'run_id': run_id, 'body': body,
              'sha256': sha(body), 'created_at': now(), 'scientific_approval': False}
    _authorized(engine, run_id, record, engine.experiment_plan(run_id))
    from .rapid_experiment_revision import _budget
    _budget(engine, run_id, len(unit.get('new_job_names', unit['jobs'])))
    def retain(old):
        if old and old['sha256'] != record['sha256']:
            raise ValueError('Existing supplemental batch is immutable')
        return old or record
    engine.store.mutate(COLLECTION, record['id'], retain)
    return unit


def validate_invocation(engine, run_id, plan, params):
    """Extend inventory only for authenticated, exact supplemental parameters."""
    from .fair_comparison import validate_invocation as original, assert_same_task
    from .benchmark_matrix import enabled, task_for
    candidates = jobs(engine, run_id, plan)
    if params.get('phase') == 'validation' and any(sha(p) == sha(params) for _, p in candidates):
        child = task_for(plan, params)['plan'] if enabled(plan) else plan
        assert_same_task(child['validation_pairs'][0]['baseline'], params)
        return
    original(plan, params)


def display_jobs(store, run_id, plan):
    """Read-only saved inventory. Unavailable approvals never crash the page."""
    from .experiment_decisions import _approved
    run = store.get('runs', run_id) or {}
    rows = [r for r in store.list(COLLECTION, run_id) if r['body']['binding']['plan_sha256'] == sha(plan)
            and r['body']['binding']['cycle'] == run.get('state', {}).get('research_cycle', 1)]
    jobs = []
    for record in rows:
        body = record['body']
        decision = store.get('experiment_decisions', body['decision_id']) or {}
        if (record['sha256'] != sha(body) or not _approved(decision)
                or decision['proposal_sha256'] != body['proposal_sha256']):
            return [], {'state': 'unavailable', 'message': '追加参数批次的批准记录待复核。'}
        jobs.extend(j for j in body['unit']['jobs'] if 'new_job_names' not in body['unit'] or j[0] in body['unit']['new_job_names'])
    return jobs, {'state': 'saved', 'batch_count': len(rows), 'display_only': True}
