"""Stage-appropriate, bounded innovation routing without inventing approvals."""
from __future__ import annotations

import copy

from . import innovation_policy
from .storage import now

COMPARE = (
    '逐一审查三个候选并完整排序。当前决定是否值得开展第二轮定向文献核查，而不是验收论文成果。'
    'qualification按选题粒度填写：important_question是有文献动机的具体重要问题；'
    'substantive_mechanism是明确、有意义且可证伪的机制假设；discriminating_test是能排除强竞争解释的实验设计；'
    'resource_feasible是当前资源内能设计验证；not_repackaged是现有证据未确认实质重复。'
    '未做实验、未得到涨点、未完成实现或尚未完成第二轮近邻检索，均不直接否定上述选题条件。'
    'blocker_kind决定下一项工作：ready表示五项前瞻条件均满足；evidence_gap表示机制等基本内容明确，'
    '但需要定向阅读原文、经典近邻或核对相似性，不能当作已证明重复；design_gap表示作者尚需补全'
    '机制、竞争对照、否证条件或可行的资源方案；scientific_flaw表示已指出的实质机制错误、明确等价重复'
    '或问题缺乏意义，必须在concern中给可核查依据。多个问题并存时科学缺陷优先，其次设计缺口。'
    'concern保留具体异议；suggested_test写明确问题、近邻/章节或所需修订。不能只说“证据不足”或“达不到CCF A”。'
    'evidence_gap可以与五项true并存；如果只是未知近邻，not_repackaged=false仅表示待查，不代表已确认重复。'
    '若其他四项有false，系统先要求修订，不能用evidence_gap绕过具体机制与区分实验要求。'
    '排名只在可进入定向核查的候选中选一个；设计缺口交作者修订后重新审查，科学缺陷才要求重想。'
    '没有候选时不得强行通过；未执行的实验不得作证据。使用中文。'
)


def assignment(engine, run_id, purpose, instruction, context, schema):
    """Freeze new inputs and adopt old paid assignments across code upgrades.

    All peers in a batch must have the same input. Existing decisions are never
    interpreted as answers to a newer prompt/schema.
    """
    from .engine import ResearchBlocked, digest
    key = f'{run_id}:innovation-assignment:{purpose}'
    saved = engine.store.get('stage_contexts', key)
    if saved:
        return tuple(copy.deepcopy(saved[k]) for k in ('instruction', 'context', 'schema'))
    inputs = []
    for handoff in engine.store.list('handoffs', run_id):
        if handoff.get('purpose') == purpose:
            original = engine.store.get('handoff_inputs', handoff['id'])
            if original:
                inputs.append({k: original[k] for k in ('instruction', 'context', 'schema')})
    unique = {digest(row): row for row in inputs}
    if len(unique) > 1:
        raise ResearchBlocked('同一创新分工存在不同输入版本，保留原始请求，不能混用不同版本的回复')
    frozen = next(iter(unique.values())) if unique else {
        'instruction': innovation_policy.apply(instruction),
        'context': {**context, 'innovation_policy_version': innovation_policy.VERSION}, 'schema': schema}
    saved = {'id': key, 'run_id': run_id, 'kind': 'innovation_assignment',
             **copy.deepcopy(frozen), 'created_at': now()}
    engine.store.put('stage_contexts', key, saved)
    return tuple(copy.deepcopy(saved[k]) for k in ('instruction', 'context', 'schema'))


async def resolve_design(engine, harness, run_id, session, candidate_input, comparison_input):
    """Revise one promising repairable candidate, then obtain fresh peer reviews.

    At most two revisions in the same attempt; interruption replays exactly the
    saved assignments. Neither revision count nor scientific rejection count is
    reset by a process restart. No experiments are dispatched here.
    """
    from . import innovation, innovation_quality as quality, innovation_review
    from .engine import ResearchBlocked, digest
    key = f'{session["id"]}:design-revisions:{session["attempt"]}'
    state = engine.store.get('stage_contexts', key) or {
        'id': key, 'run_id': run_id, 'kind': 'innovation_design_revisions',
        'candidates': copy.deepcopy(session['candidates']),
        'comparisons': copy.deepcopy(session['comparisons']), 'history': []}
    while True:
        gate = quality.assess(state['comparisons'], stage_routing=True)
        session.update(candidates=copy.deepcopy(state['candidates']), comparisons=copy.deepcopy(state['comparisons']),
                       quality_gate=gate, design_revisions=copy.deepcopy(state['history']))
        engine.store.put('innovation_sessions', session['id'], session)
        if gate['eligible_candidates'] or not any(r == 'revise_design' for r in gate['routes'].values()):
            return session
        if len(state['history']) >= 2:
            session['status'] = 'needs_design_revision'
            engine.store.put('innovation_sessions', session['id'], session)
            raise ResearchBlocked('候选设计已完成两轮定向修订，仍有具体设计缺口；原候选与修订记录已保留，未判为科学否决或批准实验。')
        roles = [role for role, route in gate['routes'].items() if route == 'revise_design']
        owner = innovation.select_candidate(state['comparisons'], roles)['candidate_id']
        original = next(c for c in state['candidates'] if c['candidate_id'] == owner)
        number = len(state['history']) + 1
        purpose = f'innovation_candidate:{session["cycle"]}:attempt:{session["attempt"]}:design:{number}'
        base_instruction, base_context, schema = candidate_input
        context = {**base_context, 'design_revision': {'candidate_id': owner, 'revision': number,
            'proposal': original['proposal'], 'proposal_hash': original['proposal_hash'],
            'issues': gate['rejections'][owner]}}
        instruction = (base_instruction + '\n本次仅修订design_revision中的本人候选。逐项落实机制、强竞争对照、'
            '区分实验或资源设计的具体缺口，返回修订后的完整提案。保留原问题与未解决的证据缺口；'
            '不得重新生成其他角色候选，不得编造实验结果或宣称审查通过。')
        instruction, context, schema = assignment(engine, run_id, purpose, instruction, context, schema)
        engine.store.put('stage_contexts', key, state)
        session.update(status='revising_design', design_revision_owner=owner, design_revision_number=number)
        engine.store.put('innovation_sessions', session['id'], session)
        await innovation_review.seed_reading(engine, harness, run_id, purpose, instruction, context, schema, roles=(owner,))
        proposal = await harness.ask(run_id, owner, purpose, instruction, context, schema)
        revised = {**original, 'proposal': proposal, 'proposal_hash': digest(proposal)}
        candidates = [revised if c['candidate_id'] == owner else c for c in state['candidates']]
        compare_instruction, compare_context, compare_schema = comparison_input
        compare_purpose = f'innovation_compare:{session["cycle"]}:attempt:{session["attempt"]}:design:{number}'
        compare_context = {**compare_context, 'innovation_candidates': candidates,
            'design_revision': {'candidate_id': owner, 'previous_proposal_hash': original['proposal_hash'],
                                'issues': gate['rejections'][owner], 'revision': number}}
        compare_instruction, compare_context, compare_schema = assignment(
            engine, run_id, compare_purpose, compare_instruction + '\n检查作者具体修订是否解决原异议；'
            '对未改动候选保留先前依据，不能要求本阶段尚未获批的实验结果。', compare_context, compare_schema)
        replies = await innovation._batch(engine, harness, run_id, compare_purpose,
                                         compare_instruction, compare_context, compare_schema)
        comparisons = [{'role': role, **reply} for role, reply in zip(innovation.ROLES, replies)]
        quality.assess(comparisons, stage_routing=True)
        state['history'].append({'revision': number, 'candidate_id': owner,
            'previous_candidate': original, 'previous_comparisons': state['comparisons'],
            'previous_quality_gate': gate, 'proposal_hash': revised['proposal_hash'], 'completed_at': now()})
        state.update(candidates=candidates, comparisons=comparisons)
        engine.store.put('stage_contexts', key, state)
        engine.store.event(run_id, 'innovation.design_revised', '候选作者已完成定向修订，三方复审记录已保存。',
                           {'candidate_id': owner, 'revision': number, 'proposal_hash': revised['proposal_hash']})
