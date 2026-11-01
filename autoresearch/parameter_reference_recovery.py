"""Read-only audit of a search rejected before its candidate was constructed."""
import copy

from jsonschema import ValidationError

from .experiment_workflow import sha

KEY = 'parameter_reference_recovery'


def audit(record, evidence, ref):
    from .parameter_search import propose, relative_path_hint
    from .experiment_analysis import _param_diff
    from .experiment_decisions import validate_proposal
    attempts = record.get('proposal_corrections', [])
    if not attempts:
        return None
    attempt = attempts[-1]
    proposal = attempt['proposal']
    spec = proposal.get('parameter_search') or {}
    if (proposal.get('action') != 'search_parameters'
            or proposal.get('evidence_sha256') != ref['sha256']
            or not attempt['error'].startswith('Search may change one existing optimizer scalar only')):
        return None
    relative = relative_path_hint(spec, evidence)
    if not relative:
        return None
    corrected = copy.deepcopy(proposal)
    corrected['parameter_search']['factor_path'] = relative
    response = corrected.get('training_response')
    if response:
        if response.get('factor_paths') != [spec['factor_path']]:
            return None
        response['factor_paths'] = [relative]
    try:
        unit = propose(corrected['parameter_search'], evidence)
    except (ValueError, KeyError, TypeError, ValidationError):
        return None  # Not a verified path-only configuration repair.
    base = next(u for u in evidence['units'] if u['id'] == spec['base_unit_id'])
    pairs = []
    for name, params in unit['jobs']:
        controls = [(n, evidence['job_parameters'][n]) for n in base['jobs']
                    if evidence['job_parameters'][n]['seed'] == params['seed']
                    and evidence['job_parameters'][n]['variant'] == params['variant']]
        if len(controls) != 1:
            return None
        control_name, control = controls[0]
        differences = _param_diff(control, params)
        if [d['path'] for d in differences] != [relative]:
            return None
        pairs.append({'control_job': control_name, 'prospective_job': name,
                      'seed': params['seed'], 'variant': params['variant'],
                      'control_sha256': sha(control), 'prospective_sha256': sha(params),
                      'differences': differences, 'control': copy.deepcopy(control),
                      'prospective': copy.deepcopy(params)})
    remaining_gate = None
    try:
        validate_proposal(corrected, evidence, ref['sha256'])
    except (ValueError, KeyError, IndexError, TypeError, ValidationError) as exc:
        remaining_gate = str(exc)
    return {'binding': copy.deepcopy(evidence['binding']), 'source_evidence': copy.deepcopy(ref),
            'trials': [], 'parameter_search_audit': {
                'rejected_proposal_sha256': sha(proposal), 'original_error': attempt['error'],
                'original_factor_path': spec['factor_path'], 'parameter_relative_path': relative,
                'rejection_stage': 'path_validation_before_candidate_construction',
                'original_candidate_constructed': False,
                'scope': 'Prospective configurations reconstructed from frozen controls, not historical execution receipts.',
                'corrected_fields': {'parameter_search.factor_path': relative,
                                     **({'training_response.factor_paths': [relative]} if response else {})},
                'pairs': pairs, 'remaining_proposal_gate': remaining_gate,
                'execution_authorized': False, 'scientific_approval': False},
            'execution_authorized': False, 'scientific_approval': False}


async def recover(engine, run_id, record, evidence, ref, checkpoint):
    """Replace a misrouted handoff once, retaining its frozen material and answer."""
    from . import experiment_evidence_handoff as handoff
    saved = record.get(handoff.KEY)
    rejected = record.get('rejected_proposal') or {}
    if (record.get(KEY) or record.get('proposal') or record.get('votes')
            or record.get('status') != 'invalid' or not saved
            or saved.get('independent_review') or saved.get('kind') == 'parameter_search_audit'
            or not record.get('rejection', '').startswith('Decision cites missing evidence')
            or not handoff.requested(rejected)):
        return
    # The old snapshot and answer must still be bound to this exact decision.
    handoff.bound_evidence(engine, run_id, record, rejected)
    material = audit(record, evidence, ref)
    if not material:
        return
    retained = {'previous_handoff': copy.deepcopy(saved),
                'previous_rejected_proposal': copy.deepcopy(rejected),
                'previous_rejection': record['rejection'],
                'audit_sha256': sha(material), 'execution_authorized': False}
    detached = {k: v for k, v in record.items() if k != handoff.KEY}

    async def retain_and_checkpoint(**changes):
        await checkpoint(**changes, **{KEY: retained})

    await handoff.prepare(engine, run_id, detached, saved['request'], evidence, ref,
                          retain_and_checkpoint)
