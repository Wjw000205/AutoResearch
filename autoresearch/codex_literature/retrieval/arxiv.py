import re
from datetime import datetime, timezone

from ..models import normalize


async def search(fetch, query, limit, **options):
    from ...lab import _arxiv_papers
    # Keep quoted phrases together; do not erase terminology variants.
    terms = re.findall(r'"[^"\n]+"|[^\W_]+', query)
    expression = ' AND '.join('all:' + term for term in terms)
    if any(options.get(k) for k in ('date_from', 'date_to', 'year_from', 'year_to')):
        lower = (options.get('date_from') or f"{options.get('year_from') or 1800}-01-01").replace('-', '')
        upper = (options.get('date_to') or f"{options.get('year_to') or datetime.now(timezone.utc).year}-12-31").replace('-', '')
        expression = f'({expression}) AND submittedDate:[{lower}0000 TO {upper}2359]'
    params = {'search_query': expression, 'start': 0, 'max_results': limit}
    if options.get('sort') == 'recent':
        params.update(sortBy='submittedDate', sortOrder='descending')
    response = await fetch('https://export.arxiv.org/api/query', params)
    return [normalize(p) for p in _arxiv_papers(response.text)]
