"""An explicit prompt upgrade continues completed fact reviews in a new record."""
import copy

from .experiment_workflow import sha
from .storage import now

COLLECTION = 'architecture_prompt_successors'


def register(engine, run_id, parent_id, instruction):
    from .experiment_evidence_handoff import KEY, require_reads
    from .architecture_experiment_contract import enabled
    from .record_archive import fingerprint, save
    from .proposal_request_contract import VERSION
    run = engine.store.get('runs', run_id) or {}
    parent = engine.store.get('experiment_decisions', parent_id) or {}
    if engine.store.get(COLLECTION, parent_id):
        return engine.store.get('experiment_decisions', route(engine, run_id, parent_id, parent['evidence']))
    evidence = engine.store.get('experiment_evidence', parent.get('evidence', {}).get('id', '')) or {}
    review = parent.get(KEY, {}).get('independent_review') or {}
    if (not instruction.strip() or run.get('status') not in {'paused', 'blocked'}
            or parent.get('run_id') != run_id or parent.get('proposal') or parent.get('votes')
            or parent.get('cycle') != engine.cycle(run_id) or not enabled(evidence.get('summary', {}))
            or evidence.get('run_id') != run_id or sha(evidence.get('summary')) != parent['evidence']['sha256']
            or {r['role'] for r in review.get('results', [])} != {'director', 'engineer', 'scout'}):
        raise ValueError('Prompt upgrade needs an explicit paused architecture decision with completed facts and no approval')
    with engine.store._read_db() as db:
        active = db.execute("SELECT key FROM records WHERE collection IN ('experiments','handoffs') "
            "AND json_extract(data,'$.run_id')=? AND json_extract(data,'$.status')='running' LIMIT 1", (run_id,)).fetchone()
    if active:
        raise ValueError('Settle current paid work before upgrading the architecture prompt')
    from .diagnostic_planning_recovery import authenticate
    assignment = review['assignment']
    for answer in review['results']:
        authenticate(engine, run_id, answer['role'], assignment['purpose'],
            {k: assignment[k] for k in ('instruction', 'context', 'schema')},
            {k: v for k, v in answer.items() if k != 'role'})
        require_reads(engine, run_id, answer['role'], **assignment)
    source = fingerprint(engine.store, 'experiment_decisions', parent_id)
    archive = save(engine.store, run_id, source)
    child_id = parent_id + ':architecture-contract-3:' + sha([source['sha256'], instruction])
    child = copy.deepcopy(parent)
    for field in ('recovery_answer', 'rejected_proposal', 'rejection', 'proposal_corrections',
                  'proposal_contract_recovery', 'proposal_trial_reference_recovery', 'proposal_contract_version',
                  'training_pending_proposal', 'training_read_dispatch', 'training_proposal_reads',
                  'optimization_read_repairs', 'consensus_parent', 'consensus_resolution'):
        child.pop(field, None)
    child.update(id=child_id, status='proposing', created_at=now(), updated_at=now(),
        proposal_request_contract_version=VERSION,
        architecture_prompt_parent={'id': parent_id, 'sha256': source['sha256']})
    child[KEY]['independent_review'].pop('synthesis_assignment', None)
    child[KEY].update(status='synthesizing', owner='engineer')
    body = {'run_id': run_id, 'parent': source, 'parent_archive': archive, 'child_id': child_id,
        'evidence': parent['evidence'], 'instruction': instruction, 'request_contract_version': VERSION,
        'retained_facts_sha256': sha(review['results']), 'execution_authorized': False, 'scientific_approval': False}
    if fingerprint(engine.store, 'experiment_decisions', parent_id) != source or engine.store.get('runs', run_id) != run:
        raise ValueError('Prompt upgrade source state changed')
    if engine.store.get('experiment_decisions', child_id):
        raise ValueError('Unexpected existing architecture successor')
    engine.store.batch([('experiment_decisions', child_id, child), (COLLECTION, parent_id,
        {'id': parent_id, 'run_id': run_id, 'body': body, 'sha256': sha(body)})])
    engine.store.event(run_id, 'experiment.architecture_prompt_upgraded',
        '架构提案使用新协议；复用已完成三方事实复核，原请求与答复保留，未授权实验。',
        {'source_decision_id': parent_id, 'decision_id': child_id, 'execution_authorized': False})
    return child


def route(engine, run_id, parent_id, ref):
    row = engine.store.get(COLLECTION, parent_id)
    if not row:
        return parent_id
    from .record_archive import fingerprint
    from .experiment_evidence_handoff import KEY
    body = row.get('body') or {}
    child = engine.store.get('experiment_decisions', body.get('child_id', '')) or {}
    if (row.get('run_id') != run_id or sha(body) != row.get('sha256') or body.get('evidence') != ref
            or body.get('execution_authorized') is not False or body.get('scientific_approval') is not False
            or body.get('parent', {}).get('key') != parent_id
            or fingerprint(engine.store, 'experiment_decisions', parent_id) != body.get('parent')
            or child.get('run_id') != run_id or child.get('evidence') != ref
            or child.get('proposal_request_contract_version') != body.get('request_contract_version')
            or child.get('architecture_prompt_parent') != {'id': parent_id, 'sha256': body['parent']['sha256']}
            or sha(child.get(KEY, {}).get('independent_review', {}).get('results')) != body.get('retained_facts_sha256')):
        raise ValueError('Architecture prompt successor lost its immutable source binding')
    return body['child_id']
