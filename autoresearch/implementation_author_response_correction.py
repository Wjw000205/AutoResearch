"""One exact author declaration correction; no document/evidence changes.

The runtime owns durable journaling and final-only dispatch. This module keeps
the received candidate and all immutable evidence intact, and leaves full
author validation and independent review mandatory after the correction.
"""
from __future__ import annotations

import copy

from .implementation_review_routing import digest

VERSION = 1
LIMIT = 1
KEY = 'implementation_author_response_correction'
HOST_DIAGNOSTIC = 'implementation_author_response_changed_fields_required'
_EDITABLE = {'changed_fields', 'revision_or_rebuttal', 'rationale'}


def _fail(message):
    from .engine import ResearchBlocked
    raise ResearchBlocked('实现作者回复格式补正：' + message)


def problems(engine, run_id, role, hid, result, context):
    """Rebuild the typed failure with the actual frozen author/evidence gate."""
    from . import implementation_issue_responses as responses
    from . import implementation_alignment_evidence as alignment
    if (role != 'engineer' or type(context.get(responses.PROTOCOL)) is not int
            or context[responses.PROTOCOL] != responses.VERSION
            or type(context.get(alignment.PROTOCOL)) is not int or context[alignment.PROTOCOL] != 1
            or type(context.get('implementation_document_authoring_protocol')) is not int
            or context['implementation_document_authoring_protocol'] != 1):
        _fail('仅支持同一工程师的真实冻结逐项回复候选。')
    try:
        alignment.validate_candidate(engine, run_id, role, hid, result, context)
    except responses.ChangedFieldsBlocked as exc:
        return copy.deepcopy(exc.problems)
    return []


def _checked_problems(original, mappings):
    from . import implementation_issue_responses as responses
    replies = original.get('issue_responses') if isinstance(original, dict) else None
    if not isinstance(replies, list) or not isinstance(mappings, list) or not mappings:
        _fail('缺少原完整候选或精确 changed_fields 诊断。')
    result = {}
    for problem in mappings:
        if not isinstance(problem, dict):
            _fail('格式补正诊断不完整。')
        index = problem.get('reply_index')
        if type(index) is not int or not 0 <= index < len(replies) or index in result:
            _fail('格式补正项重复或不属于原回复。')
        reply = replies[index]
        origin = problem.get('origin_ref')
        changed = problem.get('actual_changed_fields')
        if (not isinstance(reply, dict) or problem.get('field_path') != ['issue_responses', index, 'changed_fields']
                or problem.get('objection_id') != reply.get('objection_id')
                or problem.get('origin_ref_sha256') != reply.get('origin_ref_sha256')
                or not responses._valid_origin(origin) or digest(origin) != reply.get('origin_ref_sha256')
                or problem.get('role') != origin['role']
                or problem.get('revision_or_rebuttal') != reply.get('revision_or_rebuttal')
                or problem.get('claimed_fields') != reply.get('changed_fields')
                or problem.get('reason') not in {'revision_fields_empty', 'revision_fields_not_changed',
                                                  'rebuttal_claims_changed_fields'}
                or not isinstance(changed, list)
                or any(not isinstance(field, str) or not field for field in changed)
                or len(set(changed)) != len(changed)):
            _fail('格式补正诊断与原异议身份、原字段声明或实际差异不一致。')
        result[index] = problem
    return result


def diagnostic(result, attempt, limit, mappings):
    """Only original reply JSON and field names; no source/SPEC text is added."""
    if type(attempt) is not int or attempt != LIMIT or type(limit) is not int or limit != LIMIT:
        _fail('只允许一次有界的 final-only 回复格式补正。')
    _checked_problems(result, mappings)
    return {'host_diagnostic': HOST_DIAGNOSTIC,
        'error': 'changed_fields 声明与本版实际差异不一致，尚未封存作者提交。',
        KEY: {'version': VERSION, 'attempt': attempt, 'max_attempts': limit,
            'original_candidate': copy.deepcopy(result), 'original_candidate_sha256': digest(result),
            'problems': copy.deepcopy(mappings), 'scientific_approval': False, 'tools_allowed': False,
            'instruction': '这是同一工程师唯一一次 final-only 格式补正，不能调用任何工具。'
                '返回原 Schema 的完整 final；真实 submission 指针、全部异议 ID/origin、所有证据和未诊断回复逐字保留。'
                '只修改 problems 指定项的 changed_fields、revision_or_rebuttal 和 rationale。'
                'revision 必须指向相对原审查版本真实改变的字段或路径，不能根据其他模块变化冒领修复。'
                '未实际修复可由你诚实改为 rebuttal、changed_fields=[]，说明原问题仍未修复或已有条款的理由；'
                '宿主不会自动改写结论。原回复若是 rebuttal，只清空错误 changed_fields，禁止改为 revision。'
                '不能修改 evidence、正文、文档版本、哈希或任何其他回复，不得声称评审已接受、异议已关闭或科学批准。'}}


def validate_correction(result, original, mappings):
    """Preserve every uneditable byte-valued field before full host validation."""
    targets = _checked_problems(original, mappings)
    if (not isinstance(result, dict) or set(result) != set(original)
            or any(digest(result[key]) != digest(value) for key, value in original.items() if key != 'issue_responses')):
        _fail('真实 submission 或其他顶层字段发生改变。')
    replies, previous = result.get('issue_responses'), original['issue_responses']
    if not isinstance(replies, list) or len(replies) != len(previous):
        _fail('不能增删或重排原异议回复。')
    from .implementation_issue_responses import _field_parts
    for index, (reply, old) in enumerate(zip(replies, previous)):
        if index not in targets:
            if digest(reply) != digest(old):
                _fail('未诊断的回复必须原样保留。')
            continue
        if (not isinstance(reply, dict) or set(reply) != set(old)
                or any(digest(reply[key]) != digest(value) for key, value in old.items() if key not in _EDITABLE)):
            _fail('不能改变原异议身份、证据或非补正字段。')
        kind, fields, rationale = (reply.get(key) for key in ('revision_or_rebuttal', 'changed_fields', 'rationale'))
        if (kind not in {'revision', 'rebuttal'} or not isinstance(fields, list)
                or any(not isinstance(field, str) or not field for field in fields)
                or len(set(fields)) != len(fields) or not isinstance(rationale, str)
                or not rationale.strip()):
            _fail('补正回复仍不符合字段或理由格式。')
        if old['revision_or_rebuttal'] == 'rebuttal' and kind != 'rebuttal':
            _fail('原反驳不能借格式补正升级为文档修复。')
        if kind == 'rebuttal':
            if fields:
                _fail('反驳只能保留 changed_fields=[]。')
        elif not fields or any((parts := _field_parts(path)) is None
                              or parts[0] not in targets[index]['actual_changed_fields'] for path in fields):
            _fail('修复必须列本版真实改变的字段，精确子路径仍须完整作者验证。')
    return None
