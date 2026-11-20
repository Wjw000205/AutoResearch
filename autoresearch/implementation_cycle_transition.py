"""Adopt the document/code loop at a settled definition revision boundary."""
import copy

from . import implementation_cycle as cycle, implementation_definition_repair as definitions
from .engine import ResearchBlocked, digest
from .storage import now

COLLECTION = 'implementation_cycle_transitions'


def switch(engine,run_id):
    from .architecture_validation import operation
    from .codex_workflow import _frozen_spec_author
    from .implementation_authoring import validate_submission
    from .host_asset_definition_recovery import author_evidence
    run = engine.store.get('runs',run_id)
    if run['stage'] != 'implementation' or run['status'] not in {'paused','blocked'}:
        raise ResearchBlocked('请在实现阶段已暂停或阻断的版本边界切换。')
    rows = [w for w in engine.store.list('codex_workflows',run_id) if w.get('kind')=='implementation'
            and w.get('cycle')==engine.cycle(run_id)
            and w.get('implementation_generation')==run.get('state',{}).get('implementation_generation')]
    if len(rows)!=1:
        raise ResearchBlocked('无法唯一定位当前实现任务。')
    state, = rows
    if cycle.active(state):
        return source(engine,run_id,state) or {'strategy':cycle.STRATEGY,'already_active':True}
    record = state.get(definitions.FIELD,{})
    prior = state['specifications'][-1] if state['specifications'] else {}
    if (record.get('kind')!='engineering_definition' or record.get('status')!='authoring'
            or state.get('status')!='specification' or prior.get('approved') is not False
            or prior['index']!=record['revision_indices'][-1] or state['current_revision']!=prior['index']+1):
        raise ResearchBlocked('原工作尚未到达已退回且下一版未派发的文档边界。')
    purpose = f"implementation_spec:definition-{record['id']}:{state['current_revision']}"
    if _frozen_spec_author(engine,run_id,purpose):
        raise ResearchBlocked('下一版作者输入已冻结，应先完成该版，保留其原请求。')
    packets = prior.get('definition_reviews',[])
    if not definitions._review_members(packets) or all(p['result']['approve'] for p in packets):
        raise ResearchBlocked('缺少同一退回版本的完整评审意见。')
    with engine.store.read_snapshot() as store:
        reader = copy.copy(engine)
        reader.store = store
        operation(definitions._source)(reader,run_id,state,record)
        validate_submission(reader,run_id,state,prior)
        for packet in packets:
            operation(definitions.validate_vote)(reader,run_id,state,prior,packet)
        evidence = author_evidence(reader,run_id,state,prior)
    base = definitions.base_row(state,record)
    body = {'run_id':run_id,'workflow_id':state['id'],'cycle':state['cycle'],'strategy':cycle.STRATEGY,
        'definition_id':record['id'],'source_sha256':record['source_sha256'],
        'base_index':base['index'],'base_spec_hash':base['spec_hash'],'previous_index':prior['index'],
        'previous_spec_hash':prior['spec_hash'],'original_reviews':[p['handoff_id'] for p in packets],
        'plan_sha256':digest(state['common']['experiment_plan']),
        'asset_evidence_sha256':evidence['sha256'] if evidence else None,
        'external_dependency':any(p['result'].get('host_asset_next_action')=='external_dependency' for p in packets),
        'execution_authorized':False,'scientific_approval':False}
    receipt = {**body,'sha256':digest(body),'created_at':now()}
    updated = {**state,'implementation_review_strategy':cycle.STRATEGY,'implementation_cycle_transition':receipt['sha256']}
    engine.store.batch(puts=[(COLLECTION,state['id'],receipt),('codex_workflows',state['id'],updated)],
        expected=[('runs',run_id,run),('codex_workflows',state['id'],state),(COLLECTION,state['id'],None)])
    engine.store.event(run_id,'implementation.cycle_switched','下一版采用文档与代码两轮审核。',body)
    return receipt


def source(engine,run_id,state):
    sha = state.get('implementation_cycle_transition')
    if not sha:
        return None
    receipt = engine.store.get(COLLECTION,state['id'],{})
    if (receipt.get('sha256')!=sha or digest({k:v for k,v in receipt.items() if k not in ('sha256','created_at')})!=sha
            or receipt.get('run_id')!=run_id or receipt.get('workflow_id')!=state['id']
            or receipt.get('cycle')!=state['cycle'] or receipt.get('plan_sha256')!=digest(state['common']['experiment_plan'])):
        raise ResearchBlocked('流程切换记录与当前版本不一致。')
    record = state.get(definitions.FIELD,{})
    previous = next((r for r in state['specifications'] if r['index']==receipt['previous_index']),{})
    if (record.get('id')!=receipt['definition_id'] or record.get('source_sha256')!=receipt['source_sha256']
            or record.get('base_index')!=receipt['base_index']
            or definitions.base_row(state,record)['spec_hash']!=receipt['base_spec_hash']
            or previous.get('spec_hash')!=receipt['previous_spec_hash']
            or [p['handoff_id'] for p in previous.get('definition_reviews',[])]!=receipt['original_reviews']):
        raise ResearchBlocked('迁移继承的原问题、批准版本或审核来源已发生变化。')
    return receipt
