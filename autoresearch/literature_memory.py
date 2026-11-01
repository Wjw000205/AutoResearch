"""Layered paper memory: immutable originals, hybrid retrieval and scoped observations.

Text maps are heuristic; uninspected images and missing supplements stay gaps.
Learned embeddings are reported only when the local model actually ran.
"""
from __future__ import annotations

import copy
import hashlib
import math
import re
from collections import Counter

from .context_compiler import encoded, fingerprint, _terms
from . import context_reduction as reduction
from .storage import now
from .compression_safety import ADVERSE_KINDS

ADVERSE = ADVERSE_KINDS
HEADINGS = re.compile(r'(?im)^(?:\s{0,3}#{1,4}\s+[^\n]{1,140}|\s{0,3}(?:\d+(?:\.\d+)*\.?\s+)?'
    r'(?:abstract|introduction|background|related work|method(?:s|ology)?|model|experiment(?:s|al setup)?|'
    r'results|discussion|conclusion(?:s)?|limitations?|supplement(?:ary)?[^\n]{0,70}|appendix[^\n]{0,70}|'
    r'proof[^\n]{0,70}|references|方法|实验|结果|局限|附录|补充材料)\s*)$')
NEGATIVE = re.compile(r'(?i)\b(?:fail\w*|limitation\w*|however|worse|confound\w*|contradict\w*|'
                      r'negative result|not significant|alternative explanation)\b|局限|失败|不显著|混杂|反例|反证')
SEARCH_DEFAULTS = {'evidence_top_k': 8, 'evidence_max_results': 12,
    'evidence_lexical_candidates': 64, 'evidence_semantic_candidates': 64,
    'evidence_rrf_k': 60, 'evidence_semantic_enabled': True}


def _search_settings(engine, run_id):
    from .config import DEFAULTS
    get = getattr(engine.store, 'get', None)
    saved = get('settings', 'main', {}) if callable(get) else {}
    run = get('runs', run_id, {}) if callable(get) else {}
    config = {**SEARCH_DEFAULTS, **DEFAULTS.get('defaults', {}), **saved.get('defaults', {}), **run.get('config', {})}
    for field in SEARCH_DEFAULTS:
        if field == 'evidence_semantic_enabled':
            if type(config[field]) is not bool:
                raise ValueError('evidence_semantic_enabled must be boolean')
        elif type(config[field]) is not int or config[field] < (2 if field in {'evidence_top_k', 'evidence_max_results'} else 1):
            raise ValueError(f'{field} has an invalid positive integer value')
    return config


def source(engine, run_id, source_id, sha):
    document = engine.sources.document(run_id, source_id)
    if not isinstance(sha, str) or sha != document['text_sha256']:
        raise ValueError('Paper memory requires the exact current source text SHA256')
    return document


def structure(document, source_id):
    text = document['text']
    headings = [(m.start(), m.group().strip()) for m in HEADINGS.finditer(text)]
    if not headings or headings[0][0] != 0:
        headings.insert(0, (0, 'Unclassified opening'))
    spans = []
    for index, (start, heading) in enumerate(headings):
        end = headings[index+1][0] if index+1 < len(headings) else len(text)
        for part, offset in enumerate(range(start, end, 24000)):
            stop = min(end, offset+24000)
            spans.append({'section_id': f's{index}.{part}', 'heading': heading, 'start': offset, 'end': stop,
                'pages': [p.get('page_number', p.get('page')) for p in document.get('pages', [])
                          if p['start'] < stop and p['end'] > offset],
                'locator': f'{source_id}#chars={offset}:{stop}', 'heading_detection': 'heuristic'})
    return spans


def paper_map(engine, run_id, args):
    doc = source(engine, run_id, args['source_id'], args['expected_sha256'])
    sections = structure(doc, args['source_id'])
    return {'source_id': args['source_id'], 'text_sha256': doc['text_sha256'], 'sections': sections,
        'total_chars': len(doc['text']), 'original_artifact_id': doc.get('original_artifact_id'),
        'text_truncated': doc.get('text_truncated', False), 'pages_total': doc.get('pages_total'),
        'pages_extracted': doc.get('pages_extracted'), 'visual_content_inspected': False,
        'supplement_coverage': 'Only a supplement present in this source is mapped; external supplements need separate retrieval.',
        'provenance': 'local_text_structure', 'primary_source_reading': False,
        'policy': 'Headings are heuristic. Inspect exact text before relying on section labels; figure captions are not image inspection.'}


def saved_cards(engine, run_id, source_id, sha):
    document = source(engine, run_id, source_id, sha)
    cards = []
    for record in engine.store.list('context_evidence', run_id):
        try:
            validated = reduction.valid_saved(record, run_id)
        except (ValueError, TypeError, KeyError):
            continue
        scope = record.get('prepared', {}).get('context', {}).get('extraction_scope', {})
        for page in validated.values():
            if page['source_id'] != source_id or page['text_sha256'] != sha:
                continue
            for finding in page['findings']:
                start = page['original_offset'] + finding['offset']
                if document['text'][start:start+len(finding['quote'])] != finding['quote']:
                    continue
                cards.append({**copy.deepcopy(finding), 'id': finding['evidence_id'],
                    'source_id': source_id, 'text_sha256': sha, 'extraction_id': record['id'],
                    'hypothesis_id': fingerprint(scope), 'question_scope': scope.get('_purpose', scope.get('question', '')),
                    'scientific_claims_verified': False,
                    'expansion': copy.deepcopy(page['expansion'])})
    # Deduplicate within a question, retaining distinct graph edges across tasks.
    cards = list({(c['id'], c['hypothesis_id']): c for c in cards}.values())
    return cards


def dossier(engine, run_id, args):
    from .literature_notes import notes_view
    doc = source(engine, run_id, args['source_id'], args['expected_sha256'])
    notes = [n for n in notes_view(engine.store, run_id, document_loader=engine.sources.document)
             if (n['source_id'] == args['source_id'] and n['text_sha256'] == doc['text_sha256'])
             or n.get('parent_source_id') == args['source_id']]
    fields = ('paper_id', 'summary', 'research_question', 'method', 'datasets', 'baselines', 'results',
              'limitations', 'missing_information', 'excerpts', 'reading_scope', 'generation', 'scientific_claims_verified',
              'source_id', 'text_sha256', 'ocr_performed', 'source_transcription', 'parent_source_id', 'ocr_image', 'ocr_warning')
    cards = saved_cards(engine, run_id, args['source_id'], doc['text_sha256'])
    from .paper_media import observations
    visual = observations(engine, run_id, args['source_id'])
    sections = structure(doc, args['source_id'])
    for card in cards:
        match = re.search(r'#chars=(\d+):(\d+)$', card['locator'])
        start = int(match[1])
        # Also revalidate against the current original, not only a saved receipt.
        if doc['text'][start:int(match[2])] != card['quote']:
            raise ValueError('Saved claim no longer matches its immutable source')
        card['section'] = next((s['heading'] for s in sections if s['start'] <= start < s['end']), None)
        card['pages'] = [p.get('page_number', p.get('page')) for p in doc.get('pages', []) if p['start'] <= start < p['end']]
    payload = {'source_id': args['source_id'], 'text_sha256': doc['text_sha256'], 'title': doc.get('title'),
        'notes': [{k: copy.deepcopy(n[k]) for k in fields if k in n} for n in notes], 'claims': cards,
        'reported_observations': [c['id'] for c in cards if c['epistemic_status'] == 'reported_observation'],
        'author_interpretations': [c['id'] for c in cards if c['epistemic_status'] == 'author_interpretation'],
        'negative_results': [c['id'] for c in cards if c['kind'] in {'negative_result', 'failed_to_replicate', 'counterevidence'}],
        'anomalies': [c['id'] for c in cards if c['kind'] == 'anomaly'],
        'evidence_graph': [{'from': c['id'], 'relation': c['kind'], 'to': c['hypothesis_id'],
                            'source_locator': c['locator'], 'relation_status': 'model_interpretation'} for c in cards],
        'visual_observations': visual,
        'ocr_sources': [{'source_id': r.get('source_id'), 'text_sha256': r.get('text_sha256'),
                         'page': r['rendered']['page'], 'crop': r['rendered']['crop'], 'provenance': r['provenance']}
                        for r in engine.store.list('paper_ocr', run_id) if r.get('rendered', {}).get('source_id') == args['source_id']],
        'sections': sections, 'gaps': {'visual_content_inspected': bool(visual), 'all_visual_content_inspected': False,
            'text_truncated': doc.get('text_truncated', False), 'whole_paper_understanding_verified': False,
            'external_supplements': 'Not established by this document',
            'claim_coverage': 'Selected observations only; exact quotations verify provenance, not inference or completeness.'},
        'provenance': 'versioned_paper_dossier', 'primary_source_reading': False}
    identity = fingerprint(payload)
    key = f'{run_id}:paper-dossier:{identity}'
    engine.store.mutate('paper_dossiers', key, lambda old: old or {'id': key, 'run_id': run_id,
        'payload': payload, 'sha256': identity, 'created_at': now()})
    text = encoded(payload)
    offset, limit = args.get('offset', 0), args.get('limit', 10000)
    if type(offset) is not int or not 0 <= offset <= len(text) or type(limit) is not int or not 1 <= limit <= 16000:
        raise ValueError('Dossier paging arguments are invalid')
    if args.get('dossier_sha256') and args['dossier_sha256'] != identity:
        raise ValueError('Dossier changed; do not mix pages across versions')
    if offset and not args.get('dossier_sha256'):
        raise ValueError('Subsequent dossier pages require dossier_sha256')
    end = min(len(text), offset+limit)
    return {'dossier_id': key, 'sha256': identity, 'source_id': args['source_id'], 'text_sha256': doc['text_sha256'],
        'text': text[offset:end], 'offset': offset, 'next_offset': end if end < len(text) else None,
        'total_chars': len(text), 'truncated': end < len(text), 'untrusted': True,
        'primary_source_reading': False, 'scientific_claims_verified': False}


def search(engine, run_id, args):
    """BM25 + optional local learned embeddings + metadata/graph/adverse strata.

    No overlap threshold is imposed on adverse evidence of scoped papers.
    """
    query = args.get('query')
    config = _search_settings(engine, run_id)
    ids, limit = args.get('source_ids'), args.get('limit', config['evidence_top_k'])
    if not isinstance(query, str) or not query:
        raise ValueError('Evidence query must contain 1 to 1000 characters')
    if not isinstance(ids, list) or not 1 <= len(ids) <= 8 or len(set(ids)) != len(ids):
        raise ValueError('Evidence search requires 1 to 8 distinct task source IDs')
    if type(limit) is not int or not 2 <= limit <= config['evidence_max_results']:
        raise ValueError(f"Evidence search limit must be 2 to {config['evidence_max_results']}")
    rows, graphs = [], []
    for sid in ids:
        doc = engine.sources.document(run_id, sid)
        cards = saved_cards(engine, run_id, sid, doc['text_sha256'])
        graphs.extend(cards)
        for section in structure(doc, sid):
            if args.get('section') and str(args['section']).casefold() not in section['heading'].casefold():
                continue
            from .memory_chunking import ranges
            for local_start, local_end, strategy in ranges(doc['text'][section['start']:section['end']], size=1600, overlap=200):
                start, end = section['start']+local_start, section['start']+local_end
                body = doc['text'][start:end]
                rows.append({'source_id': sid, 'text_sha256': doc['text_sha256'], 'section': section['heading'],
                    'document_version': doc.get('document_version', doc.get('version')),
                    'chunking_strategy': strategy,
                    'start': start, 'end': end, 'quote': body,
                    'locator': f'{sid}#chars={start}:{end}', 'adverse_candidate': bool(NEGATIVE.search(body)),
                    'terms': Counter(re.findall(r'[a-z0-9_]+|[\u3400-\u9fff]', body.lower()))})
    terms = Counter(re.findall(r'[a-z0-9_]+|[\u3400-\u9fff]', query.lower()))
    n, avg = len(rows), sum(sum(r['terms'].values()) for r in rows)/max(1, len(rows))
    freq = {t: sum(t in row['terms'] for row in rows) for t in terms}
    for row in rows:
        length = sum(row['terms'].values())
        row['score'] = sum(math.log(1+(n-freq[t]+.5)/(freq[t]+.5)) * row['terms'][t]*2.2 /
            (row['terms'][t]+1.2*(.25+.75*length/max(1, avg))) for t in terms if row['terms'][t])
        row['score'] += len(_terms(query) & _terms(row['quote'])) / max(1, len(_terms(query)))
        del row['terms']
    ranked = sorted(rows, key=lambda r: (-r['score'], r['source_id'], r['start']))
    semantic = {'available': False, 'reason': 'No passages' if not rows else 'disabled'}
    if rows and config['evidence_semantic_enabled']:
        from .literature_runtime import semantic_scores
        try:
            scores, metadata = semantic_scores(engine, run_id, query, rows)
            if len(scores) != len(rows) or any(not math.isfinite(score) for score in scores):
                raise ValueError('Embedding scores must be finite and match the passage count')
        except (ImportError, ValueError, RuntimeError, OSError) as exc:
            semantic = {'available': False, 'reason': type(exc).__name__,
                        'action': 'Inspect local model setup/resources; lexical and adverse retrieval remain available.'}
        else:
            semantic = {'available': True, **metadata}
            for row, score in zip(rows, scores):
                row['semantic_score'] = score
            semantic_rank = sorted(rows, key=lambda row: (-row['semantic_score'], row['source_id'], row['start']))
            # Reciprocal rank fusion preserves both exact matches and paraphrases.
            semantic_order = {id(row): i+1 for i, row in enumerate(semantic_rank[:config['evidence_semantic_candidates']])}
            lexical_order = {id(row): i+1 for i, row in enumerate(ranked[:config['evidence_lexical_candidates']])}
            for row in rows:
                row['hybrid_score'] = sum(1/(config['evidence_rrf_k']+order[id(row)])
                    for order in (semantic_order, lexical_order) if id(row) in order)
            ranked.sort(key=lambda row: (-row['hybrid_score'], row['source_id'], row['start']))
    adverse = [r for r in ranked if r['adverse_candidate']][:max(1, limit//2)]
    candidates = ([r for r in ranked if r.get('hybrid_score', 0) > 0] if semantic['available'] else
                  ranked[:config['evidence_lexical_candidates']])
    selected = adverse + [r for r in candidates if r not in adverse][:limit-len(adverse)]
    graph = sorted(graphs, key=lambda c: (c['kind'] not in ADVERSE, -len(_terms(query) & _terms(c['claim']))))[:12]
    return {'passages': selected, 'claim_edges': graph, 'retrieval': ['BM25', 'lexical_similarity', 'source_and_section_filter', 'claim_relations', 'adverse_stratum'] + (['local_multilingual_embeddings', 'reciprocal_rank_fusion'] if semantic['available'] else []),
        'learned_semantic_embeddings': semantic['available'], 'semantic': semantic,
        'candidate_limits': {'lexical': config['evidence_lexical_candidates'], 'semantic': config['evidence_semantic_candidates']},
        'top_k': limit, 'rrf_k': config['evidence_rrf_k'],
        'untrusted': True, 'primary_source_reading': False,
        'coverage': 'Candidates, not exhaustive evidence. Adverse keywords are not a contradiction judgment. '
                    'Use evidence.expand to inspect the exact source range and its neighboring conditions.'}


async def expand(harness, run_id, role, args):
    spans = args.get('spans')
    if not isinstance(spans, list) or not 1 <= len(spans) <= 4:
        raise ValueError('Evidence expansion requires 1 to 4 bounded source spans')
    prepared = []
    for item in spans:
        if not isinstance(item, dict) or set(item) - {'source_id', 'expected_sha256', 'section_id', 'offset', 'limit'}:
            raise ValueError('Invalid evidence expansion span')
        doc = source(harness.engine, run_id, item['source_id'], item['expected_sha256'])
        offset, limit = item.get('offset', 0), item.get('limit', 6000)
        if item.get('section_id'):
            section = next((s for s in structure(doc, item['source_id']) if s['section_id'] == item['section_id']), None)
            if not section:
                raise ValueError('Unknown section ID')
            offset = section['start'] + offset
            limit = min(limit, section['end']-offset)
        if type(offset) is not int or not 0 <= offset < len(doc['text']) or type(limit) is not int or not 1 <= limit <= 16000:
            raise ValueError('Invalid evidence expansion range')
        prepared.append({'source_id': item['source_id'], 'expected_sha256': doc['text_sha256'], 'offset': offset, 'limit': limit})
    if sum(p['limit'] for p in prepared) > 24000:
        raise ValueError('Evidence expansion exceeds its 24000 character batch budget')
    # Reuse audited source reads so accepted note citations remain verifiable.
    receipts = [await harness.execute_controller_tool(run_id, role, 'sources.read', p,
        purpose='evidence-span-expansion') for p in prepared]
    return {'passages': receipts, 'provenance': 'exact_evidence_expansion',
        'next_action': 'Reassess the uncertain claim using the expanded evidence, including contrary conditions. Do not infer whole-paper coverage.'}


async def deep_analyze(harness, run_id, role, args):
    """One versioned single-paper job, independent of the global transcript.

    Does not promise free persistent provider context. Repeating this exact job
    reuses its journal/result; a changed question is a new budgeted call.
    """
    import asyncio
    doc = source(harness.engine, run_id, args['source_id'], args['expected_sha256'])
    question = args.get('question')
    if not isinstance(question, str) or not question:
        raise ValueError('Deep paper question must contain 1 to 4000 characters')
    if not doc['text']:
        raise ValueError('No extracted text for deep reading; OCR/visual inspection is required')
    scope = {'_purpose': 'deep_paper', 'question': question, 'source_id': args['source_id'], 'text_sha256': doc['text_sha256']}
    page = {'page_id': fingerprint(scope), 'source_id': args['source_id'], 'sha256': doc['text_sha256'],
        'source_offset': 0, 'text': doc['text'], 'title': doc.get('title'), 'untrusted': True}
    identity = fingerprint({'version': 1, 'scope': scope, 'pages': [page], 'schema': reduction.SCHEMA, 'task': reduction.TASK})
    key = f'{run_id}:context-evidence:{identity}'
    prepared = {'role': role, 'task': reduction.TASK, 'schema': reduction.SCHEMA,
        'context': {'_purpose': f'deep_paper:{identity}', '_output_token_limit': 8000,
            'extraction_scope': scope, 'source_pages': [page], 'paper_structure': structure(doc, args['source_id']),
            'reading_requirement': 'Inspect cross-section dependencies, definitions, methods, baselines, conditions, negative results, '
                'proof assumptions and available supplements together. Cite the decisive passages, including conflicts. '
                'Text-only: figures/images and unavailable supplements remain unresolved. No automatic novelty approval.',
            '_harness_control': {'mode': 'final_only', 'operation_id': identity}}}
    plan = reduction.reuse_plan(harness.engine.store, run_id,
        {'id': key, 'run_id': run_id, 'pages': [page], 'prepared': prepared})
    key, prepared = plan['id'], plan['prepared']
    lock = harness.intelligence._locks.setdefault('evidence:' + key, asyncio.Lock())
    async with lock:
        saved = harness.engine.store.get('context_evidence', key)
        if saved and saved.get('status') == 'completed':
            reduction.valid_saved(saved, run_id)
        else:
            if saved and saved.get('status') == 'invalid':
                raise ValueError('Deep paper extraction failed quote validation; no automatic resend')
            harness.intelligence.preflight_batch(run_id, [prepared])
            harness.engine.check_active(run_id)
            harness.engine.store.put('context_evidence', key, {**plan, 'status': 'running', 'created_at': now()})
            reply = await harness.intelligence.ask(run_id, **prepared, cache=True, max_attempts=1)
            try:
                cards = reduction.validate(plan, reply['json'])
            except (ValueError, KeyError, TypeError) as exc:
                harness.engine.store.put('context_evidence', key, {**plan, 'status': 'invalid', 'response': reply.get('json'), 'error': str(exc)})
                raise
            harness.engine.store.put('context_evidence', key, {**plan, 'status': 'completed', 'response': reply['json'],
                'cards': cards, 'request_id': reply.get('request_id'), 'completed_at': now()})
    # Dossier is a navigable observation record, never an accepted note or a
    # fabricated sources.read receipt. The normal note gate remains unchanged.
    return {'analysis_id': key, 'dossier': dossier(harness.engine, run_id,
        {'source_id': args['source_id'], 'expected_sha256': doc['text_sha256']}),
        'policy': 'Exact job results are reused. Provider sessions/cache do not guarantee zero input billing. '
                  'For a new question inspect the dossier and expand decisive passages before requesting another full reading.'}
