"""Deliver mandatory review pages before asking the model to navigate them.

Only new handoffs opt in. Actual version-bound reads stay in that role's own
ledger and still need request delivery and ordinary independent validation.
"""
from __future__ import annotations

KEY = 'implementation_review_read_batch'
MAX_CALLS = 24


def assigned_sections(context, document):
    """Only implementation obligations are host-located in scoped v1.

    Other directories are navigation, not source-span assignments. Until an
    exact relevant source span is frozen, retrieve it on demand; never infer
    relevance from a previous role's broader source delivery.
    """
    from .implementation_acceptance import CONTEXT
    from .implementation_document_review_scope import reading_dependencies
    scope = context[CONTEXT]
    if (document['document_id'], document['sha256']) == (
            scope['document']['document_id'], scope['document']['sha256']):
        return ([s for values in scope['required_sections'].values() for s in values] +
                reading_dependencies(context))
    return [s for source in context.get('implementation_document_review_scope', {}).get('source_spans', [])
            if (source['document_id'], source['sha256']) == (document['document_id'], document['sha256'])
            for s in source['sections']]


def branch(context, receipts):
    from .implementation_acceptance_reading import plan
    from .engine import obj
    if context.get(KEY) != 1 or context.get('document_read_window_protocol') != 3:
        return None
    progress = plan(context, receipts)
    calls = progress['next_calls'] if progress else []
    if not calls:
        return None
    return obj({'tool_calls': {'type': 'array', 'minItems': 1, 'maxItems': MAX_CALLS,
        'uniqueItems': True, 'items': {'enum': calls}}})


async def seed(engine, h, run_id, hid, context):
    from .implementation_whole_review import request_enabled
    if request_enabled(context):
        return  # The reviewer must request indexed pages; no host-preloaded body.
    from . import implementation_acceptance as policy
    if (policy.CONTEXT not in context or not (context.get(KEY) == 1 or
            context.get('implementation_document_review_scope'))
            or context.get('document_read_window_protocol') not in (1, 2, 3)):
        return
    if engine.store.get('handoff_progress', hid) or engine.store.get('decisions', hid):
        return
    from .implementation_acceptance_reading import plan
    from .architecture_validation import operation
    from .document_index import read
    config = engine.store.get('runs', run_id)['config']
    # Reserve most input capacity for the frozen contract, sources and verdict.
    char_budget = min(96000, max(0, config.get('context_tokens', 32000) // 4))
    reused = []
    chars = 0
    if context.get('implementation_acceptance_delivery_repair', {}).get('version') == 2:
        from .implementation_correction_reads import pages as correction_pages
        reused = await engine._read_consensus_check(run_id, operation(correction_pages),
                                                   engine, run_id, hid, context, char_budget)
        chars = sum(len(receipt['result'].get('text', '')) for receipt in reused)
    scoped = context.get('implementation_document_review_scope')
    if scoped and scoped.get('assignment') and not reused:
        from .implementation_acceptance_reading import required
        from .implementation_reading import coverage
        role = scoped['assignment']['role']
        reused_spans = {}
        for old in scoped.get('legacy_handoffs', []):
            if old['role'] != role:
                continue
            frozen = authenticate_origin(engine, run_id, role, old)
            old_docs = {(d['document_id'], d['sha256']) for d in frozen['context'].get('indexed_documents', [])}
            for doc in context['indexed_documents']:
                if (doc['document_id'], doc['sha256']) not in old_docs:
                    continue
                assigned = assigned_sections(context, doc)
                if not assigned:
                    continue
                covered = coverage(engine, run_id, doc, required(assigned), [old['handoff_id']])
                sections = {s['id']: s for s in doc['sections']}
                for span in covered['covered_ranges']:
                    # Criteria and old handoffs may overlap. Subtract the
                    # already admitted bytes, retaining each original origin.
                    from .document_read_window import _uncovered
                    key = (doc['document_id'], doc['sha256'], span['section_id'])
                    seen = reused_spans.setdefault(key, [])
                    missing = _uncovered(span['start'], span['end'], seen)
                    for a, b in missing:
                        for start in range(a, b, 6000):
                            count = min(6000, b-start, char_budget-chars)
                            if count <= 0:
                                break
                            args = {'document_id': doc['document_id'], 'expected_sha256': doc['sha256'],
                                'section_id': span['section_id'], 'offset': start-sections[span['section_id']]['start'],
                                'limit': count}
                            page = read(engine, run_id, args)
                            receipt = {'call': {'name': 'documents.read', 'arguments': args}, 'result': page,
                                       'scoped_read_provenance': old}
                            reused.append(receipt)
                            chars += count
                            seen.append((start, start + count, old['handoff_id']))
    calls = []
    for call in plan(context, reused)['next_calls']:
        count = min(call['arguments']['limit'], char_budget - chars)
        if count <= 0:
            break
        calls.append({**call, 'arguments': {**call['arguments'], 'limit': count}})
        chars += count
    if not calls and not reused:
        return
    @operation
    def pages():
        return [{'call': call, 'result': read(engine, run_id, call['arguments'])} for call in calls]
    receipts = [*reused, *await engine._read_consensus_check(run_id, pages)]
    engine.check_active(run_id)
    h._save_progress(run_id, hid, {'turn': 0, 'seen': {}, 'tool_results': receipts})
    engine.store.event(run_id, 'implementation.review_reads_preloaded',
        '已为本角色预取必读原文分页，减少模型定位往返；仍须独立审查。',
        {'handoff_id': hid, 'pages': len(receipts), 'text_chars': sum(len(r['result'].get('text', '')) for r in receipts),
         'retrieval_only': True, 'scientific_approval': False})


def authenticate_origin(engine, run_id, role, origin):
    from .engine import Harness
    from .implementation_review_routing import digest
    from .implementation_acceptance import fail
    from .evidence_delivery import tracking_required
    hid = origin['handoff_id']
    frozen = engine.store.get('handoff_inputs', hid, {})
    handoff = engine.store.get('handoffs', hid, {})
    payload = {k: frozen.get(k) for k in ('instruction', 'context', 'schema')}
    if (origin['role'] != role or handoff.get('to_role') != role or handoff.get('run_id') != run_id
            or digest(frozen) != origin['input_sha256']
            or digest(engine.store.get('handoff_progress', hid)) != origin['progress_sha256']
            or Harness.decision_key(run_id, role, handoff.get('purpose'), **payload) != hid
            or handoff.get('input_hash') != digest(payload)):
        fail('Scoped read origin identity changed')
    # A legacy retrieval without a submitted inventory is evidence only.
    if not tracking_required(engine.store, run_id, hid):
        return {**frozen, 'context': {}}
    return frozen


def validate_reused(engine, run_id, hid, document, receipt):
    from .implementation_acceptance import fail
    from .implementation_reading import coverage
    origin = receipt['scoped_read_provenance']
    role = engine.store.get('handoffs', hid, {}).get('to_role')
    context = engine.store.get('handoff_inputs', hid, {}).get('context', {})
    scoped = context.get('implementation_document_review_scope', {})
    if (origin not in scoped.get('legacy_handoffs', [])
            or scoped.get('assignment', {}).get('role') != role):
        fail('Scoped read origin is not frozen in this assignment')
    from .implementation_acceptance import CONTEXT
    from .document_index import read
    args = receipt.get('call', {}).get('arguments', {})
    if (receipt.get('call', {}).get('name') != 'documents.read'
            or args.get('document_id') != document['document_id']
            or args.get('expected_sha256') != document['sha256']
            or read(engine, run_id, args) != receipt.get('result')):
        fail('Scoped reused read bytes changed')
    if scoped:
        from .document_read_window import _uncovered
        page = receipt['result']
        assigned = [(s['start'], s['end'], '') for s in assigned_sections(context, document)
                    if s['id'] == page['section_id']]
        if _uncovered(page['char_start'], page['char_end'], sorted(assigned)):
            fail('Scoped reused read exceeds the assigned intersection')
    if origin['handoff_id'] == hid:
        fail('Scoped read provenance cannot refer to itself')
    frozen = authenticate_origin(engine, run_id, role, origin)
    if not any(d['document_id'] == document['document_id'] and d['sha256'] == document['sha256']
               for d in frozen['context'].get('indexed_documents', [])):
        fail('Scoped read origin lacks exact source delivery')
    if not coverage(engine, run_id, document, [receipt], [origin['handoff_id']])['complete']:
        fail('Scoped read origin no longer covers reused bytes')
