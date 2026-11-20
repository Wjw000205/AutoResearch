"""Display saved tuning points across dataset transitions, without changing the run."""
from collections import OrderedDict, defaultdict
from copy import deepcopy
import math
from threading import Lock

from .experiment_decisions import _approved
from .experiment_workflow import sha
from .parameter_grid import _saved_grid_rows
from .research_cycle import validation_summary


_cache = OrderedDict()
_mutex = Lock()


def view(store, run_id, trials, truncated):
    collections = ('dataset_configuration_queues', 'parameter_search_batches', 'experiment_decisions',
                   'combination_validation_plans', 'dataset_bayesian_scripts', 'bayesian_search_results')
    with store._read_db() as db:
        heads = tuple(tuple(row) for row in db.execute(
            'SELECT collection,key,updated_at FROM records WHERE collection IN (' + ','.join('?' for _ in collections)
            + ') AND key>=? AND key<? ORDER BY collection,key', (*collections, run_id+':', run_id+';')))
    queue = (store.get('runs', run_id) or {}).get('state', {}).get('dataset_configuration_queue')
    key = (str(store.path.resolve()), run_id, queue, truncated, heads,
           tuple((t['id'], t['source_updated_at']) for t in trials))
    with _mutex:
        if key in _cache:
            _cache.move_to_end(key)
            return deepcopy(_cache[key])
    result = _build(store, run_id, trials, truncated)
    with _mutex:
        _cache[key] = result
        while len(_cache) > 8:
            _cache.popitem(last=False)
    return deepcopy(result)


def _build(store, run_id, trials, truncated):
    run = store.get('runs', run_id) or {}
    state = run.get('state', {})
    queue = store.get('dataset_configuration_queues', state.get('dataset_configuration_queue', '')) or {}
    batches = queue.get('batches', [])
    by_cycle = {b['cycle']: b['dataset'] for b in batches}
    active = batches[-1]['dataset'] if batches and queue.get('status') == 'active' else None
    datasets = {}

    def dataset(name):
        return datasets.setdefault(name, {'name': name, 'status': None, 'grids': [], 'comparisons': [], 'selections': [],
                                           'incomplete': truncated, 'batch_count': 0})

    for target in queue.get('body', {}).get('targets', []):
        dataset(target['dataset'])
    for item in batches:
        dataset(item['dataset'])['status'] = item['status']
    grids = defaultdict(lambda: defaultdict(list))
    by_receipt = {r['id']: r for r in trials}
    matched = defaultdict(list)
    for trial in trials:
        cycle = trial.get('research_cycle') or 1
        trial_name = trial.get('params', {}).get('dataset') or by_cycle.get(cycle)
        dataset(trial_name if isinstance(trial_name, str) else f'未标注数据集 · 第 {cycle} 轮')
        if trial.get('params_complete') is not False and not trial.get('superseded'):
            matched[((trial.get('research_cycle') or 1), trial.get('code_sha256'), trial.get('experiment_plan_sha256'),
                     trial.get('code_revision'), trial.get('bundle_sha256'), sha(trial.get('params', {})))].append(trial)
    plans = {p['sha256']: p for p in store.list('combination_validation_plans', run_id)}
    cohorts = {}

    def add_points(name, binding, task, seed, metric, direction, rows, scores, factor, variant):
        if direction not in ('maximize', 'minimize') or truncated or not metric or type(seed) is not int:
            return
        for row in rows:
            params = row['params']
            method = params.get('variant')
            score = scores.get(str(method) + '_score')
            ev = row.get('evaluation') or {}
            if (method not in ('baseline', 'proposed') or params.get('phase') != 'validation'
                    or params.get('seed') != seed or row.get('status') != 'completed' or row.get('superseded')
                    or row.get('evaluation_errors') or not row.get('evaluation_errors_complete', True)
                    or not row.get('params_complete', True) or not row.get('metrics_complete', True)
                    or type(score) not in (float, int) or not math.isfinite(score)):
                continue
            key = sha([name, binding, task, seed, metric, direction, ev.get('dataset_id'),
                       ev.get('split_fingerprints'), ev.get('tuning_budget'), row.get('provenance')])
            if key not in cohorts:
                group = {'id': key, 'cycle': binding['cycle'], 'revision': binding['revision'], 'task': task,
                         'seed': seed, 'metric': metric, 'direction': direction, 'provenance': row.get('provenance'),
                         'paths': {'baseline': [], 'proposed': []}, 'points': {}}
                cohorts[key] = group
                dataset(name)['comparisons'].append(group)
            group = cohorts[key]
            if factor and variant in (None, 'shared', method) and factor not in group['paths'][method]:
                group['paths'][method].append(factor)
            group['points'][row['id']] = {'trial_id': row['id'], 'variant': method, 'score': score}

    for record in store.list('parameter_search_batches', run_id):
        body = record['body']
        unit, binding = body['unit'], body['binding']
        params = [p for _, p in unit['jobs']]
        labels = {p['dataset'] for p in params if isinstance(p.get('dataset'), str) and p['dataset']}
        if len(labels) > 1:
            continue
        name = next(iter(labels), None) or by_cycle.get(binding['cycle']) or f"未标注数据集 · 第 {binding['cycle']} 轮"
        section = dataset(name)
        section['batch_count'] += 1
        grid = unit.get('parameter_grid')
        if record['sha256'] != sha(body) or grid and grid['binding'] != binding:
            section['incomplete'] = True
            continue
        if 'script_admission' in body:
            proof = body['script_admission']
            script = store.get('dataset_bayesian_scripts', proof['script_id']) or {}
            plan = plans.get(unit.get('bayesian_plan_sha256'), {})
            if (plan.get('id') != proof['plan_id'] or script.get('run_id') != run_id
                    or script.get('sha256') != sha(script.get('body')) or script['body']['binding'] != binding):
                section['incomplete'] = True
                continue
        else:
            decision = store.get('experiment_decisions', body['decision_id']) or {}
            if (decision.get('run_id') != run_id or not _approved(decision)
                    or decision['proposal_sha256'] != body['proposal_sha256']):
                section['incomplete'] = True
                continue
        if grid:
            grids[name][grid['id']].append(body)
            continue
        plan = plans.get(unit.get('bayesian_plan_sha256'))
        if not plan or sha(plan['body']) != plan['sha256'] or plan['body']['plan']['binding'] != binding:
            continue
        spec = plan['body']['plan']
        stage = [matched.get((binding['cycle'], binding['code_sha256'], binding['plan_sha256'],
                              binding['revision'], binding.get('bundle_sha256'), sha(p)), []) for p in params]
        if len(stage) == 2 and all(len(hit) == 1 for hit in stage):
            rows = [hit[0] for hit in stage]
            from .parameter_grid import same_seed_score
            scores = same_seed_score(validation_summary(rows, spec['metric'], spec['direction']), spec['validation_seed'])
            if scores:
                for factor in spec['factors']:
                    add_points(name, binding, unit['task_id'], spec['validation_seed'], spec['metric'], spec['direction'],
                               rows, scores, factor['path'], factor['variant'])
    for name, groups in grids.items():
        section = datasets[name]
        projection = _saved_grid_rows({'grids': []}, groups, trials, section['incomplete'])
        section['grids'] = projection['grids']
        for grid in section['grids']:
            for point in grid['points']:
                if point['status'] in ('completed', 'reused'):
                    add_points(name, grid['binding'], grid['task_id'], grid['validation_seed'], grid['metric'],
                               grid['direction'], [by_receipt[j] for j in point['jobs'] if j in by_receipt],
                               point, grid['factor_path'], grid.get('variant'))
    # Persisted Bayesian selections also survive pagination of the experiment list.
    for record in store.list('bayesian_search_results', run_id):
        body = record.get('body', {})
        plan = next((p for p in plans.values() if p['id'] == body.get('plan_id')), None)
        script = store.get('dataset_bayesian_scripts', body.get('script_id', '')) or {}
        if (record.get('sha256') != sha(body) or not plan or plan['sha256'] != sha(plan['body'])
                or script.get('run_id') != run_id or script.get('sha256') != sha(script.get('body'))
                or script['body']['binding'] != plan['body']['plan']['binding']):
            continue
        section = dataset(script['body']['dataset'])
        scores, params = body.get('validation_scores', {}), body.get('parameters', {})
        ids = body.get('pair', {}).get('trial_ids', [])
        if (len(ids) != 2 or len(set(ids)) != 2 or any(type(scores.get(v)) not in (int, float)
                or not math.isfinite(scores[v]) or params.get(v, {}).get('variant') != v
                or params[v].get('phase') != 'validation' or params[v].get('seed') != body.get('validation_seed')
                for v in ('baseline', 'proposed'))):
            continue
        selection = []
        for variant, tid in zip(('baseline', 'proposed'), ids):
            row = by_receipt.get(tid)
            if not row:
                from .experiment_views import _projection
                with store._read_db() as db:
                    head = db.execute("SELECT key,updated_at FROM records WHERE collection='experiments' AND key=?",
                                      (run_id + ':' + tid,)).fetchone()
                row = _projection(store, *head) if head else None
            binding = script['body']['binding']
            if (not row or row.get('superseded') or row['status'] != 'completed' or row.get('evaluation_errors')
                    or not row['params_complete'] or not row['evaluation_errors_complete']
                    or row['params'] != params[variant] or row['code_sha256'] != binding['code_sha256']
                    or row['research_cycle'] != binding['cycle'] or row['experiment_plan_sha256'] != binding['plan_sha256']
                    or row['metrics'].get(body['metric']) != scores[variant]):
                break
            selection.append({'variant': variant, 'trial_id': tid, 'score': scores[variant],
                              'params': params[variant]})
        if len(selection) == 2:
            section['selections'].append({'id': plan['id'], 'metric': body['metric'], 'direction': body['direction'],
                'seed': body['validation_seed'], 'winners': selection, 'cycle': binding['cycle'], 'revision': binding['revision'],
                'paths': {v: [f['path'] for f in plan['body']['plan']['factors'] if f['variant'] in ('shared', v)]
                          for v in ('baseline', 'proposed')}})
    for section in datasets.values():
        if section['incomplete']:
            section['comparisons'] = []
            continue
        for group in section['comparisons']:
            group['points'] = sorted(group['points'].values(), key=lambda p: (
                p['variant'], p['score'] if group['direction'] == 'minimize' else -p['score'], p['trial_id']))
            first = {}
            for point in group['points']:
                point['best'] = point['score'] == first.setdefault(point['variant'], point['score'])
    return {'active_dataset': active, 'datasets': list(datasets.values())}
