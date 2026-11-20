"""Turn a host-owned Unicode span into legal, section-bound source references."""
from __future__ import annotations

PAGE_CHARS = 6000


def references(document, start, end, *, page_chars=PAGE_CHARS):
    if (type(start) is not int or type(end) is not int or not 0 <= start < end
            or type(page_chars) is not int or not 1 <= page_chars <= PAGE_CHARS):
        raise ValueError('Invalid source span or page size')
    cursor, result = start, []
    for section in document['sections']:
        if section['end'] <= cursor:
            continue
        if section['start'] > cursor:
            break  # A missing section must never be silently skipped.
        stop = min(end, section['end'])
        while cursor < stop:
            limit = min(page_chars, stop-cursor)
            result.append({'document_id':document['document_id'],'document_sha256':document['sha256'],
                'section_id':section['id'],'offset':cursor-section['start'],'limit':limit})
            cursor += limit
        if cursor == end:
            return result
    raise ValueError('Source span extends outside the indexed sections')
