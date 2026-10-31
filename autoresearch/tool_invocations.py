"""Durable execution intent and conservative recovery of uncertain side effects."""
from __future__ import annotations

import copy
import hashlib

from .execution_archive import encoded, load, sanitize
from .storage import now


class ToolOutcomeUnknown(RuntimeError):
    """A recoverable operator/reconciliation requirement; never automatic replay."""


READ_ONLY = frozenset({
    'experiments.status', 'experiments.read', 'experiments.compare', 'experiments.analyze', 'experiments.inspect_module',
    'literature.protocols',
    'datasets.discover', 'datasets.inspect',
    'memory.search', 'experiments.history', 'hardware.inspect', 'resources.assess',
    'sources.list', 'sources.links', 'sources.read', 'documents.list', 'documents.search',
    'documents.read', 'artifacts.read', 'artifacts.list', 'context.expand', 'evidence.read', 'evidence.catalog',
    'implementation.locate_document',
    'code.locate', 'code.read', 'code.record_notes', 'documents.recall',
    'evidence.search', 'pdf.info', 'pdf.search', 'pdf.read', 'pdf.manifest',
    'literature.rag_search', 'literature.rag_read', 'literature.paper_map', 'literature.dossier',
})

# These local writers validate their handoff/version and use immutable,
# content-addressed files. Implementation writes/edits also retain the exact
# original tool arguments and return their saved result before reapplying a
# patch. A changed draft must still pass its ordinary hash/CAS gates. This is
# not a read-only classification or a generic external exactly-once guarantee.
LOCAL_REPLAY_SAFE = frozenset({
    'experiments.cleanup',  # Saved per-file intent; changed/recreated files never replay deletion.
    'experiments.record_notes',
    'literature.record_protocol',
    'datasets.download',
    'architecture.draw_diagrams', 'implementation.write_document',
    'implementation.edit_document',
    'implementation.record_self_check',
})


def identity(run_id, role, handoff_id, turn, cursor, call):
    return hashlib.sha256(encoded([run_id, role, handoff_id, turn, cursor, call]).encode()).hexdigest()


def prepare(store, run_id, role, invocation_id, name, args, *, handoff_id=None, request_id=None):
    clean = sanitize(store, {'name': name, 'arguments': args})
    digest = hashlib.sha256(encoded(clean).encode()).hexdigest()
    def create(previous):
        if previous:
            if (previous.get('run_id') != run_id or previous.get('role') != role
                    or previous.get('call_sha256') != digest):
                raise ValueError('Tool invocation identity mismatch')
            return previous
        return {'id': invocation_id, 'run_id': run_id, 'role': role, 'handoff_id': handoff_id,
                'request_id': request_id, 'tool': name, 'call': clean, 'call_sha256': digest,
                'idempotency_key': invocation_id, 'status': 'pending', 'revision': 0,
                'created_at': now(), 'updated_at': now()}
    return store.mutate('tool_invocations', invocation_id, create)


def transition(store, record, status, **fields):
    revision = record['revision']
    def change(previous):
        if not previous or previous.get('revision') != revision:
            raise ToolOutcomeUnknown('Concurrent tool invocation state changed; reconcile before replay')
        return {**previous, **fields, 'status': status, 'revision': revision + 1, 'updated_at': now()}
    return store.mutate('tool_invocations', record['id'], change)


def begin(store, record):
    if record['status'] == 'succeeded':
        payload = load(store, record['run_id'], record['role'], record['archive_id'], record['archive_sha256'])
        return record, copy.deepcopy(payload['result'])
    if record['status'] in {'running', 'unknown', 'failed'}:
        # A crash may happen after the immutable archive commits but before
        # the invocation success marker. Query the committed result first.
        keys = store.record_keys('execution_archives', record['run_id'],
            predicate="json_extract(data,'$.invocation_id')=? AND json_extract(data,'$.role')=?",
            parameters=[record['id'], record['role']])
        saved = [store.get('execution_archives', key) for key in keys]
        for archive in saved:
            payload = load(store, record['run_id'], record['role'], archive['id'], archive['sha256'])
            if hashlib.sha256(encoded(payload.get('call')).encode()).hexdigest() != record['call_sha256']:
                raise ValueError('Archived tool call identity mismatch')
            recovered = transition(store, record, 'succeeded', archive_id=archive['id'],
                                   archive_sha256=archive['sha256'], recovery='committed_archive_reconciled')
            return recovered, copy.deepcopy(payload['result'])
        # Reuse a success journal committed before its archive/index mirror.
        # This is a result lookup, never a second external invocation.
        with store._read_db() as db:
            event = db.execute("SELECT id,data FROM events WHERE run_id=? AND kind='tool.completed' "
                "AND message=? AND json_extract(data,'$.role')=? AND json_extract(data,'$.tool_call_id')=? "
                "ORDER BY id LIMIT 1", (record['run_id'], record['tool'], record['role'], record['id'])).fetchone()
        if event:
            import json
            from .execution_archive import save
            payload = {'call': record['call'], 'result': json.loads(event['data']).get('result')}
            archive = save(store, record['run_id'], record['role'], record['id'], payload,
                           handoff_id=record.get('handoff_id'), request_id=record.get('request_id'), event_id=event['id'])
            recovered = transition(store, record, 'succeeded', archive_id=archive['id'],
                                   archive_sha256=archive['sha256'], recovery='committed_tool_journal_reconciled')
            return recovered, copy.deepcopy(payload['result'])
    if (record['status'] in {'running', 'unknown', 'failed'}
            and record['tool'] not in READ_ONLY and record['tool'] not in LOCAL_REPLAY_SAFE):
        if record['status'] != 'unknown':
            record = transition(store, record, 'unknown', recovery='reconciliation_required')
        raise ToolOutcomeUnknown(f"Tool {record['tool']} has an uncertain outcome; invocation_id="
                                 f"{record['id']}; inspect external result before resolving")
    return transition(store, record, 'running'), None


def reconcile(store, invocation_id, *, result=None, confirmed_not_executed=False):
    """Explicit trusted reconciliation, after inspecting the target system.

    No generic external exactly-once claim. Never treat elapsed time as proof.
    """
    record = store.get('tool_invocations', invocation_id)
    if not record or record['status'] not in {'unknown', 'running', 'failed'}:
        raise ValueError('Only uncertain invocations can be reconciled')
    if confirmed_not_executed and result is not None:
        raise ValueError('Reconciliation outcomes are mutually exclusive')
    if confirmed_not_executed:
        return transition(store, record, 'pending', recovery='confirmed_not_executed')
    if result is None:
        raise ValueError('A verified external result is required')
    from .execution_archive import save
    archive = save(store, record['run_id'], record['role'], invocation_id,
                   {'call': record['call'], 'result': result}, handoff_id=record.get('handoff_id'))
    return transition(store, record, 'succeeded', archive_id=archive['id'],
                      archive_sha256=archive['sha256'], recovery='external_result_reconciled')
