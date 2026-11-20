"""Full architecture review, preserving historical segmented decisions."""
from __future__ import annotations
import copy

from . import independent_full_review as full
from .storage import now

FIELD = 'independent_full_architecture_review'


async def review(engine, harness, run_id, design, saved, previous):
    from .architecture_documents import _schema
    from .engine import digest, ResearchBlocked
    record = copy.deepcopy(previous or saved)
    from . import architecture_convergence as convergence
    from .design_review_policy import VERSION as REVIEW_POLICY
    if previous is None:
        record['design_review_policy_version']=REVIEW_POLICY
        if convergence.enabled(engine,run_id):record['architecture_progress_protocol']=convergence.VERSION
    if FIELD not in record:
        record[FIELD] = {'version': 1, 'legacy': copy.deepcopy(previous), 'reviews': []}
    state = record[FIELD]
    if state.get('version')!=1:raise ResearchBlocked('未知架构全文审核协议版本。')
    context = {'document_index_protocol': 1, 'indexed_document_review': True,
        'architecture_workflow_protocol': 1, 'document_read_window_protocol': 3,
        'research_cycle': saved['cycle'], 'design_hash': saved['design_hash'],
        'proposal_hash': saved['proposal_hash'], 'document_sha256': saved['document']['sha256'],
        'architecture_document': saved['document'], 'approved_proposal': saved['approved_proposal'],
        'indexed_documents': [saved['document'], saved['approved_proposal']],
        'independent_full_review_documents': [saved['document'], saved['approved_proposal']],
        'prior_findings': [{'role': v.get('role'), 'issues': copy.deepcopy(v.get('issues', []))}
            for v in [*(previous or {}).get('unit_reviews', []), *(previous or {}).get('reviews', [])] if v.get('issues')]}
    instruction = ('各自独立阅读全文并审查整份架构，与已批准研究方案、完整实验组合、方法、接口、数据隔离、'
        '资源、种子、评价、验收和停止规则逐项核对。索引不是阅读，使用documents.read自行读完当前文档。'
        '旧发现必须明确判断是否仍成立，有问题给当前位置与具体修改要求。不得分组、分角色分担审核。'
        '通过仅允许进入实现，不代表实验证明或科学成功。返回同一document_sha256和design_hash。')
    from . import architecture_issue_ledger as ledger, design_review_policy as review_policy
    structured=record.get('architecture_progress_protocol')==convergence.VERSION
    schema=_schema(final=True)
    if record.get('design_review_policy_version')==review_policy.VERSION:
        context.update(design_review_policy_version=review_policy.VERSION,design_review_issue_field='issues')
        schema=review_policy.schema(schema,'issues')
        instruction+='\n'+review_policy.REVIEW
    if structured:
        context.update(convergence.context(engine,run_id,'architecture',design,role=None,structured=True))
        schema=convergence.schema(schema,structured=True)
        instruction+='\n'+convergence.STRUCTURED_REVIEW+'\n本协议每位审核者均核验完整旧问题账本，包括其他历史角色提出的问题。'
    if not state['reviews']:
        def verify(vote):
            if structured:
                ledger.validate_vote(vote,context,saved['document'])
                if not ledger.evidence_covered(engine,run_id,saved['document'],vote,
                        full.reading_handoffs(engine.store,run_id,vote),context):
                    raise ValueError('原问题的闭合或重开缺少本角色同版本真实原文。')
        state['reviews'] = await full.group(harness, run_id, 'design_document_review:full-v1:' + digest(design),
            instruction, context, schema,validator=verify)
        record.update(reviews=copy.deepcopy(state['reviews']), status='reviewing', updated_at=now())
        engine.check_active(run_id)
        engine.store.put('architecture_reviews', saved['id'], record)
    full.authenticate_complete(engine.store, run_id, state['reviews'])
    approved = all(v.get('approve') is True and not v.get('issues')
        and v.get('document_sha256') == saved['document']['sha256']
        and v.get('design_hash') == saved['design_hash'] for v in state['reviews'])
    record.update(reviews=copy.deepcopy(state['reviews']), status='approved' if approved else 'rejected', completed_at=now())
    engine.check_active(run_id)
    engine.store.put('architecture_reviews', saved['id'], record)
    if not approved:
        raise ResearchBlocked('架构全文独立审核有具体修改意见；原记录与两位审核意见已保留。')
    gate(engine, run_id, record)
    return record


def gate(engine, run_id, record):
    from .engine import ResearchBlocked
    if record.get(FIELD,{}).get('version')!=1:raise ResearchBlocked('未知架构全文审核协议版本。')
    votes = record.get(FIELD, {}).get('reviews', [])
    full.authenticate_complete(engine.store, run_id, votes)
    for vote in votes:
        full.validate_delivery(engine.store, run_id, vote, engine=engine)
        frozen=full.authenticate(engine.store,run_id,vote)
        if frozen['context'].get('issue_ledger_protocol')==1:
            from . import architecture_issue_ledger as ledger
            ledger.validate_vote(vote,frozen['context'],record['document'])
            if not ledger.evidence_covered(engine,run_id,record['document'],vote,
                    full.reading_handoffs(engine.store,run_id,vote),frozen['context']):
                raise ResearchBlocked('架构全文复核的原关闭条件缺少实际原文交付。')
        if (vote.get('approve') is not True or vote.get('issues')
                or vote.get('document_sha256') != record['document']['sha256']
                or vote.get('design_hash') != record['design_hash']):
            raise ResearchBlocked('架构全文审核未批准同一冻结版本。')
