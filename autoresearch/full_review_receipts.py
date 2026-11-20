"""Derived acquisition view; original failed reads remain immutable diagnostics.

Legacy error-only receipts have no invocation ID. Authenticate their exact
call/error against the same owner's durable invocation and failure journal.
Never repair the old arguments, grant failed reads coverage, or alter a vote.
"""
from __future__ import annotations

import hashlib
import json

from jsonschema import ValidationError

from .execution_archive import encoded


def _failed_attempts(store, run_id, role, handoff_id):
    from .independent_full_review import TOOL
    owner = store.get('handoffs', handoff_id) or {}
    frozen = store.get('handoff_inputs', handoff_id) or {}
    if (owner.get('run_id') != run_id or owner.get('to_role') != role
            or frozen.get('run_id') != run_id):
        raise ValueError('Full-review failed read has a foreign frozen owner')
    invocations = {}
    for key in store.record_keys('tool_invocations', run_id,
            predicate="json_extract(data,'$.role')=? AND json_extract(data,'$.handoff_id')=? AND json_extract(data,'$.tool')=?",
            parameters=[role, handoff_id, TOOL]):
        inv = store.get('tool_invocations', key) or {}
        call = inv.get('call')
        if (inv.get('id') != key or inv.get('run_id') != run_id or inv.get('role') != role
                or inv.get('handoff_id') != handoff_id or inv.get('tool') != TOOL
                or not isinstance(call, dict) or call.get('name') != TOOL
                or inv.get('call_sha256') != hashlib.sha256(encoded(call).encode()).hexdigest()):
            raise ValueError('Full-review failed read invocation identity differs from its original call')
        invocations[key] = encoded(call)
    # The append-only event authenticates a historical attempt even when the
    # same read-only invocation was subsequently replayed successfully.
    with store._read_db() as db:
        events = db.execute("SELECT kind,data FROM events WHERE run_id=? AND kind IN ('tool.started','tool.failed') AND message=? "
            "AND json_extract(data,'$.role')=? AND json_extract(data,'$.handoff_id')=? ORDER BY id",
            (run_id, TOOL, role, handoff_id)).fetchall()
    failures, started = set(), {}
    for row in events:
        event = json.loads(row['data'])
        key = event.get('invocation_id')
        if key not in invocations or event.get('tool_call_id') != key:
            continue
        if row['kind'] == 'tool.started':
            # The invocation mirror is mutable. The original append-only start
            # binds its arguments too; a self-consistent rehashed mirror alone
            # cannot authenticate a changed call. Ordering forbids a later
            # retry's start from authenticating an earlier failed attempt.
            started[key] = encoded({'name': TOOL, 'arguments': event.get('arguments')}) == invocations[key]
            continue
        if (not started.get(key) or event.get('interrupted') is not False
                or not isinstance(event.get('error_type'), str) or not event['error_type']
                or not isinstance(event.get('error'), str) or not event['error']):
            continue
        # execute_tool journals 1200 chars; its caller stores the first 1000.
        failures.add((invocations[key], event['error'][:1000]))
        started.pop(key, None)
    return failures


def acquired_material(store, run_id, role, handoff_id, context, rows, *, progress=None):
    """Return exact successful pages, excluding only authenticated failures.

    Supported legacy diagnostic shape is exactly {error: nonempty string}.
    Unknown, mixed success/error, foreign and unauthenticated forms fail closed.
    Successes retain the original source-byte comparison; delivery is checked
    separately by each caller. This function performs no tool execution/write.
    """
    from .independent_full_review import TOOL, MaterialReader
    failures = None
    pages, reader, completed = [], None, 0
    total = sum(isinstance(r, dict) and isinstance(r.get('call'), dict) and r['call'].get('name') == TOOL for r in rows)
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Full-review tool ledger contains an invalid record')
        call = row.get('call', {})
        if not isinstance(call, dict) or call.get('name') != TOOL:
            continue
        result = row.get('result')
        if not isinstance(result, dict):
            raise ValueError('Full-review material result has an unknown receipt shape')
        if 'error' in result:
            if set(result) != {'error'} or not isinstance(result['error'], str) or not result['error']:
                raise ValueError('Full-review material failure has an unknown or mixed receipt shape')
            if failures is None:
                failures = _failed_attempts(store, run_id, role, handoff_id)
            if (encoded(call), result['error']) not in failures:
                raise ValueError('Full-review material failure lacks its original same-owner failed attempt')
            completed += 1
            if progress:progress(completed, total)
            continue  # Diagnostic only: no read span, note anchor or approval.
        try:
            if reader is None:reader = MaterialReader(context)
            expected = reader.read(call.get('arguments', {}))
        except ValidationError as exc:
            raise ValueError('Full-review successful receipt has invalid frozen read arguments') from exc
        if result != expected:
            raise ValueError('Full-review material tool receipt differs from frozen source bytes')
        pages.append((call, result))
        completed += 1
        if progress:progress(completed, total)
    return reader, pages


def successful_reads(store, run_id, role, handoff_id, context, rows):
    return acquired_material(store, run_id, role, handoff_id, context, rows)[1]
