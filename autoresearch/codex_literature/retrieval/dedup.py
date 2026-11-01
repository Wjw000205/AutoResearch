"""Deterministic identity merging, including records that bridge two indexes."""
import copy
from difflib import SequenceMatcher

from ..models import canonical_doi, canonical_arxiv, normalized_title, normalize


def identifiers(paper):
    return {kind: value for kind, value in (
        ('doi', canonical_doi(paper.get('doi'))), ('pmid', str(paper.get('pmid') or '')),
        ('arxiv', canonical_arxiv(paper.get('arxiv_id'))),
        ('s2', paper.get('semantic_scholar_id'))) if value}


def same_paper(first, second):
    a, b = identifiers(first), identifiers(second)
    # Stronger conflicting identities cannot be overridden by a title match.
    for kind in ('doi', 'pmid', 'arxiv', 's2'):
        if a.get(kind) and b.get(kind):
            return a[kind] == b[kind]
    x, y = normalized_title(first.get('title')), normalized_title(second.get('title'))
    if x and x == y:
        return True
    years = (first.get('year'), second.get('year'))
    authors = [{normalized_title(v) for v in p.get('authors', [])} for p in (first, second)]
    # Fuzzy titles need author and year corroboration; no LLM inference.
    return bool(len(x) >= 30 and len(y) >= 30 and authors[0] & authors[1]
                and all(type(v) is int for v in years) and abs(years[0] - years[1]) <= 1
                and SequenceMatcher(None, x, y).ratio() >= .96)


def deduplicate(papers):
    groups = []
    records = [normalize(p) for p in papers]

    def identity_order(paper):
        keys = identifiers(paper)
        return (*(-bool(keys.get(kind)) for kind in ('doi', 'pmid', 'arxiv', 's2')),
                *(str(keys.get(kind) or '') for kind in ('doi', 'pmid', 'arxiv', 's2')),
                paper['id'], normalized_title(paper['title']), str(paper.get('source') or ''))

    # Discover strong identities first, so a title-only record cannot attach to
    # one DOI before a conflicting DOI arrives in a later source's response.
    for paper in sorted(records, key=identity_order):
        if not paper['title']:
            continue
        matches = [g for g in groups if any(same_paper(paper, p) for p in g)
                   and all(not identifiers(p).get('doi') or not paper.get('doi')
                           or identifiers(p)['doi'] == paper['doi'] for p in g)]
        if len({identifiers(p)['doi'] for g in matches for p in g if identifiers(p).get('doi')}) > 1:
            # A title-only record cannot bridge conflicting registered DOIs.
            matches = []
        group = [paper]
        for matched in matches:
            group.extend(matched)
            groups.remove(matched)
        groups.append(group)
    results = []
    for records in groups:
        preferred = max(records, key=lambda p: (p.get('verified') is True,
            p.get('verification', {}).get('status') == 'verified', bool(p.get('doi')),
            bool(p.get('oa_pdf_url')), len(p.get('abstract') or '')))
        merged = copy.deepcopy(preferred)
        for key in ('doi', 'pmid', 'pmcid', 'arxiv_id', 'semantic_scholar_id', 'abstract',
                    'fulltext_url', 'oa_pdf_url', 'journal', 'publication_date'):
            if not merged.get(key):
                merged[key] = next((p[key] for p in records if p.get(key)), None)
        merged['sources'] = sorted({s for p in records for s in p.get('sources', [])})
        merged['identity_aliases'] = sorted({str(alias) for p in records
            for alias in [p['id'], *p.get('identity_aliases', [])]})
        merged['matched_queries'] = sorted({q for p in records for q in p.get('matched_queries', [])})
        merged['retrieval_relations'] = [r for p in records for r in p.get('retrieval_relations', [])]
        counts = [p['citation_count'] for p in records if type(p.get('citation_count')) is int]
        merged['citation_count'] = max(counts, default=None)
        years = sorted({p['year'] for p in records if type(p.get('year')) is int})
        merged['metadata_conflicts'] = {**merged.get('metadata_conflicts', {}),
            **({'publication_years': years} if len(years) > 1 else {})}
        # Access assertions travel with their exact URL, never with a title alone.
        merged['fulltext_locations'] = list({(r['url'], r['source']): r
            for p in records for r in p.get('fulltext_locations', []) if r.get('url') and r.get('source')}.values())
        results.append(merged)
    return results
