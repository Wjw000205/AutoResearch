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


