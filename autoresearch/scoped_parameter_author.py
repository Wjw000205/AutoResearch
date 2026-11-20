"""Give a completed artifact author a fresh proposal contract if its reviewed factor was inexpressible."""
import copy

from jsonschema import Draft202012Validator

from . import experiment_evidence_handoff as handoff
from .diagnostic_planning_recovery import authenticate
from .experiment_workflow import sha
from .proposal_request_contract import bind_search_scope

KEY = 'scoped_parameter_author'
REFERENCE = 'scoped_trial_reference_owner'


def successor(engine, run_id, record, evidence, request, answer):
    if (not request or KEY in request['context']['experiment_decision']
            or record.get('proposal_request_contract_version') not in (2, 3)
            or not handoff.requested(answer)
            or (evidence.get('parameter_resolution', {}).get('next_task') or {}).get('kind') != 'search_parameters'
            or evidence.get('tuning_scope', {}).get('status') != 'accepted'
            or not request['purpose'].startswith('experiment_decide:')
            or ':artifact-evidence-1:' not in request['purpose']):
        return None
    search = request['schema']['properties'].get('parameter_search')
    if not search:
        return None
    previous_paths = Draft202012Validator(search['properties']['factor_path'])
    statement = ' '.join(answer.get(k, '') for k in ('rationale', 'verification', 'stop_condition'))
    if not any(f['disposition'] == 'search' and f['path'] in statement
               and not previous_paths.is_valid(f['path']) for f in evidence['tuning_scope']['plan']['factors']):
        return None
    payload = {k: request[k] for k in ('instruction', 'context', 'schema')}
    owner = authenticate(engine, run_id, 'engineer', request['purpose'], payload, answer)
    read = handoff.require_reads(engine, run_id, 'engineer', **request)
    marker = {'version': 1, 'original_handoff': owner, 'original_answer_sha256': sha(answer),
              'verified_reads_sha256': sha(read), 'completed_paths': read['paths'],
              'scope_sha256': evidence['tuning_scope']['plan_sha256'], 'execution_authorized': False}
    updated = copy.deepcopy(payload)
    bind_search_scope(updated['schema'], evidence)
    updated['context']['experiment_decision'][KEY] = marker
    updated['instruction'] = ('接续你已提交的产物核验。原要求的读取和回执已核实。现在 parameter_search.factor_path '
        '与三方已审 tuning_scope 一致。自行使用 experiments.files 查询需要的项目源码、已保存 YAML 版本和验证产物，'
        '按 read_call 读取原文并核对控制与候选。落实 parameter_resolution.next_task，提交一个真实可校验的提案，'
        '绑定当前证据和补充快照。已有观测及其局限继续有效。具体候选仍须独立投票及资源准入。')
    return {**updated, 'purpose': request['purpose'] + ':scoped-proposal-4:' + sha(marker)}


def trial_reference_request(engine, run_id, request, answer, evidence, error):
    """Let the author correct just its trial references on a delivered scoped proposal."""
    from .training_trial_reference import LEGACY_ERROR
    if (not request or not error.startswith(LEGACY_ERROR)
            or KEY not in request['context'].get('experiment_decision', {})
            or REFERENCE in request['context']['experiment_decision'] or answer.get('action') != 'search_parameters'):
        return None
    owner = authenticate(engine, run_id, 'engineer', request['purpose'],
                         {k: request[k] for k in ('instruction', 'context', 'schema')}, answer)
    active = evidence['training_investigation']
    required = active.get('focus_trial_ids') or (active.get('followup') or {}).get('new_trial_ids', [])
    known = sorted(r['id'] for r in evidence['records'])
    updated = copy.deepcopy(request)
    schema = updated['schema']
    response = schema['properties']['training_response']
    schema['properties'] = {k: {'const': v} for k, v in answer.items()}
    schema['properties']['training_response'] = response
    schema['required'] = list(answer)
    old = answer['training_response']
    trial_ids = response['properties']['trial_ids']
    response['properties'] = {k: {'const': v} for k, v in old.items()}
    response['properties']['trial_ids'] = trial_ids
    response['required'] = list(old)
    trial_ids.update(items={'enum': known}, allOf=[{'contains': {'const': t}} for t in required])
    updated['context']['experiment_decision'][REFERENCE] = owner
    updated['context']['experiment_decision']['proposal_correction'] = {
        'rejected_proposal': answer, 'validation_error': error, 'required_trial_ids': required}
    updated['purpose'] += ':trial-references:' + sha(answer)
    updated['instruction'] = ('接续你本人刚提交的参数提案。仅补正 training_response.trial_ids，覆盖当前冻结证据'
        '要求讨论的试验记录，保留候选、控制和其他原答复字段。查看 proposal_correction 和已知记录；'
        '需核实时自行选择工具读取。引用相关历史不扩大本次训练批次，仍须独立审批。')
    return updated


def original_reads(engine, run_id, record, request, marker):
    """Only the same author's authenticated pages for the exact two snapshots carry over."""
    owner = marker['original_handoff']
    inputs = engine.store.get('handoff_inputs', owner) or {}
    prior = {k: inputs[k] for k in ('instruction', 'context', 'schema')}
    prior['purpose'] = engine.store.get('handoffs', owner)['purpose']
    answer = engine.store.get('decisions', owner)['result']
    ref = record['evidence']
    archive = engine.store.get('experiment_evidence', ref['id']) or {}
    if (archive.get('run_id') != run_id or sha(archive.get('summary')) != ref['sha256']
            or prior['context']['validation_evidence'] != ref
            or prior['context']['experiment_decision'][handoff.KEY]['evidence'] != record[handoff.KEY]['evidence']):
        raise ValueError('Scoped proposal lost its original snapshot')
    expected = successor(engine, run_id, record, archive['summary'], prior, answer)
    decision_context = request['context']['experiment_decision']
    if expected and REFERENCE in decision_context:
        submitted = engine.store.get('decisions', decision_context[REFERENCE]) or {}
        expected = trial_reference_request(engine, run_id, expected, submitted['result'], archive['summary'],
                                           decision_context['proposal_correction']['validation_error'])
    if expected != request:
        raise ValueError('Scoped proposal lost its verified original artifact assignment')
    return handoff.require_reads(engine, run_id, 'engineer', **prior)
