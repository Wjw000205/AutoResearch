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


