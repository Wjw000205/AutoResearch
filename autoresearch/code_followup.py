"""Route missing execution and missing definitions without fabricating edits."""
from __future__ import annotations

import copy
from . import code_repair_contract as repair, code_repair_clarification as clarification
from . import code_verification as verification
from .storage import now

GUIDANCE = '''
host_verification 是宿主本轮真实有界检查回执，包含执行范围、同源哈希、失败前缀和未执行限制。
本轮只依据新增回执复核原问题，不重做已完成且未变化的阅读；可恢复 document_memory_handoffs 中同版本文档。
原补查结论在 previous_clarified_findings；已纠正的误报不要重新提出。
沿用原补查纠正后的结论时使用 corrected 并说明依据，不得为了 retained 的旧原文约束而恢复已经撤回的要求。
实际断言失败：按原实现文档定位代码缺陷，repair_code 给出具体位置、原要求和最小修复。
已有实现但某必要原用例未运行：needs_execution，明确缺少哪条原断言，不要求再次实现已有代码。
批准文档未给出必要定义：needs_definition，明确条款缺口；不得发明测量方法、探针、模型或验收门槛。
资料没读齐才用 needs_review；不把真实测试失败当成缺文献，也不把未执行当失败或通过。
不要求在代码实现阶段重复完整分支训练；正式资源探针和训练仍按原冻结计划执行与计账。
宿主 bounded_checks 的通过仅支持实际完成的断言；原资源验收中的未解决项继续保留。
'''


def enabled(engine, run_id, state):
    policy = verification.policy(engine, run_id)
    return bool(policy and policy['workflow_id'] == state['id'])


def evidence(receipt, attempt):
    keys = ('id','sha256','status','passed','identity','allowance_seconds','duration_seconds',
            'inherited_seconds','shared_allowance_seconds','exit_code','command',
            'stdout_tail','stderr_tail','error','artifacts')
    value = {k:copy.deepcopy(receipt[k]) for k in keys if k in receipt}
    # Source hashes occur once in identity; the original reports remain sealed
    # artifacts. Do not repeat every file hash inside each report in context.
    value['test_reports'] = {name:{k:copy.deepcopy(v) for k,v in report.items()
        if k != 'source_sha256'} for name,report in receipt.get('test_reports',{}).items()
        if isinstance(report,dict)}
    value['document_memory_handoffs'] = [{k:v[k] for k in ('role','handoff_id')}
        for v in [*attempt.get('review_votes',[]), *attempt.get('repair_clarification',{}).get('votes',[])]]
    value['previous_clarified_findings'] = [{k:copy.deepcopy(v[k]) for k in ('role','issues','fixes','repair_findings')}
        for v in attempt.get('repair_clarification',{}).get('votes',[])]
    return value


def validate_evidence(engine, run_id, attempt, value):
    from . import hrp_host_timing
    if value.get('receipt_collection') == hrp_host_timing.RECEIPTS:
        hrp_host_timing.validate(engine,run_id,attempt,value)
        return
    receipt = engine.store.get(verification.RECEIPTS,value['id'])
    if not receipt or receipt['run_id'] != run_id:
        repair.fail('宿主测试回执不存在或属于其他任务。')
    verification._sealed_receipt(engine,receipt,receipt['identity'])
    if evidence(receipt,attempt) != value:
        repair.fail('宿主测试复审的冻结执行证据发生变化。')


def record_definitions(engine, run_id, state, source):
    requests = []
    for issue in source['issues']:
        if issue['binding']['disposition'] not in {'needs_definition','spec_conflict'}:
            continue
        binding = issue['binding']
        value = {'run_id':run_id,'workflow_id':state['id'],'source_sha256':repair.sha(source),
            'implementation_document':source['implementation_document'],'issue':copy.deepcopy(issue),
            'references':[copy.deepcopy(source['role_evidence'][issue['role']][i]) for i in binding['evidence_indices']],
            'status':'definition_required','code_dispatch_authorized':False,'scientific_approval':False,
            'next_action':'仅由实现文档作者补齐所引条款并经独立审核；不得以代码指令代替文档定义。'}
        key=run_id+':'+repair.sha(value)
        previous=engine.store.get('implementation_definition_requests',key)
        if not previous:
            engine.store.put('implementation_definition_requests',key,{'id':key,**value,'created_at':now()})
        requests.append(key)
    return requests


async def prepare(engine,h,run_id,state,specification,attempt,source,save):
    """Skip the extra mapping round when independent issue bindings exist."""
    if any(not i.get('binding') for i in source['issues']):
        return await repair._legacy_proposal(engine,h,run_id,state,specification,attempt,source,save)
    if all(i['binding']['disposition']=='repair_code' for i in source['issues']):
        return source,clarification.mapping(source),None
    cached=attempt.get('verification_followup')
    from . import definition_code_followup, definition_execution_followup
    if (definition_code_followup.applicable(state,specification)
            and attempt.get('acceptance',{}).get('definition_checks_deferred')
            and not any(i['binding']['disposition'] in {'repair_code','needs_definition','spec_conflict'} for i in source['issues'])):
        resolved,proposal,cached=await definition_execution_followup.prepare(
            engine,h,run_id,state,specification,attempt,source,save)
    elif cached and cached.get('status')=='completed':
        resolved=await engine._read_consensus_check(run_id,clarification.verify,
            engine,run_id,state,specification,attempt,source,cached)
        proposal=clarification.mapping(resolved)
    elif (attempt.get('acceptance',{}).get('bounded_checks_only')
            or attempt.get('acceptance',{}).get('definition_checks_deferred')):
        # These votes already consumed the same real test receipt. Do not ask
        # the same three reviewers a second time before routing their findings.
        resolved,proposal,cached=source,clarification.mapping(source),None
    else:
        attempt['execution_progress']={'step':'bounded_checks','label':'宿主执行原有界测试，保留已完成阶段；不启动训练',
                                       'started_at':now(),'updated_at':now()}
        save(status='acceptance',blocked_kind=None,reason=None)
        receipt=await verification.run(engine,run_id,attempt['snapshot']['workspace'],cancel=engine.cancellations.get(run_id))
        engine.check_active(run_id)
        resolved,proposal,cached=await clarification.clarify(engine,h,run_id,state,specification,attempt,
            source,clarification.mapping(source),save,host_verification=evidence(receipt,attempt),record_key='verification_followup')
    definitions=record_definitions(engine,run_id,state,resolved)
    attempt['definition_request_ids']=definitions
    if definitions:
        from .implementation_definition_repair import begin
        from .implementation_failures import ImplementationSpecificationPending
        begin(engine,run_id,state,specification,attempt,resolved,definitions,save)
        raise ImplementationSpecificationPending()
    if resolved['issues'] and not any(i['binding']['disposition']=='repair_code' for i in resolved['issues']):
        from .implementation_failures import ImplementationCodeBlocked
        kind='codex_definition_required' if definitions else 'codex_verification_pending'
        message=('实现文档缺少必要定义：已保存对应条款与原审查问题，等待定向补定义和独立审核。' if definitions else
                 '宿主有界测试已有真实回执；仍有原用例未完成，等待按剩余额度安排，未重复训练或重写代码。')
        attempt['execution_progress']={'step':'definition_required' if definitions else 'verification_pending',
            'label':message,'started_at':now(),'updated_at':now()}
        save(status='blocked',blocked_kind=kind,reason=message)
        raise ImplementationCodeBlocked(message,kind=kind,details={'workflow_id':state['id'],
            'attempt':attempt['index'],'definition_request_ids':definitions,
            'host_verification_receipt_id':(cached or {}).get('host_verification',
                attempt.get('acceptance',{}).get('host_verification',{})).get('id')})
    return resolved,proposal,cached
