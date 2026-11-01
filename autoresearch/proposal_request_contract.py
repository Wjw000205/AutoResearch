"""Expose existing execution constraints in new requests, without rewriting history.

This is generation-time guidance, never an execution permission. Persist the
version on a new decision; unversioned decisions keep their frozen requests.
"""
import copy

VERSION = 3
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
    if version not in (1, 2, 3):
        raise ValueError('Unsupported proposal request contract version')
    return GUIDANCE if version == 1 else GUIDANCE + GUIDANCE_V2


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


def schema(base, evidence, version):
    if not version:
        return copy.deepcopy(base)
    if version not in (1, 2, 3):
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
    result = result if version == 1 else _action_payloads(result, evidence)
    if version >= 3:
        from .architecture_experiment_contract import schema as architecture_schema
        result = architecture_schema(result, evidence, generation=True)
    return result


def _action_payloads(result, evidence):
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
        {'properties': {'unit_id': null, 'changes': {'minItems': 1}}},
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
