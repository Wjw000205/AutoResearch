"""Authenticate a completed Harness delivery without guessing response fields.

Each phase validates its own versioned semantic bindings after this ownership
check. A design/amendment reply need not echo fields absent from its Schema.
"""
from __future__ import annotations


def authenticate(engine, run_id, role, handoff_id, result):
    from .engine import Harness, ResearchBlocked, digest
    handoff = engine.store.get('handoffs', handoff_id) or {}
    frozen = engine.store.get('handoff_inputs', handoff_id) or {}
    decision = engine.store.get('decisions', handoff_id) or {}
    payload = {k: frozen.get(k) for k in ('instruction', 'context', 'schema')}
    if (handoff.get('id') != handoff_id or handoff.get('run_id') != run_id
            or handoff.get('to_role') != role or handoff.get('status') != 'completed'
            or frozen.get('id') != handoff_id or frozen.get('run_id') != run_id
            or handoff.get('input_hash') != digest(payload)
            or Harness.decision_key(run_id, role, handoff.get('purpose'), **payload) != handoff_id
            or decision.get('id') != handoff_id or decision.get('run_id') != run_id
            or decision.get('role') != role or decision.get('purpose') != handoff.get('purpose')
            or decision.get('result') != result or handoff.get('output_hash') != digest(result)):
        raise ResearchBlocked('Harness delivery does not match its completed actor/request/result receipts')
    with engine.store._read_db() as db:
        row = db.execute("SELECT json_extract(data,'$.id'),json_extract(data,'$.run_id') FROM records "
                         "WHERE collection='handoff_progress' AND key=?", (handoff_id,)).fetchone()
    if row and tuple(row) != (handoff_id, run_id):
        raise ResearchBlocked('Harness delivery progress belongs to a different assignment')
    return frozen['context']
