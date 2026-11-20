"""Receipt-referenced host diagnostics are not paper-source acquisition."""
from __future__ import annotations
import copy
import re
from . import host_execution_artifacts as artifacts

DOMAIN='host_execution'


def referenced_receipts(engine,run_id,source,issues):
    """Resolve explicit same-run host IDs, including earlier source versions."""
    from . import code_verification,hrp_host_timing
    text='\n'.join(str(issue.get('issue',''))+'\n'+str(issue.get('fixes',[])) for issue in issues)
    identities=set(re.findall(re.escape(run_id)+r':[a-f0-9]{64}(?![a-f0-9])',text))
    found={}
    for identity in sorted(identities):
        for collection in (code_verification.RECEIPTS,hrp_host_timing.RECEIPTS):
            record=engine.store.get(collection,identity)
            if record and record.get('run_id')==run_id:
                code_verification._sealed_receipt(engine,record,record['identity'])
                ref=copy.deepcopy(record)
                if collection!=code_verification.RECEIPTS:ref['receipt_collection']=collection
                found[identity]=ref
                break
    return list(found.values())


def facts(context):
    return [{'receipt_id':r['id'],'receipt_sha256':r['sha256'],'status':r.get('status'),
        'passed':r.get('passed'),'source_inventory':copy.deepcopy(r.get('identity',{}).get('source_sha256')),
        'recorded_error':r.get('error'),'host_exception_traceback_recorded':bool(r.get('error_traceback')),
        'input_staging_recorded':bool(r.get('input_staging')),
        'archived_artifacts':copy.deepcopy(r.get('artifacts',[])),
        'scope':'Exact sealed receipt fields; missing fields are not a reconstructed historical stack.',
        'scientific_approval':False} for r in artifacts.receipts(context)]


def reference_context(source,issues):
    for issue in issues:
        context=source.get('source_contexts',{}).get(issue.get('role'),{})
        entries=artifacts.catalog(context)
        if not entries:
            continue
        locations='\n'.join(str(f.get('location','')) for f in issue.get('fixes',[]))
        receipt=(context.get('host_verification') or {}).get('id','')
        if receipt and receipt in locations:
            return context
        if any(re.search(r'(?<![A-Za-z0-9])'+re.escape(alias)+r'(?![A-Za-z0-9])',locations)
               or item['artifact_id'] in locations or item['path'] in locations
               for alias,item in entries.items()):
            return context
    return None


def requested(source,issues):
    return reference_context(source,issues) is not None


def input_scope(context):
    """Mechanical comparison, not an invented historical per-file copy log."""
    ref=context.get('host_verification') or {}
    inventory=(ref.get('identity') or {}).get('source_sha256')
    if not isinstance(inventory,dict):
        return {'receipt_id':ref.get('id'),'receipt_sha256':ref.get('sha256'),
                'source_inventory_available':False,'scientific_approval':False}
    return {'receipt_id':ref['id'],'receipt_sha256':ref['sha256'],
        'source_inventory_available':True,
        'source_inventory':copy.deepcopy(inventory),
        'delivery_files_outside_check_input_inventory':[
            {k:row[k] for k in ('artifact_id','path','sha256') if k in row}
            for row in context.get('delivery_artifacts',[]) if row['path'] not in inventory],
        'scope':'Frozen bounded checker input inventory, distinct from the complete archived delivery.',
        'staging_contract':'The source inventory is copied into the isolated checker. Archived test_result_files are excluded from that source inventory.',
        'historical_copy_log':copy.deepcopy(ref.get('input_staging')),
        'historical_copy_log_recorded':bool(ref.get('input_staging')),
        'scientific_approval':False,'execution_authorized':False}


def records(engine,run_id,task,result):
    frozen=engine.store.get('handoff_inputs',task['handoff_id'],{})
    hand=engine.store.get('handoffs',task['handoff_id'],{})
    context=frozen.get('context',{})
    if (hand.get('run_id')!=run_id or hand.get('to_role')!=task['owner']
            or context.get('implementation_evidence_task',{}).get('binding')!=task['binding']):
        raise ValueError('Host evidence read belongs to another frozen work item')
    actual=engine.store.get('handoff_progress',task['handoff_id'],{}).get('tool_results',[])
    rows=[]
    for identity in result['evidence_ids']:
        matches=[t for t in actual if t.get('call',{}).get('name')=='artifacts.read'
            and t.get('result',{}).get('artifact_id')==identity
            and t.get('result',{}).get('provenance')=='sealed_host_execution_artifact'
            and isinstance(t.get('result',{}).get('text'),str) and not t['result'].get('error')]
        for row in matches:
            exact=artifacts.read(engine,run_id,task['owner'],task['handoff_id'],row['call']['arguments'])
            if row['result']!=exact:
                raise ValueError('Host evidence read changed after delivery')
            if row not in rows:rows.append(copy.deepcopy(row))
    if result['status']=='completed' and not rows:
        raise ValueError('Host evidence requires this role\'s actual receipt-bound reads')
    return rows


INSTRUCTION=('本组是宿主执行证据核验，不是论文检索。读取 host_execution_artifacts 中实际所需的 artifact_id 与 sha256，'
    '核对失败摘要、冻结交付清单及 host_check_input_scope 的隔离输入边界；可按需读取交付文件和原条款。'
    '原读取与已完成检查的历史状态按实际记录复用，不扩大为整体通过。返回实际 artifacts.read 结果的 artifact_id。'
    '区分完整交付目录、隔离输入目录及新执行输出；旧回执未记录的逐文件复制日志保持未知，'
    'host_receipt_facts 明确列出原回执实际保存的错误、调用栈字段及归档附件；未记载的历史调用栈保持未知，'
    '将历史归因未知与当前源码及已完成检查分开交给原角色决定，不因缺字段循环检索普通研究记忆或重跑已通过阶段。'
    '不要把未要求的历史日志变成新增验收门禁，也不要为满足隔离检查而补造或删除历史摘要。'
    '只核对原问题与最小处置依据，不修改源码、文档或启动实验；结论由原角色后续独立判断。')
