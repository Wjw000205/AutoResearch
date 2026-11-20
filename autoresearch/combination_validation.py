"""A bounded coordinate sweep over reviewed factors, using real same-seed controls."""
import copy
import math

from .experiment_workflow import sha
from .storage import now

KEY = 'combination_validation'
REQUESTS = 'combination_validation_requests'
PLANS = 'combination_validation_plans'
GUIDANCE = '''
用户已补充组合验证要求。原单因素范围完成后，还须完成 combination_validation 的一轮有界组合验证。
该扩展补充历史“不自动叠加最优值”的范围说明，仍沿用原因素、取值、种子、预算和审批。
组合设置由实验agent提出并经三方审核；宿主执行其中的起点选择规则、步骤顺序和范围。
next_step 指定当前已测控制、方法、因素、完整网格和待测值；只选择一个待测值提交 search_parameters。
先用 hyperparameters.read 获取精确控制，用 revise 生成只改该因素的候选 YAML，逐批独立投票后运行。
同一因素的各候选固定同一个完整控制组合。该因素全部核验后才把严格更优的组合作为下一因素控制。
平分保留控制，已完成的精确配置复用。单因素结论不能代替组合实测，不能把未测组合直接宣布最佳。
组合扫描完成后，读取 steps、selected_controls 和 final_pair，独立判断训练诊断和原稳定性判据。
complete_optimization 只能选 final_pair 对应的已有同种子批次；组合覆盖本身不等于科学验收。
'''


def register(store, run_id, instruction):
    run = store.get('runs', run_id) or {}
    if run.get('status') not in {'draft', 'paused', 'blocked'} or not str(instruction).strip():
        raise ValueError('Combination validation registration requires an idle task and an instruction')
    body = {'run_id': run_id, 'version': 1, 'passes': 1, 'instruction': instruction,
            'execution_authorized': False}
    row = {'id': run_id, 'run_id': run_id, 'body': body, 'sha256': sha(body), 'created_at': now()}
    return store.mutate(REQUESTS, run_id, lambda old: old or row)


def _enabled(store, run_id):
    request = store.get(REQUESTS, run_id)
    if request:
        if request.get('run_id') != run_id or sha(request.get('body')) != request.get('sha256'):
            raise ValueError('Combination validation instruction changed')
        return True
    run = store.get('runs', run_id) or {}
    state = run.get('state', {})
    first = state.get('agent_bayesian_batch_from_cycle')
    return (run.get('config', {}).get('combination_validation_protocol') in (1, 2, 3)
            or type(first) is int and state.get('research_cycle', 1) >= first)


def _version(store, run_id, evidence):
    """Retain the algorithm in an existing scope or supplemental settings request."""
    from .combination_settings import key, PLANNER
    from .bayesian_policy import POLICY
    scope = evidence.get('tuning_scope') or {}
    settings = (scope.get('plan') or {}).get('combination_settings')
    if settings:
        if settings.get('planner') == POLICY:
            return 3
        return 2 if settings.get('planner') == PLANNER else 1
    if scope.get('plan_sha256'):
        saved = store.get('combination_validation_settings', key(run_id, evidence)) or {}
        if saved.get('rounds'):
            schema = saved['rounds'][0]['author']['schema']['properties']['settings']
            if 'selection_script' in schema['properties']:
                return 3
            return 2 if 'planner' in schema['properties'] else 1
    run = store.get('runs', run_id) or {}
    state = run.get('state', {})
    first = state.get('agent_bayesian_batch_from_cycle')
    if run.get('config', {}).get('combination_validation_protocol') == 3 or (
            type(first) is int and state.get('research_cycle', 1) >= first):
        return 3
    return 2 if run.get('config', {}).get('combination_validation_protocol') == 2 else 1


def _key(run_id, evidence):
    return run_id + ':combinations:' + sha([evidence['binding'], evidence['tuning_scope']['plan_sha256']])


def _controls(evidence, seed, task_id, metric, direction):
    """Only complete paired units with exact current validation records qualify."""
    result = {}
    records = {r['id']: r for r in evidence['records']}
    for unit in evidence['units']:
        comp = evidence['comparisons'].get(unit['id'], {})
        from .paper_baselines import control_available
        own_only = evidence.get('baseline_mode') == 'paper_reported'
        variants = ('proposed',) if own_only else ('baseline', 'proposed')
        pairs = [p for p in comp.get('own_observations' if own_only else 'pairs', []) if p['seed'] == seed]
        if (not unit.get('complete') or unit.get('kind') not in {'pilot', 'grid'}
                or not control_available(comp) or comp.get('task_id') != task_id or len(pairs) != 1
                or comp.get('metric') != metric or comp.get('direction') != direction):
            continue
        selected = {}
        for variant in variants:
            params = [evidence['job_parameters'][n] for n in unit['jobs']
                      if evidence['job_parameters'][n].get('seed') == seed
                      and evidence['job_parameters'][n].get('variant') == variant]
            trial = records.get(pairs[0].get(variant + '_trial_id'), {})
            score = pairs[0].get(variant)
            if (len(params) != 1 or trial.get('status') != 'completed' or trial.get('superseded')
                    or trial.get('evaluation_errors') or trial.get('params_complete') is False
                    or params[0].get('phase') != 'validation' or sha(trial.get('params')) != sha(params[0])
                    or type(score) not in (int, float) or not math.isfinite(score)):
                break
            selected[variant] = {'params': params[0], 'score': score, 'trial_id': trial['id']}
        if len(selected) == len(variants):
            result[unit['id']] = selected
    return result


def _best(controls, variant, direction, preferred=None):
    if not controls:
        raise ValueError('Combination validation requires completed same-seed controls')
    # Stable insertion order is the frozen unit order at setup, or the agent's
    # grid order when comparing candidates. Ties never depend on hash spelling.
    ordered = sorted(controls, key=lambda key: key != preferred)
    choose = max if direction == 'maximize' else min
    return choose(ordered, key=lambda key: controls[key][variant]['score'])


def _plan(evidence):
    scope = evidence['tuning_scope']
    if sha(scope['plan']) != scope['plan_sha256']:
        raise ValueError('Combination scope plan changed')
    criteria = evidence['dataset_optimization']['architecture_certificate']['body']['assessment']['criteria']
    reference = evidence['comparisons'][criteria['reference_unit_id']]
    from .combination_settings import validate
    settings = evidence[KEY]['settings']
    validate(settings, scope['plan']['factors'], evidence)
    seed = settings['validation_seed']
    controls = _controls(evidence, seed, reference['task_id'], reference['metric'], reference['direction'])
    factors = [copy.deepcopy(next(f for f in scope['plan']['factors']
               if f['path'] == step['path'] and f['variant'] == step['variant'])) for step in settings['steps']]
    version = evidence[KEY]['version']
    initial = {}
    for variant in (('proposed',) if evidence.get('baseline_mode') == 'paper_reported' else ('baseline','proposed')):
        eligible = controls
        if version in (2, 3):
            from .tuning_scope import configured_value, value_key
            domain = [(f['path'], {value_key(v) for v in f['values']}) for f in factors
                      if f['variant'] in ('shared', variant)]
            eligible = {name: pair for name, pair in controls.items() if all(
                value_key(configured_value(pair[variant]['params'], path, evidence)) in values
                for path, values in domain)}
        initial[variant] = _best(eligible, variant, reference['direction'], criteria['reference_unit_id'])
    return {'version': version, 'binding': evidence['binding'], 'scope_sha256': scope['plan_sha256'],
            'validation_seed': seed, 'task_id': reference['task_id'], 'metric': reference['metric'],
            'direction': reference['direction'], 'factors': factors,
            **({'planner': settings['planner']} if version in (2, 3) else {'passes': settings['passes']}),
            **({'selection_script': settings['selection_script']} if version == 3 else {}),
            'settings_sha256': sha(settings),
            'initial_unit_ids': sorted(controls),
            'initial_controls': initial,
            'max_new_trials': settings['max_new_trials'],
            'execution_authorized': False}


def _progress(plan, evidence):
    from .tuning_scope import configured_value, value_key
    from .tuning_candidates import changed
    controls = _controls(evidence, plan['validation_seed'], plan['task_id'], plan['metric'], plan['direction'])
    anchors = dict(plan['initial_controls'])
    steps = []
    for factor in plan['factors']:
        variant = factor['variant']
        scoring = 'baseline' if variant == 'baseline' else 'proposed'
        base_id = anchors[scoring]
        base = controls[base_id]
        control = configured_value(base[scoring]['params'], factor['path'], evidence)
        values = [control if value_key(v) == value_key(control) else v for v in factor['values']]
        if value_key(control) not in {value_key(v) for v in values}:
            raise ValueError('Combination control is outside the approved grid')
        spec = {'base_unit_id': base_id, 'factor_path': factor['path'], 'variant': variant,
                'control_value': control, 'validation_seed': plan['validation_seed'], 'grid_values': values,
                'scope_sha256': plan['scope_sha256']}
        points, candidates = [], {base_id: base}
        for value in values:
            target = changed(base[scoring]['params'], spec, value)
            matches = {key: item for key, item in controls.items() if sha(item[scoring]['params']) == sha(target)}
            if variant == 'shared':
                other = changed(base['baseline']['params'], spec, value)
                matches = {key: item for key, item in matches.items() if sha(item['baseline']['params']) == sha(other)}
            chosen = _best(matches, scoring, plan['direction'], base_id) if matches else None
            points.append({'value': value, 'status': ('reused' if chosen in plan['initial_unit_ids'] else 'completed')
                           if chosen else 'awaiting_review',
                'unit_id': chosen, 'score': matches[chosen][scoring]['score'] if chosen else None,
                'trial_id': matches[chosen][scoring]['trial_id'] if chosen else None})
            if chosen:
                candidates[chosen] = matches[chosen]
        pending = [p['value'] for p in points if p['unit_id'] is None]
        best = _best(candidates, scoring, plan['direction'], base_id)
        steps.append({'index': len(steps), 'variant': variant, 'factor_path': factor['path'],
            'base_unit_id': base_id, 'control_value': control, 'points': points,
            'status': 'complete' if not pending else 'pending', 'best_unit_id': best})
        if pending:
            numeric = factor['value_kind'] in {'integer', 'number'}
            return {'status': 'validating_combinations', 'steps': steps,
                'completed_steps': len(steps) - 1, 'total_steps': len(plan['factors']),
                'next_step': {**spec, 'pending_values': pending,
                    'lower_bound': min(values) if numeric else None, 'upper_bound': max(values) if numeric else None},
                'selected_controls': anchors}
        anchors[scoring] = best
        if variant == 'shared':
            previous = anchors['baseline']
            baseline_candidates = {previous: controls[previous], **candidates}
            anchors['baseline'] = _best(baseline_candidates, 'baseline', plan['direction'], previous)
            steps[-1]['baseline_best_unit_id'] = anchors['baseline']
    final = [key for key, pair in controls.items() if all(
        sha(pair[v]['params']) == sha(controls[anchors[v]][v]['params']) for v in anchors)]
    return {'status': 'complete', 'steps': steps, 'completed_steps': len(steps), 'total_steps': len(steps),
            'next_step': None, 'selected_controls': anchors,
            'final_pair': {'baseline_unit_id': anchors['baseline'], 'proposed_unit_id': anchors['proposed'],
                           'existing_unit_id': sorted(final)[0] if final else None}}


def _final_pair(store, run_id, evidence, state):
    """Pair the two selected saved records without scheduling either again."""
    pair = state.get('final_pair')
    if not pair:
        return
    controls = _controls(evidence, state['validation_seed'], state['task_id'], state['metric'], state['direction'])
    variants = ('proposed',) if evidence.get('baseline_mode') == 'paper_reported' else ('baseline','proposed')
    chosen = {v: controls[pair[v + '_unit_id']][v] for v in variants}
    ids = [chosen[v]['trial_id'] for v in variants]
    pair['trial_ids'] = ids
    if pair['existing_unit_id']:
        pair['unit_id'] = pair['existing_unit_id']
        return
    from .research_cycle import validation_summary
    from .experiment_decisions import _safe_rows
    from .experiment_workflow import validation_records
    # The agent snapshot omits split materialization and legacy test-split
    # fingerprints. Use the same host projections as ordinary paired units.
    frozen = {row['id']: row for row in evidence['records']}
    with store.read_snapshot() as reader:
        originals = validation_records(reader, run_id, trial_ids=ids)
    by_id = {row['id']: row for row in originals}
    if set(by_id) != set(ids):
        raise ValueError(f'Selected combination validation records are missing: {sorted(set(ids) - by_id.keys())}')
    rows = [by_id[tid] for tid in ids]
    if (sha(_safe_rows(rows)) != sha([frozen[tid] for tid in ids])
            or any(row.get('evaluation_errors') or not row.get('evaluation_errors_complete') for row in rows)):
        raise ValueError(f'Selected combination records changed or have invalid evaluation receipts: {ids}')
    comparison = validation_summary(rows, state['metric'], state['direction'])
    if not comparison.get('comparable'):
        issues = ', '.join(issue['code'] for issue in comparison['issues'])
        raise ValueError(f'Selected combination records cannot form an exact same-task pair: {issues}; trials={ids}')
    name = 'combination-pair-' + sha([evidence['binding'], state['plan_sha256'], ids])[:24]
    jobs = [next(n for n, p in evidence['job_parameters'].items() if sha(p) == sha(row['params'])) for row in rows]
    pair.update(unit_id=name, provenance='reused_completed_validation_records')
    evidence['units'].append({'id': name, 'kind': 'grid', 'task_id': state['task_id'],
        'jobs': jobs, 'complete': True, 'available': False, 'states': ['completed', 'completed'],
        'params_sha256': [sha(r['params']) for r in rows], 'reused_combination_pair': True})
    evidence['comparisons'][name] = {**{k: comparison.get(k) for k in (
        'metric', 'direction', 'n_pairs', 'pairs', 'paired_seeds', 'baseline_score', 'proposed_score',
        'relative_improvement', 'issues')}, 'comparable': True, 'full_seed_group': True,
        'task_id': state['task_id'], 'scope': 'Comparison of exact saved combination records; not a new execution.'}


def attach(engine, run_id, evidence):
    if not _enabled(engine.store, run_id):
        return
    virtual = {u['id'] for u in evidence['units'] if u.get('reused_combination_pair')}
    if virtual:
        evidence['units'] = [u for u in evidence['units'] if u['id'] not in virtual]
        for identifier in virtual:
            evidence['comparisons'].pop(identifier, None)
    version = _version(engine.store, run_id, evidence)
    common = {'required': True, 'version': version, 'execution_authorized': False,
              'scientific_approval': False}
    if version == 1:
        common['passes'] = 1
    if not evidence.get('dataset_optimization', {}).get('architecture_certificate'):
        evidence[KEY] = {**common, 'status': 'waiting_architecture', 'next_step': None}
        return
    scope = evidence.get('tuning_scope') or {}
    if scope.get('status') != 'accepted':
        evidence[KEY] = {**common, 'status': 'waiting_scope', 'next_step': None}
        return
    if not any(f['disposition'] == 'search' for f in scope['plan']['factors']):
        evidence[KEY] = {**common, 'status': 'complete', 'steps': [], 'next_step': None}
        return
    common['available_factors'] = [{k: f[k] for k in ('path', 'variant', 'values', 'value_kind', 'priority', 'rationale')}
                                   for f in scope['plan']['factors'] if f['disposition'] == 'search']
    from .combination_settings import verified
    evidence[KEY] = common
    settings = verified(engine, run_id, evidence)
    if settings is None:
        evidence[KEY] = {**common, 'status': 'settings_required', 'next_step': None}
        return
    common['settings'] = settings
    next_step = None
    if version == 2:
        from .scripted_tuning import grid_schedule
        scope['coverage'], next_step = grid_schedule(evidence, settings['validation_seed'])
    if version != 3 and not all(c['complete'] for c in scope.get('coverage', [])):
        evidence[KEY] = {**common, 'status': 'waiting_single_factor', 'next_step': next_step}
        return
    record = engine.store.get(PLANS, _key(run_id, evidence))
    if not record:
        evidence[KEY] = {**common, 'status': 'plan_required', 'next_step': None}
        return
    body = record['body']
    source = engine.store.get('experiment_evidence', body['source_evidence']['id']) or {}
    original = source.get('summary')
    if (record.get('run_id') != run_id or sha(body) != record.get('sha256')
            or source.get('run_id') != run_id or sha(original) != body['source_evidence']['sha256']
            or _plan(original) != body['plan'] or body['plan']['binding'] != evidence['binding']
            or body['plan']['scope_sha256'] != scope['plan_sha256']):
        raise ValueError('Combination validation plan or original controls changed')
    if version in (2, 3):
        from .scripted_tuning import bayesian_progress
        plan = body['plan']
        selection = (plan['validation_seed'], plan['task_id'], plan['metric'], plan['direction'])
        controls = _controls(evidence, *selection)
        if any(controls.get(k) != v for k, v in _controls(original, *selection).items()):
            raise ValueError('Bayesian search lost an original measured validation control')
        progress = bayesian_progress(plan, evidence, record['sha256'], controls)
    else:
        progress = _progress(body['plan'], evidence)
    evidence[KEY] = {**common, 'plan_id': record['id'], 'plan_sha256': record['sha256'],
        'task_id': body['plan']['task_id'], 'metric': body['plan']['metric'], 'direction': body['plan']['direction'],
        'validation_seed': body['plan']['validation_seed'], 'max_new_trials': body['plan']['max_new_trials'],
        **progress}
    _final_pair(engine.store, run_id, evidence, evidence[KEY])


def prepare(engine, run_id, evidence):
    if evidence.get(KEY, {}).get('status') != 'plan_required':
        return
    from .experiment_decisions import _archive
    body = {'source_evidence': _archive(engine, run_id, evidence), 'plan': _plan(evidence)}
    key = _key(run_id, evidence)
    row = {'id': key, 'run_id': run_id, 'body': body, 'sha256': sha(body), 'created_at': now()}
    engine.check_active(run_id)
    engine.store.mutate(PLANS, key, lambda old: old or row)
    attach(engine, run_id, evidence)


def validate_search(spec, evidence):
    progress = evidence.get(KEY) or {}
    scripted = progress.get('version') in (2, 3)
    if not scripted and progress.get('status') not in {'validating_combinations', 'complete'}:
        return
    step = progress.get('next_step')
    if not step:
        if scripted:
            raise ValueError('The tuning script has no admissible point for this frozen evidence')
        raise ValueError('The bounded combination sweep is complete; independently assess stability')
    from .tuning_scope import value_key
    if any(spec.get(k) != step[k] for k in ('base_unit_id', 'factor_path', 'variant', 'validation_seed', 'scope_sha256')):
        raise ValueError('Combination candidate must use the current measured control and factor')
    if value_key(spec.get('value')) not in {value_key(v) for v in step['pending_values']}:
        raise ValueError('Reuse the measured combination instead of repeating training')
    if scripted and any(spec.get(k) != step[k] for k in ('grid_values', 'lower_bound', 'upper_bound')):
        raise ValueError('The script-selected grid and bounds are frozen')


def require_complete(evidence):
    state = evidence.get(KEY)
    if state and state['status'] != 'complete':
        raise ValueError('Complete the bounded same-seed combination validation before stability approval')


def stability_scores(evidence, selected_id, reference_id):
    state = evidence.get(KEY) or {}
    pair = state.get('final_pair') or {}
    if state.get('status') != 'complete' or not pair:
        return None
    if selected_id != pair['unit_id']:
        raise ValueError('Stability selection must use the measured final combination pair')
    from .parameter_grid import same_seed_score
    control = same_seed_score(evidence['comparisons'].get(reference_id, {}), state['validation_seed'])
    selected = same_seed_score(evidence['comparisons'].get(selected_id, {}), state['validation_seed'])
    if control is None or selected is None:
        raise ValueError('Combination stability comparison requires the same fixed seed')
    return selected['proposed_score'], control['proposed_score']
