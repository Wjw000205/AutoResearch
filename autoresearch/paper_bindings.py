"""Task source IDs dynamically resolve to a canonical paper and exact bytes.

Bindings are derived references, not new reading receipts. Different content
hashes remain separate source versions of the same bibliographic paper.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

from .paper_identity import canonical, digest, register, same_paper, task_links, task_paper
from .storage import now

SOURCE_IDENTITY_PROTOCOL = 2
_read_catalogue = ContextVar('paper_binding_read_catalogue', default=None)


@contextmanager
def _using_task_papers(store, run_id, papers):
    """Borrow a note projection's directory during one synchronous loader call.

    Only data is reused. Each binding, artifact and canonical identity is still
    checked. This scope cannot authorize another store/run or outlive the
    loader, including exceptional exits; public paper directories stay owned.
    """
    if not getattr(store, 'is_read_snapshot', False):
        raise TypeError('Paper binding directory scope requires a read-only snapshot')
    token = _read_catalogue.set((store, run_id, papers))
    try:
        yield
    finally:
        _read_catalogue.reset(token)


def source_key(entry_id, document):
    """One index for exact source bytes, text and page locations.

    Per-page hashes are determined by the whole-text hash and ranges. Optional
    extraction labels belong to immutable provenance, not physical identity;
    a curated copy missing those labels must resolve to the existing index.
    Different bytes, text or page locations remain separate source versions.
    """
    return digest([SOURCE_IDENTITY_PROTOCOL, entry_id, document.get('content_sha256'),
        document['text_sha256'], [[p.get('page_number', p.get('page')), p.get('start'), p.get('end')]
            for p in document.get('pages', [])]])


def canonical_source(store, key):
    seen = set()
    while key:
        if key in seen:
            raise ValueError('Paper source redirect cycle')
        seen.add(key)
        redirect = store.get('paper_rag_source_redirects', key)
        if not redirect:
            return key
        if redirect.get('id') != key or not redirect.get('canonical_id'):
            raise ValueError('Invalid paper source redirect')
        key = redirect['canonical_id']


def bind(store, run_id, paper, reading, *, entry_id=None):
    source_id = reading.get('source_id')
    if (not source_id or reading.get('identity_verified') is not True
            or not reading.get('text_sha256') or not reading.get('content_sha256')
            or reading.get('retrieval_status') not in {'pdf_text_indexed', 'web_text_indexed'}):
        return None
    artifact = store.get('artifacts', source_id, {})
    evidence = artifact.get('evidence', {})
    if (artifact.get('id') != source_id or artifact.get('run_id') != run_id
            or artifact.get('kind') != 'source_evidence'
            or artifact.get('sha256') != reading['text_sha256']
            or evidence.get('text_sha256') != reading['text_sha256']
            or evidence.get('content_sha256') != reading['content_sha256']):
        return None
    entry_id = entry_id or register(store, paper)
    key = digest([run_id, source_id])
    document = {**evidence, 'text_sha256': reading['text_sha256']}
    value = {'id': key, 'run_id': run_id, 'source_id': source_id, 'paper_id': paper['id'],
        'canonical_paper_id': entry_id, 'rag_source_id': source_key(entry_id, document),
        'text_sha256': reading['text_sha256'], 'content_sha256': reading['content_sha256'],
        'created_at': now(), 'derived': True, 'scientific_claims_verified': False,
        'grants_reading_credit': False}
    old = store.get('paper_source_bindings', key)
    if old and all(old.get(k) == value[k] for k in value if k != 'created_at'):
        return old
    store.put('paper_source_bindings', key, value)
    return value


def valid_binding(store, run_id, source_id, *, _papers=None):
    value = store.get('paper_source_bindings', digest([run_id, source_id]))
    if not value or value.get('source_id') != source_id or value.get('run_id') != run_id:
        return None
    artifact = store.get('artifacts', source_id, {})
    evidence = artifact.get('evidence', {})
    if (artifact.get('run_id') != run_id or artifact.get('id') != source_id
            or artifact.get('sha256') != value.get('text_sha256')
            or evidence.get('text_sha256') != value.get('text_sha256')
            or evidence.get('content_sha256') != value.get('content_sha256')):
        return None
    if _papers is None:
        scope = _read_catalogue.get()
        if (scope is not None and scope[0] is store and scope[1] == run_id
                and getattr(store, 'is_read_snapshot', False)):
            _papers = scope[2]
    paper = task_paper(store, run_id, value['paper_id'], _papers)
    if not paper:
        return None
    from .paper_identity import resolve
    if resolve(store, paper) != canonical(store, value['canonical_paper_id']):
        return None
    return {**value, 'canonical_paper_id': canonical(store, value['canonical_paper_id'])}


def shared_alias(store, run_id, source_id, *, _papers=None):
    """Resolve a shared source handle through an owned acquisition reference."""
    if not isinstance(source_id, str) or not source_id.startswith('rag-source:'):
        return None
    key = canonical_source(store, source_id.removeprefix('rag-source:'))
    shared = store.get('paper_rag_sources', key)
    if not shared:
        return None
    for row in store.list('paper_source_bindings', run_id):
        if canonical_source(store, row.get('rag_source_id')) != key:
            continue
        bound = valid_binding(store, run_id, row['source_id'],
                              **({'_papers': _papers} if _papers is not None else {}))
        if (bound and canonical(store, shared['entry_id']) == bound['canonical_paper_id']
                and shared['text_sha256'] == bound['text_sha256']
                and shared['content_sha256'] == bound['content_sha256']):
            return row['source_id']
    return None


def readings_for(store, run_id, identifier, *, allow_legacy_current=False,
                 _papers=None, _links=None):
    # The note projection may supply detached, request-owned data from the
    # same SQLite snapshot. Keep public calls detached and never retain this
    # directory or acquisition history beyond that projection.
    if _papers is None:
        from .literature_reading import _run_papers
        papers = _run_papers(store, run_id)
    else:
        papers = _papers
    paper = task_paper(store, run_id, identifier, papers)
    if not paper:
        return []
    rows = []
    links = task_links(store, run_id, paper) if _links is None else (
        link for link in _links if link.get('run_id') == run_id
        and isinstance(link.get('paper'), dict)
        and same_paper(store, paper, link['paper']))
    for link in links:
        current = link.get('reading') or {}
        candidates = [current] + [r.get('result', {}) for r in link.get('reading_history', []) if isinstance(r, dict)]
        for reading in candidates:
            if not isinstance(reading, dict):
                continue
            if reading.get('identity_verified') is not True and not (
                    allow_legacy_current and reading is current and 'identity_verified' not in reading):
                continue
            declared = task_paper(store, run_id, reading.get('paper_id'), papers)
            if not declared or not same_paper(store, paper, declared):
                continue
            rows.append((link, reading))
    return rows


def same_source(store, run_id, left, right, document, *, _papers=None):
    if left == right:
        return True
    scoped = {'_papers': _papers} if _papers is not None else {}
    for identifier in (left, right):
        physical = shared_alias(store, run_id, identifier, **scoped) or identifier
        value = valid_binding(store, run_id, physical, **scoped)
        if not value or value['text_sha256'] != document.get('text_sha256') or value['content_sha256'] != document.get('content_sha256'):
            return False
        if identifier == left:
            first = value
        elif first['canonical_paper_id'] != value['canonical_paper_id']:
            return False
    return True
