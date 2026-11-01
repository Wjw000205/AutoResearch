from ..models import normalize

BASE = 'https://www.ebi.ac.uk/europepmc/webservices/rest'


def _author_name(author):
    # Core fullName is surname-first (e.g. "Deng W"). Use the structured
    # fields to match the given-name-first convention of other adapters.
    family = str(author.get('lastName') or '').strip()
    if family:
        given = str(author.get('firstName') or '').strip()
        if not given:
            # Separate combined initials so "NF Saba" becomes "N F Saba".
            given = ' '.join(c for c in str(author.get('initials') or '') if c.isalpha())
        return ' '.join(part for part in (given, family) if part)
    # Do not guess how to split unstructured or collective names.
    return str(author.get('fullName') or author.get('collectiveName') or '').strip()


def parse(item):
    pmid = item.get('id') if item.get('source') == 'MED' else item.get('pmid')
    pmcid = item.get('pmcid')
    journal = (item.get('journalInfo') or {}).get('journal') or {}
    oa = item.get('isOpenAccess') == 'Y'
    xml_url = BASE + '/' + pmcid + '/fullTextXML' if pmcid and oa else None
    authors = [name for a in (item.get('authorList') or {}).get('author', []) if (name := _author_name(a))]
    return normalize({'id': 'pmid:' + str(pmid) if pmid else 'epmc:' + str(item.get('id')),
        'pmid': pmid, 'pmcid': pmcid, 'doi': item.get('doi'), 'title': item.get('title') or '',
        'abstract': item.get('abstractText'), 'authors': authors,
        'year': int(item['pubYear']) if str(item.get('pubYear', '')).isdigit() else None,
        'journal': journal.get('title'), 'venue': {'names': [journal['title']] if journal.get('title') else [],
            'issn': [journal['issn']] if journal.get('issn') else [], 'kind': 'journal'},
        'publication_date': item.get('firstPublicationDate'), 'citation_count': item.get('citedByCount'),
        'url': 'https://europepmc.org/article/' + str(item.get('source')) + '/' + str(item.get('id')),
        'is_oa': oa, 'fulltext_url': xml_url, 'source': 'europe_pmc',
        'fulltext_locations': [{'url': xml_url, 'source': 'europe_pmc', 'access': 'open_access',
                               'format': 'xml', 'evidence': 'isOpenAccess=Y'}] if xml_url else [],
        'verified': False, 'verification': {'status': 'pending'}, 'provenance': 'live_metadata'})


async def search(fetch, query, limit, **options):
    expression = '(' + query + ')'
    if any(options.get(k) for k in ('date_from', 'date_to', 'year_from', 'year_to')):
        lower = options.get('date_from') or str(options.get('year_from') or 1800) + '-01-01'
        upper = options.get('date_to') or str(options.get('year_to') or 9999) + '-12-31'
        expression += f' AND FIRST_PDATE:[{lower} TO {upper}]'
    params = {'query': expression, 'format': 'json', 'resultType': 'core', 'pageSize': limit}
    if options.get('sort') == 'recent':
        params['sort'] = 'FIRST_PDATE_D desc'
    response = await fetch(BASE + '/search', params)
    return [parse(p) for p in response.json().get('resultList', {}).get('result', []) if p.get('title')]
