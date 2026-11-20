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
    return bool(verification.policy(engine, run_id, workflow_id=state['id']))


def evidence(receipt, attempt):
    keys = ('id','sha256','status','passed','identity','allowance_seconds','duration_seconds',
            'inherited_seconds','shared_allowance_seconds','exit_code','command',
            'stdout_tail','stderr_tail','error','artifacts','error_phase','error_traceback','input_staging')
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


def record_definitions(engine, run_id, state, source, *, issue_ids=None):
    requests = []
    for issue in source['issues']:
        if issue_ids is not None and issue['issue_id'] not in issue_ids:
            continue
        if (issue.get('binding') or {}).get('disposition') not in {'needs_definition','spec_conflict'}:
            continue
        from .host_constraint_reconciliation import owns as host_owns
        if host_owns(issue):
            continue  # Actual host policy/ledger facts cannot be authored into a SPEC.
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


