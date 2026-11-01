"""Local library browsing. Classifications are labels, never admission evidence."""
from __future__ import annotations

from collections import Counter

UNKNOWN = '__unknown__'
SORTS = {'relevance', 'publication_recent', 'analysis_recent', 'title'}


def labels(paper):
    venue = paper.get('venue') or {}
    names = venue.get('names', []) if isinstance(venue, dict) else [venue]
    ccf = paper.get('ccf') or {}
    # Prefer the canonical abbreviation when one exists; preserve supplied names otherwise.
    abbr = ccf.get('abbr') or paper.get('ccf_abbr')
    venues = [abbr] if abbr else names
    year = paper.get('year')
    if type(year) is not int:
        published = str(paper.get('publication_date') or '')
        year = int(published[:4]) if published[:4].isdigit() else None
    return {'venues': sorted({str(v).strip() for v in venues if v and str(v).strip()}),
            'domain': paper.get('ccf_domain') or paper.get('research_domain') or None,
            'topics': sorted(set(paper.get('research_topics') or [])),
            'year': year,
            # Candidate ranks (including externally submitted ranks) are not verified CCF ranks.
            'rank': ccf.get('rank') if paper.get('verified') is True and ccf.get('reason') != 'venue_unverified' else None}


def facets(papers):
    counts = {key: Counter() for key in ('venues', 'domains', 'years', 'ranks', 'topics')}
    for paper in papers:
        meta = labels(paper)
        for venue in meta['venues'] or [UNKNOWN]:
            counts['venues'][venue] += 1
        counts['domains'][meta['domain'] or UNKNOWN] += 1
        counts['years'][str(meta['year']) if meta['year'] else UNKNOWN] += 1
        counts['ranks'][meta['rank'] or UNKNOWN] += 1
        for topic in meta['topics']:
            counts['topics'][topic] += 1
    result = {}
    for key, counter in counts.items():
        values = sorted(counter, key=lambda value: (value == UNKNOWN, value.casefold()))
        if key == 'years':
            values = sorted(counter, key=lambda value: int(value) if value != UNKNOWN else 0, reverse=True)
        result[key] = [{'value': value, 'label': '未记录 / 待核验' if value == UNKNOWN and key == 'ranks'
                        else '未记录' if value == UNKNOWN else value, 'count': counter[value]} for value in values]
    return result


def browse(rag, query='', *, venue='', domain='', topic='', year_from=None, year_to=None,
           ccf_rank='', sort='relevance', offset=0, limit=None):
    if sort not in SORTS or offset < 0 or (limit is not None and not 1 <= limit <= 100):
        raise ValueError('Invalid library pagination or sort')
    if year_from and year_to and year_from > year_to:
        raise ValueError('起始年份不能晚于结束年份')
    entries = rag.store.list('paper_rag')
    from .paper_rag_metadata import paper_view
    papers = [{**paper_view(rag, entry['paper'], entry['id']), 'rag_entry_id': entry['id'], 'rag_version_id': entry['latest_version'],
               'rag_analysis_at': entry['analysis_created_at']} for entry in entries]
    # Filter the complete candidate set before paginating. The agent-facing search
    # remains bounded; a library page must not silently lose matches after hit 50.
    hits = rag.search(query, limit=None) if query.strip() else papers
    filtered = []
    for paper in hits:
        meta = labels(paper)
        if venue and venue not in (meta['venues'] or [UNKNOWN]):
            continue
        if domain and domain != (meta['domain'] or UNKNOWN):
            continue
        if topic and topic not in meta['topics']:
            continue
        if ccf_rank and ccf_rank != (meta['rank'] or UNKNOWN):
            continue
        if (year_from or year_to) and (meta['year'] is None or
                not (year_from or 1) <= meta['year'] <= (year_to or 9999)):
            continue
        filtered.append({**paper, 'rag_catalog': meta})
    # A stable tie-breaker keeps adjacent pages disjoint at equal dates/scores.
    filtered.sort(key=lambda p: (str(p.get('title') or '').casefold(), p['rag_entry_id']))
    if sort == 'publication_recent':
        filtered.sort(key=lambda p: (p['rag_catalog']['year'] or 0, p.get('publication_date') or ''), reverse=True)
    elif sort == 'analysis_recent' or (sort == 'relevance' and not query.strip()):
        filtered.sort(key=lambda p: p.get('rag_analysis_at') or '', reverse=True)
    elif sort == 'relevance':
        filtered.sort(key=lambda p: p.get('rag_score', 0), reverse=True)
    count = len(filtered)
    page = filtered[offset:offset + limit] if limit is not None else filtered[offset:]
    rag._coverage_hints(page)
    return {'papers': page, 'matched_count': count, 'offset': offset, 'limit': limit,
            'has_more': offset + len(page) < count, 'facets': facets(papers), 'paper_count': len(entries),
            'version_count': len(rag.store.list('paper_rag_versions')),
            'source_index_count': sum(not rag.store.get('paper_rag_source_redirects', s['id']) for s in rag.store.list('paper_rag_sources')),
            'identity_policy': 'one_canonical_paper_with_source_and_analysis_versions',
            'manual_supplement_count': len(rag.store.list('paper_rag_curation')),
            'reuse_count': len(rag.store.list('paper_rag_reuses')),
            'pending_count': sum(j['status'] == 'pending' for j in rag.store.list('paper_rag_jobs')),
            'method': 'SQLite 倒排索引 + 词项与特征哈希排序（本地检索，无额外模型费用）',
            'policy': '优先检索共享论文RAG；数量、相关性不足或近期检索已过期时联网补查；合格分析自动入库。'}
