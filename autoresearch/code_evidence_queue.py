"""Serial evidence work groups; each group's independent review stays parallel.

Acquisition is not approval. Immutable original decisions, completed checks and
source-bound review receipts are reused; a blocked group cannot veto unrelated
groups or authorize a code edit.
"""
from __future__ import annotations

import copy
import json

from . import code_repair_contract as repair
from .storage import now, redact

COLLECTION = 'implementation_evidence_tasks'
FIELD = 'implementation_evidence_work_item'
RECORD = 'deferred_evidence_clarification'
VERSION = 1

SOURCE_SCHEMA = repair.obj({'status': {'enum': ['completed','unavailable']},
    'evidence_ids': {'type':'array','maxItems':12,'uniqueItems':True,'items':repair.TEXT},
    'summary':repair.TEXT, 'remaining': {'type':'array','maxItems':12,'items':repair.TEXT}})
PLAN_SCHEMA = repair.obj({'ready': {'type':'boolean'}, 'entrypoint': {'type':'string','maxLength':1000},
    'requirement_ids': {'type':'array','uniqueItems':True,'items':repair.TEXT},
    'report_paths': {'type':'array','uniqueItems':True,'maxItems':30,'items':repair.TEXT},
    'timeout_seconds': {'type':['number','null'],'exclusiveMinimum':0},
    'allowance_reference': repair.obj({'requirement_id':{'type':'string','maxLength':100},
        'quote':{'type':'string','maxLength':6000}}),
    'summary':repair.TEXT, 'remaining': {'type':'array','maxItems':12,'items':repair.TEXT}})
from .code_check_invocation import SCHEMA as INVOCATION_SCHEMA
PLAN_SCHEMA_V2=repair.obj({**copy.deepcopy(PLAN_SCHEMA['properties']),'invocation':INVOCATION_SCHEMA})
CONTEXT_FIELDS = ('proposal_hash','research_cycle','indexed_documents','implementation_document',
    'delivery_artifacts','delivery_directory_protocol','document_protocol','format_contract_version',
    'code_sha256','delivery_hash','spec_hash')


def isolated_request(context):
    """Acquisition uses its complete frozen group, never unrelated role history."""
    task=context.get('implementation_evidence_task')
    if not isinstance(task,dict) or not str(context.get('_purpose','')).startswith('implementation_evidence_review:'):
        return False
    binding=task.get('binding',{})
    expected='implementation_evidence_review:'+repair.sha(binding)
    expected+=_acquisition_suffix(task)
    if (context['_purpose']!=expected
            or binding.get('version')!=VERSION or binding.get('kind')!=task.get('kind')
            or binding.get('kind') not in {'needs_review','needs_execution'}
            or sorted(i['issue_id'] for i in task.get('issues',[]))!=binding.get('issue_ids')
            or any(binding.get(k)!=context.get(k) for k in ('spec_hash','delivery_hash'))):
        repair.fail('定向补证上下文与冻结任务组不一致。')
    return True


def _acquisition_suffix(task):
    request_schema(task)  # Validate version/kind before deriving a replay key.
    if task.get('invocation_contract_version')==2:
        return ':invocation-v2'
    if task.get('source_identity_followup'):
        return ':source-identity-v1'
    if task.get('entry_memory'):
        return ':entry-memory-v1:'+repair.sha(task['entry_memory'])
    return ':read-binding-v1' if task.get('read_binding_recovery') else ''


def _recover_invocation_contract(engine,run_id,task):
    if (task.get('kind')!='needs_execution' or task.get('status')!='blocked'
            or task.get('invocation_contract_version') or not task.get('handoff_id')
            or task.get('executor')=='harness'):
        return
    from .implementation_evidence_recovery import sealed
    frozen,plan=sealed(engine,run_id,'engineer',task['handoff_id'])
    if (frozen.get('schema')!=PLAN_SCHEMA or plan.get('ready') is not False
            or plan.get('report_paths') or not plan.get('entrypoint')
            or frozen['context'].get('implementation_evidence_task',{}).get('binding')!=task['binding']):
        return
    from .implementation_alignment_evidence import _code_text,_own_code_spans
    pages=[]
    for item in frozen['context'].get('delivery_artifacts',[]):
        if item['path'] not in {plan['entrypoint'],'implementation.json'}:continue
        aid=item.get('artifact_id') or item.get('id')
        text=_code_text(engine,run_id,aid,item)
        spans=_own_code_spans(engine,task['handoff_id'],aid,item['sha256'],text)
        cursor=0
        for start,end in spans:
            if start<=cursor:cursor=max(cursor,end)
        if cursor==len(text):pages.append({'path':item['path'],'artifact_id':aid,'sha256':item['sha256'],
            'text':text,'source_handoff_id':task['handoff_id'],'new_source_reading':False})
    history=copy.deepcopy(task.get('acquisition_history',[]))+[copy.deepcopy({k:task[k] for k in
        ('handoff_id','evidence','evidence_sha256','review_record','message') if k in task})]
    recovery={'version':2,'handoff_id':task['handoff_id'],'plan_sha256':repair.sha(plan)}
    for key in ('evidence','evidence_sha256','review_record'):task.pop(key,None)
    _save(engine,task,status='queued',invocation_contract_version=2,invocation_recovery=recovery,
        invocation_memory={'original_plan':copy.deepcopy(plan),'pages':pages},acquisition_history=history,
        message='保留旧计划与实际读取；按既有入口选择未完成检查的参数、输入范围和新宿主输出。')


def _recover_unsent_context(engine, task):
    """One migration for pre-isolation admission failures; paid packets stay put."""
    if (task.get('status')!='blocked' or task.get('input_isolation_recovered')
            or not task.get('message','').startswith('Context compiler cannot extract supported source pages')
            or task.get('evidence') or task.get('review_record') or not task.get('handoff_id')):
        return
    identity=task['handoff_id']
    hand=engine.store.get('handoffs',identity,{})
    progress=engine.store.get('handoff_progress',identity,{})
    frozen=engine.store.get('handoff_inputs',identity,{})
    if (hand.get('run_id')!=task['run_id'] or hand.get('to_role')!=task['owner']
            or hand.get('model_calls') or engine.store.get('decisions',identity)
            or progress or frozen.get('context',{}).get('implementation_evidence_task',{}).get('binding')!=task['binding']):
        return
    _save(engine,task,status='queued',input_isolation_recovered=1,
        admission_failures=[{'message':task['message'],'handoff_id':identity,'updated_at':task['updated_at']}],
        message='已保留派发前失败；使用同一冻结任务组重新准备独立上下文，未重发已付费请求。')


def _recover_saved_source(engine,task):
    # Older validation rejected a valid visual receipt when the same answer
    # also cited source/PDF/citation IDs. Reuse that exact delivered answer.
    if (task.get('status')!='blocked' or task.get('kind')!='needs_review'
            or task.get('source_receipt_recovered') or task.get('evidence')
            or '来源核验必须引用本任务实际完成的原图观察回执' not in task.get('message','')):
        return
    identity=task.get('handoff_id');saved=engine.store.get('decisions',identity,{})
    frozen=engine.store.get('handoff_inputs',identity,{})
    if (saved.get('run_id')!=task['run_id'] or not isinstance(saved.get('result'),dict)
            or frozen.get('context',{}).get('implementation_evidence_task',{}).get('binding')!=task['binding']):
        return
    _save(engine,task,status='queued',source_receipt_recovered=1,
        source_validation_failure=task['message'],message='复用已交付的实际看图回执，继续处理剩余来源缺口；不重新请求模型。')


def _recover_delivery_binding(engine,run_id,task):
    """Repair only the old generic reader's byte/text hash mismatch."""
    if (task.get('kind')!='needs_execution' or task.get('status')!='blocked'
            or task.get('read_binding_recovery') or task.get('review_record')
            or task.get('evidence',{}).get('host_verification')):
        return

    from pathlib import Path
    from .delivery_files import read_page
    from .deletion import _plain_path
    identity=task.get('handoff_id');frozen=engine.store.get('handoff_inputs',identity,{})
    hand=engine.store.get('handoffs',identity,{});decision=engine.store.get('decisions',identity,{})
    ctx=frozen.get('context',{});result=decision.get('result',{})
    if (decision.get('run_id')!=run_id or decision.get('role')!='engineer' or result.get('ready') is not False
            or hand.get('input_hash')!=repair.sha({k:frozen.get(k) for k in ('instruction','context','schema')})
            or hand.get('output_hash')!=repair.sha(result)
            or ctx.get('implementation_evidence_task',{}).get('binding')!=task['binding']):
        return
    bindings={r['artifact_id']:r for r in ctx.get('delivery_artifacts',[])}
    progress=engine.store.get('handoff_progress',identity,{})
    for row in progress.get('tool_results',[]):
        call=row.get('call',{});args=call.get('arguments',{});out=row.get('result',{})
        item=bindings.get(args.get('id'))
        if (call.get('name')!='artifacts.read' or not isinstance(out,dict)
                or out.get('error')!='Artifact content changed: SHA256 does not match expected_sha256; no page returned'
                or not item or args.get('expected_sha256')!=item['sha256']):
            continue
        artifact=engine.store.get('artifacts',item['artifact_id'],{})
        if artifact.get('run_id')!=run_id or not artifact.get('path'):
            return
        path=Path(artifact['path']).absolute()
        if not path.resolve().is_relative_to(engine.run_dir(run_id)):
            return
        _plain_path(path,engine.data_dir)
        page=read_page(path,item,offset=args.get('offset',0),limit=args.get('limit',16000),expected_sha256=item['sha256'])
        correction={'version':1,'handoff_id':identity,'decision_sha256':repair.sha(result),
            'artifact_id':item['artifact_id'],'file_sha256':page['sha256'],'entrypoint':result['entrypoint'],
            'reason':'Frozen file bytes match; prior generic text reader normalized Windows newlines. Use the byte-bound delivery reader.'}
        history=[*task.get('acquisition_history',[]),{k:copy.deepcopy(task[k]) for k in
            ('handoff_id','evidence','evidence_sha256','message') if k in task}]
        task.pop('evidence',None);task.pop('evidence_sha256',None)
        _save(engine,task,status='queued',read_binding_recovery=correction,acquisition_history=history,
            message='文件字节与冻结版本一致；仅补读交付清单并选择报告路径，沿用已完成入口阅读。')
        return


def _recover_entry_memory(engine,run_id,task):
    if (task.get('kind')!='needs_execution' or task.get('status')!='blocked'
            or not task.get('read_binding_recovery') or task.get('entry_memory') or task.get('review_record')):
        return
    from .implementation_evidence_recovery import sealed, entry_memory
    frozen,result=sealed(engine,run_id,'engineer',task['handoff_id'])
    if result.get('ready') is not False:
        return
    prior=task['read_binding_recovery']
    progress=engine.store.get('handoff_progress',task['handoff_id'],{})
    if not any(row.get('call',{}).get('name')=='context.expand'
            and row['call'].get('arguments',{}).get('receipt_id')==prior['handoff_id']
            and row['call']['arguments'].get('expected_sha256')==prior['decision_sha256']
            and row.get('result',{}).get('error')=='Context receipt version or ownership mismatch'
            for row in progress.get('tool_results',[])):
        return
    memory=entry_memory(engine,run_id,task)
    history=[*task.get('acquisition_history',[]),{k:copy.deepcopy(task[k]) for k in
        ('handoff_id','evidence','evidence_sha256','message') if k in task}]
    task.pop('evidence',None);task.pop('evidence_sha256',None)
    _save(engine,task,status='queued',entry_memory=memory,acquisition_history=history,
        message='已恢复同角色同版本的入口与清单正文；仅确认报告输出映射，不重复读取或重置验收额度。')


def _recover_resource_admission(engine,run_id,task):
    """Retry only pre-execution admission, with the exact saved ready plan."""
    if (task.get('kind')!='needs_execution' or task.get('owner')!='engineer'
            or task.get('status')!='blocked' or task.get('evidence') or task.get('review_record')
            or not task.get('message','').startswith('Host verification resources unavailable: ')):
        return
    from .implementation_evidence_recovery import sealed
    from . import code_verification
    try:
        frozen,result=sealed(engine,run_id,'engineer',task['handoff_id'])
        admission=code_verification.policy(engine,run_id,workflow_id=task['binding']['workflow_id'])
    except ValueError:
        return
    if (result.get('ready') is not True or result!=task.get('plan')
            or frozen.get('schema')!=PLAN_SCHEMA
            or frozen.get('context',{}).get('implementation_evidence_task',{}).get('binding')!=task['binding']
            or not admission or admission.get('routed_task_id')!=task['id']
            or admission.get('routed_handoff_id')!=task['handoff_id']
            or any(r.get('identity',{}).get('policy_sha256')==admission['sha256']
                for r in engine.store.list(code_verification.RECEIPTS,run_id))):
        return
    recovery={'handoff_id':task['handoff_id'],'plan_sha256':repair.sha(result),
        'policy_sha256':admission['sha256']}
    history=[*task.get('admission_failures',[]),{**recovery,'phase':'resource_admission',
        'message':task['message'],'updated_at':task.get('updated_at')}]
    _save(engine,task,status='queued',resource_admission_recovery=recovery,admission_failures=history,
        message='复用工程师已交付的就绪计划，重新检查 CPU 资源准入；未重发模型请求或重置验收额度。')


def _recover_source_identity(engine,run_id,task,original):
    if (task.get('kind')!='needs_review' or task.get('status')!='blocked'
            or task.get('source_identity_followup') or not task.get('review_record')
            or not task.get('evidence',{}).get('visual_records')
            or not any(word in task.get('message','') for word in ('身份','版本'))):
        return
    history=[*task.get('evidence_history',[]),{k:copy.deepcopy(task[k]) for k in
        ('handoff_id','evidence','evidence_sha256','review_record','message') if k in task}]
    followup={'version':1,'handoff_id':task['handoff_id'],'evidence_sha256':task['evidence_sha256'],
        'evidence':copy.deepcopy(task['evidence'])}
    task.pop('review_record',None);task.pop('evidence');task.pop('evidence_sha256')
    _save(engine,task,status='queued',source_identity_followup=followup,evidence_history=history,
        message='由文献研究员检索归档来源与已有身份/版本记录；保留原看图结果，仅补查来源关联。')


def groups(source):
    grouped = {}
    for item in source['issues']:
        kind = (item.get('binding') or {}).get('disposition')
        if kind not in {'needs_review','needs_execution'}:
            continue
        requirement=item['binding']['requirement_id']
        key = (requirement, kind)
        grouped.setdefault(key, []).append({**copy.deepcopy(item),'requirement_id':requirement})
    return [(key, grouped[key]) for key in sorted(grouped,key=lambda k:(k[0],k[1]!='needs_review'))]


def _task(engine, run_id, state, attempt, original, source, key, issues):
    requirement, kind = key
    binding = {'version':VERSION,'workflow_id':state['id'],'attempt':attempt['index'],
        'spec_hash':source['spec_hash'],'delivery_hash':source['delivery_hash'],
        'original_source_sha256':repair.sha(original),'source_sha256':repair.sha(source),
        'requirement_id':requirement,'kind':kind,'issue_ids':sorted(i['issue_id'] for i in issues)}
    from . import host_evidence_work as host
    historical=host.referenced_receipts(engine,run_id,source,issues) if kind=='needs_review' else []
    host_work=kind=='needs_review' and (host.requested(source,issues) or bool(historical))
    if host_work:binding['evidence_domain']=host.DOMAIN
    if historical:
        binding['host_receipt_bindings']=[{'id':r['id'],'sha256':r['sha256']} for r in historical]
    identity = run_id + ':evidence-task:' + repair.sha(binding)
    legacy=None
    if host_work:
        prior_binding={k:v for k,v in binding.items() if k not in {'evidence_domain','host_receipt_bindings'}}
        legacy=engine.store.get(COLLECTION,run_id+':evidence-task:'+repair.sha(prior_binding))
        if legacy and (legacy.get('binding')!=prior_binding or legacy.get('issues')!=issues):
            repair.fail('原证据任务的问题或冻结版本发生变化。')
        if legacy and legacy.get('review_record'):
            return legacy  # An already judged group is replayed, never reopened by a new route.
    saved = engine.store.get(COLLECTION, identity)
    if saved:
        if saved.get('binding') != binding or saved.get('issues') != issues:
            repair.fail('证据任务的原问题或冻结版本发生变化。')
        return saved
    value = {'id':identity,'run_id':run_id,'workflow_id':state['id'],'binding':binding,'issues':issues,
        'kind':kind,'requirement_id':requirement,'owner':'scout' if kind=='needs_review' and not host_work else 'engineer',
        'label':requirement + (' 宿主证据核验' if host_work else ' 来源核验' if kind=='needs_review' else ' 宿主验收'),
        'status':'queued','message':'等待前一任务组结束；本组内部独立评审并行。',
        'created_at':now(),'updated_at':now()}
    if historical:value['host_execution_receipts']=historical
    if kind=='needs_execution':value['invocation_contract_version']=2
    if legacy:
        value['prior_route_work_item_id']=legacy['id']
        if legacy.get('route_replacement')!=identity:
            _save(engine,legacy,status='superseded',route_replacement=identity,
                route_recovery=copy.deepcopy({k:v for k,v in legacy.items()
                    if k not in {'route_recovery','route_replacement'}}),
                message='保留原请求、读取和交付；本组由宿主证据核验任务接续。')
    engine.store.put(COLLECTION, identity, value)
    return value


def _save(engine, task, **changes):
    task.update(changes, updated_at=now())
    engine.store.put(COLLECTION, task['id'], task)


def task_context(original, task):
    base = original['source_contexts'][task['owner']]
    context = {k:copy.deepcopy(base[k]) for k in CONTEXT_FIELDS if k in base}
    context.update(source_tool_protocol=1, document_index_protocol=1,
        document_read_window_protocol=3, implementation_evidence_task={
            'id':task['id'],'binding':copy.deepcopy(task['binding']),'kind':task['kind'],
            'title':task['label'],'issues':copy.deepcopy(task['issues']), 'serial_group':True,
            'approved_checks':copy.deepcopy(original['acceptance_checks']),
            'training_authorized':False,'allowance_rule':'引用原批准条款的 CPU 总截止；不重置已有额度。'})
    from . import host_evidence_work as host
    host_work=task['binding'].get('evidence_domain')==host.DOMAIN
    if task.get('invocation_contract_version')==2:
        context['implementation_evidence_task']['invocation_contract_version']=2
        spec_id=base.get('implementation_document',{}).get('document_id')
        context['indexed_documents']=[d for d in context.get('indexed_documents',[]) if d['document_id']==spec_id]
        for key in ('invocation_recovery','invocation_memory'):
            if task.get(key):context['implementation_evidence_task'][key]=copy.deepcopy(task[key])
        if base.get('host_verification'):
            context['host_verification']=copy.deepcopy(base['host_verification'])
            context['retained_host_check_facts']=host.facts(context)
    if host_work:
        from .host_execution_artifacts import catalog,FIELD as HOST_ARTIFACTS
        reference=host.reference_context(original,task['issues']) or base
        context.update(host_verification=copy.deepcopy(reference.get('host_verification',{})),
            implementation_alignment_protocol=1)
        if task.get('host_execution_receipts'):
            context['host_execution_receipts']=copy.deepcopy(task['host_execution_receipts'])
        context[HOST_ARTIFACTS]=catalog(context)
        context['host_check_input_scope']=host.input_scope(context)
        context['host_receipt_facts']=host.facts(context)
        context['implementation_evidence_task']['evidence_domain']=host.DOMAIN
        spec_id=base.get('implementation_document',{}).get('document_id')
        context['indexed_documents']=[d for d in context.get('indexed_documents',[]) if d['document_id']==spec_id]
    if task['kind']=='needs_review' and not host_work:
        context.update(pdf_tools_protocol=1,codex_literature_protocol=1)
    context['implementation_evidence_task']['paper_sources']=[
        {'source_id':'rag-source:'+d['document_id'].removeprefix('paper-rag-source:'),
         'expected_sha256':d['sha256'],'title':d.get('title')}
        for d in context.get('indexed_documents',[]) if not host_work and d['document_id'].startswith('paper-rag-source:')]
    if task.get('read_binding_recovery'):
        context['implementation_evidence_task']['read_binding_recovery']=copy.deepcopy(task['read_binding_recovery'])
    if task.get('entry_memory'):
        context['implementation_evidence_task']['entry_memory']=copy.deepcopy(task['entry_memory'])
    if task.get('source_identity_followup'):
        context['implementation_evidence_task']['source_identity_followup']=copy.deepcopy(task['source_identity_followup'])
    return context


def _instruction(task):
    common = ('只处理本组原问题，沿用已保存的文字、代码读取和结论。不修改代码、批准文档或执行科研实验。'
        '不得增加验收门槛。不同任务组串行；本组仅收集真实依据，不代表代码或科学批准。')
    if task['kind']=='needs_review':
        from .host_evidence_work import DOMAIN,INSTRUCTION
        if task['binding'].get('evidence_domain')==DOMAIN:
            return common+INSTRUCTION
        if task.get('source_identity_followup'):
            return common + ('本组原表视觉核验已完成；沿用 source_identity_followup 的实际回执，不重复看图。'
                '由你自己检索并核对剩余来源身份关联：读取实际配置 paper_reference，使用 sources.identity 获取同一归档的已有身份和版本记录，'
                '必要时 pdf.read 第一页核对原 PDF 的确切版本；不得仅凭 generic verified=false 推断不存在身份关联，也不得据 metadata_link 声称科学批准。'
                '明确连接配置引用、原 PDF 哈希、提取文本、论文身份和确切来源版本，保留预印本与正式版等同性未知及比较条件。'
                '返回实际 sources.identity 的回执 ID 和你的核对结论；不能闭合则说明具体证据缺口，不修改代码或原批准文档。')
        return common + ('补齐指定来源证据。图表问题用 pdf.manifest/search 定位同一归档 PDF，'
            '再用 pdf.analyze_figure 实际查看原图，返回其真实 paper_visual 记录 id；'
            '不要只重复文字摘录或声明没有看图。不能取得原 PDF 或工具失败时如实 unavailable，'
            '说明具体依赖。仅为必要原表、数值、列对应和来源身份补证，不重读整批代码。')
    reading = ('entry_memory.pages 已恢复本角色实际读过的入口和清单，沿用这些正文及原批准检查要求，仅缺口时按精确版本补查。'
        if task.get('entry_memory') else '先实际读取该 tests/ 下 Python 入口及原批准检查要求。')
    instruction=common + ('为宿主选择实际交付中既有的 CPU 固定验收入口。'+reading+
        '返回入口相对路径、覆盖的 requirement_ids 和本次实际生成的 report_paths。'
        '不把历史修复回执当成本次执行结果。'
        'timeout_seconds 必须来自原批准 CPU 检查的总截止，allowance_reference 引用对应验收 ID 和精确原句；不得套用其他工作流的旧限额。'
        '宿主负责隔离执行、资源和额度门禁。'
        '只进行原批准的一次 CPU 检查，不使用CUDA、真实Weather测试标签、联网、科研训练或新探针；'
        '不编写新脚本，不把未运行写成通过。入口不能满足原要求时 ready=false 并指出位置与缺口。')
    if task.get('invocation_contract_version')==2:
        from .code_check_invocation import INSTRUCTION
        instruction+=INSTRUCTION
        if task.get('invocation_memory'):
            instruction+=' invocation_memory 是本角色原计划和已验证的同版本实际读取，直接复用；只补选缺失阶段及输出。'
    else:instruction+='报告路径只选择 implementation.json 中本入口本次输出的摘要。'
    return instruction


def request_schema(task):
    """Explicit supported branches; an unknown control contract is not v1.

    The absent version is the original frozen no-argv plan. Version 2 adds
    argv/input-scope without changing allowance, output or provenance gates.
    """
    kind = task.get('kind')
    version = task.get('invocation_contract_version')
    if (kind not in {'needs_review', 'needs_execution'} or
            'invocation_contract_version' in task and (type(version) is not int or version not in (1, 2))):
        error = ValueError('Unsupported evidence acquisition kind or invocation contract version')
        error.protocol_diagnostic = {'code': 'unsupported_evidence_acquisition_contract',
            'kind': kind, 'invocation_contract_version': version,
            'owner': 'harness', 'required_action': 'capability_contract_repair',
            'dispatch_authorized': False}
        raise error
    return SOURCE_SCHEMA if kind == 'needs_review' else PLAN_SCHEMA_V2 if version == 2 else PLAN_SCHEMA


def _assignment(engine, h, run_id, task, original):
    schema = request_schema(task)
    purpose = 'implementation_evidence_review:' + repair.sha(task['binding'])+_acquisition_suffix(task)
    context, instruction = task_context(original, task), _instruction(task)
    if task.get('read_binding_recovery') and not task.get('entry_memory'):
        instruction+='宿主已核对 read_binding_recovery：原文件字节未变化，旧错误来自换行转换。沿用同角色已读入口和原截止，仅实际补读指定交付清单，补齐本入口输出路径；不要重读整批代码。'
    if task.get('entry_memory'):
        instruction+=' entry_memory.pages 已提供本角色同版本实际读过的入口和清单正文，可直接确认 report_paths。handoff_id 是归属索引，不是 context.expand 可展开的回执。这里只补选既有输出路径，执行仍由宿主按原授权负责。'
    hand = h._handoff(run_id,task['owner'],purpose,instruction,context,schema)
    _save(engine,task,handoff_id=hand['id'],purpose=purpose)
    return purpose, context, instruction, schema


def _visual_evidence(engine, run_id, task, result):
    from .paper_media import validate_visual
    from .context_compiler import fingerprint
    progress = engine.store.get('handoff_progress',task['handoff_id'],{})
    actual = {r.get('result',{}).get('id') for r in progress.get('tool_results',[])
              if r.get('call',{}).get('name')=='pdf.analyze_figure' and isinstance(r.get('result'),dict)}
    records = []
    for identity in result['evidence_ids']:
        value = engine.store.get('paper_visual',identity)
        if identity not in actual and not value and ':visual:' not in identity:
            continue  # Auxiliary source/PDF/citation references are not visual proof.
        if (identity not in actual or not value or value.get('run_id')!=run_id
                or value.get('status')!='completed' or value.get('visual_content_inspected') is not True
                or value.get('prepared',{}).get('role')!=task['owner']
                or value.get('response_sha256')!=fingerprint(value.get('response'))):
            repair.fail('来源核验必须引用本任务实际完成的原图观察回执。')
        validate_visual(value['response'])
        from . import image_inputs
        from .pdf_tools import LocalPDF
        image_inputs.resolve(engine.store,run_id,{'_image_inputs':[value['rendered']['image']]})
        doc,original_pdf=LocalPDF(engine).original(run_id,value['rendered']['source_id'])
        if (doc['text_sha256']!=value['rendered']['text_sha256']
                or original_pdf['sha256']!=value['rendered']['original_sha256']):
            repair.fail('原图观察的 PDF 原文件或来源版本发生变化。')
        frozen=engine.store.get('handoff_inputs',task['handoff_id'],{})
        allowed=frozen.get('context',{}).get('implementation_evidence_task',{}).get('paper_sources',[])
        from .paper_bindings import canonical_source
        rendered=value['rendered']
        allowed_keys={(canonical_source(engine.store,p['source_id'].removeprefix('rag-source:')),p['expected_sha256'])
                      for p in allowed}
        if (canonical_source(engine.store,rendered['source_id'].removeprefix('rag-source:')),
                rendered['text_sha256']) not in allowed_keys:
            repair.fail('原图观察没有绑定本组批准来源的精确版本。')
        records.append({'id':identity,'response_sha256':value['response_sha256'],
            'rendered':copy.deepcopy(value['rendered']),'response':copy.deepcopy(value['response'])})
    if result['status']=='completed' and not records:
        repair.fail('来源核验没有实际证据，不能登记完成。')
    return records


def _identity_evidence(engine,run_id,task,result,*,handoff_id=None):
    """Authenticate the Agent's actual lookup, without deciding its conclusion."""
    from .implementation_evidence_recovery import sealed
    from .paper_bindings import canonical_source, valid_binding
    owner=handoff_id or task['handoff_id']
    frozen,_=sealed(engine,run_id,task['owner'],owner)
    ctx=frozen.get('context',{});group=ctx.get('implementation_evidence_task',{})
    if group.get('binding')!=task['binding']:
        repair.fail('来源身份检索回执不属于当前任务组。')
    progress=engine.store.get('handoff_progress',owner,{})
    actual={row.get('result',{}).get('id'):row for row in progress.get('tool_results',[])
        if row.get('call',{}).get('name')=='sources.identity' and isinstance(row.get('result'),dict)}
    allowed={(canonical_source(engine.store,p['source_id'].removeprefix('rag-source:')),p['expected_sha256'])
        for p in group.get('paper_sources',[])}
    records=[]
    for identity in result['evidence_ids']:
        if identity not in actual:
            continue
        record=engine.store.get('source_identity_receipts',identity,{})
        body=record.get('body',{});metadata=body.get('metadata',{});row=actual[identity]
        if (body.get('run_id')!=run_id or body.get('role')!=task['owner'] or body.get('handoff_id')!=owner
                or record.get('sha256')!=repair.sha(body) or body.get('arguments')!=row['call'].get('arguments')
                or row['result']!={**metadata,'id':identity,'receipt_sha256':record['sha256']}):
            repair.fail('来源身份检索回执的实际结果或归属发生变化。')
        source=metadata.get('source_alias_id',metadata.get('source_id'))
        binding=valid_binding(engine.store,run_id,source)
        document=engine.sources.document(run_id,metadata.get('source_id'))
        if (not binding or (canonical_source(engine.store,binding['rag_source_id']),document['text_sha256']) not in allowed
                or document['text_sha256']!=metadata.get('text_sha256')
                or document.get('content_sha256')!=metadata.get('content_sha256')):
            repair.fail('来源身份检索没有绑定本组原文与实际 PDF 字节。')
        records.append(copy.deepcopy(record))
    return records


def reference(task, original=None):
    value={'id':task['id'],'sha256':task['evidence_sha256'],'evidence':copy.deepcopy(task['evidence'])}
    if original is not None:
        owners={i['issue_id']:i['role'] for i in original['issues']}
        targets=[]
        for issue in task['issues']:
            identity=issue['issue_id']
            if owners.get(identity)!=issue['role']:
                identity=(issue.get('binding') or {}).get('origin_id')
            if owners.get(identity)!=issue['role']:
                repair.fail('补查子问题没有绑定该角色的原问题。')
            targets.append(identity)
        value.update(review_target_version=1,active_review_issue_ids=sorted(set(targets)))
    if task.get('host_read_scope_recovery'):
        value['host_read_scope_recovery']=copy.deepcopy(task['host_read_scope_recovery'])
    if task.get('review_scope_update'):
        value['review_scope_update']=copy.deepcopy(task['review_scope_update'])
    return value


def _recover_peer_host_reads(engine,run_id,state,attempt,task):
    if (task.get('status')!='blocked' or not task.get('review_record')
            or task.get('host_read_scope_recovery') or not task.get('evidence',{}).get('host_receipt_facts')):
        return
    from . import host_execution_artifacts as host,code_repair_clarification as clarification
    restored=[];roles=[]
    for vote in task['review_record'].get('votes',[]):
        hid=vote['handoff_id'];frozen=engine.store.get('handoff_inputs',hid,{})
        context=frozen.get('context',{})
        for row in engine.store.get('handoff_progress',hid,{}).get('tool_results',[]):
            call=row.get('call',{});result=row.get('result')
            if call.get('name')!='artifacts.read' or not isinstance(result,dict) or not result.get('error'):
                continue
            if not host.resolve(context,call.get('arguments',{})):continue
            try:page=host.read(engine,run_id,vote['role'],hid,call['arguments'])
            except ValueError:continue
            restored.append({'handoff_id':hid,'artifact_id':page['artifact_id'],'sha256':page['sha256']})
            roles.append(vote['role'])
    if not restored:return
    clarification.verify_saved(engine,run_id,state,attempt,task['review_record'])
    history=copy.deepcopy(task.get('review_history',[]))+[copy.deepcopy(task['review_record'])]
    recovery={'version':1,'roles':sorted(set(roles)),'available_reads':restored,
        'previous_purpose':task['review_record']['purpose']}
    task.pop('review_record')
    _save(engine,task,status='evidence_ready',review_history=history,host_read_scope_recovery=recovery,
        message='保留原答复和读取错误；已恢复转交回执的读取边界，仅补问实际受影响的原角色。')


def _recover_review_lineage(engine,task,original):
    prior=task.get('review_record',{}).get('evidence_work_item')
    if not prior or prior.get('review_target_version') is not None:
        return
    expected=reference(task,original)
    # A legacy child that did not match an original ID was silently treated as
    # unchanged. Only that missed lineage needs a supplement; original votes stay.
    old=set(prior['evidence']['active_issue_ids'])
    if not set(expected['active_review_issue_ids'])-old:
        return
    history=copy.deepcopy(task.get('review_history',[]))+[copy.deepcopy(task['review_record'])]
    task.pop('review_record')
    _save(engine,task,status='evidence_ready',review_history=history,
        lineage_review_recovery={'version':1,'previous_reference':copy.deepcopy(prior)},
        message='复用已交付证据与角色答复；仅补查遗漏的子问题到原问题映射。')


def validate_reference(engine, run_id, value, original):
    task = engine.store.get(COLLECTION,value['id'])
    if task and task.get('evidence_sha256')!=value['sha256']:
        historical=next((row for row in task.get('evidence_history',[])
            if row.get('evidence_sha256')==value['sha256'] and row.get('evidence')==value['evidence']),None)
        if historical:
            task={**task,'evidence':historical['evidence'],'evidence_sha256':historical['evidence_sha256'],
                'handoff_id':historical.get('handoff_id',task['handoff_id']),
                'review_scope_update':historical.get('review_scope_update')}
    if (not task or task.get('run_id')!=run_id or task.get('evidence_sha256')!=value['sha256']
            or task.get('evidence')!=value['evidence'] or repair.sha(value['evidence'])!=value['sha256']
            or task['binding']['original_source_sha256']!=repair.sha(original)
            or task['binding']['spec_hash']!=original['spec_hash']
            or task['binding']['delivery_hash']!=original['delivery_hash']):
        repair.fail('分组补证回执没有绑定同一原问题、批准文档与实际交付。')
    if value.get('review_target_version') is not None:
        expected=reference(task,original)
        if (value.get('review_target_version')!=expected['review_target_version']
                or value.get('active_review_issue_ids')!=expected['active_review_issue_ids']
                or value.get('review_scope_update')!=expected.get('review_scope_update')):
            repair.fail('补查原问题映射超出实际任务组或角色。')
    if 'visual_records' in task['evidence']:
        result = {'status':'completed','evidence_ids':[r['id'] for r in task['evidence']['visual_records']]}
        visual_task={**task,'handoff_id':task['evidence'].get('visual_handoff_id',task['handoff_id'])}
        if _visual_evidence(engine,run_id,visual_task,result)!=task['evidence']['visual_records']:
            repair.fail('原图观察回执发生变化。')
    if 'host_execution_reads' in task['evidence']:
        from . import host_evidence_work as host
        rows=task['evidence']['host_execution_reads']
        actual=host.records(engine,run_id,task,{'status':'completed',
            'evidence_ids':list(dict.fromkeys(r['result']['artifact_id'] for r in rows))})
        frozen=engine.store.get('handoff_inputs',task['handoff_id'])
        if actual!=rows or host.input_scope(frozen['context'])!=task['evidence']['host_check_input_scope']:
            repair.fail('宿主证据或隔离输入范围在补查后发生变化。')
        if 'host_receipt_facts' in task['evidence'] and host.facts(frozen['context'])!=task['evidence']['host_receipt_facts']:
            repair.fail('宿主回执记载字段在补查后发生变化。')
    if task['evidence'].get('source_identity_receipts'):
        records=task['evidence']['source_identity_receipts']
        actual=_identity_evidence(engine,run_id,task,{'evidence_ids':[r['id'] for r in records]},
            handoff_id=task['evidence'].get('identity_handoff_id'))
        if actual!=records:
            repair.fail('文献研究员检索的来源身份回执发生变化。')
    return task


def public_queue(store, run):
    if run.get('stage')!='implementation':
        return []
    from .run_views import _rows,_field
    with store._read_db() as db:
        row=db.execute("SELECT key,json_extract(data,'$.attempts[#-1].index') FROM records WHERE collection='codex_workflows' AND json_extract(data,'$.run_id')=? "
            "AND json_extract(data,'$.cycle')=? ORDER BY updated_at DESC,key DESC LIMIT 1",
            (run['id'],run.get('state',{}).get('research_cycle',1))).fetchone()
    if not row:
        return []
    fields={k:_field(k) for k in ('id','kind','requirement_id','owner','label','status','handoff_id','updated_at')}
    fields['message']="substr(json_extract(data,'$.message'),1,1800)"
    values=_rows(store,COLLECTION,fields,run_id=run['id'],where="json_extract(data,'$.workflow_id')=? AND json_extract(data,'$.binding.attempt')=?",
        args=(row[0],row[1]),order_by="json_extract(data,'$.created_at'),key",limit=32)
    return redact(values)
