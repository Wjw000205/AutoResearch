"""One same-reviewer correction of citations omitted from a completed vote.

Reading, selected citations and approval are separate facts. Only authentic,
already consumed source pages may repair the citation list. Original votes,
requests, notes, candidate bytes and execution gates remain unchanged.
"""
import copy
import hashlib

from .experiment_workflow import sha
from .storage import now


def inventory(engine, run_id, record, vote, context):
    from .rapid_review_rejection import authenticate_vote
    from .rapid_experiment_revision import REVIEW,VOTE
    from .engine import Harness
    from .rapid_review_plan import inherited_rows
    from .dataset_review_protocol import remaining_ranges
    authenticate_vote(engine,run_id,record,vote,context)
    key=Harness.decision_key(run_id,vote['role'],record.get('review_purpose') or
        'rapid_review:'+record['candidate']['amendment_sha256'],REVIEW,context,record.get('review_schema') or VOTE)
    progress=engine.store.get('handoff_progress',key) or {}
    evidence=engine.store.get('experiment_evidence',context['validation_evidence']['id']) or {}
    if evidence.get('run_id')!=run_id or sha(evidence.get('summary'))!=context['validation_evidence']['sha256']:
        raise ValueError('Citation correction evidence identity changed')
    ref=evidence['summary']['code_sources']
    source=engine.store.get('experiment_sources',ref['id']) or {}
    if source.get('run_id')!=run_id or sha(source.get('files'))!=ref['sha256']:
        raise ValueError('Citation correction source identity changed')
    inherited=inherited_rows(engine,run_id,vote['role'],context) if record.get('structured_review') else []
    valid={r['result']['receipt_sha256']:r for r in inherited}
    for row in progress.get('tool_results',[]):
        call,result=row.get('call',{}),row.get('result',{});args=call.get('arguments',{})
        item=source['files'].get(result.get('path'),{})
        if (call.get('name')=='experiments.inspect_module' and args.get('view')=='source'
                and args.get('evidence_id')==context['validation_evidence']['id']
                and args.get('expected_sha256')==context['validation_evidence']['sha256']
                and result.get('evidence_sha256')==context['validation_evidence']['sha256']
                and result.get('source_read') is True and result.get('source_sha256')==item.get('sha256')
                and type(result.get('offset')) is int and type(result.get('end')) is int
                and 0<=result['offset']<result['end']<=len(item.get('text',''))
                and args.get('path')==result['path'] and args.get('offset',0)==result['offset']
                and result.get('text')==item.get('text','')[result['offset']:result['end']]
                and result.get('receipt_sha256')==sha({'call':call,'result':{k:v for k,v in result.items() if k!='receipt_sha256'}})):
            valid[result['receipt_sha256']]=row
    # A tool return alone may not have been consumed before an interruption.
    # Use pages the reviewer cited or consolidated in its own saved notes.
    consumed={r['result']['receipt_sha256'] for r in inherited} | (set(vote['read_receipts']) & set(valid))
    for row in progress.get('tool_results',[]):
        if row.get('call',{}).get('name')=='experiments.record_notes' and row.get('result',{}).get('saved'):
            for note in row['call'].get('arguments',{}).get('notes',[]):
                refs=set(note.get('receipt_sha256s',[]))
                if note in row['result'].get('notes',[]) and refs<=set(valid):consumed.update(refs)
    required=context.get('rapid_revision',{}).get('changed_ranges') or record['candidate']['changed_ranges']
    relevant={(r['path'],r['sha256']) for r in required}
    pages=[{k:valid[ref]['result'][k] for k in ('receipt_sha256','path','source_sha256','offset','end')}
           for ref in sorted(consumed) if (valid[ref]['result']['path'],valid[ref]['result']['source_sha256']) in relevant]
    return {'handoff_id':key,'input_sha256':sha(engine.store.get('handoff_inputs',key)),
        'progress_sha256':sha(progress),'pages':pages,'missing':remaining_ranges(required,pages),
        'read_receipts':sorted(set(vote['read_receipts']) | {r['receipt_sha256'] for r in pages})}


def verify(engine,run_id,record,vote,context,assignment):
    from .engine import Harness,digest,ResearchBlocked
    from .prose_schema import validate
    body=assignment['body']
    if sha(body)!=assignment['sha256'] or vote['role']!=body['role']:
        raise ResearchBlocked('Citation correction assignment changed')
    original={'role':vote['role'],**body['original_vote']}
    expected={**original,'read_receipts':body['inventory']['read_receipts']}
    if vote!=expected:
        raise ResearchBlocked('Citation correction changed the original review decision')
    observed=inventory(engine,run_id,record,original,context)
    if observed!=body['inventory'] or observed['missing']:
        raise ResearchBlocked('Citation correction lost original consumed reading evidence')
    key=Harness.decision_key(run_id,vote['role'],body['purpose'],body['instruction'],body['context'],body['schema'])
    response=(engine.store.get('decisions',key) or {}).get('result')
    owner=engine.store.get('handoffs',key) or {};frozen=engine.store.get('handoff_inputs',key) or {}
    if (owner.get('run_id')!=run_id or owner.get('to_role')!=vote['role'] or owner.get('purpose')!=body['purpose']
            or owner.get('status')!='completed' or owner.get('output_hash')!=digest(response)
            or frozen.get('context')!=body['context'] or frozen.get('instruction')!=body['instruction']
            or frozen.get('schema')!=body['schema']):
        raise ResearchBlocked('Citation correction has no authentic same-reviewer response')
    validate(response,body['schema'])
    if response['confirmed'] is not True:
        raise ResearchBlocked('Original reviewer did not confirm the citation correction: '+response['explanation'])
    return key


async def repair(engine,harness,run_id,record,votes,context):
    from .rapid_experiment_revision import review_receipts,COLLECTION,_base_unchanged,_candidate_unchanged
    from .engine import ResearchBlocked
    from .training_adjustment import obj
    repaired=[]
    for vote in votes:
        role=vote['role'];ctx=record.get('review_contexts',{}).get(role,context)
        if vote.get('approve') is not True or vote.get('issues'):
            repaired.append(vote);continue
        assignments=record.setdefault('citation_corrections',{})
        assignment=assignments.get(role)
        if assignment:
            body=assignment['body']
            if {k:v for k,v in vote.items() if k!='role'}!=body['original_vote']:
                raise ResearchBlocked('Citation correction no longer matches the original vote')
            if assignment.get('resolved_vote'):
                verify(engine,run_id,record,assignment['resolved_vote'],ctx,assignment)
                repaired.append(assignment['resolved_vote']);continue
        else:
            proof=review_receipts(engine,run_id,record,vote,ctx,require_complete=False)
            if proof['coverage_complete']:
                repaired.append(vote);continue
            available=inventory(engine,run_id,record,vote,ctx)
            record.setdefault('citation_diagnostics',{})[role]={
                'kind':'source_gap' if available['missing'] else 'citation_omission',
                'original_cited_count':len(vote['read_receipts']), 'available_receipt_count':len(available['read_receipts']),
                'uncited_ranges':proof['missing_ranges'],'unread_ranges':available['missing']}
            engine.store.put(COLLECTION,record['id'],record)
            if available['missing']:
                repaired.append(vote);continue  # Real unread source stays blocked by the original gate.
            maximum=(record.get('review_schema') or {}).get('properties',{}).get('read_receipts',{}).get('maxItems',256)
            if len(available['read_receipts'])>maximum:
                raise ResearchBlocked('Complete citation list exceeds its frozen review schema; original evidence retained')
            original={k:v for k,v in vote.items() if k!='role'}
            manifest_sha=sha(available)
            correction={'version':1,'role':role,'original_vote':original,'original_vote_sha256':sha(original),
                'original_handoff_id':available['handoff_id'],'receipt_manifest_sha256':manifest_sha,
                'consumed_source_pages':available['pages'],'complete_read_receipts':available['read_receipts'],
                'approval_changed':False,'new_reading_credit':False}
            payload={k:copy.deepcopy(ctx[k]) for k in ('training_context_protocol','experiment_workflow_protocol',
                'research_cycle','validation_evidence','evidence_sha256') if k in ctx}
            payload['rapid_revision']={k:ctx['rapid_revision'][k] for k in ('id','amendment_sha256','candidate_sha256','checks_sha256')}
            payload['rapid_revision']['citation_correction']=correction
            schema=obj({'original_vote_sha256':{'const':sha(original)},'receipt_manifest_sha256':{'const':manifest_sha},
                'read_receipts':{'const':available['read_receipts']},'confirmed':{'type':'boolean'},
                'explanation':{'type':'string','maxLength':500}})
            body={'role':role,'original_vote':original,'inventory':available,'context':payload,'schema':schema,
                'purpose':record.get('review_purpose','rapid_review:'+record['candidate']['amendment_sha256'])+':citations:'+sha(correction),
                'instruction':'仅补正你本人已完成代码审核的回执引用列表。宿主已逐条核验同角色、同候选、原始 source 回执及已保存笔记，必读范围没有缺口；原答案只引用了其中一部分。'
                '核对 citation_correction 中的原投票与完整已读清单，确认后返回 confirmed=true 及完整 read_receipts；不能核实则 confirmed=false 并说明。'
                '不改变 approve、issues、rationale、候选SHA或任何原审核判断；这些原字段由宿主原样保留。本次不调用工具、不重读源码、不写代码、不训练、不扩大预算。返回指定JSON。'}
            assignment={'body':body,'sha256':sha(body)};assignments[role]=assignment
            engine.store.put(COLLECTION,record['id'],record)
            engine.store.event(run_id,'experiment.review_citations_requested','实际阅读已完整，仅向原审阅人补正遗漏的回执引用。',
                {'revision_id':record['id'],'role':role,'original_handoff_id':available['handoff_id'],
                 'original_cited_count':len(vote['read_receipts']),'complete_receipt_count':len(available['read_receipts'])})
        response=await harness.ask(run_id,role,body['purpose'],body['instruction'],body['context'],body['schema'])
        _base_unchanged(engine,run_id,record);_candidate_unchanged(record)
        resolved={'role':role,**body['original_vote'],'read_receipts':body['inventory']['read_receipts']}
        correction_key=verify(engine,run_id,record,resolved,ctx,assignment)
        assignment.update(resolved_vote=resolved,completed_at=now(),handoff_id=correction_key)
        engine.store.put(COLLECTION,record['id'],record)
        repaired.append(resolved)
    return repaired
