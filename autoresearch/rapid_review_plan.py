"""Versioned review scope for generated metadata; original assignments stay frozen."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path

from .experiment_workflow import sha
from .storage import now

ROLES = ('director','engineer','scout')


def checked(record):
    plan = record.get('structured_review')
    if not plan:
        return None
    body = plan['body']; c = record['candidate']
    if (sha(body) != plan['sha256'] or any(body[k] != c[k] for k in ('amendment_sha256','checks_sha256'))
            or body['candidate_sha256'] != c['bundle']['sha256']):
        raise ValueError('Structured review plan lost candidate binding')
    return body


def inherited_rows(engine, run_id, role, context, _depth=0):
    scope = context.get('rapid_revision') or {}
    prior = scope.get('prior_reading')
    if not prior:
        return []
    if _depth>=8:
        raise ValueError('Prior review reading chain is too deep')
    from .engine import digest
    key = prior['handoff_id']
    owner = engine.store.get('handoffs',key) or {}
    frozen = engine.store.get('handoff_inputs',key) or {}
    old = frozen.get('context',{})
    same_checks=old.get('rapid_revision',{}).get('checks_sha256')==scope.get('checks_sha256')
    if not same_checks and scope.get('host_check_repair',{}).get('reason') in {'pathlib_api_compatibility','windows_path_compatibility','delivery_invocation_receipts','windows_cwd_compatibility','full_acceptance_budget'}:
        ref=scope['host_check_repair']['original']
        saved=engine.store.get('experiment_evidence',ref['id']) or {}
        original=(saved.get('summary') or {}).get('rapid_revision_attempt',{})
        same_checks=(saved.get('run_id')==run_id and sha(saved.get('summary'))==ref['sha256']
            and original.get('id')==scope.get('id')
            and original.get('candidate',{}).get('checks_sha256')==old.get('rapid_revision',{}).get('checks_sha256')
            and original.get('review_contexts',{}).get(role)==old)
    if (owner.get('run_id')!=run_id or owner.get('to_role')!=role
            or not owner.get('purpose','').startswith('rapid_review:')
            or sha(frozen)!=prior['input_sha256'] or old.get('validation_evidence')!=prior['validation_evidence']
            or not same_checks or any(old.get('rapid_revision',{}).get(k)!=scope.get(k)
                   for k in ('amendment_sha256','candidate_sha256'))):
        raise ValueError('Prior review reading owner/version changed')
    evidence = engine.store.get('experiment_evidence',context['validation_evidence']['id']) or {}
    if sha(evidence.get('summary'))!=context['validation_evidence']['sha256']:
        raise ValueError('Current review evidence changed')
    ref = evidence['summary']['code_sources']
    archived = engine.store.get('experiment_sources',ref['id']) or {}
    if archived.get('run_id')!=run_id or sha(archived.get('files'))!=ref['sha256']:
        raise ValueError('Current review source changed')
    progress = engine.store.get('handoff_progress',key) or {}
    if sha(progress)!=prior['progress_sha256']:
        raise ValueError('Prior review progress changed after the recovery snapshot')
    ancestors=inherited_rows(engine,run_id,role,old,_depth+1)
    valid={}
    for row in ancestors:
        value=row['result'];item=archived['files'].get(value['path'],{})
        if value['source_sha256']==item.get('sha256') and value['text']==item.get('text','')[value['offset']:value['end']]:
            valid[value['receipt_sha256']]=row
    ancestor_refs=set(valid)
    for row in progress.get('tool_results',[]):
        call,result = row.get('call',{}),row.get('result',{})
        args=call.get('arguments',{}); item=archived['files'].get(result.get('path'),{})
        if (call.get('name')=='experiments.inspect_module' and result.get('source_read') is True
                and args.get('evidence_id')==prior['validation_evidence']['id']
                and args.get('expected_sha256')==prior['validation_evidence']['sha256']
                and result.get('evidence_sha256')==prior['validation_evidence']['sha256']
                and result.get('source_sha256')==item.get('sha256')
                and result.get('text')==item.get('text','')[result.get('offset',0):result.get('end',0)]
                and result.get('receipt_sha256')==sha({'call':call,'result':{k:v for k,v in result.items() if k!='receipt_sha256'}})):
            valid[result['receipt_sha256']]=row
    noted=set(ancestor_refs)
    for row in progress.get('tool_results',[]):
        if row.get('call',{}).get('name')=='experiments.record_notes' and row.get('result',{}).get('saved'):
            # record_notes historically has no separate receipt_sha256. Its
            # exact call/result is bound by the frozen progress digest above.
            # Credit only notes actually submitted and returned by that call.
            for note in row['call'].get('arguments',{}).get('notes',[]):
                if note in row['result'].get('notes',[]) and set(note.get('receipt_sha256s',[]))<=set(valid):
                    noted.update(note['receipt_sha256s'])
    decision=(engine.store.get('decisions',key) or {}).get('result')
    if decision and owner.get('status')=='completed' and owner.get('output_hash')==digest(decision):
        noted.update(set(decision.get('read_receipts',[])) & set(valid))
    required={r['path'] for r in scope['changed_ranges']}
    return [row for ref,row in valid.items() if ref in noted and row['result']['path'] in required]


def context(engine, run_id, record, base):
    body=checked(record)
    if not body:
        return base
    result=copy.deepcopy(base)
    result.update(validation_evidence=body['evidence'],evidence_sha256=body['evidence']['sha256'])
    result['rapid_revision'].update(review_read_protocol=3,changed_ranges=body['required_ranges'],
        structured_review_plan_sha256=record['structured_review']['sha256'],
        metadata_audit=body['audit'],host_post_exit_contract=body['post_exit_contract'],
        reading_guidance='Required source ranges remain mandatory. Generated JSON is exhaustively checked by the host static audit; review its report, full schema, generator, validator and tests. Inspect raw JSON only for a specific unresolved question. Host audit is not candidate behavior or approval. Reuse only this role\'s authenticated prior reading; do not restart covered ranges. Other roles\' votes are collected by the host, not a prerequisite for your independent vote.')
    return result


def role_context(engine, run_id, record, common, role):
    body=checked(record); result=copy.deepcopy(common)
    prior=body['prior_reading'].get(role)
    if prior:
        result['rapid_revision']['prior_reading']=prior
        rows=inherited_rows(engine,run_id,role,result)
        result['rapid_revision']['inherited_source_coverage']=[{k:r['result'][k] for k in
            ('receipt_sha256','path','source_sha256','offset','end')} for r in rows]
        result['rapid_revision']['prior_reading_note']='Same-role actual reads only; no approval is inherited. Correct obsolete missing-read notes using this ledger.'
        valid={r['result']['receipt_sha256'] for r in rows}
        progress=engine.store.get('handoff_progress',prior['handoff_id']) or {}
        notes=[]
        for row in progress.get('tool_results',[]):
            if row.get('call',{}).get('name')=='experiments.record_notes' and row.get('result',{}).get('saved'):
                notes=[n for n in row['result'].get('notes',[]) if set(n.get('receipt_sha256s',[]))<=valid]
        result['experiment_notes']={'notes':notes,'provenance':'same_reviewer_saved_observations'}
        decision=(engine.store.get('decisions',prior['handoff_id']) or {}).get('result')
        if decision:
            result['rapid_revision']['prior_role_findings']={k:decision.get(k) for k in ('approve','rationale','issues')}
    return result


async def collect(engine,harness,run_id,record,common):
    from .rapid_experiment_revision import REVIEW,VOTE,COLLECTION
    from .rapid_review_rejection import authenticate_vote
    schema=record.get('review_schema') or VOTE
    contexts={role:role_context(engine,run_id,record,common,role) for role in ROLES}
    record['review_contexts']=contexts
    engine.store.put(COLLECTION,record['id'],record)
    tasks={asyncio.create_task(harness.ask(run_id,role,record['review_purpose'],REVIEW,contexts[role],schema)):role
           for role in ROLES}
    votes=[]
    try:
        pending=set(tasks)
        while pending:
            done,pending=await asyncio.wait(pending,return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                role=tasks[task]; vote={'role':role,**task.result()}
                authenticate_vote(engine,run_id,record,vote,contexts[role])
                votes.append(vote)
            if any(v.get('approve') is False and v.get('issues') for v in votes):
                record['review_interruption']={'reason':'authenticated_veto','pending_roles':sorted(tasks[t] for t in pending),
                    'completed_roles':sorted(v['role'] for v in votes),'created_at':now(), 'approval_inherited':False}
                for task in pending: task.cancel()
                await asyncio.gather(*pending,return_exceptions=True)
                engine.store.put(COLLECTION,record['id'],record)
                break
    finally:
        for task in tasks:
            if not task.done(): task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
    return sorted(votes,key=lambda v:ROLES.index(v['role']))


def prepare(engine, run_id, record_id, instruction):
    from . import rapid_experiment_revision as rapid, record_archive
    from .candidate_repair_source import read_file
    from .experiment_analysis import freeze_sources
    from .experiment_decisions import _archive
    from .review_metadata_audit import audit,GENERATED
    from . import rapid_post_exit
    from .engine import Harness
    run=engine.store.get('runs',run_id)
    record=engine.store.get(rapid.COLLECTION,record_id)
    if run['status'] not in {'paused','blocked'} or not instruction.strip() or record['status']!='reviewing':
        raise ValueError('Structured review recovery requires a stopped, unapproved review')
    if record.get('structured_review'):
        checked(record);return record['structured_review']
    if record.get('check') or record.get('votes'):
        raise ValueError('Do not rewrite a completed code/check decision')
    rapid._validate_base(engine,run_id,record);rapid._candidate_unchanged(record)
    original_context=rapid._review_context(engine,run_id,record)
    purpose=record.get('review_purpose') or 'rapid_review:'+record['candidate']['amendment_sha256']
    prior_record=record
    if (record.get('host_check_repair') or {}).get('reason') in {'pathlib_api_compatibility','windows_path_compatibility','delivery_invocation_receipts','windows_cwd_compatibility','full_acceptance_budget'}:
        ref=record['host_check_repair']['original'];saved=engine.store.get('experiment_evidence',ref['id']) or {}
        if saved.get('run_id')!=run_id or sha(saved.get('summary'))!=ref['sha256']:
            raise ValueError('Original host check review changed')
        prior_record=saved['summary']['rapid_revision_attempt'];purpose=prior_record['review_purpose']
    prior={}
    for role in ROLES:
        prior_context=prior_record.get('review_contexts',{}).get(role,original_context)
        key=Harness.decision_key(run_id,role,purpose,rapid.REVIEW,prior_context,prior_record.get('review_schema') or rapid.VOTE)
        frozen=engine.store.get('handoff_inputs',key)
        if frozen:
            prior[role]={'handoff_id':key,'input_sha256':sha(frozen),
                         'progress_sha256':sha(engine.store.get('handoff_progress',key) or {}),
                         'validation_evidence':prior_context['validation_evidence'],'approval_inherited':False}
    c=record['candidate'];bundle=c['review_bundle']
    files={r['path']:read_file(bundle['root'],r['path']) for r in bundle['files']}
    report=audit(files)
    contract={'version':1,'verifier':'_host_review/rapid_post_exit.py',
        'actual_call_site':'_host_review/check_dispatch.py::_check',
        'source_sha256':hashlib.sha256(Path(rapid_post_exit.__file__).read_bytes()).hexdigest(),
        'worker_return_and_failure_and_timeout':True,'candidate_module_imported':False,
        'pass_requires': 'Terminal runner receipt + matching candidate/check hashes + durable history/artifact hashes + complete main checks on success',
        'controlled_probe_is_real_timeout':False,'scientific_approval':False}
    # Complete host source is available to independent reviewers, not just a claim.
    files['_host_review/rapid_post_exit.py']=Path(rapid_post_exit.__file__).read_bytes()
    files['_host_review/delivery_files.py']=Path(__file__).with_name('delivery_files.py').read_bytes()
    files['_host_review/check_workspace.py']=Path(__file__).with_name('check_workspace.py').read_bytes()
    files['_host_review/check_budget_dispatch.py']=Path(__file__).with_name('rapid_check_budget.py').read_bytes()
    files['_host_review/check_worker_dispatch.py']=Path(__file__).with_name('lab.py').read_bytes()
    files['_host_review/check_dispatch.py']=Path(rapid.__file__).read_bytes()
    files['_host_review/metadata_audit.py']=Path(__file__).with_name('review_metadata_audit.py').read_bytes()
    files['_host_review/metadata_audit.json']=json.dumps(report,ensure_ascii=False,indent=2).encode()
    files['_host_review/post_exit_contract.json']=json.dumps(contract,ensure_ascii=False,indent=2).encode()
    root=engine.run_dir(run_id)/'structured-reviews'/sha({n:hashlib.sha256(v).hexdigest() for n,v in files.items()})
    review_bundle=rapid._write_files(root,files)
    sources=freeze_sources(engine,run_id,c['program'],review_bundle,delivery_support=True)
    ref=_archive(engine,run_id,{'binding':record['binding'],'code_sources':sources,'candidate_not_executed':True,
        'rapid_revision':{'patch':record['patch'],'decision':record['decision']},'metadata_audit':report,
        'host_post_exit_contract':contract,'original_validation_evidence':record['evidence']})
    required=[r for r in c['changed_ranges'] if r['path'] not in GENERATED]
    for name in files:
        if name.startswith('_host_review/') and name not in {'_host_review/check_dispatch.py','_host_review/check_worker_dispatch.py'}:
            required.append({'path':name,'start':0,'end':len(files[name].decode()),'sha256':hashlib.sha256(files[name]).hexdigest(),'scope':'host_contract'})
    # The full dispatch source stays inspectable; require the actual runner and gate functions.
    import ast
    worker='_host_review/check_worker_dispatch.py';worker_text=files[worker].decode()
    start=worker_text.index('            attempt = int((previous or {}).get("attempt", 0)) + 1')
    end=worker_text.index('            result = {"id": trial_id',start)
    required.append({'path':worker,'start':start,'end':end,'sha256':hashlib.sha256(files[worker]).hexdigest(),'scope':'host_check_directory_and_receipts'})
    name='_host_review/check_dispatch.py';text=files[name].decode();lines=text.splitlines(keepends=True)
    for node in ast.parse(text).body:
        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name in ('_check','_check_passed','_reviewed'):
            required.append({'path':name,'start':sum(map(len,lines[:node.lineno-1])),
                'end':sum(map(len,lines[:node.end_lineno])),'sha256':hashlib.sha256(files[name]).hexdigest(),'scope':'host_contract'})
    archive=record_archive.save(engine.store,run_id,record_archive.fingerprint(engine.store,rapid.COLLECTION,record_id))
    body={'version':3,'candidate_sha256':c['bundle']['sha256'],'amendment_sha256':c['amendment_sha256'],
        'checks_sha256':c['checks_sha256'],'required_ranges':required,'audit':report,'post_exit_contract':contract,
        'evidence':ref,'prior_reading':prior,'original_record':archive,'instruction':instruction,
        'approval_inherited':False,'scientific_approval':False}
    plan={'body':body,'sha256':sha(body)}
    from .rapid_check_budget import review_schema as budget_review_schema
    record['review_schema']=budget_review_schema(c,record.get('review_schema') or rapid.VOTE)
    record.update(structured_review=plan,post_exit_protocol=1,
        review_purpose=purpose+':structured:'+plan['sha256'],updated_at=now())
    engine.store.put(rapid.COLLECTION,record_id,record)
    engine.store.event(run_id,'experiment.structured_review_prepared',
        '生成材料全量结构化核对；保留独立源码审核及同角色原阅读，接入退出后证据复核。',
        {'revision_id':record_id,'plan_sha256':plan['sha256'],'required_chars':sum(r['end']-r['start'] for r in required),
         'original_required_chars':sum(r['end']-r['start'] for r in c['changed_ranges'])})
    return plan
