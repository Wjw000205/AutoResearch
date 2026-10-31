"""Task-scoped navigation aliases over existing canonical Paper RAG indexes.

No document, chunk, source, artifact or reading credit is created here. Exact
source versions retain their bytes, text and page coordinates in the one RAG.
"""
from __future__ import annotations

import re

from . import paper_bindings, paper_identity

PREFIX = 'paper-rag-source:'


def is_source(document_id):
    return isinstance(document_id, str) and document_id.startswith(PREFIX)


def _sections(document):
    """Page navigation with disjoint Unicode ranges, including separators."""
    pages, text = document.get('pages') or [], document['text']
    if not pages:
        return [{'id': 's0', 'title': '已保留原文', 'start': 0, 'end': len(text), 'chars': len(text)}]
    starts = [page['start'] for page in pages]
    if starts != sorted(set(starts)):
        raise ValueError('Canonical source page navigation is ambiguous')
    starts[0] = 0
    return [{'id': f's{i}', 'title': '第 ' + str(page.get('page_number', page.get('page'))) + ' 页',
             'start': starts[i], 'end': starts[i + 1] if i + 1 < len(starts) else len(text),
             'chars': (starts[i + 1] if i + 1 < len(starts) else len(text)) - starts[i]}
            for i, page in enumerate(pages)]


def load(engine, run_id, document_id, expected_sha256):
    """Validate task ownership and immutable source before returning any text."""
    from .document_index import _sha
    raw_key = document_id.removeprefix(PREFIX)
    if not re.fullmatch('[0-9a-f]{64}', raw_key):
        raise ValueError('Invalid canonical paper source navigation ID')
    key = paper_bindings.canonical_source(engine.store, raw_key)
    shared = engine.store.get('paper_rag_sources', key)
    if (not shared or shared.get('id') != key
            or shared.get('source_identity_protocol') != paper_bindings.SOURCE_IDENTITY_PROTOCOL
            or not isinstance(expected_sha256, str) or shared.get('text_sha256') != expected_sha256):
        raise ValueError('Canonical source ownership or version SHA256 mismatch')
    source_id = 'rag-source:' + key
    # This resolves only through a valid acquisition binding owned by this run.
    document = engine.sources.index_document(run_id, source_id)
    if (document['text_sha256'] != expected_sha256 or _sha(document['text']) != expected_sha256
            or document.get('content_sha256') != shared.get('content_sha256')
            or paper_bindings.source_key(shared['entry_id'], document) != key
            or document.get('canonical_paper_id') != paper_identity.canonical(engine.store, shared['entry_id'])):
        raise ValueError('Canonical paper source integrity mismatch')
    physical_id = shared['memory_document_id']
    manifest = engine.store.get('memory_documents', physical_id)
    if (not manifest or manifest.get('id') != physical_id or manifest.get('run_id') != 'global'
            or manifest.get('source_id') != source_id or manifest.get('superseded_by')
            or manifest.get('text_sha256') != expected_sha256
            or manifest.get('content_sha256') != shared['content_sha256']
            or manifest.get('text_chars') != len(document['text'])
            or not manifest.get('chunk_ids')
            or manifest.get('chunk_count') != len(manifest['chunk_ids'])):
        raise ValueError('Existing canonical paper index is missing or changed')
    # An in-memory navigation view, never a second persisted memory_document.
    return {**manifest, 'id': document_id, 'run_id': run_id,
        'artifact_id': document['source_alias_id'], 'document_kind': 'paper_rag_source',
        'document_index_protocol': 1, 'sections': _sections(document),
        'physical_memory_document_id': physical_id, 'canonical_source_id': source_id,
        'canonical_paper_id': document['canonical_paper_id'],
        'source_format': document.get('format'),
        'source_pages': [{k: page[k] for k in ('page_number', 'page', 'start', 'end', 'text_sha256') if k in page}
                         for page in document.get('pages', [])]}, document['text']


def for_papers(engine, run_id, paper_ids):
    """Freeze only source versions of the task's explicitly selected papers."""
    from .document_index import directory
    wanted = set()
    for paper_id in paper_ids:
        paper = paper_identity.task_paper(engine.store, run_id, paper_id)
        if not paper:
            raise ValueError('Selected paper does not belong to this research')
        wanted.add(paper_identity.resolve(engine.store, paper))
    keys, found = set(), set()
    for row in engine.store.list('paper_source_bindings', run_id):
        canonical_id = paper_identity.canonical(engine.store, row['canonical_paper_id'])
        if canonical_id not in wanted:
            continue
        bound = paper_bindings.valid_binding(engine.store, run_id, row['source_id'])
        if not bound:
            raise ValueError('Selected paper source binding is invalid')
        keys.add(paper_bindings.canonical_source(engine.store, bound['rag_source_id']))
        found.add(canonical_id)
    if wanted - found:
        raise ValueError('Selected paper has no task-owned canonical RAG source')
    result = []
    for key in sorted(keys):
        shared = engine.store.get('paper_rag_sources', key)
        if not shared:
            raise ValueError('Selected canonical RAG source is missing')
        result.append(directory(engine, run_id, PREFIX + key, shared['text_sha256']))
    return result


def metadata(manifest, *, start=None, end=None):
    if manifest.get('document_kind') != 'paper_rag_source':
        return {}
    value = {k: manifest[k] for k in ('physical_memory_document_id', 'canonical_source_id',
        'canonical_paper_id', 'content_sha256', 'source_format', 'text_truncated', 'empty_pages')}
    value['source_scope'] = 'this_research_only'
    if start is not None:
        value['source_pages'] = [page for page in manifest['source_pages']
                                if page['start'] < end and page['end'] > start]
    return value
