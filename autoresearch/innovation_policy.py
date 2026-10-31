"""Shared prospective contribution standard, frozen with each new assignment."""

VERSION = 2

INSTRUCTION = (
    '\n实质贡献标准v2：新颖性不限于从0到1的原创。组合创新、架构改进、训练或推断机制改进均可成立，'
    '不要求每个组件首次提出，也不要求必须发现全新规律或提出新理论。'
    '候选机制无需简单，允许复杂组合与架构；效果及对痛点的实质贡献是核心。'
    '不限定方法类型、范式或实现路径，不以是否采用特定机制作为资格条件。'
    '只要能在本机当前真实硬件、已授权时限和预算内运行并完成验证，就可进入评审，不人为偏好轻量、CPU-only或小模型。'
    '资源是否可行由实际资源计划与主机门禁判断。'
    '统一判断以下问题：具体痛点是否仍存在且有文献或实际证据；组合关系或改进如何针对该痛点；'
    '相对当前最相似完整工作，新增了什么有意义的能力、适用范围、可靠性或效率；'
    '主要对照按痛点、目标、方法作用和适用条件选择当前最相关/最接近的已有工作，不能只对比弱基线或任意简单模型。'
    '检索同时覆盖近期相似工作与必要的经典源头，核查已有方案已解决和仍未解决的部分。'
    '采用能回答上述差异的公平比较；简单基线、组件单独使用、朴素组合、结构消融和等成本比较'
    '仅在具体贡献归因需要时使用，不要求每个候选机械地满足全部对照，也不把方法简单作为资格门槛。'
    '组合创新应说明组件间的信息流、耦合、约束或互补如何起作用；架构改进应说明所改变的结构或学习/推断机制'
    '为何能解决原有瓶颈。小幅代码或结构修改也可能有实质价值，修改规模、组件数量和模型复杂度不决定贡献大小。'
    '仅换名、常规调参、无机制理由的堆叠、只报涨点却不能归因，或不解决具体痛点的小修小改不合格。'
    '查重以完整的问题—机制—组合关系—适用条件为单位；单个组件已有、思想有源头或与近邻共享骨干，'
    '不能单独判定duplicate_found/high_overlap。应区分组件重合和贡献实质重合，给出可定位原文及完整机制对照。'
    '选题与新颖性审查阶段接受有依据且可证伪的贡献假设和验证计划，不要求先有尚未获批的实验结果；'
    '新颖性门禁采用已完成检索范围内的排重标准：没有找到具有可定位原文依据的高度相似完整机制，'
    '就放行到后续设计、共识与验证阶段；不要求证明全网不存在相同工作，也不要求先证明能力增量。'
    'insufficient_evidence仅说明结论局限；当已执行检索并完成核心阅读，且closest_work整体风险均为low/medium时，'
    '不能仅因检索覆盖不完整、来源不可得或还需实验验证而阻断新颖性门禁。'
    'differentiated表示当前检索范围内未发现高度相似机制，不表示已证明原创性或实测效果。'
    'high/duplicate只用于完整机制高度相似的具体对照；组件重合与未知近邻不能标为high/duplicate。'
    '没有实际检索/核心阅读、无效引用或明确的高相似线索尚未核实时先完成对应检查，不把这些情况判为不新颖。'
    '设计缺口明确修订，实质重复或无意义改动才退回；未验证能力与检索范围保留为局限。'
    'CCF A是研究目标，不是必须从零发明的条件，也不保证录用；最终价值仍须真实实验验证，预算、资源与三方共识门禁不变。'
)


def apply(instruction):
    return instruction if INSTRUCTION in instruction else instruction + INSTRUCTION


def update_pending_review(engine, run_id, clarification, *, reuse_existing_review=False):
    """Explicit criteria update; preserve evidence, counters and original votes.

    A fresh versioned review may run on resume. This is not a new candidate or
    an additional supplementation allowance, and never grants experiment consent.
    """
    import copy
    from .engine import ResearchBlocked
    from .storage import now

    store = engine.store
    run = store.get('runs', run_id)
    cycle = engine.cycle(run_id)
    session = store.get('innovation_sessions', f'{run_id}:{cycle}') or {}
    number = session.get('attempt', 1)
    key = f'{run_id}:novelty-evidence:{cycle}:{number}'
    audit_key = f'{key}:contribution-policy:{VERSION}'
    if store.get('stage_contexts', audit_key):
        return run
    state = store.get('stage_contexts', key)
    if (run['status'] not in {'blocked', 'paused'} or run['stage'] != 'related_work'
            or not state or not session.get('proposal_hash')
            or state['result'].get('selected_idea_hash') != session['proposal_hash']
            or store.get('consensus', run_id, {}).get('unanimous')
            or store.list('experiments', run_id)):
        raise ResearchBlocked('更新创新评审标准需要已暂停且未批准实验的相关论文审查；保留当前任务与评审记录。')
    audit = {'id': audit_key, 'run_id': run_id, 'kind': 'innovation_policy_update',
        'version': VERSION, 'clarification': clarification, 'instruction': INSTRUCTION,
        'previous_state': copy.deepcopy(state), 'previous_session': copy.deepcopy(session),
        'previous_step': store.get('steps', f'{run_id}:related_work'),
        'previous_error': run.get('error'), 'created_at': now()}
    state = {**state, 'gate': state.get('gate') if reuse_existing_review else None, 'policy_update': audit_key}
    updated = {**run, 'config': {**run['config'], 'innovation_policy_version': VERSION,
        'innovation_policy_clarification': clarification},
        'error': None, 'updated_at': now()}
    store.batch([('stage_contexts', audit_key, audit), ('stage_contexts', key, state),
        ('runs', run_id, updated)], deletes=[('steps', f'{run_id}:related_work')])
    store.event(run_id, 'innovation.policy_updated',
        '按用户要求，在已完成检索范围内未发现高度相似完整机制即通过新颖性门禁；保留原评审与覆盖局限，不批准实验。',
        {'version': VERSION, 'attempt': number, 'evidence_round': state['round'], 'audit_id': audit_key,
         'reuse_existing_review': reuse_existing_review})
    return updated
