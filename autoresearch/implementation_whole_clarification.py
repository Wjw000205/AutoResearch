"""Bounded author clarification followed by independent whole-document review."""
from __future__ import annotations

import copy

from . import implementation_acceptance as policy
from . import implementation_acceptance_flow as flow
from .implementation_review_routing import digest


def validate(engine, run_id, state, row, packet, role):
    from .implementation_whole_review import KEY, POLICY
    from .implementation_whole_reading import validate_refs
    context = flow.check_packet(engine, run_id, packet)
    marker = context.get(KEY, {})
    question = marker.get('question', {})
    original_id = question.get('handoff_id')
    original = engine.store.get('decisions', original_id, {})
    original_context = engine.store.get('handoff_inputs', original_id, {}).get('context', {})
    if (packet['role'] != 'engineer' or marker.get('mode') != 'author_source_clarification'
            or marker.get('policy') != POLICY or marker.get('document') != row['document']
            or marker.get('workflow_id') != state['id'] or question.get('reviewer') != role
            or original.get('role') != role or original.get('run_id') != run_id
            or question.get('report') != original.get('result')
            or question.get('result_sha256') != digest(original.get('result'))
            or original_context.get(KEY, {}).get('document') != row['document']):
        policy.fail('Author clarification differs from its exact independent source question')
    validate_refs(engine, run_id, packet, packet['result']['evidence'], current=row['document'], field='author_clarification')
    if packet['result']['resolution'] == 'external_dependency_missing' and not packet['result']['missing_resource'].strip():
        policy.delivery_error('External dependency clarification must identify the actual missing resource')
