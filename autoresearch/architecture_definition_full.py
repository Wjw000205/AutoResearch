"""Versioned engineer definition authoring with two whole-source reviews."""
from __future__ import annotations
import copy

from . import independent_full_review as full
from .storage import now

FIELD='independent_full_definition_review'


def check_candidate(engine,run_id,record):
    from . import architecture_definitions as definitions, document_index
    from .diagnostic_planning_recovery import authenticate
    from .engine import ResearchBlocked
    state=record[FIELD];slot=state['rounds'][state.get('current_round',len(state['rounds'])-1)];request=slot['author']
    authenticate(engine,run_id,'engineer',request['purpose'],{k:request[k] for k in ('instruction','context','schema')},request['answer'])
    from .scientific_author_materials import validate as validate_author
    validate_author(engine,run_id,request)
    if record['candidate']!=request['answer'] or record['document']!=slot['document']:
        raise ResearchBlocked('全文科学定义未绑定同一实际工程师交付。')
    definitions._binding(record,record['candidate'])
    ids=[d['issue_id'] for d in record['candidate']['definitions']]
    _,text=document_index.load(engine,run_id,record['document']['document_id'],record['document']['sha256'])
    if len(ids)!=len(set(ids)) or set(ids)!={i['id'] for i in record['issues']} or text!=definitions._render(record,record['candidate']):
        raise ResearchBlocked('全文科学定义遗漏原问题或实际文档字节不同。')


def check_vote(engine,run_id,record,vote):
    from . import architecture_definitions as definitions
    from .engine import ResearchBlocked
    full.authenticate(engine.store,run_id,vote)
    full.validate_delivery(engine.store,run_id,vote,engine=engine)
    frozen=full.authenticate(engine.store,run_id,vote)
    if frozen['context'].get('issue_ledger_protocol')==1:
        from . import architecture_issue_ledger as ledger
        ledger.validate_vote(vote,frozen['context'],record['document'])
        if not ledger.evidence_covered(engine,run_id,record['document'],vote,
                full.reading_handoffs(engine.store,run_id,vote),frozen['context']):
            raise ResearchBlocked('定义全文复核的原关闭条件缺少本角色同版本原文交付。')
    definitions._binding(record,vote)
    ids=vote['reviewed_issue_ids']
    if vote['document_sha256']!=record['document']['sha256'] or len(ids)!=len(set(ids)) or set(ids)!={i['id'] for i in record['issues']}:
        raise ResearchBlocked('全文定义评议未覆盖同一版本全部原问题。')


async def advance(engine,h,run_id,revision,record):
    from . import architecture_definitions as definitions, architecture_revisions as revisions
    from .architecture_definition_completion import check_candidate as check_original
    from .diagnostic_planning_recovery import authenticate
    from .engine import ResearchBlocked, digest
    if FIELD not in record:
        if record.get('candidate') and record.get('document'):
            check_original(engine,run_id,record)
        record[FIELD]={'version':1,'legacy':copy.deepcopy(record),'rounds':[]}
        record[FIELD]['legacy'].pop(FIELD,None)
        engine.store.put('architecture_definitions',record['id'],record)
    state=record[FIELD]
    if state.get('version')!=1:raise ResearchBlocked('未知科学定义全文审核协议版本。')
    start=max(0,len(state['rounds'])-1)
    if state['rounds'] and ('votes' in state['rounds'][-1] or 'author_rejection' in state['rounds'][-1]):start=len(state['rounds'])
    for number in range(start,4):
        if len(state['rounds'])<=number:
            previous=state['rounds'][-1] if state['rounds'] else None
            context=definitions._context(engine,run_id,record)
            context.update(previous_definition_document=record.get('document'),
                    previous_candidate=record.get('candidate'),
                whole_definition_feedback=copy.deepcopy(previous.get('votes',[]) if previous else state['legacy'].get('votes',[])))
            if previous and previous.get('author_rejection'):context['whole_definition_author_diagnostic']=copy.deepcopy(previous['author_rejection'])
            from .scientific_author_materials import prepare
            request={'purpose':'architecture_definitions_author:full-v1:'+digest([record['id'],number]),
                'instruction':'工程师统一提交所有原科学问题的完整定义候选。按索引核对批准方案、原架构和必要来源，'
                    '保留原比较、种子、阈值、数据隔离、预算及未知。逐项处理保留意见，不能改变批准范围或声明实验证明。'
                    '本轮由你统一修订，随后总监、研究员各自阅读全文独立审核。',
                'context':prepare(engine,h,run_id,context),'schema':definitions._schema()}
            state['rounds'].append({'author':request})
            state['current_round']=number
            engine.store.put('architecture_definitions',record['id'],record)
        slot=state['rounds'][number];request=slot['author']
        state['current_round']=number
        if 'answer' not in request:
            from .scientific_author_materials import ask as ask_author
            slot['author']=request=await ask_author(engine,h,run_id,request)
            engine.check_active(run_id)
            engine.store.put('architecture_definitions',record['id'],record)
        candidate=request['answer']
        authenticate(engine,run_id,'engineer',request['purpose'],{k:request[k] for k in ('instruction','context','schema')},candidate)
        definitions._binding(record,candidate)
        if not candidate['compatible_with_approved_scope'] or candidate['scope_issues'] or any(d['status']=='unresolved' for d in candidate['definitions']):
            slot['author_rejection']={'scope_issues':copy.deepcopy(candidate['scope_issues']),
                'unresolved_issue_ids':[d['issue_id'] for d in candidate['definitions'] if d['status']=='unresolved'],
                'instruction':'按原批准范围自行补读、澄清具体未决定义；不能改变原比较、标准或预算。'}
            engine.store.put('architecture_definitions',record['id'],record)
            continue
        if 'document' not in slot:
            slot['document']=revisions._save(engine,h,run_id,
                f'cycle-{record["cycle"]}/architecture/definitions/full-v1-{digest(candidate)}.md',
                definitions._render(record,candidate),'architecture_scientific_definitions')
        record.update(candidate=candidate,document=slot['document'],status='reviewing')
        check_candidate(engine,run_id,record)
        if 'votes' not in slot:
            context=definitions._context(engine,run_id,record)
            docs=[record['document'],*[d for d in record['input_documents'] if d.get('kind') in {'approved_proposal','architecture_design'}]]
            context.update(definition_document=record['document'],indexed_documents=[record['document'],*context['indexed_documents']],
                independent_full_review_documents=docs,whole_definition_feedback=copy.deepcopy(state['legacy'].get('votes',[])))
            schema=definitions._schema(vote=True)
            from . import architecture_convergence as convergence
            if record.get('architecture_progress_protocol')==convergence.VERSION:
                context.update(convergence.context(engine,run_id,'definitions',candidate,role=None,structured=True))
                schema=convergence.schema(schema,structured=True)
            def verify(vote):
                check_vote(engine,run_id,record,vote)
            slot['votes']=await full.group(h,run_id,'architecture_definitions_review:full-v1:'+record['document']['sha256'],
                '各自独立读取完整科学定义候选、批准方案及架构。逐项覆盖全部definition_issues并回查必要来源，'
                '审查机制、公式、采样、种子、配置、归约、停止及科学边界；不得拆分角色或模块。'
                '不批准时给出具体位置及required_changes，不能把未知视为通过。保持原绑定哈希及document_sha256。'
                '若有完整历史问题账本，两位均须按原关闭条件核验全部ID，独立给出progress.issue_updates与实际原文锚点。',
                context,schema,validator=verify)
            engine.check_active(run_id)
            engine.store.put('architecture_definitions',record['id'],record)
        record['votes']=copy.deepcopy(slot['votes'])
        full.authenticate_complete(engine.store,run_id,record['votes'])
        approved=all(v['approve'] and not v['required_changes'] for v in record['votes'])
        record.update(status='approved' if approved else 'rejected',completed_at=now(),
            reason='完整定义已获同版本两位独立批准。' if approved else '完整定义仍有独立修改意见。')
        engine.check_active(run_id)
        engine.store.put('architecture_definitions',record['id'],record)
        if record.get('architecture_progress_protocol')==1:
            from . import architecture_convergence as convergence
            convergence.record(engine,run_id,'definitions',record,candidate)
        if approved:
            definitions.validate_adoption(engine,run_id,record['id'])
            from .architecture_integration import prepare_from_adoption,activate
            return await activate(engine,h,run_id,prepare_from_adoption(engine,run_id,revision,record))
        signature=digest(sorted(c for v in record['votes'] for c in v['required_changes']))
        if number and signature==state['rounds'][number-1].get('issue_signature'):
            raise ResearchBlocked('完整定义的相同独立意见尚未解决；历史候选和原票保留。')
        slot['issue_signature']=signature
        engine.store.put('architecture_definitions',record['id'],record)
        if record.get('architecture_progress_protocol')==1:convergence.check(engine,run_id,'definitions')
    raise ResearchBlocked('完整科学定义尚未取得同版本两位独立批准，保留全部修订与原票。')
