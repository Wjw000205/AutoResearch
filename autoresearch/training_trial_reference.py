"""One bounded author correction for the former ambiguous trial-ID error."""
import copy

from .experiment_workflow import sha

LEGACY_ERROR = 'Training response must cover the current focus, including baseline incidents or new verification trials'


def eligible(record):
    return (record.get('status') == 'invalid' and record.get('rejection') == LEGACY_ERROR
            and bool(record.get('rejected_proposal', {}).get('training_response'))
            and not record.get('proposal_trial_reference_recovery')
            and not record.get('proposal_contract_recovery'))


def request(record, evidence, context, instruction, base_schema):
    from .training_investigation import proposal_schema
    rejected = copy.deepcopy(record['rejected_proposal'])
    known = sorted(r['id'] for r in evidence['records'])
    active = evidence['training_investigation']
    required = active['focus_trial_ids'] or (active.get('followup') or {}).get('new_trial_ids', [])
    schema = proposal_schema(base_schema, evidence)
    for field, value in rejected.items():
        if field != 'training_response':
            schema['properties'][field] = {'const': value}
    response = schema['properties']['training_response']
    for field, value in rejected['training_response'].items():
        if field != 'trial_ids':
            response['properties'][field] = {'const': value}
    response['properties']['trial_ids']['items'] = {'enum': known}
    response['properties']['trial_ids']['allOf'] = [{'contains': {'const': t}} for t in required]
    corrected_context = copy.deepcopy(context)
    text = ('仅纠正 training_response.trial_ids，其余字段保持最后原答复。'
        '必须包含 required_trial_ids，只能从 allowed_trial_ids 选择。'
        '独立受控检查属于 evidence_paths 引用，不属于当前训练 /records；移除 outside_trial_ids。'
        '这是已有证据的编号分类错误，无需补日志、重新读取或重跑实验。修正后仍须三方独立投票。')
    corrected_context['experiment_decision']['proposal_correction'] = {
        'rejected_proposal': rejected, 'validation_error': record['rejection'],
        'instruction': text, 'allowed_trial_ids': known, 'required_trial_ids': required,
        'outside_trial_ids': sorted(set(rejected['training_response']['trial_ids']) - set(known))}
    return {'version': 1, 'rejected_proposal_sha256': sha(rejected),
            'purpose': 'experiment_decide:' + context['evidence_sha256'] + ':trial-reference-1:' + sha(rejected),
            'instruction': instruction + '\n' + text, 'context': corrected_context, 'schema': schema}
