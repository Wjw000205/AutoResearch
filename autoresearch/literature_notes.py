"""Task-scoped AI extraction notes grounded in retained, actually inspected text.

Matching a quotation establishes its provenance only. It does not establish the
paper's scientific claims or that the whole document has been read. Source text
is untrusted data; this module neither executes it nor interprets instructions.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
from types import SimpleNamespace

from .evidence_tools import SourceEvidenceService
from .literature_reading import _active, _run_papers
from .storage import Store, RunDeletedError, now


NOTE_FIELDS = {'paper_id', 'source_id', 'text_sha256', 'summary', 'research_question',
               'method', 'datasets', 'baselines', 'results', 'limitations', 'relevance',
               'excerpts', 'missing_information'}
RESULT_FIELDS = {'dataset', 'metric', 'value', 'setting', 'locator', 'quote'}
EXCERPT_FIELDS = {'locator', 'quote'}
READING_STATUSES = {'pdf_text_indexed', 'web_text_indexed'}
SCOPE_NOTE = ('笔记由智能体从已检查片段提取。引文与原文及工具阅读回执已对应，'
              '不代表整篇文献已读完，也不代表科学结论已经验证。')


class LiteratureNoteValidationError(ValueError):
    """Safe, actionable validation details; never a rewritten or accepted quote."""
    def __init__(self, message, *, path, code, details=None):
        super().__init__(message)
        self.details = {'path': path, 'code': code, **(details or {})}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _text(value, label, *, limit=2400, chinese=False):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label} must be nonempty text')
    if chinese and not re.search(r'[\u3400-\u9fff]', value):
        raise ValueError(f'{label} must use Chinese prose; keep identifiers and measurements unchanged')
    return value


def _strings(value, label, *, chinese=False):
    if not isinstance(value, list) or len(value) > 32:
        raise ValueError(f'{label} must be a list of at most 32 strings')
    return [_text(item, label, limit=1200, chinese=chinese) for item in value]


def _schema(note, path='notes[0]'):
    if not isinstance(note, dict) or set(note) != NOTE_FIELDS:
        raise ValueError('Each literature note must contain exactly the documented note fields')
    for key in ('paper_id', 'source_id'):
        _text(note[key], key, limit=500)
    if not isinstance(note['text_sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', note['text_sha256']):
        raise ValueError('text_sha256 must be the exact lowercase retained text SHA256')
    for key in ('summary', 'research_question', 'method', 'relevance'):
        _text(note[key], key, chinese=True)
    for key in ('datasets', 'baselines'):
        _strings(note[key], key)
    for key in ('limitations', 'missing_information'):
        _strings(note[key], key, chinese=True)
    for key, fields in (('results', RESULT_FIELDS), ('excerpts', EXCERPT_FIELDS)):
        rows = note[key]
        if not isinstance(rows, list) or len(rows) > 12 or (key == 'excerpts' and not rows):
            raise ValueError(f'{key} must contain {"1 to" if key == "excerpts" else "0 to"} 12 entries')
        for index, row in enumerate(rows):
            try:
                if not isinstance(row, dict) or set(row) != fields:
                    raise ValueError(f'Each {key} entry must contain exactly its documented fields')
                for field in fields:
                    _text(row[field], f'{key}.{field}', limit=3200 if field == 'quote' else 1200,
                          chinese=field == 'setting')
                if key == 'results' and not _value_in_quote(row['value'], row['quote']):
                    raise LiteratureNoteValidationError('Each result value must occur unchanged in its cited quote',
                        path=f'{path}.{key}[{index}]', code='value_not_in_quote')
            except LiteratureNoteValidationError:
                raise
            except ValueError as exc:
                raise LiteratureNoteValidationError(str(exc), path=f'{path}.{key}[{index}]', code='invalid_structure') from exc
    if not note['results'] and not note['missing_information']:
        raise ValueError('Notes without numeric results must explain missing_information')
    normalized = copy.deepcopy(note)
    for field in ('excerpts', 'results'):
        for row in normalized[field]:
            # Explicit relative references bind to this note's exact source.
            # A different complete source ID is never guessed or corrected.
            if re.fullmatch(r'#(?:page=[1-9][0-9]*|chars=[0-9]+:[0-9]+)', row['locator']):
                row['locator'] = note['source_id'] + row['locator']
    return normalized


def _space(value):
    return ' '.join(value.split())


def _value_in_quote(value, quote):
    pattern = re.escape(_space(value))
    if re.search(r'\d', value):
        # A sentence-ending period is punctuation, not an extra decimal digit.
        pattern = r'(?<![\d.])' + pattern + r'(?!\d|\.\d)'
    return re.search(pattern, _space(quote)) is not None


def _reading(store, run_id, note, document, *, _papers=None, _links=None):
    from .paper_bindings import readings_for, same_source
    from .paper_identity import task_paper
    paper = task_paper(store, run_id, note['paper_id'], _papers)
    if not paper:
        raise ValueError('Paper has no task-owned read_papers acquisition receipt')
    candidates = readings_for(store, run_id, note['paper_id'],
                              _papers=_papers, _links=_links)
    if not candidates:
        raise ValueError('Paper has no task-owned read_papers acquisition receipt')
    for evidence, row in candidates:
        if (isinstance(row, dict) and row.get('text_sha256') == note['text_sha256']
                and row.get('content_sha256') == document.get('content_sha256')
                and row.get('identity_verified') is True
                and row.get('retrieval_status') in READING_STATUSES
                and same_source(store, run_id, row.get('source_id'), note['source_id'], document,
                                **({'_papers': _papers} if _papers is not None else {}))):
            return row, ('run_evidence', evidence['id'], _digest(evidence))
    raise ValueError('No successful read_papers receipt matches this paper and retained document hash')


def _read_events(store, run_id):
    """Read complete receipt history, never the recent-events display window."""
    with store._db() as db:
        rows = db.execute("SELECT * FROM events WHERE run_id=? AND kind='tool.completed' "
                          "AND message IN ('sources.read','pdf.read') ORDER BY id", (run_id,)).fetchall()
    return [{**dict(row), 'data': json.loads(row['data'])} for row in rows]


def _read_receipts(store, run_id, document, *, allow_empty=False, _events=None, _papers=None):
    """Validate real receipts; projections may supply their same-snapshot events."""
    receipts = []
    for event in _read_events(store, run_id) if _events is None else _events:
        data = event['data']
        result = data.get('result') if isinstance(data, dict) else None
        if not isinstance(data, dict) or data.get('role') != 'scout' or not isinstance(result, dict):
            continue
        text, offset = result.get('text'), result.get('offset')
        from .paper_bindings import same_source
        # Reject other versions before resolving aliases. Historical reads for
        # every other paper must not each rescan the task's complete catalogue.
        if (result.get('run_id') != run_id
                or result.get('text_sha256') != document['text_sha256']
                or result.get('error') or not isinstance(text, str) or not text
                or type(offset) is not int or offset < 0 or len(text) > 16000
                or result.get('total_chars') != len(document['text'])
                or document['text'][offset:offset + len(text)] != text
                or result.get('page_sha256') != hashlib.sha256(text.encode('utf-8')).hexdigest()
                or not same_source(store, run_id, result.get('source_id'), document['source_id'], document,
                                   **({'_papers': _papers} if _papers is not None else {}))):
            continue
        receipts.append({'id': event['id'], 'start': offset, 'end': offset + len(text),
                         'event_sha256': _digest(event)})
    if not receipts and not allow_empty:
        raise ValueError('No completed scout sources.read receipt matches this retained document')
    return receipts


def _citation_error(note, document, receipts, path, code, message, read_span=None):
    ranges = []
    for receipt in sorted(receipts, key=lambda row: (row['start'], row['end'])):
        if ranges and receipt['start'] <= ranges[-1]['end']:
            ranges[-1]['end'] = max(ranges[-1]['end'], receipt['end'])
        else:
            ranges.append({'start': receipt['start'], 'end': receipt['end']})
    length = len(document['text'])
    start, end = read_span if read_span is not None else (0, min(length, 8000))
    details = {'source_id': note['source_id'], 'text_sha256': document['text_sha256'],
        'available_pages': [page.get('page_number', page.get('page')) for page in document.get('pages', [])],
        'valid_character_range': {'start': 0, 'end': length}, 'read_ranges': ranges,
        'accepted_locator_forms': ['#page=N', '#chars=S:E', 'exact source_id followed by either form'],
        'quote_policy': 'Copy exact source text; only whitespace differences are accepted. No fuzzy correction or source ID guessing.'}
    if 0 <= start < end <= length:
        details['required_read'] = {'name': 'sources.read', 'arguments': {'source_id': note['source_id'],
            'offset': start, 'limit': min(16000, end - start), 'expected_sha256': document['text_sha256']}}
        details['inspection_range'] = {'start': start, 'end': end}
        details['follow_next_offset'] = end - start > 16000
    return LiteratureNoteValidationError(message, path=path, code=code, details=details)


def _quote_proof(note, excerpt, document, receipts, *, path='notes[0].excerpts[0]'):
    source_id = note['source_id']
    locator, quote = excerpt['locator'], excerpt['quote']
    page_number = None
    from .paper_bindings import same_source
    store = document.get('_binding_store')
    prefix = locator.rsplit('#', 1)[0]
    if prefix != source_id and (store is None or not same_source(store, note.get('_binding_run_id'), prefix, source_id, document,
            **({'_papers': document['_binding_papers']} if '_binding_papers' in document else {}))):
        raise _citation_error(note, document, receipts, path, 'invalid_locator', 'Citation does not bind to this paper source version')
    match = re.fullmatch(re.escape(prefix) + r'#page=([1-9][0-9]*)', locator)
    if match:
        page_number = int(match[1])
        pages = [page for page in document.get('pages', [])
                 if page.get('page_number', page.get('page')) == page_number]
        if len(pages) != 1:
            raise _citation_error(note, document, receipts, path, 'invalid_locator',
                                  'Citation page is absent or ambiguous in the retained extraction')
        start, end = pages[0]['start'], pages[0]['end']
    else:
        match = re.fullmatch(re.escape(prefix) + r'#chars=([0-9]+):([0-9]+)', locator)
        if not match:
            raise _citation_error(note, document, receipts, path, 'invalid_locator',
                                  'locator must be source_id#page=N or source_id#chars=S:E')
        start, end = map(int, match.groups())
        if not 0 <= start < end <= len(document['text']):
            raise _citation_error(note, document, receipts, path, 'invalid_locator',
                                  'Citation character range is outside retained source text')
    pattern = r'\s+'.join(re.escape(part) for part in quote.split())
    first_match = None
    for matched in re.finditer(pattern, document['text'][start:end]):
        quote_start, quote_end = start + matched.start(), start + matched.end()
        if first_match is None:
            first_match = (quote_start, quote_end)
        cursor, used = quote_start, []
        for receipt in sorted(receipts, key=lambda row: (row['start'], -row['end'], row['id'])):
            if receipt['start'] <= cursor < receipt['end']:
                used.append(receipt)
                cursor = receipt['end']
                if cursor >= quote_end:
                    return {'locator': locator, 'quote': quote, 'char_start': quote_start,
                            'char_end': quote_end, 'page_number': page_number,
                            'source_read_event_ids': [row['id'] for row in used]}
    raise _citation_error(note, document, receipts, path, 'not_read' if first_match else 'quote_not_in_locator',
        'Quote must match its locator and be fully covered by completed scout sources.read receipts',
        first_match or (start, end))


def validate_notes(engine, run_id, notes, *, require_active=True, allowed_read_event_ids=None,
                   _projection=None, handoff_id=None, allowed_delivery_receipt_ids=None):
    """Validate a bounded batch without writes; return records and commit anchors."""
    if not isinstance(notes, list) or not 1 <= len(notes) <= 6:
        raise ValueError('notes must contain 1 to 6 literature notes')
    normalized = []
    for index, note in enumerate(notes):
        try:
            from .pdf_tools import resolve_note_citations
            note = resolve_note_citations(engine, run_id, note, path=f'notes[{index}]')
            normalized.append(_schema(note, path=f'notes[{index}]'))
        except LiteratureNoteValidationError:
            raise
        except ValueError as exc:
            raise LiteratureNoteValidationError(str(exc), path=f'notes[{index}]', code='invalid_structure') from exc
    if require_active:
        _active(engine, run_id)
    elif engine.store.get('run_deletions', run_id):
        raise RunDeletedError('Research is being deleted or has been deleted')
    elif not engine.store.get('runs', run_id):
        raise ValueError('Research not found')
    papers = _projection.papers if _projection is not None else _run_papers(engine.store, run_id)
    from .paper_identity import task_paper
    prepared = []
    for note_index, note in enumerate(normalized):
        paper = task_paper(engine.store, run_id, note['paper_id'], papers)
        if not paper:
            raise ValueError('Every noted paper must be identity-verified in this research task')
        document = engine.sources.document(run_id, note['source_id'])
        if document['text_sha256'] != note['text_sha256']:
            raise ValueError('Note text_sha256 does not match the retained source snapshot')
        try:
            reading, anchor = (_projection.reading(note, document) if _projection is not None
                               else _reading(engine.store, run_id, note, document))
        except ValueError as exc:
            raise LiteratureNoteValidationError(str(exc), path=f'notes[{note_index}]', code='acquisition_mismatch',
                details={'paper_id': note['paper_id'], 'source_id': note['source_id'],
                    'next_step': 'For an alternate arXiv version, call literature.link_source with these IDs; '
                                 'it independently checks the registry identity. Other sources remain separate evidence. '
                                 'Then inspect passages and retry the note; do not change source IDs or hashes.'}) from exc
        receipts = (_projection.receipts(document) if _projection is not None
                    else _read_receipts(engine.store, run_id, document, allow_empty=True))
        if allowed_read_event_ids is not None:
            receipts = [row for row in receipts if row['id'] in allowed_read_event_ids]
        citation_document = {**document, '_binding_store': engine.store}
        if _projection is not None:
            citation_document['_binding_papers'] = _projection.papers
        citation_note = {**note, '_binding_run_id': run_id}
        citations = [_quote_proof(citation_note, excerpt, citation_document, receipts,
                                 path=f'notes[{note_index}].{kind}[{index}]') | {'kind': kind, 'index': index}
                     for kind in ('excerpts', 'results') for index, excerpt in enumerate(note[kind])]
        delivery = {}
        delivery_anchors = []
        from .evidence_delivery import tracking_required, delivery_spans, covering_receipts
        if allowed_delivery_receipt_ids is not None or tracking_required(engine.store, run_id, handoff_id):
            delivered = delivery_spans(engine.store, run_id, 'scout', note['source_id'], note['text_sha256'],
                handoff_id=handoff_id, text=document['text'],
                aliases=[document[key] for key in ('source_alias_id', 'canonical_source_id') if document.get(key)])
            if allowed_delivery_receipt_ids is not None:
                delivered = [row for row in delivered if row.get('id') in allowed_delivery_receipt_ids]
            selected = {}
            for citation in citations:
                used = covering_receipts(delivered, citation['char_start'], citation['char_end'])
                if not used:
                    error = _citation_error(note, document, receipts, f'notes[{note_index}]',
                        'evidence_not_delivered',
                        'Quoted source text was stored/retrieved but not delivered to an acknowledged scout request',
                        (citation['char_start'], citation['char_end']))
                    error.details.update(recoverable=True, delivery_required=True, handoff_id=handoff_id,
                        next_step='Include the exact read range in a scout model request before resubmitting this note.')
                    raise error
                selected.update({row['id']: row for row in used})
            delivery = {'evidence_delivery_status': 'delivered', 'evidence_handoff_id': handoff_id,
                'evidence_request_ids': sorted({row['request_id'] for row in selected.values()}),
                'evidence_delivery_receipt_ids': sorted(selected), 'reading_compatibility': 'request_delivery_v1'}
            for row in selected.values():
                delivery_anchors.append(('evidence_deliveries', row['id'],
                    _digest(engine.store.get('evidence_deliveries', row['id']))))
                manifest = engine.store.get('context_manifests', row.get('manifest_id', row['request_id']))
                if manifest is not None:
                    delivery_anchors.append(('context_manifests', manifest['id'], _digest(manifest)))
        used_ids = sorted({identifier for citation in citations for identifier in citation['source_read_event_ids']})
        note_hash = _digest([run_id, note])
        record = {**note, 'id': f'{run_id}:literature-note:{note_hash}', 'run_id': run_id,
                  'role': 'scout', 'note_sha256': note_hash, 'created_at': now(),
                  'generation': 'ai_extraction', 'reading_scope': 'inspected_passages',
                  'scientific_claims_verified': False, 'document_identity_verified': False,
                  'paper_identity_verified': True, 'source_untrusted': True,
                  'source_text_truncated': document.get('text_truncated', False),
                  'content_sha256': document.get('content_sha256'),
                  'retrieval_status': reading['retrieval_status'], 'citations': citations,
                  'source_read_event_ids': used_ids,
                  'source_read_receipts': [row for row in receipts if row['id'] in used_ids],
                  'scope_note': SCOPE_NOTE, **delivery}
        if paper.get('identity_kind') == 'uploaded_document':
            record.update(identity_kind='uploaded_document', content_integrity_verified=True,
                bibliographic_identity_verified=paper.get('bibliographic_identity_verified') is True,
                identity_scope='用户选定上传原文的字节身份；不表示发表身份或科学结论已核验。')
        if document.get('ocr_performed'):
            record.update(ocr_performed=True, source_transcription='machine_ocr',
                parent_source_id=document.get('parent_source_id'), ocr_image=document.get('ocr_image'),
                ocr_warning=document.get('ocr_warning'))
        physical_id = document.get('source_alias_id', note['source_id'])
        artifact = engine.store.get('artifacts', physical_id)
        anchors = [anchor, ('artifacts', physical_id, _digest(artifact)), *delivery_anchors]
        if physical_id != note['source_id']:
            from .paper_identity import digest
            binding_key = digest([run_id, physical_id])
            anchors.append(('paper_source_bindings', binding_key, _digest(engine.store.get('paper_source_bindings', binding_key))))
        prepared.append({'record': record, 'paper_sha256': _digest(paper),
                         'anchors': anchors,
                         'read_receipts': [row for row in receipts if row['id'] in used_ids]})
    if len({item['record']['id'] for item in prepared}) != len(prepared):
        raise ValueError('A literature note batch cannot repeat the same note')
    if require_active:
        _active(engine, run_id)
    return prepared


class _BufferedMemoryStore:
    def __init__(self):
        self.records = []

    def put(self, collection, key, value):
        if collection != 'memory':
            raise ValueError('Literature note indexing may only stage memory records')
        self.records.append((collection, key, value))
        return value


class _TransactionView:
    def __init__(self, db):
        self.db = db

    def get(self, collection, key, default=None):
        row = self.db.execute('SELECT data FROM records WHERE collection=? AND key=?', (collection, key)).fetchone()
        return json.loads(row[0]) if row else default

    def list(self, collection, run_id):
        rows = self.db.execute('SELECT data FROM records WHERE collection=? ORDER BY updated_at,key', (collection,))
        return [value for row in rows if (value := json.loads(row['data'])).get('run_id') == run_id]


def _memory_text(note):
    lines = [f'文献笔记：{note["paper_id"]}', f'摘要：{note["summary"]}',
             f'研究问题：{note["research_question"]}', f'方法：{note["method"]}',
             '数据集：' + '、'.join(note['datasets']), '比较基线：' + '、'.join(note['baselines'])]
    for result in note['results']:
        lines.append(f'结果卡：数据集 {result["dataset"]}；指标 {result["metric"]}；原值 {result["value"]}；'
                     f'设置 {result["setting"]}；来源 {result["locator"]}；原文 {result["quote"]}')
    lines += ['局限：' + '；'.join(note['limitations']), f'与当前研究的关系：{note["relevance"]}',
              '缺失信息：' + '；'.join(note['missing_information']), SCOPE_NOTE]
    return '\n'.join(lines)


def record_notes(engine, intelligence, run_id, notes, *, handoff_id=None):
    """Atomically persist immutable notes and memory_add's searchable chunks."""
    prepared = validate_notes(engine, run_id, notes, handoff_id=handoff_id)
    staging = _BufferedMemoryStore()
    indexer = copy.copy(intelligence)
    indexer.store = staging
    for item in prepared:
        note = item['record']
        metadata = {key: note[key] for key in ('paper_id', 'source_id', 'text_sha256', 'content_sha256',
            'generation', 'reading_scope', 'scientific_claims_verified', 'document_identity_verified',
            'source_text_truncated', 'source_read_event_ids', 'citations')}
        metadata.update(kind='literature_note', note_id=note['id'], provenance='ai_extraction', shared=False)
        memory = indexer.memory_add(run_id, 'scout', _memory_text(note), metadata)
        note.update(memory_document_id=memory['document_id'], memory_chunk_ids=memory['chunk_ids'])
    _active(engine, run_id)
    saved = []
    with engine.store._db() as db:
        db.execute('BEGIN IMMEDIATE')
        engine.store._guard_run(db, run_id)
        run_row = db.execute("SELECT data FROM records WHERE collection='runs' AND key=?", (run_id,)).fetchone()
        if not run_row or json.loads(run_row['data']).get('status') != 'running':
            raise asyncio.CancelledError('Research is no longer active; literature notes were not saved')
        papers = _run_papers(_TransactionView(db), run_id)
        for item in prepared:
            note = item['record']
            from .paper_identity import task_paper
            if _digest(task_paper(_TransactionView(db), run_id, note['paper_id'], papers)) != item['paper_sha256']:
                raise ValueError('Task paper verification changed while notes were being prepared')
            for collection, key, expected in item['anchors']:
                row = db.execute('SELECT data FROM records WHERE collection=? AND key=?', (collection, key)).fetchone()
                if not row or _digest(json.loads(row['data'])) != expected:
                    raise ValueError('Source acquisition evidence changed while notes were being prepared')
            for receipt in item['read_receipts']:
                row = db.execute('SELECT * FROM events WHERE run_id=? AND id=?', (run_id, receipt['id'])).fetchone()
                if not row or _digest({**dict(row), 'data': json.loads(row['data'])}) != receipt['event_sha256']:
                    raise ValueError('Source reading receipt changed while notes were being prepared')
        for collection, key, value in staging.records + [('literature_notes', item['record']['id'], item['record']) for item in prepared]:
            engine.store._guard_write(db, collection, key, value)
            row = db.execute('SELECT data FROM records WHERE collection=? AND key=?', (collection, key)).fetchone()
            if row:
                existing = json.loads(row['data'])
                if _digest({k: v for k, v in existing.items() if k != 'created_at'}) != _digest({k: v for k, v in value.items() if k != 'created_at'}):
                    raise ValueError('Immutable literature note or memory identity collision')
                value = existing
            else:
                db.execute('INSERT INTO records VALUES(?,?,?,?)',
                           (collection, key, json.dumps(value, ensure_ascii=False, allow_nan=False), now()))
            if collection == 'literature_notes':
                saved.append(value)
    rag = getattr(engine, 'paper_rag', None)
    publication = rag.publish(engine, run_id, saved) if rag else None
    return {'notes': saved, 'recorded_count': len(saved), 'reading_scope': 'inspected_passages',
            **({'shared_rag': publication} if publication is not None else {}),
            'scientific_claims_verified': False, 'scope_note': SCOPE_NOTE}


def notes_view(store, run_id, *, document_loader=None):
    """Return only revalidated task notes, without writes or a running-state gate.

    The default uses the application's conventional data directory beside its
    SQLite store. Engines with another source root supply sources.document.
    Missing/tampered source files, receipts or note contents never count as read.
    """
    if isinstance(store, Store) and not getattr(store, 'is_read_snapshot', False):
        with store.read_snapshot() as snapshot:
            owner = getattr(document_loader, '__self__', None)
            loader = document_loader
            if isinstance(owner, SourceEvidenceService) and owner.store is store:
                reader = copy.copy(owner)
                reader.store = snapshot
                loader = reader.document
            result = _notes_view(snapshot, run_id, document_loader=loader)
        # Deletion can finish while the isolated reader examines originals.
        # Never return an old snapshot after the task has been deleted.
        if store.get('run_deletions', run_id):
            raise RunDeletedError('Research is being deleted or has been deleted')
        if not store.get('runs', run_id):
            raise ValueError('Research not found')
        return result
    return _notes_view(store, run_id, document_loader=document_loader)


class _NoteProjection:
    """One read-only request's exact proofs, discarded after rendering.

    Each retained file is fully checked by the normal document loader and each
    receipt by the normal proof validator. Only repeated work in this SQLite
    snapshot is reused; no process cache, TTL, persisted verification or new
    scientific/reading credit is created. Writes still call validate_notes
    without this projection and recheck their commit anchors transactionally.
    """
    def __init__(self, store, run_id, loader):
        if not getattr(store, 'is_read_snapshot', False):
            raise TypeError('Note projection requires a read-only snapshot')
        self.store, self.run_id, self.loader = store, run_id, loader
        self._documents, self._readings, self._receipts = {}, {}, {}
        self._papers, self._events, self._links = None, None, None

    @property
    def papers(self):
        if self._papers is None:
            self._papers = _run_papers(self.store, self.run_id)
        return self._papers

    def document(self, run_id, source_id):
        if run_id != self.run_id:
            raise ValueError('Note projection belongs to another research')
        if source_id not in self._documents:
            from .paper_bindings import _using_task_papers
            with _using_task_papers(self.store, self.run_id, self.papers):
                self._documents[source_id] = self.loader(run_id, source_id)
        return copy.deepcopy(self._documents[source_id])

    def reading(self, note, document):
        key = (note['paper_id'], note['source_id'], note['text_sha256'], document.get('content_sha256'))
        if key not in self._readings:
            if self._links is None:
                self._links = self.store.list('run_evidence', self.run_id)
            self._readings[key] = _reading(self.store, self.run_id, note, document,
                                          _papers=self.papers, _links=self._links)
        return self._readings[key]

    def receipts(self, document):
        key = (document['source_id'], document['text_sha256'], document.get('content_sha256'))
        if key not in self._receipts:
            if self._events is None:
                self._events = {}
                for event in _read_events(self.store, self.run_id):
                    data = event['data']
                    result = data.get('result') if isinstance(data, dict) else None
                    if isinstance(result, dict) and isinstance(result.get('text_sha256'), str):
                        self._events.setdefault(result['text_sha256'], []).append(event)
            self._receipts[key] = _read_receipts(self.store, self.run_id, document, allow_empty=True,
                _events=self._events.get(document['text_sha256'], []), _papers=self.papers)
        return self._receipts[key]


def _notes_view(store, run_id, *, document_loader=None):
    if store.get('run_deletions', run_id):
        raise RunDeletedError('Research is being deleted or has been deleted')
    if not store.get('runs', run_id):
        raise ValueError('Research not found')
    loader = document_loader or SourceEvidenceService(store, store.path.parent).document
    root = getattr(getattr(loader, '__self__', None), 'data_dir', store.path.parent)
    projection = _NoteProjection(store, run_id, loader) if getattr(store, 'is_read_snapshot', False) else None
    effective_loader = projection.document if projection is not None else loader
    engine = SimpleNamespace(store=store, sources=SimpleNamespace(document=effective_loader))
    valid = []
    for note in store.list('literature_notes', run_id):
        try:
            if (note.get('role') != 'scout' or note.get('generation') != 'ai_extraction'
                    or note.get('reading_scope') != 'inspected_passages'
                    or note.get('scientific_claims_verified') is not False
                    or note.get('document_identity_verified') is not False
                    or note.get('paper_identity_verified') is not True):
                continue
            event_ids = note.get('source_read_event_ids')
            if not isinstance(event_ids, list) or not event_ids or any(type(identifier) is not int for identifier in event_ids):
                continue
            original = {key: note[key] for key in NOTE_FIELDS}
            checked = validate_notes(engine, run_id, [original], require_active=False,
                allowed_read_event_ids=event_ids, _projection=projection,
                handoff_id=note.get('evidence_handoff_id'),
                allowed_delivery_receipt_ids=note.get('evidence_delivery_receipt_ids'))[0]['record']
            bound_fields = ('id', 'note_sha256', 'citations', 'source_read_event_ids', 'source_read_receipts',
                            'content_sha256', 'source_text_truncated', 'retrieval_status')
            if any(note.get(field) != checked[field] for field in bound_fields):
                continue
            if any(note.get(field) != checked.get(field) for field in
                   ('ocr_performed', 'source_transcription', 'parent_source_id', 'ocr_image', 'ocr_warning',
                    'evidence_delivery_status', 'evidence_handoff_id', 'evidence_request_ids',
                    'evidence_delivery_receipt_ids', 'reading_compatibility')):
                continue
            valid.append(note)
        except RunDeletedError:
            raise
        except (ValueError, KeyError, TypeError, OSError):
            continue
    from .paper_rag import PaperRAG
    valid.extend(PaperRAG(store, root).reused_notes(run_id, effective_loader))
    return valid


def valid_paper_ids(engine, run_id):
    """Paper IDs with preserved extraction notes and currently valid source proofs."""
    from .paper_identity import noted_papers
    return noted_papers(engine.store, run_id, notes_view(engine.store, run_id,
                         document_loader=engine.sources.document))
