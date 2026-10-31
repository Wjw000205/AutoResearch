"""One same-reviewer supplement; original paid inputs and decisions stay immutable."""
import copy
from .experiment_workflow import sha

SUPPLEMENT = '''补齐你本人本次受控诊断评审漏读的证据，然后独立重新确认或拒绝。
rapid_revision.source_completion 中保留你本人原答复及真实读取索引；这些索引不是新阅读或宿主批准。
只需读取 missing_ranges 的缺失源码区间（experiments.inspect_module，snapshot=candidate，view=source，limit=6000，全部分页）以及缺失的 controlled_admission 分页。
不得从头重读其他已完成源码、补日志、运行训练或替其他角色表决。读取后重新判断原意见是否成立，允许提出新异议。
返回同一候选的三个哈希、approve、rationale、issues 和真实 read_receipts；可引用 source_completion.inherited_receipts 加上你本次实际新读回执，覆盖所有必要区间和宿主额度台账。'''


def _owned(engine, record, vote, context, purpose, instruction):
    from .engine import Harness
    from .rapid_experiment_revision import VOTE
    key=Harness.decision_key(record['run_id'],vote['role'],purpose,instruction,context,VOTE)
    owner=engine.store.get('handoffs',key) or {}
    frozen=engine.store.get('handoff_inputs',key) or {}
    saved=(engine.store.get('decisions',key) or {}).get('result')
    if (owner.get('run_id')!=record['run_id'] or owner.get('to_role')!=vote['role']
            or frozen.get('context')!=context or saved!={k:v for k,v in vote.items() if k!='role'}):
        raise ValueError('Controlled review owner, frozen input or original decision changed')
    return key


def _authentic(engine, record, context, key, refs):
    from .experiment_analysis import inspect_module
    from .experiment_tools import compare
    rows=[]
    for row in (engine.store.get('handoff_progress',key) or {}).get('tool_results',[]):
        call,value=row.get('call',{}),row.get('result',{})
        if value.get('receipt_sha256') not in refs:continue
        body={k:v for k,v in value.items() if k!='receipt_sha256'}
        if value['receipt_sha256']!=sha({'call':call,'result':body}):continue
        fn={'experiments.inspect_module':inspect_module,'experiments.compare':compare}.get(call.get('name'))
        if fn and not value.get('error') and fn(engine,record['run_id'],context,call['arguments'])==body:
            rows.append(row)
    return rows


def _coverage(record, rows):
    from .dataset_review_protocol import remaining_ranges
    sources=[v for r in rows for v in (r.get('inherited_coverage',[]) if 'inherited_coverage' in r
        else [r['result']] if r['result'].get('source_read') else [])]
    missing=remaining_ranges(record['candidate']['changed_ranges'],sources)
    windows=[r['result'] for r in rows if r['call'].get('name')=='experiments.compare'
             and r['call'].get('arguments',{}).get('path')=='/controlled_admission']
    end=0
    for r in sorted(windows,key=lambda r:r['offset']):
        if r['offset']>end:break
        end=max(end,r['end'])
    budget=not record.get('admission') or bool(windows and end==windows[0]['total_chars'])
    return missing,budget


def validate(engine, record, vote, context):
    from .controlled_training_checks import REVIEW
    context=record.get('review_contexts',{}).get(vote['role'],context)
    purpose=record.get('review_purpose') or 'rapid_review:controlled:'+record['candidate']['amendment_sha256']
    supplement=record.get('source_completions',{}).get(vote['role'])
    original=supplement['original_vote'] if supplement else vote
    key=_owned(engine,record,original,context,purpose,record.get('review_instruction') or REVIEW)
    rows=_authentic(engine,record,context,key,original['read_receipts'])
    if record.get('prior_review'):
        from .causal_review_repair import inherited_rows
        rows += [r for r in inherited_rows(engine,record,vote['role'])
                 if r['result']['receipt_sha256'] in original['read_receipts']]
    original_refs={r['result']['receipt_sha256'] for r in rows}
    if original_refs!=set(original['read_receipts']):
        raise ValueError('Controlled review contains unauthenticated original receipts')
    if supplement:
        if (supplement['context']['validation_evidence']!=context['validation_evidence']
                or supplement['original_handoff']!=key):
            raise ValueError('Controlled supplement evidence or ancestry changed')
        newkey=_owned(engine,record,vote,supplement['context'],supplement['purpose'],SUPPLEMENT)
        rows += _authentic(engine,record,supplement['context'],newkey,vote['read_receipts'])
    else:newkey=key
    cited=[r for r in rows if r['result']['receipt_sha256'] in vote['read_receipts']]
    valid={r['result']['receipt_sha256'] for r in cited}
    if valid!=set(vote['read_receipts']):
        raise ValueError('Controlled review cited another owner or unauthenticated receipt')
    missing,budget=_coverage(record,cited)
    return {'role':vote['role'],'handoff_id':newkey,'original_handoff_id':key,
            'receipt_sha256s':sorted(valid),'missing_ranges':missing,'budget_complete':budget,
            'complete':not missing and budget,'approval_inherited':False},cited


async def complete(engine,harness,record,context,save):
    from .engine import ResearchBlocked
    from .rapid_experiment_revision import VOTE
    proofs=[]
    base_context=context
    for index,vote in enumerate(record['votes']):
        context=record.get('review_contexts',{}).get(vote['role'],base_context)
        assignment=record.get('source_completions',{}).get(vote['role'])
        # Resume an interrupted supplement through Harness' immutable decision cache.
        if not assignment:
            proof,rows=validate(engine,record,vote,context)
            if proof['complete'] or not vote['approve'] or vote['issues']:
                proofs.append(proof);continue
            newcontext=copy.deepcopy(context)
            inherited=[r['result']['receipt_sha256'] for r in rows]
            newcontext['rapid_revision']['source_completion']={'original_handoff':proof['handoff_id'],
                'original_vote':copy.deepcopy(vote),'missing_ranges':proof['missing_ranges'],
                'controlled_admission_complete':proof['budget_complete'],
                'inherited_receipts':sorted(set(inherited)), 'approval_inherited':False}
            newcontext['experiment_review_evidence']={'source_reads':[{k:r['result'][k] for k in
                ('receipt_sha256','path','source_sha256','offset','end')} for r in rows if r['result'].get('source_read')],
                'scientific_approval':False}
            assignment={'original_vote':copy.deepcopy(vote),'original_handoff':proof['handoff_id'],
                'context':newcontext,'purpose':(record.get('review_purpose') or 'rapid_review:controlled:'+record['candidate']['amendment_sha256'])+':source-completion-1'}
            additions={**record.get('source_completions',{}),vote['role']:assignment}
            save(source_completions=additions)
        response=await harness.ask(record['run_id'],vote['role'],assignment['purpose'],SUPPLEMENT,assignment['context'],VOTE)
        answer={'role':vote['role'],**response}
        proof,_=validate(engine,record,answer,context)
        if not proof['complete']:
            raise ResearchBlocked('受控诊断补读仍未覆盖必要源码或宿主额度台账；不重复派发或执行。')
        votes=copy.deepcopy(record['votes']);votes[index]=answer;save(votes=votes)
        proofs.append(proof)
    return proofs
