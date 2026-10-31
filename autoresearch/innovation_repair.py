"""Bounded evidence and design work between rejected innovation attempts.

The deliveries are attributed hypotheses, not votes. Original paid assignments,
rejections and budgets survive recovery; every new candidate is reviewed again.
"""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path

from .storage import now

STAGE_RULES = (
    '\n选题阶段边界：当前只决定是否值得进入第二轮定向全文核查，不是认证CCF-A成果。'
    '有文献依据的重要科学问题不必先有用户访谈或本项目实测失败；可证伪的明确机制不必先被实验验证。'
    '必须有具体问题、推导或机制、强竞争解释与区分实验；组合创新和架构改进应说明对痛点的实质作用，'
    '不要求从0到1；仅“检查是否有问题”、无明确作用的堆模块或重新命名仍不合格。'
    'not_repackaged检查已有证据是否显示实质重复，不能因第二轮近邻检索尚未发生就循环否决全部候选；'
    '未知的近邻覆盖范围须明确交第二轮核查，完整贡献已被同构机制覆盖且没有实质增量时才以重复否决。'
    '候选之间相似要明确去重，但不能仅因两个作者独立提出相似方向就把两者的科学价值全部判零。'
    'resource_feasible按当前用户配置检查能否设计有辨识力的实验；代码、数据哈希和实测计时属于后续执行门禁。'
    '不得要求尚未获批的实验、3%涨点或完整实现先于选题存在；这些边界不免除真实科学缺陷。'
)
CANDIDATE = (
    STAGE_RULES + '\n若提供innovation_repair_index，先读取角色补证据/可行性工作，再提出候选。'
    '逐项回应退回原因：证据缺口用已读取的结果解决，设计缺陷作实质修改，已证伪或已重复的方向放弃。'
    '不是每个补证据请求都要求换一个题目；不能在同一小批论文上反复提出未经核查的新风险。'
    '明确提出解决痛点的方案、作用依据和与当前相似工作的比较计划，不限定方法类型，不只提交未来可能设计方法的诊断计划。'
)
COMPARE = STAGE_RULES + (
    '\n每个候选填写blocker_kind：ready仅用于全部qualification为true；'
    'scientific_flaw表示已识别的科学/机制缺陷或重复；evidence_gap表示可以通过定向阅读补齐；'
    'design_gap表示需要作者具体化机制/实验。concern给依据，suggested_test给下一项可执行工作。'
    '只有全部标准均得到三方明确认可才能进入聚焦核查，不能因为已到最后一轮就通过。'
)


def enabled(run):
    return run.get('config', {}).get('innovation_constructive_protocol') == 1


def comparison_schema(base):
    schema = copy.deepcopy(base)
    item = schema['properties']['comparisons']['items']
    item['properties']['blocker_kind'] = {'enum': ['ready', 'scientific_flaw', 'evidence_gap', 'design_gap']}
    item['required'].append('blocker_kind')
    return schema


def work_schema():
    text = {'type': 'string', 'minLength': 1, 'maxLength': 2400}
    return {'type': 'object', 'additionalProperties': False, 'properties': {
        'summary': text, 'outcomes': {'type': 'array', 'minItems': 3, 'maxItems': 3,
            'items': {'type': 'object', 'additionalProperties': False, 'properties': {
                'candidate_id': {'enum': ['director', 'engineer', 'scout']},
                'finding': text, 'remaining_gap': text,
                'recommendation': {'enum': ['revise', 'rethink', 'focused_reading']},
                'evidence_ids': {'type': 'array', 'maxItems': 12, 'items': {'type': 'string'}}},
                'required': ['candidate_id', 'finding', 'remaining_gap', 'recommendation', 'evidence_ids']}}},
        'required': ['summary', 'outcomes']}


def reference_bindings(engine, run_id, handoff_id, result, evidence):
    """Resolve only saved, actually delivered handles; never upgrade a claim.

    PDF aliases and index artifacts are valid references to inspected material,
    but neither is proof of a scientific conclusion. Keep their distinct kinds.
    """
    from .engine import ResearchBlocked
    from .pdf_tools import citation_alias
    store = engine.store
    allowed = {p['id'] for p in evidence['verified_papers']} | {p['source_id'] for p in evidence['sources']} | {p['id'] for p in evidence['literature_notes']}
    progress = store.get('handoff_progress', handoff_id, {})
    receipts = progress.get('tool_results', []) if progress.get('run_id') == run_id else []
    citations = store.list('pdf_citations', run_id)
    bindings = []
    for ref in sorted({ref for row in result['outcomes'] for ref in row['evidence_ids']} - allowed):
        artifact = store.get('artifacts', ref)
        if artifact and artifact.get('run_id') == run_id:
            path = Path(artifact['path']).resolve()
            if path.is_relative_to(engine.run_dir(run_id).resolve()) and path.is_file():
                content = path.read_text(encoding='utf-8')
                sha = hashlib.sha256(content.encode('utf-8')).hexdigest()
                if any(t.get('call', {}).get('name') == 'artifacts.read'
                       and t.get('call', {}).get('arguments', {}).get('id') == ref
                       and t.get('result', {}).get('sha256') == sha
                       and not t.get('result', {}).get('error') for t in receipts):
                    bindings.append({'reference': ref, 'kind': 'inspected_context_index',
                                     'artifact_id': ref, 'sha256': sha, 'scientific_claims_verified': False})
                    continue
        matches = [c for c in citations if ref in (c['id'], citation_alias(c))]
        if len(matches) == 1:
            citation = matches[0]
            inspected = any(t.get('call', {}).get('name') == 'pdf.read'
                and t.get('result', {}).get('source_id') == citation['source_id']
                and t.get('result', {}).get('text_sha256') == citation['text_sha256']
                and not t.get('result', {}).get('error')
                and any(c.get('canonical_citation_id') == citation['id'] for c in t.get('result', {}).get('citations', []))
                for t in receipts)
            if inspected and citation['source_id'] in allowed:
                document = engine.sources.document(run_id, citation['source_id'])
                start, end = citation['start'], citation['end']
                if (document['text_sha256'] == citation['text_sha256']
                    and document['content_sha256'] == citation['content_sha256']
                    and 0 <= start < end <= len(document['text'])
                    and document['text'][start:end] == citation['quote']
                    and citation['locator'] == f'{citation["source_id"]}#chars={start}:{end}'):
                    bindings.append({'reference': ref, 'kind': 'inspected_pdf_excerpt',
                                     'citation_id': citation['id'], 'source_id': citation['source_id'],
                                     'text_sha256': citation['text_sha256'], 'locator': citation['locator'],
                                     'scientific_claims_verified': False})
                    continue
        raise ResearchBlocked(f'创新补充工作引用了未保存的证据或无法核验的索引：{ref}；原回复已保留')
    return bindings


async def prepare(engine, harness, run_id, session, common):
    """One durable scout delivery and one engineering delivery per rejection."""
    from .discussion_actions import compact_evidence
    from .engine import ResearchBlocked
    from .literature_rounds import persist_index
    store = engine.store
    source_attempt = session.get('repair_source_attempt')
    if source_attempt is None:
        return None
    key = f'{session["id"]}:innovation-repair:{source_attempt}'
    saved = store.get('stage_contexts', key)
    if saved and saved.get('status') == 'completed':
        return saved['index']
    if not saved:
        saved = {'id': key, 'run_id': run_id, 'kind': 'innovation_repair', 'status': 'working',
            'source_attempt': source_attempt, 'assignments': {}, 'deliveries': [], 'created_at': now()}
        store.put('stage_contexts', key, saved)
        store.event(run_id, 'innovation.repair_started', '先补齐退回意见中的文献证据和机制规格，再提出候选。',
                    {'source_attempt': source_attempt})
    for role in ('scout', 'engineer'):
        if any(row['role'] == role for row in saved['deliveries']):
            continue
        assignment = saved['assignments'].get(role)
        if not assignment:
            context = {k: copy.deepcopy(v) for k, v in common.items() if k.endswith('_protocol') or k.endswith('_index')}
            context.update(objective=store.get('runs', run_id)['objective'],
                rejection_index=session['rejection_index'],
                evidence=compact_evidence(engine.evidence_snapshot(run_id)),
                execution_constraints=common.get('execution_constraints', {}),
                hardware=common.get('hardware', {}), source_tool_protocol=1,
                peer_work_indexes=saved.get('delivery_indexes', []))
            instruction = ('先通过artifacts.read完整读取rejection_index和idea_literature_index；'
                '本次是补齐具体工作，不是投票或要求他人以后补齐。对最近一轮三个candidate_id各给一项outcome。'
                'finding记录已实际完成的工作或可直接采用的具体设计；remaining_gap如实保留未解决部分。'
                'evidence_ids只引用本任务真实来源/笔记/论文ID；不编造实验，不声称创新已证明。' + STAGE_RULES)
            if role == 'scout':
                instruction += ('先检索持久RAG，围绕异议中最高风险近邻读取方法、实验、局限和附录；'
                    '已有PDF用pdf.read或sources.read继续查看关键章节，必要时定向网络检索并下载。'
                    '实际调用literature.record_notes保存笔记，避免重复宽泛综述。保留CCF A/B和当前第一轮近期范围；'
                    '经典近邻的完整检索交后续第二轮。区分原文已有机制、原文未讨论和当前无法判断；'
                    '不能把未找到当作不存在。不能用再次描述缺少全文代替实际调用阅读工具。')
            else:
                instruction += ('通过artifacts.read读取peer_work_indexes中的文献交付，逐个给出具体方案/作用依据、当前最相似工作的解法及竞争解释、'
                    '区分实验及当前预算内的最小配置。明确数据官方入口与可用条件，未知许可保留待核验。'
                    '不要执行训练或用未执行实验支持机制；无法提出实质方案时明确rethink及不同问题空间。')
            assignment = {'purpose': f'{"literature_reading" if role == "scout" else "discussion_action"}:innovation-repair:{engine.cycle(run_id)}:{source_attempt}',
                'instruction': instruction, 'context': context, 'schema': work_schema()}
            saved['assignments'][role] = assignment
            store.put('stage_contexts', key, saved)
        result = await harness.ask(run_id, role, **assignment)
        if sorted(o['candidate_id'] for o in result['outcomes']) != ['director', 'engineer', 'scout']:
            raise ResearchBlocked('创新补充工作未逐项覆盖三个候选；已保留交付记录')
        evidence = compact_evidence(engine.evidence_snapshot(run_id))
        handoff_id = harness.decision_key(run_id, role, **assignment)
        bindings = reference_bindings(engine, run_id, handoff_id, result, evidence)
        delivery = {'role': role, 'handoff_id': handoff_id, **result, 'reference_bindings': bindings}
        delivery_index = persist_index(engine, harness.intelligence, run_id,
            f'innovation-repair-{engine.cycle(run_id)}-{source_attempt}-{role}', delivery)
        saved['deliveries'].append(delivery)
        saved.setdefault('delivery_indexes', []).append(delivery_index)
        store.put('stage_contexts', key, saved)
        store.event(run_id, 'innovation.repair_delivered', result['summary'], {'role': role, 'handoff_id': handoff_id})
    payload = {'deliveries': saved['deliveries'], 'evidence': compact_evidence(engine.evidence_snapshot(run_id)),
        'scope': '补充材料供三方独立提案与审查，不构成科学结论或实验授权。'}
    index = persist_index(engine, harness.intelligence, run_id, f'innovation-repair-{engine.cycle(run_id)}-{source_attempt}', payload)
    store.put('stage_contexts', key, {**saved, 'status': 'completed', 'index': index, 'completed_at': now()})
    return index


def upgrade_pending(engine, intelligence, run_id):
    """One maintenance recovery for an exhausted, unapproved v1 selection."""
    from .engine import ResearchBlocked
    from .innovation_quality import feedback
    from .literature_rounds import persist_index
    store = engine.store
    run = store.get('runs', run_id)
    if enabled(run):
        return run
    session = store.get('innovation_sessions', f'{run_id}:{engine.cycle(run_id)}') or {}
    if (run['status'] not in {'blocked', 'paused'} or run['stage'] != 'innovation'
            or session.get('status') != 'rejected_quality'
            or store.get('consensus', run_id, {}).get('unanimous') or store.list('experiments', run_id)):
        raise ResearchBlocked('只能恢复尚未选定方向、无实验授权的旧版创新审查')
    index = persist_index(engine, intelligence, run_id, f'quality-repair-upgrade-{engine.cycle(run_id)}', feedback(store, run_id, session))
    fresh = {**session, 'attempt': session['attempt'] + 1, 'status': 'rethinking',
        'repair_source_attempt': session['attempt'], 'rejection_index': index}
    for field in ('candidates', 'comparisons', 'quality_gate', 'candidate_handoffs', 'comparison_handoffs'):
        fresh.pop(field, None)
    # Keep the exhausted bound: one repaired extra attempt, never three new retries.
    updated = {**run, 'config': {**run['config'], 'innovation_constructive_protocol': 1}, 'error': None, 'updated_at': now()}
    store.batch([('runs', run_id, updated), ('innovation_sessions', session['id'], fresh)],
        deletes=[('steps', f'{run_id}:innovation'), ('stage_contexts', f'{run_id}:innovation')])
    store.event(run_id, 'innovation.repair_upgraded', '保留原三轮否决，补文献与机制设计后进行一次新候选审查；预算和实验门禁不变。')
    return updated
