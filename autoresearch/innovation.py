"""Three independent candidates, attributed comparison, then one discussion seed.

Selection is deterministic and is never experiment authorization. All model work
uses the existing durable Harness, transport recovery and budget preflight.
"""
from __future__ import annotations

import asyncio
import copy

from . import idea_provenance, innovation_review, innovation_quality, innovation_repair, innovation_routing
from .storage import now

ROLES = ('director', 'engineer', 'scout')


def enable_for_pending_run(store, run_id):
    """Opt an inactive, not-yet-approved legacy task into the new stage."""
    from .engine import ResearchBlocked
    step = store.get('steps', f'{run_id}:debate') or {}
    consensus = store.get('consensus', run_id) or {}
    def upgrade(run):
        if not run:
            raise ResearchBlocked('研究任务不存在')
        if run['config'].get('innovation_protocol') == 1:
            return run
        if (run['status'] not in {'draft', 'paused', 'blocked', 'failed'}
                or run['stage'] not in {'intake', 'literature', 'debate', 'innovation'}
                or step.get('status') == 'completed' or consensus.get('unanimous') is True):
            raise ResearchBlocked('只能为未运行且尚未通过共识的任务补充创新点讨论')
        return {**run, 'config': {**run['config'], 'innovation_protocol': 1}, 'updated_at': now()}
    return store.mutate('runs', run_id, upgrade)


TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 2400, 'pattern': r'\S'}
COMPARISON_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'ranking': {'type': 'array', 'minItems': 3, 'maxItems': 3, 'uniqueItems': True,
                    'items': {'enum': list(ROLES)}},
        'rationale': TEXT,
        'comparisons': {'type': 'array', 'minItems': 3, 'maxItems': 3, 'items': {
            'type': 'object', 'additionalProperties': False,
            'properties': {'candidate_id': {'enum': list(ROLES)}, 'strength': TEXT,
                           'concern': TEXT, 'suggested_test': TEXT},
            'required': ['candidate_id', 'strength', 'concern', 'suggested_test']}},
    }, 'required': ['ranking', 'rationale', 'comparisons'],
}
CANDIDATE_INSTRUCTION = (
    '创新点独立思考阶段：根据同一份研究目标、已保存文献与资源约束，独立提出一个候选创新点。'
    '不要请求其他角色替你提出想法，不要假设其他角色已同意。研究总监侧重科学问题与可证伪机制；'
    '实验工程师侧重可实现的结构/算法改变与成本；文献研究员侧重相近工作未解决的缺口与反例。'
    '清楚说明与基线的实质差异、已有证据、未知部分及可否定该想法的最小实验；'
    '允许针对痛点的组合创新与架构改进，但不能仅凭组件组合、换名或涨点宣称贡献已证实。'
    '只使用本任务证据标识；资源计划必须匹配硬件、已安装依赖和实验预算。返回一份完整候选提案，使用中文。'
    '后续研究周期若提供 previous_proposal 与 previous_cycle，应针对已记录的验证失败改进，不得退回已评估的旧方案。'
)
COMPARISON_INSTRUCTION = (
    '创新点比较讨论阶段：现在三位角色的独立候选已全部保存。逐一比较全部三个候选的科学价值、'
    '与已有工作的差异、反例、证据缺口和资源可行性，每个候选给出优势、担忧和验证建议。'
    'ranking 按值得优先深入讨论的顺序列出全部 candidate_id，不能遗漏、重复或偏袒自己的候选。'
    '这是选择讨论方向，不是批准实验；即使三个候选都有问题也要给出相对排序并保留所有担忧。'
    '系统按第一名2分、第二名1分、第三名0分汇总；同分先比较第一名票数，再采用研究总监的排序。'
    '最终只选择一个完整候选进入后续严格共识评审。使用中文，不声称选择过程验证了创新性。'
)


def select_candidate(comparisons, eligible=None):
    """Require all three attributable complete rankings; never fabricate consent."""
    from .engine import ResearchBlocked
    if len(comparisons) != 3 or {row.get('role') for row in comparisons} != set(ROLES):
        raise ResearchBlocked('创新点讨论尚未收到三个角色的完整比较意见')
    scores, first = dict.fromkeys(ROLES, 0), dict.fromkeys(ROLES, 0)
    for row in comparisons:
        ranking = row.get('ranking', [])
        assessments = row.get('comparisons', [])
        if (len(ranking) != 3 or set(ranking) != set(ROLES) or len(assessments) != 3
                or {entry.get('candidate_id') for entry in assessments} != set(ROLES)):
            raise ResearchBlocked('创新点比较必须覆盖全部三个候选，不能遗漏或重复')
        for index, candidate in enumerate(ranking):
            scores[candidate] += 2 - index
        first[ranking[0]] += 1
    director = next(row['ranking'] for row in comparisons if row['role'] == 'director')
    pool = ROLES if eligible is None else eligible
    if not pool or not set(pool) <= set(ROLES):
        raise ResearchBlocked('没有通过研究价值门禁的候选，不能仅凭相对排名强行选定方向')
    chosen = min(pool, key=lambda role: (-scores[role], -first[role], director.index(role)))
    return {'candidate_id': chosen, 'scores': scores, 'first_choices': first,
            'rule': '三方排序分别计2/1/0分；同分先比较首选票数，再采用研究总监排序。',
            'experiment_authorized': False,
            'scope': '只选定一个待讨论方向；原始分歧与证据缺口保留，仍需三方共识及资源、证据门禁。'}


async def _batch(engine, harness, run_id, purpose, instruction, context, schema):
    from .engine import Harness
    await innovation_review.seed_reading(engine, harness, run_id, purpose, instruction, context, schema)
    if engine.store.get('runs', run_id)['mode'] == 'live' and hasattr(harness.intelligence, 'preflight_batch'):
        pending = [role for role in ROLES if not engine.store.get('decisions',
                   Harness.decision_key(run_id, role, purpose, instruction, context, schema))]
        for role in pending:
            await harness.complete_pending_tools(run_id, role, purpose, instruction, context, schema)
        pending = [role for role in pending if not engine.store.get('decisions',
                   Harness.decision_key(run_id, role, purpose, instruction, context, schema))]
        if pending:
            requests = [harness.next_request(run_id, role, purpose, instruction, context, schema) for role in pending]
            if hasattr(harness.intelligence, 'prepare_context_batch'):
                await harness.intelligence.prepare_context_batch(run_id, requests)
            harness.intelligence.preflight_batch(run_id, requests)
    return await engine._collect_debate_votes([asyncio.create_task(
        harness.ask(run_id, role, purpose, instruction, context, schema)) for role in ROLES])


async def discuss(engine, harness, run_id, common):
    from .engine import Harness, PROPOSAL, digest
    cycle = engine.cycle(run_id)
    key = f'{run_id}:{cycle}'
    saved = engine.store.get('innovation_sessions', key)
    if saved and saved.get('status') == 'completed':
        return saved
    # Candidate requests share one immutable input and contain no peer proposals.
    context = {**common, 'innovation_protocol': 1}
    strict = innovation_review.enabled(engine.store.get('runs', run_id))
    quality = innovation_quality.enabled(engine.store.get('runs', run_id))
    repair = quality and innovation_repair.enabled(engine.store.get('runs', run_id))
    if repair and (saved or {}).get('repair_source_attempt') is not None:
        engine.store.put('innovation_sessions', key, {**saved, 'status': 'repairing_evidence'})
        repair_index = await innovation_repair.prepare(engine, harness, run_id, saved, common)
        context['innovation_repair_index'] = repair_index
        context['evidence'] = innovation_review.load_index(engine, run_id, repair_index)[0]['evidence']
    attempt = (saved or {}).get('attempt', 1)
    if quality:
        context.update(innovation_quality_protocol=1, innovation_attempt=attempt,
                       research_brief=(saved or {}).get('research_brief') or common.get('research_brief'))
        if (saved or {}).get('rejection_index'):
            context['rejection_index'] = saved['rejection_index']
        # A rejected direction is a boundary, never the seed for another tweak.
        context.pop('proposal', None)
        context.pop('previous_proposal', None)
        context.pop('design', None)
    if strict:
        index = common.get('idea_literature_index')
        payload, _ = innovation_review.load_index(engine, run_id, index)
        from .literature_rounds import recent, policy
        papers = payload.get('papers', [])
        from .reference_papers import uploaded_paper_ids
        uploaded_ids = uploaded_paper_ids(engine.store, run_id)
        recent_papers = [p for p in papers if p.get('generation') != 'reused_external_analysis'
                         and recent(p, policy(engine.store, run_id))]
        if not recent_papers or any(not recent(p, policy(engine.store, run_id))
                and p.get('paper_id') not in uploaded_ids for p in papers):
            from .engine import ResearchBlocked
            raise ResearchBlocked('创新讨论需要当前日期窗口内的已读近期论文，不能用旧论文或日期不明的材料替代')
        context.update(innovation_review_protocol=1, required_literature_index=index,
                       recent_paper_ids=[p['paper_id'] for p in recent_papers],
                       uploaded_paper_ids=sorted(uploaded_ids), innovation_attempt=attempt)
        if (saved or {}).get('rejection_index'):
            context['rejection_index'] = saved['rejection_index']
    revision = engine.store.get('revision_plans', f'{run_id}:{cycle}')
    if revision and not quality:
        context['previous_proposal'] = revision['proposal']
    instruction = CANDIDATE_INSTRUCTION
    if context.get('literature_protocol') == 2:
        instruction += ('\nidea_literature_index是三位角色收到的同一份第一轮文献索引。先用artifacts.read分页读取目录，'
            '再用memory.search与sources.read按需读取近期论文的方法架构、局限和原文；不要要求在聊天中打包全部论文。'
            '提出idea后系统另有第二轮最相关论文检索，当前不能把待核实的经典方法比较写成已经完成。')
        if context.get('uploaded_paper_ids'):
            instruction += ('\n同一索引也包含用户上传seed的精读笔记或明确归属的历史RAG分析；'
                '结合这些seed与近期工作提出idea，旧seed不计近期进展。外部分析保留未核验状态与原来源，不能冒充本次新阅读。')
    if common.get('idea_provenance_protocol') == 1:
        instruction += idea_provenance.PROPOSAL_INSTRUCTION
    if quality:
        instruction += innovation_quality.CANDIDATE
    if repair:
        instruction += innovation_repair.CANDIDATE
    schema = idea_provenance.proposal_schema(PROPOSAL, context)
    if strict:
        instruction += ('\n三位角色的同一份最新进展材料已通过artifacts.read分别读取到tool_results。'
            'research_progress总结已读论文的进展、架构与剩余缺口，paper_ids引用其中的论文。'
            '若有rejection_index，其内容也已读取：依据相似论文与退回意见处理具体缺陷，不能只改标题或故事线。'
            '科学缺陷须实质改变机制或问题，材料缺口结合补充阅读处理。解释论文故事的问题、缺口、假设、方法和验证计划，保留尚未证实的部分。')
        schema = innovation_review.candidate_schema(schema, context)
    attempt_suffix = f':attempt:{attempt}' if strict or quality else ''
    purpose = f'innovation_candidate:{cycle}{attempt_suffix}'
    instruction, context, schema = innovation_routing.assignment(engine, run_id, purpose, instruction, context, schema)
    candidate_input = (instruction, context, schema)
    session = {'id': key, 'run_id': run_id, 'cycle': cycle, 'status': 'candidates',
               'created_at': (saved or {}).get('created_at', now()),
               'provenance': engine.store.get('runs', run_id)['mode'],
               'candidate_handoffs': {role: Harness.decision_key(run_id, role, purpose, instruction, context, schema)
                                      for role in ROLES}}
    if strict:
        session.update(attempt=attempt, review_protocol=1, rejection_index=(saved or {}).get('rejection_index'))
    if quality:
        session.update(attempt=attempt, quality_protocol=1,
            quality_start_attempt=(saved or {}).get('quality_start_attempt', attempt),
            research_brief=context.get('research_brief'),
            rejection_index=(saved or {}).get('rejection_index'))
    if repair:
        session.update(constructive_protocol=1, repair_source_attempt=(saved or {}).get('repair_source_attempt'),
                       repair_index=context.get('innovation_repair_index'))
    if context.get('idea_literature_index'):
        session['literature_index'] = copy.deepcopy(context['idea_literature_index'])
    engine.store.put('innovation_sessions', key, session)
    replies = await _batch(engine, harness, run_id, purpose, instruction, context, schema)
    candidates = [{'candidate_id': role, 'role': role, 'proposal': reply, 'proposal_hash': digest(reply)}
                  for role, reply in zip(ROLES, replies)]
    compare_context = {**context, 'innovation_candidates': candidates}
    purpose = f'innovation_compare:{cycle}{attempt_suffix}'
    # The same three roles already received this snapshot before their candidates.
    compare_context.pop('required_literature_index', None)
    compare_instruction = innovation_quality.COMPARE if quality else COMPARISON_INSTRUCTION
    compare_schema = innovation_quality.comparison_schema(COMPARISON_SCHEMA) if quality else COMPARISON_SCHEMA
    if repair:
        compare_instruction = innovation_routing.COMPARE + innovation_repair.STAGE_RULES
        compare_schema = innovation_repair.comparison_schema(compare_schema)
    compare_instruction, compare_context, compare_schema = innovation_routing.assignment(
        engine, run_id, purpose, compare_instruction, compare_context, compare_schema)
    session.update(status='comparing', candidates=candidates,
        comparison_handoffs={role: Harness.decision_key(run_id, role, purpose, compare_instruction,
                             compare_context, compare_schema) for role in ROLES})
    engine.store.put('innovation_sessions', key, session)
    replies = await _batch(engine, harness, run_id, purpose, compare_instruction, compare_context, compare_schema)
    comparisons = [{'role': role, **reply} for role, reply in zip(ROLES, replies)]
    eligible = None
    if quality:
        session.update(comparisons=comparisons, quality_gate=innovation_quality.assess(comparisons, stage_routing=repair))
        engine.store.put('innovation_sessions', key, session)
        if repair:
            session = await innovation_routing.resolve_design(engine, harness, run_id, session,
                candidate_input, (innovation_routing.COMPARE + innovation_repair.STAGE_RULES, compare_context, compare_schema))
            candidates, comparisons = session['candidates'], session['comparisons']
        eligible = session['quality_gate']['eligible_candidates']
        if not eligible:
            innovation_quality.next_attempt(engine, harness, run_id, session)
            return await discuss(engine, harness, run_id, common)
    selection = select_candidate(comparisons, eligible)
    if quality:
        selection['rule'] = ('在基本机制、区分实验和资源方案明确且无科学否决的候选中按三方排序选择；'
            '待查证据随候选交第二轮核查，不作为创新已证实。' if repair else
            '仅三方均通过研究价值门禁的候选参与选择；按三方排序计分，同分先比较首选票数，再用总监排序。')
        if repair:
            selection.update(route=session['quality_gate']['routes'][selection['candidate_id']],
                             verification_tasks=session['quality_gate']['rejections'][selection['candidate_id']])
    candidate = next(row for row in candidates if row['candidate_id'] == selection['candidate_id'])
    if candidate['proposal_hash'] in {row.get('proposal_hash') for row in engine.store.list('research_cycles', run_id)}:
        from .engine import ResearchBlocked
        raise ResearchBlocked('创新点收束重复了已评估的旧方案；需要针对已有验证结果进行实质修改')
    session.update(status='completed', comparisons=comparisons, selection=selection,
                   proposal=copy.deepcopy(candidate['proposal']), proposal_hash=candidate['proposal_hash'],
                   completed_at=now())
    engine.store.put('innovation_sessions', key, session)
    engine.store.event(run_id, 'innovation.selected', '三个独立候选已比较，选定一个方向进入相关论文核查',
                       {'cycle': cycle, 'candidate_id': selection['candidate_id'], 'proposal_hash': session['proposal_hash']})
    return session


def view(engine, run_id):
    """Read-only projection, including completed peers in an interrupted batch."""
    session = engine.store.get('innovation_sessions', f'{run_id}:{engine.cycle(run_id)}')
    if not session:
        return None
    session = copy.deepcopy(session)
    if session.get('review_protocol') == 1 or session.get('attempt'):
        session['history'] = sorted([row for row in engine.store.list('innovation_attempts', run_id)
                                    if row.get('cycle') == engine.cycle(run_id)], key=lambda row: row['attempt'])
        for row in session['history']:
            row.pop('stage_contexts', None)
            row.pop('stage_records', None)
        session['reading_receipts'] = [row for row in engine.store.list('innovation_readings', run_id)
                                      if row['id'] in session.get('candidate_handoffs', {}).values()]
    for field, kind in [('candidate_handoffs', 'candidates'), ('comparison_handoffs', 'comparisons')]:
        if kind not in session:
            session[kind] = []
            for role, key in session.get(field, {}).items():
                decision = engine.store.get('decisions', key)
                if decision:
                    result = decision['result']
                    session[kind].append({'role': role, 'candidate_id': role, 'proposal': result}
                                         if kind == 'candidates' else {'role': role, **result})
    return session
