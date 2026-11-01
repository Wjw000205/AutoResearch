"""Explicit, metadata-verified linkage of an alternate retained paper version."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from .lab import arxiv_version_match, source_text_issue, _title_matches, _author_matches, _doi
from .literature_reading import _active, _run_papers, _save_link, SOURCE_FIELDS, NOTE


async def link_source(engine, intelligence, run_id, paper_id, source_id):
    """Link arXiv HTML/PDF using its registry identity; never accept arbitrary URLs.

    The paper's formal venue remains authoritative. Metadata linkage does not
    establish equivalence between a preprint and its published version, and does
    not count as reading. Notes still require exact inspected source passages.
    """
    _active(engine, run_id)
    paper = _run_papers(engine.store, run_id).get(paper_id)
    if not paper:
        raise ValueError('Alternate source requires a task-owned verified paper')
    from . import venue_policy
    if venue_policy.enabled(engine.store.get('runs', run_id)) and not venue_policy.assess(paper)['eligible']:
        raise ValueError('Alternate source cannot bypass the formal CCF A/B publication requirement')
    document = engine.sources.document(run_id, source_id)
    location = urlsplit(document.get('url', ''))
    match = re.fullmatch(r'/(?:abs|html|pdf)/(\d{4}\.\d{4,5}(?:v\d+)?)(?:\.pdf)?', location.path)
    if (location.scheme != 'https' or location.hostname not in {'arxiv.org', 'www.arxiv.org', 'export.arxiv.org'}
            or location.username or location.password or location.port not in (None, 443)
            or location.query or location.fragment or not match):
        raise ValueError('Only a retained arXiv HTML/PDF version can be linked; other sources remain separate evidence')
    if source_text_issue(document) or not document.get('text', '').strip():
        raise ValueError('Alternate paper source has no usable retained text')
    candidate = await engine.literature.verify({'id': match[1], 'arxiv_id': match[1]}, arxiv_only=True)
    basis = arxiv_version_match(paper, candidate)
    # Survey cards can retain a shortened supplied author list. Resolve the
    # complete formal DOI record rather than weakening one-to-one linkage or
    # editing historical cards/paid handoffs to pretend they were complete.
    supplied, alternate = paper.get('authors') or [], candidate.get('authors') or []
    if (basis is None and _doi(paper.get('doi')) and isinstance(supplied, list)
            and isinstance(alternate, list) and 0 < len(supplied) < len(alternate) <= 200
            and _title_matches(paper.get('title', ''), candidate.get('title', ''))
            and all(any(_author_matches(author, known) for known in alternate) for author in supplied)):
        formal = await engine.literature.verify({'id': paper['id'], 'doi': paper['doi']})
        complete = formal.get('authors') or []
        if (formal.get('verified') is True and formal.get('verification', {}).get('status') == 'verified'
                and _doi(formal.get('doi')) == _doi(paper['doi'])
                and _title_matches(paper.get('title', ''), formal.get('title', ''))
                and isinstance(complete, list) and len(complete) <= 200
                and all(any(_author_matches(author, known) for known in complete) for author in supplied)):
            basis = arxiv_version_match(formal, candidate)
            if basis:
                basis.update(formal_metadata_refreshed=True,
                    formal_metadata_sha256=formal['verification'].get('metadata_sha256'),
                    formal_metadata_evidence_url=formal['verification'].get('evidence_url'))
    if not basis:
        raise ValueError('Alternate source identifier does not match the verified paper title/authors or DOI')
    _active(engine, run_id)
    # Re-open after the asynchronous metadata lookup; source integrity and run
    # ownership are checked before recording any acquisition relationship.
    current = engine.sources.document(run_id, source_id)
    if any(current.get(k) != document.get(k) for k in ('text_sha256', 'content_sha256', 'url')):
        raise ValueError('Alternate source changed during identity verification')
    indexed = intelligence.memory_index_document(run_id, 'scout', engine.sources.index_document(run_id, source_id),
        source_id=source_id, artifact_id=source_id)
    result = {k: current[k] for k in SOURCE_FIELDS if k in current}
    result.update(paper_id=paper_id, source_id=source_id, artifact_id=source_id,
        memory_document_id=indexed['memory_document_id'], index_status=indexed['index_status'],
        retrieval_status='pdf_text_indexed' if current.get('format') == 'pdf' else 'web_text_indexed',
        identity_verified=True, document_identity_verified=False, scientific_claims_verified=False,
        ai_read=False, alternate_version=True, identity_match_basis=basis,
        alternate_identifier=candidate['arxiv_id'], note=NOTE,
        version_warning='Preprint/publication equivalence is not verified; retain version differences and limitations.')
    _save_link(engine.store, run_id, paper, result)
    return result
