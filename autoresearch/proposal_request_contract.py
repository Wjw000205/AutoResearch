"""Expose existing execution constraints in new requests, without rewriting history.

This is generation-time guidance, never an execution permission. Persist the
version on a new decision; unversioned decisions keep their frozen requests.
"""
import copy

VERSION = 15


def scope_diagnostic_routes(context, evidence):
    from .architecture_experiment_contract import enabled
    resolution = evidence.get('parameter_resolution') or {}
    if enabled(evidence) and (resolution.get('next_task') or {}).get('kind') != 'diagnose':
        packet = context['experiment_decision']
        if packet.pop('bounded_diagnostic_capabilities', None) is not None:
            packet['bounded_diagnostic_route'] = (
                '未分配经独立审核的专用诊断协议。当前 diagnose/instrument 修改单由普通候选 CPU 检查入口执行。'
                '专用 bounded_host 钩子由参数协议调度器单独派发；协调意见本身不能启用该入口。')

DIAGNOSTIC_ROUTING = (
    'controlled_diagnostic 是执行已有冻结批次的动作，需要可用 unit_id、无源码修改及具体 factor_paths。'
    '若本意是新增 CPU 行为检查，diagnose 应填写 unit_id=null、具体 changes，训练应答用 instrument 或 repair_code；'
    '检查走候选实现、独立代码复核和宿主 CPU 断言入口。受限诊断脚本的专用入口只对单独审核的诊断协议开放。'
    '若行为用例验证通过，仍需回到架构评估；若发现规范偏差，说明受影响代码及同条件复验方式。')


def routed_diagnostics(schema):
    if 'training_response' in schema.get('properties', {}):
        schema.setdefault('allOf', []).append({
            'if': {'properties': {'action': {'const': 'diagnose'}, 'unit_id': {'type': 'null'},
                                  'changes': {'minItems': 1}}, 'required': ['action', 'unit_id', 'changes']},
            'then': {'properties': {'training_response': {'properties': {
                'disposition': {'enum': ['instrument', 'repair_code']}}}}}})
    return schema
GUIDANCE = '''
本次提案请求已绑定原执行契约：complete_optimization 的 criteria 必须逐字段复制
dataset_optimization.architecture_certificate.body.assessment.criteria，包括 rationale 原文。
不得重新总结、翻译说明或改数值。它只描述原规则，不填写本轮结果解释；新解释填 stability_rationale。
architecture_assessment 只用于架构准入或只读取证，不能搭配 complete_optimization。
仍有训练告警时，完成优化必须另有真实证据支持 continue 或 resolved；不能为满足字段约束
把 unknown/hypothesis 改成 supported、把告警标成已解决。缺证时提出有区分力的具体诊断，
或用既有只读取证通道明确尚缺什么与为何阻塞，不重读、重测已经完成的同一项。
'''

GUIDANCE_V2 = '''
新请求已按动作声明必填载荷和可用批次。架构/稳定性评估必须填写 optimization_assessment；
search_parameters 必须填写 parameter_search，factor_path 是参数对象内的相对路径，
不是 /job_parameters 开头的证据路径。执行既有批次只能选择当前 available 的 unit。
不执行、不修改而只读取既有证据时，使用 diagnose、diagnosis=insufficient_evidence、
unit_id=null、changes=[]；有 training_response 时使用 architecture_assessment。
若只缺某些已完成 validation 运行的产物，在 artifact_trial_ids 明列所需 trial ID，
并在 verification 说明待核对问题；其他训练告警仍在原证据中，不因取证范围缩小而解除。
若没有可执行或可区分原因的方案，如实说明缺口，不虚构修改、可用批次或 supported 结论。
这些字段约束不构成批准；实际证据、独立投票、预算、资源和科学门禁仍需分别通过。
'''


def guidance(version):
    if not version:
        return ''
    if version not in range(1, VERSION + 1):
        raise ValueError('Unsupported proposal request contract version')
    return GUIDANCE if version == 1 else GUIDANCE + GUIDANCE_V2


def architecture_iteration(evidence, version):
    from .architecture_experiment_contract import enabled
    return version >= 14 and enabled(evidence) and not evidence.get('hyperparameter_yaml', {}).get('migration_required')


def archived_diagnostic_memory(memory, recent):
    if (not memory or not recent or recent.get('status') not in ('rejected', 'admission_blocked')
            or not recent.get('decision_id') or memory.get('sources', {}).get('decision_id') != recent['decision_id']
            or memory.get('next_action') != 'diagnose'
            or memory.get('training_investigation', {}).get('disposition') not in ('instrument', 'repair_code')):
        return memory
    return {'kind': 'historical_diagnostic_reference',
            'original': {'id': memory['id'], 'sha256': memory['sha256']}, 'sources': memory['sources'],
            'observed': memory.get('observed'), 'review_status': recent['status'], 'scientific_approval': False}


def focus_architecture_context(context):
    packet = context['experiment_decision']
    packet['architecture_revision_workflow'] = 1
    memory = context.get('experiment_memory')
    previous = archived_diagnostic_memory(memory, packet.get('recent_revision'))
    if previous is not memory:
        context['experiment_memory'] = previous
        if (packet.get('previous_decision') or {}).get('action') == 'diagnose':
            packet['previous_decision'] = {'source_decision': previous['sources']['decision_id'],
                                           'review_status': previous['review_status']}
    for key in ('bounded_diagnostic_route', 'bounded_diagnostic_capabilities', 'cpu_behavior_check',
                'prior_diagnostic_review', 'diagnostic_host_checks', 'diagnostic_host_source_locations'):
        packet.pop(key, None)


ARCHITECTURE_ITERATION = """依据本轮已完成的真实验证，评估当前架构并提出下一步。两席独立判断，工程师综合实测证据和实际预测调用链。
列出各任务的验证指标及误差集中条件。论文参照须注明数据、预测任务、划分、尺度和聚合口径。只对口径一致的结果计算正式差距。
先核对输入、预处理、评分和实际训练实现。遇到训练告警，保留真实未知；按 focus_trial_ids 读 experiments.analyze 的训练报告与相关源码，并用原回执引用。复用本角色已有的同版读取。
结果不及预期时，对照保留当前模型与一个具体的单因素机制修改，说明误差模式、对应源码、可解释的影响范围、竞争解释、预期改善、反证和回退条件。
选择架构改动用 revise_architecture、diagnosis=architecture、architecture_review；有训练应答时用 architecture_experiment。证实代码偏离已审设计时用 revise_architecture、diagnosis=implementation、implementation_correction，有训练应答时用 repair_code。
提案获批后更新相应文档，修改实际运行代码、独立复审，再回到实验验证。沿用指定数据集、配置、划分、种子、训练预算及指标，复用已有对照；用新版在相同协议上的真实验证结果判断改动是否改善。
先用现有工具核对版本和实验材料。确实需要宿主提供既有验证产物时，在只读请求中指定试次及待核字段；只有冻结目录中已批准的真实验证批次可以按 unit_id 选择。只有现有证据已满足架构验收条件时才可提请 accept_architecture。
CPU 回归检查随实际代码交付完成。既有分析对旧夹具的建议及代码票作为历史记录；本阶段不另派夹具、不复核宿主指标执行能力。遵守 research_anchor、冻结计划、实际证据引用和独立评审约束。"""


def focus_stage(instruction, evidence, version):
    if architecture_iteration(evidence, version):
        if version >= 15:
            from .architecture_validation_batch import GUIDANCE as validation_guidance
            return ARCHITECTURE_ITERATION + '\n' + validation_guidance
        return ARCHITECTURE_ITERATION
    if version < 13 or (evidence.get('dataset_optimization') or {}).get('phase') != 'architecture':
        return instruction
    if (evidence.get('tuning_scope') or {}).get('status') == 'not_planned':
        from .tuning_scope import GUIDANCE as tuning_guidance
        instruction = instruction.replace('\n' + tuning_guidance, '')
    if (evidence.get('combination_validation') or {}).get('status') == 'waiting_architecture':
        from .combination_settings import SCRIPT_GUIDANCE
        from .combination_validation import GUIDANCE as combination_guidance
        for deferred in (SCRIPT_GUIDANCE, combination_guidance):
            instruction = instruction.replace('\n' + deferred, '')
    return instruction


def decision_roles(instruction, version):
    if version < 5:
        return instruction
    return (instruction.replace('三方独立评审后仅追加该批次', '独立评审通过后仅追加该批次')
            .replace('纠正后仍由三方独立评审', '纠正后的提案仍须独立评审')
            .replace('你的新提案必须重新由三方独立投票', '你的新提案必须重新交由独立评审投票')
            .replace('完整提案、读取、三方投票及执行门禁', '完整提案、读取、独立投票及执行门禁')
            .replace('本轮仅有三方诊断、一个最小方案和三方复核', '本轮先独立诊断，再由工程师提出一个最小方案并交独立复核'))


def frozen_criteria(schema, evidence):
    result = copy.deepcopy(schema)
    criteria = (evidence.get('dataset_optimization', {}).get('architecture_certificate')
                or {}).get('body', {}).get('assessment', {}).get('criteria')
    if criteria:
        result.setdefault('allOf', []).append({
            'if': {'properties': {'action': {'const': 'complete_optimization'}}, 'required': ['action']},
            'then': {'properties': {'optimization_assessment': {'properties': {
                'criteria': {'const': copy.deepcopy(criteria)}}}}}})
    return result


def schema(base, evidence, version, *, ordinary_cpu=False, run_id=None):
    if not version:
        return copy.deepcopy(base)
    if version not in range(1, VERSION + 1):
        raise ValueError('Unsupported proposal request contract version')
    result = frozen_criteria(base, evidence)
    scope = evidence.get('dataset_optimization')
    if scope and scope.get('phase') == 'parameters':
        actions = result['properties']['action']['enum']
        result['properties']['action']['enum'] = [a for a in actions if a != 'accept_architecture']
    if scope and evidence.get('training_investigation', {}).get('required'):
        result.setdefault('allOf', []).append({
            'if': {'properties': {'action': {'const': 'complete_optimization'}}, 'required': ['action']},
            'then': {'properties': {'training_response': {'properties': {
                'disposition': {'enum': ['continue', 'resolved']}}}}, 'required': ['training_response']}})
        result['allOf'].append({
            'if': {'properties': {'training_response': {'properties': {
                'disposition': {'const': 'continue'}}, 'required': ['disposition']}},
                'required': ['training_response']},
            'then': {'properties': {'training_response': {'properties': {
                'cause': {'enum': ['batch_noise', 'telemetry']},
                'cause_status': {'const': 'supported'}}}}}})
    mainline = architecture_iteration(evidence, version)
    result = result if version == 1 else _action_payloads(result, evidence, standalone_checks=not mainline)
    if version >= 3:
        from .architecture_experiment_contract import schema as architecture_schema
        result = architecture_schema(result, evidence, generation=True, implementation_correction=version >= 6)
    if version >= 4:
        bind_search_scope(result, evidence)
        combination = evidence.get('combination_validation') or {}
        step = combination.get('next_step')
        if step and (combination.get('version') == 2 or combination.get('status') == 'validating_combinations'):
            search = result['properties']['parameter_search']
            for field in ('base_unit_id', 'factor_path', 'variant', 'validation_seed', 'scope_sha256',
                          'grid_values', 'lower_bound', 'upper_bound'):
                search['properties'][field] = {'const': copy.deepcopy(step[field])}
            search['properties']['value'] = {'enum': copy.deepcopy(step['pending_values'])}
        if combination:
            excluded = {'search_parameters'} if combination['status'] == 'complete' else {'complete_optimization'}
            if combination.get('version') == 2 and not step:
                excluded.add('search_parameters')
            if combination.get('version') == 2 and combination['status'] in (
                    'waiting_single_factor', 'validating_combinations', 'waiting_measured_batch', 'waiting_trial_budget'):
                excluded.add('grid_search')
            result['properties']['action']['enum'] = [a for a in result['properties']['action']['enum'] if a not in excluded]
    if mainline:
        if version >= 15:
            from .architecture_validation_batch import bind
            bind(result, evidence, run_id)
        # diagnose is still available for reading saved evidence and validated inventory units.
        if 'training_response' in result['properties']:
            choices = result['properties']['training_response']['properties']['disposition']['enum']
            choices.remove('instrument')
        result['allOf'].append({'if': {'properties': {'action': {'const': 'diagnose'}, 'unit_id': {'type':'null'}},
                                        'required': ['action', 'unit_id']}, 'then': {'required':['artifact_trial_ids']}})
        return result
    if version >= 11 and ordinary_cpu and 'training_response' in result['properties']:
        from .diagnostic_resources import bind_schema
        bind_schema(result, measured=version >= 12)
    return routed_diagnostics(result) if version >= 8 else result


def bind_search_scope(result, evidence):
    """Match the scoped executor, retaining the optimizer rule for unscoped requests."""
    from .tuning_scope import safe_path
    from .experiment_workflow import sha
    scope = evidence.get('tuning_scope') or {}
    search = result.get('properties', {}).get('parameter_search')
    if not search or scope.get('status') != 'accepted':
        return
    plan = scope.get('plan') or {}
    if sha(plan) != scope.get('plan_sha256'):
        raise ValueError('The reviewed tuning plan changed before proposal generation')
    by_variant = {}
    for factor in plan['factors']:
        if factor['disposition'] == 'search' and safe_path(factor['path']):
            by_variant.setdefault(factor['variant'], []).append(factor['path'])
    if not by_variant:
        result['properties']['parameter_search'] = False
        return
    search['properties']['factor_path'] = {'enum': sorted({p for paths in by_variant.values() for p in paths})}
    search['properties']['scope_sha256'] = {'const': scope['plan_sha256']}
    search['required'] = list(dict.fromkeys([*search['required'], 'scope_sha256', 'variant']))
    search['anyOf'] = [{'properties': {'variant': {'const': variant}, 'factor_path': {'enum': sorted(paths)}}}
                       for variant, paths in sorted(by_variant.items())]


def _action_payloads(result, evidence, *, standalone_checks=True):
    """Mirror structural execution preconditions, without deciding scientific merit."""
    from .parameter_search import ALLOWED, PARENTS
    from .experiment_evidence_handoff import SCOPE_SCHEMA, scope_ids

    def when(actions, properties, required=()):
        result.setdefault('allOf', []).append({
            'if': {'properties': {'action': {'enum': actions}}, 'required': ['action']},
            'then': {'properties': properties, **({'required': list(required)} if required else {})}})

    empty = {'maxItems': 0}
    null = {'type': 'null'}
    result['properties']['artifact_trial_ids'] = copy.deepcopy(SCOPE_SCHEMA)
    if 'records' in evidence:
        ids = scope_ids(evidence)
        result['properties']['artifact_trial_ids']['items'] = {'enum': ids} if ids else False
    result.setdefault('allOf', []).append({
        'if': {'required': ['artifact_trial_ids']},
        'then': {'properties': {'action': {'const': 'diagnose'},
            'diagnosis': {'const': 'insufficient_evidence'}, 'unit_id': null, 'changes': empty,
            'training_response': {'properties': {'disposition': {'const': 'architecture_assessment'}}}}}})
    when(['accept_architecture', 'complete_optimization'], {
        'unit_id': null, 'changes': empty,
        'optimization_assessment': {'properties': {'remaining_architecture_issues': empty}}},
        ['optimization_assessment'])
    when(['accept_architecture'], {'optimization_assessment': {
        'properties': {'selected_unit_id': null}}})
    when(['complete_optimization'], {'optimization_assessment': {
        'properties': {'selected_unit_id': {'type': 'string', 'minLength': 1}}}})
    when(['search_parameters'], {'unit_id': null, 'changes': empty,
        'diagnosis': {'enum': ['training', 'no_architecture_evidence']}}, ['parameter_search'])
    when([a for a in result['properties']['action']['enum'] if a != 'search_parameters'],
         {'parameter_search': False})
    when(['stop', 'review'], {'unit_id': null, 'changes': empty})
    when(['revise_architecture'], {'unit_id': null, 'changes': {'minItems': 1}})
    when(['grid_search'], {'unit_id': {'type': 'string'}, 'changes': empty,
                           'diagnosis': {'const': 'no_architecture_evidence'}})
    # There are three distinct diagnostic routes: a frozen batch, concrete local
    # changes, or a read-only evidence request. An empty execution is never one.
    when(['diagnose'], {})
    result['allOf'][-1]['then'] = {'oneOf': [
        {'properties': {'unit_id': {'type': 'string'}, 'changes': empty}},
        *([{'properties': {'unit_id': null, 'changes': {'minItems': 1}}}] if standalone_checks else []),
        {'properties': {'unit_id': null, 'changes': empty,
                        'diagnosis': {'const': 'insufficient_evidence'},
                        'training_response': {'properties': {
                            'disposition': {'const': 'architecture_assessment'}}}}}]}
    search = result['properties'].get('parameter_search')
    if search:
        # Use the executor's allowlists; do not change the historical base schema.
        search['properties']['factor_path']['pattern'] = (
            r'^/(?:(?:' + '|'.join(sorted(PARENTS)) + r')/){0,2}(?:'
            + '|'.join(sorted(ALLOWED)) + r')$(?![\s\S])')
    if 'units' in evidence:
        available = [u for u in evidence['units'] if u.get('available')]
        def identifiers(rows):
            ids = sorted({u['id'] for u in rows})
            return {'enum': ids} if ids else False
        # null preserves the two non-executing-unit diagnostic routes.
        when(['diagnose'], {'unit_id': {'anyOf': [null, identifiers(available)]}})
        when(['grid_search'], {'unit_id': identifiers(
            u for u in available if u.get('kind') in {'pilot', 'grid'} and any(
                v.get('comparable') and v.get('task_id') == u.get('task_id')
                for v in evidence.get('comparisons', {}).values()))})
    scope = evidence.get('dataset_optimization')
    excluded = ({'revise_architecture'} if scope and scope.get('architecture_frozen') else set())
    if not scope:
        excluded.update({'accept_architecture', 'complete_optimization'})
    result['properties']['action']['enum'] = [
        a for a in result['properties']['action']['enum'] if a not in excluded]
    return result
