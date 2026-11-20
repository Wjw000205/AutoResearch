"""Explicit pre-code worker model selection, without rewriting frozen work.

The append-only receipt selects an execution profile. It grants no code,
scientific or resource approval. Every attempt must freeze its receipt reference;
old documents, workflow identity, source model and research configuration stay.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

from .storage import now
from .local_codex import LocalCodexClient

COLLECTION='code_model_migrations'
FIELD='code_model_migration'
VERSION=1
KIND='pre_first_code_model_migration'
SUCCESSOR='unsubmitted_code_resume_model_migration'
SCOPE='run_new_code_requests'
_MODEL=re.compile(r'[A-Za-z0-9._:/-]{1,200}\Z')


def _fail(message):
    raise ValueError(message)


def _sha(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def _same_reference(actual,expected):
    return isinstance(actual,dict) and type(actual.get('version')) is int and actual==expected


def _get(db,collection,key,default=None):
    row=db.execute('SELECT data FROM records WHERE collection=? AND key=?',(collection,key)).fetchone()
    return json.loads(row[0]) if row else default


def _identity(state):
    if not isinstance(state,dict):_fail('Missing worker source workflow')
    common=state.get('common',{})
    if (not isinstance(common,dict) or any(not isinstance(common.get(k),dict) for k in ('proposal','design','experiment_plan'))
            or not isinstance(state.get('id'),str) or not isinstance(state.get('run_id'),str)
            or not isinstance(state.get('input_hash'),str) or not re.fullmatch('[0-9a-f]{64}',state['input_hash'])
            or type(state.get('cycle')) is not int or state['cycle']<1
            or not isinstance(state.get('model'),str) or not isinstance(state.get('reasoning_effort'),str)
            or any(not isinstance(state.get(k),str) or not state[k] for k in ('project_root','workspace'))
            or state.get('proposal_hash')!=_sha(common['proposal'])):
        _fail('Worker model migration requires the exact frozen workflow and scientific source')
    for key in ('format_contract_version','document_protocol'):
        value=state.get(key)
        if value is not None and (type(value) is not int or value not in {1,2}):
            _fail('Unknown worker source/context version')
    return {key:copy.deepcopy(state.get(key)) for key in ('id','run_id','cycle','kind','input_hash',
        'implementation_generation','proposal_hash','model','reasoning_effort','project_root','workspace',
        'format_contract_version','document_protocol')}|{
        'scientific_source':{key:copy.deepcopy(common[key]) for key in ('proposal','design','experiment_plan')}}


def _cursor(run):
    state=run.get('state',{})
    return {'run_id':run['id'],'status':run.get('status'),'stage':run.get('stage'),
        **{key:copy.deepcopy(state.get(key)) for key in ('research_cycle','implementation_generation',
            'implementation_restart','implementation_graph_thread_id','implementation_code_recovery')}}


def _unsubmitted_attempt(state):
    attempts = state.get('attempts') or []
    index = state.get('current_revision')
    if (state.get('status') != 'blocked' or type(index) is not int or index < 0
            or len(attempts) != index + 1 or state.get('turn_id') or state.get('result')):
        _fail('Worker migration needs the blocked controller-owned code resume')
    attempt = attempts[index]
    cp = attempt.get('checkpoint')
    if (attempt.get('index') != index or attempt.get('status') != 'implementing' or attempt.get('snapshot')
            or not LocalCodexClient.unsubmitted_code_resume(cp) or cp['thread_id'] != state.get('thread_id')
            or cp.get('operation_id') != attempt.get('operation_id') or cp.get('run_id') != state['run_id']
            or cp.get('workspace') != state['workspace'] or cp.get('project_root') != state['project_root']
            or cp.get('instruction_sha256') != hashlib.sha256(attempt['instruction'].encode()).hexdigest()
            or cp['instruction_sha256'] != attempt.get('instruction_sha256')):
        _fail('Worker migration lacks its exact unsubmitted native operation')
    return attempt


def _leaf(rows):
    if not rows:
        return None
    by_id = {row['id']: row for row in rows}
    parents = {row['predecessor_id'] for row in rows if row.get('predecessor_id')}
    leaves = set(by_id) - parents
    if len(leaves) != 1 or parents - set(by_id):
        _fail('Conflicting worker model migration receipts')
    current = leaf = by_id[leaves.pop()]
    remaining = set(by_id)
    while True:
        if current['id'] not in remaining:
            _fail('Cycle in worker model migration history')
        remaining.remove(current['id'])
        if not current.get('predecessor_id'):
            break
        current = by_id[current['predecessor_id']]
    if remaining:
        _fail('Disconnected worker model migration history')
    return leaf


def _snapshot(engine,db,run_id,target_model):
    if not isinstance(target_model,str) or not _MODEL.fullmatch(target_model):_fail('Invalid target worker model')
    run=_get(db,'runs',run_id,{})
    if run.get('id')!=run_id or run.get('status') not in {'paused','blocked'} or run.get('stage')!='implementation':
        _fail('Worker model migration requires the paused implementation controller')
    cycle=run.get('state',{}).get('research_cycle',1)
    if type(cycle) is not int or cycle<1:_fail('Unknown controller research cycle')
    generation=run.get('state',{}).get('implementation_generation')
    rows=db.execute("SELECT key,data FROM records WHERE collection='codex_workflows' AND json_extract(data,'$.run_id')=? "
        "AND json_extract(data,'$.kind')='implementation' AND json_extract(data,'$.cycle')=?",(run_id,cycle)).fetchall()
    owners=[(key,json.loads(raw)) for key,raw in rows if json.loads(raw).get('implementation_generation')==generation]
    if len(owners)!=1:_fail('Controller must own exactly one pre-code implementation workflow')
    key,state=owners[0]
    if state.get('id')!=key or state.get('run_id')!=run_id or state.get('kind')!='implementation':
        _fail('Worker workflow ownership changed')
    identity=_identity(state)
    successor = run['status']=='blocked' and bool(state.get('attempts'))
    predecessor = None
    source_model = state['model']
    if successor:
        attempt = _unsubmitted_attempt(state)
        predecessor = run_profile(engine,run_id).get(FIELD)
        selected = profile(engine,run_id,state,attempt)
        source_model = selected['model']
        if (selected.get(FIELD) != predecessor or attempt['checkpoint'].get('requested_model') != source_model
                or attempt['checkpoint'].get('requested_effort') != (selected['effort'] or None)):
            _fail('Unsubmitted operation differs from its frozen worker profile')
    elif (run['status']!='paused' or state.get('attempts')!=[] or state.get('status') not in {'specification','spec_review','scope_review','implementing','blocked'}
            or any(state.get(k) is not None for k in ('thread_id','turn_id','result','checkpoint'))):
        _fail('Worker model migration is supported only before the first code attempt or native request')
    worker=_get(db,'runtime','worker',{})
    task=getattr(engine,'tasks',{}).get(run_id)
    if worker.get('held') and worker.get('run_id')==run_id or task is not None and not task.done():
        _fail('Stop the current worker before freezing its model migration')
    if target_model==source_model:_fail('Target worker model is already the frozen source model')
    snapshot={'workflow':copy.deepcopy(state),'run_cursor':_cursor(run),'run_config':copy.deepcopy(run.get('config',{}))}
    body={'version':VERSION,'kind':KIND,'scope':SCOPE,'run_id':run_id,'workflow_id':key,'target_model':target_model,
        'source_identity':identity,'source_snapshot':snapshot}
    if successor:
        body.update(version=2,kind=SUCCESSOR,predecessor_id=predecessor['receipt_id'] if predecessor else None)
    return body


def preview(engine,run_id,target_model):
    with engine.store._read_db() as db:body=_snapshot(engine,db,run_id,target_model)
    source = body['source_snapshot']['workflow']
    return {'version':body['version'],'kind':body['kind'],'scope':SCOPE,'run_id':run_id,'workflow_id':body['workflow_id'],
        'source_model':_unsubmitted_attempt(source)['checkpoint']['requested_model'] if body['version']==2 else source['model'],'target_model':target_model,
        'reasoning_effort':body['source_identity']['reasoning_effort'],'expect_sha':_sha(body),
        'scientific_approval':False,'execution_authorized':False}


def _record(receipt,run_id,state):
    if (not isinstance(receipt,dict) or not isinstance(receipt.get('source_snapshot'),dict)
            or not isinstance(receipt['source_snapshot'].get('workflow'),dict)
            or not isinstance(receipt['source_snapshot'].get('run_cursor'),dict)):
        _fail('Missing worker model migration audit source')
    successor = receipt.get('version') == 2 and receipt.get('kind') == SUCCESSOR
    source = receipt['source_snapshot']['workflow']
    if successor:
        attempt = _unsubmitted_attempt(source)
        prior = attempt.get(FIELD)
        if (receipt.get('predecessor_id') != (prior['receipt_id'] if prior else None)
                or receipt['target_model'] == attempt['checkpoint']['requested_model']):
            _fail('Successor model migration lost its original request binding')
    if (type(receipt.get('version')) is not int or not (successor or receipt['version']==VERSION and receipt.get('kind')==KIND)
            or receipt.get('scope')!=SCOPE or receipt.get('run_id')!=run_id
            or receipt.get('workflow_id')!=state.get('id') or state.get('run_id')!=run_id
            or receipt.get('scientific_approval') is not False or receipt.get('execution_authorized') is not False
            or receipt.get('authorization')!='explicit_operator_request'
            or receipt.get('sha256')!=_sha({k:v for k,v in receipt.items() if k!='sha256'})
            or receipt.get('source_snapshot_sha256')!=_sha(receipt.get('source_snapshot'))
            or receipt.get('source_identity')!=_identity(state)
            or receipt.get('source_identity_sha256')!=_sha(receipt['source_identity'])
            or _identity(receipt['source_snapshot']['workflow'])!=receipt['source_identity']
            or not successor and (source.get('attempts')!=[] or any(source.get(k) is not None for k in ('thread_id','turn_id','result','checkpoint')))
            or receipt['source_snapshot'].get('run_cursor',{}).get('status')!=('blocked' if successor else 'paused')
            or receipt['source_snapshot']['run_cursor'].get('stage')!='implementation'
            or receipt['source_snapshot']['run_cursor'].get('run_id')!=run_id
            or (receipt['source_snapshot']['run_cursor'].get('research_cycle')
                if receipt['source_snapshot']['run_cursor'].get('research_cycle') is not None else 1)!=receipt['source_identity']['cycle']
            or receipt['source_snapshot']['run_cursor'].get('implementation_generation')!=receipt['source_identity']['implementation_generation']
            or not isinstance(receipt.get('target_model'),str) or not _MODEL.fullmatch(receipt['target_model'])
            or not successor and receipt['target_model']==receipt['source_identity']['model']):
        _fail('Worker model migration receipt/version or frozen source changed')
    body={key:receipt[key] for key in ('version','kind','scope','run_id','workflow_id','target_model','source_identity','source_snapshot')}
    if successor:
        body['predecessor_id'] = receipt['predecessor_id']
    if receipt.get('expect_sha')!=_sha(body):_fail('Worker migration preview binding changed')
    return receipt


def _request_key(run_id,expect_sha,idempotency_key,reason):
    if (not isinstance(expect_sha,str) or not re.fullmatch('[0-9a-f]{64}',expect_sha)
            or not isinstance(idempotency_key,str) or not 1<=len(idempotency_key)<=128
            or not isinstance(reason,str) or not reason.strip()):_fail('Migration needs its preview SHA, idempotency key and reason')
    return run_id+':code-model-migration:'+_sha(idempotency_key)


def _replay(db,run_id,target_model,expect_sha,key,reason):
    previous=_get(db,COLLECTION,key)
    if previous:
        if (previous.get('id')!=key or previous.get('expect_sha')!=expect_sha
                or previous.get('target_model')!=target_model or previous.get('reason')!=reason):
            _fail('Idempotency key already binds a different model migration')
        return copy.deepcopy(_record(previous,run_id,_get(db,'codex_workflows',previous['workflow_id'],{})))
    return None


def replay(engine,run_id,target_model,*,expect_sha,idempotency_key,reason):
    key=_request_key(run_id,expect_sha,idempotency_key,reason)
    with engine.store._read_db() as db:return _replay(db,run_id,target_model,expect_sha,key,reason)


def apply(engine,run_id,target_model,*,expect_sha,idempotency_key,reason):
    key=_request_key(run_id,expect_sha,idempotency_key,reason)
    with engine.store._db() as db:
        db.execute('BEGIN IMMEDIATE');engine.store._guard_run(db,run_id)
        previous=_replay(db,run_id,target_model,expect_sha,key,reason)
        if previous:return previous
        body=_snapshot(engine,db,run_id,target_model)
        if _sha(body)!=expect_sha:_fail('Worker migration preview is stale; no configuration or workflow changed')
        if body['version']==1 and db.execute("SELECT 1 FROM records WHERE collection=? AND json_extract(data,'$.run_id')=?",(COLLECTION,run_id)).fetchone():
            _fail('This run already has a frozen worker model migration')
        receipt={**body,'id':key,'expect_sha':expect_sha,'source_identity_sha256':_sha(body['source_identity']),
            'source_snapshot_sha256':_sha(body['source_snapshot']),'authorization':'explicit_operator_request',
            'reason':reason,'created_at':now(),'scientific_approval':False,'execution_authorized':False}
        receipt['sha256']=_sha(receipt)
        engine.store._guard_write(db,COLLECTION,key,receipt)
        db.execute('INSERT INTO records VALUES (?,?,?,?)',(COLLECTION,key,json.dumps(receipt,ensure_ascii=False,allow_nan=False),now()))
        if body['version']==2:
            state = copy.deepcopy(body['source_snapshot']['workflow'])
            attempt = _unsubmitted_attempt(state)
            attempt.pop('checkpoint')
            attempt.update(operation_id=_sha([key,expect_sha])[:32], created_at=receipt['created_at'])
            attempt[FIELD] = _reference(receipt,state)
            state['updated_at'] = now()
            engine.store._guard_write(db,'codex_workflows',state['id'],state)
            db.execute('UPDATE records SET data=?,updated_at=? WHERE collection=? AND key=?',
                       (json.dumps(state,ensure_ascii=False,allow_nan=False),state['updated_at'],'codex_workflows',state['id']))
        return copy.deepcopy(receipt)


def _reference(receipt,state):
    ref={'version':receipt['version'],'kind':receipt['kind'],'scope':SCOPE,'receipt_id':receipt['id'],'receipt_sha256':receipt['sha256'],
        'source_workflow_id':receipt['workflow_id'],'workflow_id':state['id'],
        'model':receipt['target_model'],'effort':receipt['source_identity']['reasoning_effort']}
    if state.get('kind')=='implementation' and state['id']!=receipt['workflow_id']:
        frozen=state.get(FIELD,{}).get('workflow_frozen_profile')
        if not isinstance(frozen,dict) or set(frozen)!={'model','effort'} or any(not isinstance(v,str) for v in frozen.values()):
            _fail('New workflow lacks its original frozen transport profile')
        original={**state,'model':frozen['model'],'reasoning_effort':frozen['effort']}
        ref.update(workflow_source_sha256=_sha(_identity(original)),workflow_frozen_profile=copy.deepcopy(frozen))
    return ref


def _owned_receipt(engine,run_id,state):
    ids=engine.store.record_keys(COLLECTION,predicate="json_extract(data,'$.workflow_id')=?",parameters=[state['id']])
    if not ids:return None
    stored=engine.store.get('codex_workflows',state['id'],{})
    rows=[]
    for key in ids:
        row=engine.store.get(COLLECTION,key)
        if row.get('id')!=key:_fail('Worker migration storage identity changed')
        rows.append(_record(row,run_id,stored))
    return _record(_leaf(rows),run_id,state)


def _native(state,attempt,ref,*,completed=False):
    if not _same_reference(attempt.get(FIELD),ref):_fail('Migrated code attempt lost or changed its model reference')
    cp=attempt.get('checkpoint')
    if cp is None:
        if completed:_fail('Accepted migrated code requires its actual native checkpoint')
        return
    if (not isinstance(cp,dict) or type(cp.get('version')) is not int or cp['version']!=1
            or cp.get('reasoning_only') is True or cp.get('run_id')!=state['run_id']
            or cp.get('operation_id')!=attempt.get('operation_id')
            or cp.get('requested_model')!=ref['model'] or cp.get('requested_effort')!=(ref['effort'] or None)
            or cp.get('workspace')!=attempt.get('writer_workspace',state['workspace']) or cp.get('project_root')!=state['project_root']
            or not isinstance(attempt.get('instruction'),str)
            or cp.get('instruction_sha256')!=hashlib.sha256(attempt['instruction'].encode()).hexdigest()
            or completed and (cp.get('status')!='completed' or not cp.get('thread_id') or not cp.get('turn_id')
                or not cp.get('turn_dispatched_at'))):
        _fail('Migrated worker checkpoint differs from its real operation/model/source')


def _resolve(engine,run_id,state,seen=()):
    if state.get('run_id')!=run_id or state.get('id') in seen or len(seen)>=32:_fail('Invalid worker model lineage')
    receipt=_owned_receipt(engine,run_id,state)
    if receipt:
        if FIELD in state and not _same_reference(state[FIELD],_reference(receipt,state)):_fail('Worker source model reference changed')
        return receipt
    if FIELD in state:
        ref=state[FIELD]
        receipt=engine.store.get(COLLECTION,ref.get('receipt_id'),{}) if isinstance(ref,dict) else {}
        root=engine.store.get('codex_workflows',receipt.get('workflow_id'),{})
        _record(receipt,run_id,root)
        stored=engine.store.get('codex_workflows',state['id'])
        if (state.get('kind')!='implementation' or state.get('model')!=receipt['target_model']
                or state.get('reasoning_effort')!=receipt['source_identity']['reasoning_effort']
                or not _same_reference(ref,_reference(receipt,state))
                or stored is not None and (not _same_reference(stored.get(FIELD),ref) or _identity(stored)!=_identity(state))):
            _fail('New workflow changed its frozen run model/source reference')
        return receipt
    parent=state.get('repair',{}).get('program') if isinstance(state.get('repair'),dict) else None
    if not isinstance(parent,dict):return None
    parent_state=engine.store.get('codex_workflows',parent.get('delivery_id'),{})
    if not parent_state:
        if FIELD in parent:_fail('Inherited model reference lacks its actual accepted parent workflow')
        return None
    receipt=_resolve(engine,run_id,parent_state,(*seen,state['id']))
    if receipt is None:
        if FIELD in parent:_fail('Unknown inherited worker model reference')
        return None
    if state.get('kind')!='runtime_repair':_fail('Only ordinary runtime repair can inherit an accepted worker profile')
    ref=_reference(receipt,parent_state);accepted=parent_state.get('result') or {}
    fields=('delivery_id','code','dependencies','code_sha256','spec_hash','delivery_hash','codex_model',
        'codex_reasoning_effort','codex_project_dir','codex_workspace',FIELD)
    if (parent_state.get('status')!='completed' or any(accepted.get(k)!=parent.get(k) for k in fields)
            or not _same_reference(parent.get(FIELD),ref) or not _same_reference(accepted.get(FIELD),ref)
            or parent.get('codex_model')!=ref['model']
            or parent.get('codex_reasoning_effort')!=ref['effort']
            or state.get('model')!=ref['model'] or state.get('reasoning_effort')!=ref['effort']
            or state.get('project_root')!=parent_state.get('project_root') or state.get('workspace')!=accepted.get('codex_workspace')):
        _fail('Inherited repair model changed its actual accepted program/profile')
    attempts=[a for a in parent_state.get('attempts',[]) if a.get('status')=='approved'
        and a.get('code_sha256')==parent.get('code_sha256')]
    if len(attempts)!=1:_fail('Inherited worker profile lacks a unique accepted native attempt')
    _native(parent_state,attempts[0],ref,completed=True)
    return receipt


def check_receipt(engine,run_id,state,ref=None):
    receipt=_resolve(engine,run_id,state)
    if ref is not None and (receipt is None or not _same_reference(ref,_reference(receipt,state))):_fail('Foreign or missing worker model migration reference')
    return receipt


def _attempt_reference(receipt,state,attempt):
    if receipt['version']==2 and state['id']==receipt['workflow_id']:
        source=receipt['source_snapshot']['workflow']
        boundary=source['current_revision']
        index=attempt['index']
        if type(index) is not int or index<0:_fail('Invalid code attempt ordinal')
        if index<boundary:
            if attempt!=source['attempts'][index]:_fail('Historical worker attempt changed across migration')
            return attempt.get(FIELD)
        if index==boundary and (attempt['operation_id']!=_sha([receipt['id'],receipt['expect_sha']])[:32]
                or attempt['instruction_sha256']!=source['attempts'][index]['instruction_sha256']
                or hashlib.sha256(attempt['instruction'].encode()).hexdigest()!=attempt['instruction_sha256']):
            _fail('Replacement code operation differs from the frozen successor')
    return _reference(receipt,state)


def _check_attempts(engine,state,receipt,*,candidate=None):
    stored=engine.store.get('codex_workflows',state['id'],{})
    for attempt in [*stored.get('attempts',[]),*state.get('attempts',[])]:
        if candidate is not None and attempt is candidate:continue
        ref=_attempt_reference(receipt,state,attempt)
        if ref is not None:_native(state,attempt,ref)


def profile(engine,run_id,state,attempt=None):
    receipt=check_receipt(engine,run_id,state)
    if receipt is None:
        if (FIELD in state or FIELD in (state.get('result') or {}) or any(FIELD in a for a in state.get('attempts',[]))
                or attempt is not None and FIELD in attempt):_fail('Unknown worker model migration reference')
        return {'model':state.get('model',''),'effort':state.get('reasoning_effort','')}
    ref=_attempt_reference(receipt,state,attempt) if attempt is not None else _reference(receipt,state)
    _check_attempts(engine,state,receipt)
    if ref is None:return {'model':state['model'],'effort':state['reasoning_effort']}
    if attempt is not None:_native(state,attempt,ref)
    return {'model':ref['model'],'effort':ref['effort'],FIELD:ref}


def bind_attempt(engine,run_id,state,attempt):
    receipt=check_receipt(engine,run_id,state)
    if receipt is None:
        if FIELD in attempt:_fail('Unrecognized worker model reference on a legacy attempt')
        return attempt
    ref=_reference(receipt,state);stored=engine.store.get('codex_workflows',state['id'],{})
    exists=any(a.get('index')==attempt.get('index') or a.get('operation_id')==attempt.get('operation_id') for a in stored.get('attempts',[]))
    if FIELD in attempt or exists:
        if not _same_reference(attempt.get(FIELD),ref):_fail('Persisted migrated attempt cannot lose or replace its model reference')
    elif (type(attempt.get('index')) is not int or attempt['index']<0 or not isinstance(attempt.get('operation_id'),str)
            or not attempt['operation_id'] or any(attempt.get(k) is not None for k in ('checkpoint','thread_id','turn_id'))):
        _fail('Only a new undispatched attempt can freeze a worker model reference')
    _check_attempts(engine,state,receipt,candidate=attempt)
    attempt[FIELD]=ref;_native(state,attempt,ref)
    return attempt


def bind_workflow(engine,run_id,state):
    """Freeze run-level selection only while creating a genuinely new workflow."""
    if FIELD in state:
        check_receipt(engine,run_id,state);return state
    selected=run_profile(engine,run_id)
    if FIELD not in selected:return state
    if (engine.store.get('codex_workflows',state.get('id')) is not None
            or state.get('kind')!='implementation' or state.get('run_id')!=run_id
            or state.get('attempts')!=[] or any(state.get(k) is not None for k in ('thread_id','turn_id','result','checkpoint'))):
        _fail('Only a new undispatched implementation can freeze the run worker profile')
    _identity(state)
    receipt=engine.store.get(COLLECTION,selected[FIELD]['receipt_id'])
    frozen={'model':state['model'],'effort':state['reasoning_effort']}
    state.update(model=selected['model'],reasoning_effort=selected['effort'],**{FIELD:{'workflow_frozen_profile':frozen}})
    state[FIELD]=_reference(receipt,state)
    return state


def validate_checkpoint(engine,run_id,state,attempt,checkpoint=None,*,completed=False):
    selected=profile(engine,run_id,state,attempt)
    if FIELD not in selected:return True
    actual=attempt if checkpoint is None else {**attempt,'checkpoint':checkpoint}
    _native(state,actual,selected[FIELD],completed=completed)
    return True


def validate_program_profile(engine,run_id,state,attempt,program):
    selected=profile(engine,run_id,state,attempt)
    if FIELD not in selected:
        if FIELD in program:_fail('Legacy program contains an unauthenticated worker model reference')
        return True
    _native(state,attempt,selected[FIELD],completed=True)
    expected={'delivery_id':state['id'],'codex_model':selected['model'],'codex_reasoning_effort':selected['effort'],
        'codex_project_dir':state['project_root'],'codex_workspace':attempt.get('writer_workspace',state['workspace']),
        'codex_thread_id':attempt['checkpoint']['thread_id'],FIELD:selected[FIELD]}
    if any(not _same_reference(program.get(k),v) if k==FIELD else program.get(k)!=v for k,v in expected.items()):
        _fail('Program model/profile differs from its actual migrated worker checkpoint')
    saved=state.get('result') or {}
    if state.get('status')=='completed' and any(not _same_reference(saved.get(k),v) if k==FIELD else saved.get(k)!=v for k,v in expected.items()):
        _fail('Saved accepted program lost or changed its migrated worker model/profile')
    return True


def projection(store,run,workflow_metadata):
    """Small SQL-only display. This is explicitly not runtime authentication."""
    metadata=workflow_metadata or {};rid=run.get('id');wid=metadata.get('id')
    result={'model':metadata.get('model',''),'reasoning_effort':metadata.get('reasoning_effort',''),
        'frozen_model':metadata.get('model',''),'projection_only':True}
    if not rid or not wid:return result
    fields=('id','run_id','version','kind','scope','workflow_id','target_model','sha256','predecessor_id')
    selection=','.join("'"+key+"',json_extract(data,'$."+key+"')" for key in fields)+",'version_json_type',json_type(data,'$.version')"
    with store._read_db() as db:
        rows=db.execute("SELECT json_object("+selection+",'effort',json_extract(data,'$.source_identity.reasoning_effort'),"
            "'source_model',json_extract(data,'$.source_identity.model')) FROM records WHERE collection=? "
            "AND json_extract(data,'$.run_id')=? AND json_extract(data,'$.workflow_id')=?",(COLLECTION,rid,wid)).fetchall()
        inherited=None
        if not rows:
            row=db.execute("SELECT coalesce(json_extract(data,'$."+FIELD+"'),json_extract(data,'$.repair.program."+FIELD+"')) FROM records WHERE collection='codex_workflows' AND key=?",(wid,)).fetchone()
            inherited=json.loads(row[0]) if row and row[0] else None
            if inherited:
                rows=db.execute("SELECT json_object("+selection+",'effort',json_extract(data,'$.source_identity.reasoning_effort'),"
                    "'source_model',json_extract(data,'$.source_identity.model')) FROM records WHERE collection=? AND key=?",(COLLECTION,inherited.get('receipt_id'))).fetchall()
    if rows:
        row=_leaf([json.loads(item[0]) for item in rows]) if not inherited else json.loads(rows[0][0])
        if (row.get('version_json_type')!='integer' or type(row.get('version')) is not int or (row['version'],row.get('kind')) not in {(1,KIND),(2,SUCCESSOR)} or row.get('scope')!=SCOPE or row.get('run_id')!=rid
                or not inherited and (row.get('source_model')!=metadata.get('model') or row.get('effort')!=metadata.get('reasoning_effort'))
                or inherited and (inherited.get('receipt_sha256')!=row.get('sha256') or inherited.get('model')!=row.get('target_model')
                    or type(inherited.get('version')) is not int or inherited.get('version')!=row['version'] or inherited.get('kind')!=row['kind'] or inherited.get('scope')!=SCOPE
                    or inherited.get('source_workflow_id')!=row.get('workflow_id') or inherited.get('effort')!=row.get('effort')
                    or metadata.get('model')!=row.get('target_model') or metadata.get('reasoning_effort')!=row.get('effort'))):
            _fail('Unknown worker model display migration')
        ref={'version':row['version'],'kind':row['kind'],'scope':SCOPE,'receipt_id':row['id'],'receipt_sha256':row['sha256'],
            'source_workflow_id':row['workflow_id'],'workflow_id':wid,'model':row['target_model'],'effort':row['effort']}
        result.update(model=row['target_model'],reasoning_effort=row['effort'],**{FIELD:ref})
    return result


def run_profile(engine,run_id):
    """The explicit receipt covers this run's future, separately admitted jobs."""
    ids=engine.store.record_keys(COLLECTION,predicate="json_extract(data,'$.run_id')=? OR json_extract(data,'$.source_identity.run_id')=?",parameters=[run_id,run_id])
    if not ids:
        config=engine.store.get('runs',run_id,{}).get('config',{})
        return {'model':config.get('codex_model',''),'effort':config.get('codex_reasoning_effort','')}
    rows=[]
    for key in ids:
        row=engine.store.get(COLLECTION,key)
        state=engine.store.get('codex_workflows',row.get('workflow_id'),{})
        if row.get('id')!=key:_fail('Run worker migration storage identity changed')
        rows.append(_record(row,run_id,state))
    receipt=_leaf(rows)
    state=engine.store.get('codex_workflows',receipt['workflow_id'])
    ref=_reference(receipt,state)
    return {'model':ref['model'],'effort':ref['effort'],FIELD:ref}


def _job_ref(selected,job):
    if (not isinstance(job.get('operation_id'),str) or not job['operation_id']
            or not isinstance(job.get('instruction'),str) or not job['instruction']):
        _fail('A new code job needs its exact operation and instruction')
    return {**selected[FIELD],'job_id':job.get('id'),'job_operation_id':job['operation_id'],
        'job_instruction_sha256':hashlib.sha256(job['instruction'].encode()).hexdigest()}


def _legacy_job(job,*,strict=True):
    cp=job.get('checkpoint')
    selection=job.get('selection',{})
    if not strict:
        if isinstance(selection,dict) and 'model' in selection and 'effort' in selection:
            return {k:selection[k] for k in ('model','effort')}
        if isinstance(cp,dict) and 'requested_model' in cp and 'requested_effort' in cp:
            return {'model':cp['requested_model'],'effort':cp['requested_effort']}
        return None
    if cp:
        if not isinstance(cp,dict) or 'requested_model' not in cp or 'requested_effort' not in cp:
            _fail('Legacy code checkpoint has no original frozen profile')
        result={'model':cp['requested_model'],'effort':cp['requested_effort']}
        if any(k in selection and selection[k]!=result[k] for k in ('model','effort')):
            _fail('Legacy code selection conflicts with its actual native checkpoint')
        return result
    if isinstance(selection,dict) and 'model' in selection and 'effort' in selection:
        return {k:selection[k] for k in ('model','effort')}
    return None


def job_profile(engine,run_id,job,*,work=None):
    if job.get('run_id',run_id)!=run_id:_fail('Foreign code job run')
    selected=run_profile(engine,run_id)
    if FIELD not in job:
        legacy=_legacy_job(job,strict=FIELD in selected)
        if legacy is not None:return legacy
        if FIELD in selected:_fail('New code job is missing its frozen run model reference')
        return selected
    frozen=job[FIELD]
    if isinstance(frozen,dict) and frozen.get('receipt_id')!=(selected.get(FIELD) or {}).get('receipt_id'):
        prior=engine.store.get(COLLECTION,frozen.get('receipt_id'),{})
        state=engine.store.get('codex_workflows',prior.get('workflow_id'),{})
        _record(prior,run_id,state)
        if prior['id']!=frozen['receipt_id']:_fail('Historical job model receipt identity changed')
        ref=_reference(prior,state)
        selected={'model':ref['model'],'effort':ref['effort'],FIELD:ref}
    if FIELD not in selected or not _same_reference(job[FIELD],_job_ref(selected,job)):_fail('Code job changed its frozen worker profile/source')
    cp=job.get('checkpoint')
    if cp is not None and (not isinstance(cp,dict) or type(cp.get('version')) is not int or cp['version']!=1
            or cp.get('reasoning_only') is True or cp.get('run_id')!=run_id or cp.get('operation_id')!=job['operation_id']
            or cp.get('requested_model')!=selected['model'] or cp.get('requested_effort')!=(selected['effort'] or None)
            or cp.get('instruction_sha256')!=job[FIELD]['job_instruction_sha256']):
        _fail('Code job checkpoint changed its frozen migration profile')
    if work is not None:
        actual=work.get('checkpoint')
        if (not isinstance(work.get('instruction'),str) or not work['instruction'] or not work.get('operation_id')
                or actual is not None and (not isinstance(actual,dict) or type(actual.get('version')) is not int
                    or actual['version']!=1 or actual.get('reasoning_only') is True
                    or actual.get('run_id')!=run_id or actual.get('operation_id')!=work['operation_id']
                    or actual.get('requested_model')!=selected['model'] or actual.get('requested_effort')!=(selected['effort'] or None)
                    or actual.get('instruction_sha256')!=hashlib.sha256(work['instruction'].encode()).hexdigest())):
            _fail('Code continuation checkpoint changed its actual operation/instruction/profile')
    return {'model':selected['model'],'effort':selected['effort'],FIELD:copy.deepcopy(job[FIELD])}


def bind_job(engine,run_id,job):
    if job.get('run_id',run_id)!=run_id:_fail('Foreign code job run')
    if FIELD in job:job_profile(engine,run_id,job);return job
    selected=run_profile(engine,run_id)
    if FIELD not in selected:return job
    if job.get('checkpoint') is not None or any(job.get(k) for k in ('thread_id','turn_id','continuations','native_history','recovery_history')):
        if _legacy_job(job) is None:_fail('Existing code job cannot acquire a new model migration reference')
        return job
    if FIELD in selected:job[FIELD]=_job_ref(selected,job)
    return job
