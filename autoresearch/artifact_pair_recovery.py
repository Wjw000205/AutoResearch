"""A new paired-evidence review continues an incomplete artifact handoff.

Keep old paid requests/results intact. The successor adds missing controls,
reuses the authenticated assessment, and still requires three independent reads.
"""
import copy

from .experiment_workflow import sha
from .storage import now

COLLECTION = 'artifact_evidence_successors'


def register(engine, run_id, parent_id, instruction):
    from . import experiment_evidence_handoff as handoff, artifact_fact_review as facts
    from .experiment_decisions import _archive
    from .record_archive import fingerprint,save
    run=engine.store.get('runs',run_id) or {}
    parent=engine.store.get('experiment_decisions',parent_id) or {}
    saved=parent.get(handoff.KEY) or {};review=saved.get('independent_review') or {}
    if (run.get('status') not in {'paused','blocked'} or not instruction.strip()
            or parent.get('run_id')!=run_id or parent.get('cycle')!=run.get('state',{}).get('research_cycle',1)
            or parent.get('proposal') or parent.get('votes')
            or not review.get('author_assignment') or not review.get('assessment')):
        raise ValueError('Paired evidence repair requires explicit direction on a settled, non-executable artifact review')
    ref=parent['evidence']
    existing=engine.store.get(COLLECTION,ref['id'])
    if existing:
        return engine.store.get('experiment_decisions',route(engine,run_id,ref))
    with engine.store._read_db() as db:
        active=db.execute("SELECT key FROM records WHERE collection IN ('experiments','handoffs') "
            "AND json_extract(data,'$.run_id')=? AND json_extract(data,'$.status')='running' LIMIT 1",(run_id,)).fetchone()
    if active:raise ValueError('Settle current paid work before registering paired evidence')
    assignment=review['author_assignment'];assessment=review['assessment']
    owner,read=facts.authenticate(engine,run_id,saved,assessment,assignment)
    base=engine.store.get('experiment_evidence',ref['id']) or {}
    if base.get('run_id')!=run_id or sha(base.get('summary'))!=ref['sha256']:
        raise ValueError('Original decision evidence changed')
    selected=saved['request'].get('artifact_trial_ids')
    if not selected:raise ValueError('Pair repair needs the exact original requested trial scope')
    material=handoff.collect(engine,run_id,base['summary'],ref,selected)
    if not material.get('pair_scope',{}).get('dependency_trial_ids'):
        raise ValueError('No previously omitted control records were located')
    prior_reviews=[]
    from .diagnostic_planning_recovery import authenticate
    from .engine import Harness
    old_assignment=review['assignment']
    for role in ('director','engineer','scout'):
        key=Harness.decision_key(run_id,role,old_assignment['purpose'],old_assignment['instruction'],
                                old_assignment['context'],old_assignment['schema'])
        prior=engine.store.get('handoffs',key) or {}
        if prior.get('status')!='completed':continue
        answer=engine.store.get('decisions',key)['result']
        authenticate(engine,run_id,role,old_assignment['purpose'],
            {k:old_assignment[k] for k in ('instruction','context','schema')},answer)
        prior_reviews.append({'role':role,'handoff_id':key,'answer_sha256':sha(answer),
            'closed_findings':[f for f in answer['findings'] if f['outcome'] in ('equal','different','not_applicable')],
            'remaining_gaps':answer['remaining_gaps'],'approval_inherited':False})
    material['fact_review_scope']={'source_decision_id':parent_id,'source_assessment_sha256':sha(assessment),
        'original_artifact_evidence':saved['evidence'],'prior_reviews':prior_reviews,
        'instruction':instruction.strip(),'question':'Do the now-supplied exact same-seed historical controls close the initialization linkage gaps?',
        'limits':'Retain prior capacity and metric checks. Saved-record equality is not an independent execution witness, mechanism benefit or stability.',
        'fit_calls':0,'evaluation_calls':0,'model_restore_calls':0,'execution_authorized':False,'scientific_approval':False}
    supplement=_archive(engine,run_id,material)
    parent_ref=fingerprint(engine.store,'experiment_decisions',parent_id)
    parent_archive=save(engine.store,run_id,parent_ref)
    child_id=parent_id+':paired-evidence:'+supplement['sha256']
    child=copy.deepcopy(parent)
    for field in ('recovery_answer','rejection','proposal_corrections','proposal_contract_recovery',
                  'proposal_trial_reference_recovery','consensus_parent','consensus_resolution'):
        child.pop(field,None)
    child.update(id=child_id,status='collecting_evidence',created_at=now(),updated_at=now(),
        rejected_proposal=copy.deepcopy(assessment),votes=[],
        artifact_source_parent={'id':parent_id,'sha256':parent_ref['sha256']})
    child_saved=copy.deepcopy(saved)
    child_saved.pop('independent_review',None);child_saved.pop('blocker',None)
    child_saved.update(status='assigned',owner='engineer')
    child_saved[facts.KEY]={'kind':'paired_initialization_fact_review','registered_at':now(),
        'source_handoff_id':owner,'assessment_sha256':sha(assessment),
        'original_artifact_evidence':copy.deepcopy(saved['evidence']),'source_assignment':copy.deepcopy(assignment),
        'source_read':read,'evidence':supplement,
        'required_paths':['/pair_scope','/pair_comparisons','/fact_review_scope'],
        'execution_authorized':False,'scientific_approval':False}
    child[handoff.KEY]=child_saved
    body={'run_id':run_id,'evidence':ref,'parent':parent_ref,'parent_archive':parent_archive,
        'child_id':child_id,'supplement':supplement,'instruction':instruction,
        'execution_authorized':False,'scientific_approval':False}
    value={'id':ref['id'],'run_id':run_id,'body':body,'sha256':sha(body)}
    if engine.store.get('runs',run_id)!=run or fingerprint(engine.store,'experiment_decisions',parent_id)!=parent_ref:
        raise ValueError('Source state changed before paired evidence registration')
    if engine.store.get('experiment_decisions',child_id):raise ValueError('Unexpected existing paired-evidence successor')
    engine.store.batch([('experiment_decisions',child_id,child),(COLLECTION,ref['id'],value)])
    engine.store.event(run_id,'experiment.artifact_pair_recovered',
        '已补齐精确同种子历史控制；旧评审保留，新复核只处理新增配对证据，未授权执行。',
        {'source_decision_id':parent_id,'decision_id':child_id,'evidence':supplement,'execution_authorized':False})
    return child


def route(engine, run_id, ref):
    row=engine.store.get(COLLECTION,ref['id'])
    if not row:return ref['id']
    from .record_archive import fingerprint
    from . import experiment_evidence_handoff as handoff, artifact_fact_review as facts
    body=row.get('body') or {}
    if (row.get('run_id')!=run_id or sha(body)!=row.get('sha256') or body.get('evidence')!=ref
            or body.get('execution_authorized') is not False or body.get('scientific_approval') is not False
            or fingerprint(engine.store,'experiment_decisions',body['parent']['key'])!=body['parent']):
        raise ValueError('Paired-evidence successor source binding changed')
    child=engine.store.get('experiment_decisions',body['child_id']) or {}
    spec=child.get(handoff.KEY,{}).get(facts.KEY) or {}
    supplement=engine.store.get('experiment_evidence',body['supplement']['id']) or {}
    if (child.get('run_id')!=run_id or child.get('evidence')!=ref or spec.get('evidence')!=body['supplement']
            or child.get('artifact_source_parent')!={'id':body['parent']['key'],'sha256':body['parent']['sha256']}
            or supplement.get('run_id')!=run_id or sha(supplement.get('summary'))!=body['supplement']['sha256']):
        raise ValueError('Paired-evidence successor or supplement changed')
    return body['child_id']
