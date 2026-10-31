"""Independent scoped lexical/optional learned-semantic memory candidate streams.

The existing SQLite inverted index supplies lexical candidates. Learned vectors
reuse the pinned local literature runtime; optional adapters are explicit and
never inferred from configured chat providers. Hash features are lexical only.
"""
from __future__ import annotations

import hashlib
import heapq
import json
import math

from .record_reads import iter_records

DEFAULTS = {'memory_semantic_enabled': False, 'memory_lexical_candidates': 64,
            'memory_semantic_candidates': 64, 'memory_top_k': 6,
            'memory_rrf_k': 60, 'memory_semantic_min_score': .2}
INACTIVE = ('invalid', 'invalidated', 'deleted', 'superseded', 'retracted', 'withdrawn')


def settings(intelligence, run_id):
    config = {**DEFAULTS, **intelligence.settings.get('defaults', {}),
              **intelligence.store.get('runs', run_id, {}).get('config', {})} if run_id else {
                  **DEFAULTS, **intelligence.settings.get('defaults', {})}
    for field in ('memory_lexical_candidates', 'memory_semantic_candidates', 'memory_top_k', 'memory_rrf_k'):
        if type(config[field]) is not int or config[field] < 1:
            raise ValueError(f'{field} must be a positive integer')
    if type(config['memory_semantic_enabled']) is not bool:
        raise ValueError('memory_semantic_enabled must be boolean')
    if type(config['memory_semantic_min_score']) not in (float, int) or not -1 <= config['memory_semantic_min_score'] <= 1:
        raise ValueError('memory_semantic_min_score must be within -1..1')
    return config


def visibility(store, run_id, role, scope):
    """Apply visibility/version checks in SQL before either channel sees text."""
    from .implementation_activity import receipt_metadata, predicate
    clauses, args = [], []
    if run_id is not None:
        clauses.append("(json_extract(data,'$.run_id') IS NULL OR json_extract(data,'$.run_id') IN (?, 'global') OR json_extract(data,'$.metadata.shared')=1)")
        args.append(run_id)
    private = "(coalesce(json_extract(data,'$.metadata.private'),0)=1 OR coalesce(json_extract(data,'$.metadata.kind'),'') IN ('role_history','private_history','execution_history'))"
    if role is None or run_id is None:
        clauses.append('NOT ' + private)
    else:
        clauses.append('(NOT ' + private + " OR (json_extract(data,'$.run_id')=? AND json_extract(data,'$.role')=?))")
        args.extend([run_id, role])
    # A scoped private history needs an exact current binding; shared scientific
    # documents retain research/global/shared semantics, not role isolation.
    for field in ('proposal_version', 'proposal_hash', 'research_cycle', 'cycle'):
        clauses.append(f"(NOT {private} OR json_extract(data,'$.metadata.{field}') IS NULL OR json_extract(data,'$.metadata.{field}') IS ?)")
        args.append((scope or {}).get(field))
    inactive = ','.join('?' for _ in INACTIVE)
    for field in ('metadata.source_status', 'metadata.status'):
        clauses.append(f"coalesce(json_extract(data,'$.{field}'),'active') NOT IN ({inactive})")
        args.extend(INACTIVE)
    clauses.append("json_extract(data,'$.metadata.superseded_by') IS NULL")
    clauses.append("NOT EXISTS(SELECT 1 FROM records manifest WHERE manifest.collection='memory_documents' AND manifest.key=json_extract(records.data,'$.document_id') AND (json_extract(manifest.data,'$.superseded_by') IS NOT NULL OR coalesce(json_extract(manifest.data,'$.source_status'),'active') IN (" + inactive + ')))')
    args.extend(INACTIVE)
    # Explicit ordered versions only: opaque versions never get an invented
    # chronology. A correlated check avoids read/modify/write races between
    # concurrent source indexers and preserves every old manifest/receipt.
    clauses.append("NOT EXISTS(SELECT 1 FROM records current JOIN records newer ON newer.collection='memory_documents' "
        "AND json_extract(newer.data,'$.run_id') IS json_extract(current.data,'$.run_id') "
        "AND json_extract(newer.data,'$.source_id')=json_extract(current.data,'$.source_id') "
        "AND json_extract(newer.data,'$.document_version_order')>json_extract(current.data,'$.document_version_order') "
        "WHERE current.collection='memory_documents' AND current.key=json_extract(records.data,'$.document_id') "
        "AND json_extract(newer.data,'$.superseded_by') IS NULL "
        "AND coalesce(json_extract(newer.data,'$.source_status'),'active') NOT IN (" + inactive + '))')
    args.extend(INACTIVE)
    active, parameters = predicate(receipt_metadata(store, run_id), 'memory')
    clauses.append(active); args.extend(parameters)
    return ' AND '.join(clauses), args


def _retain(heap, row, score, capacity, ordinal):
    # Preserve the historical stable collection order for equal score/date.
    order = (score, row.get('created_at', ''), -ordinal)
    if len(heap) < capacity:
        heapq.heappush(heap, (order, row))
    elif order > heap[0][0]:
        heapq.heapreplace(heap, (order, row))


def _overlaps(left, right):
    a, b = left.get('metadata', {}), right.get('metadata', {})
    source_a = a.get('source_id') or left.get('document_id')
    source_b = b.get('source_id') or right.get('document_id')
    version = lambda r: tuple(r.get(k) for k in ('text_sha256', 'content_sha256',
        'document_version', 'source_version', 'version', 'rag_version_id', 'page_number', 'section_id'))
    if source_a != source_b or version(a) != version(b):
        return False
    if all(type(m.get(k)) is int for m in (a, b) for k in ('char_start', 'char_end')):
        return max(a['char_start'], b['char_start']) < min(a['char_end'], b['char_end'])
    return left.get('text') == right.get('text')


def search(intelligence, query, run_id=None, limit=None, *, role=None, scope=None):
    from .intelligence import _features
    from .literature_runtime import memory_encoder, cached_vectors, cosine_score
    if not isinstance(query, str):
        raise ValueError('Memory query must be text')
    config = settings(intelligence, run_id)
    limit = config['memory_top_k'] if limit is None else limit
    if type(limit) is not int or limit < 1:
        raise ValueError('Memory limit must be a positive integer')
    intelligence._ensure_memory_index()
    visible, args = visibility(intelligence.store, run_id, role, scope)
    features, lexical = _features(query), []
    readonly = getattr(intelligence.store, '_memory_search_scan_visible', False)
    if features or not query.strip():
        terms = json.dumps(list(features), ensure_ascii=False)
        candidate = visible
        parameters = list(args)
        if features and not readonly:
            candidate += " AND (key IN (SELECT t.memory_id FROM memory_terms t JOIN json_each(?) q ON t.term=q.value) OR json_extract(data,'$.metadata.source_id')=? OR key=?)"
            parameters.extend([terms, query.strip(), query.strip()])
        with intelligence.store._read_db() as db:
            stat = db.execute("SELECT count(*),coalesce(avg(length(json_extract(data,'$.text'))),1) FROM records WHERE collection='memory' AND " + visible, args).fetchone()
            n, avg = stat[0], max(1, stat[1])
            counts = dict(db.execute("SELECT term,count(*) FROM memory_terms JOIN records ON records.collection='memory' AND records.key=memory_terms.memory_id WHERE " + visible + ' AND term IN (SELECT value FROM json_each(?)) GROUP BY term', [*args, terms]).fetchall())
        for ordinal, row in enumerate(iter_records(intelligence.store, 'memory', where=candidate, args=parameters)):
            terms = _features(row.get('text', ''))
            exact = row.get('metadata', {}).get('source_id') == query.strip() or row['id'] == query.strip()
            overlap = any(terms.get(term, 0) for term in features)
            if features and not overlap and not exact:
                continue
            score = sum(math.log(1+(max(n-counts.get(term, 0), 0)+.5)/(counts.get(term, 0)+.5)) *
                        terms[term]*2.2/(terms[term]+1.2*(.25+.75*len(row.get('text', ''))/avg))
                        for term in features if terms.get(term, 0))
            if exact:
                score += 1000
            _retain(lexical, row, score, config['memory_lexical_candidates'], ordinal)
    lexical = [dict(row, lexical_score=order[0]) for order, row in sorted(lexical, key=lambda x: x[0], reverse=True)]
    semantic, diagnostics = [], {'available': False, 'reason': 'disabled', 'fallback': 'lexical'}
    if config['memory_semantic_enabled'] and query.strip() and not readonly:
        encoder = None
        try:
            encoder = memory_encoder(intelligence)
            query_vectors = encoder.vectors(query)
            diagnostics = {'available': True, 'model': encoder.model_id, 'model_version': encoder.model_version,
                           'runtime': encoder.provenance, 'cached_chunks': 0, 'indexed_chunks': 0}
            for ordinal, row in enumerate(iter_records(intelligence.store, 'memory', where=visible, args=args)):
                vectors, cached = cached_vectors(intelligence.store, run_id, encoder, row.get('text', ''))
                diagnostics['cached_chunks'] += int(cached); diagnostics['indexed_chunks'] += 1
                score = cosine_score(query_vectors, vectors)
                if score >= config['memory_semantic_min_score']:
                    _retain(semantic, row, score, config['memory_semantic_candidates'], ordinal)
            semantic = [dict(row, semantic_score=order[0]) for order, row in sorted(semantic, key=lambda x: x[0], reverse=True)]
        except (ImportError, ValueError, RuntimeError, OSError, TypeError) as exc:
            semantic = []
            diagnostics = {'available': False, 'reason': type(exc).__name__, 'fallback': 'lexical',
                           'model': getattr(encoder, 'model_id', None),
                           'model_version': getattr(encoder, 'model_version', None),
                           'action': 'Configure an installed local embedding model or an explicit embedding adapter.'}
    merged = {}
    for channel, rows in (('lexical', lexical), ('semantic', semantic)):
        for rank, row in enumerate(rows, 1):
            item = merged.setdefault(row['id'], {'row': {}, 'score': 0., 'ranks': {}})
            item['row'].update(row); item['score'] += 1/(config['memory_rrf_k']+rank); item['ranks'][channel] = rank
    def exact(item):
        return (item['row'].get('metadata', {}).get('source_id') == query.strip()
                or item['row']['id'] == query.strip())
    ranked = sorted(merged.values(), key=lambda r: (not exact(r), -r['score'], min(r['ranks'].values()), r['row']['id']))
    results = []
    for item in ranked:
        row = item['row']
        # Re-check by primary key in case a source/version was invalidated while
        # embedding ran. Neither candidates nor the result bypass visibility.
        with intelligence.store._read_db() as db:
            if not db.execute("SELECT 1 FROM records WHERE collection='memory' AND key=? AND " + visible, [row['id'], *args]).fetchone():
                continue
        if any(_overlaps(row, selected) for selected in results):
            continue
        row = {k: v for k, v in row.items() if k not in {'features', 'vector'}}
        row.update(score=round(item['score'], 10), retrieval_method='independent-bm25+optional-embedding+rrf',
                   retrieval_ranks=item['ranks'], semantic=diagnostics,
                   learned_semantic_embeddings=diagnostics['available'] and diagnostics.get('runtime') != 'deterministic_test_embedding')
        results.append(row)
        if len(results) == limit:
            break
    if run_id and not readonly:
        # Capture this call's local values before exposing the compatibility
        # property: concurrent roles must never log another request's status.
        intelligence.store.event(run_id, 'memory.retrieval', 'Memory retrieval completed', {
            'role': role, 'query_sha256': hashlib.sha256(query.encode('utf-8')).hexdigest(),
            'candidate_counts': {'lexical': len(lexical), 'semantic': len(semantic), 'merged': len(merged)},
            'returned_count': len(results), 'available': diagnostics['available'],
            'fallback': diagnostics.get('fallback'), 'reason': diagnostics.get('reason'),
            'model': diagnostics.get('model'), 'model_version': diagnostics.get('model_version')})
    intelligence.last_memory_retrieval = diagnostics
    return results
