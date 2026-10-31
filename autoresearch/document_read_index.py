"""One navigation index over disposable document-read references.

Raw tool receipts, paid requests and visible current text remain unchanged.
The certificate authenticates clipping against an unchanged ledger prefix;
neither this index nor its reconstructed rows grants reading or approval.
"""
from __future__ import annotations

import copy
import hashlib
import re
from collections import defaultdict

from .document_read_window import _reference, enabled, sha
from .document_working_notes import read_directories

VERSION = 1
GUIDANCE = ('\n阅读记录统一见 document_read_window.read_index；documents 只汇总当前任务相关精确版本的已取回范围。'
    '索引和范围不是当前已展示原文，也不授予阅读信用或科学批准。'
    '当前可见正文及 document_read_fragments 保留原样；其余详情仍保存在原始工具账本。'
    '需要核对具体内容时，按索引的 document_id、SHA、section_id 和 reopen_policy 定向重开。'
    '当前工作笔记累计此前记录；只按当前阶段明确绑定的版本恢复相关内容，不要逐版重读历史笔记或实现文档。')
LEGACY_KINDS = frozenset({'document_working_notes', 'implementation_specification'})
_ARGS = frozenset({'document_id', 'expected_sha256', 'section_id', 'offset', 'limit'})
_PAGE = frozenset({'document_id', 'artifact_id', 'sha256', 'section_id', 'title',
    'document_version', 'evidence_state', 'text', 'char_start', 'char_end', 'offset',
    'total_chars', 'next_offset', 'truncated', 'document_budget_limited', 'text_returned',
    'primary_source_reading', 'scientific_claims_verified', 'untrusted', 'provenance',
    'physical_memory_document_id', 'canonical_source_id', 'canonical_paper_id',
    'content_sha256', 'source_format', 'text_truncated', 'empty_pages', 'source_scope', 'source_pages'})
_INDEX_FIELDS = frozenset({'version', 'documents', 'current_sources_sha256', 'archived_positions',
    'archived_receipts_sha256', 'archived_reads', 'historical_archived_reads', 'ledger_receipts',
    'ledger_sha256', 'reading_credit', 'scientific_approval', 'reopen_policy', 'scope', 'sha256'})


def _directories(context):
    documents = {}
    try:
        current = read_directories(context)
        optional = context.get('implementation_on_demand_documents', [])
        if not isinstance(current, list) or not isinstance(optional, list):
            raise ValueError('Current directories must be arrays')
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('Document read index current directories are invalid') from exc
    for doc in [*current, *optional]:
        if (not isinstance(doc, dict) or not isinstance(doc.get('document_id'), str)
                or not isinstance(doc.get('sha256'), str) or not isinstance(doc.get('sections'), list)
                or any(not isinstance(section, dict) for section in doc['sections'])):
            raise ValueError('Document read index current directory is invalid')
        key = (doc.get('document_id'), doc.get('sha256'))
        if key in documents and documents[key] != doc:
            raise ValueError('Document read index has conflicting current directories')
        documents[key] = doc
    return documents


def _scope_sha(documents):
    return sha([{'document_id': key[0], 'sha256': key[1], 'kind': doc.get('kind'),
        'sections': [{field: section.get(field) for field in ('id', 'start', 'end')}
                     for section in doc.get('sections', [])]}
                for key, doc in sorted(documents.items())])


def _known_read(row):
    """Recognize exact successful host reads; extensions must remain visible."""
    if not isinstance(row, dict) or set(row) != {'call', 'result'}:
        return False
    call, page = row['call'], row['result']
    if (not isinstance(call, dict) or set(call) != {'name', 'arguments'}
            or call['name'] != 'documents.read' or not isinstance(page, dict)
            or set(page) - _PAGE):
        return False
    args = call['arguments']
    if not isinstance(args, dict) or set(args) - _ARGS:
        return False
    did, digest, section = page.get('document_id'), page.get('sha256'), page.get('section_id')
    start, end, text = page.get('char_start'), page.get('char_end'), page.get('text')
    offset, limit = args.get('offset', 0), args.get('limit', 4000)
    if (not isinstance(did, str) or not did or not isinstance(digest, str)
            or not re.fullmatch('[0-9a-f]{64}', digest) or not isinstance(section, str) or not section
            or args.get('document_id') != did or args.get('expected_sha256') != digest
            or args.get('section_id') != section or type(start) is not int or type(end) is not int
            or not 0 <= start <= end or not isinstance(text, str) or end - start != len(text)
            or type(offset) is not int or offset < 0 or start < offset
            or type(limit) is not int or not 1 <= limit <= 6000 or end - start > limit
            or ('offset' in page and (type(page['offset']) is not int or page['offset'] != offset))
            or ('total_chars' in page and (type(page['total_chars']) is not int
                or page['total_chars'] < offset + len(text)))
            or page.get('evidence_state', 'retrieved') != 'retrieved'
            or page.get('provenance', 'exact_document_section') != 'exact_document_section'
            or page.get('primary_source_reading', False) is not False
            or page.get('scientific_claims_verified', False) is not False
            or page.get('untrusted', True) is not True
            or page.get('text_returned', True) is not True):
        return False
    pages = page.get('source_pages', [])
    if not isinstance(pages, list) or any(not isinstance(item, dict) or set(item) - {
            'page_number', 'page', 'start', 'end', 'text_sha256'}
            or type(item.get('start')) is not int or type(item.get('end')) is not int
            or not 0 <= item['start'] < item['end'] for item in pages):
        return False
    return True


def _in_directory(row, document):
    page, args = row['result'], row['call']['arguments']
    section = next((s for s in document.get('sections', []) if s.get('id') == page['section_id']), None)
    return (section is not None and type(section.get('start')) is int and type(section.get('end')) is int
        and section['start'] <= page['char_start'] <= page['char_end'] <= section['end']
        and page['char_start'] == section['start'] + args.get('offset', 0))


def _matching_view(row, raw, position):
    if row == raw:
        return True
    if not isinstance(row, dict) or set(row) != {'call', 'result'} or row['call'] != raw['call']:
        return False
    page = row['result']
    if not isinstance(page, dict) or 'text' in page:
        return False
    fragments = page.get('document_read_fragments', [])
    if not isinstance(fragments, list):
        return False
    expected = _reference(raw, position)
    clean = {key: value for key, value in page.items() if key != 'document_read_fragments'}
    if clean != expected['result']:
        return False
    for fragment in fragments:
        if not isinstance(fragment, dict):
            return False
        start, end = fragment.get('char_start'), fragment.get('char_end')
        original = raw['result']
        if (type(start) is not int or type(end) is not int
                or not original['char_start'] <= start < end <= original['char_end']):
            return False
        # Preserve unknown fragment extensions instead of treating them as display metadata.
        part = original['text'][start-original['char_start']:end-original['char_start']]
        expected_part = {key: copy.deepcopy(value) for key, value in original.items()
            if key not in {'text', 'char_start', 'char_end', 'offset', 'next_offset',
                           'truncated', 'source_pages', 'total_chars'}}
        expected_part.update(text=part, char_start=start, char_end=end, text_returned=True,
            text_sha256=hashlib.sha256(part.encode()).hexdigest(), primary_source_reading=False,
            scientific_claims_verified=False, provenance='exact_archived_read_prompt_fragment', untrusted=True)
        if 'source_pages' in original:
            expected_part['source_pages'] = [copy.deepcopy(item) for item in original['source_pages']
                                            if item['start'] < end and item['end'] > start]
        if fragment != expected_part:
            return False
    return True


def _merge(ranges):
    result = []
    for start, end in sorted(ranges):
        if result and start <= result[-1][1]:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result


def _documents_index(documents, rows):
    groups = defaultdict(lambda: defaultdict(list))
    for _, raw in rows:
        page = raw['result']
        key = (page['document_id'], page['sha256'])
        if key in documents:
            if not _in_directory(raw, documents[key]):
                raise ValueError('Document read index range is outside its current frozen section')
            if page['char_start'] < page['char_end']:
                groups[key][page['section_id']].append([page['char_start'], page['char_end']])
    result = []
    for key, sections in sorted(groups.items()):
        bounds = {section['id']: section['start'] for section in documents[key]['sections']}
        result.append({'document_id': key[0], 'sha256': key[1], 'sections': [
            {'section_id': sid, 'section_start': bounds[sid], 'ranges': _merge(ranges)}
            for sid, ranges in sorted(sections.items())], 'reopen': {'name': 'documents.read',
                'arguments': {'document_id': key[0], 'expected_sha256': key[1]}}})
    return result


def archived_rows(context, ledger):
    """Authenticate archived original rows, including after later ledger appends.

    The caller must still validate actual local bytes and delivery for reading.
    A missing index is ordinary legacy input; a damaged index fails closed.
    """
    window = context.get('document_read_window', {})
    if not isinstance(window, dict) or 'read_index' not in window:
        return []
    index = window['read_index']
    if (not isinstance(index, dict) or set(index) != _INDEX_FIELDS
            or type(index.get('version')) is not int or index['version'] != VERSION
            or index.get('reading_credit') is not False or index.get('scientific_approval') is not False
            or type(index.get('archived_reads')) is not int or index['archived_reads'] < 0
            or type(index.get('historical_archived_reads')) is not int or index['historical_archived_reads'] < 0
            or index.get('sha256') != sha({key: value for key, value in index.items() if key != 'sha256'})):
        raise ValueError('Document read index certificate is invalid')
    count, positions = index.get('ledger_receipts'), index.get('archived_positions')
    if (not isinstance(ledger, list) or type(count) is not int or not 0 <= count <= len(ledger)
            or sha(ledger[:count]) != index.get('ledger_sha256') or not isinstance(positions, list)):
        raise ValueError('Document read index ledger prefix changed')
    rows, previous_end = [], -1
    for interval in positions:
        if (not isinstance(interval, list) or len(interval) != 2
                or any(type(value) is not int for value in interval)
                or not 0 <= interval[0] < interval[1] <= count or interval[0] <= previous_end):
            raise ValueError('Document read index archived positions are not canonical ranges')
        start, end = interval
        rows.extend((position, ledger[position]) for position in range(start, end))
        previous_end = end
    if (any(not _known_read(raw) for _, raw in rows) or len(rows) != index.get('archived_reads')
            or sha([[position, sha(raw)] for position, raw in rows]) != index.get('archived_receipts_sha256')):
        raise ValueError('Document read index archived receipts changed or are not exact successful reads')
    documents = _directories(context)
    historical = sum((raw['result']['document_id'], raw['result']['sha256']) not in documents for _, raw in rows)
    if (_scope_sha(documents) != index.get('current_sources_sha256')
            or _documents_index(documents, rows) != index.get('documents')
            or historical != index.get('historical_archived_reads')):
        raise ValueError('Document read index current versions or ranges changed')
    return rows


def compact(context, ledger, *, historical_directories=()):
    """Project references into one index without modifying either argument.

    Current versions come only from the post-phase directories. Earlier notes
    or SPEC text can be archived only against explicit pre-phase directories,
    never by choosing the latest item of a kind or inferring a filename.
    """
    result = copy.deepcopy(context)
    if not enabled(context) or not isinstance(context.get('document_read_window'), dict):
        return result
    if 'read_index' in context['document_read_window']:
        archived_rows(context, ledger)
        return result
    if (not isinstance(ledger, list)
            or context['document_read_window'].get('ledger_receipts') != len(ledger)
            or context['document_read_window'].get('ledger_sha256') != sha(ledger)):
        raise ValueError('Document read index requires this exact original window ledger')
    documents = _directories(context)
    historical = {}
    for doc in historical_directories:
        if isinstance(doc, dict) and doc.get('kind') in LEGACY_KINDS:
            key = (doc.get('document_id'), doc.get('sha256'))
            if key in historical and historical[key] != doc:
                raise ValueError('Document read index has conflicting explicit historical directories')
            historical[key] = doc
    by_sha = defaultdict(list)
    for position, row in enumerate(ledger):
        if _known_read(row):
            by_sha[sha(row)].append(position)
    kept, archived = [], {}
    for row in context.get('tool_results', []):
        page = row.get('result', {}) if isinstance(row, dict) else {}
        ref = page.get('document_read_reference', {}) if isinstance(page, dict) else {}
        position = ref.get('ledger_position') if isinstance(ref, dict) else None
        if type(position) is not int:
            candidates = by_sha.get(sha(row), [])
            position = next((pos for pos in candidates if pos not in archived), None)
        if type(position) is not int or not 0 <= position < len(ledger):
            kept.append(copy.deepcopy(row))
            continue
        raw = ledger[position]
        if not _known_read(raw) or not _matching_view(row, raw, position):
            kept.append(copy.deepcopy(row))
            continue
        key = (raw['result']['document_id'], raw['result']['sha256'])
        document = documents.get(key)
        old = document is None and key in historical
        eligible = document or (historical[key] if old else None)
        if (not eligible or not _in_directory(raw, eligible)
                or (not old and ('text' in page or page.get('document_read_fragments')))):
            kept.append(copy.deepcopy(row))
            continue
        archived[position] = raw
    if not archived:
        return result
    rows = sorted(archived.items())
    index = {'version': VERSION, 'documents': _documents_index(documents, rows),
        'current_sources_sha256': _scope_sha(documents),
        'archived_positions': _merge([[pos, pos + 1] for pos in archived]),
        'archived_receipts_sha256': sha([[pos, sha(raw)] for pos, raw in rows]),
        'archived_reads': len(rows), 'historical_archived_reads': sum(
            (raw['result']['document_id'], raw['result']['sha256']) not in documents for _, raw in rows),
        'ledger_receipts': len(ledger), 'ledger_sha256': sha(ledger),
        'reading_credit': False, 'scientific_approval': False,
        'reopen_policy': 'Use documents.read with the exact document_id/SHA/section. '
            'offset=start-section_start; choose a bounded limit, at most 6000. '
            'Historical notes or SPEC versions require explicit task authorization; do not reread them by default.',
        'scope': 'Navigation over archived successful reads only. Current visible text and fragments stay verbatim. '
            'The full immutable ledger stays in handoff_progress; an index is not displayed text, reading credit or approval.'}
    index['sha256'] = sha(index)
    result['tool_results'] = kept
    result['document_read_window']['read_index'] = index
    archived_rows(result, ledger)
    return result
