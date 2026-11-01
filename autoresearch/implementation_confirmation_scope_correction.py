"""Current indexed reply confirmation scope; no inherited deferred votes.

Every original obligation for this role is required in the frozen full round.
Unknown confirmations cannot be removed by a host-derived correction.
"""
from __future__ import annotations

from .implementation_review_routing import digest

BOUNDARY_ERROR = '实现异议逐项闭环：原提出者必须逐项确认冻结工作单中的全部异议来源。'


def _fail(message):
    from .engine import ResearchBlocked
    raise ResearchBlocked('实现确认集合格式补正：' + message)


def problems(engine, run_id, role, hid, result, context):
    from . import implementation_issue_responses as responses
    from . import implementation_indexed_review as indexed
    if context.get(responses.PROTOCOL) != responses.VERSION:
        return []
    contract = responses._contract(context)
    frozen = engine.store.get('handoff_inputs', hid, {})
    assignment = engine.store.get('handoffs', hid, {})
    if (indexed.CONTRACT not in context or context.get(indexed.PROTOCOL) != indexed.VERSION
            or assignment.get('id') != hid or assignment.get('run_id') != run_id
            or assignment.get('to_role') != role or frozen.get('context') != context):
        _fail('确认集合不属于当前角色的真实冻结索引审核。')
    required, obligations = contract.get('requirements'), contract.get('obligations')
    if not isinstance(required, list) or not isinstance(obligations, list):
        _fail('本角色冻结异议清单已损坏。')
    expected = {item.get('origin_ref_sha256') for item in required if isinstance(item, dict)}
    all_own = {item.get('origin_ref_sha256') for item in obligations if isinstance(item, dict)}
    if (None in expected or len(expected) != len(required) or expected != all_own
            or any(item.get('role') != role for item in [*required, *obligations])):
        _fail('当前整轮必须逐项核对本角色全部冻结来源，不接受旧延后范围。')
    confirmations = result.get('issue_confirmations')
    if (not isinstance(confirmations, list) or any(not isinstance(item, dict) for item in confirmations)
            or len({item.get('origin_ref_sha256') for item in confirmations}) != len(confirmations)
            or {item.get('origin_ref_sha256') for item in confirmations} != expected):
        _fail('确认只能完整对应当前冻结工作单；不得增加、遗漏或借旧流程删除异议来源。')
    return []


def validate_correction(engine, run_id, role, hid, result, original, context, expected_problems):
    # An old checkpoint cannot activate a deferred-source production branch.
    if expected_problems or digest(result) != digest(original):
        _fail('旧延后确认补正已删除；必须返回当前完整独立审核。')
    problems(engine, run_id, role, hid, result, context)


def diagnostic(result, attempt, limit, mappings):
    _fail('当前整轮没有可自动排除的延后确认来源。')
