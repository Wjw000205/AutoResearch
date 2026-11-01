"""Bounded, independently authenticated per-unit supplement review receipts.

Original paid reports remain immutable. A unit packet is its own handoff, not
an invented rewrite of that report. Unknown is retained after one scoped pass.
"""
from __future__ import annotations

import copy

from .engine import ResearchBlocked, digest
from . import implementation_acceptance_flow as flow


def _header(engine, run_id, packet, context, role, units):
    from . import implementation_source_supplement as review
    bound = flow.check_packet(engine, run_id, packet)
    result = packet['result']
    if (bound != context or packet['role'] != role
            or result['supplement_sha256'] != context['source_supplement']['document']['sha256']
            or len(result['checks']) != len(units)
            or {c['id'] for c in result['checks']} != set(units)):
        raise ResearchBlocked('补充逐项审核的角色、版本、范围或冻结输入不一致。')
    if role not in review.ROLES:
        raise ResearchBlocked('补充逐项审核角色越界。')


def pending(engine, run_id, original, context, role):
    from . import implementation_source_supplement as review
    if original is None:
        return list(review.UNITS)
    _header(engine, run_id, original, context, role, review.UNITS)
    units = []
    for item in original['result']['checks']:
        try:
            review.validate_item(engine, run_id, original, context['source_supplement']['document'], context, item)
        except ResearchBlocked:
            units.append(item['id'])
        else:
            if item['status'] == 'unknown':
                units.append(item['id'])
    return [u for u in review.UNITS if u in units]


def unit_context(context, unit, original):
    value = copy.deepcopy(context)
    value['source_supplement']['units'] = [unit]
    value['source_supplement_unit_review'] = {
        'version': 1, 'unit': unit, 'one_scoped_pass': True,
        'original_packet_sha256': digest(original) if original else None,
        'original_handoff_id': original['handoff_id'] if original else None,
        'previous_check': next((copy.deepcopy(c) for c in original['result']['checks']
                                if c['id'] == unit), None) if original else None,
        'instruction': '仅核对本条，读取对应候选和原依据后立即提交结论。归档定位不是正文；需要时只补读本条精确范围。仍缺真实来源则如实 unknown，不扩读其他条目。'}
    return value


def validate_unit(engine, run_id, packet, context, role, unit, original):
    from . import implementation_source_supplement as review
    expected = unit_context(context, unit, original)
    _header(engine, run_id, packet, expected, role, [unit])
    review.validate_item(engine, run_id, packet, context['source_supplement']['document'], expected,
                         packet['result']['checks'][0])


def aggregate(engine, run_id, context, role, original, packets, *, complete):
    from . import implementation_source_supplement as review
    todo = pending(engine, run_id, original, context, role)
    if not isinstance(packets, dict) or set(packets) - set(todo):
        raise ResearchBlocked('补充逐项审核回执包含重复或范围外条目。')
    for unit, packet in packets.items():
        validate_unit(engine, run_id, packet, context, role, unit, original)
    if complete and set(packets) != set(todo):
        raise ResearchBlocked('补充逐项审核尚未全部完成。')
    checks = {c['id']: copy.deepcopy(c) for c in original['result']['checks']} if original else {}
    checks.update({unit: copy.deepcopy(packet['result']['checks'][0]) for unit, packet in packets.items()})
    return {'supplement_sha256': context['source_supplement']['document']['sha256'],
            'checks': [checks[u] for u in review.UNITS if u in checks],
            'summary': '逐项独立审核回执汇总；原始报告和各项付费答复分别保留，不代表实现稿批准。'}


def validate_vote(engine, run_id, vote, context, role=None):
    if (vote.get('unit_review_protocol') != 1 or vote.get('context') != context
            or (role and vote.get('role') != role)):
        raise ResearchBlocked('补充逐项审核汇总身份不一致。')
    result = aggregate(engine, run_id, context, vote['role'], vote.get('original_packet'),
                       vote['unit_packets'], complete=True)
    if vote['result'] != result:
        raise ResearchBlocked('补充逐项审核汇总与真实付费回执不一致。')


async def review_role(engine, h, run_id, state, record, role, instruction):
    from . import implementation_source_supplement as review
    from .codex_workflow import _frozen_spec_assignment
    context = record['payload']['context']
    purpose = 'implementation_source_supplement:' + record['sha256'] + ':' + role
    checkpoint = record.get('unit_reviews', {}).get(role)
    if checkpoint is None:
        # Finish/reuse a dispatched old all-unit request exactly once. New roles
        # start with individual packets so source text need not survive 11 units.
        frozen = _frozen_spec_assignment(engine, run_id, purpose, role)
        original = (await flow.ask(engine, h, run_id, state, purpose, role, instruction,
                                  copy.deepcopy(context), review.SCHEMA)) if frozen else None
        todo = pending(engine, run_id, original, context, role)
        checkpoint = {'original_packet': original, 'pending_units': todo, 'packets': {}}
    original, packets = checkpoint['original_packet'], checkpoint['packets']
    todo = pending(engine, run_id, original, context, role)
    if checkpoint['pending_units'] != todo:
        raise ResearchBlocked('补充逐项审核续接范围已改变。')
    aggregate(engine, run_id, context, role, original, packets, complete=False)

    def save():
        engine.check_active(run_id)
        current = engine.store.get(review.COLLECTION, record['id'])
        review.checked(engine, run_id, current, state)
        current = {**current, 'unit_reviews': {**current.get('unit_reviews', {}), role: copy.deepcopy(checkpoint)}}
        engine.store.put(review.COLLECTION, current['id'], current)

    save()
    for unit in todo:
        if unit in packets:
            continue
        schema = copy.deepcopy(review.SCHEMA)
        schema['properties']['checks']['minItems'] = schema['properties']['checks']['maxItems'] = 1
        schema['properties']['checks']['items']['properties']['id'] = {'enum': [unit]}
        scoped_instruction = (instruction.replace('逐项核对H01/H02/H03/H04/D01/D02/G01/G02/G03/G04/G05。',
                                                   '仅核对' + unit + '。') + '\n本交接仅审核 ' + unit + '，只返回这一项。'
            '其他项的已保存结论由宿主复用，不重读或重审。完成本条必要读取后立即输出结论，'
            '不要先遍历其他条目。上一报告的证据位置仅作导航，本次结论须有本交接真实交付正文。'
            '本条仍有科学缺口时如实记录 unknown/revise；这次答复后不自动循环同一条。')
        packet = await flow.ask(engine, h, run_id, state,
            purpose + ':unit:' + unit + ':' + digest(original), role, scoped_instruction,
            unit_context(context, unit, original), schema)
        validate_unit(engine, run_id, packet, context, role, unit, original)
        packets[unit] = packet
        save()  # Successful units survive a later unit failure or restart.
    return {'unit_review_protocol': 1, 'role': role, 'context': copy.deepcopy(context),
            'original_packet': original, 'unit_packets': copy.deepcopy(packets),
            'result': aggregate(engine, run_id, context, role, original, packets, complete=True)}
