"""Bind an operator-scoped document correction to the reviewed delivery that raised it."""
from . import implementation_cycle as cycle
from .engine import ResearchBlocked, digest

FIELD = 'implementation_document_revision'
COLLECTION = 'implementation_document_revisions'
MAX_EDITS = 64


def admit(engine, run_id, spec_hash, delivery_hash, instruction):
    from .codex_workflow import _frozen_spec_author
    run = engine.store.get('runs',run_id)
    workflows = [w for w in engine.store.list('codex_workflows',run_id)
        if w.get('kind')=='implementation' and w.get('cycle')==engine.cycle(run_id)
        and w.get('implementation_generation')==run.get('state',{}).get('implementation_generation')]
    if len(workflows)!=1 or not cycle.active(workflows[0]):
        raise ResearchBlocked('无法定位当前文档与代码工作流。')
    state, = workflows
    previous = current(engine,run_id,state)
    if previous and (previous['spec_hash'],previous['delivery_hash'],previous['instruction'])==(spec_hash,delivery_hash,instruction):
        return previous
    doc = state['specifications'][-1] if state['specifications'] else {}
    attempt = state['attempts'][-1] if state['attempts'] else {}
    if (run.get('status') not in ('paused','blocked') or run.get('stage')!='implementation'
            or state.get('status')!='implementing' or attempt.get('status')!='rejected'
            or state.get('current_revision')!=attempt['index']+1 or doc.get('approved') is not True
            or doc.get('spec_hash')!=spec_hash or attempt.get('delivery_hash')!=delivery_hash):
        raise ResearchBlocked('文档更正需要当前已退回的代码交付及其已批准文档。')
    if _frozen_spec_author(engine,run_id,f"implementation_write:{state['id']}:{doc['index']+1}"):
        raise ResearchBlocked('下一版文档请求已经冻结。')
    cycle.validate_document(engine,run_id,state,doc)
    votes = attempt.get('review_votes',[])
    cycle.check_votes(engine,run_id,votes,cycle.code_materials(engine,run_id,state,doc,attempt))
    if all(v['result']['approve'] for v in votes):
        raise ResearchBlocked('当前交付没有代码审核退回意见。')
    receipt = {'run_id':run_id,'workflow_id':state['id'],'cycle':state['cycle'],'base_index':doc['index'],
        'spec_hash':spec_hash,'document_sha256':doc['document']['sha256'],'attempt_index':attempt['index'],
        'delivery_hash':delivery_hash,'review_handoffs':[v['handoff_id'] for v in votes],
        'instruction':instruction,'proposal_hash':state['proposal_hash'],'plan_hash':digest(state['common']['experiment_plan'])}
    key = digest(receipt)
    engine.store.batch(puts=[(COLLECTION,key,receipt),('codex_workflows',state['id'],{
        **state,FIELD:key,'status':'specification','current_revision':doc['index']+1})],
        expected=[('runs',run_id,run),('codex_workflows',state['id'],state),(COLLECTION,key,None)])
    return receipt


def current(engine,run_id,state):
    key = state.get(FIELD)
    if not key:
        return None
    row = engine.store.get(COLLECTION,key,{})
    if (digest(row)!=key or row.get('run_id')!=run_id or row.get('workflow_id')!=state['id']
            or row['cycle']!=state['cycle'] or row['proposal_hash']!=state['proposal_hash']
            or row['plan_hash']!=digest(state['common']['experiment_plan'])):
        raise ResearchBlocked('文档更正授权与当前工作流不一致。')
    return row


def apply(previous, edits):
    from .implementation_authoring_preservation import _patch, SaveConflict
    from .implementation_document_locator import _node, _replace_text
    grouped = {}
    for edit in edits:
        grouped.setdefault(edit['path'],[]).append({'before':edit['before'],'after':edit['after']})
    if not grouped:
        raise SaveConflict('empty_edit','文档更正缺少实际改动。')
    return _patch(previous,[{'op':'replace','path':p,'value':_replace_text(_node(previous,p),items,limit=MAX_EDITS)}
                            for p,items in grouped.items()])


def schema(base_hash):
    text = {'type':'string','minLength':1}
    properties = {'path':{**text,'pattern':'^/'},'before':text,'after':{'type':'string'}}
    return {'type':'object','additionalProperties':False,'required':['base_spec_hash','edits'],
        'properties':{'base_spec_hash':{'const':base_hash},'edits':{'type':'array','minItems':1,'maxItems':MAX_EDITS,
            'items':{'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}}}}
