"""Version-bound document navigation using the existing shared memory index.

An index is navigation, never paper reading credit or scientific verification.
Only bounded, exact selections enter model inputs; the complete file stays local.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .storage import now
from .architecture_validation import existing_only

MAX_TEXT_BYTES = 4 * 1024**2


def _sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def artifact_text(engine, run_id, artifact_id):
    from .deletion import _plain_path
    artifact = engine.store.get('artifacts', artifact_id)
    if not artifact or artifact.get('run_id') != run_id:
        raise ValueError('Document does not belong to this run')
    path = Path(artifact['path']).absolute()
    _plain_path(path, engine.data_dir)
    if not path.resolve().is_relative_to(engine.run_dir(run_id)) or path.suffix != '.md':
        raise ValueError('Document must be task-owned Markdown')
    if path.stat().st_size > MAX_TEXT_BYTES:
        raise ValueError('Document exceeds local index size limit')
    text = path.read_text(encoding='utf-8')
    if _sha(text) != artifact.get('sha256'):
        raise ValueError('Document artifact SHA256 changed')
    return artifact, text


def sections(text):
    """Exact disjoint character ranges, including headings and fenced code."""
    starts, fenced = [], False
    position = 0
    for line in text.splitlines(keepends=True):
        if re.match(r'^\s{0,3}(```|~~~)', line):
            fenced = not fenced
        if not fenced and re.match(r'^#{1,3} ', line):
            starts.append((position, line.lstrip('#').strip()))
        position += len(line)
    if not starts or starts[0][0] != 0:
        starts.insert(0, (0, '文档说明'))
    return [{'id': f's{index}', 'title': title, 'start': start,
             'end': starts[index + 1][0] if index + 1 < len(starts) else len(text),
             'chars': (starts[index + 1][0] if index + 1 < len(starts) else len(text)) - start}
            for index, (start, title) in enumerate(starts)]


def index(engine, intelligence, run_id, artifact_id, *, kind='research_document'):
    artifact, text = artifact_text(engine, run_id, artifact_id)
    sha = _sha(text)
    # Content identity is independent of its artifact/source alias. Reuse the
    # existing memory_documents/chunks transaction instead of another vector DB.
    manifest = intelligence.memory_index_document(run_id, 'shared', {
        'text': text, 'content_sha256': sha, 'title': artifact['name'],
        'provenance': 'generated_research_document', 'text_truncated': False},
        source_id='document:' + sha, artifact_id=artifact_id)
    aliases = list(dict.fromkeys([*(manifest.get('artifact_ids') or [manifest['artifact_id']]), artifact_id]))
    updated = {**manifest, 'document_index_protocol': 1, 'document_kind': manifest.get('document_kind', kind),
               'artifact_ids': aliases, 'sections': sections(text)}
    if updated != manifest:
        updated['updated_at'] = now()
        engine.store.put('memory_documents', manifest['id'], updated)
    return directory(engine, run_id, updated['id'], sha)


@existing_only
def load(engine, run_id, document_id, expected_sha256):
    from . import paper_document_navigation as papers
    if papers.is_source(document_id):
        return papers.load(engine, run_id, document_id, expected_sha256)
    manifest = engine.store.get('memory_documents', document_id)
    if (not manifest or manifest.get('run_id') != run_id
            or manifest.get('document_index_protocol') != 1
            or not isinstance(expected_sha256, str) or manifest.get('text_sha256') != expected_sha256):
        raise ValueError('Document ownership or version SHA256 mismatch')
    _, text = artifact_text(engine, run_id, manifest['artifact_id'])
    if _sha(text) != expected_sha256 or manifest.get('sections') != sections(text):
        raise ValueError('Document index integrity mismatch')
    return manifest, text


def directory(engine, run_id, document_id, expected_sha256):
    from .paper_document_navigation import metadata
    manifest, _ = load(engine, run_id, document_id, expected_sha256)
    return {'document_id': document_id, 'artifact_id': manifest['artifact_id'],
            'sha256': expected_sha256, 'title': manifest['title'],
            'kind': manifest['document_kind'], 'total_chars': manifest['text_chars'],
            'chunk_count': manifest['chunk_count'], 'sections': manifest['sections'],
            'read': {'tool': 'documents.read', 'arguments': {
                'document_id': document_id, 'expected_sha256': expected_sha256,
                'section_id': 's0', 'offset': 0, 'limit': 4000}},
            'search': {'tool': 'documents.search', 'arguments': {
                'document_id': document_id, 'expected_sha256': expected_sha256, 'query': ''}},
            'primary_source_reading': False, 'scientific_claims_verified': False,
            'provenance': 'local_document_directory', **metadata(manifest)}


def list_documents(engine, run_id, args=None):
    if args is not None and not isinstance(args, dict):
        raise ValueError('Document directory arguments must be an object')
    if args:
        if set(args) != {'document_id', 'expected_sha256'}:
            raise ValueError('Document directory requires document_id and expected_sha256 together')
        return [directory(engine, run_id, args['document_id'], args['expected_sha256'])]
    from .implementation_activity import records as activity_records
    return [directory(engine, run_id, row['id'], row['text_sha256'])
            for row in activity_records(engine.store, 'memory_documents', run_id)
            if row.get('document_index_protocol') == 1]


def read(engine, run_id, args):
    from .paper_document_navigation import metadata
    manifest, text = load(engine, run_id, args.get('document_id'), args.get('expected_sha256'))
    section_id = args.get('section_id')
    selected = next((s for s in manifest['sections'] if s['id'] == section_id), None)
    if selected is None:
        raise ValueError('Document read requires a section from its index')
    offset, limit = args.get('offset', 0), args.get('limit', 4000)
    if type(offset) is not int or not 0 <= offset <= selected['chars'] or type(limit) is not int or not 1 <= limit <= 6000:
        raise ValueError('Document section offset/limit is invalid')
    end = min(selected['chars'], offset + limit)
    start = selected['start'] + offset
    return {'document_id': manifest['id'], 'artifact_id': manifest['artifact_id'],
            'sha256': manifest['text_sha256'], 'section_id': section_id, 'title': selected['title'],
            'document_version': manifest.get('document_version', manifest['text_sha256']),
            'evidence_state': 'retrieved',
            'text': text[start:selected['start'] + end], 'char_start': start,
            'char_end': selected['start'] + end, 'offset': offset, 'total_chars': selected['chars'],
            'next_offset': end if end < selected['chars'] else None, 'truncated': end < selected['chars'],
            'primary_source_reading': False, 'scientific_claims_verified': False,
            'untrusted': True, 'provenance': 'exact_document_section',
            **metadata(manifest, start=start, end=selected['start'] + end)}


def search(engine, run_id, args):
    from .intelligence import _features
    from .paper_document_navigation import metadata
    manifest, text = load(engine, run_id, args.get('document_id'), args.get('expected_sha256'))
    query, limit = args.get('query'), args.get('limit', 5)
    if not isinstance(query, str) or not query.strip() or type(limit) is not int or not 1 <= limit <= 8:
        raise ValueError('Document search requires a bounded query and limit')
    terms = _features(query)
    hits = []
    for key in manifest['chunk_ids']:
        chunk = engine.store.get('memory', key)
        if not chunk or chunk.get('document_id') != manifest.get('physical_memory_document_id', manifest['id']):
            raise ValueError('Document chunk missing or changed')
        meta = chunk['metadata']
        start, end = meta['char_start'], meta['char_end']
        if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)
                or chunk['text'] != text[start:end]):
            raise ValueError('Document chunk text integrity mismatch')
        features = _features(chunk['text'])
        score = sum(min(count, features.get(term, 0)) for term, count in terms.items())
        if not score:
            continue
        section = next(s for s in manifest['sections'] if s['start'] <= start < s['end'])
        hits.append({'chunk_id': key, 'score': score, 'section_id': section['id'],
            'title': section['title'], 'char_start': start, 'char_end': end,
            'excerpt': chunk['text'][:480], 'excerpt_truncated': len(chunk['text']) > 480,
            'read': {'tool': 'documents.read', 'arguments': {'document_id': manifest['id'],
                'expected_sha256': manifest['text_sha256'], 'section_id': section['id'],
                'offset': start - section['start'], 'limit': min(6000, section['end'] - start)}}})
    return {'document_id': manifest['id'], 'sha256': manifest['text_sha256'],
            'hits': sorted(hits, key=lambda row: (-row['score'], row['char_start']))[:limit],
            'retrieval_method': 'existing_memory_chunks_lexical', 'learned_semantic_embeddings': False,
            'primary_source_reading': False, 'scientific_claims_verified': False,
            'provenance': 'document_search_navigation', **metadata(manifest)}
