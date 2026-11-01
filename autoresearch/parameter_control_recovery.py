"""Revalidate an unchanged authored search rejected by alert-only control lookup."""
import copy

from .prose_schema import validate

from .experiment_workflow import sha

KEY = 'parameter_control_recovery'
ERROR = ('Every diagnostic job needs a same-seed control differing only in its declared factors; '
         'generic ablation is insufficient')


async def recover(engine, run_id, record, evidence, ref, checkpoint):
    from .engine import Harness, digest
    from .experiment_decisions import validate_proposal
    from .parameter_search import control_records

    proposal = record.get('rejected_proposal') or {}
    if (record.get('status') != 'invalid' or record.get('rejection') != ERROR
            or record.get('proposal') or record.get('votes')
            or proposal.get('action') != 'search_parameters'
            or record.get('evidence') != ref or sha(evidence) != ref['sha256']
            or proposal.get('evidence_sha256') != ref['sha256']):
        return None
    # Recover only the authenticated, already completed same-author answer.
    prefix = 'experiment_decide:' + ref['sha256']
    keys = engine.store.record_keys('decisions', run_id,
        predicate="json_extract(data,'$.role')=? AND json_extract(data,'$.purpose') LIKE ?",
        parameters=('engineer', prefix + '%'))
    for hid in reversed(keys):
        answer = engine.store.get('decisions', hid)
        if answer.get('result') != proposal:
            continue
        assignment = engine.store.get('handoffs', hid, {})
        inputs = engine.store.get('handoff_inputs', hid, {})
        payload = {k: inputs.get(k) for k in ('instruction', 'context', 'schema')}
        purpose = answer.get('purpose', '')
        if (purpose != prefix and not purpose.startswith(prefix + ':')
                or assignment.get('id') != hid or assignment.get('run_id') != run_id
                or assignment.get('to_role') != 'engineer' or assignment.get('purpose') != purpose
                or assignment.get('status') != 'completed' or assignment.get('output_hash') != digest(proposal)
                or inputs.get('id') != hid or inputs.get('run_id') != run_id
                or assignment.get('input_hash') != digest(payload)
                or Harness.decision_key(run_id, 'engineer', purpose, **payload) != hid
                or (payload['context'] or {}).get('validation_evidence') != ref):
            raise ValueError('Parameter control recovery lost its original author/evidence binding')
        validate(proposal, payload['schema'])
        validate_proposal(proposal, evidence, ref['sha256'])
        controls = control_records(proposal['parameter_search'], evidence)
        audit = {'version': 1, 'author_handoff_id': hid, 'proposal_sha256': sha(proposal),
                 'evidence_sha256': ref['sha256'], 'original_rejection': ERROR,
                 'base_unit_id': proposal['parameter_search']['base_unit_id'],
                 'controls': [{'trial_id': r['id'], 'params_sha256': sha(r['params']),
                               'seed': r['params']['seed'], 'variant': r['params']['variant']}
                              for r in controls],
                 'scope': 'Frozen paired controls are separate from mandatory training-alert focus; original answer unchanged.',
                 'approval_inherited': False, 'execution_authorized': False}
        if record.get(KEY) and record[KEY] != audit:
            raise ValueError('Parameter control recovery audit changed')
        if not record.get(KEY):
            await checkpoint(**{KEY: audit})
        return copy.deepcopy(proposal)
    return None
