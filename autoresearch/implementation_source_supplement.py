"""Explicit, immutable supplementary-definition review before author recovery.

This is a separate review record. It cannot approve the implementation, change
the frozen scientific contract, manufacture author reads, or dispatch code.
"""
from __future__ import annotations

import copy

from .engine import ResearchBlocked, digest
from .storage import now
from . import implementation_acceptance as policy
from . import implementation_acceptance_flow as flow
from . import implementation_acceptance_author as author
from . import document_index

COLLECTION = 'implementation_source_supplements'
POINTER = ':active-source-supplement'
UNITS = ('H01','H02','H03','H04','D01','D02','G01','G02','G03','G04','G05')
ROLES = ('scout','engineer','director')
SCHEMA = policy.obj({'supplement_sha256':policy.SHA,
    'checks':policy.array(policy.obj({'id':{'enum':list(UNITS)},
        'status':{'enum':['accept','revise','unknown']},'reason':policy.TEXT,
        'required_change':{'type':'string','maxLength':6000},'evidence':policy.REFS}),len(UNITS),len(UNITS)),
    'summary':policy.TEXT})


def _source(engine, run_id, hid):
    frozen=engine.store.get('handoff_inputs',hid,{})
    ctx=frozen.get('context',{})
    assignment=engine.store.get('handoffs',hid,{})
    decision=engine.store.get('decisions',hid,{})
    if (assignment.get('run_id')!=run_id or assignment.get('to_role')!='engineer'
            or assignment.get('status')!='completed' or not decision.get('result')
            or ctx.get(author.PROTOCOL)!=1):
        raise ResearchBlocked('补充审核只能绑定已完成且保留付费答复的验收作者交接。')
    from . import implementation_authoring as drafting
    from .engine import Harness
    from .intelligence import parse_json
    import json
    _,bound_context,bound_state,_=drafting._assignment(engine,run_id,'engineer',hid)
    payload={k:frozen.get(k) for k in ('instruction','context','schema')}
    if (bound_context!=ctx or assignment.get('input_hash')!=digest(payload)
            or Harness.decision_key(run_id,'engineer',assignment.get('purpose'),**payload)!=hid
            or decision.get('id')!=hid or decision.get('run_id')!=run_id or decision.get('role')!='engineer'
            or decision.get('purpose')!=assignment.get('purpose')
            or assignment.get('output_hash')!=digest(decision['result'])):
        raise ResearchBlocked('补充审核原作者付费答复或冻结交接身份不一致。')
    parse_json(json.dumps(decision['result'],ensure_ascii=False),frozen['schema'])
    state=engine.store.get('codex_workflows',ctx['implementation_document_authoring']['workflow_id'])
    # Authenticate inherited source even when this continuation saved no new draft.
    base=author.continuation_base(engine,run_id,state,ctx)
    if not base:
        raise ResearchBlocked('补充审核缺少认证继承稿。')
    if (author._workflow_scope(bound_state)!=author._workflow_scope(state)
            or {k:decision['result'].get(k) for k in drafting.SUBMISSION['properties']}!=drafting.pointer({'row':base})):
        raise ResearchBlocked('补充审核的原作者版本与继承稿不一致。')
    guards={col:digest(value) for col,value in author._source_records(engine.store,hid).items()}
    return ctx,state,base,guards


def prepare(engine,run_id,*,source_handoff_id,document_refs,supplement_id):
    """Called by the operator route under its lifecycle lock; no model dispatch."""
    if (not isinstance(source_handoff_id,str) or not source_handoff_id
            or not isinstance(supplement_id,str) or not supplement_id
            or not isinstance(document_refs,list) or not 2 <= len(document_refs) <= 16
            or any(not isinstance(ref,dict) or set(ref)!={'document_id','sha256'}
                   or not all(isinstance(v,str) and v for v in ref.values()) for ref in document_refs)):
        raise ValueError('Expected explicit source identity and 2–16 document_id/sha256 references')
    run=engine.store.get('runs',run_id)
    if run['status'] not in ('blocked','paused') or run.get('mode')!='live':
        raise ResearchBlocked('补充审核只能由已停止的真实任务显式准备。')
    ctx,state,base,guards=_source(engine,run_id,source_handoff_id)
    refs={}
    for ref in document_refs:
        doc=document_index.directory(engine,run_id,ref['document_id'],ref['sha256'])
        if doc['document_id'] in refs:
            raise ResearchBlocked('补充来源目录重复。')
        refs[doc['document_id']]=doc
    if supplement_id not in refs or refs[supplement_id]['kind']!='scientific_supplement_candidate':
        raise ResearchBlocked('缺少明确标注为候选的补充文档。')
    refs[base['document']['document_id']]=base['document']
    if len(refs)<3:
        raise ResearchBlocked('补充审核需要原依据，不能只有补充稿和实现稿。')
    context=flow.base_context(state,list(refs.values()))
    context.update(source_tool_protocol=1,document_read_window_protocol=2,
        implementation_source_supplement_protocol=1,
        source_supplement={'document':refs[supplement_id], 'base_document':base['document'],
            'units':list(UNITS),'original_source_handoff':source_handoff_id,
            'review_only':True,'scientific_approval':False,'execution_authorized':False},
        objective=run['objective'])
    payload={'run_id':run_id,'source_handoff_id':source_handoff_id,'source_guards':guards,
        'workflow_id':state['id'],'workflow_sha256':digest(author._workflow_scope(state)),
        'context':context,'schema':SCHEMA,'roles':list(ROLES)}
    key=run_id+':source-supplement:'+digest(payload)
    old=engine.store.get(COLLECTION,key)
    pointer={'id':run_id+POINTER,'run_id':run_id,'record_id':key}
    current=engine.store.get('stage_contexts',run_id+POINTER)
    if current and current!=pointer:
        prior=engine.store.get(COLLECTION,current['record_id'],{})
        if prior.get('status') in ('prepared','reviewing'):
            raise ResearchBlocked('已有未结束的补充审核，不能重复准备另一份。')
    if not old:
        old={'id':key,'run_id':run_id,'payload':payload,'sha256':digest(payload),
            'status':'prepared','votes':[],'created_at':now(),'adopted':False,
            'scientific_approval':False,'execution_authorized':False}
    elif old['payload']!=payload or old['sha256']!=digest(payload):
        raise ResearchBlocked('已有补充审核身份不一致。')
    engine.store.batch([(COLLECTION,key,old),('stage_contexts',pointer['id'],pointer)])
    return old


def checked(engine,run_id,record,state):
    p=record['payload']
    if (record.get('run_id')!=run_id or record.get('sha256')!=digest(p)
            or p['run_id']!=run_id or p['schema']!=SCHEMA or p['roles']!=list(ROLES)
            or state['id']!=p['workflow_id'] or digest(author._workflow_scope(state))!=p['workflow_sha256']):
        raise ResearchBlocked('补充审核的冻结合同或范围已改变。')
    guards={col:digest(value) for col,value in author._source_records(engine.store,p['source_handoff_id']).items()}
    if guards!=p['source_guards']:
        raise ResearchBlocked('补充审核原作者记录已改变；未派发。')
    _,bound_state,base,_=_source(engine,run_id,p['source_handoff_id'])
    if (author._workflow_scope(bound_state)!=author._workflow_scope(state)
            or base['document']!=p['context']['source_supplement']['base_document']):
        raise ResearchBlocked('补充审核继承来源已改变。')
    for d in p['context']['indexed_documents']:
        document_index.load(engine,run_id,d['document_id'],d['sha256'])
    return p


def validate_vote(engine,run_id,packet,supplement,expected_context,expected_role=None):
    if 'unit_review_protocol' in packet:
        from .implementation_supplement_units import validate_vote as validate_units
        return validate_units(engine,run_id,packet,expected_context,expected_role)
    context=flow.check_packet(engine,run_id,packet)
    if context!=expected_context or packet['role'] not in ROLES or (expected_role and packet['role']!=expected_role):
        raise ResearchBlocked('补充审核回执未绑定本轮冻结来源和指定角色。')
    result=packet['result']
    if (result['supplement_sha256']!=supplement['sha256']
            or len(result['checks'])!=len(UNITS)
            or {item['id'] for item in result['checks']}!=set(UNITS)):
        raise ResearchBlocked('补充审核必须逐项覆盖同一候选版本，不能漏项或重复。')
    for item in result['checks']:
        validate_item(engine,run_id,packet,supplement,context,item)


def validate_item(engine,run_id,packet,supplement,context,item):
    if item['status']!='accept' and not item['required_change'].strip():
        raise ResearchBlocked('未通过的补充条目必须给出具体修订或取证要求。')
    flow.validate_refs(engine,run_id,packet['handoff_id'],context,item['evidence'],
        current=supplement,field='supplement '+item['id'])
    if item['status']=='accept' and not any(r['document_id']!=supplement['document_id'] for r in item['evidence']):
        raise ResearchBlocked('接受补充必须同时引用原依据或宿主实现，不能只引用候选自身。')


async def review_if_pending(engine,h,run_id,state,hid):
    pointer=engine.store.get('stage_contexts',run_id+POINTER,{})
    record=engine.store.get(COLLECTION,pointer.get('record_id',''))
    if not record or record['payload']['source_handoff_id']!=hid:
        return
    if record['payload'].get('repair'):
        from .implementation_supplement_repair import continue_repair
        return await continue_repair(engine, h, run_id, state, record)
    p=checked(engine,run_id,record,state)
    supplement=p['context']['source_supplement']['document']
    # All retained votes are reauthenticated. A saved paid response is never
    # replaced or repeated after a restart or request timeout.
    roles=set()
    for packet in record['votes']:
        if packet['role'] not in ROLES or packet['role'] in roles:
            raise ResearchBlocked('补充审核角色重复或越界。')
        roles.add(packet['role']);validate_vote(engine,run_id,packet,supplement,p['context'])
    for role in ROLES:
        if role in roles:
            continue
        engine.check_active(run_id)
        record={**record,'status':'reviewing','active_role':role}
        engine.store.put(COLLECTION,record['id'],record)
        instruction=('审核本任务补充候选及其已登记依赖，逐项核对当前审查单元列出的问题。'
            '仅审该补充和涉及的旧条款，不重审无关已完成条件、不增加科研标准。'
            '用documents.read读取候选对应段和原始依据，返回精确证据范围。'
            '区分论文公式恢复、项目新增唯一实施候选、软件接口修复和未执行资源探针。'
            '原论文和代码不一致时说明冲突，不宣称任一自动批准；新实施细节可认可为待作者采用的项目定义。'
            '不要求现在跑实验证明效果；资源事项审查核验计划，同步事项审查定向变更和后续验收职责。'
            '拒绝时一次给出本条全部具体替换定义或缺少的精确来源，不能仅要求再读。'
            '本轮不能修改文档、批准实现稿、关闭原问题、放宽预算或授权代码执行。'
            '每条接受必须有候选及另一份原依据/宿主实现的真实已交付证据。'
            '读取已交付的相同范围不得反复请求；材料足够即给结论。职责：'+role)
        from .implementation_supplement_units import review_role
        packet=await review_role(engine,h,run_id,state,record,role,instruction)
        record=engine.store.get(COLLECTION,record['id'])
        checked(engine,run_id,record,state)
        validate_vote(engine,run_id,packet,supplement,p['context'],role)
        record={**record,'votes':[*record['votes'],packet]}
        engine.store.put(COLLECTION,record['id'],record)
    issues=[{'role':v['role'],**item} for v in record['votes'] for item in v['result']['checks'] if item['status']!='accept']
    record={**record,'status':'changes_required' if issues else 'reviewed', 'issues':issues,
        'active_role':None,'completed_at':record.get('completed_at') or now()}
    engine.store.put(COLLECTION,record['id'],record)
    if issues:
        raise ResearchBlocked('补充定义已完成三方独立审核，仍需定向修订：'+'；'.join(
            f"{i['role']} {i['id']}：{i['required_change']}" for i in issues))
    raise ResearchBlocked('补充定义已完成三方独立审核；待显式绑定新来源和作者修订交接。原实现稿及科学门禁未自动批准。')
