"""Bounded prompt views over an unchanged, complete document-read ledger.

A prompt budget is not a lifetime reading quota. New assignments opt in;
already dispatched requests and their original receipts keep their contract.
"""
from __future__ import annotations

import copy
import hashlib
import json

VERSION = 1
RANGE_VERSION = 2
RETAINED_VERSION = 3
TOTAL_LIMIT = 12000
DOCUMENT_LIMIT = 6000
AUTHOR_READ = 'implementation_author_read_recovery'
AUTHOR_INHERITED_DELIVERY = 'implementation_author_inherited_delivery_recovery'


class DocumentReadWindowBudgetExceeded(ValueError):
    """A mandatory, actually read range cannot fit the disposable view."""
    def __init__(self, diagnostic):
        self.diagnostic = diagnostic
        super().__init__('Document window cannot fit mandatory read evidence: '
                         f"required_read_chars={diagnostic['protected_read_chars']}, "
                         f"total_limit={diagnostic['total_limit']}; no request was dispatched")


def enabled(context):
    return (context.get('document_read_window_protocol') in (VERSION, RANGE_VERSION, RETAINED_VERSION)
            and context.get('indexed_document_context') is True
            and context.get('document_index_protocol') == 1)


def sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def bound_read(document, page):
    """Each actual read is bounded; earlier reads never exhaust this allowance."""
    limit = min(DOCUMENT_LIMIT, max(0, document['total_chars'] - 1))
    if limit <= 0:
        return {'error': '文档无可分页正文', 'text_returned': False}
    if len(page['text']) <= limit:
        return page
    return {**page, 'text': page['text'][:limit],
        'char_end': page['char_start'] + limit, 'next_offset': page['offset'] + limit,
        'truncated': True, 'document_budget_limited': True}


def _reference(receipt, position):
    page = receipt['result']
    result = {k: copy.deepcopy(v) for k, v in page.items() if k != 'text'}
    result.update(text_returned=False, document_read_reference={
        'ledger_position': position, 'receipt_sha256': sha(receipt),
        'text_sha256': hashlib.sha256(page['text'].encode('utf-8')).hexdigest(),
        'text_chars': len(page['text']),
        'reopen': copy.deepcopy(receipt['call']),
        'scope': 'Previously inspected exact range, archived unchanged; text absent from this prompt.'})
    return {**copy.deepcopy(receipt), 'result': result}


def project(context, receipts):
    """Keep newest pages visible, older pages as exact recoverable references.

    This never edits the durable ledger. Coverage validators inspect that
    ledger, not this disposable view, and still validate local source bytes.
    """
    if not enabled(context):
        return {'tool_results': copy.deepcopy(receipts)}
    if (context['document_read_window_protocol'] in (RANGE_VERSION, RETAINED_VERSION)
            or context.get(AUTHOR_READ) is not None
            or context.get(AUTHOR_INHERITED_DELIVERY) is not None):
        return _range_project(context, receipts)
    from .document_working_notes import read_directories
    documents = {d['document_id']: d for d in read_directories(context)}
    views = copy.deepcopy(receipts)
    used, per_document, visible, seen = 0, {}, [], set()
    for position in range(len(receipts) - 1, -1, -1):
        row = receipts[position]
        if not isinstance(row, dict):
            continue
        call, page = row.get('call', {}), row.get('result', {})
        if (call.get('name') not in {'documents.read', 'documents.search'}
                or not isinstance(page, dict) or page.get('error')):
            continue
        doc = documents.get(page.get('document_id'))
        if not doc or page.get('sha256') != doc['sha256']:
            raise ValueError('Document window receipt is outside its frozen index')
        did = doc['document_id']
        available = min(TOTAL_LIMIT - used,
                        min(DOCUMENT_LIMIT, doc['total_chars'] - 1) - per_document.get(did, 0))
        if call['name'] == 'documents.search':
            # Search excerpts use the same prompt budget; anchors survive eviction.
            kept = []
            for hit in page.get('hits', []):
                excerpt = hit.get('excerpt', '')
                text = excerpt[:max(0, available)]
                kept.append({**copy.deepcopy(hit), 'excerpt': text,
                             'excerpt_truncated': len(text) != len(excerpt) or hit.get('excerpt_truncated', False)})
                available -= len(text)
                used += len(text)
                per_document[did] = per_document.get(did, 0) + len(text)
            views[position]['result']['hits'] = kept
            continue
        text = page.get('text')
        if not isinstance(text, str):
            continue
        fingerprint = sha(row)
        if fingerprint not in seen and len(text) <= available:
            used += len(text)
            per_document[did] = per_document.get(did, 0) + len(text)
            visible.append(fingerprint)
            seen.add(fingerprint)
        else:
            views[position] = _reference(row, position)
    return {'tool_results': views, 'document_read_window': {
        'version': VERSION, 'text_chars': used, 'total_limit': TOTAL_LIMIT,
        'per_document_limit': DOCUMENT_LIMIT, 'per_document_chars': per_document,
        'ledger_receipts': len(receipts), 'ledger_sha256': sha(receipts),
        'visible_receipt_sha256': list(reversed(visible)),
        'scope': 'Prompt view only. Complete receipts remain in handoff_progress; '
                 'reopen archived ranges through documents.read with the frozen SHA.'}}


def _uncovered(start, end, spans):
    remaining = [(start, end)]
    for covered_start, covered_end, _ in spans:
        remaining = [piece for a, b in remaining for piece in
                     ((a, min(b, covered_start)), (max(a, covered_end), b))
                     if piece[0] < piece[1]]
    return remaining


def _visible_signature(source_spans):
    """Stable source ranges even when overlapping calls split them differently."""
    signatures = []
    for source, spans in sorted(source_spans.items()):
        merged = []
        for start, end, text in sorted(spans):
            if merged and start == merged[-1][1]:
                a, _, before = merged[-1]
                merged[-1] = (a, end, before + text)
            else:
                merged.append((start, end, text))
        signatures.extend(sha([source, start, end, hashlib.sha256(text.encode('utf-8')).hexdigest()])
                          for start, end, text in merged)
    return signatures


def _merge_ranges(ranges):
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _author_required_ranges(context, documents):
    """Validate locators only; the host authenticates the recovery seal.

    The marker does not supply text, authorize a tool, or grant reading credit.
    Only exact actual read receipts over this same frozen ID/SHA can supply it.
    """
    marker = context.get(AUTHOR_READ)
    if marker is None:
        return None
    if (not isinstance(marker, dict) or type(marker.get('version')) is not int
            or marker['version'] != VERSION or marker.get('scientific_approval') is not False
            or marker.get('role', 'engineer') != 'engineer'
            or not isinstance(marker.get('document'), dict)
            or not isinstance(marker.get('required_ranges'), list) or not marker['required_ranges']):
        raise ValueError('Document window mandatory read marker is invalid')
    binding = marker['document']
    doc = documents.get(binding.get('document_id'))
    if not doc or binding.get('sha256') != doc['sha256']:
        raise ValueError('Document window mandatory read version is outside its frozen index')
    ranges = {}
    for row in marker['required_ranges']:
        if not isinstance(row, dict):
            raise ValueError('Document window mandatory read range is invalid')
        section = next((s for s in doc['sections'] if s['id'] == row.get('section_id')), None)
        start, end = row.get('start'), row.get('end')
        if (section is None or type(start) is not int or type(end) is not int
                or not section['start'] <= start < end <= section['end']
                or row.get('document_id', doc['document_id']) != doc['document_id']
                or row.get('sha256', doc['sha256']) != doc['sha256']
                or row.get('expected_sha256', doc['sha256']) != doc['sha256']):
            raise ValueError('Document window mandatory read range is outside its frozen section/version')
        ranges.setdefault(section['id'], []).append((start, end))
    return {'document': binding, 'ranges': {sid: _merge_ranges(rows) for sid, rows in ranges.items()}}


def _exact_read(doc, args, page):
    text = page['text']
    start, end = page.get('char_start'), page.get('char_end')
    section = next((s for s in doc['sections'] if s['id'] == page.get('section_id')), None)
    offset, limit = args.get('offset', 0), args.get('limit', 4000)
    if (type(start) is not int or type(end) is not int or section is None
            or type(offset) is not int or type(limit) is not int or not 1 <= limit <= DOCUMENT_LIMIT
            or args.get('section_id') != section['id']
            or not section['start'] <= start <= end <= section['end']
            or start != section['start'] + offset or end > start + limit or end - start != len(text)):
        raise ValueError('Document window receipt has an inconsistent exact range')
    return start, end


def _inherited_delivery_required_ranges(context, documents):
    """Validate host priority locators without supplying text or read credit."""
    marker = context.get(AUTHOR_INHERITED_DELIVERY)
    if marker is None:
        return None
    if (not isinstance(marker, dict) or type(marker.get('version')) is not int
            or marker['version'] != VERSION or marker.get('role') != 'engineer'
            or marker.get('scientific_approval') is not False
            or not isinstance(marker.get('required_documents'), list)
            or not marker['required_documents']):
        raise ValueError('Document window inherited delivery marker is invalid')
    required = []
    for entry in marker['required_documents']:
        if not isinstance(entry, dict):
            raise ValueError('Document window inherited delivery document is invalid')
        required.append(_author_required_ranges({AUTHOR_READ: {
            'version': VERSION, 'role': 'engineer', 'scientific_approval': False,
            'document': entry.get('document'), 'required_ranges': entry.get('required_ranges')}}, documents))
    return required


def _range_project(context, receipts):
    """Deduplicate physical source ranges without rewriting an actual receipt.

    Partial text lives in explicitly disposable fragments. Its parent retains
    the original exact-range reference, so this view cannot become read credit.
    Each fragment is a slice of one original read, never a stitched quotation.
    """
    from .document_working_notes import read_directories
    documents = {d['document_id']: d for d in read_directories(context)}
    retained = context['document_read_window_protocol'] == RETAINED_VERSION
    workflow = context.get('architecture_workflow_protocol') == 1
    mandatory = _author_required_ranges(context, documents)
    inherited = _inherited_delivery_required_ranges(context, documents)
    total_limit, document_limit = ((24000, 12000) if workflow and not inherited
                                   else (TOTAL_LIMIT, DOCUMENT_LIMIT))
    if retained:
        # Retain only actually read, deduplicated text from frozen versions.
        # Model input admission remains the hard limit: never silently evict
        # decisive earlier pages and ask the reviewer to read them in a loop.
        total_limit = sum(d['total_chars'] for d in documents.values())
        document_limit = max((d['total_chars'] for d in documents.values()), default=0)
    sources, limits = {}, {}
    for did, doc in documents.items():
        key = sha([doc.get('physical_memory_document_id', did), doc['sha256']])
        sources[did] = key
        limit = doc['total_chars'] if retained else min(document_limit, max(0, doc['total_chars'] - 1))
        limits[key] = min(limits.get(key, limit), limit)
    views = copy.deepcopy(receipts)
    used, per_document, per_source, spans = 0, {}, {}, {}
    retained_ranges = {}
    deduplicated, evicted = 0, 0
    positions = list(range(len(receipts) - 1, -1, -1))
    required = context.get('architecture_required_documents', []) if workflow else []
    # Prioritize actual scoped contract reads without copying source text or
    # changing a ledger position. Additional decisive evidence remains visible.
    def outside_required(position):
        row = receipts[position]
        if not isinstance(row, dict):
            return True
        call, page = row.get('call'), row.get('result')
        return not (isinstance(call, dict) and call.get('name') in {'documents.read', 'documents.search'}
                    and isinstance(page, dict) and page.get('document_id') in required)

    positions.sort(key=outside_required)
    if retained:
        # Search excerpts must not consume space needed by exact read evidence.
        positions.sort(key=lambda p: receipts[p].get('call', {}).get('name') == 'documents.search'
                       if isinstance(receipts[p], dict) else False)

    def retain(position, page, did, source, start, end):
        nonlocal used
        part = page['text'][start-page['char_start']:end-page['char_start']]
        spans.setdefault(source, []).append((start, end, part))
        retained_ranges.setdefault(position, []).append((start, end))
        used += len(part)
        per_source[source] = per_source.get(source, 0) + len(part)
        per_document[did] = per_document.get(did, 0) + len(part)

    # Reserve exact required intersections before recent optional pages/search
    # excerpts can spend their budget. Nothing is synthesized for unread gaps.
    if mandatory:
        did = mandatory['document']['document_id']
        source = sources[did]
        for position in positions:
            row = receipts[position]
            if not isinstance(row, dict):
                continue
            call, page = row.get('call', {}), row.get('result', {})
            if (not isinstance(call, dict) or call.get('name') != 'documents.read'
                    or not isinstance(page, dict) or page.get('error')
                    or page.get('document_id') != did or not isinstance(page.get('text'), str)):
                continue
            args = call.get('arguments', {})
            if (page.get('sha256') != documents[did]['sha256']
                    or args.get('document_id') != did
                    or args.get('expected_sha256') != documents[did]['sha256']):
                raise ValueError('Document window mandatory read receipt is outside its frozen index')
            start, end = _exact_read(documents[did], args, page)
            for a, b in mandatory['ranges'].get(page['section_id'], []):
                a, b = max(a, start), min(b, end)
                if a < b:
                    for left, right in _uncovered(a, b, spans.get(source, [])):
                        retain(position, page, did, source, left, right)
        required_chars = sum(b-a for rows in mandatory['ranges'].values() for a, b in rows)
        protected_read_chars = used
        if used > total_limit:
            raise DocumentReadWindowBudgetExceeded({
                'kind': 'mandatory_document_read_budget', 'document': copy.deepcopy(mandatory['document']),
                'required_range_chars': required_chars, 'protected_read_chars': used,
                'total_limit': total_limit, 'scientific_approval': False, 'request_dispatched': False})
        # The usual per-document allowance is advisory for this exact required
        # union. The total window ceiling and every ordinary source cap remain.
        limits[source] = max(limits[source], used)
    if inherited:
        inherited_source_ranges = {}
        for required_document in inherited:
            did = required_document['document']['document_id']
            source = sources[did]
            for ranges in required_document['ranges'].values():
                inherited_source_ranges.setdefault(source, []).extend(ranges)
            for position in positions:
                row = receipts[position]
                if not isinstance(row, dict):
                    continue
                call, page = row.get('call', {}), row.get('result', {})
                if (not isinstance(call, dict) or call.get('name') != 'documents.read'
                        or not isinstance(page, dict) or page.get('error')
                        or page.get('document_id') != did or not isinstance(page.get('text'), str)):
                    continue
                args = call.get('arguments', {})
                if (not isinstance(args, dict) or page.get('sha256') != documents[did]['sha256']
                        or args.get('document_id') != did
                        or args.get('expected_sha256') != documents[did]['sha256']):
                    raise ValueError('Document window inherited delivery receipt is outside its frozen index')
                start, end = _exact_read(documents[did], args, page)
                for a, b in required_document['ranges'].get(page['section_id'], []):
                    a, b = max(a, start), min(b, end)
                    if a < b:
                        for left, right in _uncovered(a, b, spans.get(source, [])):
                            retain(position, page, did, source, left, right)
            limits[source] = max(limits[source], per_source.get(source, 0))
        inherited_source_ranges = {source: _merge_ranges(ranges)
                                   for source, ranges in inherited_source_ranges.items()}
        inherited_required_chars = sum(b-a for ranges in inherited_source_ranges.values() for a, b in ranges)
        inherited_unread_chars = sum(right-left
            for source, ranges in inherited_source_ranges.items() for a, b in ranges
            for left, right in _uncovered(a, b, spans.get(source, [])))
        inherited_protected_chars = inherited_required_chars - inherited_unread_chars
        if used > total_limit:
            raise DocumentReadWindowBudgetExceeded({
                'kind': 'mandatory_inherited_delivery_budget',
                'required_documents': copy.deepcopy(context[AUTHOR_INHERITED_DELIVERY]['required_documents']),
                'required_range_chars': inherited_required_chars, 'protected_read_chars': used,
                'total_limit': total_limit, 'scientific_approval': False, 'request_dispatched': False})
    for position in positions:
        row = receipts[position]
        if not isinstance(row, dict):
            continue
        call, page = row.get('call', {}), row.get('result', {})
        if (not isinstance(call, dict) or call.get('name') not in {'documents.read', 'documents.search'}
                or not isinstance(page, dict) or page.get('error')):
            continue
        doc = documents.get(page.get('document_id'))
        args = call.get('arguments', {})
        if (not doc or page.get('sha256') != doc['sha256']
                or args.get('document_id') != doc['document_id']
                or args.get('expected_sha256') != doc['sha256']):
            raise ValueError('Document window receipt is outside its frozen index')
        did, source = doc['document_id'], sources[doc['document_id']]
        available = min(total_limit - used, limits[source] - per_source.get(source, 0))
        if call['name'] == 'documents.search':
            kept = []
            for hit in page.get('hits', []):
                excerpt = hit.get('excerpt', '')
                text = excerpt[:max(0, available)]
                kept.append({**copy.deepcopy(hit), 'excerpt': text,
                             'excerpt_truncated': len(text) != len(excerpt) or hit.get('excerpt_truncated', False)})
                available -= len(text)
                used += len(text)
                per_source[source] = per_source.get(source, 0) + len(text)
                per_document[did] = per_document.get(did, 0) + len(text)
            views[position]['result']['hits'] = kept
            continue
        text = page.get('text')
        if not isinstance(text, str):
            continue
        start, end = _exact_read(doc, args, page)
        covered = spans.setdefault(source, [])
        missing = _uncovered(start, end, covered)
        already_retained = sum(b-a for a, b in retained_ranges.get(position, []))
        deduplicated += len(text) - sum(b-a for a, b in missing) - already_retained
        added = 0
        for a, b in missing:
            b = min(b, a + max(0, available))
            if b <= a:
                continue
            retain(position, page, did, source, a, b)
            available -= b-a
            added += b-a
        evicted += sum(b-a for a, b in missing) - added

    for position in positions:
        row = receipts[position]
        if not isinstance(row, dict):
            continue
        call, page = row.get('call', {}), row.get('result', {})
        if (not isinstance(call, dict) or call.get('name') != 'documents.read'
                or not isinstance(page, dict) or page.get('error') or not isinstance(page.get('text'), str)):
            continue
        start, end = page['char_start'], page['char_end']
        kept = _merge_ranges(retained_ranges.get(position, []))
        if kept == [(start, end)]:
            continue  # The complete exact receipt remains visible verbatim.
        fragments = []
        for a, b in kept:
            part = page['text'][a-start:b-start]
            fragment = {k: copy.deepcopy(v) for k, v in page.items()
                        if k not in {'text', 'char_start', 'char_end', 'offset', 'next_offset',
                                     'truncated', 'source_pages', 'total_chars'}}
            fragment.update(text=part, char_start=a, char_end=b, text_returned=True,
                text_sha256=hashlib.sha256(part.encode('utf-8')).hexdigest(),
                primary_source_reading=False, scientific_claims_verified=False,
                provenance='exact_archived_read_prompt_fragment', untrusted=True)
            if 'source_pages' in page:
                fragment['source_pages'] = [copy.deepcopy(p) for p in page['source_pages']
                                            if p['start'] < b and p['end'] > a]
            fragments.append(fragment)
        views[position] = _reference(row, position)
        if fragments:
            views[position]['result']['document_read_fragments'] = fragments
    metadata = {
        'version': RETAINED_VERSION if retained else RANGE_VERSION, 'text_chars': used, 'total_limit': total_limit,
        'per_document_limit': document_limit, 'per_document_chars': per_document,
        'per_source_version_chars': per_source, 'deduplicated_read_chars': deduplicated,
        'evicted_read_chars': evicted, 'ledger_receipts': len(receipts), 'ledger_sha256': sha(receipts),
        'visible_receipt_sha256': _visible_signature(spans),
        'scope': 'Prompt view only. document_read_fragments contain exact subsets of one archived read, '
                 'with Unicode source locators; they do not replace the original receipt or grant read credit. '
                 'Unshown text and duplicate aliases remain recoverable via document_read_reference.reopen. '
                 'Complete original receipts remain unchanged in handoff_progress.'}
    if retained:
        if evicted:
            raise DocumentReadWindowBudgetExceeded({
                'kind': 'retained_document_read_budget', 'protected_read_chars': used + evicted,
                'total_limit': total_limit, 'scientific_approval': False, 'request_dispatched': False})
        metadata['retention_policy'] = 'all_exact_unique_reads; configured_model_input_capacity_is_hard_limit'
    if mandatory:
        metadata['mandatory_read_projection'] = {
            'document': copy.deepcopy(mandatory['document']), 'required_range_chars': required_chars,
            'protected_read_chars': protected_read_chars, 'unread_required_chars': required_chars - protected_read_chars,
            'required_ranges_sha256': sha(mandatory['ranges']), 'scientific_approval': False,
            'reading_credit_granted': False}
    if inherited:
        metadata['inherited_delivery_projection'] = {
            'required_read_chars': inherited_required_chars,
            'protected_read_chars': inherited_protected_chars,
            'unread_required_chars': inherited_unread_chars,
            'required_documents_sha256': sha(context[AUTHOR_INHERITED_DELIVERY]['required_documents']),
            'scientific_approval': False, 'reading_credit_granted': False}
    return {'tool_results': views, 'document_read_window': metadata}


def read_signature(context, receipts, call):
    """Track the requested range, not changes to unrelated visible pages.

    An evicted range can be reopened. Alternating other pages or changing a
    fully visible read's limit cannot manufacture a new progress signature.
    This is repetition control, never reading credit.
    """
    view = project(context, receipts)
    from .document_working_notes import read_directories
    args = call.get('arguments', {})
    doc = next((d for d in read_directories(context)
                if d['document_id'] == args.get('document_id')
                and d['sha256'] == args.get('expected_sha256')), None)
    section = next((s for s in (doc or {}).get('sections', [])
                    if s['id'] == args.get('section_id')), None)
    offset, limit = args.get('offset', 0), args.get('limit', 4000)
    if (not section or type(offset) is not int or type(limit) is not int
            or offset < 0 or limit <= 0):
        return sha(call)
    start = section['start'] + offset
    end = min(section['end'], start + min(limit, DOCUMENT_LIMIT))
    spans = []
    for row in view['tool_results']:
        page = row.get('result', {}) if isinstance(row, dict) else {}
        if (not isinstance(page, dict) or page.get('error')
                or page.get('document_id') != doc['document_id']
                or page.get('sha256') != doc['sha256']
                or page.get('section_id') != section['id']):
            continue
        for part in [page, *page.get('document_read_fragments', [])]:
            if not isinstance(part.get('text'), str) or not part['text']:
                continue
            a, b = part.get('char_start'), part.get('char_end')
            if type(a) is int and type(b) is int and a < b:
                a, b = max(a, start), min(b, end)
                if a < b:
                    spans.append((a, b))
    visible = _merge_ranges(spans)
    covered = start < end and visible == [(start, end)]
    target = [doc['document_id'], doc['sha256'], section['id'], offset,
              'already_visible' if covered else end]
    # Real author writes create a new repair step; unrelated reads do not.
    return sha([target, 'already_visible' if covered else visible,
                (context.get('implementation_document_base') or {}).get('spec_hash')])
