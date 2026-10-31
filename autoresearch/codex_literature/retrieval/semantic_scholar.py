from urllib.parse import quote

from ..models import normalize, canonical_doi

BASE = 'https://api.semanticscholar.org/graph/v1/paper'
FIELDS = 'paperId,title,abstract,authors,year,externalIds,url,venue,publicationDate,citationCount,openAccessPdf,publicationTypes'


def parse(item):
    ids = item.get('externalIds') or {}
    oa = item.get('openAccessPdf') or {}
    url = oa.get('url') if isinstance(oa, dict) else None
    return normalize({'id': 's2:' + str(item['paperId']), 'semantic_scholar_id': item['paperId'],
        'title': item.get('title') or '', 'abstract': item.get('abstract'),
        'authors': [a['name'] for a in item.get('authors', []) if a.get('name')],
        'year': item.get('year'), 'doi': canonical_doi(ids.get('DOI')), 'pmid': ids.get('PubMed'),
        'pmcid': ids.get('PubMedCentral'), 'arxiv_id': ids.get('ArXiv'),
        'journal': item.get('venue'), 'venue': {'names': [item['venue']] if item.get('venue') else []},
        'publication_date': item.get('publicationDate'), 'citation_count': item.get('citationCount'),
        'url': item.get('url'), 'oa_pdf_url': url, 'fulltext_url': url,
        'publication_types': item.get('publicationTypes') or [], 'source': 'semantic_scholar',
        'fulltext_locations': [{'url': url, 'source': 'semantic_scholar', 'access': 'open_access',
            'format': 'pdf', 'evidence': 'openAccessPdf'}] if url else [],
        'verified': False, 'verification': {'status': 'pending'}, 'provenance': 'live_metadata'})


async def search(fetch, query, limit, **options):
    params = {'query': query, 'limit': min(100, limit), 'fields': FIELDS}
    if options.get('year_from') or options.get('year_to'):
        params['year'] = f"{options.get('year_from') or ''}-{options.get('year_to') or ''}"
    response = await fetch(BASE + '/search', params)
    return [parse(p) for p in response.json().get('data', []) if p.get('paperId') and p.get('title')]


def identifier(paper):
    return (str(paper.get('semantic_scholar_id') or '') or
            ('DOI:' + canonical_doi(paper['doi']) if canonical_doi(paper.get('doi')) else '') or
            ('PMID:' + str(paper['pmid']) if paper.get('pmid') else '') or
            ('ARXIV:' + str(paper['arxiv_id']) if paper.get('arxiv_id') else ''))


async def lookup(fetch, paper):
    key = identifier(paper)
    if not key:
        return None
    response = await fetch(BASE + '/' + quote(key, safe=''), {'fields': FIELDS})
    item = response.json()
    return parse(item) if item.get('paperId') else None


async def neighbors(fetch, paper, relation, limit):
    key = identifier(paper)
    if not key:
        return []
    response = await fetch(BASE + '/' + quote(key, safe='') + '/' + relation,
                           {'fields': FIELDS, 'limit': limit})
    field = 'citedPaper' if relation == 'references' else 'citingPaper'
    found = []
    for row in response.json().get('data', []):
        item = row.get(field) or {}
        if item.get('paperId') and item.get('title'):
            record = parse(item)
            record['retrieval_relations'] = [{'seed_paper_id': paper['id'], 'relation': relation}]
            found.append(record)
    return found
