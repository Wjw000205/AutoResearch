"""Scoped amendment of an unexecuted rejected contrast; preserve owned reads."""
import copy
from difflib import SequenceMatcher

from .experiment_workflow import sha
from .storage import now

ARCHIVES='causal_followup_repair_archives'
GUIDANCE='''本次仅修复上一版三方指出的状态证据和中断保存缺项，单种子64步方案及判据不变。
rapid_revision.prior_review 保留你本人原意见；inherited_source_coverage 是经同角色回执与新旧源码逐字相等区间核验的复用，不是新阅读，也不继承批准。
只需读取 unread_required_ranges 中修改的区间、新增模块和当前 /controlled_admission；必要时读取具体关联上下文，不要从头重读全部旧源码。
修改后的核心对照程序和判据需全文审核；其余已读相同材料可复用。合并邻近缺项分页，最终只引用覆盖所需范围的回执，最多24个。
最终 read_receipts 可包含 prior_review.inherited_receipts 和本次新读回执，必须独立重新判断修改是否解决原异议。
重点核验非有限事件真实前后状态与批次配对、缺证据不能判阳性、零更新状态可重建、原子提交及中断后有效前缀。离线检查不代表本次训练已执行。'''


def register(engine,run_id,authorization,test_evidence):
    from .causal_training_followup import COLLECTION
    from .controlled_training_checks import unchanged
    key=f'{run_id}:{engine.cycle(run_id)}';old=engine.store.get(COLLECTION,key)
    if (engine.store.get('runs',run_id)['status'] not in {'blocked','paused'} or not authorization
            or not old or old['status']!='rejected' or old.get('trial_id')
            or len(old.get('votes',[]))!=3 or all(v['approve'] for v in old['votes'])):
        raise ValueError('Requires an explicitly authorized, rejected, unexecuted contrast')
    trial='causal-check-'+old['candidate']['amendment_sha256'][:24]
    if engine.store.get('experiments',run_id+':'+trial):raise ValueError('Executed contrasts cannot be amended or replayed')
    # This operator repair changes host code; original inputs and candidate remain immutable.
    unchanged(engine,{**old,'host_sources':{}})
    archive=key+':'+old['candidate']['amendment_sha256']
    existing=engine.store.get(ARCHIVES,archive)
    if existing and existing!=old:raise ValueError('Original rejected review archive changed')
    engine.store.put(ARCHIVES,archive,old)
    retained=('id','run_id','cycle','binding','authorization','created_at','cpu_revision_id','cpu_check',
              'suggestions','planning_reads','planning_context','plan','scientific_approval')
    record={k:copy.deepcopy(old[k]) for k in retained if k in old}
    record.update(status='planning',updated_at=now(),test_evidence=copy.deepcopy(test_evidence),
        prior_review={'id':archive,'sha256':sha(old),'authorization':authorization,'approval_inherited':False})
    engine.store.put(COLLECTION,key,record)
    engine.store.event(run_id,'causal_followup.repair','按三方具体异议修复未执行候选；保留原方案、拒绝意见和读取回执',record['prior_review'])
    return record


def inherited_rows(engine,record,role):
    reference=record.get('prior_review')
    if not reference:return []
    old=engine.store.get(ARCHIVES,reference['id'])
    if not old or sha(old)!=reference['sha256']:raise ValueError('Prior review archive changed')
    from .controlled_training_checks import review_context
    from .controlled_review_completion import _owned,_authentic
    context=review_context(engine,old)
    vote=next(v for v in old['votes'] if v['role']==role)
    key=_owned(engine,old,vote,context,old['review_purpose'],old['review_instruction'])
    rows=_authentic(engine,old,context,key,vote['read_receipts'])
    if {r['result']['receipt_sha256'] for r in rows}!=set(vote['read_receipts']):
        raise ValueError('Prior review has unauthenticated receipts')
    snapshots=[]
    for candidate in (old['candidate'],record['candidate']):
        ref=candidate['code_sources'];snapshot=engine.store.get('experiment_sources',ref['id'])
        if not snapshot or sha(snapshot['files'])!=ref['sha256']:raise ValueError('Source archive changed')
        snapshots.append(snapshot['files'])
    prior,current=snapshots;matches={};retained=[]
    for row in rows:
        r=row['result'];path=r.get('path')
        if not r.get('source_read') or path not in prior or path not in current:continue
        if r['source_sha256']!=prior[path]['sha256']:continue
        if path in {'diagnostic_probe.py','diagnostic_verdicts.py'} and prior[path]['sha256']!=current[path]['sha256']:continue
        if path not in matches:
            left,right=prior[path]['text'].splitlines(keepends=True),current[path]['text'].splitlines(keepends=True)
            def offsets(lines):
                result=[0]
                for line in lines:result.append(result[-1]+len(line))
                return result
            aoff,boff=offsets(left),offsets(right)
            matches[path]=[(aoff[a],boff[b],aoff[a+n]-aoff[a]) for a,b,n in
                SequenceMatcher(None,left,right,autojunk=False).get_matching_blocks()]
        coverage=[]
        for a,b,size in matches[path]:
            lo,hi=max(a,r['offset']),min(a+size,r['end'])
            if hi>lo:coverage.append({'path':path,'source_sha256':current[path]['sha256'],
                'offset':b+lo-a,'end':b+hi-a,'receipt_sha256':r['receipt_sha256'],
                'inherited':True,'original_source_sha256':r['source_sha256'],
                'original_offset':lo,'original_end':hi,'original_handoff_id':key})
        if coverage:retained.append({**row,'inherited_coverage':coverage})
    return retained


def context_for(engine,record,context,role):
    if not record.get('prior_review'):return context
    from .dataset_review_protocol import remaining_ranges
    rows=inherited_rows(engine,record,role)
    coverage=[c for row in rows for c in row['inherited_coverage']]
    old=engine.store.get(ARCHIVES,record['prior_review']['id'])
    prior=next(v for v in old['votes'] if v['role']==role)
    result=copy.deepcopy(context)
    result['rapid_revision'].update(inherited_source_coverage=coverage,
        prior_review={'archive':record['prior_review'],'original_vote':prior,
            'inherited_receipts':sorted({r['result']['receipt_sha256'] for r in rows}),
            'unread_required_ranges':remaining_ranges(record['candidate']['changed_ranges'],coverage),
            'approval_inherited':False})
    return result
