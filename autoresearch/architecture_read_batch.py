"""Batch exact scoped pages; no wider reading or additional tool permissions."""

import copy


RANGE_GUIDANCE = ('\nreview_unit.ranges 的 start/end 是文档全文的 Unicode 全局坐标；'
    'documents.read.offset 是所选章节内部的相对偏移。两者不能直接互换。'
    '先逐字使用 review_range_reads 提供的精确调用读取本段；'
    '宿主已按 offset=start-section.start 换算。核对回执 char_start/char_end 覆盖原全局范围，'
    '未覆盖须补读，不得仅凭章节目录、邻近片段或自述已读完而批准。')


def review_calls(context):
    """Translate immutable global review spans to the reader's local offsets."""
    doc = context['architecture_document']
    calls = []
    for scope in context['review_unit']['ranges']:
        section = next((s for s in doc['sections'] if s['id'] == scope['section_id']), None)
        start, end = scope['start'], scope['end']
        if (not section or type(start) is not int or type(end) is not int
                or not section['start'] <= start < end <= section['end']):
            raise ValueError('Architecture review range must use document-global coordinates inside its frozen section')
        while start < end:
            limit = min(6000, end - start)
            calls.append({'name': 'documents.read', 'arguments': {
                'document_id': doc['document_id'], 'expected_sha256': doc['sha256'],
                'section_id': section['id'], 'offset': start - section['start'], 'limit': limit}})
            start += limit
    return calls


def attach_review_ranges(context):
    result = copy.deepcopy(context)
    result.update(architecture_range_read_protocol=1, review_range_reads=review_calls(context))
    return result


def branch(context, receipts=None):
    if (context.get('architecture_range_read_protocol') == 1
            and context.get('architecture_workflow_protocol') == 1):
        from .engine import obj
        calls = review_calls(context)
        if calls != context.get('review_range_reads'):
            raise ValueError('Architecture read calls differ from the frozen global review ranges')
        from .architecture_review_budget import from_context
        budget = from_context(context)
        chars = budget['unit_char_limit'] if budget else 11000
        count = budget['max_batch_calls'] if budget else 24
        if not calls or sum(c['arguments']['limit'] for c in calls) > chars or len(calls) > count:
            return None
        return obj({'tool_calls': {'type': 'array', 'minItems': 1, 'maxItems': count,
                                   'uniqueItems': True, 'items': {'enum': calls}}})
    if (context.get('architecture_integration_protocol') == 1
            and context.get('architecture_workflow_protocol') == 1):
        from .architecture_integration import next_reads
        from .engine import obj
        calls = next_reads(context, receipts or [])
        if not calls:
            return None
        return obj({'tool_calls': {'type': 'array', 'minItems': 1, 'maxItems': 24,
                                   'uniqueItems': True, 'items': {'enum': calls}}})
    if (context.get('architecture_read_batch_protocol') != 1
            or context.get('architecture_workflow_protocol') != 1):
        return None
    from .engine import obj
    unit = context.get('review_unit') or {}
    docs = {d['document_id']: d for d in unit.get('documents', [])}
    calls = []
    for scope in unit.get('ranges', []):
        doc = docs.get(scope['document_id'])
        if not doc or doc['document_id'] not in context.get('architecture_required_documents', []):
            return None
        section = next((s for s in doc['sections'] if s['id'] == scope['section_id']), None)
        if not section or not section['start'] <= scope['start'] < scope['end'] <= section['end']:
            return None
        start = scope['start']
        while start < scope['end']:
            limit = min(6000, scope['end'] - start)
            calls.append({'name': 'documents.read', 'arguments': {
                'document_id': doc['document_id'], 'expected_sha256': doc['sha256'],
                'section_id': section['id'], 'offset': start - section['start'], 'limit': limit}})
            start += limit
    if not calls or sum(c['arguments']['limit'] for c in calls) > 11000:
        return None
    return obj({'tool_calls': {'type': 'array', 'minItems': 1, 'maxItems': 24,
                               'uniqueItems': True, 'items': {'enum': calls}}})
