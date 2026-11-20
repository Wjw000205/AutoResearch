"""Resolve the actual ordinary save author from controller-owned audit edges."""
from __future__ import annotations


def resolve(engine, run_id, handoff_id):
    return trace(engine, run_id, handoff_id)[0]


def trace(engine, run_id, handoff_id, *, recovery_edge=None):
    """Traverse both audited edge types before selecting an ordinary author.

    The second return value is the live entry to the final ordinary save
    chain. Its ordinary delegate still owns native reconciliation and replay.
    """
    from .document_author_cleanup import clear
    from .document_efficiency_recovery import _packet
    from .document_recovery_receipts import inherited_rows
    from .document_read_convergence import digest
    from .engine import ResearchBlocked
    seen = set()
    delegate = None
    ordinary_parent = None
    while True:
        if handoff_id in seen or len(seen) >= 16:
            raise ResearchBlocked('Author lineage is cyclic or exceeds its supported depth')
        seen.add(handoff_id)
        # Recovery owns the only authenticated way to inspect a cleared
        # node. It supplies its archived packet for the ordinary-edge check.
        edge = recovery_edge(handoff_id) if recovery_edge else None
        packet = edge['packet'] if edge else _packet(engine, run_id, handoff_id)
        if ordinary_parent is not None:
            owner, _ = inherited_rows(engine, run_id, 'engineer', handoff_id, [], inputs=packet['input'])
            if (owner != ordinary_parent['id'] or packet['handoff'].get('to_role') != 'engineer'
                    or packet['handoff'].get('purpose') != ordinary_parent['purpose'] + ':save:1'
                    or packet['input']['context'].get('implementation_document_completion_protocol') != 1):
                raise ResearchBlocked('Ordinary save successor has a foreign owner, role, purpose or protocol')
        if delegate is None:
            delegate = packet
        audit = engine.store.get('stage_contexts', handoff_id + ':save-predecessor:v1')
        if edge:
            if audit:
                raise ResearchBlocked('Author lineage has conflicting ordinary and recovery successors')
            if not edge.get('preserve_source'):
                clear(engine, run_id, handoff_id, edge['audit'], edge['successor_id'])
            handoff_id = edge['successor_id']
            delegate = ordinary_parent = None
            continue
        if not audit:
            return packet, delegate
        payload = audit.get('payload', {})
        if (audit.get('run_id') != run_id or digest(payload) != audit.get('sha256')
                or payload.get('input') != packet['input'] or payload.get('progress') != packet['progress']):
            raise ResearchBlocked('Ordinary save predecessor changed before author recovery')
        ordinary_parent = packet['handoff']
        handoff_id = audit['successor_id']


def native_identity(run_id, request):
    from .codex_reasoning import key_for
    from .engine import digest
    from .intelligence import Intelligence

    prepared = request.get('prepared', {})
    if (request.get('provider_id') != 'local_codex' or not isinstance(prepared, dict)
            or set(prepared) != {'role', 'task', 'context', 'schema'}
            or prepared['role'] != 'engineer' or digest(prepared) != request.get('prepared_sha256')):
        return None
    return key_for(Intelligence.__new__(Intelligence), run_id, **prepared)


def unsettled(engine, run_id, packet, *, preserve_source=False):
    import json
    from .tool_invocations import READ_ONLY
    hid, progress = packet['handoff']['id'], packet['progress']
    request = progress.get('normal_request', {})
    final = progress.get('finalization', {})
    pending_normal = (request.get('status') in {'dispatching', 'dispatched', 'running', 'unknown'}
                      or request.get('dispatched_at') and not request.get('response'))
    if (pending_normal and not preserve_source
            or final.get('dispatched_at') and final.get('status') not in {'completed', 'failed', 'interrupted'}
            or any(c.get('name') not in {'documents.read', 'documents.search', 'documents.list'}
                   for c in progress.get('pending_tool_calls', [])[progress.get('tool_cursor', 0):])):
        return True
    request_ids = {row['request_id'] for row in packet['handoff'].get('model_calls', []) if row.get('request_id')}
    operation = request.get('operation_id')
    operations = {operation} - {None}
    resolved_normal = not pending_normal
    original_native = native_identity(run_id, request) if pending_normal and preserve_source else None
    retained_reservations = {}
    with engine.store._read_db() as db:
        for raw, in db.execute("SELECT data FROM records WHERE collection='tool_invocations' AND json_extract(data,'$.run_id')=? "
                "AND json_extract(data,'$.handoff_id')=?", (run_id, hid)):
            row = json.loads(raw)
            if row.get('status') in {'running', 'unknown'} or row.get('status') == 'pending' and row.get('tool') not in READ_ONLY:
                return True
            request_ids.add(row.get('request_id'))
        for raw, in db.execute("SELECT data FROM records WHERE collection='codex_reasoning' AND json_extract(data,'$.run_id')=? "
                "AND json_extract(data,'$.memory_capture_binding.handoff_id')=?", (run_id, hid)):
            row = json.loads(raw)
            checkpoint = row.get('checkpoint', {})
            reservation = row.get('reservation', {})
            if preserve_source:
                from .implementation_stage_readback import terminal_proof, observed_checkpoint
                if terminal_proof(db, run_id, row):
                    checkpoint = observed_checkpoint(db, run_id, row)
                if (checkpoint.get('status') in {'interrupted', 'failed', 'cancelled'}
                        and checkpoint.get('recovered_read_only') is True
                        and checkpoint.get('run_id') == run_id and checkpoint.get('operation_id') == row.get('id')
                        and checkpoint.get('thread_id') and checkpoint.get('turn_id')
                        and (row.get('memory_capture_binding') or {}).get('role') == 'engineer'
                        and not any(str(m.get('text', '')).strip() for m in checkpoint.get('public_messages', [])
                                    if m.get('phase') in {None, 'final_answer'})
                        and reservation.get('provider') == 'local_codex' and reservation.get('run_id') == run_id
                        and reservation.get('operation_id') == row.get('operation_id') and reservation.get('id')):
                    retained_reservations[reservation['id']] = reservation
                    resolved_normal |= original_native == (row.get('id'), row.get('operation_id'))
            if checkpoint.get('status') not in {'completed', 'failed', 'interrupted', 'cancelled'}:
                return True
            request_ids.add(reservation.get('id'))
            operations.add(row.get('operation_id'))
    request_ids.discard(None)
    operations.discard(None)
    return not resolved_normal or any((row.get('id') in request_ids or row.get('operation_id') in operations)
        and retained_reservations.get(row.get('id')) != row
        for row in engine.store.get('budgets', run_id, {}).get('pending', {}).values())
