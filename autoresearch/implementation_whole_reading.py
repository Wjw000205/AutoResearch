"""Authenticated same-reviewer read continuity, without preloading source text."""
from __future__ import annotations

import copy

from . import implementation_acceptance as policy
from .implementation_review_routing import digest


def required(document):
    return [{'result': {'section_id': s['id'], 'char_start': s['start'], 'char_end': s['end']}}
            for s in document['sections'] if s['end'] > s['start']]


def union_ranges(ranges):
    """Pagination, overlapping reopens and order do not constitute new evidence."""
    grouped = {}
    for span in ranges:
        grouped.setdefault(span['section_id'], []).append((span['start'], span['end']))
    result = []
    for section, spans in sorted(grouped.items()):
        merged = []
        for start, end in sorted(spans):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(end, merged[-1][1])
            else:
                merged.append([start, end])
        result.extend({'section_id': section, 'start': a, 'end': b} for a, b in merged)
    return result


def handoffs(engine, run_id, packet, *, seen=frozenset()):
    from . import implementation_acceptance_flow as flow
    from .implementation_whole_review import KEY, POLICY
    from .evidence_delivery import tracking_required
    hid = packet['handoff_id']
    if hid in seen or len(seen) >= 16:
        policy.fail('Cyclic whole-document read continuation')
    context = flow.check_packet(engine, run_id, packet)
    if context.get(KEY, {}).get('reviewer') != packet['role']:
        policy.fail('Whole-document read ownership differs from the actual author')
    if not tracking_required(engine.store, run_id, hid):
        policy.delivery_error('Whole-document review requires an actual model-delivery inventory', action='submit_missing_read_delivery')
    result = [hid]
    continuation = context[KEY].get('continuation')
    if not continuation:
        return result
    parent_id = continuation['handoff_id']
    frozen = engine.store.get('handoff_inputs', parent_id, {})
    raw = engine.store.get('decisions', parent_id, {})
    parent = {'handoff_id': parent_id, 'role': packet['role'], 'context': frozen.get('context', {}), 'result': raw.get('result')}
    before = parent['context'].get(KEY, {})
    after = context[KEY]
    if (raw.get('role') != packet['role'] or raw.get('run_id') != run_id
            or continuation['result_sha256'] != digest(parent['result'])
            or continuation.get('previous_own_report') != parent['result']
            or continuation.get('progress_sha256') != digest(engine.store.get('handoff_progress', parent_id))
            or before.get('policy') != POLICY or before.get('reviewer') != packet['role']
            or any(before.get(k) != after.get(k) for k in ('workflow_id', 'round', 'document', 'spec_hash', 'architecture'))
            or parent['context'].get(policy.CONTEXT, {}).get('contract') != context[policy.CONTEXT]['contract']):
        policy.fail('Whole-document read continuation changed owner, source version or original receipt')
    return [*result, *handoffs(engine, run_id, parent, seen={*seen, hid})]


def inherited_index(engine, run_id, prior):
    from .implementation_reading import coverage
    from .implementation_whole_review import KEY
    ids = handoffs(engine, run_id, prior)
    docs = [prior['context'][policy.CONTEXT]['document'], prior['context'][KEY]['architecture']]
    return [{'document_id': d['document_id'], 'sha256': d['sha256'],
             'covered_ranges': union_ranges(coverage(engine, run_id, d, required(d), ids)['covered_ranges'])}
            for d in docs]


def complete(engine, run_id, packet, document):
    from .implementation_reading import coverage
    return coverage(engine, run_id, document, required(document), handoffs(engine, run_id, packet))['complete']


def validate_refs(engine, run_id, packet, refs, *, current, field):
    from .implementation_alignment_evidence import _required_ranges
    from .implementation_reading import coverage
    context = packet['context']
    documents = {d['document_id']: d for d in context['indexed_documents']}
    if not any((r['document_id'], r['document_sha256']) == (current['document_id'], current['sha256']) for r in refs):
        policy.delivery_error('Whole-document judgment must locate current implementation text', field=field)
    ranges = _required_ranges(engine, run_id, refs, {k: d['sha256'] for k, d in documents.items()})
    ids = handoffs(engine, run_id, packet)
    for did, spans in ranges.items():
        if not coverage(engine, run_id, documents[did], spans, ids)['complete']:
            policy.delivery_error('Whole-document reference lacks this reviewer\'s submitted source delivery', field=field)


def attach(engine, run_id, context, prior):
    from .implementation_whole_review import KEY
    context = copy.deepcopy(context)
    if (context[KEY]['reviewer'] != prior['role']
            or any(context[KEY][k] != prior['context'][KEY][k] for k in
                   ('workflow_id', 'round', 'document', 'spec_hash', 'architecture'))):
        policy.fail('Cannot deliver another role or version as the reviewer\'s own previous reading')
    context[KEY]['continuation']['progress_sha256'] = digest(engine.store.get('handoff_progress', prior['handoff_id']))
    context[KEY]['inherited_read_index'] = inherited_index(engine, run_id, prior)
    return context
