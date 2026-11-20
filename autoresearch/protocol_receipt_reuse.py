"""Reuse an exact successful Scout protocol extraction, not its scientific approval."""
from __future__ import annotations

import copy


def result(engine, run_id, reference):
    from .engine import digest
    if not isinstance(reference, dict):
        raise ValueError('Protocol reuse reference must identify one saved Scout delivery')
    identifier = reference.get('handoff_id', '')
    saved = engine.store.get('decisions', identifier, {})
    handoff = engine.store.get('handoffs', identifier, {})
    answer = saved.get('result')
    if (saved.get('run_id') != run_id or saved.get('role') != 'scout'
            or not saved.get('purpose', '').startswith('literature_reading:design_protocol:')
            or handoff.get('run_id') != run_id or handoff.get('status') != 'completed'
            or handoff.get('to_role') != 'scout' or handoff.get('purpose') != saved.get('purpose')
            or digest(answer) != reference.get('output_sha256')
            or handoff.get('output_hash') != reference.get('output_sha256')):
        raise ValueError('Reused protocol extraction requires its exact completed Scout delivery')
    if (not isinstance(answer, dict) or not isinstance(answer.get('protocols'), list)
            or not isinstance(answer.get('unresolved'), list)):
        raise ValueError('Reused protocol extraction has no valid structured result')
    # prepare_protocols still revalidates every protocol, note/source hash and target identity.
    return copy.deepcopy(answer)
