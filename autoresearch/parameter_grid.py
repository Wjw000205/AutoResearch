"""Finite single-factor grids over an immutable paired validation control.

This module describes candidates and reuses measured receipts. It never launches
jobs, prunes a grid from test scores, or grants execution/scientific approval.
"""
import copy
import math

from .experiment_workflow import sha

PROTOCOL = 2
VALUES = {'type': 'array', 'items': {'type': 'number'}, 'minItems': 2,
          'maxItems': 9, 'uniqueItems': True}
GUIDANCE = '''
参数微调使用有界单因素网格，先在 parameter_search.grid_values 声明完整离散取值（含原对照值，
最多9个值），填写 grid_rationale 解释取值与间距依据。value 只选择本轮待评审的一个新值；
其余点只是计划，不获得执行授权。每个点保持同一 validation_seed，baseline/proposed 同步修改。
先复用相同代码、数据、配置、种子的已有结果；不能把种子重复、空网格或日志补修算成调参。
parameter_grids 提供取值、复用/待评审/部分完成/失败状态、剩余调用数及同种子验证比较。
同一对照、参数路径和种子的网格一旦批准不可暗改；需要新范围时先完成当前分析，再提出新的对照计划。
每次只独立评审一个成对批次，执行后复核；预算不足、执行失败或证据改变留在当前阶段。
选参依据固定验证主指标与方向，必须与同种子控制比较；不得将单种子分数与旧多种子均值直接排名。
平分优先保留原对照，其次按预声明网格顺序。未执行点明确保留未执行；覆盖网格不等于参数稳定。
提前结束探索须在稳定性评审中解释未执行点、实际对照与停止依据，不能自动跳到下一数据集或测试集。
'''


def definition(spec, evidence, control_value, task_id):
    values = spec.get('grid_values')
    if values is None:
        if evidence.get('parameter_search_protocol', 1) >= PROTOCOL:
            raise ValueError('New parameter search requires a predeclared finite grid_values')
        return None
    if type(spec.get('validation_seed')) is not int:
        raise ValueError('A new grid requires one fixed validation_seed')
    if (not 2 <= len(values) <= 9 or
            any(type(v) not in (int, float) or not math.isfinite(v) for v in values) or
            len(set(values)) != len(values)):
        raise ValueError('Grid values must be 2 to 9 distinct finite scalars')
    if control_value not in values or spec['value'] not in values:
        raise ValueError('Grid must include both the existing control and selected candidate')
    if any(not spec['lower_bound'] <= v <= spec['upper_bound'] for v in values):
        raise ValueError('Every grid value must be inside the predeclared bounds')
    if spec['factor_path'].split('/')[-1] != 'weight_decay' and any(v <= 0 for v in values):
        raise ValueError('Learning-rate and clipping grid values must be positive')
    if not str(spec.get('grid_rationale', '')).strip():
        raise ValueError('Grid values and spacing require an evidence-based rationale')
    comparison = evidence['comparisons'][spec['base_unit_id']]
    body = {k: copy.deepcopy(spec[k]) for k in (
        'base_unit_id', 'factor_path', 'validation_seed', 'grid_values',
        'lower_bound', 'upper_bound', 'grid_rationale')}
    # Preserve the original control's exact params hash (e.g. weight_decay=0),
    # while treating JSON 1 and 1.0 as the same new optimizer value.
    body['grid_values'] = [control_value if v == control_value else float(v) for v in values]
    body.update(binding=copy.deepcopy(evidence['binding']), control_value=control_value,
                task_id=task_id, metric=comparison['metric'], direction=comparison['direction'],
                selection_rule='best_same_seed_proposed_validation_control_first_on_tie')
    result = {'id': 'parameter-grid-' + sha(body)[:24], **body}
    for unit in evidence['units']:
        old = unit.get('parameter_grid')
        if old and old['task_id'] == task_id and old['validation_seed'] != result['validation_seed']:
            raise ValueError('Keep the same control seed across the current task parameter search')
        if old and all(old[k] == result[k] for k in ('base_unit_id', 'factor_path', 'validation_seed')):
            if old != result:
                raise ValueError('Previously reviewed grid is immutable; reuse its exact definition')
    return result


def _params_at(evidence, grid, value):
    base = next(u for u in evidence['units'] if u['id'] == grid['base_unit_id'])
    result = []
    for name in base['jobs']:
        params = evidence['job_parameters'][name]
        if params['seed'] != grid['validation_seed']:
            continue
        if grid.get('scope_sha256'):
            from .tuning_candidates import changed
            result.append(changed(params, grid, value))
            continue
        candidate = copy.deepcopy(params)
        target = candidate
        parts = grid['factor_path'].strip('/').split('/')
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]] = value
        result.append(candidate)
    return result


def same_seed_score(comparison, seed):
    pairs = [p for p in comparison.get('pairs', []) if p['seed'] == seed]
    if not comparison.get('comparable') or len(pairs) != 1:
        return None
    pair = pairs[0]
    if any(type(pair.get(k)) not in (int, float) or not math.isfinite(pair[k])
           for k in ('baseline', 'proposed')):
        return None
    delta = pair['baseline'] - pair['proposed']
    if comparison['direction'] == 'maximize':
        delta = -delta
    return {'baseline_score': pair['baseline'], 'proposed_score': pair['proposed'],
            'relative_improvement': delta / abs(pair['baseline']) if pair['baseline'] else None}


def progress(evidence, grid):
    by_params = {}
    for name, params in evidence['job_parameters'].items():
        by_params.setdefault(sha(params), []).append(name)
    states = {j['name']: j['state'] for j in evidence['ledger']['jobs']}
    points = []
    for value in grid['grid_values']:
        matches = [by_params.get(sha(p), []) for p in _params_at(evidence, grid, value)]
        names = [m[0] for m in matches if len(m) == 1]
        statuses = [states.get(n, 'unknown') for n in names]
        point = {'value': value, 'control': value == grid['control_value'], 'jobs': names,
                 'new_trials_needed': sum(not m for m in matches), 'unit_id': None}
        if any(len(m) > 1 for m in matches):
            point['status'] = 'unavailable'
        elif len(names) == 2 and statuses == ['completed', 'completed']:
            point['status'] = 'reused' if point['control'] else 'completed'
        elif any(s not in {'missing', 'completed', 'running'} for s in statuses):
            point['status'] = 'blocked'
        elif 'running' in statuses:
            point['status'] = 'running'
        elif 'completed' in statuses:
            point['status'] = 'partial'
        else:
            point['status'] = 'approved_pending' if names else 'awaiting_review'
        if len(names) == 2:
            for unit in evidence['units']:
                if set(names) <= set(unit['jobs']):
                    point['unit_id'] = unit['id']
                    score = same_seed_score(evidence['comparisons'].get(unit['id'], {}),
                                            grid['validation_seed'])
                    if score and point['status'] in {'reused', 'completed'}:
                        point.update(score)
                    break
        points.append(point)
    scored = [p for p in points if 'proposed_score' in p]
    # Stable tie-breaking: control first, then the explicitly declared value order.
    preferred = sorted(scored, key=lambda p: not p['control'])
    score_key = 'baseline_score' if grid.get('variant') == 'baseline' else 'proposed_score'
    best = (min if grid['direction'] == 'minimize' else max)(
        preferred, key=lambda p: p[score_key]) if preferred else None
    control = next((p for p in scored if p['control']), None)
    for point in scored:
        if control and control[score_key]:
            delta = control[score_key] - point[score_key]
            point['improvement_vs_control'] = (
                delta if grid['direction'] == 'minimize' else -delta) / abs(control[score_key])
    return {**grid, 'points': points,
            'new_trials_needed': sum(p['new_trials_needed'] for p in points),
            'completed_new_points': sum(p['status'] == 'completed' for p in points),
            'best_measured_value': best['value'] if best else None,
            'best_measured_unit_id': best['unit_id'] if best else None,
            'complete': all(p['status'] in {'completed', 'reused'} for p in points),
            'scientific_approval': False, **({'selection_score': score_key} if grid.get('scope_sha256') else {})}


def summaries(evidence):
    grids = {u['parameter_grid']['id']: u['parameter_grid']
             for u in evidence['units'] if u.get('parameter_grid')}
    return [progress(evidence, grid) for grid in grids.values()]


def check_budget(evidence, grid):
    remaining = progress(evidence, grid)['new_trials_needed']
    budget = evidence.get('budget', {})
    limit = budget.get('trial_limit')
    # Reserve existing unfinished frozen work. This does not enlarge a budget.
    reserved = sum(j['state'] == 'missing' for j in evidence['ledger']['jobs'])
    if limit is not None and budget.get('attempts_used', 0) + remaining + reserved > limit:
        raise ValueError('Finite parameter grid exceeds remaining budget after reserving frozen work')


def reviewed_lineage(evidence, selected_id, reference_id):
    """Follow measured same-seed controls back to the immutable first reference.

    Catalog construction authenticates each grid's independent authorization.
    This traversal only connects existing measured control/candidate receipts;
    it never extends a grid or permits an unreviewed point.
    """
    grids = summaries(evidence)
    reference = evidence['comparisons'].get(reference_id, {})
    for seed in dict.fromkeys(g['validation_seed'] for g in grids):
        reached, linked = {reference_id}, []
        for _ in range(len(grids)):
            additions = [g for g in grids if g not in linked and g['base_unit_id'] in reached
                and g['validation_seed'] == seed and g['task_id'] == reference.get('task_id')
                and g['metric'] == reference.get('metric') and g['direction'] == reference.get('direction')
                and g['binding'] == evidence['binding']
                and any(p['control'] and p['unit_id'] == g['base_unit_id'] and 'proposed_score' in p
                        for p in g['points'])]
            if not additions:
                break
            linked.extend(additions)
            reached.update(p['unit_id'] for g in additions for p in g['points'] if 'proposed_score' in p)
        if any(p['unit_id'] == selected_id and 'proposed_score' in p for g in linked for p in g['points']):
            return linked
    return []


def stability_scores(evidence, selected_id, reference_id):
    """Never rank a new single-seed candidate against an old multi-seed mean."""
    grids = reviewed_lineage(evidence, selected_id, reference_id)
    if not grids:
        if any(u.get('parameter_grid') for u in evidence['units']):
            raise ValueError('Selection must belong to a reviewed same-seed parameter grid')
        return None  # Historical certificates retain their original contract.
    grid = grids[0]
    scored = list({p['unit_id']: p for g in reversed(grids) for p in g['points'] if 'proposed_score' in p}.values())
    control = next((p for p in scored if p['unit_id'] == reference_id), None)
    selected = next(p for p in scored if p['unit_id'] == selected_id)
    if not control:
        raise ValueError('Same-seed control measurement is unavailable')
    preferred = sorted(scored, key=lambda p: p['unit_id'] != reference_id)
    separate_baseline = any(g.get('scope_sha256') and g.get('variant') == 'baseline' for g in grids)
    best = (min if grid['direction'] == 'minimize' else max)(
        preferred, key=lambda p: (p['proposed_score'], p['baseline_score']) if separate_baseline else p['proposed_score'])
    if selected_id != best['unit_id']:
        raise ValueError('Stable selection must use the best measured same-seed grid candidate')
    return selected['proposed_score'], control['proposed_score']


def certified_selection(engine, run_id):
    """Carry the independently reviewed winner forward without reranking old means."""
    from .dataset_optimization import enabled, certificate
    from .experiment_decisions import _binding
    if not enabled(engine.store, run_id):
        return None
    _, binding = _binding(engine, run_id)
    saved = certificate(engine.store, run_id, binding, 'complete_optimization')
    if not saved:
        return None
    decision = engine.store.get('experiment_decisions', saved['body']['decision_id'])
    evidence = engine.store.get('experiment_evidence', decision['evidence']['id'])['summary']
    if not any(u.get('parameter_grid') for u in evidence['units']):
        return None  # Do not reinterpret historical selections.
    assessment = saved['body']['assessment']
    selected, reference = assessment['selected_unit_id'], assessment['criteria']['reference_unit_id']
    grids = reviewed_lineage(evidence, selected, reference)
    grid = grids[0]  # The certificate has already checked stability_scores.
    pairs = evidence['comparisons'][selected]['pairs']
    ids = [p[k] for p in pairs for k in ('baseline_trial_id', 'proposed_trial_id')]
    return {'certificate_id': saved['id'], 'unit_id': selected, 'task_id': grid['task_id'],
            'validation_seed': grid['validation_seed'], 'trial_ids': ids,
            'validation_seeds': [p['seed'] for p in pairs],
            'records': [r for r in evidence['records'] if r['id'] in ids]}


def selected_summary(trials, metric, direction, selection):
    """Use the exact approved pair; retain ordinary coverage/acceptance gates outside."""
    from .research_cycle import validation_summary
    rows = [r for r in trials if r['id'] in selection['trial_ids']]
    archived = {r['id']: r for r in selection['records']}
    expected = 2 * len(selection['validation_seeds'])
    if len(rows) != expected or len(archived) != expected:
        raise ValueError('Reviewed grid selection is missing its exact paired receipts')
    for row in rows:
        old = archived.get(row['id'])
        fields = ('params', 'code_revision', 'code_sha256', 'experiment_plan_sha256')
        if (not old or row.get('status') != 'completed' or row.get('superseded')
                or any(sha(row.get(k)) != sha(old.get(k)) for k in fields)
                or row.get('metrics', {}).get(metric) != old.get('metrics', {}).get(metric)
                or any(row.get('evaluation', {}).get('split_fingerprints', {}).get(k)
                       != old.get('evaluation', {}).get('split_fingerprints', {}).get(k)
                       for k in ('train', 'validation'))):
            raise ValueError('Reviewed grid selection evidence changed; reassessment is required')
    result = validation_summary(rows, metric, direction)
    if not result['comparable'] or set(result['paired_seeds']) != set(selection['validation_seeds']):
        raise ValueError('Reviewed grid selection no longer has a comparable fixed-seed pair')
    result['parameter_grid_selection'] = {k: v for k, v in selection.items() if k != 'records'}
    return result


def view(store, run_id, trials, *, truncated=False, plan=None):
    """Saved display only: no source validation or execution authorization."""
    from .parameter_search import COLLECTION
    from .experiment_decisions import _approved
    from .python_training import code_hash
    from .research_cycle import validation_summary
    result = {'protocol': PROTOCOL, 'grids': [], 'display_only': True,
              'new_plan_seed_policy': 'single', 'scientific_approval': False, 'status': 'not_planned'}
    records = [r for r in store.list(COLLECTION, run_id)
               if r['body']['unit'].get('parameter_grid')]
    if not records:
        return result
    run = store.get('runs', run_id) or {}
    program = store.get('programs', run_id) or (
        store.get('steps', run_id + ':implementation') or {}).get('output', {})
    cycle = run.get('state', {}).get('research_cycle', 1)
    # Avoid parsing an unbounded legacy plan merely to show a page.
    if plan is None:
        import json
        with store._read_db() as db:
            row = db.execute(
                "SELECT CASE WHEN length(data)<=262144 THEN data ELSE NULL END FROM records "
                "WHERE collection='experiment_plans' AND (key=? OR json_extract(data,'$.run_id')=?) "
                "AND coalesce(json_extract(data,'$.cycle'),1)=? ORDER BY updated_at DESC,key DESC LIMIT 1",
                (run_id, run_id, cycle)).fetchone()
        plan = json.loads(row[0]) if row and row[0] else None
    if not program.get('code') or plan is None:
        return {**result, 'status': 'unavailable', 'reason': '当前代码或计划绑定暂不可读取，暂不展示参数排名。'}
    current_code, current_plan = code_hash(program), sha(plan)
    groups = {}
    for record in records:
        body, binding = record['body'], record['body']['binding']
        if (binding['cycle'] != cycle or binding['revision'] != program.get('revision', 0)
                or binding['code_sha256'] != current_code or binding['plan_sha256'] != current_plan):
            continue
        grid = body['unit'].get('parameter_grid')
        if not grid:
            continue
        decision = store.get('experiment_decisions', body['decision_id']) or {}
        if (record['sha256'] != sha(body) or not _approved(decision)
                or decision['proposal_sha256'] != body['proposal_sha256']):
            return {**result, 'status': 'unavailable', 'reason': '参数网格批准记录待复核，暂不展示选参结论。'}
        groups.setdefault(grid['id'], []).append(body)
    for bodies in groups.values():
        grid, source = bodies[0]['unit']['parameter_grid'], bodies[0]['unit']['jobs']
        binding = grid['binding']
        evidence = {'units': [], 'job_parameters': {}, 'comparisons': {}, 'ledger': {'jobs': []}}
        from .tuning_scope import value_key, get
        from .tuning_candidates import changed
        parts = grid['factor_path'].strip('/').split('/')
        approved_values = {value_key(grid['control_value'])}
        approved_units = {}
        for body in bodies:
            params = next(p for _, p in body['unit']['jobs'] if grid.get('variant', 'shared') in ('shared', p['variant']))
            value = get(params, grid['factor_path'])
            approved_values.add(value_key(value))
            approved_units[value_key(value)] = body['unit']
        for value in grid['grid_values']:
            names, measured = [], []
            approved = approved_units.get(value_key(value))
            uid = (grid['base_unit_id'] if value == grid['control_value'] else
                   approved['id'] if approved else grid['id'] + ':' + str(value))
            for index, (_, saved_params) in enumerate(source):
                params = changed(saved_params, grid, value) if grid.get('scope_sha256') else copy.deepcopy(saved_params)
                if not grid.get('scope_sha256'):
                    target = params
                    for part in parts[:-1]:
                        target = target[part]
                    target[parts[-1]] = value
                found = [r for r in trials if not r.get('superseded')
                         and r.get('research_cycle', 1) == cycle and sha(r.get('params', {})) == sha(params)]
                if not found and value_key(value) not in approved_values:
                    continue
                name = (found[0]['id'] if len(found) == 1 else
                        approved['jobs'][index][0] if approved else uid + ':' + str(index))
                state = 'duplicate' if len(found) > 1 else 'missing'
                if len(found) == 1:
                    row = found[0]
                    state = row['status']
                    if state == 'completed' and (
                            row.get('code_revision') != binding['revision']
                            or row.get('code_sha256') != binding['code_sha256']
                            or row.get('experiment_plan_sha256') != binding['plan_sha256']
                            or row.get('bundle_sha256') != binding.get('bundle_sha256')
                            or row.get('evaluation_errors') or row.get('params_complete') is False):
                        state = 'invalid'
                    if state == 'completed':
                        measured.append(row)
                evidence['job_parameters'][name] = params
                evidence['ledger']['jobs'].append({'name': name, 'state': state})
                names.append(name)
            evidence['units'].append({'id': uid, 'jobs': names})
            if not truncated and len(measured) == 2:
                evidence['comparisons'][uid] = validation_summary(measured, grid['metric'], grid['direction'])
        result['grids'].append(progress(evidence, grid))
    result['status'] = 'unavailable' if truncated else 'saved' if result['grids'] else 'not_planned'
    if truncated:
        result['reason'] = '页面记录有截断；原始记录保留，暂不展示参数排名。'
    return result
