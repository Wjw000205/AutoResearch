"""Explainable retrieval scores; lexical relevance is never labelled semantic AI."""
import math
from datetime import datetime, timezone

from ..models import normalized_title

WEIGHTS = {
    'relevance': dict(relevance=.50, query_coverage=.20, recency=.10, influence=.08, source_quality=.02, fulltext=.10),
    'recent': dict(relevance=.50, query_coverage=.20, recency=.20, influence=.02, source_quality=.02, fulltext=.06),
    'foundational': dict(relevance=.50, query_coverage=.20, recency=0, influence=.22, source_quality=.02, fulltext=.06),
    'mechanism': dict(relevance=.55, query_coverage=.25, recency=.05, influence=.03, source_quality=.02, fulltext=.10),
}
STOP = {'a', 'an', 'and', 'the', 'of', 'in', 'to', 'for', 'with', 'on', 'or', 'from'}


def rank_papers(papers, queries, *, mode='relevance', as_of_year=None):
    if mode not in WEIGHTS:
        raise ValueError('Unknown literature ranking mode')
    year = as_of_year or datetime.now(timezone.utc).year
    term_sets = [set(normalized_title(q).split()) - STOP for q in queries]
    results = []
    for paper in papers:
        title = set(normalized_title(paper.get('title')).split())
        abstract = set(normalized_title(paper.get('abstract') or paper.get('abstract_excerpt')).split())
        matches = [len(q & (title | abstract)) / len(q) if q else 0 for q in term_sets]
        relevance = max((.7 * len(q & title) / len(q) + .3 * len(q & abstract) / len(q)
                         for q in term_sets if q), default=0)
        values = dict(relevance=relevance,
            query_coverage=sum(v >= .4 for v in matches) / max(1, len(matches)),
            recency=max(0, 1 - max(0, year - paper['year']) / 10)
                if type(paper.get('year')) is int and paper['year'] <= year else 0,
            influence=min(1, math.log1p(max(0, paper.get('citation_count') or 0)) / math.log1p(1000)),
            source_quality=1.0 if paper.get('verified') is True else 0.0,
            fulltext=float(bool(paper.get('oa_pdf_url') or any(
                r.get('access') == 'open_access' for r in paper.get('fulltext_locations', [])))))
        results.append({**paper, 'relevance_score': round(sum(values[k] * w for k, w in WEIGHTS[mode].items()), 6),
            'ranking': {'mode': mode, 'components': values, 'weights': WEIGHTS[mode],
                        'relevance_method': 'lexical_title_abstract', 'scientific_quality_verified': False}})
    return sorted(results, key=lambda p: (not p.get('verified', False), -p['relevance_score'], str(p['id'])))
