import re
from datetime import datetime, timezone

from ..models import normalize


def query_expression(query):
    """Keep Boolean operators and groups as syntax, not all:AND search terms.

    https://github.com/arXiv/arxiv-docs/blob/develop/source/help/api/user-manual.md
    """
    tokens = re.findall(r'"[^"\n]+"|[()]|[^\s()"]+', query.replace('\\"', '"'))
    parts, depth, operand = [], 0, True
    for token in tokens:
        if token == '(':
            if not operand:
                parts.append('AND')
            parts.append(token)
            depth, operand = depth + 1, True
        elif token == ')':
            if not depth or operand:
                raise ValueError('Unbalanced arXiv query group')
            parts.append(token)
            depth, operand = depth - 1, False
        elif token.upper() in {'AND', 'OR', 'ANDNOT'}:
            if operand:
                raise ValueError('arXiv Boolean operator requires a left operand')
            parts.append(token.upper())
            operand = True
        else:
            if not operand:
                parts.append('AND')
            term = token if token.startswith('"') or re.fullmatch(r'\w+', token) else '"' + token + '"'
            parts.append('all:' + term)
            operand = False
    if not parts or depth or operand:
        raise ValueError('Incomplete arXiv search query')
    return ' '.join(parts)


async def search(fetch, query, limit, **options):
    from ...lab import _arxiv_papers
    # Keep quoted phrases together; do not erase terminology variants.
    expression = query_expression(query)
    if any(options.get(k) for k in ('date_from', 'date_to', 'year_from', 'year_to')):
        lower = (options.get('date_from') or f"{options.get('year_from') or 1800}-01-01").replace('-', '')
        upper = (options.get('date_to') or f"{options.get('year_to') or datetime.now(timezone.utc).year}-12-31").replace('-', '')
        expression = f'({expression}) AND submittedDate:[{lower}0000 TO {upper}2359]'
    params = {'search_query': expression, 'start': 0, 'max_results': limit}
    if options.get('sort') == 'recent':
        params.update(sortBy='submittedDate', sortOrder='descending')
    response = await fetch('https://export.arxiv.org/api/query', params)
    return [normalize(p) for p in _arxiv_papers(response.text)]
