from ..models import normalize


async def search(fetch, query, limit, **options):
    from ...lab import _crossref_paper
    params = {'query.title' if options.get('ccf_ab_only') else 'query.bibliographic': query,
              'rows': 120 if options.get('ccf_ab_only') else limit}
    if options.get('sort') == 'recent' and not options.get('ccf_ab_only'):
        params.update(sort='published', order='desc')
    filters = []
    for field, date, year, suffix in [('from-pub-date', 'date_from', 'year_from', '-01-01'),
                                     ('until-pub-date', 'date_to', 'year_to', '-12-31')]:
        if options.get(date) or options.get(year):
            filters.append(field + ':' + (options.get(date) or str(options[year]) + suffix))
    if filters:
        params['filter'] = ','.join(filters)
    response = await fetch('https://api.crossref.org/works', params)
    return [normalize(_crossref_paper(p)) for p in response.json().get('message', {}).get('items', []) if isinstance(p, dict)]
