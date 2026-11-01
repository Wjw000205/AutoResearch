"""Private immutable-archive reuse during one read-only scope verification.

Every receipt and selected page is still checked. Archive hashes are reused only
inside the same SQLite read transaction, never across requests or approvals.
"""
from __future__ import annotations

import copy
import json
from contextlib import contextmanager
from contextvars import ContextVar

from .experiment_workflow import sha

_CURRENT = ContextVar('scope_read_session', default=None)
MAX_BYTES = 64 * 1024 * 1024
MAX_RECORDS = 8


@contextmanager
def reading(engine):
    active = _CURRENT.get()
    if active is not None and active['store'] is engine.store:
        yield engine
        return
    with engine.store.read_snapshot() as view:
        reader = copy.copy(engine)
        reader.store = view
        state = {'store': view, 'records': {}, 'bytes': 0}
        token = _CURRENT.set(state)
        try:
            yield reader
        finally:
            state['records'].clear()
            _CURRENT.reset(token)


def verified_record(engine, collection, key, run_id, digest, *, error):
    """Return a private read-only operand only to compare/source-page readers.

Outside this local verification session, None selects the original read path.
Callers may read but must not mutate the returned archive or expose its objects.
Their public page results contain serialized text, not references into the cache.
"""
    active = _CURRENT.get()
    if active is None or active['store'] is not engine.store:
        return None
    field = {'experiment_evidence': 'summary', 'experiment_sources': 'files'}[collection]
    identity = (collection, key, run_id, digest)
    if identity in active['records']:
        return active['records'][identity]
    with engine.store._read_db() as db:
        row = db.execute('SELECT data FROM records WHERE collection=? AND key=?', (collection, key)).fetchone()
    record = json.loads(row['data']) if row else {}
    if record.get('run_id') != run_id or sha(record.get(field)) != digest:
        raise ValueError(error)
    size = len(row['data'].encode('utf-8'))
    if len(active['records']) < MAX_RECORDS and active['bytes'] + size <= MAX_BYTES:
        active['records'][identity] = record
        active['bytes'] += size
    return record
