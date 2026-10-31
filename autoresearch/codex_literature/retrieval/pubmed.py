import re
import xml.etree.ElementTree as ET
from datetime import date

from ..models import normalize

BASE = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/'


def parse(xml):
    root = ET.fromstring(xml)
    records = []
    for item in root.findall('.//PubmedArticle'):
        title = ' '.join(item.find('.//ArticleTitle').itertext()) if item.find('.//ArticleTitle') is not None else ''
        pmid = item.findtext('.//MedlineCitation/PMID')
        if not pmid or not title:
            continue
        ids = {v.get('IdType'): v.text for v in item.findall('.//ArticleId')}
        pubdate = item.find('.//Journal/JournalIssue/PubDate')
        year = pubdate.findtext('Year') if pubdate is not None else None
        year = year or (re.search(r'\d{4}', pubdate.findtext('MedlineDate') or '').group()
                        if pubdate is not None and re.search(r'\d{4}', pubdate.findtext('MedlineDate') or '') else None)
        published = None
        if pubdate is not None and year:
            try:
                month = pubdate.findtext('Month')
                if month and not month.isdigit():
                    from datetime import datetime
                    month = str(datetime.strptime(month[:3], '%b').month)
                published = date(int(year), int(month), int(pubdate.findtext('Day'))).isoformat()
            except (TypeError, ValueError):
                pass  # Never invent a day for year-only metadata.
        authors = []
        for author in item.findall('.//Article/AuthorList/Author'):
            authors.append(author.findtext('CollectiveName') or ' '.join(filter(None,
                [author.findtext('ForeName'), author.findtext('LastName')])))
        journal = item.findtext('.//Journal/Title')
        records.append(normalize({'id': 'pmid:' + pmid, 'pmid': pmid, 'pmcid': ids.get('pmc'),
            'doi': ids.get('doi'), 'title': title, 'authors': authors,
            'abstract': '\n'.join(' '.join(n.itertext()) for n in item.findall('.//Abstract/AbstractText')),
            'year': int(year) if year and year.isdigit() else None, 'publication_date': published,
            'journal': journal, 'venue': {'names': [journal] if journal else [], 'kind': 'journal'},
            'url': f'https://pubmed.ncbi.nlm.nih.gov/{pmid}/', 'source': 'pubmed',
            'verified': False, 'verification': {'status': 'pending'}, 'provenance': 'live_metadata'}))
    return records


async def lookup(fetch, pmids):
    response = await fetch(BASE + 'efetch.fcgi', {'db': 'pubmed', 'id': ','.join(pmids), 'retmode': 'xml'})
    return parse(response.text)


async def search(fetch, query, limit, **options):
    params = {'db': 'pubmed', 'term': query, 'retmode': 'json', 'retmax': limit,
              'sort': 'pub date' if options.get('sort') == 'recent' else 'relevance'}
    if any(options.get(k) for k in ('date_from', 'date_to', 'year_from', 'year_to')):
        params.update(datetype='pdat', mindate=options.get('date_from') or str(options.get('year_from') or 1800),
                      maxdate=options.get('date_to') or str(options.get('year_to') or 9999))
    response = await fetch(BASE + 'esearch.fcgi', params)
    ids = response.json().get('esearchresult', {}).get('idlist', [])
    return await lookup(fetch, ids) if ids else []
