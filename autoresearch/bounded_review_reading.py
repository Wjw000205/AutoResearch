"""Repair a proven host candidate-selector collision with a new review snapshot."""
import copy
import hashlib

from .experiment_workflow import sha
from .storage import now

COLLECTION = 'bounded_review_recoveries'


def source_reference(evidence):
    return (evidence.get('candidate_code_sources') or (evidence.get('rapid_revision') or {}).get('code_sources')
            or (evidence.get('code_sources') if evidence.get('candidate_not_executed') is True else None))


def excluded(store, record):
    repair = store.get(COLLECTION, record['id'])
    if not repair:
        return False
    if repair['source_sha256']!=sha(record):
        raise ValueError('Original misbound review changed after repair')
    return True


def recover(engine, record):
    from .bounded_diagnostics import build, source_task
    from .parameter_resolution import owner
    from .diagnostic_resource_evidence import capture
    from .controlled_training_checks import review_context
    from .experiment_analysis import inspect_module
    if record.get('status')!='reviewing_code' or record.get('trial_id') or record.get('outcome'):
        return record
    rd = record['rounds'][-1]
    failed = []
    for role, request in rd.get('votes',{}).items():
        ref = request['context']['validation_evidence']
        old = engine.store.get('experiment_evidence',ref['id']) or {}
        if old.get('run_id')!=record['run_id'] or sha(old.get('summary'))!=ref['sha256']:
            raise ValueError('Frozen bounded review evidence changed')
        target = record['candidate']['code_sources']
        if source_reference(old['summary'])==target:
            continue
        if old['summary'].get('code_sources')!=target or old['summary'].get('candidate_not_executed') is not True:
            raise ValueError('Cannot identify original scoped review candidate')
        for req in (request,request.get('read_correction')):
            if req and req.get('answer'):
                owner(engine,record['run_id'],role,req)
        failed.append({'role':role,'request_sha256':sha(request),'evidence':ref,
                       'misbound_sources':source_reference(old['summary'])})
    if not failed:
        return record
    source_task(engine,record)
    owner(engine,record['run_id'],'engineer',rd['author'])
    key = record['id']+':source-binding-v1'
    replacement = {k:copy.deepcopy(v) for k,v in record.items() if k not in (
        'id','rounds','candidate','code','protocol','admission','votes','review_reads','updated_at',
        'resource_evidence','test_evidence','status','created_at','failure')}
    replacement.update(id=key,status='implementing',created_at=now(),rounds=[{'author':copy.deepcopy(rd['author'])}],
        source_binding_repair={'source_id':record['id'],'source_sha256':sha(record),'failed_assignments':failed,
                              'approval_inherited':False})
    proof = capture(engine,record['run_id'],replacement)
    replacement.update(resource_evidence=proof,test_evidence=proof.get('offline_checks'))
    replacement['rounds'][0]['build'] = build(engine,replacement,rd['author']['answer'])
    replacement.update(replacement['rounds'][0]['build'])
    context = review_context(engine,replacement)
    ref = context['validation_evidence']
    # Replay every public source window before allowing another model request.
    # This validates transport, without granting review or execution approval.
    for area in replacement['candidate']['changed_ranges']:
        offset, text = 0, ''
        while True:
            page = inspect_module(engine,record['run_id'],context,{'evidence_id':ref['id'],'expected_sha256':ref['sha256'],
                'snapshot':'candidate','view':'source','path':area['path'],'offset':offset,'limit':6000})
            if page['source_sha256']!=area['sha256']:
                raise ValueError('Repaired review source differs from candidate')
            text += page['text']
            offset = page['next_offset']
            if offset is None:break
        if hashlib.sha256(text.encode()).hexdigest()!=area['sha256'] or len(text)!=area['end']:
            raise ValueError('Repaired review pages are incomplete')
    engine.check_active(record['run_id'])
    audit = {'id':record['id'],'run_id':record['run_id'],'source_sha256':sha(record),'target_id':key,
             'candidate_sha256':replacement['candidate']['bundle']['sha256'],'created_at':now()}
    engine.store.batch(puts=[('bounded_diagnostics',key,replacement),(COLLECTION,record['id'],audit)],
        expected=[('bounded_diagnostics',record['id'],record),('bounded_diagnostics',key,None),(COLLECTION,record['id'],None)])
    return replacement
