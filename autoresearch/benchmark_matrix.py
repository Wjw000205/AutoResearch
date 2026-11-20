"""Source-bound independent task plans; never rank across tasks."""
from __future__ import annotations

import copy
import re
from collections import Counter

from .fair_comparison import canonical_dimensions, dimension_name, strict_equal, task_signature


TASK_KEY = 'benchmark_task_id'
SOURCE_KEY = 'benchmark_source'
PLAN_FIELDS = ('baseline_params', 'proposed_params', 'ablation_params', 'search_space',
               'ablation_search_space', 'validation_seeds', 'final_test_seeds')
REQUIRED_META = ('task_id', 'protocol_id', 'protocol_sha256', 'dataset')
META_FIELDS = (*REQUIRED_META, 'task_dimensions', 'input_length', 'output_length')


def metadata(task):
    return {k: copy.deepcopy(task[k]) for k in META_FIELDS if k in task}


def dimensions(task):
    values = dict(task.get('task_dimensions', {}))
    for name in ('dataset', 'input_length', 'output_length'):
        if name in task:
            if name in values and not strict_equal(values[name], task[name]):
                raise ValueError(f'Conflicting task dimension {name}')
            values[name] = task[name]
    return canonical_dimensions(values)


def enabled(plan):
    return isinstance(plan, dict) and plan.get('version') == 3 and 'task_plans' in plan


def verify_sources(engine, run_id, plan, design):
    if not enabled(plan):
        return
    if plan.get('metric_contract'):
        from .metric_contract_recovery import checked
        checked(engine, run_id, plan, design)
    if plan.get('dataset_configuration_protocol') == 1:
        from .dataset_experiments import current, gate
        saved = current(engine, run_id)
        if not saved or saved['plan'] != plan or saved['design'] != design:
            raise ValueError('Dataset configuration no longer matches its reviewed version')
        gate(engine, run_id)
        return
    from .experiment_protocols import resolve_design
    resolved = resolve_design(engine, run_id, design)
    for task in plan['task_plans']:
        if not strict_equal(task['source_protocol'], resolved.get(task['protocol_id'])):
            raise ValueError('Frozen task paper protocol or its source evidence changed')


def design_schema(schema):
    schema = copy.deepcopy(schema)
    axis_names = {'type': 'array', 'minItems': 1, 'maxItems': 64, 'uniqueItems': True,
                  'items': {'type': 'string', 'pattern': '^[a-z][a-z0-9_]{0,79}$'}}
    schema['properties']['benchmark_io_axes'] = {'type': 'object',
        'properties': {'inputs': axis_names, 'outputs': axis_names},
        'required': ['inputs', 'outputs'], 'additionalProperties': False}
    props = {key: copy.deepcopy(schema['properties'][key]) for key in PLAN_FIELDS}
    # Explicit per-condition coverage may omit an ablation without inventing jobs.
    props['ablation_search_space']['minItems'] = 0
    props.update({key: {'type': 'string', 'minLength': 1} for key in ('task_id', 'protocol_id')})
    from .dataset_contract import name_schema
    props['dataset'] = name_schema()
    props['task_id']['pattern'] = '^[a-z][a-z0-9_-]{0,39}$'
    props['protocol_sha256'] = {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}
    props.update({key: {'type': 'integer', 'minimum': 1} for key in ('input_length', 'output_length')})
    props['task_dimensions'] = {'type': 'object', 'maxProperties': 64,
        'propertyNames': {'pattern': '^[a-z][a-z0-9_]{0,79}$'},
        'additionalProperties': {'type': ['string', 'number', 'boolean']}}
    schema['properties']['benchmark_tasks'] = {'type': 'array', 'minItems': 1, 'maxItems': 64,
        'items': {'type': 'object', 'properties': props, 'required': [*REQUIRED_META, *PLAN_FIELDS], 'additionalProperties': False}}
    return schema


def _source_values(task, source):
    if not source or source.get('id') != task['protocol_id'] or source.get('protocol_sha256') != task['protocol_sha256']:
        raise ValueError('Every benchmark task requires a revalidated exact paper protocol')
    fields = {}
    for field in source['parameters']:
        name = dimension_name(field['name'])
        previous = fields.get(name)
        if previous is not None and not strict_equal(
                {k: previous.get(k) for k in ('status', 'values')},
                {k: field.get(k) for k in ('status', 'values')}):
            raise ValueError(f'Conflicting paper protocol aliases for {name}')
        fields[name] = field
    state = 'configured' if source.get('kind') == 'experiment_configuration' else 'reported'
    for name, value in dimensions(task).items():
        field = fields.get(name, {})
        if field.get('status') != state or not any(strict_equal(value, v) for v in field.get('values', [])):
            raise ValueError(f'{task["task_id"]}.{name} is not a reported value of the cited paper setting')
    return fields


def compile_task(design, task, source):
    if (type(task) is not dict or not {*REQUIRED_META, *PLAN_FIELDS} <= set(task)
            or not set(task) <= {*META_FIELDS, *PLAN_FIELDS}
            or type(task.get('task_dimensions', {})) is not dict
            or not isinstance(task['task_id'], str) or not re.fullmatch('[a-z][a-z0-9_-]{0,39}', task['task_id'])
            or any(type(task[n]) is not int or task[n] <= 0 for n in ('input_length', 'output_length') if n in task)):
        raise ValueError('Each benchmark task requires explicit metadata, complete branch parameters, grids and seed lists')
    fields = _source_values(task, source)
    child = {**{k: copy.deepcopy(v) for k, v in design.items() if k != 'benchmark_tasks'},
             **{k: copy.deepcopy(task[k]) for k in PLAN_FIELDS}}
    if task['ablation_search_space'] == [] and design.get('baseline_mode') == 'paper_reported':
        child['condition_ablation_protocol'] = 1
    for role in ('baseline', 'proposed', 'ablation'):
        params = child[role + '_params']
        if TASK_KEY in params or SOURCE_KEY in params:
            raise ValueError('Benchmark identity is assigned by the host, not model parameters')
        contract = params.get('comparison_task')
        if not contract:
            raise ValueError('Matrix branches require explicit comparison_task dimensions and exact parameter paths')
        signature = canonical_dimensions(task_signature(params))
        for name, value in dimensions(task).items():
            if not strict_equal(signature.get(name), value):
                raise ValueError(f'{task["task_id"]}.{role}.{name} differs from its source-bound task')
        for name, value in signature.items():
            if name == 'dataset_assets':
                continue  # File provenance is independently checked by the dataset service.
            field = fields.get(name, {})
            state = 'configured' if source.get('kind') == 'experiment_configuration' else 'reported'
            if field.get('status') != state or not any(strict_equal(value, v) for v in field.get('values', [])):
                raise ValueError(f'Common dimension {name} lacks a reported value in this paper setting')
        params.update({TASK_KEY: task['task_id'], SOURCE_KEY: {
            'protocol_id': task['protocol_id'], 'protocol_sha256': task['protocol_sha256']}})
    return child


def _totals(children, axis):
    axis = dimension_name(axis)
    total = sum(t['plan']['planned_trials'] for t in children)
    finals = sum(t['plan']['final_pairs'] for t in children)
    if not axis:
        return total, finals
    from .experiment_workflow import sha
    groups = {}
    for task in children:
        dims = dimensions(task)
        if task['plan'].get('baseline_mode') != 'paper_reported' or axis not in dims:
            raise ValueError('Paper selection requires an explicit validation axis')
        key = sha({k:v for k,v in dims.items() if k != axis})
        seeds = task['plan']['final_test_seeds']
        if groups.setdefault(key, seeds) != seeds:
            raise ValueError('Paper selection candidates must share the final seed inventory')
    final_count = sum(len(seeds) for seeds in groups.values())
    return total-finals+final_count, final_count


def build(design, max_trials, protocols):
    from .experiment_plan import build_plan, _signature
    tasks = design['benchmark_tasks']
    if type(tasks) is not list or not 1 <= len(tasks) <= 64:
        raise ValueError('benchmark_tasks must contain 1 to 64 explicitly sourced tasks')
    children, seen, identities = [], set(), set()
    for task in tasks:
        source = (protocols or {}).get(task.get('protocol_id'))
        child_design = compile_task(design, task, source)
        identity = _signature({k: v for k, v in canonical_dimensions(task_signature(child_design['baseline_params'])).items()
                               if k not in (TASK_KEY, SOURCE_KEY)})
        if task['task_id'] in seen or identity in identities:
            raise ValueError('Duplicate benchmark task ID or complete comparison setting')
        seen.add(task['task_id'])
        identities.add(identity)
        # Both seed inventories are explicit, so spare budget never adds tests.
        child = build_plan(child_design, max_trials)
        children.append({**metadata(task),
            'design': child_design, 'plan': child, 'source_protocol': copy.deepcopy(source)})
    total, finals = _totals(children, design.get('paper_selection_axis'))
    if not any(task['plan']['ablation_candidates'] for task in children):
        raise ValueError('A benchmark matrix must retain at least one explicitly declared ablation condition')
    from .matrix_acceptance import binding
    aggregate = binding(design, children)
    if max_trials is not None and total > max_trials:
        raise ValueError(f'Benchmark matrix needs at least {total} trials; max_trials={max_trials}. No tasks, grid points or seeds were discarded.')
    return {'version': 3, 'task_plans': children, 'planned_trials': total,
        **({'matrix_acceptance': aggregate} if aggregate else {}),
        **({'paper_selection_axis':design['paper_selection_axis']} if design.get('paper_selection_axis') else {}),
        **({'baseline_mode':'paper_reported', 'paper_reference':copy.deepcopy(design['paper_reference'])}
           if design.get('baseline_mode') == 'paper_reported' else {}),
        'grid': [p for t in children for p in t['plan']['grid']],
        'validation_pairs': [p for t in children for p in t['plan']['validation_pairs']],
        'ablation_candidates': [p for t in children for p in t['plan']['ablation_candidates']],
        'ablation_params': copy.deepcopy(children[0]['plan']['ablation_params']),
        'selection_seed': children[0]['plan']['selection_seed'],
        'final_pairs': finals,
        'protocol': ('Each task runs only our model and own ablations. Published baseline scores are external references.'
                    if design.get('baseline_mode') == 'paper_reported' else
                    'Each source-bound task setting has its own paired search, selection and frozen final tests. '
                    'All tasks and declared seeds are mandatory; no pooled winner or pooled performance score.')}


def validate(plan, design=None):
    from .fair_comparison import validate_plan
    children = plan.get('task_plans')
    if not isinstance(children, list) or not children:
        raise ValueError('Matrix requires independent task plans')
    from .matrix_acceptance import binding
    if plan.get('matrix_acceptance') != binding(design or children[0]['design'], children):
        raise ValueError('Matrix acceptance binding differs from the declared metric and tasks')
    if design is not None and any(plan.get(k) != design.get(k) for k in ('baseline_mode','paper_reference','paper_selection_axis')):
        raise ValueError('Matrix external comparison source or selection rule changed')
    ids = [t['task_id'] for t in children]
    if len(set(ids)) != len(ids):
        raise ValueError('Duplicate matrix task IDs')
    if design is not None and ids != [t['task_id'] for t in design.get('benchmark_tasks', [])]:
        raise ValueError('Matrix task coverage differs from the approved design')
    for index, task in enumerate(children):
        if design is not None and not strict_equal(metadata(task), metadata(design['benchmark_tasks'][index])):
            raise ValueError('Matrix metadata differs from the approved task declaration')
        declaration = {**metadata(task),
                       **{k: task['design'][k] for k in PLAN_FIELDS}}
        # Recompile without trusted host metadata to check the exact branches.
        for role in ('baseline', 'proposed', 'ablation'):
            declaration[role + '_params'] = {k: v for k, v in declaration[role + '_params'].items()
                                              if k not in (TASK_KEY, SOURCE_KEY)}
        expected = compile_task(design or task['design'],
            design['benchmark_tasks'][index] if design is not None else declaration, task['source_protocol'])
        if not strict_equal(task['design'], expected):
            raise ValueError('Matrix child design changed')
        validate_plan(task['plan'], task['design'])
        from .experiment_plan import build_plan
        if not strict_equal(task['plan'], build_plan(task['design'], task['plan']['planned_trials'])):
            raise ValueError('Matrix child plan differs from the frozen task design')
    for field in ('grid', 'validation_pairs', 'ablation_candidates'):
        if not strict_equal(plan[field], [p for t in children for p in t['plan'][field]]):
            raise ValueError('Matrix flattened inventory differs from independent task plans')
    if (plan['planned_trials'], plan['final_pairs']) != _totals(children, plan.get('paper_selection_axis')):
        raise ValueError('Matrix trial totals changed')
    return {'benchmark_tasks': [metadata(t) for t in children]}


def task_for(plan, params):
    found = [task for task in plan['task_plans'] if task['task_id'] == params.get(TASK_KEY)]
    if len(found) != 1:
        raise ValueError('Invocation has no unique frozen benchmark task')
    return found[0]


def task_trials(trials, task):
    return [t for t in trials if t.get('params', {}).get(TASK_KEY) == task['task_id'] and not t.get('superseded')]


def jobs(plan, stage):
    from .python_training import jobs as child_jobs
    return [(task['task_id'] + '--' + name, params) for task in plan['task_plans']
            for name, params in child_jobs(task['plan'], stage)]


def _coverage(task, trials, additional_jobs=()):
    from .python_training import jobs as child_jobs
    from .experiment_plan import _signature
    expected = Counter(_signature(p) for stage in ('experiments', 'tuning') for _, p in child_jobs(task['plan'], stage))
    expected.update(_signature(p) for _, p in additional_jobs if p.get(TASK_KEY) == task['task_id'])
    actual = Counter(_signature(t['params']) for t in trials if t.get('status') == 'completed'
                     and t['params'].get('phase') == 'validation')
    return expected == actual


def validation_summary(plan, trials, metric, direction, *, additional_jobs=(), grid_selection=None, admission=None):
    from .research_cycle import validation_summary as summarize
    from .experiment_plan import check_outcome, validation_seeds
    summaries = []
    for task in plan['task_plans']:
        rows = task_trials(trials, task)
        if plan.get('baseline_mode') == 'paper_reported':
            from .paper_baselines import own_validation
            ranked = rows
            if grid_selection and grid_selection['task_id'] == task['task_id']:
                ranked = [r for r in rows if r['id'] in grid_selection['trial_ids']]
            summary = summarize(ranked, metric, direction)
            summary.update(own_validation(summary, ranked, validation_seeds(task['plan'])))
            summary['issues'] = [i for i in summary['issues'] if i['code'] != 'no_comparable_pairs']
        elif grid_selection and grid_selection['task_id'] == task['task_id']:
            from .parameter_grid import selected_summary
            summary = selected_summary(rows, metric, direction, grid_selection)
        else:
            summary = summarize(rows, metric, direction)
        summary['plan_complete'] = _coverage(task, rows, additional_jobs)
        if not summary['plan_complete']:
            summary['comparable'] = False
            summary['issues'].append({'code': 'matrix_task_incomplete', 'trial_ids': [],
                'message': 'Every declared configuration, ablation and validation seed must complete before selection.'})
        ids = {tid for group in (summary.get('best_baseline'), summary.get('best_proposed')) if group for tid in group['trial_ids']}
        if plan.get('matrix_acceptance') or plan.get('metric_contract'):
            summary['acceptance_checks'] = {'phase': 'validation', 'criteria_complete': False, 'checks': [],
                'research_outcome': 'inconclusive', 'scope': 'Acceptance is evaluated once over the complete matrix.'}
        else:
            summary['acceptance_checks'] = check_outcome([r for r in rows if r['id'] in ids],
                plan['acceptance']['criteria'], phase='validation',
                complete=plan['acceptance']['complete'] and summary['plan_complete'],
                expected_seeds=validation_seeds(task['plan']))
        summaries.append({**metadata(task), **summary})
    if plan.get('metric_contract'):
        from .metric_contract import evaluate
        acceptance = evaluate(plan, trials, summaries, admission)
    elif plan.get('matrix_acceptance'):
        from .matrix_acceptance import evaluate
        acceptance = evaluate(plan, trials)
    else:
        acceptance = aggregate_outcomes([s['acceptance_checks'] for s in summaries], summaries, 'validation')
    return {'phase': 'validation', 'metric': metric, 'direction': direction, 'benchmark_matrix': True,
        **({'metric_contract': plan['metric_contract']} if plan.get('metric_contract') else {}),
        **({'matrix_acceptance': plan['matrix_acceptance'],
            'relative_improvement': acceptance['checks'][0]['relative_improvement']} if plan.get('matrix_acceptance') else {}),
        **({'baseline_mode':'paper_reported','paper_reference':plan['paper_reference']} if plan.get('baseline_mode') else {}),
        'benchmark_tasks': summaries, 'comparable': all(s['comparable'] for s in summaries),
        'acceptance_checks': acceptance, 'issues': [{**i, 'task_id': s['task_id']} for s in summaries for i in s['issues']],
        'scope': ('Individual task scores and an equal-weight paired validation gate over all tasks.'
                  if plan.get('matrix_acceptance') else 'Independent task summaries only; scores, ranks and seed pairs are never pooled across tasks.')}


def aggregate_outcomes(outcomes, tasks, phase):
    checks = [{**check, **metadata(task)}
              for task, outcome in zip(tasks, outcomes) for check in outcome['checks']]
    complete = bool(outcomes) and all(o['criteria_complete'] for o in outcomes)
    status = ('negative' if any(o['research_outcome'] == 'negative' for o in outcomes) else
              'positive' if complete and all(o['research_outcome'] == 'positive' for o in outcomes) else 'inconclusive')
    return {'research_outcome': status, 'phase': phase, 'criteria_complete': complete, 'checks': checks,
        'benchmark_tasks': [{**metadata(t), 'outcome': o} for t, o in zip(tasks, outcomes)],
        'test_reuse_for_tuning_allowed': False, 'scope': 'Every task is assessed separately; positive requires all tasks. No pooled mean or publication claim.'}


def select(plan, trials, metric, direction, *, additional_jobs=(), grid_selection=None):
    from .engine import digest
    from .storage import now
    summary = validation_summary(plan, trials, metric, direction, additional_jobs=additional_jobs,
                                 grid_selection=grid_selection)
    selections = []
    published = plan.get('baseline_mode') == 'paper_reported'
    for task in summary['benchmark_tasks']:
        valid = (task['plan_complete'] and task.get('own_control_eligible') if published
                 else task['comparable'] and task.get('best_baseline'))
        if not valid or not task.get('best_proposed'):
            raise ValueError(f'{task["task_id"]}: incomplete or incomparable validation evidence; no selection committed')
        best, baseline = task['best_proposed'], task['best_baseline']
        by_id = {t['id']: t for t in trials}
        selections.append({**metadata(task),
            'params': copy.deepcopy(by_id[best['trial_ids'][0]]['params']),
            'baseline_params': None if published else copy.deepcopy(by_id[baseline['trial_ids'][0]]['params']),
            'selected_trial_ids': best['trial_ids'], 'baseline_trial_ids': [] if published else baseline['trial_ids'],
            'validation_score': task['proposed_score'], 'baseline_validation_score': task['baseline_score'],
            'metric': metric, 'validation_seeds': best['seeds'] if published else task['paired_seeds'],
            **({'parameter_grid_selection': task['parameter_grid_selection']}
               if task.get('parameter_grid_selection') else {})})
    return {'benchmark_matrix': True, 'benchmark_tasks': selections,
        **({'baseline_mode':'paper_reported','paper_reference':plan['paper_reference']} if published else {}),
        'params': {s['task_id']: copy.deepcopy(s['params']) for s in selections},
        'baseline_params': {s['task_id']: copy.deepcopy(s['baseline_params']) for s in selections},
        'plan_hash': digest(plan), 'frozen_at': now(), 'metric': metric}


async def tune(engine, harness, run_id, design, plan, *, execute=True):
    from .python_training import run as train
    from .engine import ResearchBlocked, digest
    from .storage import now
    candidates = []
    for name, params in jobs(plan, 'tuning'):
        candidates.append(params)
        if execute:
            await train(engine, harness, run_id, name, params)
    from .experiment_workflow import require_complete
    await engine._read_consensus_check(run_id, require_complete, engine, run_id, plan=plan)
    from .parameter_search import jobs as search_jobs
    supplemental = search_jobs(engine, run_id, plan)
    candidates.extend(p for _, p in supplemental)
    from .parameter_grid import certified_selection
    grid_selection = certified_selection(engine, run_id)
    try:
        selection = select(plan, [t for t in engine.store.list('experiments', run_id)
            if t.get('research_cycle', 1) == engine.cycle(run_id)], design['primary_metric'], design['direction'],
            additional_jobs=supplemental, grid_selection=grid_selection)
    except ValueError as exc:
        raise ResearchBlocked(str(exc)) from exc
    engine.store.batch([
        ('selections', run_id, {'id': run_id, 'run_id': run_id, **selection}),
        ('tuning_plans', run_id, {'id': run_id, 'run_id': run_id, 'candidates': candidates,
            'final_pairs': plan['final_pairs'], 'experiment_plan_hash': digest(plan),
            'benchmark_tasks': [{**metadata(t), 'final_pairs': t['plan']['final_pairs']} for t in plan['task_plans']],
            'created_at': now()})])
    return {'selection': selection, 'candidates': candidates, 'final_pairs': plan['final_pairs'],
        'benchmark_tasks': selection['benchmark_tasks'], 'validation_results': engine.validation_history(run_id)}


def final_jobs(plan, selection, *, additional_jobs=()):
    from .engine import digest
    from .experiment_plan import final_test_seeds
    if selection.get('plan_hash') != digest(plan):
        raise ValueError('Matrix selection does not bind the current plan')
    selected = selection.get('benchmark_tasks', [])
    if [s['task_id'] for s in selected] != [t['task_id'] for t in plan['task_plans']]:
        raise ValueError('Frozen selections do not cover the matrix exactly')
    rows = []
    # For a paper reporting the best lookback per horizon, freeze that choice
    # on validation before accessing any test row. Other task dimensions stay distinct.
    finalists = None
    if plan.get('paper_selection_axis'):
        axis = plan['paper_selection_axis']
        groups = {}
        for t, winner in zip(plan['task_plans'], selected):
            dims = dimensions(t)
            if axis not in dims:
                raise ValueError('Paper selection axis is missing from a declared task')
            group = digest({k:v for k,v in dims.items() if k != axis})
            score = winner['validation_score']
            rank = (score if t['design']['direction']=='minimize' else -score,
                    dims[axis] if plan.get('matrix_acceptance') else t['task_id'], t['task_id'])
            if group not in groups or rank < groups[group]:
                groups[group] = rank
        finalists = {rank[-1] for rank in groups.values()}
    for task, winner in zip(plan['task_plans'], selected):
        from .python_training import jobs as child_jobs
        candidates = [p for stage in ('experiments', 'tuning') for _, p in child_jobs(task['plan'], stage)]
        candidates.extend(p for _, p in additional_jobs if p.get(TASK_KEY) == task['task_id'])
        published = task['plan'].get('baseline_mode') == 'paper_reported'
        for field in (('params',) if published else ('params', 'baseline_params')):
            if not strict_equal(selection[field].get(task['task_id']), winner[field]):
                raise ValueError('Matrix selected parameters changed')
            variant = 'proposed' if field == 'params' else 'baseline'
            if winner[field].get('variant') != variant or not any(strict_equal(winner[field], p) for p in candidates):
                raise ValueError('Matrix winner is not an exact validated configuration of its task')
        for seed in final_test_seeds(task['plan']):
            if finalists is not None and task['task_id'] not in finalists:
                continue
            for variant in (('proposed',) if published else ('baseline', 'proposed')):
                params = winner['baseline_params' if variant == 'baseline' else 'params']
                rows.append((task['task_id'] + f'--final-{variant}-{seed}',
                    {**copy.deepcopy(params), 'phase': 'test', 'variant': variant, 'seed': seed}))
    return rows


def final_outcome(plan, trials):
    from .experiment_plan import check_outcome, final_test_seeds
    criteria = [] if plan.get('matrix_acceptance') or plan.get('metric_contract') else plan['acceptance']['criteria']
    outcomes = [check_outcome(task_trials(trials, task), criteria,
        complete=bool(criteria) and plan['acceptance']['complete'], expected_seeds=final_test_seeds(task['plan'])) for task in plan['task_plans']]
    return aggregate_outcomes(outcomes, plan['task_plans'], 'test')


def decide(summary, reviews, cycle, max_cycles, threshold, policy):
    from .research_cycle import decide_cycle
    children = [decide_cycle({**child, 'proposal_hash': summary.get('proposal_hash'),
        'revision_hash': summary.get('revision_hash')}, reviews, cycle, max_cycles, threshold, policy)
        for child in summary['benchmark_tasks']]
    decisions = {c['decision'] for c in children}
    if summary.get('metric_contract'):
        from .metric_contract import decide as decide_bound
        return decide_bound(summary, reviews, cycle, max_cycles, threshold, policy)
    if summary.get('baseline_mode') == 'paper_reported':
        unanimous = bool(children) and all(c['unanimous'] for c in children)
        ready = bool(children) and all(t.get('plan_complete') and t.get('own_control_eligible')
                                      for t in summary['benchmark_tasks'])
        mechanism = summary.get('matrix_acceptance')
        passed = (summary['acceptance_checks']['research_outcome'] == 'positive'
                  and summary['relative_improvement'] >= threshold) if mechanism else False
        if mechanism:
            ready = ready and passed
        return {'decision':'finalize' if ready and unanimous and (max_cycles is None or cycle<=max_cycles) else 'blocked',
                'cycle':cycle,'max_cycles':max_cycles,'threshold_met':passed,'unanimous':unanimous,
                'proposal_hash':summary.get('proposal_hash'),'revision_hash':summary.get('revision_hash'),
                'iteration_policy':copy.deepcopy(policy),'min_relative_improvement':threshold,
                'rationale':('须取得完整矩阵的配对验证改善、宿主主指标门槛和独立评议同意后才可测试。' if mechanism else
                             '独立表决和本地验证覆盖决定能否冻结测试；对论文基线的性能结论待同协议结果比较。')}
    decision = ('finalize' if decisions == {'finalize'} else 'blocked' if 'blocked' in decisions else
                'stop' if 'stop' in decisions and decisions <= {'stop', 'finalize'} else
                'blocked' if 'stop' in decisions else 'iterate')
    return {'decision': decision, 'cycle': cycle, 'max_cycles': max_cycles,
        'threshold_met': all(c['threshold_met'] for c in children), 'unanimous': all(c['unanimous'] for c in children),
        'proposal_hash': summary.get('proposal_hash'), 'revision_hash': summary.get('revision_hash'),
        'iteration_policy': copy.deepcopy(policy), 'min_relative_improvement': threshold,
        'rationale': 'Independent dataset/horizon decisions: ' + '; '.join(f'{t["task_id"]}: {c["decision"]}' for t, c in zip(summary['benchmark_tasks'], children)),
        'benchmark_tasks': [{**metadata(t), **c} for t, c in zip(summary['benchmark_tasks'], children)],
        **({'continuation_hold_reason': 'reported_negative_under_stop_policy'} if decision == 'blocked' and 'stop' in decisions else {})}


def evaluation(plan, trials, metric, direction):
    from .evaluation import evaluate_trials
    result = evaluate_trials([], metric, direction)
    tasks = [{**metadata(t), 'evaluation': evaluate_trials(task_trials(trials, t), metric, direction)} for t in plan['task_plans']]
    result.update(benchmark_tasks=tasks, scope='Per-task paired evaluation; no pooled score or overall winner.')
    for key in ('paired_comparisons', 'issues', 'groups'):
        if key in result:
            result[key] = [{**row, **metadata(t)} for t in tasks for row in t['evaluation'].get(key, [])]
    result['tuning_budget'] = {'scope': 'per_task', 'tasks': {t['task_id']: t['evaluation']['tuning_budget'] for t in tasks}}
    statuses = {t['evaluation']['provenance']['status'] for t in tasks}
    result['provenance'].update(status='inconsistent' if 'inconsistent' in statuses else
        'consistent' if statuses == {'consistent'} else 'unverified',
        execution_provenance_unverified=any(t['evaluation']['provenance']['execution_provenance_unverified'] for t in tasks),
        contains_simulation=any(t['evaluation']['provenance']['contains_simulation'] for t in tasks))
    for key in ('eligible_trial_ids', 'included_trial_ids', 'excluded_trial_ids', 'execution_tags'):
        result['provenance'][key] = sorted({i for t in tasks for i in t['evaluation']['provenance'][key]})
    result['provenance']['completed_test_records'] = sum(t['evaluation']['provenance']['completed_test_records'] for t in tasks)
    return result


def progress(plan, trials, metric, direction, *, truncated=False, selection=None, additional_jobs=()):
    validation = validation_summary(plan, trials, metric, direction, additional_jobs=additional_jobs)
    final = final_outcome(plan, trials)
    result = []
    frozen = {}
    if selection:
        try:
            final_jobs(plan, selection, additional_jobs=additional_jobs)
            frozen = {row['task_id']: row for row in selection['benchmark_tasks']}
        except (ValueError, KeyError, TypeError):
            pass
    for task, val, test in zip(plan['task_plans'], validation['benchmark_tasks'], final['benchmark_tasks']):
        rows = task_trials(trials, task)
        tests = [r for r in rows if r['params'].get('phase') == 'test']
        winner = frozen.get(task['task_id'])
        published = task['plan'].get('baseline_mode') == 'paper_reported'
        selected = (not truncated and val['plan_complete'] and winner is not None
            and winner.get('selected_trial_ids') == (val.get('best_proposed') or {}).get('trial_ids')
            and (published or winner.get('baseline_trial_ids') == (val.get('best_baseline') or {}).get('trial_ids')))
        if selected:
            by_id = {row['id']: row for row in rows}
            selected = strict_equal(winner['params'], by_id[winner['selected_trial_ids'][0]]['params'])
            if not published:
                selected &= strict_equal(winner['baseline_params'], by_id[winner['baseline_trial_ids'][0]]['params'])
        test_state = ('not_run' if not tests else 'failed' if any(t['status'] == 'failed' for t in tests)
                      else 'completed' if len(tests) == (1 if published else 2) * task['plan']['final_pairs'] and all(t['status'] == 'completed' for t in tests) else 'incomplete')
        result.append({**metadata(task), 'task_dimensions': dimensions(task), 'planned_trials': task['plan']['planned_trials']
                       + sum(p.get(TASK_KEY) == task['task_id'] for _, p in additional_jobs),
            'completed_trials': sum(t['status'] == 'completed' for t in rows),
            'validation_complete': val['plan_complete'] and not truncated,
            'selection_status': 'frozen' if selected else 'pending',
            'best_baseline': {'params': winner['baseline_params']} if selected and not published else None,
            'best_proposed': {'params': winner['params']} if selected else None,
            'validation_outcome': val['acceptance_checks']['research_outcome'] if not truncated else 'inconclusive',
            'test_status': test_state if not truncated else 'incomplete',
            'test_outcome': test['outcome']['research_outcome'] if not truncated else 'inconclusive',
            'display_only': True})
    return result
