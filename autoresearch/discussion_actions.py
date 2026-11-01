"""Durable work between reviews: owners deliver remedies before another vote.

These are proposed repairs, never approvals or experiment results. Original
reviews, paid assignments and the stagnation counter remain immutable.
"""
from __future__ import annotations

import copy
from datetime import datetime

from .discussion import MATERIAL_FIELDS, previous_review, platform_evidence_work
from .storage import now


def enabled(run):
    return run.get('mode') == 'live' and run.get('config', {}).get('discussion_mode') == 'until_stalled'


def scope(engine, run_id):
    cycle = engine.cycle(run_id)
    session = engine.store.get('innovation_sessions', f'{run_id}:{cycle}') or {}
    return f'{cycle}:idea:{session["attempt"]}' if session.get('attempt') else str(cycle)


def recovery_allowed(engine, run_id, record):
    """One work-based recovery per cycle; replay resumes that exact round."""
    if not enabled(engine.store.get('runs', run_id)):
        return False
    votes = record.get('votes') or []
    if (len(votes) == 3 and {v.get('role') for v in votes} == {'director', 'engineer', 'scout'}
            and all(v.get('approve') is True and not v.get('required_changes')
                    and v.get('proposal_hash') == record['proposal_hash'] for v in votes)
            and platform_evidence_work(record)):
        work = engine.store.get('stage_contexts',
            f'{run_id}:discussion-actions:{scope(engine, run_id)}:{record["round"]}', {})
        if work and not work.get('platform_blockers'):
            # Replaying a frozen legacy revision cannot reserve new source
            # work. Its original one-round recovery is handled below.
            return _legacy_recovery_allowed(engine, run_id, record)
        # A platform source defect is a different work owner from scientific
        # dissent. Supply one targeted recovery, preserving old votes and the
        # stagnation count; repeated identical paid work resumes, never rebuys.
        from .engine import digest
        key = f'{run_id}:platform-evidence-work-recovery:{scope(engine, run_id)}'
        identity = {'round': record['round'], 'proposal_hash': record['proposal_hash'],
                    'assessment_sha256': digest(record['evidence_assessment'])}
        prior = engine.store.get('stage_contexts', key)
        origin = {}
        if prior and any(prior.get(k) != v for k, v in identity.items()):
            if not _unused_legacy_recovery(engine, run_id, prior):
                return False
            # v1 could reserve an already frozen legacy round during replay.
            # Retain that exact record and bind one corrective fresh boundary
            # to its hash; an actual platform assignment cannot use this path.
            origin = {'original_recovery_id': prior['id'], 'original_recovery_sha256': digest(prior),
                      'recovery_version': 2, 'trigger': 'legacy_replay_reserved_without_platform_work'}
            key += ':fresh-boundary-v2'
        saved = engine.store.mutate('stage_contexts', key, lambda old: old or {
            'id': key, 'run_id': run_id, 'kind': 'platform_evidence_work_recovery', **identity,
            **origin, 'created_at': now(), 'grants_experiment_authorization': False,
            'scientific_claims_verified': False})
        return all(saved.get(k) == v for k, v in identity.items())
    return _legacy_recovery_allowed(engine, run_id, record)


def _legacy_recovery_allowed(engine, run_id, record):
    key = f'{run_id}:discussion-action-recovery:{scope(engine, run_id)}'
    identity = {'round': record['round'], 'proposal_hash': record['proposal_hash']}
    saved = engine.store.mutate('stage_contexts', key, lambda old: old or {
        'id': key, 'run_id': run_id, 'kind': 'discussion_action_recovery', **identity, 'created_at': now()})
    return all(saved.get(k) == v for k, v in identity.items())


def _unused_legacy_recovery(engine, run_id, receipt):
    """Prove that a v1 reservation postdated completed, non-platform work."""
    if type(receipt.get('round')) is not int:
        return False
    row = engine.store.get('stage_contexts',
        f'{run_id}:discussion-actions:{scope(engine, run_id)}:{receipt["round"]}', {})
    if (row.get('status') != 'completed' or row.get('proposal_hash') != receipt.get('proposal_hash')
            or row.get('platform_blockers') or row.get('platform_evidence_assessment_hash')):
        return False
    if any(a.get('context', {}).get('platform_blockers') for a in row.get('assignments', {}).values()):
        return False
    try:
        work_time = datetime.fromisoformat(row['created_at'])
        receipt_time = datetime.fromisoformat(receipt['created_at'])
        return bool(work_time.tzinfo and receipt_time.tzinfo and work_time < receipt_time)
    except (KeyError, TypeError, ValueError):
        return False


def index(engine, run_id, round_number):
    key = f'{run_id}:discussion-actions:{scope(engine, run_id)}:{round_number}'
    row = engine.store.get('stage_contexts', key, {})
    return copy.deepcopy(row.get('delivery')) if row.get('status') == 'completed' else None


def compact_evidence(snapshot):
    """Keep retrieval handles; do not repack source links, quotations or papers."""
    return {
        'verified_papers': [{k: p[k] for k in ('id', 'title', 'year', 'url') if k in p}
                            for p in snapshot.get('verified_papers', [])],
        'sources': [{k: p[k] for k in ('source_id', 'title', 'text_sha256', 'total_chars', 'pages_total') if k in p}
                    for p in snapshot.get('sources', [])],
        'literature_notes': [{k: p[k] for k in ('id', 'paper_id', 'source_id', 'text_sha256', 'reading_scope') if k in p}
                             for p in snapshot.get('literature_notes', [])],
        'policy': '索引不代表正文已读或结论已验证；按ID读取相关片段，保留未解决缺口。',
    }


def work_schema(objections):
    text = {'type': 'string', 'minLength': 1, 'maxLength': 2400}
    item = {'type': 'object', 'properties': {
        'objection_id': {'enum': [o['id'] for o in objections]},
        'target_field': {'enum': list(MATERIAL_FIELDS)},
        'deliverable': text, 'verification': text,
        'evidence_ids': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 12},
        'remaining_gap': {'type': 'string', 'maxLength': 1400},
    }, 'required': ['objection_id', 'target_field', 'deliverable', 'verification', 'evidence_ids', 'remaining_gap'],
        'additionalProperties': False}
    return {'type': 'object', 'properties': {
        'summary': text, 'recommendation': {'enum': ['revise', 'rethink']},
        'outcomes': {'type': 'array', 'items': item, 'minItems': len(objections), 'maxItems': len(objections)},
    }, 'required': ['summary', 'recommendation', 'outcomes'], 'additionalProperties': False}


COMMON = (
    '当前讨论反复列出相同缺口，先完成属于你的具体工作，再交给总监修订；本次不是再次投票。'
    '逐项回应 original_objections，每个 objection_id 恰好一次。deliverable 必须是已查到的证据、'
    '可直接写进方案的具体参数/公式/决定或明确不可行结论，不能只写“应当补充/需要核验”。'
    'verification 写如何复核你本次交付的内容；remaining_gap 保留实际未完成之处。'
    '不属于你职责的事项明确交给负责角色或后续执行门禁，不得重复要求总监自行调查。'
    'evidence_ids 只写工具已返回的本任务来源、论文或笔记ID，不把设计建议当科学证据。'
    '历史检查点、文献交接目录及记忆摘要仅用于追溯；不能作为论文原文、已读状态或科学结论。'
    '实验尚未获批，禁止执行训练、声称完成基准测量或编造代码提交/数据哈希。'
    '协议必须可执行，但实现代码、运行计时和最终数据校验在设计/实现阶段完成，不应要求它们先于立项共识存在。'
    '当前 execution_constraints 和 hardware.execution_preferences 是执行上限；旧提案的120秒等更小估计不是用户限制，'
    '可在当前上限内提出有理由的资源调整。若无法形成可检验且与近邻有实质差异的方案，推荐 rethink 并给替代方向。'
)
SCOUT = (
    '文献职责：优先检索共享RAG并读取已有笔记；缺方法证据才补检索、下载和按页阅读，调用'
    'literature.record_notes实际保存笔记。聚焦原异议中的最高风险近邻，不重新做宽泛综述。'
    '至少两种机制表述的定向检索要有真实回执；复用已有合格回执时明确来源和日期。'
    '交付路由输入/粒度/预测器共享/步长条件/训练目标/消融的机制对照及可定位证据。'
    '同一论文无法取得正文时尝试身份一致的公开版本，仍不可读则记录缺口与可读替代，不能反复声称补齐。'
)
ENGINEER = (
    '工程职责：结合文献交付，直接选定一个可获取的数据集及官方来源、时间切分、输入窗口、'
    '通道、预测跨度、训练集预处理、三种子、公平基线与消融的固定配置；写清张量形状、'
    '所提机制的训练/推断公式、梯度路径与初始化。用工具核对当前硬件和可用包，给出有理由的资源需求。'
    '把必要的实现审计/校验/计时检查列为实现完成后的执行前门禁；不伪造已生成代码或测量。'
    '模型复杂度、实验规模或时限确实不可行时给出符合当前用户上限的替代配置，而不是循环重复否决。'
)
BUDGET = (
    '本次异议全部是 resource_plan 的执行计数问题，没有待补的论文证据。'
    '先读取 execution_constraints.host_budget_review 与 trial_accounting，按平台已配置的 trial、'
    'runner 尝试、模型调用和研究周期额度，交付可直接写入方案的计数与资源边界。'
    '已明确的额度和计数规则不再要求主控另行确认；不能把单次资源准入当作完整矩阵或实验获批。'
    '区分固定算法内按训练目标进行的参数估计与额外的验证集配置选择，不能只凭内部拟合次数把两者混同。'
    '具体矩阵与完整参数计划有冲突时给出明确修订或保留实际缺口。'
    '本次不用重新检索文献、下载原文或保存重复阅读笔记；科学异议仍交给相应文献工作处理。'
    'evidence_ids 保持为空；verification 引用平台控制事实及可复核的计数计算。'
    '这次交付不投票、不批准实验，不执行训练或编造计时、代码和测量结果。'
)


def resource_only_work(vote):
    """Route only complete, typed resource objections with no paper evidence."""
    changes, fixes = vote.get('required_changes'), vote.get('fixes')
    if not isinstance(changes, list) or not changes or not isinstance(fixes, list) or not fixes:
        return False
    covered = set()
    for fix in fixes:
        if not isinstance(fix, dict):
            return False
        position = fix.get('objection_index')
        if (type(position) is not int or not 0 <= position < len(changes)
                or fix.get('target_field') != 'resource_plan' or fix.get('evidence_ids') != []):
            return False
        covered.add(position)
    return covered == set(range(len(changes)))


PLATFORM_EVIDENCE = (
    '\nplatform_blockers 来自平台的实际证据校验，不是评审员的否决票；原三方评审意见保持原样。'
    '先复用本任务已有原文、有效阅读笔记和共享RAG，逐篇给出可复核的 source_id#page=N 或 chars=start:end。'
    '已有全文不得降成缺失摘要后继续声称有摘要依据；错误证据层级与定位交付明确修正。'
    '缺近期检索回执时调用 literature.survey，search_profile="novelty"，queries 提供至少两种机制表达。'
    '此配置保存真实近期排序、年份范围和不限年代的基线回执；不能把相关性排序改写成已执行近期排序。'
    '未读或不可获取的近邻继续保留未知和缺口，不能冒充已比较或替代原文。'
    '补证据完成后才交给总监整合并独立复审；本次交付不授予共识、阅读信用或实验授权。'
)


INTEGRATE = (
    '\n本轮 discussion_work 是角色已完成的补证据和技术交付，先逐项采用或有理由地拒绝其具体内容。'
    '明确选择数据/切分/参数/算法/统计判据，不把它们继续写成“待锁定”。'
    '区分立项前必须解决的科学异议与实现后执行前的代码、校验值、计时检查。'
    '未取得的文献证据保持未知，不能把专家交付或检索命中当成创新审查通过。'
    '若原方向无法区分近邻，则据交付重新设计机制或明确停止条件；不得仅换题名与叙述。'
    '采用 execution_constraints 和 hardware.execution_preferences 的当前额度重新估算资源，'
    '禁止把旧提案的小预算当作用户硬上限。保留完整PROPOSAL与provenance字段；本轮仍需三方独立评审。'
)
REVIEW = (
    '\n本轮已完成独立补证据/工程交付，见 discussion_work（可按handoff_id查看原始记录）。'
    '逐项核对新交付，已具备可执行规格的事项不再重复索要承诺。严格区分立项前科学/方法门禁'
    '与实现后执行前检查：不能要求尚未获准生成的代码、运行时间或本轮实测结果先于立项存在。'
    '有具体证据支持的高相似整体机制、科学机制重复或不可执行的协议仍须否决。'
    '完成范围内未找到高相似整体机制时可继续立项讨论；未取得的近邻证据保留为未知，不能仅因覆盖不穷尽否决新颖性。使用当前执行上限，'
    '不要沿用旧评审的120秒等过期限制。只能把有依据解决的异议计为进展。'
)


async def deliver(engine, harness, run_id, round_number, context):
    from .engine import ResearchBlocked, digest
    store = engine.store
    key = f'{run_id}:discussion-actions:{scope(engine, run_id)}:{round_number}'
    saved = store.get('stage_contexts', key)
    if saved and saved['proposal_hash'] != context['proposal_hash']:
        raise ResearchBlocked('Discussion work belongs to a different proposal; original delivery retained')
    if (saved and saved.get('platform_evidence_assessment_hash')
            and saved['platform_evidence_assessment_hash'] != digest(context.get('evidence_assessment'))):
        raise ResearchBlocked('Discussion platform evidence assessment changed; original assignment retained')
    if saved and saved.get('status') == 'completed':
        return saved['delivery'], saved.get('effective_constraints', saved['current_constraints']), saved['hardware']
    if not saved:
        run = store.get('runs', run_id)
        from .execution_contract import constraints as current_constraints
        constraints = current_constraints(run['config'], context.get('execution_constraints'))
        saved = {'id': key, 'run_id': run_id, 'kind': 'discussion_actions', 'status': 'working',
            'round': round_number, 'proposal_hash': context['proposal_hash'],
            'current_constraints': constraints, 'hardware': await engine.execution_hardware(run_id),
            'created_at': now(), 'assignments': {}, 'deliveries': []}
        blockers = platform_evidence_work(context)
        if blockers:
            saved.update(platform_blockers=blockers,
                         platform_evidence_assessment_hash=digest(context['evidence_assessment']))
        store.put('stage_contexts', key, saved)
        store.event(run_id, 'discussion.work_started', '暂停重复评议，文献与工程角色先交付补证据和可执行方案。',
                    {'round': round_number, 'work_id': key})
    # A limit change must reach the next unfrozen revision while original paid
    # assignments and completed earlier rounds keep their exact replay identity.
    config = store.get('runs', run_id)['config']
    from .execution_contract import constraints as current_constraints, host_budget_snapshot
    effective = current_constraints(config, saved['current_constraints'])
    effective['host_budget_review'] = host_budget_snapshot(store, run_id, context['proposal'],
        context['proposal_hash'], engine.cycle(run_id))
    if effective != saved.get('effective_constraints', saved['current_constraints']):
        saved['effective_constraints'] = effective
        update = {'previous': copy.deepcopy(saved['current_constraints']), 'current': copy.deepcopy(effective),
            'updated_at': now(), 'scope': '当前配置用于后续未冻结的交付和修订；已付费请求仍按原输入复用，历史额度描述不覆盖当前执行门禁。'}
        saved.setdefault('constraint_updates', []).append(update)
        store.put('stage_contexts', key, saved)
        store.event(run_id, 'discussion.constraints_updated', '讨论后续修订已同步当前执行上限；原请求与历史额度保留。',
                    {'work_id': key, **update})
    effective = saved.get('effective_constraints', saved['current_constraints'])
    objections = previous_review({'round': round_number, 'proposal': context['proposal'], 'votes': context['votes']})['objections']
    for role in ('scout', 'engineer'):
        platform = [o for o in saved.get('platform_blockers', []) if o['assigned_role'] == role]
        owned = [o for o in objections if o['role'] == role] + platform
        if not owned or any(d['role'] == role for d in saved['deliveries']):
            continue
        assignment = saved['assignments'].get(role)
        if not assignment:
            vote = next(v for v in context['votes'] if v['role'] == role)
            budget_only = not platform and resource_only_work(vote)
            child = {k: copy.deepcopy(v) for k, v in context.items()
                if not budget_only and (k.endswith('_protocol') or k.endswith('_index'))}
            child.update(objective=store.get('runs', run_id)['objective'], proposal=context['proposal'],
                proposal_hash=context['proposal_hash'], original_objections=owned,
                evidence=compact_evidence(engine.evidence_snapshot(run_id)),
                source_tool_protocol=1, execution_constraints=effective, hardware=saved['hardware'],
                peer_deliveries=saved['deliveries'], discussion_action_protocol=1)
            if context.get('source_bindings') and not budget_only:
                child['source_bindings'] = copy.deepcopy(context['source_bindings'])
            if budget_only:
                child['discussion_action_kind'] = 'execution_accounting'
            if platform:
                child.update(platform_blockers=copy.deepcopy(platform),
                             platform_evidence_assessment_hash=saved['platform_evidence_assessment_hash'])
            if context.get('novelty_screening'):
                child['novelty_screening'] = copy.deepcopy(context['novelty_screening'])
            purpose = f'{"literature_reading" if role == "scout" and not budget_only else "discussion_action"}:discussion:{round_number}'
            assignment = {'purpose': purpose, 'instruction': COMMON + (BUDGET if budget_only else SCOUT if role == 'scout' else ENGINEER),
                          'context': child, 'schema': work_schema(owned)}
            if platform:
                assignment['instruction'] += PLATFORM_EVIDENCE
            if child.get('novelty_screening') and not budget_only:
                assignment['instruction'] += '\n' + child['novelty_screening']['instruction']
            saved['assignments'][role] = assignment
            store.put('stage_contexts', key, saved)
        if (assignment['context'].get('source_tool_protocol') == 1
                and assignment['context'].get('discussion_action_kind') != 'execution_accounting'
                and config.get('context_compiler_version') in (1, 2, 3)):
            # Deliver navigation before new work. Existing paid assignments keep
            # their exact receipts and prepared inputs, even after a restart.
            manifest = next((assignment['context'][name] for name in (
                'required_literature_index', 'related_work_index', 'literature_catalog_index',
                'literature_index', 'idea_literature_index') if name in assignment['context']), None)
            if manifest is not None:
                from .innovation_review import seed_reading
                await seed_reading(engine, harness, run_id, assignment['purpose'], assignment['instruction'],
                    assignment['context'], assignment['schema'], roles=(role,), index=manifest, unstarted_only=True)
        result = await harness.ask(run_id, role, assignment['purpose'], assignment['instruction'], assignment['context'], assignment['schema'])
        if sorted(o['objection_id'] for o in result['outcomes']) != sorted(o['id'] for o in owned):
            raise ResearchBlocked('Discussion delivery omitted or duplicated an assigned objection')
        snapshot = engine.evidence_snapshot(run_id)
        handoff_id = harness.decision_key(run_id, role, assignment['purpose'], assignment['instruction'], assignment['context'], assignment['schema'])
        from .discussion_evidence import DiscussionEvidenceError, resolve_delivery
        try:
            derived, audit = resolve_delivery(engine, run_id, handoff_id, result, snapshot)
        except DiscussionEvidenceError as exc:
            saved['evidence_issue'] = {'role': role, 'handoff_id': handoff_id, 'invalid_ids': exc.identifiers,
                'snapshot_hash': snapshot.get('snapshot_hash'), 'checked_at': now()}
            store.put('stage_contexts', key, saved)
            raise ResearchBlocked(str(exc)) from exc
        saved.pop('evidence_issue', None)
        saved['deliveries'].append({'role': role, 'handoff_id': handoff_id, **derived,
            'evidence_resolution': audit})
        store.put('stage_contexts', key, saved)
        if audit['record_references']:
            store.event(run_id, 'discussion.records_resolved',
                '已复核历史记录的读取回执并与原文证据分开；复用原回复，未新增模型请求或阅读信用。', audit)
        store.event(run_id, 'discussion.work_delivered', result['summary'], {'role': role, 'round': round_number, 'handoff_id': handoff_id})
    delivery = {'work_id': key, 'round': round_number, 'deliveries': saved['deliveries'],
        'scope': '角色交付是待独立审查的修订材料，不是已批准或已验证的结论。'}
    if saved.get('platform_blockers'):
        delivery.update(platform_blockers=copy.deepcopy(saved['platform_blockers']),
                        platform_evidence_assessment_hash=saved['platform_evidence_assessment_hash'])
    if saved.get('constraint_updates'):
        delivery.update(execution_constraints=copy.deepcopy(effective),
                        constraint_updates=copy.deepcopy(saved['constraint_updates']))
    saved.update(status='completed', delivery=delivery, completed_at=now(), delivery_hash=digest(delivery))
    store.put('stage_contexts', key, saved)
    return delivery, effective, saved['hardware']
