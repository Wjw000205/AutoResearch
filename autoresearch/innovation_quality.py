"""Prospective research-value screening, separate from novelty and execution.

All scientific judgments remain attributed to three reviewers. Ranking alone
cannot qualify a weak candidate; an explicit fresh brief never erases receipts.
"""
from __future__ import annotations

import copy

from . import innovation_policy
from .storage import now

ROLES = ('director', 'engineer', 'scout')
CRITERIA = ('important_question', 'substantive_mechanism', 'discriminating_test',
            'resource_feasible', 'not_repackaged')
AGENDA_FIELDS = {
    'research_question': '重要且具体的研究问题',
    'paradigm_limitation': '近期强方法仍未解决的共同限制及其适用范围',
    'why_simple_fixes_fail': '当前最相似工作为何尚未充分解决痛点；不预设对照或候选必须简单',
    'mechanism_claim': '核心机制、组合关系或架构改进及其针对痛点的实质作用',
    'competing_explanation': '当前最相近解法及适用边界、收益的竞争解释',
    'decisive_experiment': '与当前最相关工作公平比较的关键实验；按贡献归因需要安排消融',
    'falsification': '什么结果将否定核心假设并停止该方向',
    'scope_and_significance': '结论的适用边界与超出单数据集小幅涨点的价值',
}


def enabled(run):
    return run.get('config', {}).get('innovation_quality_protocol') == 1


def proposal_schema(base, context):
    if context.get('innovation_quality_protocol') != 1:
        return base
    schema = copy.deepcopy(base)
    text = {'type': 'string', 'minLength': 1, 'maxLength': 1600, 'pattern': r'\S'}
    schema['properties']['research_agenda'] = {'type': 'object', 'additionalProperties': False,
        'properties': {k: {**text, 'description': v} for k, v in AGENDA_FIELDS.items()},
        'required': list(AGENDA_FIELDS)}
    schema['required'] = [*schema['required'], 'research_agenda']
    return schema


def comparison_schema(base):
    schema = copy.deepcopy(base)
    item = schema['properties']['comparisons']['items']
    item['properties']['qualification'] = {'type': 'object', 'additionalProperties': False,
        'properties': {k: {'type': 'boolean'} for k in CRITERIA}, 'required': list(CRITERIA)}
    item['required'].append('qualification')
    return schema


CANDIDATE = (
    '\n研究价值门禁v1：先选择有意义的科学问题，再设计方法；不要把“对现有模型做一点改进”当作研究问题。'
    '三人独立开拓不同问题空间：总监从现有预测范式的关键假设出发；工程师从学习/推断机制的瓶颈出发；'
    '文献研究员从近期方法共享的盲点、反例或评测失真出发。角色视角是起点而非强制题目。'
    'research_agenda必须填写具体问题、当前相似工作未解决的限制、方案如何解决痛点、作用机制或原理、竞争解释、'
    '能区分解释的关键实验、否证条件与适用范围。用已读近期论文支撑问题，未知处必须标明假设。'
    '组合创新和架构改进可以构成贡献；说明组件间的作用关系或结构改变如何解决已有方法未解决的具体痛点，'
    '主要与当前最相似工作公平比较，必要时用针对性的对照解释效果来源。方法类型不限、机制无需简单，也无需揭示全新规律；'
    '不能仅靠加门控/权重/损失项/模块或换名宣称贡献，也不能仅因这些组件已有就否定有实质作用的改进。'
    '目标会议等级只是研究目标，不是已达到的结论。复杂架构与大模型同样可选，以有效性及本机真实资源可运行性判断。'
    '按当前真实资源设计足以区分假设的实验；先明确科学贡献，再用有辨识力的小实验检验，而非先把贡献缩成微调。'
    '若有research_brief/rejection_index，明确的科学否决是探索边界；证据缺口先补读，设计缺口先修订。'
    '不得把所有待办都视为方向已被否定，也不能对已证伪或已重复的方向只换名、换故事。'
)
INTAKE = (
    '从用户真实目标提出重要的研究问题和1至3个覆盖最新研究前沿的英文学术检索式。'
    '研究目标不是“给已有模型加模块并涨几个百分点”。优先识别领域内尚未解决的问题、近期强方法的共同假设与局限、'
    '能带来新认识的可证伪假设；检索覆盖前沿范式与负结果，不能仅围绕轻量小改动或某个已否定机制。'
    '本阶段界定问题空间，三个角色稍后独立提出不同候选。指标涨幅是执行验收约束，不是科学贡献定义。'
    '允许组合创新与架构改进，以是否直击痛点、有明确机制和可验证贡献判断，不把从0到1或全新理论设为前提。'
    '硬件与execution_constraints是当前用户上限，不能沿用旧提案的120秒或CPU-only限制；'
    '不预设简单、轻量或小模型优先，允许本机资源与预算内能运行的复杂机制。研究规模应足以检验实际效果并区分科学解释。'
    'research_brief是用户本次重选要求，旧方向是排除的探索边界；会议等级保留为目标，不得声称已达标。'
)
COMPARE = (
    '研究价值审查：对全部三个候选独立审查并完整排序，但排名不代表合格。逐个填写qualification：'
    'important_question（有近期文献支撑的重要问题，而非泛泛提高精度）；'
    'substantive_mechanism（针对痛点的明确机制、组合关系或架构改进，有可检验贡献，超出未经解释的模块拼接）；'
    'discriminating_test（关键实验与当前相似工作公平比较，能检验痛点是否解决和贡献来源，含否证与停止条件）；'
    'resource_feasible（在当前资源内能做有辨识力的实验）；not_repackaged（不是旧方向换名或候选之间同构改写）。'
    '逐项保守判断；concern说明哪些条件未满足及原因，suggested_test给具体区分实验或替代研究问题。'
    '比较候选之间的问题/核心机制是否实质不同，重合不应被包装成三个独立方向。'
    '三方均认可全部条件的候选才参与排名选取；三个都不合格就全部退回重新思考，不选矮子中的高个。'
    '这是进入聚焦文献核查的前瞻性判断，不是CCF-A质量认证、创新已证实或实验许可；证据不足必须保留。'
)
REVIEW = (
    '\n本任务采用研究价值门禁：继续检查research_agenda中的重要问题、机制贡献与区分实验。'
    '不要把数据/超参/故事补齐误判为核心贡献增强。若问题本身不重要或机制仅是近邻重述，必须明确否决，'
    '复杂机制或架构改进若实际解决原异议可以继续，但无实质作用的堆叠与叙事包装不算解决；完整实现检查安排在实现后的执行门禁。'
)
REVIEW = innovation_policy.apply(REVIEW)


def assess(comparisons, *, stage_routing=False):
    from .engine import ResearchBlocked
    if len(comparisons) != 3 or {r.get('role') for r in comparisons} != set(ROLES):
        raise ResearchBlocked('研究价值审查需要三个独立角色的完整意见')
    reasons = {role: [] for role in ROLES}
    for review in comparisons:
        items = review.get('comparisons', [])
        if len(items) != 3 or {i.get('candidate_id') for i in items} != set(ROLES):
            raise ResearchBlocked('研究价值审查必须覆盖所有候选且不能重复')
        for item in items:
            qualification = item.get('qualification', {})
            if set(qualification) != set(CRITERIA) or any(type(v) is not bool for v in qualification.values()):
                raise ResearchBlocked('研究价值审查缺少明确的逐项判断')
            failed = [k for k in CRITERIA if not qualification[k]]
            kind = item.get('blocker_kind')
            if stage_routing and kind not in {'ready', 'scientific_flaw', 'evidence_gap', 'design_gap'}:
                raise ResearchBlocked('研究价值审查缺少有效的处理分类；不能猜测审查人的结论')
            if (not stage_routing and 'blocker_kind' in item and ((kind == 'ready') != (not failed))
                    or stage_routing and kind == 'ready' and failed):
                raise ResearchBlocked('研究价值判断与待办分类不一致；不能用分类代替逐项审查')
            if failed or stage_routing and kind != 'ready':
                reasons[item['candidate_id']].append({'reviewer': review['role'], 'criteria': failed,
                    'reason': item['concern'], 'next_question_or_test': item['suggested_test'],
                    **({'blocker_kind': item['blocker_kind']} if 'blocker_kind' in item else {})})
    if stage_routing:
        routes = {}
        for candidate, issues in reasons.items():
            if any(i['blocker_kind'] == 'scientific_flaw' for i in issues):
                routes[candidate] = 'rethink'
            elif any(i['blocker_kind'] == 'design_gap' or set(i['criteria']) - {'not_repackaged'} for i in issues):
                # An evidence label cannot excuse an absent mechanism/test/resource plan.
                routes[candidate] = 'revise_design'
            elif issues:
                routes[candidate] = 'supplement_evidence'
            else:
                routes[candidate] = 'focused_reading'
        eligible = [r for r in ROLES if routes[r] in {'focused_reading', 'supplement_evidence'}]
        return {'version': 2, 'routes': routes, 'eligible_candidates': eligible,
            'rejections': reasons, 'approved_for_focused_reading': bool(eligible),
            'experiment_authorized': False,
            'scope': '按选题阶段分流：实质缺陷重想、设计缺口修订、证据缺口交定向核查。原始异议保留；不证明创新成立或实验已获批。'}
    eligible = [r for r in ROLES if not reasons[r]]
    return {'version': 1, 'eligible_candidates': eligible, 'rejections': reasons,
        'approved_for_focused_reading': bool(eligible), 'experiment_authorized': False,
        'scope': '三方前瞻性研究价值判断；不证明创新成立、达到目标会议水平或实验已获批。'}


def feedback(store, run_id, session):
    rows = {r['attempt']: r for r in store.list('innovation_attempts', run_id)
            if r.get('cycle') == session['cycle'] and r.get('quality_gate')}
    rows[session['attempt']] = session
    return {'latest_attempt': session['attempt'], 'history': [
        {'attempt': r['attempt'], 'quality_gate': r['quality_gate'],
         'rejected_directions': [{'role': c['role'], 'title': c['proposal']['title'],
              'research_agenda': c['proposal'].get('research_agenda', {})} for c in r.get('candidates', [])]}
        for _, r in sorted(rows.items())],
        'instruction': '保留历次否决依据。科学缺陷必须实质改变问题或机制；证据缺口先补读核查，不能只换名或重复承诺。'}


def next_attempt(engine, harness, run_id, session):
    """Durably reject all, with a shared feedback index and finite paid budget."""
    from .engine import ResearchBlocked
    from .literature_rounds import persist_index
    store = engine.store
    attempt = session['attempt']
    archive = {**session, 'id': f'{session["id"]}:{attempt}', 'status': 'rejected_quality'}
    store.put('innovation_attempts', archive['id'], archive)
    store.put('innovation_sessions', session['id'], {**session, 'status': 'rejected_quality'})
    if attempt - session['quality_start_attempt'] >= 2:
        raise ResearchBlocked('研究价值审查连续三次未选出合格方向；已保留具体否决理由，停止自动消耗预算。')
    rejection = {'quality_gate': session['quality_gate'],
        'rejected_directions': [{'role': c['role'], 'title': c['proposal']['title'],
            'research_agenda': c['proposal']['research_agenda']} for c in session['candidates']],
        'instruction': '从未解决的科学问题重新出发，实质改变问题或机制；不能仅补旧方向执行细节或换名。'}
    from .innovation_repair import enabled as repair_enabled
    repair = repair_enabled(store.get('runs', run_id))
    if repair:
        rejection = feedback(store, run_id, session)
    index = persist_index(engine, harness.intelligence, run_id, f'quality-rejection-{engine.cycle(run_id)}-{attempt}', rejection)
    store.put('innovation_sessions', session['id'], {'id': session['id'], 'run_id': run_id,
        'cycle': engine.cycle(run_id), 'attempt': attempt + 1, 'status': 'rethinking',
        'quality_protocol': 1, 'quality_start_attempt': session['quality_start_attempt'],
        'research_brief': session.get('research_brief'), 'rejection_index': index,
        **({'repair_source_attempt': attempt} if repair else {}),
        'created_at': session['created_at'], 'review_protocol': session.get('review_protocol')})
    store.event(run_id, 'innovation.quality_rejected', '三个候选均未通过研究价值审查，自动重新提出方向。',
                {'attempt': attempt, 'quality_gate': session['quality_gate']})


def restart_pending(engine, intelligence, run_id, brief):
    """Explicit user-requested change of research brief. Idempotent migration."""
    from .engine import ResearchBlocked
    from .literature_rounds import persist_index
    store = engine.store
    run = store.get('runs', run_id)
    if enabled(run):
        return run
    if (run['status'] not in {'draft', 'paused', 'blocked', 'failed'}
            or run['stage'] not in {'innovation', 'related_work', 'debate'}
            or store.get('consensus', run_id, {}).get('unanimous') is True
            or store.list('experiments', run_id)):
        raise ResearchBlocked('研究方向重开仅适用于未运行且尚未获批实验的任务')
    cycle = engine.cycle(run_id)
    session = store.get('innovation_sessions', f'{run_id}:{cycle}') or {}
    if not session or run['config'].get('literature_protocol') != 2:
        raise ResearchBlocked('研究方向重开需要已有创新记录和两轮文献分析流程')
    attempt = session.get('attempt', 1)
    consensus = store.get('consensus', run_id) or {}
    stages = ('intake', 'literature', 'innovation', 'related_work', 'debate')
    archive = {**session, 'id': f'{run_id}:{cycle}:quality-upgrade', 'status': 'superseded_direction',
        'attempt': attempt, 'upgrade_reason': brief, 'consensus': consensus,
        'stage_records': {s: store.get('steps', f'{run_id}:{s}') for s in stages},
        'stage_contexts': {s: store.get('stage_contexts', f'{run_id}:{s}') for s in stages},
        'literature_inputs': {key: store.get('literature_inputs', key) for key in (run_id, f'{run_id}:reading-analysis')},
        'archived_at': now()}
    proposal = consensus.get('proposal') or session.get('proposal') or {}
    feedback = {'research_brief': brief, 'rejected_title': proposal.get('title'),
        'rejected_mechanism': proposal.get('method'), 'reason': '用户要求跳出缺乏实质贡献的小修小补，重选有意义的问题。',
        'scope': '旧记录完整保留；这里只传本次重选依据，不把旧方向当待修补的初始提案。'}
    index = persist_index(engine, intelligence, run_id, f'research-brief-{cycle}-{attempt+1}', feedback)
    fresh = {'id': session['id'], 'run_id': run_id, 'cycle': cycle, 'attempt': attempt + 1,
        'status': 'rethinking', 'review_protocol': 1, 'quality_protocol': 1, 'quality_start_attempt': attempt + 1,
        'research_brief': brief, 'rejection_index': index, 'created_at': now()}
    updated = {**run, 'config': {**run['config'], 'innovation_quality_protocol': 1, 'innovation_review_protocol': 1,
        'research_brief': brief}, 'stage': 'intake', 'progress': 0, 'error': None, 'updated_at': now(),
        'state': {**{key: value for key, value in run.get('state', {}).items() if key not in {
            'implementation_restart', 'implementation_generation', 'implementation_graph_thread_id'}},
            'research_brief_restart': archive['id']}}
    store.batch([('innovation_attempts', archive['id'], archive), ('innovation_sessions', session['id'], fresh),
        ('runs', run_id, updated)], deletes=[('consensus', run_id),
        ('literature_inputs', run_id), ('literature_inputs', f'{run_id}:reading-analysis')] +
        [(kind, f'{run_id}:{s}') for s in stages for kind in ('steps', 'stage_contexts')])
    store.event(run_id, 'innovation.direction_reopened', '按用户要求归档旧方向，从重要研究问题重新提出创新候选。',
                {'attempt': attempt + 1, 'rejection_index': index})
    return updated
