"""Narrow coordinate diagnostics, never a derived or substituted review vote.

Only a uniquely section-relative anchor outside the absolute section bounds may
be corrected. The reviewer must return the complete original verdict unchanged,
apart from the exact fixed-section translation of those two numeric fields.
"""
from __future__ import annotations

import copy

from . import implementation_review_routing as routing

BOUNDARY_ERROR = '复核后继引用了固定索引外的文档或范围'


def problems(result, context, role):
    """Find only unambiguous relative coordinates in the immutable directory.

    Ordinary routing, identity, source, hash and range errors remain hard errors.
    A valid absolute range stays absolute even if it could also be interpreted
    as relative; this function never changes an already valid location.
    """
    if 'unit_reviews' in result or context.get('review_unit') is not None:
        raise ValueError('旧分段或批量评议已删除，不能进行当前索引审核的坐标补正。')
    boundary_failed = False
    try:
        routing.validate(result, context, role)
    except ValueError as exc:
        if str(exc) != BOUNDARY_ERROR:
            raise
        boundary_failed = True
    if result.get('review_routing') is None:
        return []
    docs = {}
    for doc in context.get('indexed_documents', []):
        did = doc.get('document_id')
        if did in docs and docs[did] != doc:
            raise ValueError(BOUNDARY_ERROR)
        docs[did] = doc
    result_problems = []
    for entry_index, item in enumerate(result['review_routing']['issues']):
        for target_index, target in enumerate(item['required_reads']):
            doc = docs.get(target.get('document_id'))
            sections = [section for section in (doc or {}).get('sections', [])
                        if section.get('id') == target.get('section_id')]
            start, end = target.get('start'), target.get('end')
            if (not doc or target.get('sha256') != doc.get('sha256') or len(sections) != 1
                    or type(start) is not int or type(end) is not int or start < 0 or end <= start):
                raise ValueError(BOUNDARY_ERROR)
            section = sections[0]
            left, right = section.get('start'), section.get('end')
            if type(left) is not int or type(right) is not int or not 0 <= left < right:
                raise ValueError(BOUNDARY_ERROR)
            if left <= start < end <= right:
                continue
            if not left or not 0 <= start < end <= right - left:
                raise ValueError(BOUNDARY_ERROR)
            corrected = {**target, 'start': left + start, 'end': left + end}
            result_problems.append({'entry_index': entry_index, 'issue_index': item['issue_index'],
                'target_index': target_index, 'original': copy.deepcopy(target),
                'corrected': corrected, 'section_bounds': {'start': left, 'end': right},
                'kind': 'section_relative_coordinates'})
    if not result_problems:
        if boundary_failed:
            raise ValueError(BOUNDARY_ERROR)
        return []
    # Correcting coordinates cannot excuse another routing error after the
    # first invalid anchor (for example an unsupported milestone or identity).
    expected = _apply(result, result_problems)
    routing.validate(expected, context, role)
    return result_problems


def _apply(result, mappings):
    expected = copy.deepcopy(result)
    for item in mappings:
        expected['review_routing']['issues'][item['entry_index']]['required_reads'][item['target_index']] = copy.deepcopy(item['corrected'])
    return expected


def corrected(result, context, role):
    """Expected response for schema/validation only; never save it as a vote."""
    mappings = problems(result, context, role)
    return _apply(result, mappings) if mappings else None


def validate_correction(result, original, context, role):
    """Require the actual model response to be this one exact format change."""
    from .engine import ResearchBlocked, digest
    expected = corrected(original, context, role)
    if expected is None or digest(result) != digest(expected):
        raise ResearchBlocked('复核坐标格式修正改变了原判断、异议或非坐标字段，未登记批准。')


def diagnostic(result, context, role, attempt, max_attempts):
    mappings = problems(result, context, role)
    return {'host_diagnostic': 'implementation_routing_coordinates_required',
        'error': BOUNDARY_ERROR,
        'alignment_read_correction': {'attempt': attempt, 'max_attempts': max_attempts},
        'implementation_routing_coordinate_correction': {
            'mappings': mappings, 'original_candidate': copy.deepcopy(result),
            'instruction': '仅把 mappings 中原 required_reads 的章节相对 start/end 改为 corrected 的全文绝对字符坐标。'
                '返回原 Schema 的完整答复；所有其他字段、数组顺序、原 approve、issues、fixes、路由类型、'
                '恢复条件、来源ID/哈希/章节、进展和架构结论必须逐字保留，不得新增批准或删异议。'
                '此处只更正坐标表示，不代表要求的范围已经读完或核实，也不改变科学判断。'
                '未列在 mappings 的范围原样保留；禁止扩大来源、改变原范围长度或替换文档版本。'}}
