"""Bounded retrieval/indexing of papers already identity-verified for one task."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from urllib.parse import urlsplit

import httpx

from .lab import SourceUnavailableError, arxiv_version_match, source_text_issue
from .storage import RunDeletedError, now


NOTE = ('Retrieval and indexing do not establish that an AI read this paper, that the downloaded document '
        'matches its bibliographic identity, or that its scientific claims are verified. Use memory.search '
        'and sources.read to inspect retained text; extraction and coverage limits remain explicit.')


def _active(engine, run_id):
    if engine.store.get('run_deletions', run_id):
        raise RunDeletedError('Research is being deleted or has been deleted')
    if not engine.store.get('runs', run_id):
        raise ValueError('Research not found')
    engine.check_active(run_id)


async def refresh_failed_sources(engine, intelligence, run_id, pending_ids):
    """Retry failed acquisition once per explicit resume, before model repair.

    A saved no-progress model decision must not prevent a repaired downloader
    from acquiring new text. Network attempts alone never count as evidence.
    """
    run = engine.store.get('runs', run_id) or {}
    generation = run.get('state', {}).get('resume_generation')
    if run.get('mode') != 'live' or not generation:
        return
    owned = _run_papers(engine.store, run_id)
    excluded = {row['paper_id'] for row in engine._literature_acquisition_exclusions(run_id)}
    for identifier in sorted(set(pending_ids)):
        if identifier in excluded:
            continue  # Explicit access exclusions must not be retried on every resume.
        previous = engine.store.get('run_evidence', f'{run_id}:{identifier}', {}).get('reading', {})
        if identifier not in owned:
            continue
        unavailable = previous.get('retrieval_status') in {'fetch_failed', 'no_text'}
        if not unavailable and previous.get('source_id'):
            try:
                unavailable = bool(source_text_issue(engine.sources.document(run_id, previous['source_id'])))
            except (ValueError, OSError, KeyError, TypeError):
                pass  # Local integrity failures are not source-access exclusions.
        if not unavailable:
            continue
        key = f'{run_id}:source-refresh:' + hashlib.sha256(json.dumps([generation, identifier]).encode()).hexdigest()
        if engine.store.get('stage_contexts', key):
            continue
        _active(engine, run_id)
        marker = {'id': key, 'run_id': run_id, 'kind': 'source_refresh', 'paper_id': identifier,
                  'resume_generation': generation, 'status': 'started', 'started_at': now()}
        engine.store.put('stage_contexts', key, marker)
        result = (await read_papers(engine, intelligence, run_id, [identifier]))[0]
        engine.store.put('stage_contexts', key, {**marker, 'status': 'completed',
            'retrieval_status': result.get('retrieval_status'), 'completed_at': now()})
        engine.store.event(run_id, 'literature.source_refreshed', '文献下载器已复查待补原文来源',
            {'paper_id': identifier, 'retrieval_status': result.get('retrieval_status'), 'model_called': False})


def _verified(paper):
    if not isinstance(paper, dict) or not isinstance(paper.get('id'), str):
        return False
    verification = paper.get('verification')
    if not isinstance(verification, dict):
        return False
    if paper.get('verified') is True and verification.get('status') == 'verified':
        return True
    # Local originals have an exact-byte identity, not a verified DOI/title.
    # Run ownership is checked separately when building the task catalogue.
    content_hash = paper.get('uploaded_content_sha256') or paper.get('content_sha256')
    return (paper.get('identity_kind') == 'uploaded_document'
            and verification.get('status') == 'uploaded_document'
            and isinstance(content_hash, str) and re.fullmatch(r'[a-f0-9]{64}', content_hash) is not None
            and verification.get('content_sha256') == content_hash)


def _run_papers(store, run_id):
    if getattr(store, 'is_read_snapshot', False):
        return store.memoized_read(('verified_task_papers', run_id), lambda: _collect_run_papers(store, run_id))
    from .architecture_validation import source_read
    return source_read(store, ('verified_task_papers', run_id), lambda: _collect_run_papers(store, run_id))


def _paper_catalogue(store, run_id):
    """Private read-only lookup view; callers must never mutate its papers."""
    if getattr(store, 'is_read_snapshot', False):
        return _run_papers(store, run_id)
    from .architecture_validation import source_read
    return source_read(store, ('verified_task_papers', run_id),
                       lambda: _collect_run_papers(store, run_id), borrowed=True)


def _paper_records(store, collection, run_id, field):
    """Project paper payloads without decoding unrelated acquisition histories."""
    from .storage import Store, _ReadSnapshot
    # Custom/transactional views retain their list contract, including virtual
    # or delegated rows. The SQL fast path is only the native SQLite store.
    if (getattr(type(store), 'list', None) not in {Store.list, _ReadSnapshot.list}
            or 'list' in getattr(store, '__dict__', {})):
        yield from store.list(collection, run_id)
        return
    where, parameters = store._record_filter(collection, run_id)
    path = '$.' + field  # Only the two static internal fields below are used.
    with store._read_db() as db:
        rows = db.execute("SELECT CASE WHEN json_type(data)='object' THEN "
            "CASE WHEN json_type(data,?) IS NULL THEN '{}' "
            "ELSE json_object(?,json_extract(data,?)) END ELSE data END AS data "
            'FROM records WHERE ' + where + ' ORDER BY updated_at,key',
            [path, field, path, *parameters])
        for row in rows:
            yield json.loads(row['data'])


def _collect_run_papers(store, run_id):
    from .reference_papers import is_uploaded_paper, is_shared_uploaded_paper

    def accepted(paper):
        return _verified(paper) and (paper.get('identity_kind') != 'uploaded_document'
                                    or is_uploaded_paper(store, run_id, paper)
                                    or is_shared_uploaded_paper(store, paper))

    papers = {}
    # The main input and survey_query checkpoints both retain complete paper
    # dictionaries. A survey_plan has no papers. No global registry is read.
    for record in _paper_records(store, 'literature_inputs', run_id, 'papers'):
        for paper in record.get('papers', []):
            if accepted(paper):
                papers[paper['id']] = paper
    for record in _paper_records(store, 'run_evidence', run_id, 'paper'):
        paper = record.get('paper')
        if accepted(paper):
            # Old run_evidence may predate fulltext_url capture. Retain a
            # verified checkpoint's optional locator when the old row lacks it.
            papers[paper['id']] = {**papers.get(paper['id'], {}),
                                  **{key: value for key, value in paper.items() if value is not None}}
    return papers


def _url(paper):
    if paper.get('fulltext_url'):
        return paper['fulltext_url']
    arxiv_id = paper.get('arxiv_id')
    if not arxiv_id:
        location = urlsplit(str(paper.get('url') or ''))
        if location.hostname in {'arxiv.org', 'www.arxiv.org', 'export.arxiv.org'}:
            arxiv_id = re.sub(r'^/(abs|pdf)/', '', location.path).removesuffix('.pdf')
    if isinstance(arxiv_id, str) and re.fullmatch(r'(?:\d{4}\.\d{4,5}|[a-zA-Z.-]+/\d{7})(?:v\d+)?', arxiv_id):
        return 'https://arxiv.org/pdf/' + arxiv_id
    if paper.get('url'):
        return paper['url']
    raise ValueError('Verified paper has no retrievable URL')


def _save_link(store, run_id, paper, result):
    key = f'{run_id}:{paper["id"]}'

    def merge(old):
        if old and old.get('run_id') != run_id:
            raise ValueError('Paper evidence record belongs to another research')
        old = old or {}
        if old.get('reading') == result:
            return old
        history = list(old.get('reading_history', []))
        if old.get('reading') and not history:
            history.append({'result': old['reading'], 'recorded_at': old.get('reading_recorded_at')})
        history.append({'result': result, 'recorded_at': now()})
        return {**old, 'id': key, 'run_id': run_id, 'paper': old.get('paper') or paper,
                'reading': result, 'reading_history': history, 'reading_recorded_at': history[-1]['recorded_at']}

    store.mutate('run_evidence', key, merge)
    if _verified(paper):
        from .paper_identity import register
        from .paper_bindings import bind
        entry_id = register(store, paper)
        bind(store, run_id, paper, result, entry_id=entry_id)


def _safe_error(exc):
    result = {'error_type': type(exc).__name__,
              'error': 'Source retrieval or indexing failed; no complete reading is claimed.'}
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if type(status) is int and 100 <= status <= 599:
            result['http_status'] = status
    if isinstance(exc, SourceUnavailableError):
        result.update(source_unavailable_code=exc.code,
                      error='Retrieved page is an intermediary or access challenge, not document text; no new memory was indexed.')
        if exc.code == 'tun_public_dns_unavailable':
            result['error'] = 'TUN returned synthetic DNS addresses and public DNS verification is unavailable; no paper content was downloaded.'
    return result


def _fallback_allowed(exc):
    # Never turn a corrupt retained snapshot or local indexing fault into an
    # excuse to acquire a different document. Transport/source availability only.
    return isinstance(exc, (httpx.HTTPError, SourceUnavailableError)) or (
        type(exc) is ValueError and str(exc) in {
            'No readable reference text found (scanned PDFs require OCR)',
            'Too many reference redirects', 'Verified paper has no retrievable URL'})


SOURCE_FIELDS = ['source_id', 'artifact_id', 'url', 'original_url', 'content_sha256', 'text_sha256',
                 'provenance', 'format', 'pages_total', 'pages_extracted', 'empty_text_pages',
                 'text_truncated', 'extraction', 'original_bytes_available', 'original_artifact_id',
                 'memory_document_id', 'chunk_count', 'index_version', 'index_status']


def has_fulltext(document):
    return document.get('format') == 'pdf' or document.get('format') == 'xml' and document.get('fulltext_xml') is True


def uploaded_rag_evidence(engine, run_id):
    """Reuse attributed shared analyses without granting external reading credit."""
    from .reference_papers import uploaded_paper_ids
    from .paper_identity import same_paper
    owned = _run_papers(engine.store, run_id)
    result = {}
    for paper_id in uploaded_paper_ids(engine.store, run_id):
        paper = owned.get(paper_id)
        link = engine.store.get('run_evidence', f'{run_id}:{paper_id}', {})
        reading = link.get('reading') or {}
        version_id, source_id = reading.get('rag_version_id'), reading.get('source_id')
        if not paper or not version_id or not source_id:
            continue
        try:
            payload = engine.paper_rag.load(version_id)
            if (payload.get('paper', {}).get('identity_kind') == 'uploaded_document'
                    and payload.get('uploaded_analysis_complete') is not True):
                continue
            source = engine.sources.document(run_id, source_id)
            if (source.get('rag_version_id') != version_id
                    or source['text_sha256'] != payload['note']['text_sha256']
                    or source.get('content_sha256') != payload['document'].get('content_sha256')
                    or not same_paper(engine.store, paper, payload['paper'])):
                continue
            result[paper_id] = {'paper_id': paper_id, 'source_id': source_id,
                'rag_version_id': version_id, 'payload': payload,
                'provenance': payload.get('provenance'), 'new_reading_credit': False,
                'scientific_claims_verified': False}
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return result


def uploaded_reading_status(engine, run_id):
    """Require notes and inspected extracted text for each new uploaded original.

    RAG analyses retain their historical/external attribution and are not read
    again. Completeness is about retained text; unavailable pages remain gaps.
    """
    from .reference_papers import uploaded_paper_ids
    from .literature_notes import valid_paper_ids, _read_events, _read_receipts
    uploaded_ids = uploaded_paper_ids(engine.store, run_id)
    if not uploaded_ids:
        return {'approved': True, 'issues': [], 'pending_paper_ids': [], 'papers': [],
                'reused_rag_ids': [], 'newly_read_ids': []}
    owned = _run_papers(engine.store, run_id)
    noted = valid_paper_ids(engine, run_id)
    reused = uploaded_rag_evidence(engine, run_id)
    events = _read_events(engine.store, run_id)
    # Presence of a scout compiler manifest establishes the current tracked
    # protocol, including a compiled-but-not-submitted request. Offline/legacy
    # ledgers without a model request retain their explicit compatibility scope.
    if hasattr(engine.store, 'record_keys'):
        delivery_required = bool(engine.store.record_keys('context_manifests', run_id,
            predicate="json_extract(data,'$.role')=?", parameters=['scout'], limit=1))
    else:
        delivery_required = any(row.get('role') == 'scout' for row in
                                engine.store.list('context_manifests', run_id))
    rows, issues, pending, completed = [], [], [], []
    for paper_id in sorted(uploaded_ids):
        if paper_id in reused:
            reused_evidence = reused[paper_id]
            rows.append({k: reused_evidence[k] for k in ('paper_id', 'source_id', 'rag_version_id', 'provenance',
                'new_reading_credit', 'scientific_claims_verified')} | {'status': 'reused_rag_analysis',
                'uploaded_original_source_id': owned[paper_id].get('uploaded_source_id'),
                'uploaded_original_content_sha256': owned[paper_id].get('uploaded_content_sha256'),
                'scope': '复用已有分析，保留原分析时间和来源；本任务未重复分析或新增阅读回执。'})
            continue
        row = {'paper_id': paper_id, 'status': 'pending', 'missing_ranges': [],
               'scientific_claims_verified': False, 'full_document_read': False}
        link = engine.store.get('run_evidence', f'{run_id}:{paper_id}', {})
        reading = link.get('reading') or {}
        source_id = reading.get('source_id')
        try:
            if paper_id not in owned or not source_id:
                raise ValueError('上传原文尚未建立本任务来源绑定')
            document = engine.sources.document(run_id, source_id)
            if (reading.get('text_sha256') != document['text_sha256']
                    or reading.get('content_sha256') != document.get('content_sha256')):
                raise ValueError('上传原文来源哈希与获取回执不匹配')
            receipts = _read_receipts(engine.store, run_id, document, allow_empty=True,
                _events=events, _papers=owned)
            delivered = []
            effective = receipts
            if delivery_required:
                from .evidence_delivery import delivery_spans
                delivered = delivery_spans(engine.store, run_id, 'scout', source_id,
                    document['text_sha256'], text=document['text'],
                    aliases=[document[key] for key in ('source_alias_id', 'canonical_source_id')
                             if document.get(key)])
                effective = []
                for receipt in receipts:
                    for span in delivered:
                        start = max(receipt['start'], span['char_start'])
                        end = min(receipt['end'], span['char_end'])
                        if start < end:
                            effective.append({'start': start, 'end': end})
            text, cursor, missing = document['text'], 0, []
            for receipt in sorted(effective, key=lambda item: (item['start'], item['end'])):
                if receipt['start'] > cursor and text[cursor:receipt['start']].strip():
                    missing.append({'start': cursor, 'end': receipt['start']})
                cursor = max(cursor, receipt['end'])
            if cursor < len(text) and text[cursor:].strip():
                missing.append({'start': cursor, 'end': len(text)})
            extraction = document.get('extraction') or {}
            gaps = []
            if not text.strip():
                gaps.append('上传原文无可读文本')
            if document.get('text_truncated') or extraction.get('text_truncated'):
                gaps.append('上传原文提取文本被截断')
            if (extraction.get('unextracted_pages') or
                    (document.get('pages_total') or 0) > (document.get('pages_extracted') or 0)):
                gaps.append('上传PDF存在未提取页面')
            if document.get('empty_text_pages') or extraction.get('empty_text_pages'):
                gaps.append('上传PDF存在无文本页面，需补充OCR或图像阅读证据')
            row.update(source_id=source_id, text_sha256=document['text_sha256'],
                content_sha256=document.get('content_sha256'), retained_chars=len(text),
                missing_ranges=missing, extraction_gaps=gaps, note_saved=paper_id in noted,
                delivery_required=delivery_required,
                reading_compatibility='acknowledged_model_delivery' if delivery_required else 'legacy_or_offline_tool_ledger',
                source_read_event_ids=sorted({receipt['id'] for receipt in receipts}),
                evidence_delivery_receipt_ids=sorted({span['id'] for span in delivered}),
                reading_scope='all_retained_extracted_text' if not missing and text.strip() else 'partial_extracted_text',
                scope='精读门禁核对已提取正文的实际工具阅读、受跟踪请求的已确认模型文本投递与合格笔记；图像理解和科研结论不由文本覆盖自动验证。')
            if missing:
                receipt_scope = '实际阅读和已确认模型投递' if delivery_required else '实际阅读'
                issues.append(f'上传论文 {paper_id} 尚有正文未精读，缺少 {len(missing)} 段{receipt_scope}回执')
            if paper_id not in noted:
                issues.append(f'上传论文 {paper_id} 缺少与实际原文阅读绑定的合格笔记')
            issues.extend(f'{paper_id}：{gap}' for gap in gaps)
            if not missing and paper_id in noted and not gaps:
                row['status'] = 'read_and_noted'
                completed.append(paper_id)
            else:
                pending.append(paper_id)
        except (OSError, ValueError, KeyError, TypeError):
            issues.append(f'上传论文 {paper_id} 的可读原文或来源绑定尚未就绪')
            pending.append(paper_id)
        rows.append(row)
    return {'approved': not issues, 'issues': issues, 'pending_paper_ids': pending, 'papers': rows,
            'reused_rag_ids': sorted(reused), 'newly_read_ids': completed,
            'scope': '用户上传seed逐篇精读并记笔记，已有RAG分析仅复用；不表示科研结论已验证。'}


async def _retrieve(engine, intelligence, run_id, url, result):
    previous = engine.store.get('run_evidence', f'{run_id}:{result["paper_id"]}', {}).get('reading', {})
    fetched = document = None
    if (previous.get('source_id') and not previous.get('rag_version_id')
            and previous.get('paper_metadata_sha256') == result.get('paper_metadata_sha256')):
        # Shared-RAG reuse is validated by adopt() before this path. A rejected
        # shared version must continue through ordinary source acquisition.
        retained = engine.sources.document(run_id, previous['source_id'])
        fulltext = retained.get('format') == 'pdf' or retained.get('format') == 'xml' and retained.get('fulltext_xml')
        if retained.get('original_url') == url and (not result.get('lawful_fulltext_required') or fulltext):
            # A previously accepted HTML/XML snapshot can finish local indexing
            # without a new PDF-preferred download. Integrity is checked above.
            fetched = document = engine.sources.index_document(run_id, previous['source_id'])
    if fetched is None:
        fetched = await engine.sources.fetch(run_id, url, limit=800, prefer_pdf=True)
    _active(engine, run_id)
    for key in SOURCE_FIELDS:
        if key in fetched:
            result[key] = fetched[key]
    document = document or engine.sources.index_document(run_id, fetched['source_id'])
    if result.get('lawful_fulltext_required') and not (
            document.get('format') == 'pdf' or document.get('format') == 'xml' and document.get('fulltext_xml')):
        raise SourceUnavailableError('resolved_location_not_fulltext')
    _index(engine, intelligence, run_id, document, fetched, result)


def _index(engine, intelligence, run_id, document, fetched, result):
    issue = source_text_issue(document)
    if issue:
        raise SourceUnavailableError(issue)
    # Indexing is kept outside the availability exception classification.
    result['_indexing_started'] = True
    manifest = intelligence.memory_index_document(run_id, 'scout', document,
        source_id=fetched['source_id'], artifact_id=fetched['artifact_id'])
    for key in ['memory_document_id', 'chunk_count', 'index_version', 'index_status']:
        result[key] = manifest.get(key)
    if not document.get('text', '').strip() or not manifest.get('chunk_count'):
        result['retrieval_status'] = 'no_text'
    else:
        pdf = document.get('format') == 'pdf' or bool(document.get('pages_total'))
        result['retrieval_status'] = 'pdf_text_indexed' if pdf else 'web_text_indexed'


async def _resolved_retrieve(engine, intelligence, run_id, paper, result):
    resolution = await engine.resolve_literature_fulltext(run_id, paper)
    result.update(fulltext_resolution=resolution, lawful_fulltext_required=True, acquisition_attempts=[])
    if not resolution['candidates']:
        raise SourceUnavailableError('legal_fulltext_unavailable')
    failure = None
    for location in resolution['candidates']:
        _active(engine, run_id)
        attempt = {**location, 'status': 'pending'}
        result['acquisition_attempts'].append(attempt)
        try:
            if location.get('source_id'):
                document = engine.sources.document(run_id, location['source_id'])
                for key in SOURCE_FIELDS:
                    if key in document:
                        result[key] = document[key]
                _index(engine, intelligence, run_id, document, document, result)
            else:
                result['requested_url'] = location['url']
                await _retrieve(engine, intelligence, run_id, location['url'], result)
            attempt['status'] = 'retrieved'
            result['fulltext_access'] = location['access']
            return
        except (asyncio.CancelledError, RunDeletedError):
            raise
        except Exception as exc:
            attempt.update(status='failed', **_safe_error(exc))
            if result.get('_indexing_started') or not _fallback_allowed(exc):
                raise
            failure = exc
            # A rejected HTML/abstract snapshot is not the next candidate's
            # identity or source. Keep the attempt, discard its transient fields.
            for key in SOURCE_FIELDS:
                result.pop(key, None)
            result['source_id'] = None
    raise failure or SourceUnavailableError('legal_fulltext_unavailable')


async def _fallback(engine, intelligence, run_id, paper, result, generation):
    service = getattr(engine.sources, 'literature', None)
    finder = getattr(service, 'find_arxiv_version', None)
    result.pop('_indexing_started', None)
    result.update(fallback_resume_generation=generation, skip_recommended=True)
    result['original_error'] = {key: result[key] for key in
        ['error_type', 'error', 'http_status', 'source_unavailable_code'] if key in result}
    if not result['original_error']:
        result['original_error'] = {'error': 'Original source has no readable text.'}
    if not callable(finder):
        result.update(fallback_status='not_supported')
        return
    result['fallback_attempted'] = True
    _active(engine, run_id)
    try:
        report = await finder(paper)
        _active(engine, run_id)
    except (asyncio.CancelledError, RunDeletedError):
        raise
    except Exception as exc:
        result.update(fallback_status='search_failed', fallback_error=_safe_error(exc))
        return
    result['fallback_search'] = {key: report[key] for key in
        ['candidates_checked', 'candidate_limit', 'query_count', 'bounded_search'] if key in report}
    status = report.get('status')
    if status != 'matched':
        result['fallback_status'] = status if status in {'no_match', 'ambiguous', 'search_failed'} else 'search_failed'
        return
    candidate = report.get('paper', {})
    basis = arxiv_version_match(paper, candidate)
    identifier = candidate.get('arxiv_id')
    if not basis or not isinstance(identifier, str) or not re.fullmatch(
            r'(?:\d{4}\.\d{4,5}|[a-zA-Z.-]+/\d{7})(?:v\d+)?', identifier):
        result['fallback_status'] = 'no_match'
        return
    url = 'https://arxiv.org/pdf/' + identifier
    result.update(resolved_arxiv_id=identifier, resolved_arxiv_url=url, identity_match_basis=basis,
                  resolved_arxiv_record={key: candidate.get(key) for key in
                    ['id', 'arxiv_id', 'title', 'authors', 'doi', 'source', 'verified', 'verification']})
    if result.get('requested_url') == url:
        result['fallback_status'] = 'no_text' if result['retrieval_status'] == 'no_text' else 'fetch_failed'
        return  # The same source already failed once in this invocation.
    if result.get('source_id'):
        result['original_source_id'] = result['source_id']
    for key in SOURCE_FIELDS + ['error_type', 'error', 'http_status', 'source_unavailable_code']:
        result.pop(key, None)
    result.update(source_id=None, url=None, memory_document_id=None, index_status=None)
    result['retrieval_status'] = 'fetch_failed'
    try:
        await _retrieve(engine, intelligence, run_id, url, result)
        result['fallback_status'] = 'no_text' if result['retrieval_status'] == 'no_text' else 'source_retrieved'
        result['skip_recommended'] = result['fallback_status'] != 'source_retrieved'
    except (asyncio.CancelledError, RunDeletedError):
        raise
    except Exception as exc:
        result.update(_safe_error(exc))
        result['fallback_status'] = 'fetch_failed'
        if result.get('_indexing_started') or not _fallback_allowed(exc):
            result['skip_recommended'] = False  # Local integrity/index errors are not absent papers.


async def read_papers(engine, intelligence, run_id, paper_ids, *, require_fulltext=False):
    """Return one metadata-only outcome per requested ID, preserving input order.

    Source snapshots and memory manifests supply durable idempotence. A failed
    fetch remains visible and can be retried by a later bounded tool invocation.
    Cancellation/deletion aborts the batch and drains its child tasks.
    """
    if type(require_fulltext) is not bool:
        raise ValueError('require_fulltext must be a boolean')
    if (not isinstance(paper_ids, list) or not 1 <= len(paper_ids) <= 6
            or any(not isinstance(item, str) or not item for item in paper_ids)
            or len(set(paper_ids)) != len(paper_ids)):
        raise ValueError('paper_ids must contain 1 to 6 unique exact paper IDs')
    _active(engine, run_id)
    papers = _run_papers(engine.store, run_id)
    from .paper_identity import task_paper
    resolved = {identifier: task_paper(engine.store, run_id, identifier, papers) for identifier in paper_ids}
    if any(not paper for paper in resolved.values()):
        raise ValueError('Every paper ID must be identity-verified in this research task')
    paper_ids = list(dict.fromkeys(p['id'] for p in resolved.values()))
    from . import venue_policy
    from .reference_papers import is_uploaded_paper
    if venue_policy.enabled(engine.store.get('runs', run_id)):
        excluded = {pid: venue_policy.assess(papers[pid])['reason'] for pid in paper_ids
                    if not is_uploaded_paper(engine.store, run_id, papers[pid])
                    and not venue_policy.assess(papers[pid])['eligible']}
        if excluded:
            raise ValueError('CCF A/B scope: verify publication venue or select eligible papers before reading: '
                             + json.dumps(excluded, ensure_ascii=False))
    semaphore = asyncio.Semaphore(2)

    async def read_one(identifier):
        async with semaphore:
            _active(engine, run_id)
            paper = papers[identifier]
            result = {'paper_id': identifier, 'title': paper.get('title'), 'source_id': None, 'url': None,
                      'memory_document_id': None, 'index_status': None, 'retrieval_status': 'fetch_failed',
                      'paper_metadata_sha256': hashlib.sha256(json.dumps(paper, sort_keys=True,
                          ensure_ascii=False).encode()).hexdigest(),
                      'identity_verified': True, 'document_identity_verified': False, 'ai_read': False,
                      'scientific_claims_verified': False, 'note': NOTE,
                      'original_paper_id': identifier, 'fallback_attempted': False, 'fallback_status': 'not_attempted'}
            if is_uploaded_paper(engine.store, run_id, paper):
                result.update(identity_kind='uploaded_document', content_integrity_verified=True,
                    bibliographic_identity_verified=paper.get('bibliographic_identity_verified') is True)
            generation = engine.store.get('runs', run_id).get('state', {}).get('resume_generation')
            previous = engine.store.get('run_evidence', f'{run_id}:{identifier}', {}).get('reading', {})
            rag = getattr(engine, 'paper_rag', None)
            if rag and engine.store.get('runs', run_id).get('mode') == 'live':
                try:
                    reused = rag.adopt(engine, intelligence, run_id, paper,
                                       **({'require_fulltext': True} if require_fulltext
                                           and not is_uploaded_paper(engine.store, run_id, paper) else {}))
                    if reused:
                        return reused
                except (ValueError, OSError, KeyError, TypeError):
                    # A damaged/missing shared copy must fall back to normal acquisition.
                    pass
            if is_uploaded_paper(engine.store, run_id, paper):
                # Creation-time originals already have a selected-reference
                # receipt; never send an upload ID to a network resolver.
                fetched = engine.sources.from_reference(run_id, paper['source_reference_id'])
                document = engine.sources.index_document(run_id, fetched['source_id'])
                expected = paper.get('uploaded_content_sha256') or paper.get('content_sha256')
                if document.get('content_sha256') != expected:
                    raise ValueError('Uploaded paper content hash changed')
                for key in SOURCE_FIELDS:
                    if key in fetched:
                        result[key] = fetched[key]
                _index(engine, intelligence, run_id, document, fetched, result)
                _active(engine, run_id)
                _save_link(engine.store, run_id, paper, result)
                return result
            same_metadata = previous.get('paper_metadata_sha256') == result['paper_metadata_sha256']
            if (same_metadata and previous.get('fallback_attempted') is True
                    and previous.get('fallback_resume_generation') == generation
                    and previous.get('skip_recommended') is True):
                return previous  # One bounded acquisition per failed paper per explicit resume.
            reused_fallback = (same_metadata and previous.get('fallback_status') == 'source_retrieved'
                               and arxiv_version_match(paper, previous.get('resolved_arxiv_record', {})))
            can_fallback = False
            try:
                if reused_fallback:
                    result = dict(previous)
                    candidate = 'https://arxiv.org/pdf/' + previous['resolved_arxiv_record']['arxiv_id']
                else:
                    if require_fulltext or engine.store.get('runs', run_id)['config'].get('codex_literature_protocol') == 1:
                        await _resolved_retrieve(engine, intelligence, run_id, paper, result)
                        candidate = None
                    else:
                        candidate = _url(paper)
                        result['requested_url'] = candidate
                if candidate is not None:
                    await _retrieve(engine, intelligence, run_id, candidate, result)
                can_fallback = result['retrieval_status'] == 'no_text' and not reused_fallback
            except (asyncio.CancelledError, RunDeletedError):
                raise
            except Exception as exc:
                # HTTP exceptions can contain server bodies and URLs with tokens.
                # Persist their type only, never arbitrary remote error strings.
                result.update(_safe_error(exc))
                result['retrieval_status'] = 'fetch_failed'
                can_fallback = _fallback_allowed(exc) and not result.get('_indexing_started') and not reused_fallback
                if reused_fallback:
                    result.update(fallback_status='fetch_failed', skip_recommended=False)
            if can_fallback:
                await _fallback(engine, intelligence, run_id, paper, result, generation)
            result.pop('_indexing_started', None)
            _active(engine, run_id)
            _save_link(engine.store, run_id, paper, result)
            return result

    tasks = [asyncio.create_task(read_one(identifier)) for identifier in paper_ids]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
