"""Professional APIs find records; the harness owns model decisions and budgets."""
import asyncio
import re

from . import semantic_scholar, crossref, openalex, arxiv, pubmed, europe_pmc
from .dedup import deduplicate
from .ranking import rank_papers
from ..models import normalize

ADAPTERS = {m.__name__.rsplit('.', 1)[-1]: m for m in
            (semantic_scholar, crossref, openalex, arxiv, pubmed, europe_pmc)}
DEFAULT_SOURCES = ('semantic_scholar', 'crossref', 'openalex', 'arxiv')


def biomedical(query):
    return bool(re.search(r'\b(biomedical|pubmed|cancer|tumou?r|mitochondri\w*|clinical|patient\w*|'
                          r'chemoresistan\w*|cell\w*|protein\w*|genom\w*|therap\w*|disease\w*)\b', query, re.I)
                or re.search(r'生医|医学|肿瘤|线粒体|细胞|临床|耐药', query))


def safe_error(source, exc):
    return {'source': source, 'error': type(exc).__name__,
            'status_code': getattr(getattr(exc, 'response', None), 'status_code', None)}


class LiteratureRetriever:
    def __init__(self, service):
        self.service = service

    async def fetch_candidates(self, query, limit, *, sources=None, **options):
        names = list(sources or DEFAULT_SOURCES)
        if sources is None and biomedical(query):
            names.extend(['pubmed', 'europe_pmc'])
        if any(n not in ADAPTERS for n in names):
            raise ValueError('Unknown scholarly source')
        batches = await asyncio.gather(*(ADAPTERS[s].search(self.service._fetch, query, limit, **options)
                                        for s in names), return_exceptions=True)
        return names, batches

    async def search(self, query, *, top_k=20, **options):
        if type(top_k) is not int or not 1 <= top_k <= 120:
            raise ValueError('top_k must be between 1 and 120')
        report = await self.service.search_with_report(query, limit=top_k, **options)
        return [normalize(p) for p in report['papers'][:top_k]]

    async def expand_citations(self, paper, *, backwards=True, forwards=True, limit=10):
        if type(limit) is not int or not 1 <= limit <= 20 or type(backwards) is not bool or type(forwards) is not bool:
            raise ValueError('Citation expansion requires boolean directions and limit 1..20')
        if not backwards and not forwards:
            raise ValueError('Select at least one citation direction')
        relations = [r for enabled, r in ((backwards, 'references'), (forwards, 'citations')) if enabled]
        batches = await asyncio.gather(*(semantic_scholar.neighbors(self.service._fetch, paper, r, limit)
                                        for r in relations), return_exceptions=True)
        errors = [safe_error(r, b) for r, b in zip(relations, batches) if isinstance(b, Exception)]
        records = deduplicate([p for batch in batches if isinstance(batch, list) for p in batch])
        # The edge is bibliographic metadata. Each neighbor still needs its own
        # identifier/title/author verification before acquiring a full text.
        verified = []
        for start in range(0, len(records), 6):
            verified.extend(await asyncio.gather(*(self.service.verify(p) for p in records[start:start + 6])))
        return {'papers': verified, 'seed_paper_id': paper['id'], 'errors': errors,
                'directions': relations, 'limit_per_direction': limit,
                'exhaustive': False, 'provenance': 'live_citation_metadata', 'full_text_read_count': 0}

    @staticmethod
    def merge_and_rank(papers, queries, *, mode='relevance', as_of_year=None):
        return rank_papers(deduplicate(papers), queries, mode=mode, as_of_year=as_of_year)
