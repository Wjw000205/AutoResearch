"""Task-owned uploaded articles, retained originals and shared analysis reuse.

The SHA256 identifies the supplied document, not its bibliographic metadata or
scientific claims. Uploading/indexing never produces an AI reading receipt.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import re
from pathlib import Path

from .storage import now


REFERENCE_MAX_BYTES = 12 * 1024 * 1024
REFERENCE_MAX_FILES = 10


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _complete_text(document):
    """Partial extraction/abstract prefixes cannot establish document identity."""
    return (isinstance(document.get('text'), str) and len(document['text'].strip()) >= 1000
            and document.get('text_truncated') is not True
            and not document.get('empty_text_pages')
            and document.get('extraction_status') not in {'no_text_requires_ocr', 'partial_text'}
            and (document.get('pages_total') is None
                 or document.get('pages_total') == document.get('pages_extracted')))


def find_rag_analysis(rag, document):
    """Find an intact immutable analysis by exact bytes or complete exact text.

    A title, filename, DOI found in a bibliography, or a truncated text hash is
    never a deduplication key. Explicit bibliographic verification can still
    add aliases through the existing paper identity protocol.
    """
    content_hash, text_hash = document.get('content_sha256'), document.get('text_sha256')
    if not _sha(content_hash):
        return None
    matches = []
    complete_text = _complete_text(document)
    sources = rag.store.list('paper_rag_sources')
    for source in sources:
        by_content = source.get('content_sha256') == content_hash
        by_text = (complete_text and _sha(text_hash)
                   and source.get('text_sha256') == text_hash)
        if by_content or by_text:
            matches.append((not by_content, source))
    # Old installations may not yet have source manifests; versions are still
    # authoritative and their compact SQLite metadata is safe to inspect.
    if not sources:
        matches = [(False, {'versions': [row['id']]})
                   for row in rag.store.list('paper_rag_versions')]
    for _, source in sorted(matches, key=lambda row: (row[0], row[1].get('id', ''))):
        versions = list(source.get('versions', []))
        if source.get('entry_id'):
            entry = rag.store.get('paper_rag', rag.canonical_id(source['entry_id']), {})
            if entry.get('latest_version') in versions:
                versions.remove(entry['latest_version'])
                versions.insert(0, entry['latest_version'])
        for version_id in versions:
            try:
                payload = rag.load(version_id)
                if (payload.get('paper', {}).get('identity_kind') == 'uploaded_document'
                        and payload.get('uploaded_analysis_complete') is not True):
                    continue
                retained = payload['document']
                exact_bytes = retained.get('content_sha256') == content_hash
                exact_text = (retained.get('text_sha256') == text_hash
                              and complete_text and _complete_text(retained)
                              and retained['text'] == document['text'])
                if not (exact_bytes or exact_text):
                    continue
                from .literature_notes import NOTE_FIELDS
                if not NOTE_FIELDS <= payload.get('note', {}).keys():
                    continue
                return {'version_id': version_id, 'entry_id': rag.canonical_id(payload['entry_id']),
                        'payload': payload, 'basis': 'content_sha256' if exact_bytes else 'complete_text_sha256'}
            except (OSError, ValueError, KeyError, TypeError):
                continue
    return None


def reference_status(rag, document):
    match = find_rag_analysis(rag, document)
    return ({'rag_status': 'already_present', 'rag_entry_id': match['entry_id'],
             'rag_version_id': match['version_id'], 'rag_match_basis': match['basis']}
            if match else {'rag_status': 'pending_analysis', 'rag_entry_id': None, 'rag_version_id': None})


def bibliographic_candidates(document):
    """Conservative navigation hints; these never enter identity aliases."""
    text = str(document.get('text') or '')[:6000]
    dois = list(dict.fromkeys(value.rstrip('.,;:)]}').casefold()
                            for value in re.findall(r'\b10\.\d{4,9}/[^\s<>"]+', text, re.I)))[:8]
    arxiv = list(dict.fromkeys(re.findall(r'\barxiv\s*:?\s*(\d{4}\.\d{4,5}(?:v\d+)?)', text, re.I)))[:4]
    return {'title': document.get('title'), 'doi_candidates': dois, 'arxiv_candidates': arxiv,
            'verified': False, 'note': '原文提取的检索线索；可能来自引用，尚未核验论文书目身份。'}


def _selected_reference(store, run_id, paper):
    if not isinstance(paper, dict):
        return None
    reference_id = paper.get('source_reference_id')
    if not isinstance(reference_id, str) or not reference_id:
        return None
    run = store.get('runs', run_id, {})
    if reference_id not in run.get('config', {}).get('reference_ids', []):
        return None
    reference = store.get('references', reference_id)
    expected = paper.get('uploaded_content_sha256') or paper.get('verification', {}).get('content_sha256')
    if not reference or not _sha(expected) or reference.get('content_sha256') != expected:
        return None
    return reference


def is_uploaded_paper(store, run_id, paper):
    """Require selected bytes and a server-prepared task checkpoint."""
    if not _selected_reference(store, run_id, paper):
        return False
    checkpoint = store.get('literature_inputs', f'{run_id}:uploaded-papers', {})
    return (checkpoint.get('kind') == 'uploaded_papers'
            and checkpoint.get('run_id') == run_id
            and any(item.get('id') == paper.get('id')
                    and item.get('source_reference_id') == paper.get('source_reference_id')
                    and item.get('uploaded_content_sha256') == paper.get('uploaded_content_sha256')
                    for item in checkpoint.get('papers', [])))


def uploaded_ids(store, run_id):
    checkpoint = store.get('literature_inputs', f'{run_id}:uploaded-papers', {})
    return {paper['id'] for paper in checkpoint.get('papers', [])
            if is_uploaded_paper(store, run_id, paper)}


uploaded_paper_ids = uploaded_ids


def is_shared_uploaded_paper(store, paper):
    """Recognize a saved, fully inspected original without selected-ref scope.

    Ordinary shared discoveries retain date, venue and relevance filters. This
    grants only the stored document's content identity, never DOI verification.
    """
    if not isinstance(paper, dict) or paper.get('identity_kind') != 'uploaded_document':
        return False
    version_id = paper.get('rag_version_id')
    content_hash = paper.get('uploaded_content_sha256') or paper.get('content_sha256')
    verification = paper.get('verification', {})
    if (not _sha(version_id) or not _sha(content_hash)
            or verification.get('status') != 'uploaded_document'
            or verification.get('content_sha256') != content_hash):
        return False
    try:
        from .paper_rag import PaperRAG
        from .paper_identity import same_paper, resolve
        rag = PaperRAG(store, store.path.parent)
        payload = rag.load(version_id)  # Exact immutable version and snapshot hash.
        saved = payload['paper']
        entry_id = rag.canonical_id(payload['entry_id'])
        entry = store.get('paper_rag', entry_id, {})
        if (payload.get('uploaded_analysis_complete') is not True
                or saved.get('identity_kind') != 'uploaded_document'
                or saved.get('uploaded_content_sha256') != content_hash
                or payload['document'].get('content_sha256') != content_hash
                or version_id not in entry.get('versions', [])
                or resolve(store, paper) != entry_id
                or not same_paper(store, paper, saved)):
            return False
        if paper.get('rag_entry_id') and rag.canonical_id(paper['rag_entry_id']) != entry_id:
            return False
        return True
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def _paper(document, reference_id, match):
    if match:
        paper = copy.deepcopy(match['payload']['paper'])
        paper.update(rag_entry_id=match['entry_id'], rag_version_id=match['version_id'],
                     rag_match_basis=match['basis'])
    else:
        paper = {'id': 'upload:' + document['content_sha256'], 'title': document.get('title') or 'Uploaded article',
                 'authors': [], 'year': None, 'publication_date': None, 'doi': None,
                 'url': document.get('url'), 'source': 'user_file', 'provenance': 'user_supplied_untrusted',
                 'verified': False}
    paper['bibliographic_identity_verified'] = (paper.get('verified') is True
        and paper.get('verification', {}).get('status') == 'verified')
    if paper.get('verified') is not True or paper.get('verification', {}).get('status') != 'verified':
        paper['bibliographic_verification'] = copy.deepcopy(paper.get('verification', {}))
        paper.update(verified=False, verification={
            'status': 'uploaded_document', 'method': 'uploaded_document_identity',
            'content_sha256': document['content_sha256'], 'bibliographic_identity_verified': False,
            'scientific_claims_verified': False,
            'note': '仅核验用户所选原文的内容哈希；书目身份和科学结论尚未独立核验。'})
    paper.update(identity_kind='uploaded_document', source_reference_id=reference_id,
                 uploaded_content_sha256=document['content_sha256'], user_uploaded=True,
                 bibliographic_candidates=bibliographic_candidates(document))
    return paper


def _prepare(engine, intelligence, run_id):
    from .deletion import _plain_path
    from .literature_reading import SOURCE_FIELDS, _save_link
    engine.check_active(run_id)
    run = engine.store.get('runs', run_id)
    selected = list(dict.fromkeys(run.get('config', {}).get('reference_ids', [])))
    if run.get('config', {}).get('uploaded_papers_protocol') != 1 or not selected:
        return []
    if len(selected) > REFERENCE_MAX_FILES:
        raise ValueError('At most 10 uploaded reference articles are supported')
    old = engine.store.get('literature_inputs', f'{run_id}:uploaded-papers')
    if old and old.get('reference_ids') != selected:
        raise ValueError('Selected reference articles changed after preparation')
    papers, documents = [], []
    seen = set()
    for reference_id in selected:
        document = copy.deepcopy(engine.store.get('references', reference_id))
        if not document or not isinstance(document.get('text'), str) or not _sha(document.get('content_sha256')):
            raise ValueError('Selected reference article has no retained document identity')
        if hashlib.sha256(document['text'].encode()).hexdigest() != document.get('text_sha256'):
            raise ValueError('Selected reference article text integrity check failed')
        saved_paper = next((paper for paper in (old or {}).get('papers', [])
                            if paper.get('source_reference_id') == reference_id), None)
        if saved_paper:
            if saved_paper.get('uploaded_content_sha256') != document['content_sha256']:
                raise ValueError('Selected reference article content changed after preparation')
            paper = copy.deepcopy(saved_paper)
            match = ({'version_id': paper['rag_version_id'], 'entry_id': paper['rag_entry_id'],
                      'payload': engine.paper_rag.load(paper['rag_version_id']),
                      'basis': paper['rag_match_basis']} if paper.get('rag_version_id') else None)
        else:
            match = find_rag_analysis(engine.paper_rag, document)
            paper = _paper(document, reference_id, match)
        if paper['id'] in seen:
            continue
        seen.add(paper['id'])
        papers.append(paper)
        documents.append((paper, document, match))
    checkpoint = {'id': f'{run_id}:uploaded-papers', 'run_id': run_id, 'kind': 'uploaded_papers',
                  'papers': papers, 'reference_ids': selected, 'created_at': (old or {}).get('created_at', now()),
                  'provenance': 'user_supplied_untrusted', 'ai_read': False,
                  'scientific_claims_verified': False}
    # Membership precedes binding; it is the authoritative scope guard used by
    # reading/notes. No original or analysis receipt is fabricated here.
    engine.check_active(run_id)
    engine.store.put('literature_inputs', checkpoint['id'], checkpoint)
    for paper, document, match in documents:
        engine.check_active(run_id)
        previous = engine.store.get('run_evidence', f'{run_id}:{paper["id"]}', {})
        original_path = document.get('original_path')
        if original_path:
            path = Path(original_path).absolute()
            if not path.is_relative_to(engine.data_dir / 'references'):
                raise ValueError('Selected reference original is outside its managed directory')
            _plain_path(path, engine.data_dir)
            if not path.is_file() or path.stat().st_size > REFERENCE_MAX_BYTES:
                raise ValueError('Selected reference original is missing or exceeds the 12 MiB bound')
            document['_content_bytes'] = path.read_bytes()
        source_id = engine.sources._persist(run_id, document, origin={
            'source_reference_id': paper['source_reference_id'],
            'original_url': document.get('original_url') or document.get('url')})
        current = engine.sources.document(run_id, source_id)
        paper['uploaded_source_id'] = source_id
        if previous.get('reading', {}).get('source_id'):
            engine.sources.document(run_id, previous['reading']['source_id'])
            continue
        if match:
            # The selected original remains separate from an inherited source.
            # Exact text can link a PDF to a text-only RAG analysis; its original
            # bytes must not be replaced by the historical analysis's bytes.
            continue
        manifest = intelligence.memory_index_document(run_id, 'scout', current,
            source_id=source_id, artifact_id=source_id)
        reading = {key: current.get(key) for key in SOURCE_FIELDS if key in current}
        reading.update({key: manifest.get(key) for key in
                        ('memory_document_id', 'chunk_count', 'index_version', 'index_status')})
        reading.update(paper_id=paper['id'], source_id=source_id, artifact_id=source_id,
                       retrieval_status=('pdf_text_indexed' if document.get('format') == 'pdf' else 'web_text_indexed')
                           if document['text'].strip() and manifest.get('chunk_count') else 'no_text',
                       identity_verified=True, content_integrity_verified=True,
                       document_identity_verified=False, bibliographic_identity_verified=False,
                       provenance='user_supplied_untrusted', ai_read=False, scientific_claims_verified=False,
                       note='用户所选原文已保留并建立索引；尚未完成AI精读或科学结论核验。')
        _save_link(engine.store, run_id, paper, reading)
    engine.check_active(run_id)
    engine.store.put('literature_inputs', checkpoint['id'], checkpoint)
    return papers


async def prepare(engine, intelligence, run_id):
    """Prepare references without network/model calls or event-loop extraction."""
    result = await asyncio.to_thread(_prepare, engine, intelligence, run_id)
    engine.check_active(run_id)
    return result


def reusable_rag_ids(engine, run_id):
    """External saved analyses may support ideas without new reading credit.

    Only an actually adopted, source-bound intact version qualifies. Search hits
    and upload matches alone do not bypass the task's source preparation.
    """
    from .literature_notes import NOTE_FIELDS
    result = set()
    for reuse in engine.store.list('paper_rag_reuses', run_id):
        try:
            if reuse.get('run_id') != run_id:
                continue
            payload = engine.paper_rag.load(reuse['version_id'])
            if payload.get('provenance') != 'external_agent_analysis':
                continue
            evidence = engine.store.get('run_evidence', f'{run_id}:{reuse["paper_id"]}', {})
            paper, reading = evidence.get('paper', {}), evidence.get('reading', {})
            if not is_uploaded_paper(engine.store, run_id, paper):
                continue
            source = engine.sources.document(run_id, reuse['source_id'])
            if (reading.get('rag_version_id') != reuse['version_id']
                    or reading.get('source_id') != reuse['source_id']
                    or source.get('rag_version_id') != reuse['version_id']
                    or source.get('text_sha256') != payload['note'].get('text_sha256')
                    or source.get('content_sha256') != payload['document'].get('content_sha256')
                    or not NOTE_FIELDS <= payload['note'].keys()):
                continue
            from .paper_identity import same_paper
            if same_paper(engine.store, paper, payload['paper']):
                result.add(reuse['paper_id'])
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return result
