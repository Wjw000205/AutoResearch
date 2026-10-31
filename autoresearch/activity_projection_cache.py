"""Exact-snapshot cache for display metadata, never for evidence/approval gates.

Large handoff JSON only needs parsing again when an input dependency changes.
Tool/recovery observations and authenticated reading results remain live.
"""
import hashlib
import json
import os

from .billing_projection_cache import TotalsCache


CACHE = TotalsCache(capacity=96)
COLLECTIONS = ('handoff_inputs', 'handoff_progress', 'implementation_document_drafts',
               'codex_workflows', 'stage_contexts')


def revision(snapshot):
    def heads():
        with snapshot._read_db() as db:
            rows = [tuple(row) for row in db.execute(
                'SELECT collection,key,updated_at FROM records INDEXED BY records_recent '
                'WHERE collection IN (' + ','.join('?' for _ in COLLECTIONS) + ') '
                'ORDER BY collection,updated_at,key', COLLECTIONS)]
        return hashlib.sha256(json.dumps(rows).encode()).hexdigest()
    # Reuse only within this transaction across the three cards.
    return snapshot.memoized_read(('display_activity_heads',), heads)


def project(snapshot, run_id, hid, kind):
    from .assignment_activity import _meta, _directories
    key = (os.path.normcase(str(snapshot.path.resolve())), run_id, hid, kind, revision(snapshot))

    def build():
        with snapshot._read_db() as db:
            return {'metadata': _meta, 'directories': _directories}[kind](db, run_id, hid)

    return CACHE.get(key, build)
