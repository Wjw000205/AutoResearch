"""Resolve lawful full text independently of search and download.

No keyword Unpaywall search, paywall bypass, or fabricated contact email.
Returned locations are candidates; only actual source ingestion proves access.
"""
import os
from urllib.parse import quote, urlparse

from ..models import canonical_doi, canonical_arxiv
from . import europe_pmc
from .dedup import same_paper
from .retriever import safe_error


class FullTextResolver:
    def __init__(self, service):
        self.service = service

    async def resolve(self, paper, *, user_sources=()):
        candidates, errors = [], []
        copies = [s for s in user_sources if s.get('format') == 'pdf' and s.get('source_id')]
        if copies:
            return {'paper_id': paper['id'], 'status': 'located', 'candidates': [
                {'source_id': s['source_id'], 'source': s.get('source') or 'user_pdf',
                 'access': s.get('access') or 'user_provided', 'origin_provenance': s.get('origin_provenance'),
                 'format': 'pdf', 'evidence': 'task_owned_legal_copy'} for s in copies][:8],
                'errors': [], 'unpaywall': 'not_needed', 'provenance': 'lawful_fulltext_resolution',
                'downloaded': False, 'ai_read': False}

        def add(url, source, evidence, *, format='pdf', access='open_access'):
            parsed = urlparse(str(url or ''))
            if (parsed.scheme in {'http', 'https'} and parsed.hostname and not parsed.username
                    and not parsed.password and len(url) <= 2048 and not any(ord(c) < 32 for c in url)):
                if not any(c.get('url') == url for c in candidates):
                    candidates.append({'url': url, 'source': source, 'access': access,
                                       'format': format, 'evidence': evidence})

        async def attempt(source, callback):
            try:
                await callback()
            except Exception as exc:
                errors.append(safe_error(source, exc))

        doi = canonical_doi(paper.get('doi'))
        # Europe PMC must assert OA. A PMCID alone is not a reuse licence.
        for row in paper.get('fulltext_locations', []):
            if row.get('source') == 'europe_pmc' and row.get('access') == 'open_access':
                add(row['url'], 'europe_pmc', row.get('evidence'), format=row.get('format', 'xml'))

        async def epmc():
            if candidates or not (doi or paper.get('pmid') or paper.get('pmcid')):
                return
            expression = ('DOI:' + doi if doi else 'EXT_ID:' + str(paper['pmid']) + ' AND SRC:MED'
                          if paper.get('pmid') else 'PMCID:' + str(paper['pmcid']))
            found = await europe_pmc.search(self.service._fetch, expression, 3)
            for record in found:
                if same_paper(paper, record) and record.get('is_oa') is True:
                    for row in record.get('fulltext_locations', []):
                        add(row['url'], 'europe_pmc', row['evidence'], format='xml')
        await attempt('europe_pmc', epmc)

        async def arxiv_location():
            arxiv = canonical_arxiv(paper.get('arxiv_id'))
            if not arxiv:
                return
            from ...lab import arxiv_version_match
            record = await self.service.verify({'id': 'arxiv:' + arxiv, 'arxiv_id': paper['arxiv_id']}, arxiv_only=True)
            from ...lab import _digest
            stored = self.service.store.get('literature', _digest(paper['id']))
            original = stored if stored and stored.get('verified') is True and same_paper(paper, stored) else paper
            direct_record = (paper.get('verification', {}).get('method') == 'arxiv_identifier_record'
                             and canonical_arxiv(record.get('arxiv_id')) == arxiv and record.get('verified') is True)
            if direct_record or arxiv_version_match(original, record):
                add('https://arxiv.org/pdf/' + record['arxiv_id'], 'arxiv', 'verified_arxiv_version_link')
        await attempt('arxiv', arxiv_location)

        async def openalex():
            if not doi:
                return
            response = await self.service._fetch('https://api.openalex.org/works/' + quote('https://doi.org/' + doi, safe=''))
            item = response.json()
            if canonical_doi(item.get('doi')) != doi:
                raise ValueError('OA resolver returned a different DOI')
            for loc in [item.get('best_oa_location'), *(item.get('locations') or [])]:
                if isinstance(loc, dict) and loc.get('is_oa') is True:
                    add(loc.get('pdf_url'), 'openalex', 'DOI and location.is_oa=true')
        await attempt('openalex', openalex)

        async def unpaywall():
            email = os.environ.get('UNPAYWALL_EMAIL', '').strip()
            if not doi or not email:
                return
            response = await self.service._fetch('https://api.unpaywall.org/v2/' + quote(doi, safe=''), {'email': email})
            item = response.json()
            if canonical_doi(item.get('doi')) != doi:
                raise ValueError('OA resolver returned a different DOI')
            if item.get('is_oa') is True:
                for loc in [item.get('best_oa_location'), *(item.get('oa_locations') or [])]:
                    if isinstance(loc, dict):
                        add(loc.get('url_for_pdf'), 'unpaywall', 'DOI and is_oa=true')
        await attempt('unpaywall', unpaywall)

        # Publisher/S2 links need a location-specific OA assertion. Crossref's
        # generic PDF link is a TDM link and does not itself establish open access.
        for row in paper.get('fulltext_locations', []):
            if row.get('access') == 'open_access' and row.get('source') != 'arxiv':
                add(row['url'], row['source'], row.get('evidence'),
                    format=row.get('format', 'pdf'), access=row['access'])
        # Independently confirm the OA URL rather than trusting an LLM-supplied
        # access assertion on literature.verify arguments.
        async def scholar_oa():
            from . import semantic_scholar
            if not (doi or paper.get('semantic_scholar_id')):
                return
            record = await semantic_scholar.lookup(self.service._fetch, paper)
            if record and same_paper(paper, record) and record.get('oa_pdf_url'):
                add(record['oa_pdf_url'], 'semantic_scholar', 'identifier_lookup.openAccessPdf')
        await attempt('semantic_scholar', scholar_oa)
        return {'paper_id': paper['id'], 'status': 'located' if candidates else 'unavailable',
                'candidates': candidates[:8], 'errors': errors,
                'unpaywall': 'doi_lookup' if os.environ.get('UNPAYWALL_EMAIL') else 'not_configured',
                'provenance': 'lawful_fulltext_resolution', 'downloaded': False, 'ai_read': False}
