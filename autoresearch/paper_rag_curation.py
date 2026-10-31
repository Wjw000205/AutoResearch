"""Explicit operator-authorized supplements, separate from autonomous reading notes.

No model calls, task reading receipts, novelty decisions, or run state changes.
Source files and reviewed passages remain content-addressed and independently readable.
"""
from __future__ import annotations

import copy
import hashlib
import json

from .paper_rag import digest
from .storage import now


COLLECTION = 'paper_rag_curation'


def save(rag, version_id, review, sources):
    """Publish an explicitly authored review after checking every source/locator.

    sources contain extracted text and original bytes. Callers must establish paper
    identity; hashes establish integrity, not identity or scientific correctness.
    This function is deliberately not exposed as an autonomous agent write tool.
    """
    base = rag.load(version_id)
    if review.get('provenance') != 'codex_manual_review' or not review.get('authorization'):
        raise ValueError('Curation requires explicit provenance and authorization')
    if rag.canonical_id(review.get('entry_id')) != rag.canonical_id(base['entry_id']):
        raise ValueError('Curation paper identity mismatch')
    documents = {}
    originals = {}
    for source in sources:
        item = copy.deepcopy(source)
        body = item.pop('original_bytes')
        text = item['text']
        text_sha = hashlib.sha256(text.encode()).hexdigest()
        if text_sha != item['text_sha256'] or hashlib.sha256(body).hexdigest() != item['content_sha256']:
            raise ValueError('Curation source integrity check failed')
        item['original'] = item['content_sha256'] + ('.pdf' if body.startswith(b'%PDF') else '.bin')
        if not item.get('url') or not item.get('identity_basis'):
            raise ValueError('Curation source requires a source URL and identity basis')
        item.update(entry_id=base['entry_id'], provenance='curation_source', scientific_claims_verified=False)
        if item.get('existing_version_id'):
            existing = rag.load(item['existing_version_id'])
            if rag.canonical_id(existing['entry_id']) != rag.canonical_id(base['entry_id']) or existing['document']['text'] != text:
                raise ValueError('Existing source version does not match curated paper')
        documents[text_sha] = item
        originals[item['original']] = body
    if not review.get('sections') or not review.get('remaining_gaps'):
        raise ValueError('Curation must disclose content and remaining verification gaps')
    for section in review['sections']:
        if not section.get('text') or not section.get('citations'):
            raise ValueError('Every curation section requires evidence locators')
        for ref in section['citations']:
            source = documents.get(ref['text_sha256'])
            start, end = ref['start'], ref['end']
            if not source or type(start) is not int or type(end) is not int or not 0 <= start < end <= len(source['text']):
                raise ValueError('Invalid curation citation range')
            if hashlib.sha256(source['text'][start:end].encode()).hexdigest() != ref['passage_sha256']:
                raise ValueError('Curation citation integrity check failed')
    # Validate the entire review before any publication/indexing.
    for name, body in originals.items():
        rag._write(name, body)
    source_records = []
    for sha, item in documents.items():
        source_id = digest(item)
        rag._write(source_id + '.json', json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())
        source_records.append({'text_sha256': sha, 'snapshot': source_id + '.json',
            'url': item['url'], 'original': item['original'], 'chars': len(item['text']),
            'section': 'curation_source:' + sha})
    payload = {**copy.deepcopy(review), 'base_version_id': version_id, 'sources': source_records,
        'scientific_claims_verified': False, 'generation': 'codex_manual_review',
        'reading_scope': 'selected_methods_results_limitations', 'grants_task_reading_credit': False,
        'grants_novelty_approval': False}
    review_id = digest(payload)
    rag._write(review_id + '.json', json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())
    metadata = {'kind': 'paper_rag_curation', 'shared': True, 'rag_entry_id': base['entry_id'],
        'rag_version_id': version_id, 'curation_id': review_id, 'paper_id': base['paper']['id'],
        'provenance': 'codex_manual_review', 'scientific_claims_verified': False,
        'read_tool': 'literature.rag_read', 'grants_task_reading_credit': False}
    rag.indexer.memory_add('global', 'shared', base['paper']['title'] + '\n' + json.dumps(payload, ensure_ascii=False), metadata)
    for item in documents.values():
        rag.index_source(base['entry_id'], version_id, {**item, 'title': base['paper']['title'], 'snapshot': digest(item) + '.json'})
    rag.store.put(COLLECTION, review_id, {'id': review_id, 'entry_id': base['entry_id'],
        'snapshot': review_id + '.json', 'created_at': review['reviewed_at'], 'indexed_at': now()})
    return {'id': review_id, 'source_count': len(documents), 'scientific_claims_verified': False}


def _load(rag, name):
    body = rag._path(name).read_bytes()
    payload = json.loads(body)
    if digest(payload) + '.json' != name:
        raise ValueError('Curation snapshot integrity check failed')
    return payload


def reviews(rag, entry_id):
    entry_id = rag.canonical_id(entry_id)
    records = [r for r in rag.store.list(COLLECTION) if rag.canonical_id(r.get('entry_id')) == entry_id]
    result = []
    for record in sorted(records, key=lambda r: r['created_at'], reverse=True):
        value = _load(rag, record['snapshot'])
        if rag.canonical_id(value['entry_id']) != entry_id:
            raise ValueError('Curation entry identity mismatch')
        result.append({'curation_id': record['id'], **value})
    return result


def source_text(rag, entry_id, sha):
    for review in reviews(rag, entry_id):
        for source in review['sources']:
            if source['text_sha256'] != sha:
                continue
            value = _load(rag, source['snapshot'])
            if rag.canonical_id(value['entry_id']) != rag.canonical_id(entry_id) or hashlib.sha256(value['text'].encode()).hexdigest() != sha:
                raise ValueError('Curation source identity mismatch')
            return value['text']
    raise ValueError('Curation source does not belong to this paper')


def analysis_bundle(rag, payload):
    supplemental = reviews(rag, payload['entry_id'])
    from .paper_rag_metadata import paper_view
    entry_id = rag.canonical_id(payload['entry_id'])
    bundle = {'paper': paper_view(rag, payload['paper'], entry_id), 'canonical_paper_id': entry_id,
              'identifiers': rag.store.get('paper_identities', entry_id, {}).get('identifiers', []),
              'source_versions': [{k: value[k] for k in ('id', 'source_id', 'content_sha256', 'text_sha256', 'versions')}
                  for value in rag.store.list('paper_rag_sources') if rag.canonical_id(value['entry_id']) == entry_id
                      and not rag.store.get('paper_rag_source_redirects', value['id'])]}
    if supplemental:
        bundle['manual_supplements'] = supplemental
        bundle['scope_notice'] = '用户授权的 Codex 逐篇补录；保留作者报告值及核查疑点，未独立复现，不代替本任务阅读或新颖性审查。'
    bundle.update(analysis=payload['note'], origin_run_id=payload['origin_run_id'])
    bundle.update(provenance=payload.get('provenance'), analysis_created_at=payload['analysis_created_at'])
    if payload.get('provenance') == 'external_agent_analysis':
        bundle['scope_notice'] = '外部 Agent 提交的分析；引文已匹配所提交文本，论文身份及文本与原件的一致性未独立核验，不计入任务阅读或新颖性审批。'
        bundle['external_source'] = {k: payload['document'].get(k) for k in
            ('url', 'reported_coverage', 'missing_parts', 'extraction_method', 'text_sha256', 'content_sha256')}
    # Keep distinct provenance/locators instead of overwriting an older detailed note
    # with a newer appendix-only note. Read lazily, never inject whole papers into prompts.
    entry = rag.store.get('paper_rag', entry_id, {})
    versions = entry.get('versions', [])
    if len(versions) > 1:
        bundle['other_analysis_versions'] = [
            {'version_id': v, 'analysis': other['note'], 'origin_run_id': other['origin_run_id'],
             'analysis_created_at': other['analysis_created_at']}
            for v in versions if v != digest(payload) and (other := rag.load(v))]
    return bundle
