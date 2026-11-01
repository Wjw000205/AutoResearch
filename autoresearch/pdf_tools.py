"""Local PDF reading with exact, task-scoped citation handles.

Downloads and indexing are not reading receipts. Only completed pdf.read calls
can support notes; citation handles preserve raw extraction without fuzzy repair.
"""
from __future__ import annotations

import copy
import difflib
import hashlib
import json
import math
import re
from pathlib import Path
from urllib.parse import quote

from pypdf import PdfReader

from .storage import now

RECOVERY_TOOLS = ('pdf.info', 'pdf.search', 'pdf.read', 'pdf.ocr', 'pdf.analyze_figure', 'literature.record_notes')

_SEARCH_STOP_WORDS = frozenset('a an and are as at be by for from in is it of on or that the this to was were with'.split())


def _search_fold(text):
    """Normalize retrieval text while mapping every folded character to raw text."""
    parts, offsets, cursor = [], [], 0

    def append(start, end):
        for index in range(start, end):
            folded = text[index].casefold()
            parts.append(folded)
            offsets.extend([index] * len(folded))

    for match in re.finditer(r'(?<=[A-Za-z])\s*-\s*\n\s*(?=[a-z])', text):
        append(cursor, match.start())
        cursor = match.end()
    append(cursor, len(text))
    return ''.join(parts), offsets


def _search_pages(pages, query):
    terms = list(dict.fromkeys(term for term in re.findall(r'\w+', _search_fold(query)[0])
                               if term not in _SEARCH_STOP_WORDS))
    if not terms or not pages:
        return []
    patterns = {term: re.compile(re.escape(term) if re.search(r'[\u3400-\u9fff]', term)
                                else r'(?<!\w)' + re.escape(term) + r'(?!\w)') for term in terms}
    indexed = []
    for page in pages:
        folded, offsets = _search_fold(page['text'])
        hits = {term: list(pattern.finditer(folded)) for term, pattern in patterns.items()}
        indexed.append((page, offsets, hits, max(1, len(re.findall(r'\w+', folded)))))
    average_length = sum(row[3] for row in indexed) / len(indexed)
    mixed_query = any(not term.isdecimal() for term in terms)
    # Page-level BM25 saturates repeated terms and accounts for page length.
    # Standalone query numbers retain support, but a mixed method query must
    # not be dominated by reference numbers, years or equation counters.
    weights = {term: math.log1p((len(pages) - frequency + 0.5) / (frequency + 0.5))
               * (0.1 if mixed_query and term.isdecimal() else 1)
               for term in terms
               for frequency in [sum(bool(row[2][term]) for row in indexed)]}
    matches = []
    for page, offsets, hits, length in indexed:
        found = [term for term in terms if hits[term]]
        if not found:
            continue
        score = sum(weights[term] * len(hits[term]) * 2.2
                    / (len(hits[term]) + 1.2 * (0.25 + 0.75 * length / average_length)) for term in found)
        best = max(found, key=lambda term: weights[term])
        position = offsets[hits[best][0].start()]
        matches.append((page, round(score, 6), position))
    return sorted(matches, key=lambda match: (-match[1], match[0].get('page_number', match[0].get('page'))))


def recovery_focus(context):
    focus = context.get('pdf_reading_focus')
    return focus if context.get('pdf_recovery_protocol') == 2 and isinstance(focus, dict) else None


def validate_recovery_call(context, call, *, engine=None, run_id=None):
    """New focused assignments cannot drift back to stale source/quote repair."""
    focus = recovery_focus(context)
    if not focus:
        return
    name, args = call.get('name'), call.get('arguments', {})
    if name not in RECOVERY_TOOLS:
        raise ValueError('This PDF reading assignment requires pdf.read and citation_id notes; use the supplied focus read call.')
    if name.startswith('pdf.'):
        if args.get('source_id') != focus['source_id']:
            raise ValueError('Read only pdf_reading_focus.source_id in this assignment; completed papers are already preserved.')
    else:
        notes = args.get('notes')
        if not isinstance(notes, list) or len(notes) != 1:
            raise ValueError('Save one note for pdf_reading_focus.paper_id in this assignment.')
        note = notes[0]
        derived = False
        if engine and isinstance(note, dict) and note.get('source_id') != focus['source_id']:
            doc = engine.sources.document(run_id, note.get('source_id'))
            derived = doc.get('format') == 'ocr' and doc.get('parent_source_id') == focus['source_id']
        if not isinstance(note, dict) or note.get('paper_id') != focus['paper_id'] or (note.get('source_id') != focus['source_id'] and not derived):
            raise ValueError('Note must belong to the current pending paper and its exact local PDF source.')
        for field in ('excerpts', 'results'):
            rows = note.get(field)
            if not isinstance(rows, list) or any(not isinstance(row, dict) or not row.get('citation_id') for row in rows):
                raise ValueError('Use citation_id returned by pdf.read for every excerpt/result; do not retype PDF quotes.')


def recovery_error(context, output):
    focus = recovery_focus(context)
    if output.get('validation', {}).get('code') == 'note_too_large':
        return {**output, 'validation': {**output['validation'],
            **({'focus_paper_id': focus['paper_id']} if focus else {}),
            'action': 'resubmit_note',
            'instruction': 'The note was not saved. The 32000-character limit includes original quotes expanded from citation IDs. Select fewer decisive citations and numeric results, avoid repeating a long citation in multiple entries, and shorten analysis. Keep exact citation IDs, source ID and hash; do not edit original quotes or repeat completed reads. Preserve unresolved evidence in missing_information.'}}
    if output.get('validation', {}).get('code') == 'value_not_in_quote':
        return {**output, 'validation': {**output['validation'],
            **({'focus_paper_id': focus['paper_id']} if focus else {}),
            'action': 'resubmit_note',
            'instruction': 'The indicated results[].value must occur verbatim in its cited passage: for example value="2.7%", not "摘要报告降低 2.7%". Put narrative explanations in setting. Correct the result only if the already-read text supports it; otherwise use results:[] and record unresolved measurements in missing_information. Do not invent a measurement or claim verified results.'}}
    if not focus:
        return output
    if output.get('validation', {}).get('available_citations'):
        return {**output, 'validation': {**output['validation'], 'focus_paper_id': focus['paper_id'],
            'action': 'resubmit_note',
            'instruction': 'Correct only the indicated citation_id using an exact available_citations ID and resubmit the note. These passages already have completed read receipts; no repeat read is required.'}}
    return {**output, 'validation': {**output.get('validation', {}),
        'required_read': focus['read'][0],
        'focus_paper_id': focus['paper_id'],
        'quote_policy': 'Read the local PDF and use its citation_id. Exact raw quotes are resolved locally; do not retype or normalize them.'}}


def pending_note_correction(context, tool_results):
    """A repairable failed save is unfinished work, not a final deliverable."""
    focus = recovery_focus(context)
    if not focus and context.get('literature_notes_protocol') != 1:
        return None
    resolved = set()
    for item in reversed(tool_results):
        call, result = item.get('call', {}), item.get('result', {})
        if call.get('name') != 'literature.record_notes' or not isinstance(result, dict):
            continue
        notes = call.get('arguments', {}).get('notes', [])
        paper_ids = {note['paper_id'] for note in notes if isinstance(note, dict) and isinstance(note.get('paper_id'), str)}
        if focus:
            paper_ids &= {focus['paper_id']}
        pending = paper_ids - resolved
        if result.get('recorded_count', 0) > 0:
            resolved.update(paper_ids)
            continue
        if not pending:
            continue
        validation = result.get('validation', {})
        if result.get('error') and (not focus or validation.get('action') == 'resubmit_note'):
            return {'paper_id': sorted(pending)[0], 'validation': copy.deepcopy(validation), 'error': result['error'],
                'instruction': 'Your note has not been saved. Correct the indicated field and call literature.record_notes again before returning a final summary. Reuse the existing read receipts, exact source ID and hash. For PDF use exact citation IDs; for HTML use the saved source locator and a verbatim quote. Keep unsupported findings in missing_information; do not invent evidence or claim a successful save before receiving recorded_count.'}
    return None


def focused_recovery(engine, run_id, reading, diagnostics, sources, material):
    """Project current pending work only; historical failed candidates stay saved."""
    pending = set(reading['reading_gate']['pending_core_ids'])
    completed = set(reading['reading_gate']['noted_core_ids'])
    has_reads = {row['paper_id'] for row in material['read_ranges']}
    failed = {row['paper_id']: row for row in diagnostics['failed_notes'] if row['paper_id'] in pending}
    tasks = []
    for source in sources:
        if source.get('format') != 'pdf' or source.get('paper_id') not in pending or not source.get('source_id'):
            continue
        try:
            document, _ = LocalPDF(engine).original(run_id, source['source_id'])
        except (ValueError, OSError):
            continue  # Acquisition recovery remains available for missing/corrupt files.
        pages = set()
        failure = failed.get(source['paper_id'], {})
        for excerpt in failure.get('candidate_note', {}).get('excerpts', []):
            match = re.search(r'#page=([1-9][0-9]*)$', str(excerpt.get('locator', '')))
            if match:
                pages.add(int(match[1]))
        valid_pages = {p.get('page_number', p.get('page')) for p in document.get('pages', [])}
        pages = sorted(pages & valid_pages) or sorted(p for p in valid_pages if type(p) is int)[:1]
        if not pages:
            continue
        tasks.append({'paper_id': source['paper_id'], 'source_id': source['source_id'],
            'read': [{'name': 'pdf.read', 'arguments': {'source_id': source['source_id'], 'page': page,
                'expected_sha256': document['text_sha256']}} for page in pages],
            'then': '用pdf.search定位方法、架构与实验相关页，pdf.read查看后以citation_id保存当前论文笔记，明确未查看章节。'})
    # Do not let a repeated quote failure starve an entirely uninspected paper.
    tasks.sort(key=lambda row: (row['paper_id'] in has_reads, row['paper_id']))
    by_paper = {row['paper_id']: row for row in tasks}
    errors = []
    for paper_id, failure in failed.items():
        projected = {k: v for k, v in failure.items() if k != 'candidate_note'}
        if paper_id in by_paper:
            projected['validation'] = {**(failure.get('validation') or {}),
                'required_read': by_paper[paper_id]['read'][0],
                'quote_policy': 'Use citation_id returned by pdf.read; the rejected handwritten quote is historical only.'}
        errors.append(projected)
    return {'pdf_recovery_protocol': 2, 'pdf_reading_focus': tasks[0] if tasks else None,
        'pdf_reading_tasks': tasks[:1],
        'reading_work_queue': {'pending_core_ids': sorted(pending),
            'completed_note_index': [{k: note.get(k) for k in ('id', 'paper_id', 'source_id', 'text_sha256')}
                for note in reading['notes'] if note['paper_id'] in completed],
            'scope': 'Current locally validated state. Completed notes are retained; each assignment handles one pending PDF.'},
        'reading_repair_diagnostics': {**diagnostics, 'failed_notes': errors,
            'unusable_sources': [row for row in diagnostics['unusable_sources'] if row['paper_id'] in pending]}}


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class LocalPDF:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def original(self, run_id, source_id):
        # document validates ownership, path containment/reparse points, source
        # and original byte hashes. Never accept model-supplied filesystem paths.
        document = self.engine.sources.document(run_id, source_id)
        original = self.store.get('artifacts', document.get('original_artifact_id'))
        if document.get('format') != 'pdf' or not original or Path(original['path']).suffix.lower() != '.pdf':
            raise ValueError('No downloaded local PDF for this source; use papers.download first')
        return document, original

    def info(self, run_id, source_id):
        document, original = self.original(run_id, source_id)
        with Path(original['path']).open('rb') as stream:
            reader = PdfReader(stream)
            if reader.is_encrypted:
                raise ValueError('Encrypted PDF requires an accessible unencrypted source')
            count = len(reader.pages)
        return {'source_id': source_id, 'original_artifact_id': original['id'],
            'local_filename': original['name'], 'downloaded': True, 'reader': 'pypdf',
            'content_sha256': document['content_sha256'], 'text_sha256': document['text_sha256'],
            'pages_total': count, 'pages_indexed': len(document.get('pages', [])),
            'text_truncated': document.get('text_truncated', False),
            'pdf_url': f'/api/runs/{run_id}/pdf/file?source_id={quote(source_id, safe="")}',
            'pages': [{k: page.get(k) for k in ('page_number', 'start', 'end', 'empty', 'text_truncated')}
                      for page in document.get('pages', [])],
            'next_tool': {'name': 'pdf.read', 'arguments': {'source_id': source_id, 'page': 1}},
            'scope': 'Local file and extraction metadata only; this does not establish reading or scientific validity.'}

    def read(self, run_id, source_id, page, *, offset=0, limit=8000, expected_sha256=None):
        if type(page) is not int or page < 1 or type(offset) is not int or offset < 0:
            raise ValueError('PDF page must be a positive integer and page offset nonnegative')
        if type(limit) is not int or not 1 <= limit <= 16000:
            raise ValueError('PDF read limit must be between 1 and 16000 characters')
        document, original = self.original(run_id, source_id)
        if expected_sha256 is not None and expected_sha256 != document['text_sha256']:
            raise ValueError('PDF text SHA256 does not match the requested snapshot')
        matches = [p for p in document.get('pages', []) if p.get('page_number', p.get('page')) == page]
        if len(matches) != 1:
            raise ValueError('PDF page is outside the retained extraction; inspect pdf.info for available pages')
        retained = matches[0]
        with Path(original['path']).open('rb') as stream:
            reader = PdfReader(stream)
            if reader.is_encrypted or page > len(reader.pages):
                raise ValueError('Requested PDF page is unavailable')
            local_text = (reader.pages[page - 1].extract_text() or '').replace('\r\n', '\n').replace('\r', '\n')
        if not local_text.startswith(retained['text']) or (not retained.get('text_truncated') and local_text != retained['text']):
            raise ValueError('Local PDF extraction changed; refusing to mix source versions')
        if offset > len(retained['text']):
            raise ValueError('PDF offset exceeds the retained page text')
        start = retained['start'] + offset
        available = max(0, retained['end'] - start)
        # Standard source receipt shape is retained for the existing note gate.
        result = self.engine.sources.read(run_id, source_id, offset=start,
            limit=max(1, min(limit, available)), expected_sha256=document['text_sha256'])
        if not available:
            result.update(text='', page_sha256=hashlib.sha256(b'').hexdigest(), next_offset=None)
        text = result['text']
        citations, cursor = [], 0
        while cursor < len(text):
            end = min(len(text), cursor + 1100)
            if end < len(text):
                boundary = max(text.rfind('\n', cursor + 550, end), text.rfind('. ', cursor + 550, end))
                if boundary > cursor:
                    end = boundary + 1
            raw = text[cursor:end]
            if raw.strip():
                record = {'run_id': run_id, 'source_id': source_id, 'text_sha256': document['text_sha256'],
                    'content_sha256': document['content_sha256'], 'page': page,
                    'start': start + cursor, 'end': start + end, 'quote': raw,
                    'locator': f'{source_id}#chars={start + cursor}:{start + end}'}
                cid = f'{run_id}:pdf-citation:{_hash(record)}'
                self.store.mutate('pdf_citations', cid, lambda old, record=record, cid=cid:
                    old or {**record, 'id': cid, 'created_at': now()})
                citations.append({'citation_id': citation_alias({**record, 'id': cid}),
                    'canonical_citation_id': cid, 'locator': record['locator'],
                    'start': record['start'], 'end': record['end'], 'preview': raw[:100]})
            cursor = end
        return {**result, 'pdf_page': page, 'page_offset': offset,
            'next_page_offset': offset + len(text) if offset + len(text) < len(retained['text']) else None,
            'page_text_truncated': retained.get('text_truncated', False),
            'reader': 'pypdf', 'read_from': 'downloaded_local_pdf', 'original_artifact_id': original['id'],
            'needs_ocr': not local_text.strip(), 'citations': citations,
            'citation_usage': 'Use the short citation_id exactly as returned in excerpts:[{citation_id:...}]. The canonical_citation_id is retained for audit and legacy compatibility. Exact raw text and locator are resolved locally; never retype or normalize quotations.'}

    def search(self, run_id, source_id, query, *, limit=6):
        if not isinstance(query, str) or not query.strip():
            raise ValueError('PDF search query must contain 1 to 400 characters')
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError('PDF search limit must be between 1 and 10')
        document, _ = self.original(run_id, source_id)
        matches = []
        for page, score, position in _search_pages(document.get('pages', []), query)[:limit]:
            text = page['text']
            number = page.get('page_number', page.get('page'))
            matches.append({'page': number, 'score': score,
                'preview': text[max(0, position - 60):position + 220],
                'read': {'name': 'pdf.read', 'arguments': {'source_id': source_id, 'page': number,
                    'offset': max(0, position - 400), 'expected_sha256': document['text_sha256']}}})
        return {'source_id': source_id, 'query': query,
            'matches': matches,
            'text_sha256': document['text_sha256'], 'scope': 'Local indexed-page search only. Call pdf.read before citing; search is not a reading receipt.'}


def citation_alias(citation):
    """Short, exact handle, scoped by the note's source and full snapshot hash."""
    return f'p{citation["page"]}:{citation["start"]}:{citation["end"]}:{citation["id"].rsplit(":", 1)[-1][:8]}'


def _citation_error(engine, run_id, note, cid, path, *, code):
    from .literature_notes import LiteratureNoteValidationError, _read_receipts
    available = []
    try:
        document = engine.sources.document(run_id, note.get('source_id'))
        receipts = _read_receipts(engine.store, run_id, document, allow_empty=True)
        if document['text_sha256'] == note.get('text_sha256'):
            for row in engine.store.list('pdf_citations', run_id):
                if (row.get('source_id') == note.get('source_id') and row.get('text_sha256') == note.get('text_sha256')
                        and row.get('content_sha256') == document['content_sha256']
                        and type(row.get('start')) is int and type(row.get('end')) is int
                        and 0 <= row['start'] < row['end'] <= len(document['text'])
                        and document['text'][row['start']:row['end']] == row.get('quote')
                        and any(receipt['start'] <= row['start'] < row['end'] <= receipt['end'] for receipt in receipts)):
                    available.append({'citation_id': citation_alias(row), 'canonical_citation_id': row['id'],
                        'page': row['page'], 'start': row['start'], 'end': row['end'], 'preview': row['quote'][:100]})
    except (ValueError, TypeError, KeyError):
        available = []
    # Similarity orders suggestions only; it never changes the submitted ID or
    # accepts a near match. The caller must explicitly select a valid handle.
    available.sort(key=lambda row: max(difflib.SequenceMatcher(None, cid, row[field]).ratio()
                                      for field in ('citation_id', 'canonical_citation_id')), reverse=True)
    raise LiteratureNoteValidationError(
        f'PDF citation does not belong to this task/source snapshot ({path}); 请按此字段修正引用编号后重新保存。',
        path=path, code=code, details={'available_citations': available[:8],
            'instruction': 'Use an exact citation_id from available_citations; do not guess or regenerate identifiers.'})


def resolve_note_citations(engine, run_id, note, *, path='note'):
    """Expand only exact same-source handles, then use the unchanged proof gate."""
    value = copy.deepcopy(note)
    if not isinstance(value, dict):
        return value
    for field in ('excerpts', 'results'):
        rows = value.get(field)
        if not isinstance(rows, list):
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or 'citation_id' not in row:
                continue
            allowed = {'citation_id'} | ({'dataset', 'metric', 'value', 'setting'} if field == 'results' else set())
            if set(row) != allowed:
                raise ValueError('Citation handle cannot be mixed with supplied quote or locator fields')
            cid = row['citation_id']
            if not isinstance(cid, str):
                raise ValueError('PDF citation_id must be a string')
            citation = engine.store.get('pdf_citations', cid)
            if citation is None and re.fullmatch(r'p[1-9][0-9]*:[0-9]+:[0-9]+:[0-9a-f]{8}', cid):
                matches = [item for item in engine.store.list('pdf_citations', run_id)
                    if item.get('source_id') == value.get('source_id') and item.get('text_sha256') == value.get('text_sha256')
                    and citation_alias(item) == cid]
                citation = matches[0] if len(matches) == 1 else None
            if (not citation or citation.get('run_id') != run_id
                    or citation.get('source_id') != value.get('source_id')
                    or citation.get('text_sha256') != value.get('text_sha256')):
                _citation_error(engine, run_id, value, cid, f'{path}.{field}[{index}].citation_id',
                    code='unknown_pdf_citation' if citation is None else 'pdf_citation_snapshot_mismatch')
            document = engine.sources.document(run_id, value['source_id'])
            start, end = citation['start'], citation['end']
            if (not 0 <= start < end <= len(document['text']) or document['text'][start:end] != citation['quote']
                    or citation['content_sha256'] != document['content_sha256']
                    or citation['locator'] != f'{value["source_id"]}#chars={start}:{end}'):
                raise ValueError('PDF citation no longer matches its immutable local source')
            rows[index] = {k: v for k, v in row.items() if k != 'citation_id'} | {
                'locator': citation['locator'], 'quote': citation['quote']}
    return value
