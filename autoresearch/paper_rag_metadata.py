"""Auditable search labels; original papers, analyses and reading credit stay immutable."""
from __future__ import annotations

import copy
import json
from urllib.parse import urlparse

from .storage import now
from .venue_policy import assess, catalog

COLLECTION = 'paper_rag_metadata'


def _load(rag, entry_id):
    from .paper_rag import digest
    entry_id = rag.canonical_id(entry_id)
    records = [r for r in rag.store.list(COLLECTION) if rag.canonical_id(r['entry_id']) == entry_id]
    record = max(records, key=lambda r: (r.get('updated_at', ''), r['id'])) if records else None
    if not record:
        return None
    value = json.loads(rag._path(record['snapshot']).read_bytes())
    if digest(value) != record['id'] or value['entry_id'] != record['entry_id']:
        raise ValueError('Paper classification integrity check failed')
    return value


def paper_view(rag, paper, entry_id):
    """Project current labels consistently, without changing a hashed snapshot."""
    value = copy.deepcopy(paper)
    # A reused paper can retain an earlier query's annotations in its snapshot.
    # Search builds these fields from the current query and selected analysis.
    for field in ('rag_subject_query', 'rag_matched_aspects', 'rag_evidence', 'rag_score'):
        value.pop(field, None)
    ccf = assess(paper)
    if paper.get('verified') is True and (paper.get('verification') or {}).get('venue_metadata'):
        value['ccf'] = ccf
    if ccf.get('domain'):
        source = catalog()['source_pdf'] + '#page=' + str(ccf['domain_catalog_page'])
        value.update(ccf_domain=ccf['domain'], ccf_domain_source_url=source,
            domain_classification={'basis': 'verified_ccf_venue', 'label': '已核验出版出处对应的 CCF 第七版目录分类',
                'source_url': source, 'catalog_version': ccf['catalog_version'],
                'catalog_page': ccf['catalog_page'], 'heading_page': ccf['domain_catalog_page'],
                'venue_evidence_url': ccf.get('evidence_url')})
    reviewed = _load(rag, entry_id)
    if reviewed:
        value.update(research_domain=reviewed['research_domain'], research_topics=reviewed['research_topics'],
            research_classification={k: reviewed[k] for k in ('provenance', 'reviewed_at', 'source_url', 'basis')})
        if not value.get('ccf_domain'):
            value['domain_classification'] = {'basis': 'reviewed_content',
                'label': '依据已保存的逐篇补录标注内容领域；仅用于检索', 'source_url': reviewed['source_url']}
    return value


def validate(rag, review):
    """Validate the whole label proposal before a publisher performs any writes."""
    from .paper_rag_curation import reviews
    if review.get('provenance') != 'operator_metadata_review' or not review.get('authorization'):
        raise ValueError('Paper classification requires operator authorization and provenance')
    base = rag.load(review['base_version_id'])
    if rag.canonical_id(review.get('entry_id')) != rag.canonical_id(base['entry_id']):
        raise ValueError('Paper classification identity mismatch')
    if not isinstance(review.get('research_domain'), str) or not review['research_domain'].strip():
        raise ValueError('Paper classification requires a content domain')
    topics = review.get('research_topics')
    if not isinstance(topics, list) or not topics or any(not isinstance(t, str) or not t.strip() for t in topics):
        raise ValueError('Paper classification requires nonempty topic labels')
    basis = review['basis']
    saved = next((r for r in reviews(rag, base['entry_id']) if r['curation_id'] == basis['curation_id']), None)
    section_index = basis.get('section_index')
    if not saved or type(section_index) is not int or not 0 <= section_index < len(saved['sections']):
        raise ValueError('Paper classification requires a saved review section')
    quote = basis.get('quote')
    if not isinstance(quote, str) or len(quote.strip()) < 10 or quote not in saved['sections'][section_index]['text']:
        raise ValueError('Paper classification basis does not match the saved review')
    source_url = review.get('source_url')
    if (source_url not in [s['url'] for s in saved['sources']]
            or urlparse(source_url).scheme not in {'https', 'http'}):
        raise ValueError('Paper classification source is not bound to the saved review')
    return {**copy.deepcopy(review), 'paper_id': base['paper']['id'],
        'scientific_claims_verified': False, 'grants_task_reading_credit': False,
        'grants_novelty_approval': False}


def save(rag, review):
    """Publish operator-authorized labels anchored to a saved, hashed review."""
    from .paper_rag import digest
    value = validate(rag, review)
    entry_id = value['entry_id']
    previous = rag.store.get(COLLECTION, entry_id)
    old = _load(rag, entry_id) if previous else None
    if old and {k: v for k, v in old.items() if k != 'previous_metadata_id'} == value:
        return previous
    value['previous_metadata_id'] = previous['id'] if previous else None
    metadata_id = digest(value)
    rag._write(metadata_id + '.json', json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())
    record = {'id': metadata_id, 'entry_id': entry_id, 'snapshot': metadata_id + '.json', 'updated_at': now()}
    rag.store.put(COLLECTION, entry_id, record)
    base = rag.load(review['base_version_id'])
    projected = paper_view(rag, base['paper'], entry_id)
    rag.indexer.memory_add('global', 'shared', '\n'.join([base['paper']['title'],
        projected.get('ccf_domain') or '', value['research_domain'], *value['research_topics']]),
        {'kind': 'paper_rag_metadata', 'rag_entry_id': entry_id, 'rag_version_id': review['base_version_id'],
            'metadata_id': metadata_id, 'provenance': 'operator_metadata_review', 'scientific_claims_verified': False,
            'grants_task_reading_credit': False})
    return record
