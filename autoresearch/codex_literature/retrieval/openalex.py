from ..models import normalize


async def search(fetch, query, limit, **options):
    from ...lab import _openalex_paper
    params = {'search': query, 'per-page': min(100, limit)}
    if options.get('sort') == 'recent':
        params['sort'] = 'publication_date:desc'
    filters = []
    for field, date, year, suffix in [('from_publication_date', 'date_from', 'year_from', '-01-01'),
                                     ('to_publication_date', 'date_to', 'year_to', '-12-31')]:
        if options.get(date) or options.get(year):
            filters.append(field + ':' + (options.get(date) or str(options[year]) + suffix))
    if filters:
        params['filter'] = ','.join(filters)
    response = await fetch('https://api.openalex.org/works', params)
    return [normalize(_openalex_paper(p)) for p in response.json().get('results', []) if isinstance(p, dict)]
