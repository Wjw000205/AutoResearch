"""Same-role source rechecks, with immutable predecessors and real read tools."""
from __future__ import annotations

import copy

from . import implementation_acceptance as policy
from . import implementation_acceptance_flow as flow
from . import implementation_document_review_scope as scoped
from . import supplemental_reads as reads
from .implementation_review_routing import digest

KEY = 'implementation_source_read_recovery'


def needed(packet):
    return (scoped.KEY in packet['context'] and KEY not in packet['context']
            and not packet.get('carried_proof') and not packet.get('legacy_proof')
            and any(c['status'] == 'UNKNOWN' for c in [*packet['result']['checks'],
                *(c for v in packet['result'].get('verifications', []) for c in v['conditions'])]))


def context_for(engine, run_id, packet):
    flow.check_packet(engine, run_id, packet)
    context = reads.opt_in(packet['context'])
    # These describe paid predecessor corrections, not this new recheck.
    for field in ('implementation_acceptance_delivery_repair', 'implementation_delivery_correction',
                  'implementation_read_window_recovery', 'implementation_acceptance_read_recovery',
                  'implementation_acceptance_citation_correction'):
        context.pop(field, None)
    scope = context[scoped.KEY]
    origin = {'role': packet['role'], 'handoff_id': packet['handoff_id'],
              'input_sha256': digest(engine.store.get('handoff_inputs', packet['handoff_id'])),
              'progress_sha256': digest(engine.store.get('handoff_progress', packet['handoff_id']))}
    scope['legacy_handoffs'] = [*scope.get('legacy_handoffs', []), origin]
    context[KEY] = {'version': 1, 'parent_handoff_id': packet['handoff_id'],
                    'parent_result_sha256': digest(packet['result']),
                    'parent_report': copy.deepcopy(packet['result']), 'scientific_approval': False}
    if packet.get('citation_correction'):
        context[KEY]['parent_citation_correction'] = copy.deepcopy(packet['citation_correction'])
    if packet.get('report_amendment'):
        context[KEY]['parent_report_amendment'] = copy.deepcopy(packet['report_amendment'])
    return scoped.new_read_contract(context)


def _wrapped_source(engine, run_id, row, packet, marker, seen):
    from . import implementation_delivery_correction as delivery
    from . import implementation_acceptance_read_recovery as reading
    from . import implementation_read_window_recovery as window
    context = packet['context']
    current_purpose = engine.store.get('handoffs', packet['handoff_id'], {}).get('purpose')
    for field, suffix in ((delivery.REPAIR, 'delivery-repair'), (reading.KEY, 'read-coverage-recovery'),
                          (window.KEY, 'read-window-recovery'), (delivery.KEY, 'delivery-correction')):
        wrapper = context.get(field)
        if not isinstance(wrapper, dict):
            continue
        hid = wrapper.get('source_handoff_id')
        before = engine.store.get('handoffs', hid, {})
        ending = (digest([hid, wrapper.get('diagnostic')])[:16] if field == delivery.REPAIR else digest(hid)[:16])
        if current_purpose != before.get('purpose', '') + ':' + suffix + ':' + ending:
            continue
        parent = {'handoff_id': hid, 'role': packet['role'],
                  'context': engine.store.get('handoff_inputs', hid, {}).get('context', {}),
                  'result': wrapper.get('original_result' if field == delivery.REPAIR else 'original_report')}
        if wrapper.get('citation_correction'):
            parent['citation_correction'] = wrapper['citation_correction']
        if wrapper.get('report_amendment'):
            parent['report_amendment'] = wrapper['report_amendment']
        from .implementation_report_amendments import restore_provenance
        restore_provenance(parent, wrapper)
        if parent['context'].get(KEY) != marker:
            policy.fail('Source recovery wrapper changed its original source marker')
        flow.check_packet(engine, run_id, parent)
        validate_source(engine, run_id, row, parent, _seen=seen)
        if field == delivery.REPAIR:
            delivery.validate_repair(engine, run_id, packet)
        elif field == reading.KEY:
            reading.validate_source(engine, run_id, row, packet)
        else:
            expected = copy.deepcopy(parent['context'])
            if field == window.KEY:
                window.validate_source(engine, run_id, row, packet)
                expected = window.opt_in(expected)
            else:
                from .engine import ResearchBlocked
                try:
                    flow.validate_report(engine, run_id, row, parent)
                except ResearchBlocked as exc:
                    if getattr(exc, 'document_delivery_error', None) != wrapper.get('diagnostic'):
                        raise
                else:
                    policy.fail('Source delivery correction lacks its original rejection')
                from .implementation_document_delivery import prepare
                expected[policy.CONTEXT].update(document_revision=row['index'], spec_hash=row['spec_hash'])
                expected = prepare(engine, run_id, expected)
            expected[field] = copy.deepcopy(wrapper)
            if (expected != context or wrapper.get('source_result_sha256') != digest(parent['result'])
                    or type(wrapper.get('version')) is not int or wrapper['version'] != 1):
                policy.fail('Source recovery wrapper changed its exact frozen context')
        return True
    return False


def validate_source(engine, run_id, row, packet, *, _seen=frozenset()):
    marker = packet['context'].get(KEY)
    if marker is None:
        return
    if (not isinstance(marker, dict) or type(marker.get('version')) is not int or marker['version'] != 1
            or marker.get('scientific_approval') is not False):
        policy.fail('Invalid source-read recovery version')
    if packet['handoff_id'] in _seen or len(_seen) >= 16:
        policy.fail('Cyclic source recovery wrapper lineage')
    if _wrapped_source(engine, run_id, row, packet, marker, {*_seen, packet['handoff_id']}):
        if any(i not in packet['result']['issues'] for i in marker['parent_report']['issues']):
            policy.delivery_error('Source recheck must preserve unresolved original findings')
        return
    hid = marker['parent_handoff_id']
    parent = {'handoff_id': hid, 'role': packet['role'], 'context':
              engine.store.get('handoff_inputs', hid, {}).get('context', {}), 'result': marker['parent_report']}
    if marker.get('parent_citation_correction'):
        parent['citation_correction'] = marker['parent_citation_correction']
    if marker.get('parent_report_amendment'):
        parent['report_amendment'] = marker['parent_report_amendment']
    if not needed(parent) or marker['parent_result_sha256'] != digest(parent['result']):
        policy.fail('Source recheck lost its original UNKNOWN report')
    flow.validate_report(engine, run_id, row, parent)
    expected = context_for(engine, run_id, parent)
    if packet['context'] != expected:
        policy.fail('Source recheck changed its frozen task, sources, budget or role')
    hand = engine.store.get('handoffs', packet['handoff_id'], {})
    if hand.get('purpose') != purpose(engine, parent):
        policy.fail('Source recheck changed its deterministic parent identity')
    if any(i not in packet['result']['issues'] for i in parent['result']['issues']):
        policy.delivery_error('Source recheck must preserve unresolved original findings')


def purpose(engine, packet):
    return (engine.store.get('handoffs', packet['handoff_id'])['purpose']
            + ':source-read-recovery-v1:' + digest(packet['handoff_id'])[:16])


def effective(saved):
    edges = {}
    for packet in saved.get('source_followups', []):
        parent = packet['context'][KEY]['parent_handoff_id']
        if parent in edges:
            policy.fail('Ambiguous source/endpoint followup lineage')
        edges[parent] = packet
    for packet in saved.get('endpoint_followups', []):
        parents = packet['context'][scoped.KEY].get('endpoint_gap', [])
        if len(parents) != 1 or parents[0]['handoff_id'] in edges:
            policy.fail('Ambiguous source/endpoint followup lineage')
        edges[parents[0]['handoff_id']] = packet
    result, used = [], set()
    for original in saved['packets']:
        packet, seen = original, {original['handoff_id']}
        while packet['handoff_id'] in edges:
            used.add(packet['handoff_id'])
            packet = edges[packet['handoff_id']]
            if packet['handoff_id'] in seen:
                policy.fail('Cyclic source/endpoint followup lineage')
            seen.add(packet['handoff_id'])
        result.append(packet)
    if used != set(edges):
        policy.fail('Source followup has no unique original assignment')
    return result


def validate_followups(engine, run_id, state, row, saved):
    originals = {p['handoff_id']: p for p in [*saved['packets'], *saved.get('endpoint_followups', [])]}
    for packet in saved.get('source_followups', []):
        marker = packet['context'][KEY]
        parent = originals.pop(marker['parent_handoff_id'], None)
        if (parent is None or not needed(parent) or packet['role'] != parent['role']
                or marker['parent_report'] != parent['result']
                or packet['context'][scoped.KEY]['assignment'] != parent['context'][scoped.KEY]['assignment']):
            policy.fail('Source followup changed the original assignment or duplicated its recheck')
        scoped.validate_task_packet(engine, run_id, state, row, packet,
                                    parent['context'][scoped.KEY]['assignment'])


