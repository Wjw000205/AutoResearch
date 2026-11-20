"""Agent-authored, independently reviewed settings for bounded combination checks."""

from .async_ownership import gather as gather_owned, cancel as cancel_owned
import copy
from . import independent_full_review as full

from .experiment_workflow import sha, encoded
from .training_adjustment import obj
from .storage import now
from . import bayesian_policy

COLLECTION = 'combination_validation_settings'
TEXT = {'type': 'string', 'minLength': 1}
IDENTITY = {'path': TEXT, 'variant': {'enum': ['baseline', 'proposed', 'shared']}, 'rationale': TEXT}
SCHEMA = obj({'validation_seed': {'type': 'integer', 'minimum': 0}, 'passes': {'const': 1},
    'initial_control_rule': {'const': 'best_measured_same_seed'},
    'steps': {'type': 'array', 'maxItems': 160, 'items': obj({
        **IDENTITY, 'grid_values': {'type': 'array', 'minItems': 2, 'maxItems': 9, 'items': {}}})},
    'excluded_factors': {'type': 'array', 'maxItems': 160, 'items': obj(IDENTITY)},
    'max_new_trials': {'type': 'integer', 'minimum': 0, 'maximum': 2560},
    'selection_rule': {'const': 'strict_improvement_control_first_on_tie'},
    'stopping_rule': TEXT, 'rationale': TEXT})
PLANNER = 'grid_then_bayesian_ei_v1'
SCRIPT_SCHEMA = copy.deepcopy(SCHEMA)
del SCRIPT_SCHEMA['properties']['passes']
SCRIPT_SCHEMA['required'].remove('passes')
SCRIPT_SCHEMA['properties']['planner'] = {'const': PLANNER}
SCRIPT_SCHEMA['required'].append('planner')
BATCH_SCHEMA = copy.deepcopy(SCRIPT_SCHEMA)
BATCH_SCHEMA['properties']['planner'] = {'const': bayesian_policy.POLICY}
BATCH_SCHEMA['properties']['selection_script'] = {'type': 'string', 'minLength': 1}
BATCH_SCHEMA['properties']['script_sha256'] = {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}
BATCH_SCHEMA['required'].extend(['selection_script', 'script_sha256'])
SCRIPT_GUIDANCE = ('在 combination_settings 声明 planner=grid_then_bayesian_ei_v1、固定 validation_seed、'
    'steps 中的联合搜索维度及完整 grid_values、排除因素和 max_new_trials。预算为联合阶段新增训练硬上限。'
    '先以架构批准的同种子对照逐项运行预设单因素网格。随后脚本在已测组合的一因素邻域中，'
    '用完整多参数向量拟合高斯过程，按预期改进选一个候选。预算耗尽或可行候选耗尽即停止选点。'
    '脚本点位在 next_step 中，YAML 与选点一致后逐批独立审核，由宿主派发、复用并恢复训练。')


def scripted(evidence):
    return evidence.get('combination_validation', {}).get('version') in (2, 3)


def settings_schema(evidence):
    if evidence.get('combination_validation', {}).get('version') == 3:
        return BATCH_SCHEMA
    return SCRIPT_SCHEMA if scripted(evidence) else SCHEMA


def settings_guidance(evidence):
    if evidence.get('combination_validation', {}).get('version') == 3:
        return bayesian_policy.GUIDANCE
    return SCRIPT_GUIDANCE if scripted(evidence) else GUIDANCE

GUIDANCE = ('用户已要求在实验设置中由agent补充组合验证方案；这是追加设置任务，原范围及训练回执保留。'
    '在实验设置中明确组合验证方案 combination_settings。声明一个固定validation_seed、'
    'initial_control_rule=best_measured_same_seed、passes=1。由你安排steps顺序及逐项理由，'
    '每步只改一个已审因素，grid_values逐项复制该因素完整有界取值。'
    '不参与组合扫描的已审因素须列excluded_factors并给出理由，每项恰好出现一次。'
    'max_new_trials为各step的非控制值数之和，shared每值计两次，复用现有配置会减少实际训练。'
    'selection_rule=strict_improvement_control_first_on_tie，完整声明停止条件与组合验证依据。'
    '两方法分别选同种子已测较优起点，完整评测一步后按验证主指标选择下一步控制。'
    '范围设置本身不授权任何训练；每个候选仍通过YAML校验、独立投票和资源准入。')

class MissingSettingsReads(ValueError):
    pass


def _active(request):
    return request.get('read_correction') or request


def validate(settings, factors, evidence):
    from .prose_schema import validate as validate_json
    from .tuning_scope import value_key
    validate_json(settings, settings_schema(evidence))
    if settings.get('planner') == bayesian_policy.POLICY:
        bayesian_policy.compile_policy(settings['selection_script'])
    available = {(f['variant'], f['path']): f for f in factors if f['disposition'] == 'search'}
    if available and not settings['steps']:
        raise ValueError('Declare at least one bounded combination step for the searchable factors')
    selected = []
    bound = 0
    for step in settings['steps']:
        key = (step['variant'], step['path'])
        f = available.get(key)
        if not f or [value_key(v) for v in step['grid_values']] != [value_key(v) for v in f['values']]:
            raise ValueError('Combination step must retain the independently reviewed factor and exact finite grid')
        selected.append(key)
        bound += (len(step['grid_values']) - 1) * (2 if step['variant'] == 'shared' else 1)
    selected += [(f['variant'], f['path']) for f in settings['excluded_factors']]
    if len(selected) != len(set(selected)) or set(selected) != set(available):
        raise ValueError('Agent combination settings must account for each reviewed factor once')
    if not scripted(evidence) and settings['max_new_trials'] != bound:
        raise ValueError('Combination maximum must match the finite declared steps')
    if scripted(evidence) and settings['steps'] and settings['max_new_trials'] < 1:
        raise ValueError('Declare a positive bounded Bayesian search budget')
    seeds = {g['validation_seed'] for g in evidence.get('parameter_grids', [])}
    if seeds and seeds != {settings['validation_seed']}:
        raise ValueError('Combination settings must keep the already fixed tuning seed')
    from .paper_baselines import control_available
    observations = 'own_observations' if evidence.get('baseline_mode') == 'paper_reported' else 'pairs'
    if not any(control_available(c) and any(p.get('seed') == settings['validation_seed'] for p in c.get(observations, []))
               for c in evidence.get('comparisons', {}).values()):
        raise ValueError('Combination settings require an existing paired control at this seed')
    if scripted(evidence):
        from .combination_validation import _controls
        from .tuning_scope import configured_value
        cert = evidence['dataset_optimization']['architecture_certificate']
        base = cert['body']['assessment']['criteria']['reference_unit_id']
        comp = evidence['comparisons'][base]
        controls = _controls(evidence, settings['validation_seed'], comp['task_id'], comp['metric'], comp['direction'])
        if base not in controls:
            raise ValueError('The grid seed must have an authenticated architecture reference pair')
        for f in available.values():
            values = {value_key(configured_value(row['params'], f['path'], evidence))
                      for v, row in controls[base].items() if f['variant'] in ('shared', v)}
            if len(values) != 1 or not values <= {value_key(v) for v in f['values']}:
                raise ValueError('Each scripted grid must include its exact fixed control: ' + f['path'])


def key(run_id, evidence):
    return run_id + ':combination-settings:' + sha([evidence['binding'], evidence['tuning_scope']['plan_sha256']])


def _reads(engine, run_id, role, request, paths):
    from .diagnostic_planning_recovery import authenticate
    from .experiment_decisions import _pointer
    from .experiment_tools import compare
    from .training_read_dispatch import _gaps
    owner = authenticate(engine, run_id, role, request['purpose'],
        {k: request[k] for k in ('instruction', 'context', 'schema')}, request['answer'])
    context = request['context']
    ref = context['validation_evidence']
    archive = engine.store.get('experiment_evidence', ref['id']) or {}
    if archive.get('run_id') != run_id or sha(archive.get('summary')) != ref['sha256']:
        raise ValueError('Combination settings read source changed')
    cited = set(request['answer']['read_receipts'])
    coverage = {path: [] for path in paths}
    found = set()
    for row in engine.store.get('handoff_progress', owner, {}).get('tool_results', []):
        call, result = row.get('call', {}), row.get('result', {})
        receipt = result.get('receipt_sha256')
        if receipt not in cited:
            continue
        args = call.get('arguments', {})
        body = {k: v for k, v in result.items() if k != 'receipt_sha256'}
        if (call.get('name') != 'experiments.compare' or args.get('path') not in coverage
                or receipt != sha({'call': call, 'result': body}) or compare(engine, run_id, context, args) != body):
            raise ValueError('Combination settings cite an invalid or unrelated read receipt')
        coverage[args['path']].append((result['offset'], result['end']))
        found.add(receipt)
    if found != cited or any(_gaps(len(encoded(_pointer(archive['summary'], path))), spans)
                             for path, spans in coverage.items()):
        raise MissingSettingsReads('Read all supplied combination settings pages before deciding')


def verified(engine, run_id, evidence):
    scope = evidence['tuning_scope']
    if scope['plan'].get('combination_settings'):
        settings = scope['plan']['combination_settings']
        validate(settings, scope['plan']['factors'], evidence)
        return settings  # tuning_scope.verify already authenticated the author and three readers.
    record = engine.store.get(COLLECTION, key(run_id, evidence)) or {}
    if record.get('status') != 'accepted':
        return None
    if record.get('run_id') != run_id or record.get('binding') != evidence['binding'] or record.get('scope_sha256') != scope['plan_sha256']:
        raise ValueError('Combination settings lost their code and scope binding')
    row = record['rounds'][-1]
    author = _active(row['author'])
    settings = author['answer']['settings']
    validate(settings, scope['plan']['factors'], evidence)
    _reads(engine, run_id, 'engineer', author, ['/combination_validation/available_factors'])
    if record.get('settings_sha256') != sha(settings) or not full.assignment_members({r:_active(a) for r,a in row['votes'].items()}):
        raise ValueError('Combination settings have no complete independent review')
    for role, saved in row['votes'].items():
        request = _active(saved)
        _reads(engine, run_id, role, request, ['/combination_settings_plan', '/bayesian_script_file']
               if settings.get('planner') == bayesian_policy.POLICY else ['/combination_settings_plan'])
        if not request['answer']['approve'] or request['answer']['issues'] or request['answer']['settings_sha256'] != sha(settings):
            raise ValueError('Combination settings retain an independent objection')
    return settings


async def ensure(engine, harness, run_id, evidence):
    if evidence.get('combination_validation', {}).get('status') != 'settings_required':
        return False
    from .experiment_decisions import _archive, packet
    from .prose_schema import validate as validate_json
    identifier = key(run_id, evidence)
    record = engine.store.get(COLLECTION, identifier) or {'id': identifier, 'run_id': run_id,
        'binding': evidence['binding'], 'scope_sha256': evidence['tuning_scope']['plan_sha256'], 'rounds': []}
    if record.get('status') == 'accepted':
        from .combination_validation import attach
        attach(engine, run_id, evidence)
        return False
    def save(status):
        engine.check_active(run_id)
        record.update(status=status, updated_at=now())
        engine.store.put(COLLECTION, identifier, record)
    ref = _archive(engine, run_id, evidence)
    context = packet(evidence, ref)
    if evidence['combination_validation']['version'] == 3:
        context['experiment_decision']['bayesian_script_authoring'] = 1
    if not record['rounds'] or record['rounds'][-1].get('rejected'):
        prior = record['rounds'][-1] if record['rounds'] else {}
        feedback = prior.get('votes', {})
        context['experiment_decision']['combination_settings_feedback'] = {
            'votes': {role: _active(r).get('answer') for role, r in feedback.items()},
            'validation_error': prior.get('error'), 'previous_settings': (prior.get('author', {}).get('answer') or {}).get('settings')}
        receipts = {'type': 'array', 'minItems': 1, 'items': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'}}
        record['rounds'].append({'author': {
            'purpose': 'experiment_decide:combination-settings:' + sha(identifier) + ':' + str(len(record['rounds'])),
            'instruction': settings_guidance(evidence) + '先用experiments.compare完整读取/combination_validation/available_factors。'
                '最终settings字段填写组合设置，read_receipts只引用该路径的真实读取回执。',
            'context': context, 'schema': obj({'settings': settings_schema(evidence), 'read_receipts': receipts})}, 'votes': {}})
        save('planning')
    row = record['rounds'][-1]
    async def ask(role, request):
        if 'answer' not in request:
            request['answer'] = await full.ask_assignment(harness, run_id, role, request)
            save('planning' if role == 'engineer' and request is row['author'] else 'reviewing')
        validate_json(request['answer'], request['schema'])
        return request['answer']
    author = await ask('engineer', row['author'])
    try:
        validate(author['settings'], evidence['tuning_scope']['plan']['factors'], evidence)
        if evidence['combination_validation']['version'] == 3:
            from .bayesian_script_tool import verified as verify_script
            verify_script(engine, run_id, evidence, author['settings'])
    except ValueError as exc:
        row.update(rejected=True, error=str(exc))
        save('correcting')
        return True
    async def finish_reading(role, request, paths):
        active = _active(request)
        await ask(role, active)
        try:
            _reads(engine, run_id, role, active, paths)
        except MissingSettingsReads:
            if request.get('read_correction'):
                raise
            corrected = {k: copy.deepcopy(request[k]) for k in ('purpose', 'instruction', 'context', 'schema')}
            corrected['purpose'] += ':read-correction'
            corrected['instruction'] += '\n仅补齐上述指定路径的完整读取与真实回执，保留已保存设置；读完后独立返回完整答复。'
            if 'settings' in request['answer']:
                corrected['schema']['properties']['settings'] = {'const': request['answer']['settings']}
            request['read_correction'] = corrected
            save('reading')
            await ask(role, corrected)
            _reads(engine, run_id, role, corrected, paths)
    await finish_reading('engineer', row['author'], ['/combination_validation/available_factors'])
    vote_paths = ['/combination_settings_plan']
    if evidence['combination_validation']['version'] == 3:
        vote_paths.append('/bayesian_script_file')
    review_roles, review_binding = full.prepare_assignments(row, 'votes', full.make_binding({
        'settings_sha256': sha(author['settings']), 'binding': evidence['binding'],
        'scope_sha256': evidence['tuning_scope']['plan_sha256']}, 'combination_settings'))
    if not row['votes']:
        voting = {**copy.deepcopy(evidence), 'combination_settings_plan': author['settings']}
        if len(vote_paths) == 2:
            from pathlib import Path
            script = verify_script(engine, run_id, evidence, author['settings'])
            voting['bayesian_script_file'] = {'relative_path': script['body']['relative_path'],
                'sha256': script['body']['file_sha256'], 'source': Path(script['body']['path']).read_text(encoding='utf-8')}
        vote_ref = _archive(engine, run_id, voting)
        ctx = {**context, 'validation_evidence': vote_ref, 'evidence_sha256': vote_ref['sha256']}
        for role in review_roles:
            row['votes'][role] = {'purpose': 'experiment_vote:combination-settings:' + sha(author['settings']) + ':' + str(len(record['rounds'])),
                'instruction': '完整读取/combination_settings_plan，独立检查组合顺序、排除理由、预算上界和停止条件。'
                    + ('完整读取 /bayesian_script_file；三方批准后授权这份脚本在已审边界内批量训练。'
                       if len(vote_paths) == 2 else '批准设置不授权训练。') + settings_guidance(evidence),
                'context': copy.deepcopy(ctx), 'schema': obj({'settings_sha256': {'const': sha(author['settings'])},
                    'approve': {'type': 'boolean'}, 'issues': {'type': 'array', 'items': TEXT},
                    'rationale': TEXT, 'read_receipts': row['author']['schema']['properties']['read_receipts']})}
            if review_binding:
                row['votes'][role]['context'] = full.bind(row['votes'][role]['context'], review_binding)
        save('reviewing')
    import asyncio
    tasks = [asyncio.create_task(finish_reading(role, req, vote_paths)) for role, req in row['votes'].items()]
    try:
        await gather_owned(*tasks)
    except BaseException:
        for task in tasks:
            cancel_owned(task)
        await gather_owned(*tasks, return_exceptions=True)
        raise
    if all(_active(r)['answer']['approve'] and not _active(r)['answer']['issues'] for r in row['votes'].values()):
        record['settings_sha256'] = sha(author['settings'])
        save('accepted')
    else:
        row['rejected'] = True
        save('reconciling')
    engine.store.event(run_id, 'experiment.combination_settings', '组合验证实验设置已保存。',
                       {'status': record['status'], 'execution_authorized': False})
    return True
