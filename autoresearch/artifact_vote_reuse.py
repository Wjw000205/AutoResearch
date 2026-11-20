"""Reuse same-role pages from the reviewed predecessor of a parameter decision."""
from . import artifact_review_pages as pages
from .diagnostic_planning_recovery import authenticate
from .experiment_evidence_handoff import KEY
from .experiment_workflow import sha


def _summary(store, run_id, ref):
    row = store.get('experiment_evidence', ref['id']) or {}
    if (row.get('id') != ref['id'] or row.get('run_id') != run_id
            or row.get('sha256') != ref['sha256'] or sha(row.get('summary')) != ref['sha256']):
        raise ValueError('Artifact read reuse lost its frozen evidence')
    return row['summary']


def predecessor(engine, run_id, context):
    """Follow only the resolution published in the voter's own frozen request."""
    decision = context.get('experiment_decision', {})
    link = decision.get('parameter_resolution') or {}
    task_id = link.get('successor_task_id')
    if not task_id:
        return None
    store = engine.store
    ref = context['validation_evidence']
    current = _summary(store, run_id, ref)
    from .parameter_resolution import COLLECTION, compact
    resolution = current.get('parameter_resolution') or {}
    if (not resolution or ref['sha256'] != context.get('evidence_sha256')
            or compact(resolution) != link or not context.get('decision_proposal')
            or sha(context['decision_proposal']) != context.get('proposal_sha256')):
        return None
    published = next((r for r in link['reviews'] if r['id'] == task_id), None)
    task = store.get(COLLECTION, task_id) or {}
    if (not published or task.get('run_id') != run_id or task.get('status') != 'accepted'
            or task.get('evidence') != published['evidence'] or task.get('plan_sha256') != published['plan_sha256']):
        return None
    parent = store.get('experiment_decisions', task['source_decision_id']) or {}
    if (parent.get('id') != task['source_decision_id'] or parent.get('run_id') != run_id
            or parent.get('cycle') != context.get('research_cycle') or parent.get('evidence') != published['evidence']):
        return None
    review = (parent.get(KEY) or {}).get('independent_review') or {}
    if not review.get('results'):
        return None
    prior = review['assignment']['context']
    if prior.get('validation_evidence') != parent['evidence'] or prior.get('evidence_sha256') != parent['evidence']['sha256']:
        return None
    old = prior['experiment_decision'][KEY]
    new = decision[KEY]
    if (context['decision_proposal'].get('artifact_evidence_sha256') != new['evidence']['sha256']
            or not set(new['required_paths']).issubset(old['required_paths'])):
        return None
    previous = _summary(store, run_id, old['evidence'])
    supplied = _summary(store, run_id, new['evidence'])
    original = _summary(store, run_id, parent['evidence'])
    if (previous.get('source_evidence') != parent['evidence'] or supplied.get('source_evidence') != ref
            or 'binding' not in current or supplied.get('binding') != current['binding']
            or original.get('binding') != current['binding']
            or sha({k: v for k, v in supplied.items() if k != 'source_evidence'})
            != sha({k: v for k, v in previous.items() if k != 'source_evidence'})):
        return None
    return review, old['evidence'], new


def role_read(engine, run_id, role, source):
    review, old_ref, new = source
    answer = next(row for row in review['results'] if row['role'] == role)
    request = pages.assignment(review, role)
    owner = authenticate(engine, run_id, role, request['purpose'],
        {k: request[k] for k in ('instruction', 'context', 'schema')}, {k: v for k, v in answer.items() if k != 'role'})
    proof = pages.verified_reads(engine, run_id, role, review)
    if proof != next(r for r in review['reads'] if r['role'] == role) or proof['handoff_id'] != owner:
        raise ValueError('Original independent review or page receipts changed')
    # Only carry the verified supplemental pages. Primary analyses and votes
    # have different inputs and cannot be credited to this decision.
    verified = set(proof['receipt_sha256s'])
    receipts = set()
    for hid in [*proof.get('prior_handoff_ids', []), owner]:
        for row in engine.store.get('handoff_progress', hid)['tool_results']:
            call, result = row.get('call', {}), row.get('result') or {}
            args = call.get('arguments', {})
            if (call.get('name') == 'experiments.compare' and args.get('evidence_id') == old_ref['id']
                    and args.get('expected_sha256') == old_ref['sha256'] and args.get('path') in new['required_paths']
                    and result.get('receipt_sha256') in verified):
                receipts.add(result['receipt_sha256'])
    return {'role': role, 'handoff_id': owner, 'evidence_sha256': new['evidence']['sha256'],
            'source_artifact_evidence': old_ref, 'paths': new['required_paths'], 'receipt_sha256s': sorted(receipts),
            'analysis_kinds': [], 'source_paths': [], 'read_origin': 'unchanged_predecessor_artifact',
            'approval_inherited': False}
