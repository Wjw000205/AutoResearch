"""Bounded intake of findings first discovered during document adjudication.

Original decisions and frozen inputs remain immutable. New findings need a
complete issue, real reads, an author response and their own adjudication.
"""
from __future__ import annotations

import copy

from . import implementation_acceptance as policy
from . import implementation_acceptance_flow as flow
from .implementation_review_routing import digest

KEY = 'implementation_adjudication_intake'
INSTRUCTION = (
    '根据冻结合同及工程师一次回应逐项裁决。decisions 必须恰好覆盖 proposed_issues 的 issue_id，'
    '每个一次，禁止使用 local_id 或在 decisions 中自行新增编号。P1 须违反必要项，P2 进入 DEFERRED。'
    '重复指向同等或更高等级开放原问题；误报 REJECTED 须给原文反证。已确认 P0/P1 不能风险豁免。'
    '高影响证据疑点转 NEEDS_PROBE，并提供输入、步骤、预算、通过/失败判据和产物要求的 probe_plan。'
    '关键假设或合同失效转 NEEDS_REPLAN。不能以裁决代替运行验证。'
    '多个可行方案的选择写入 decision_records，记录候选、原因、适用条件和允许重开条件。'
    '若输出合同含 additional_issues，新发现的独立缺陷必须在那里完整登记，不能直接裁决；无新增返回空数组。'
    '宿主最多安排一次补充登记、作者回应与裁决；补充裁决不得再新增问题。'
)


def schema(ids, *, supplemental=False):
    value = copy.deepcopy(policy.ADJUDICATION_SCHEMA)
    decisions = value['properties']['decisions']
    decisions.update(minItems=len(ids), maxItems=len(ids))
    decisions['items']['properties']['issue_id'] = {'enum': list(ids)}
    if not supplemental:
        value['properties']['additional_issues'] = policy.array(policy.ISSUE)
        value['required'].append('additional_issues')
    return value


def split(ledger, packet, ids):
    """Never interpret an unknown decision as an admitted issue or drop it."""
    decisions = packet['result']['decisions']
    received = [d['issue_id'] for d in decisions]
    if len(set(ids)) != len(ids) or len(set(received)) != len(received) or not set(ids) <= set(received):
        policy.fail('总监裁决存在漏项或重复编号，不能登记或跳过问题。')
    known = [d for d in decisions if d['issue_id'] in ids]
    extra = [d for d in decisions if d['issue_id'] not in ids]
    if extra and ('additional_issues' in packet['result'] or any(
            d['issue_id'] in ledger['issues'] or d['action'] != 'OPEN'
            or d['duplicate_of'] is not None or d.get('probe_plan') is not None for d in extra)):
        policy.fail('未登记裁决只能作为新的 OPEN 候选问题补充登记，不能引用其他轮次或授予结论。')
    return known, extra


def validate_binding(engine, run_id, state, row, packet, ids):
    context = flow.check_packet(engine, run_id, packet)
    proposed = context.get('proposed_issues', [])
    if (context.get('acceptance_workflow_id') != state['id']
            or context.get('acceptance_contract') != state[policy.FIELD]['contract']
            or len(proposed) != len(ids) or {i['issue_id'] for i in proposed} != set(ids)
            or context.get('implementation_document_delivery', {}).get('current_document', {}).get('document_sha256')
            != row['document']['sha256']):
        policy.fail('裁决或回应没有绑定本轮合同、文档和候选问题。')
    for issue in proposed:
        expected = state[policy.FIELD]['issues'][issue['issue_id']]
        if any(issue.get(k) != expected.get(k) for k in (
                'criterion_id', 'claim', 'close_when', 'evidence', 'source_handoff_id', 'document_version')):
            policy.fail('裁决或回应改变了原候选问题。')
    return context


def apply_response(engine, run_id, state, row, packet, ids):
    context = validate_binding(engine, run_id, state, row, packet, ids)
    if packet['role'] != 'engineer':
        policy.fail('候选问题必须由工程师回应。')
    responses = packet['result']['responses']
    if len(responses) != len(ids) or {r['issue_id'] for r in responses} != set(ids):
        policy.fail('结构化回应必须覆盖全部候选问题。')
    ledger = state[policy.FIELD]
    for item in responses:
        flow.validate_refs(engine, run_id, packet['handoff_id'], context, item['evidence'], current=row['document'])
        if ledger['issues'][item['issue_id']]['dispute_exchanges'] >= ledger['budget']['max_dispute_exchanges_per_issue']:
            policy.fail('同一问题的结构化交锋预算已耗尽。')
    for item in responses:
        ledger['issues'][item['issue_id']]['dispute_exchanges'] += 1


def validate_decisions(engine, run_id, state, row, packet, ids):
    context = validate_binding(engine, run_id, state, row, packet, ids)
    if packet['role'] != 'director':
        policy.fail('准入裁决必须由总监交付。')
    for decision in packet['result']['decisions']:
        flow.validate_refs(engine, run_id, packet['handoff_id'], context, decision['evidence'], current=row['document'])
        if decision.get('probe_plan'):
            flow.validate_refs(engine, run_id, packet['handoff_id'], context, decision['probe_plan']['input_refs'])


def intake_context(packet, extra):
    return {'version': 1, 'source_handoff_id': packet['handoff_id'],
        'source_result_sha256': digest(packet['result']), 'original_extra_decisions': copy.deepcopy(extra),
        'attempt': 1, 'attempt_limit': 1}


def intake_schema(extra):
    item = copy.deepcopy(policy.ISSUE)
    item['properties']['reopen_issue_id'] = {'type': 'null'}
    item['properties']['needs'] = {'const': 'REPAIR'}
    item['oneOf'] = [{'properties': {'local_id': {'const': d['issue_id']},
        'claim': {'const': d['reason']}, 'evidence': {'const': d['evidence']}}} for d in extra]
    return policy.obj({'issues': policy.array(item, len(extra), len(extra))})


def findings(engine, run_id, state, row, packet, ids, intake):
    _, extra = split(state[policy.FIELD], packet, ids)
    source = packet
    if extra:
        if not intake:
            policy.fail('新增裁决缺少完整问题登记，原始意见必须保留。')
        context = validate_binding(engine, run_id, state, row, intake, ids)
        if intake['role'] != 'director' or context.get(KEY) != intake_context(packet, extra):
            policy.fail('补充登记没有绑定原总监裁决。')
        issues = intake['result']['issues']
        by_id = {i['local_id']: i for i in issues}
        if len(by_id) != len(issues) or set(by_id) != {d['issue_id'] for d in extra}:
            policy.fail('补充登记必须唯一保留所有新增问题。')
        for item in extra:
            issue = by_id[item['issue_id']]
            if (issue['claim'] != item['reason'] or issue['evidence'] != item['evidence']
                    or issue['reopen_issue_id'] is not None or issue['needs'] != 'REPAIR'):
                policy.fail('补充登记不能删除或改变原新增缺陷及其证据。')
        source = intake
    else:
        if intake:
            policy.fail('没有未登记裁决时不能附加补充恢复交接。')
        issues = packet['result'].get('additional_issues', [])
    for issue in issues:
        flow.validate_refs(engine, run_id, source['handoff_id'], source['context'], issue['evidence'], current=row['document'])
    return issues, source


def apply_primary(engine, run_id, state, row, slot, packet, intake=None):
    ledger = state[policy.FIELD]
    ids = slot['proposed_ids']
    validate_decisions(engine, run_id, state, row, packet, ids)
    known, _ = split(ledger, packet, ids)
    issues, source = findings(engine, run_id, state, row, packet, ids, intake)
    policy.adjudicate(ledger, known, ids, packet['handoff_id'])
    flow.record_decisions(engine, run_id, ledger, packet)
    if issues:
        added = policy.propose(ledger, state['id'] + ':adjudication', row, 'director', {'issues': issues}, source['handoff_id'])
        slot['supplemental'] = {'proposed_ids': added, 'intake': copy.deepcopy(intake)}
    slot['adjudication'] = copy.deepcopy(packet)


def context_for(state, row, common, ids):
    ledger = state[policy.FIELD]
    context = flow.base_context(state, [*common['indexed_documents'], row['document']], row)
    context.update(acceptance_contract=copy.deepcopy(ledger['contract']),
        proposed_issues=[copy.deepcopy(ledger['issues'][iid]) for iid in ids],
        existing_issues=copy.deepcopy(list(ledger['issues'].values())),
        decision_records=copy.deepcopy(ledger['decision_records']))
    return context


async def respond(engine, h, run_id, state, row, common, persist, *, supplemental=False):
    slot = state[policy.FIELD]['rounds'][-1]
    target = slot['supplemental'] if supplemental else slot
    if target.get('author_response'):
        return
    ids = target['proposed_ids']
    context = context_for(state, row, common, ids)
    suffix = ':supplemental' if supplemental else ''
    response = await flow.ask(engine, h, run_id, state,
        f'implementation_acceptance_response:{digest(state["id"])}:{row["index"]}' + suffix, 'engineer',
        '对每个 proposed_issues 用原文证据回应一次。接受缺陷或提交反证；不能修改文档、关闭问题或扩大范围。',
        context, policy.obj({'responses': policy.array(policy.obj({'issue_id': {'enum': ids},
            'response': policy.TEXT, 'evidence': policy.REFS}), len(ids), len(ids))}))
    updated = copy.deepcopy(state)
    apply_response(engine, run_id, updated, row, response, ids)
    target = updated[policy.FIELD]['rounds'][-1]
    if supplemental:
        target = target['supplemental']
    target['author_response'] = response
    state[policy.FIELD] = updated[policy.FIELD]
    persist()


async def adjudicate(engine, h, run_id, state, row, common, persist):
    slot = state[policy.FIELD]['rounds'][-1]
    if not slot['proposed_ids']:
        return
    if not slot.get('adjudication'):
        state[policy.FIELD]['phase'] = 'ADJUDICATE'
        await respond(engine, h, run_id, state, row, common, persist)
        slot = state[policy.FIELD]['rounds'][-1]
        ids = slot['proposed_ids']
        context = context_for(state, row, common, ids)
        context['author_response'] = slot['author_response']['result']
        purpose = f'implementation_acceptance_adjudication:{digest(state["id"])}:{row["index"]}'
        packet = await flow.ask(engine, h, run_id, state, purpose, 'director', INSTRUCTION, context, schema(ids))
        validate_decisions(engine, run_id, state, row, packet, ids)
        _, extra = split(state[policy.FIELD], packet, ids)
        intake = None
        if extra:
            context = copy.deepcopy(packet['context'])
            context[KEY] = intake_context(packet, extra)
            intake = await flow.ask(engine, h, run_id, state, purpose + ':intake', 'director',
                '原裁决完整保留，但其中新增编号尚未登记。仅对 original_extra_decisions 补齐标准问题结构，'
                '每项 local_id 原样使用 issue_id，claim 原样保留 reason，evidence 原样保留并通过 documents.read 真实读取。'
                '按冻结合同给出 criterion_id、severity、impact、minimal_fix 和可核验 close_when。'
                'needs=REPAIR、reopen_issue_id=null；不得遗漏、新增或改写原问题，不得修改原裁决。'
                '这是一次补充登记，随后仍须工程师回应和正式裁决，不能授权实现或实验。',
                context, intake_schema(extra))
        updated = copy.deepcopy(state)
        apply_primary(engine, run_id, updated, row, updated[policy.FIELD]['rounds'][-1], packet, intake)
        state[policy.FIELD] = updated[policy.FIELD]
        persist()
    slot = state[policy.FIELD]['rounds'][-1]
    if slot.get('supplemental') and not slot['supplemental'].get('adjudication'):
        await respond(engine, h, run_id, state, row, common, persist, supplemental=True)
        slot = state[policy.FIELD]['rounds'][-1]
        supplemental = slot['supplemental']
        ids = supplemental['proposed_ids']
        context = context_for(state, row, common, ids)
        context['author_response'] = supplemental['author_response']['result']
        packet = await flow.ask(engine, h, run_id, state,
            f'implementation_acceptance_adjudication:{digest(state["id"])}:{row["index"]}:supplemental',
            'director', INSTRUCTION, context, schema(ids, supplemental=True))
        updated = copy.deepcopy(state)
        apply_supplemental(engine, run_id, updated, row, updated[policy.FIELD]['rounds'][-1], packet)
        state[policy.FIELD] = updated[policy.FIELD]
        persist()


def apply_supplemental(engine, run_id, state, row, slot, packet):
    ids = slot['supplemental']['proposed_ids']
    validate_decisions(engine, run_id, state, row, packet, ids)
    # No recursive discovery or silent filtering in the bounded second batch.
    policy.adjudicate(state[policy.FIELD], packet['result']['decisions'], ids, packet['handoff_id'])
    if packet['result'].get('additional_issues'):
        policy.fail('补充裁决不能递归新增问题。')
    flow.record_decisions(engine, run_id, state[policy.FIELD], packet)
    slot['supplemental']['adjudication'] = copy.deepcopy(packet)


def replay(engine, run_id, state, row, saved, ledger):
    """Rebuild both batches from authenticated original deliveries at release."""
    replay_state = {**state, policy.FIELD: ledger}
    slot = {'proposed_ids': saved['proposed_ids']}
    apply_response(engine, run_id, replay_state, row, saved['author_response'], slot['proposed_ids'])
    apply_primary(engine, run_id, replay_state, row, slot, saved['adjudication'],
                  (saved.get('supplemental') or {}).get('intake'))
    if bool(slot.get('supplemental')) != bool(saved.get('supplemental')):
        policy.fail('补充问题登记无法由原裁决重建。')
    if slot.get('supplemental'):
        original = saved['supplemental']
        ids = slot['supplemental']['proposed_ids']
        if ids != original['proposed_ids'] or not original.get('author_response') or not original.get('adjudication'):
            policy.fail('补充问题必须完成真实回应和裁决，不能删除或跳过。')
        apply_response(engine, run_id, replay_state, row, original['author_response'], ids)
        apply_supplemental(engine, run_id, replay_state, row, slot, original['adjudication'])
