"""Same-role corrections for repairable implementation acceptance deliveries.

The rejected decision and frozen request remain immutable. A new bounded handoff
must independently read its evidence; its result passes the ordinary validator.
"""
from __future__ import annotations

import copy

from . import implementation_acceptance as policy
from . import implementation_acceptance_flow as flow
from .implementation_document_delivery import prepare
from .implementation_review_routing import digest

KEY = 'implementation_delivery_correction'
REPAIR = 'implementation_acceptance_delivery_repair'


def repair_context(engine, run_id, previous, marker):
    expected = {**copy.deepcopy(previous['context']), 'document_read_window_protocol': 3, REPAIR: copy.deepcopy(marker)}
    version = marker.get('version')
    if type(version) is not int or version not in (1, 2):
        policy.fail('Unsupported delivery repair input version')
    if version == 2:
        from .implementation_correction_reads import bind
        expected = bind(engine, run_id, previous, expected)
    return expected


def validate_repair(engine, run_id, packet):
    """Reauthenticate the complete correction lineage at replay and release."""
    marker = packet['context'][REPAIR]
    hid = marker['source_handoff_id']
    source = engine.store.get('handoffs', hid, {})
    frozen = engine.store.get('handoff_inputs', hid, {})
    previous = {'handoff_id': hid, 'role': marker['role'], 'result': marker['original_result'],
                'context': frozen.get('context', {})}
    if 'citation_correction' in marker:
        previous['citation_correction'] = marker['citation_correction']
    if 'report_amendment' in marker:
        previous['report_amendment'] = marker['report_amendment']
    flow.check_packet(engine, run_id, previous)
    expected = repair_context(engine, run_id, previous, marker)
    from .implementation_document_review_scope import replay_read_contract
    purpose = source.get('purpose', '') + ':delivery-repair:' + digest([hid, marker['diagnostic']])[:16]
    expected = replay_read_contract(expected, packet['context'], engine, run_id, packet=packet, purpose=purpose)
    if (marker['role'] != packet['role']
            or marker['source_input_hash'] != source.get('input_hash')
            or marker['source_result_sha256'] != digest(previous['result'])
            or packet['context'] != expected):
        policy.fail('交付补正的原角色、输入或原始答复发生变化。')


