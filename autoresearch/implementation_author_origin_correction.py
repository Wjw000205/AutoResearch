"""One author-returned correction of a proven historical origin binding.

The host can diagnose an exact historical-to-current mapping, but cannot save
its projected answer as an author delivery. Full ordinary author validation
must pass on the diagnostic projection before granting a final-only successor,
and on the actual author's answer before sealing it. No scientific issue closes.
"""
from __future__ import annotations

import copy

from .implementation_review_routing import digest

VERSION = 1
LIMIT = 1
KEY = 'implementation_author_origin_correction'
HOST_DIAGNOSTIC = 'implementation_author_current_origin_required'
BOUNDARY_ERROR = '实现异议逐项闭环：作者必须逐一回应全部本轮文档异议来源，不能遗漏或重复。'
_MAPPING_FIELDS = frozenset({'reply_index', 'field_path', 'objection_id', 'role',
    'original_origin_ref', 'original_origin_ref_sha256',
    'corrected_origin_ref', 'corrected_origin_ref_sha256'})


def _fail(message):
    from .engine import ResearchBlocked
    raise ResearchBlocked('实现作者来源绑定补正：' + message)


def _checked_mappings(original, mappings):
    """Bind each exact replacement to an unchanged original reply and role."""
    from . import implementation_issue_responses as responses
    replies = original.get('issue_responses') if isinstance(original, dict) else None
    if (not isinstance(replies, list) or not replies
            or any(not isinstance(reply, dict) for reply in replies)
            or not isinstance(mappings, list) or not mappings):
        _fail('缺少原完整候选或精确历史来源映射。')
    original_keys = [reply.get('origin_ref_sha256') for reply in replies]
    if (any(not isinstance(key, str) or not key for key in original_keys)
            or len(set(original_keys)) != len(original_keys)):
        _fail('原回复的来源重复或不完整，不能借补正增删异议。')
    checked = {}
    for mapping in mappings:
        if not isinstance(mapping, dict) or set(mapping) != _MAPPING_FIELDS:
            _fail('来源补正映射字段不完整。')
        index = mapping['reply_index']
        if type(index) is not int or not 0 <= index < len(replies) or index in checked:
            _fail('来源补正项重复或不属于原回复。')
        reply = replies[index]
        old, new = mapping['original_origin_ref'], mapping['corrected_origin_ref']
        if (mapping['field_path'] != ['issue_responses', index, 'origin_ref_sha256']
                or not isinstance(mapping['objection_id'], str) or not mapping['objection_id']
                or mapping['objection_id'] != reply.get('objection_id')
                or not responses._valid_origin(old) or not responses._valid_origin(new)
                or mapping['role'] != old['role'] or mapping['role'] != new['role']
                or not old['revision'] < new['revision']
                or mapping['original_origin_ref_sha256'] != digest(old)
                or mapping['original_origin_ref_sha256'] != reply.get('origin_ref_sha256')
                or mapping['corrected_origin_ref_sha256'] != digest(new)
                or mapping['corrected_origin_ref_sha256'] == mapping['original_origin_ref_sha256']):
            _fail('补正映射改变原异议、角色或缺少真实历史到当前来源关系。')
        checked[index] = mapping
    projected_keys = [checked[index]['corrected_origin_ref_sha256'] if index in checked else key
                      for index, key in enumerate(original_keys)]
    if len(set(projected_keys)) != len(projected_keys):
        _fail('补正后的来源重复，不能合并独立修改要求。')
    return checked


def _project(original, mappings):
    """In-memory diagnostic only; never persist or use as an author decision."""
    checked = _checked_mappings(original, mappings)
    projected = copy.deepcopy(original)
    for index, mapping in checked.items():
        projected['issue_responses'][index]['origin_ref_sha256'] = mapping['corrected_origin_ref_sha256']
    return projected


def problems(engine, run_id, role, hid, result, context):
    """Admit only an authenticated, bijective historical-source confusion."""
    from . import implementation_issue_responses as responses
    from . import implementation_alignment_evidence as alignment
    from .engine import ResearchBlocked
    markers = (responses.PROTOCOL, alignment.PROTOCOL, 'implementation_document_authoring_protocol',
               'implementation_indexed_review_protocol')
    if (role != 'engineer' or any(type(context.get(marker)) is not int or context[marker] != 1
                                 for marker in markers)
            or context.get('indexed_response_actions') is not True):
        _fail('仅支持同一工程师当前整轮索引修订的真实冻结候选。')
    # This authenticates the original submission, frozen engineer handoff and
    # every original obligation before reaching the exact origin-set boundary.
    try:
        alignment.validate_candidate(engine, run_id, role, hid, result, context)
    except ResearchBlocked as error:
        if str(error) != BOUNDARY_ERROR:
            raise
    else:
        return []
    contract = responses._contract(context)
    actions, obligations = contract.get('actions'), contract.get('obligations')
    replies = result.get('issue_responses') if isinstance(result, dict) else None
    if (type(contract.get('revision')) is not int or contract['revision'] < 1
            or not isinstance(actions, list) or not actions or not isinstance(obligations, list)
            or not isinstance(replies, list) or len(replies) != len(actions)):
        _fail('原回复必须与当前逐项要求数量一致。')
    origins = {}
    for item in obligations:
        ref = item.get('origin_ref') if isinstance(item, dict) else None
        if (not responses._valid_origin(ref) or item.get('role') != ref['role']
                or not isinstance(item.get('objection_id'), str) or not item['objection_id']
                or item.get('origin_ref_sha256') != digest(ref)
                or item['origin_ref_sha256'] in origins):
            _fail('历史来源身份不完整或重复。')
        origins[item['origin_ref_sha256']] = item
    current, owners = {}, {}
    for action in actions:
        ref = action.get('origin_ref') if isinstance(action, dict) else None
        if (not responses._valid_origin(ref) or action.get('role') != ref['role']
                or not isinstance(action.get('objection_id'), str) or not action['objection_id']
                or ref['revision'] != contract['revision'] - 1
                or action.get('origin_ref_sha256') != digest(ref)
                or action['origin_ref_sha256'] in current):
            _fail('当前修改要求必须绑定唯一紧邻前轮的真实来源。')
        authenticated = origins.get(action['origin_ref_sha256'])
        if (authenticated is None or authenticated.get('objection_id') != action.get('objection_id')
                or authenticated['role'] != action['role']):
            _fail('当前修改要求缺少合同内同一身份的已核验真实来源。')
        current[action['origin_ref_sha256']] = action
        owners.setdefault((action.get('objection_id'), action['role']), []).append(action)
    mappings, matched, original_keys = [], [], set()
    for index, reply in enumerate(replies):
        if not isinstance(reply, dict):
            _fail('原回复不是完整对象。')
        key, identity = reply.get('origin_ref_sha256'), reply.get('objection_id')
        if not isinstance(key, str) or key in original_keys:
            _fail('原回复不能包含重复或缺失来源。')
        original_keys.add(key)
        if key in current:
            target = current[key]
            if identity != target['objection_id']:
                _fail('已正确的当前来源不能更换原异议身份。')
        else:
            old = origins.get(key)
            if old is None or old.get('objection_id') != identity:
                _fail('旧来源必须属于原回复相同的真实规范异议身份。')
            targets = owners.get((identity, old['role']), [])
            if len(targets) != 1:
                _fail('历史来源不能猜测角色或选择多个当前修改要求。')
            target = targets[0]
            mappings.append({'reply_index': index,
                'field_path': ['issue_responses', index, 'origin_ref_sha256'],
                'objection_id': identity, 'role': old['role'],
                'original_origin_ref': copy.deepcopy(old['origin_ref']),
                'original_origin_ref_sha256': key,
                'corrected_origin_ref': copy.deepcopy(target['origin_ref']),
                'corrected_origin_ref_sha256': target['origin_ref_sha256']})
        matched.append(target['origin_ref_sha256'])
    if len(set(matched)) != len(matched) or set(matched) != set(current):
        _fail('历史到当前来源必须逐项一一对应，不能遗漏或合并。')
    projected = _project(result, mappings)
    # Changed fields are now checked against the current action's actual source
    # version, and ordinary evidence/reading gates must all pass. Any further
    # failure propagates; it cannot receive this origin-only recovery allowance.
    alignment.validate_candidate(engine, run_id, role, hid, projected, context)
    return copy.deepcopy(mappings)


def diagnostic(result, attempt, limit, mappings):
    if type(attempt) is not int or attempt != LIMIT or type(limit) is not int or limit != LIMIT:
        _fail('只允许一次有界的 final-only 作者来源补正。')
    _checked_mappings(result, mappings)
    return {'host_diagnostic': HOST_DIAGNOSTIC, 'error': BOUNDARY_ERROR,
        KEY: {'version': VERSION, 'attempt': attempt, 'max_attempts': limit,
            'original_candidate': copy.deepcopy(result), 'original_candidate_sha256': digest(result),
            'problems': copy.deepcopy(mappings), 'scientific_approval': False, 'tools_allowed': False,
            'instruction': '这是同一工程师唯一一次 final-only 来源绑定补正，不能调用任何工具。'
                '原 issue_responses 使用了真实历史来源；本次必须由你本人逐项回应冻结合同 actions 中的当前修改要求。'
                '返回原 Schema 的完整 final，仅将 problems 指定位置的 origin_ref_sha256 改为 corrected_origin_ref_sha256。'
                '这不是普通坐标平移：corrected_origin_ref 指向紧邻前轮的真实评审，须核对保留的回复理由是否对应当前要求。'
                'submission、文档ID/SHA、spec_hash、全部数组顺序、objection_id、rationale、revision_or_rebuttal、'
                'changed_fields、evidence 和未诊断回复逐字保留，禁止修改正文、证据、声明或其他字段。'
                '不能把宿主诊断当作作者交付、评审接受、异议关闭或科学批准；实际答复仍须完整作者校验及三方独立复核。'
                '如果原理由和证据无法支持当前要求，不要强行绑定或宣称已修复；保留原答复使其继续阻塞。'}}


def validate_correction(result, original, mappings):
    """Accept only the actual author's complete, exact hash-only replacement."""
    expected = _project(original, mappings)
    if not isinstance(result, dict) or digest(result) != digest(expected):
        _fail('仅允许指定来源哈希变化；原 submission、异议身份、顺序、声明和证据必须保持。')
    return None
