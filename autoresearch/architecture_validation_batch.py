"""Pair a missing frozen validation ablation with its measured control."""
import copy

from .experiment_analysis import _param_diff
from .experiment_workflow import sha
from .architecture_validation_comparison import eligible


GUIDANCE = ('training_response.trial_ids 引用全部当前训练告警；artifact_trial_ids 只限定需要补读的产物。'
    '真实消融可用 diagnose 加已冻结的 unit_id、changes=[]。有训练应答时用 coverage_validation，'
    '承认训练根因仍未知，在 factor_paths 列出该批相对于已完成同种子模型的全部参数差异。'
    '对照用于检验架构机制，混杂项须列明。跑完后用 experiments.analyze(kind=ablation) 指定消融及对照的 '
    'unit_id 和 reference_unit_id，读取同种子指标、配置差异与划分，再判断是否改善。')
CORRECTION = 'architecture_batch_correction'


def pairs(evidence, unit, run_id):
    """Reuse the ledger's validated controls. Every missing job needs a matching control."""
    if not unit or unit['kind'] != 'ablation' or not unit['available'] or unit['complete']:
        return []
    ledger = evidence['ledger']['jobs']
    jobs = {j['name']: j for j in ledger}
    parameters = evidence['job_parameters']
    rows = {r['id']: r for r in evidence['records']}
    result = []
    for name in unit['jobs']:
        candidate = jobs[name]
        if candidate['state'] == 'completed':
            continue
        if (candidate['state'] != 'missing' or candidate['variant'] != 'ablation'
                or parameters[name].get('phase') != 'validation'):
            return []
        matches = []
        for control in ledger:
            if (control['state'] != 'completed' or control['variant'] != 'proposed'
                    or control['task_id'] != candidate['task_id'] or control['seed'] != candidate['seed']):
                continue
            reference = parameters[control['name']]
            row = rows[control['trial_ids'][0]]
            if (not eligible(row, run_id, evidence['binding'])
                    or row.get('provenance') != 'real_experiment_execution'
                    or row['params'] != reference
                    or any(reference.get(k) != parameters[name].get(k)
                           for k in ('phase', 'dataset', 'dataset_assets', 'data', 'comparison_task'))):
                continue
            paths = [v['path'] for v in _param_diff(reference, parameters[name])]
            if paths:
                matches.append({'trial_id': control['trial_ids'][0], 'factor_paths': paths})
        if not matches:
            return []
        result.append({'job': name, 'controls': matches})
    return result


def bind(schema, evidence, run_id):
    active = evidence.get('training_investigation') or {}
    if not active.get('required'):
        unavailable = [u['id'] for u in evidence.get('units', []) if u.get('available')
                       and u['kind'] == 'ablation' and not pairs(evidence, u, run_id)]
        if unavailable:
            schema.setdefault('allOf', []).append({'not': {'properties': {
                'action': {'const': 'diagnose'}, 'unit_id': {'enum': unavailable}},
                'required': ['action', 'unit_id']}})
        return
    response = schema['properties']['training_response']['properties']
    focus = active.get('focus_trial_ids') or (active.get('followup') or {}).get('new_trial_ids', [])
    ids = {**response['trial_ids'], 'items': {'enum': [r['id'] for r in evidence['records']]}}
    if focus:
        ids['allOf'] = [{'contains': {'const': t}} for t in focus]
    response['trial_ids'] = ids
    batches = []
    for unit in evidence.get('units', []):
        scope = pairs(evidence, unit, run_id)
        if scope:
            # A proposal uses one factor list across all seeds in this unit.
            common = set.intersection(*[{tuple(c['factor_paths']) for c in row['controls']} for row in scope])
            for paths in sorted(common):
                controls = [{'contains': {'enum': [c['trial_id'] for c in row['controls']
                    if tuple(c['factor_paths']) == paths]}} for row in scope]
                batches.append({'properties': {'unit_id': {'const': unit['id']}, 'training_response': {
                    'properties': {'disposition': {'const': 'coverage_validation'}, 'cause': {'const': 'unknown'},
                        'cause_status': {'const': 'hypothesis'}, 'trial_ids': {'allOf': controls},
                        'factor_paths': {'items': {'enum': list(paths)}, 'minItems': len(paths),
                                         'maxItems': len(paths)}}}}})
    schema.setdefault('allOf', []).append({'if': {'properties': {'action': {'const': 'diagnose'},
        'unit_id': {'type': 'string'}}, 'required': ['action', 'unit_id']},
        'then': {'anyOf': batches} if batches else False})


def validate(proposal, evidence, run_id):
    response = proposal['training_response']
    unit = next((u for u in evidence['units'] if u['id'] == proposal['unit_id']), None)
    scope = pairs(evidence, unit, run_id)
    if (proposal['action'] != 'diagnose' or proposal['changes'] or not scope
            or response['cause'] != 'unknown' or response['cause_status'] != 'hypothesis'
            or not all(any(c['trial_id'] in response['trial_ids'] and
                           set(c['factor_paths']) == set(response['factor_paths']) for c in row['controls'])
                       for row in scope)):
        raise ValueError('Architecture validation requires a missing frozen ablation, a measured same-task/seed '
                         'control for every job, exact parameter differences and an unresolved training cause')


def correction(engine, run_id, request, answer, evidence, error):
    from .diagnostic_planning_recovery import authenticate
    from .experiment_evidence_handoff import KEY, require_reads
    from .training_trial_reference import LEGACY_ERROR
    if (not request or CORRECTION in request['context']['experiment_decision']
            or KEY not in request['context']['experiment_decision'] or answer.get('action') != 'diagnose'
            or not answer.get('unit_id') or not (error.startswith(LEGACY_ERROR)
                or error.startswith('Architecture assessment cannot authorize training'))):
        return None
    parent = authenticate(engine, run_id, 'engineer', request['purpose'],
                          {k: request[k] for k in ('instruction', 'context', 'schema')}, answer)
    require_reads(engine, run_id, 'engineer', **request)
    child = copy.deepcopy(request)
    bind(child['schema'], evidence, run_id)
    unit = next((u for u in evidence['units'] if u['id'] == answer['unit_id']), None)
    child['context']['experiment_decision'][CORRECTION] = {
        'parent': parent, 'answer': answer, 'rejection': error, 'execution_authorized': False,
        'selected_batch_controls': pairs(evidence, unit, run_id)}
    child['instruction'] += '\n' + GUIDANCE + ' 原产物读取已核验。核对退回原因，自行修订真实验证提案或提出架构改动，交独立评审。'
    child['purpose'] += ':architecture-validation-v15:' + sha(answer)
    return child
