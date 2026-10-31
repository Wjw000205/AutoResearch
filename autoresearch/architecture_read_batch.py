"""Batch exact scoped pages; no wider reading or additional tool permissions."""


def branch(context, receipts=None):
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
