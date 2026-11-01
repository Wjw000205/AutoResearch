"""Agent-owned, source-bound hyperparameter scope and per-dataset coverage.

This is planning authorization only. Each candidate still needs ordinary
independent experiment approval, runtime capability checks and resource gates.
"""
from __future__ import annotations

import copy
import asyncio
import math

from jsonschema import ValidationError

from .prose_schema import validate

from .experiment_workflow import sha
from .storage import now
from .training_adjustment import obj
from .tuning_scope_reads import active_vote

REQUESTS = 'tuning_scope_requests'
COLLECTION = 'tuning_scopes'
ROLES = ('director', 'engineer', 'scout')
TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 1200}
VALUE = {'anyOf': [{'type': 'number'}, {'type': 'boolean'}, {'type': 'string', 'minLength': 1, 'maxLength': 80},
                  {'type': 'array', 'minItems': 1, 'maxItems': 16, 'items': {'type': 'number'}}]}
VALUES = {'type': 'array', 'minItems': 2, 'maxItems': 9, 'items': VALUE}
# Data, task identity, evaluation, seeds and host limits are not hyperparameters.
COMMON = {'lr', 'learning_rate', 'weight_decay', 'grad_clip', 'clip_grad_norm', 'max_grad_norm',
          'batch_size', 'optimizer', 'optimizer_eps', 'betas', 'scheduler', 'patience',
          'min_delta', 'max_epochs', 'max_steps', 'drop_last'}
PROTECTED = {'seed', 'validation_seeds', 'final_test_seeds', 'dataset', 'dataset_assets', 'comparison_task',
    'comparison_inputs', 'comparison_transport', 'ablation_configuration', 'evaluation', 'acceptance',
    'benchmark_task_id', 'benchmark_source', 'phase', 'variant', 'runtime', 'loss', 'loss_contract',
    'algorithm', 'evaluation_contract_version', 'implementation_contract', 'configurations',
    'common_ref', 'baseline_ref', 'proposed_ref', 'fredn_ref'}


def safe_path(path):
    parts = path.strip('/').split('/')
    if not path.startswith('/') or not parts or any(not p or p in PROTECTED for p in parts):
        return False
    if parts[0] == 'common':
        return len(parts) == 2 and parts[1] in COMMON
    return not any(any(x in p.lower() for x in ('seed', 'data', 'metric', 'budget', 'resource', 'threshold', 'contract')) for p in parts)


def get(params, path):
    value = params
    for part in path.strip('/').split('/'):
        value = value[part.replace('~1', '/').replace('~0', '~')]
    return value


def configured_value(params, path, evidence):
    try:
        return get(params, path)
    except KeyError:
        capability = next((c for c in evidence.get('parameter_capabilities', []) if c['path'] == path
                           and c['variant'] in ('shared', params['variant'])), None)
        if not capability:
            raise ValueError('Parameter path has no source-declared default: ' + path)
        return copy.deepcopy(capability['default'])


def set_value(params, path, value):
    parts = path.strip('/').split('/')
    target = params
    for part in parts[:-1]:
        target = target[part.replace('~1', '/').replace('~0', '~')]
    target[parts[-1].replace('~1', '/').replace('~0', '~')] = copy.deepcopy(value)


def value_key(value):
    # Preserve JSON types: bool is not a numeric setting, integer widths stay integers.
    return sha({'type': 'number' if type(value) in (int, float) else type(value).__name__,
                'value': float(value) if type(value) in (int, float) else value})


def inventory(evidence):
    found = {}
    def walk(value, prefix, variant):
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, prefix + '/' + key.replace('~', '~0').replace('/', '~1'), variant)
        elif safe_path(prefix):
            try:
                validate(value, VALUE)
            except ValidationError:
                return
            if type(value) is float and not math.isfinite(value):
                return
            item = found.setdefault(prefix, {'path': prefix, 'values_by_variant': {}})
            values = item['values_by_variant'].setdefault(variant, [])
            if all(value_key(value) != value_key(v) for v in values):
                values.append(value)
    for params in evidence['job_parameters'].values():
        if params.get('variant') in {'baseline', 'proposed'}:
            walk(params, '', params['variant'])
    for c in evidence.get('parameter_capabilities', []):
        row = found.setdefault(c['path'], {'path': c['path'], 'values_by_variant': {}, 'source_capability': c})
        for variant in ('baseline', 'proposed') if c['variant'] == 'shared' else (c['variant'],):
            row['values_by_variant'].setdefault(variant, [c['default']])
    return sorted(found.values(), key=lambda row: row['path'])


def enabled(store, run_id):
    run = store.get('runs', run_id) or {}
    return run.get('mode') == 'live' and (store.get(REQUESTS, run_id) is not None or run.get('config', {}).get('tuning_scope_protocol') == 1
        or (store.get('programs',run_id) or {}).get('hyperparameter_yaml_protocol') == 1)


def request(store, run_id):
    saved = store.get(REQUESTS, run_id)
    if saved:
        if saved.get('sha256') and sha(saved['body']) != saved['sha256']:
            raise ValueError('Broad tuning request changed')
        return saved['body']['request'] if saved.get('body') else saved['request']
    return '由智能体全面盘点本数据集的可调参数，决定取值范围、优先级及停止条件，经三方评审逐批执行；完成优化与稳定性复核后自动继续下一数据集。'


def register_request(engine, run_id, text):
    """Append the user's scope request without changing frozen run config hashes."""
    if not text.strip():
        raise ValueError('An explicit tuning request is required')
    body = {'run_id': run_id, 'request': text}
    old = engine.store.get(REQUESTS, run_id)
    if old and old.get('sha256') == sha(body):
        return old
    if old:
        engine.store.put(REQUESTS, run_id + ':history:' + sha(old), {**old, 'archived': True})
    row = {'id': run_id, 'run_id': run_id, 'body': body, 'sha256': sha(body), 'created_at': now()}
    engine.store.put(REQUESTS, run_id, row)
    return row


def key(run_id, binding, request_text=None):
    return run_id + ':tuning-scope:' + sha({'binding': binding, 'request': request_text})


def current_key(engine, run_id, binding):
    base = key(run_id, binding, request(engine.store, run_id))
    head = engine.store.get('tuning_scope_revisions', base)
    if not head:
        return base
    from .experiment_decisions import _approved
    body = head['body']
    decision = engine.store.get('experiment_decisions', body['decision_id']) or {}
    if (sha(body) != head['sha256'] or body['binding'] != binding or not _approved(decision)
            or decision['proposal_sha256'] != body['proposal_sha256']
            or decision['proposal']['action'] != 'revise_tuning_scope'):
        raise ValueError('Tuning scope refinement lost its independent authorization')
    archive = engine.store.get('experiment_evidence', decision['evidence']['id']) or {}
    if sha(archive.get('summary')) != decision['evidence']['sha256'] or archive['summary']['tuning_scope']['id'] != body['previous_scope_id']:
        raise ValueError('Tuning scope refinement source changed')
    return base + ':refinement:' + body['proposal_sha256']


def reopen(engine, run_id, decision, evidence):
    from .experiment_decisions import _approved
    if not _approved(decision) or evidence.get('tuning_scope', {}).get('status') != 'accepted':
        raise ValueError('Scope refinement needs a current scope and three independent votes')
    body = {'binding': evidence['binding'], 'decision_id': decision['id'], 'proposal_sha256': decision['proposal_sha256'],
            'previous_scope_id': evidence['tuning_scope']['id'], 'rationale': decision['proposal']['rationale']}
    base = key(run_id, evidence['binding'], request(engine.store, run_id))
    old = engine.store.get('tuning_scope_revisions', base)
    if old and old['sha256'] != sha(body):
        engine.store.put('tuning_scope_revisions', base + ':history:' + old['sha256'], {**old, 'archived': True})
    engine.store.put('tuning_scope_revisions', base, {'id': base, 'run_id': run_id, 'body': body, 'sha256': sha(body)})
    current_key(engine, run_id, evidence['binding'])
    return {'tuning_pending': True, 'next_node': 'tuning', 'activity': 'tuning_scope_refinement'}


def schema(evidence):
    paths = [r['path'] for r in inventory(evidence)]
    return obj({'binding_sha256': {'const': sha(evidence['binding'])},
        'strategy': TEXT, 'ordering_rationale': TEXT, 'stopping_rule': TEXT,
        'factors': {'type': 'array', 'minItems': 1, 'maxItems': 160, 'items': obj({
            'path': {'enum': paths}, 'variant': {'enum': ['shared', 'baseline', 'proposed']},
            'category': {'enum': ['capacity', 'regularization', 'optimizer', 'training', 'mechanism']},
            'disposition': {'enum': ['search', 'fixed', 'requires_implementation']},
            'values': {'type': 'array', 'maxItems': 9, 'items': VALUE},
            'value_kind': {'enum': ['number', 'integer', 'boolean', 'categorical', 'integer_vector', 'number_vector']},
            'priority': {'type': 'integer', 'minimum': 1, 'maximum': 160},
            'rationale': TEXT, 'runtime_support': TEXT,
            'source_paths': {'type': 'array', 'minItems': 1, 'maxItems': 8, 'uniqueItems': True,
                             'items': {'type': 'string', 'minLength': 1}}})},
        'read_receipts': {'type': 'array', 'minItems': 1, 'maxItems': 256, 'uniqueItems': True,
                          'items': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'}},
        'execution_authorized': {'const': False}})


def validate_scope(plan, evidence, source_files):
    validate(plan, schema(evidence))
    discovered = {r['path']: r for r in inventory(evidence)}
    covered = set()
    for f in plan['factors']:
        variants = set(discovered[f['path']]['values_by_variant']) if f['variant'] == 'shared' else {f['variant']}
        if not variants <= set(discovered[f['path']]['values_by_variant']):
            raise ValueError('Factor variant does not own this configuration path')
        for variant in variants:
            pair = (f['path'], variant)
            if pair in covered:
                raise ValueError('A parameter/variant must have exactly one scope decision')
            covered.add(pair)
        if not set(f['source_paths']) <= set(source_files):
            raise ValueError('Tuning capability references an unavailable source file')
        if f['disposition'] == 'fixed':
            # A singleton annotation of the unchanged value is not a search.
            # Retain the paid answer verbatim; never rewrite it to [] on recovery.
            if f['values'] and (len(f['values']) != 1 or any(
                    value_key(f['values'][0]) not in {value_key(v) for v in discovered[f['path']]['values_by_variant'][variant]}
                    for variant in variants)):
                raise ValueError('A fixed parameter may only annotate one existing control value, not introduce a grid')
            continue
        validate(f['values'], VALUES)
        if len({value_key(v) for v in f['values']}) != len(f['values']):
            raise ValueError('Tuning values must be distinct with exact JSON types')
        for v in f['values']:
            validate_value(v, f['value_kind'])
        for variant in variants:
            if not any(value_key(v) in {value_key(x) for x in discovered[f['path']]['values_by_variant'][variant]}
                       for v in f['values']):
                raise ValueError('Each factor grid must retain a measured/frozen control value')
        structural = any(token in f['path'].split('/')[-1] for token in ('depth', 'width', 'channels', 'dims', 'layers', 'dilations', 'embedding_dim', 'harmonics', 'kernel_size'))
        yaml_supported = evidence.get('hyperparameter_yaml', {}).get('ready') and any(
            c['path'] == f['path'] and c['variant'] in (f['variant'],'shared') for c in evidence.get('parameter_capabilities',[]))
        if evidence.get('dataset_optimization', {}).get('architecture_frozen') and (
                f['category'] == 'mechanism' or (f['category'] == 'capacity' or structural) and not yaml_supported):
            raise ValueError('Later datasets retain the confirmed architecture; reopening needs explicit architecture authorization')
    expected = {(r['path'], v) for r in discovered.values() for v in r['values_by_variant']}
    if covered != expected:
        raise ValueError('Every discovered parameter requires search, fixed-with-reason, or implementation disposition')


def validate_value(value, kind):
    valid = {'number': type(value) in (int, float) and math.isfinite(value) if type(value) in (int, float) else False,
             'integer': type(value) is int,
             'boolean': type(value) is bool,
             'categorical': type(value) is str,
             'integer_vector': isinstance(value, list) and bool(value) and all(type(v) is int and v > 0 for v in value),
             'number_vector': isinstance(value, list) and bool(value) and all(type(v) in (int, float) and math.isfinite(v) for v in value)}
    if not valid.get(kind):
        raise ValueError('Candidate type differs from reviewed factor type')


def verify(engine, run_id, record):
    from .scope_read_session import reading
    with reading(engine) as reader:
        return _verify(reader, run_id, record)


def _verify(engine, run_id, record):
    from .parameter_resolution import owner
    archive = engine.store.get('experiment_evidence', record['evidence']['id']) or {}
    evidence = archive.get('summary')
    if sha(evidence) != record['evidence']['sha256'] or evidence['binding'] != record['binding']:
        raise ValueError('Tuning scope source evidence changed')
    final = record['rounds'][-1]
    author = final.get('correction') or final['author']
    owner(engine, run_id, 'engineer', author)
    plan = author['answer']
    source = engine.store.get('experiment_sources', evidence['code_sources']['id'])
    if sha(source['files']) != evidence['code_sources']['sha256']:
        raise ValueError('Tuning scope source code changed')
    validate_scope(plan, evidence, source['files'])
    check_reads(engine, run_id, 'engineer', author, plan, scope_record=record)
    if record.get('plan_sha256') != sha(plan) or set(final.get('votes', {})) != set(ROLES):
        raise ValueError('Tuning scope needs its exact plan and three independent votes')
    for role, request in final['votes'].items():
        request = active_vote(request)
        owner(engine, run_id, role, request)
        vote = request['answer']
        if not vote['approve'] or vote['issues'] or vote['proposal_sha256'] != sha(plan):
            raise ValueError('Tuning scope has unresolved independent dissent')
        check_reads(engine, run_id, role, request, plan, voting=True, scope_record=record)
    return plan


def attach(engine, run_id, evidence):
    if not enabled(engine.store, run_id):
        return
    import json
    sources = engine.store.get('experiment_sources', evidence['code_sources']['id'])
    if sha(sources['files']) != evidence['code_sources']['sha256']:
        raise ValueError('Tuning capability source identity changed')
    capabilities = []
    item = sources['files'].get('configs/method_configs.json', {})
    if item.get('text'):
        capabilities = json.loads(item['text']).get('_tuning_capabilities', [])
    program = engine.store.get('programs', run_id) or {}
    if program.get('hyperparameter_yaml_protocol') == 1:
        from .hyperparameter_yaml import leaves
        contract = program['hyperparameter_contract']
        paths = [contract['contract'][key] for key in ('defaults','schema','loader')]
        supplied = []
        for variant, tree in contract['defaults']['profiles'].items():
            if variant not in {'baseline','proposed'}: continue
            for path, value in leaves(tree):
                if not safe_path(path): continue
                kind = ('boolean' if type(value) is bool else 'categorical' if isinstance(value,str) else
                        'number_vector' if isinstance(value,list) else 'number')
                try: validate_value(value,kind)
                except ValueError: continue
                supplied.append({'path':path,'variant':variant,'default':value,'value_kind':kind,'source_paths':paths})
        capabilities = supplied
    capability_schema = obj({'path': {'type': 'string'}, 'variant': {'enum': ['shared', 'baseline', 'proposed']},
        'value_kind': {'enum': ['number', 'integer', 'boolean', 'categorical', 'integer_vector', 'number_vector']},
        'default': VALUE, 'source_paths': {'type': 'array', 'minItems': 1, 'maxItems': 8, 'items': {'type': 'string'}}})
    validate(capabilities, {'type': 'array', 'maxItems': 256, 'items': capability_schema})
    seen = set()
    for c in capabilities:
        if not safe_path(c['path']) or not set(c['source_paths']) <= set(sources['files']):
            raise ValueError('Source tunable capability changes protected input or cites absent code')
        validate_value(c['default'], c['value_kind'])
        for variant in ('baseline', 'proposed') if c['variant'] == 'shared' else (c['variant'],):
            if (c['path'], variant) in seen:
                raise ValueError('Source tunable capability is duplicated')
            seen.add((c['path'], variant))
    evidence['parameter_capabilities'] = capabilities
    instruction = request(engine.store, run_id)
    record = engine.store.get(COLLECTION, current_key(engine, run_id, evidence['binding']))
    evidence['tuning_scope'] = {'required': True, 'status': 'not_planned', 'inventory': inventory(evidence),
        'request': instruction, 'execution_authorized': False}
    if record and record['status'] == 'accepted':
        if record['binding'] != evidence['binding']:
            raise ValueError('Tuning scope does not match current dataset/code/configuration')
        plan = verify(engine, run_id, record)
        evidence['tuning_scope'].update(status='accepted', plan=plan, plan_sha256=record['plan_sha256'], id=record['id'])
        from .tuning_scope_acceptance import project
        evidence['tuning_scope']['acceptance'] = project(engine, run_id, record, plan)
        evidence['tuning_scope']['coverage'] = coverage(evidence)


def factor(spec, evidence):
    scope = evidence.get('tuning_scope') or {}
    if scope.get('status') != 'accepted' or spec.get('scope_sha256') != scope.get('plan_sha256'):
        raise ValueError('Broad parameter search requires the current independently approved scope')
    f = next((f for f in scope['plan']['factors'] if f['path'] == spec['factor_path']
              and f['variant'] == spec.get('variant', 'shared')), None)
    if not f or f['disposition'] != 'search':
        raise ValueError('This factor is fixed or still requires an implementation amendment')
    if [value_key(v) for v in spec['grid_values']] != [value_key(v) for v in f['values']]:
        raise ValueError('Candidate grid must equal the agent-reviewed finite factor grid')
    validate_value(spec['value'], f['value_kind'])
    if value_key(spec['value']) not in {value_key(v) for v in f['values']}:
        raise ValueError('Candidate is outside the reviewed factor values')
    return f


def coverage(evidence):
    scope = evidence['tuning_scope']
    result = []
    for f in scope['plan']['factors']:
        grids = [g for g in evidence.get('parameter_grids', []) if g['factor_path'] == f['path']
                 and g.get('variant', 'shared') == f['variant']]
        measured = {value_key(p['value']) for g in grids for p in g['points'] if p['status'] in {'completed', 'reused'}}
        pending = [v for v in f['values'] if value_key(v) not in measured]
        result.append({'path': f['path'], 'variant': f['variant'], 'disposition': f['disposition'],
            'pending_values': pending, 'completed_new_points': sum(g['completed_new_points'] for g in grids),
            'complete': f['disposition'] == 'fixed' or f['disposition'] == 'search' and not pending})
    return result


def compact(value):
    result = {k: copy.deepcopy(value[k]) for k in ('required', 'status', 'id', 'plan_sha256') if k in value}
    result['details_path'] = '/tuning_scope'
    if value.get('acceptance'):
        result['acceptance'] = copy.deepcopy(value['acceptance'])
    if value.get('plan'):
        states = {(r['path'], r['variant']): r for r in value['coverage']}
        pending = [f for f in value['plan']['factors'] if not states[(f['path'], f['variant'])]['complete']]
        result['next_factors'] = [{k: copy.deepcopy(f[k]) for k in ('path', 'variant', 'disposition', 'values', 'value_kind', 'priority')}
                                  for f in sorted(pending, key=lambda f: f['priority'])[:8]]
        result['pending_factor_count'] = len(pending)
        result['strategy'] = value['plan']['strategy']
        result['stopping_rule'] = value['plan']['stopping_rule']
    return result


def require_complete(evidence, proposal):
    scope = evidence.get('tuning_scope')
    if not scope:
        return
    if scope['status'] != 'accepted':
        raise ValueError('Agent-owned parameter scope must be independently approved before dataset completion')
    incomplete = [f for f in coverage(evidence) if not f['complete']]
    stops = proposal.get('parameter_search_stops') or []
    for row in incomplete:
        stop = next((s for s in stops if s['path'] == row['path'] and s['variant'] == row['variant']), None)
        if row['disposition'] == 'requires_implementation' or row['completed_new_points'] == 0 or not stop or not stop.get('rationale', '').strip():
            raise ValueError('Unfinished reviewed tuning factor: ' + row['variant'] + ':' + row['path'])


GUIDANCE = '''tuning_scope 是当前数据集、代码和配置绑定的三方调参计划。按照其优先级与实测结果选择下一单因素批次。
若实测结果支持扩展/收缩范围或调整搜索次序，选择 revise_tuning_scope（unit_id=null、changes=[]）说明具体依据，
training_response.disposition=scope_planning、根因仍可 unknown；三方批准后重新设计范围，原批准和结果保留。无需等待用户逐项选参数。
范围由你们决定，不能把学习率两点默认当作全面调参；逐项处理容量、正则化、优化器及训练配置。
search_parameters 使用 scope_sha256、variant(shared/baseline/proposed) 与完整已审 grid_values；value 保持 JSON 类型。
数值因素填写覆盖全部值的 lower_bound/upper_bound；布尔、类别、向量因素的两个数值边界都填 null，以离散值集合限定范围。
variant-specific 只改该方法，另一方法复用同种子已完成对照。数据、划分、种子、目标和科学阈值不可作为参数搜索。
requires_implementation 必须先通过 revise_architecture 给出具体配置化改动，保持原控制配置行为，三方审代码与 CPU 检查后重新验证；
不能把配置字段存在当作模型实际支持，也不能修改硬编码维度后跳过架构评审。后续数据集保持已确认的模型架构。
需要新增规范化深度/宽度字段时，可在既有 configs/method_configs.json 中声明 _tuning_capabilities 数组，
每项为 path、variant、value_kind、default、source_paths；默认值须恢复原控制行为，构造器/校验/形状闭包必须实际支持。
该源码声明不是批准，修改后仍重新制定范围并审核。不要改写旧 params 来补新增字段；缺省值由已审实现负责。
完成当前数据集前逐项核对 scope coverage；若停止某个已经实测的网格，parameter_search_stops 写明 path、variant、实测停止理由，
交三方审核，不能把未开始的搜索或未实现能力写成完成。通过原稳定性、报告门禁后由宿主自动配置下一数据集。'''


def check_reads(engine, run_id, role, request, plan, voting=False, *, scope_record=None):
    from .tuning_scope_reads import check
    from .scope_read_session import reading
    with reading(engine) as reader:
        return check(reader, run_id, role, request, plan, voting=voting, scope_record=scope_record)


def reading_blocker(row, exc, save):
    """A receipt/coverage failure is a local repair, never a new science round."""
    from .engine import ResearchBlocked
    row.update(error=str(exc), reading_issues=copy.deepcopy(getattr(exc, 'issues', [])))
    row.pop('needs_reconciliation', None)
    save(status='blocked_reading')
    raise ResearchBlocked('调参范围读取合同未通过；保留当前方案，未派发整份重写。' + str(exc)) from exc


async def ensure(engine, harness, run_id):
    from .engine import ResearchBlocked
    from .experiment_decisions import snapshot, _archive, packet
    from .parameter_resolution import owner
    if not enabled(engine.store, run_id):
        return None
    evidence = await engine._read_consensus_check(run_id, snapshot, engine, run_id)
    if not evidence['records'] or not evidence.get('dataset_optimization', {}).get('architecture_certificate'):
        return None
    request_text = request(engine.store, run_id)
    identifier = current_key(engine, run_id, evidence['binding'])
    record = engine.store.get(COLLECTION, identifier)
    if record and record['status'] == 'accepted':
        verify(engine, run_id, record)
        return None
    if not record:
        evidence['tuning_inventory'] = inventory(evidence)
        ref = _archive(engine, run_id, evidence)
        record = {'id': identifier, 'run_id': run_id, 'binding': evidence['binding'], 'evidence': ref,
                  'created_at': now(), 'status': 'planning', 'rounds': []}
    else:
        evidence = engine.store.get('experiment_evidence', record['evidence']['id'])['summary']
        if sha(evidence) != record['evidence']['sha256'] or evidence['binding'] != record['binding']:
            raise ValueError('Tuning scope source evidence changed')
    context = packet(evidence, record['evidence'])
    context['experiment_decision']['tuning_scope_design'] = {
        'inventory_path': '/tuning_inventory', 'request': request_text,
        'factor_count': sum(len(row['values_by_variant']) for row in inventory(evidence)),
        'scope': 'Plan all available hyperparameters for this dataset; no execution or scientific approval'}
    amendment = engine.store.get('tuning_scope_revisions', key(run_id, evidence['binding'], request_text))
    if amendment:
        context['experiment_decision']['tuning_scope_design']['approved_refinement'] = amendment['body']
    from .tuning_runtime_audit import available as runtime_audit
    audit = runtime_audit(engine, run_id, evidence)
    enriched = {**evidence, **({'tuning_runtime_audit': audit} if audit else {})}
    if audit:
        audit_ref = _archive(engine, run_id, enriched)
        context.update(validation_evidence=audit_ref, evidence_sha256=audit_ref['sha256'])
        context['experiment_decision']['tuning_scope_design'].update(
            runtime_audit_path='/tuning_runtime_audit', runtime_route_paths=audit['runtime_route_paths'],
            runtime_audit_scope='CPU capability evidence only; no training, stability or execution approval')
    from .tuning_scope_context import context as scope_context, GUIDANCE as scope_stage_guidance
    context = scope_context(engine, run_id, context)
    def save(**kw):
        engine.check_active(run_id)
        record.update(kw, updated_at=now())
        engine.store.put(COLLECTION, identifier, record)
    async def ask(role, request):
        if 'answer' not in request:
            request['answer'] = await harness.ask(run_id, role, request['purpose'], request['instruction'], request['context'], request['schema'])
            save()
        owner(engine, run_id, role, request)
        return request['answer']
    instruction = GUIDANCE + scope_stage_guidance + '''\n制定覆盖广泛、由你们决定取值、顺序和停止条件的调参计划。完整读取 /tuning_inventory；
对每个发现的 path/variant 必须明确 search、fixed 或 requires_implementation，并提供理由，不能遗漏或用一个学习率网格概括。
固定的安全边界/公式常量不是必须搜索；不要盲目穷举。可调容量（深度、宽度、embedding、dropout）、正则化、优化器、训练配置均须审视。
现有硬编码能力如需开放必须写 requires_implementation，不能把字段出现当成可执行。首数据集支持经过审查的架构配置化，后续冻结架构。
每个 search/implementation 因素声明含原控制值的有限单因素 values，数值类型准确，值域、间距及意义由你们决定。
fixed 的 values 填 []，也可仅注记一个实际原控制值；不能为 fixed 引入新值或多点网格。
已归档的完整源码读取回执仍有效；当前窗口没有正文不等于没读过。必要时定向重读，不重复全量盘点。
若有 runtime_audit_path，完整读取其脚本、来源、结果和局限，并读 runtime_route_paths 的真实执行入口；
配置化能力要沿当前 experiment.py 的实际调用链判断，不能把未使用的旧合成数据 loader 当成实际入口。
CPU 证实某候选能进入真实优化器不等于科研效果已验证，但不能无其他证据地把该配置路由又列为缺失实现。
shared 只用于双方有相同语义和相同原值的路径；模型特有参数用 baseline 或 proposed，保留另一侧对照。
source_paths 写实际实现/配置校验文件；用 inspect_module 完整读取你引用的每个文件。read_receipts 返回 inventory 和这些源码的全部真实分页回执。
不得修改历史计划、原证据、种子、划分或验收，不运行实验。只交付本次计划，交三方独立审核。'''
    source = engine.store.get('experiment_sources', evidence['code_sources']['id'])
    if sha(source['files']) != evidence['code_sources']['sha256']:
        raise ValueError('Tuning scope source code changed')
    from .tuning_consensus import mediate, issues, pending
    # A host reading-contract correction does not require the author to write
    # the same parameter plan again. Revalidate the exact paid delivery first.
    latest = record['rounds'][-1] if record['rounds'] else {}
    if (latest.get('needs_reconciliation') and not latest.get('votes') and not latest.get('mediation')
            and str(latest.get('error', '')).startswith(('Tuning scope cites unread evidence',
                'Read the complete tuning scope source/evidence:', 'Tuning scope reading evidence needs repair:'))
            and latest.get('author', {}).get('answer')):
        retained = latest['author']
        try:
            validate_scope(retained['answer'], evidence, source['files'])
            reading = check_reads(engine, run_id, 'engineer', retained, retained['answer'], scope_record=record)
        except (ValueError, ValidationError):
            pass  # Genuine missing/changed evidence still follows normal repair.
        else:
            latest.setdefault('validation_history', []).append({'error': latest.pop('error'),
                'revalidated_at': now(), 'reading_evidence': reading, 'approval_inherited': False})
            latest.pop('needs_reconciliation', None)
            save(status='planning')
    # Each graph turn handles one candidate. Dissent creates a specific next
    # assignment, not a terminal state after an arbitrary two-vote limit.
    index = max(0, len(record['rounds']) - 1)
    resolution = None
    if record['rounds'] and record['rounds'][-1].get('needs_reconciliation'):
        previous = record['rounds'][-1]
        prior_votes = [{'role': role, **active_vote(v).get('answer', {})}
                       for role, v in previous.get('votes', {}).items()]
        resolution = await mediate(engine, harness, run_id, previous, context,
            {'scope_id': identifier, 'round': index, 'plan': previous['author'].get('answer'), 'votes': prior_votes},
            issues(prior_votes, previous.get('error')), lambda: save(status='reconciling'))
        index += 1
    for index in [index]:
        if len(record['rounds']) <= index:
            ctx = copy.deepcopy(context)
            ctx['experiment_decision']['tuning_scope_design']['feedback'] = [
                {'error': r.get('error'), **({'reading_issues': r['reading_issues']} if r.get('reading_issues') else {}),
                 'votes': {role: a.get('answer') for role, a in r.get('votes', {}).items()}}
                for r in record['rounds'][-2:]]
            if resolution:
                ctx['experiment_decision']['tuning_scope_design']['consensus_resolution'] = resolution
                ctx['experiment_decision']['tuning_scope_design']['previous_plan'] = record['rounds'][-1]['author'].get('answer')
            record['rounds'].append({'author': {'purpose': 'experiment_decide:tuning-scope:' + sha(identifier) + ':' + str(index),
                'instruction': instruction, 'context': ctx, 'schema': schema(evidence)}})
            save(status='planning')
        row = record['rounds'][index]
        if row.get('error') and index < len(record['rounds']) - 1:
            continue  # A later paid author revision already superseded this draft.
        plan = await ask('engineer', row['author'])
        from .tuning_scope_reads import ReadEvidenceError
        try:
            validate_scope(plan, evidence, source['files'])
        except (ValueError, ValidationError) as exc:
            row.update(error=str(exc), needs_reconciliation=True)
            if getattr(exc, 'issues', None):
                row['reading_issues'] = copy.deepcopy(exc.issues)
            save(status='reconciling')
            return pending(scope_id=identifier, reason='范围草稿需定向修订，已保留原答复与具体校验问题')
        try:
            check_reads(engine, run_id, 'engineer', row['author'], plan, scope_record=record)
        except ValueError as exc:
            reading_blocker(row, exc, save)
        if row.get('error'):
            row.setdefault('validation_history', []).append({'error': row.pop('error'), 'revalidated_at': now()})
        voting_evidence = {**enriched, 'tuning_scope_plan': plan}
        ref = _archive(engine, run_id, voting_evidence)
        ctx = {**copy.deepcopy(context), 'validation_evidence': ref, 'evidence_sha256': ref['sha256'],
               'proposal_sha256': sha(plan)}
        vote_schema = obj({'proposal_sha256': {'const': sha(plan)}, 'approve': {'type': 'boolean'},
            'rationale': TEXT, 'issues': {'type': 'array', 'maxItems': 8, 'items': TEXT},
            'read_receipts': schema(evidence)['properties']['read_receipts']})
        votes = row.setdefault('votes', {})
        for role in ROLES:
            new_assignment = role not in votes
            votes.setdefault(role, {'purpose': 'experiment_vote:tuning-scope:' + sha(plan) + ':round:' + str(index),
                'instruction': '独立完整读取 /tuning_inventory、/tuning_scope_plan 及计划引用的全部 source_paths，返回全部真实分页回执。'
                    '若给出 runtime_audit_path，还须完整读取该 CPU 能力证据及 runtime_route_paths，区分已有可用路由与真正硬编码能力。'
                    '检查覆盖是否充分、固定理由是否成立、候选范围及资源是否合理、源码是否实际支持。'
                    '拟搜索且当前不支持的参数须列 requires_implementation；本轮保持不变的参数可列 fixed，不能虚报可调。批准仅是范围与后续实现待办，不是任何实验执行。'
                    '需修改则列具体 issues；同意 approve=true 且 issues=[]。\n' + GUIDANCE + scope_stage_guidance,
                'context': copy.deepcopy(ctx), 'schema': vote_schema})
            if new_assignment:
                from .tuning_scope_reads import retained_index
                index_ = await engine._read_consensus_check(
                    run_id, retained_index, engine, run_id, role, record, ctx, plan)
                votes[role]['context']['experiment_decision']['tuning_scope_design']['retained_reads'] = index_
                votes[role]['instruction'] += ('\nretained_reads 是宿主逐页核验过的本角色历史读取索引；'
                    '只在代码/计划绑定和对应正文未变时列出。可直接在 read_receipts 引用其中回执，不必重读同一页；'
                    '新计划正文及索引未覆盖的区间仍须实际读取。该索引不继承批准，当前方案仍由你独立判断。')
        # Freeze every independent assignment before dispatch. A slow reader
        # must not prevent the other roles from starting their own review.
        save(status='reviewing')
        async def read_vote(role):
            parent = votes[role].get('operator_review') or votes[role].get('contract_clarification') or votes[role]
            active = active_vote(votes[role])
            await ask(role, active)
            try:
                await engine._read_consensus_check(run_id, check_reads, engine, run_id, role, active, plan, voting=True, scope_record=record)
            except ValueError as exc:
                if parent.get('read_correction') or votes[role].get('operator_review'):
                    return {'role': role, 'error': str(exc), 'issues': getattr(exc, 'issues', [])}
                corrected = copy.deepcopy(active['context'])
                corrected['experiment_decision']['tuning_scope_design']['read_correction'] = {
                    'original_answer': active['answer'], 'error': str(exc), 'issues': copy.deepcopy(getattr(exc,'issues',[]))}
                parent['read_correction'] = {'purpose': active['purpose'] + ':read-correction-1',
                    'instruction': active['instruction'] + '\n补齐本角色缺失读取并独立重判，保留原答复，不继承批准。',
                    'context': corrected, 'schema': copy.deepcopy(active['schema'])}
                save()
                active = parent['read_correction']
                await ask(role, active)
                try:
                    await engine._read_consensus_check(run_id, check_reads, engine, run_id, role, active, plan, voting=True, scope_record=record)
                except ValueError as exc:
                    return {'role': role, 'error': str(exc), 'issues': getattr(exc, 'issues', [])}
            return None
        async def review_role(role):
            error = await read_vote(role)
            if error:
                return error
            active = active_vote(votes[role])
            original_contract = active['context'].get('experiment_decision', {}).get('tuning_scope_design', {}).get('stage_contract')
            if active['answer']['approve'] or original_contract or votes[role].get('contract_clarification'):
                return None
            # One same-role clarification of an old request's stage contract.
            # Keep its original veto; the plan is unchanged and new votes remain independent.
            clarified = scope_context(engine, run_id, active['context'])
            from .tuning_scope_reads import retained_index
            clarified['experiment_decision']['tuning_scope_design'].update(
                original_vote=copy.deepcopy(active['answer']),
                retained_reads=await engine._read_consensus_check(run_id, retained_index,
                    engine, run_id, role, record, clarified, plan))
            votes[role]['contract_clarification'] = {
                'purpose': active['purpose'] + ':scope-contract-v1',
                'context': clarified, 'schema': copy.deepcopy(active['schema']),
                'instruction': ('按新提供的宿主阶段合同，对同一份调参范围作一次独立澄清。原否决完整保留。'
                    '仅核对原异议是否仍为本阶段实质阻塞，不重写计划，不重新盘点全部105项。'
                    '复用本角色有效回执；必读正文缺口仍须补齐。存在真实问题必须继续否决，'
                    '不能因澄清或宿主字段存在而自动批准。\n' + GUIDANCE + scope_stage_guidance)}
            save(status='reviewing')
            return await read_vote(role)
        async def independent_review(role):
            from .engine import ResearchBlocked
            from .intelligence import ProviderError, BudgetExceeded, PolicyReviewRequired
            from .storage import redact
            try:
                return await review_role(role)
            except (BudgetExceeded, PolicyReviewRequired):
                raise  # Global admission failures still stop the group.
            except (ResearchBlocked, ProviderError) as exc:
                # A role-local unknown receipt must not cancel another already
                # authorized review and create yet another unknown receipt.
                return {'role': role, 'request_error': redact(str(exc))}
        tasks = [asyncio.create_task(independent_review(role)) for role in ROLES]
        try:
            errors = [error for error in await asyncio.gather(*tasks) if error]
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        request_errors = [error for error in errors if error.get('request_error')]
        if request_errors:
            from .engine import ResearchBlocked
            row['request_blockers'] = request_errors
            save(status='blocked_requests')
            raise ResearchBlocked('独立复核请求待恢复；其他角色已完成的结果保留：' + '; '.join(
                error['role'] + ': ' + error['request_error'] for error in request_errors))
        row.pop('request_blockers', None)
        if errors:
            detail = ReadEvidenceError([{'kind': 'role_reading_contract', 'path': error['role'],
                'detail': error['error'], 'issues': error['issues']} for error in errors])
            reading_blocker(row, detail, save)
        answers = [active_vote(v)['answer'] for v in votes.values()]
        if all(v['approve'] and not v['issues'] for v in answers):
            if amendment:
                prior = engine.store.get(COLLECTION, amendment['body']['previous_scope_id'])
                old_plan = verify(engine, run_id, prior)
                fields = ('path', 'variant', 'category', 'disposition', 'values', 'value_kind', 'priority')
                content = lambda p: [{k: f[k] for k in fields} for f in p['factors']]
                if content(old_plan) == content(plan) and old_plan['stopping_rule'] == plan['stopping_rule']:
                    row['error'] = 'Refinement must change an actual scope decision, range, order or stopping rule, not only wording'
                    row['needs_reconciliation'] = True
                    save(status='reconciling')
                    return pending(scope_id=identifier, reason=row['error'])
            save(status='accepted', plan_sha256=sha(plan))
            verify(engine, run_id, record)
            return {'tuning_pending': True, 'next_node': 'tuning', 'activity': 'tuning_scope'}
    row['needs_reconciliation'] = True
    save(status='reconciling')
    return pending(scope_id=identifier, reason='三方存在具体异议，交 director 协调、工程师修订后重新独立投票')
