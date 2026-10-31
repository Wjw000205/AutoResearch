"""Bounded, single-flight reading-card calculations, never evidence gate state.

Only UI callers opt in. Nothing is persisted and an old revision is never served
as a current percentage. One worker reuses the original evidence validator.
"""
from __future__ import annotations

import copy
import multiprocessing
import hashlib
import json
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace


def pending(state='calculating'):
    return {'state': state, 'reported': False, 'percent': None, 'completed': None,
            'total': None, 'unit': 'characters', 'source': 'same_handoff_delivered_receipts',
            'scope': 'current_task_sources'}


def database_identity(path):
    return os.path.normcase(str(Path(path).resolve()))


def revision(store, hid, workflow_id, directories):
    """Observe covering-index record heads, without inspecting large JSON bodies.

    Current input/progress/draft/workflow heads bind navigation and the ledger.
    Global manifest/snapshot/delivery metadata conservatively invalidates every
    card when any authenticated dependency changes, including legacy ownership.
    """
    with store._read_db() as db:
        heads = []
        for collection, key in (('handoff_inputs', hid), ('handoff_progress', hid),
                                ('implementation_document_drafts', hid), ('codex_workflows', workflow_id)):
            row = db.execute('SELECT updated_at FROM records WHERE collection=? AND key=?',
                             (collection, key)).fetchone()
            heads.append((collection, key, row[0] if row else None))
        for collection in ('context_manifests', 'context_snapshots', 'evidence_deliveries'):
            row = db.execute('SELECT count(*),max(updated_at) FROM records INDEXED BY records_recent '
                             'WHERE collection=?', (collection,)).fetchone()
            heads.append((collection, *row))
    value = [heads, directories]
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@contextmanager
def read_snapshot(path):
    """Open only a background read transaction; do not construct a migrating Store."""
    from .storage import _ReadSnapshot
    source = SimpleNamespace(path=Path(path))
    db = sqlite3.connect(source.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    db.execute('BEGIN')
    snapshot = _ReadSnapshot(source, db)
    try:
        yield snapshot
    finally:
        snapshot._closed = True
        snapshot._clear_cache()
        db.close()


def calculate_reading(path, run_id, hid, role, workflow_id, token):
    """Picklable CPU job: only identifiers cross the process boundary.

    Open original receipts read-only in the child; never copy a parent Store,
    connection, execution engine, approval or credentials into the job.
    """
    from .assignment_activity import _meta, _directories, _reading
    with read_snapshot(path) as snapshot, snapshot._read_db() as db:
        meta = _meta(db, run_id, hid)
        directories = _directories(db, run_id, hid)
        if (meta.get('workflow_id') != workflow_id
                or revision(snapshot, hid, workflow_id, directories) != token):
            return False, None
        reading = _reading(db, run_id, hid, directories, store=snapshot, role=role)
    with read_snapshot(path) as snapshot:
        valid = revision(snapshot, hid, workflow_id, directories) == token
    return valid, reading


class ReadingCache:
    """Coalesce repeated revisions; queue only the latest job for each identity."""
    def __init__(self, *, capacity=128, queue_capacity=32, isolated=False):
        self._lock = threading.RLock()
        self._entries = OrderedDict()
        self._pending = OrderedDict()
        self._running = None
        self._capacity, self._queue_capacity = capacity, queue_capacity
        self._isolated = isolated
        self._executor = None
        self._retired = []
        self._closing = False

    def get(self, identity, token, calculate):
        with self._lock:
            if self._closing:
                return pending('unavailable')
            saved = self._entries.get(identity)
            if saved and saved['token'] == token:
                self._entries.move_to_end(identity)
                if saved['state'] == 'ready':
                    return copy.deepcopy(saved['value'])
                if time.monotonic() < saved['retry_after']:
                    return pending('unavailable')
            self._entries.pop(identity, None)
            waiting = self._pending.get(identity)
            if self._running != (identity, token) and (not waiting or waiting[0] != token):
                self._pending[identity] = (token, calculate)
                while len(self._pending) > self._queue_capacity:
                    self._pending.popitem(last=False)
            self._start_locked()
            return pending()

    def _start_locked(self):
        if self._closing or self._running is not None or not self._pending:
            return
        if self._executor is None:
            self._executor = (ProcessPoolExecutor(max_workers=1,
                mp_context=multiprocessing.get_context('spawn')) if self._isolated
                else ThreadPoolExecutor(max_workers=1, thread_name_prefix='reading-card'))
        identity, (token, calculate) = self._pending.popitem(last=False)
        self._running = identity, token
        try:
            future = self._executor.submit(calculate)
        except BrokenProcessPool as exc:
            from concurrent.futures import Future
            future = Future()
            future.set_exception(exc)
        future.add_done_callback(lambda result: self._finished(identity, token, result))

    def _finished(self, identity, token, future):
        broken = False
        try:
            valid, value = future.result()
            saved = {'token': token, 'state': 'ready', 'value': copy.deepcopy(value)} if valid else None
        except Exception as exc:
            broken = isinstance(exc, BrokenProcessPool)
            # A failed computation has no percentage and never becomes approval.
            saved = {'token': token, 'state': 'unavailable', 'retry_after': time.monotonic() + 2}
        with self._lock:
            if broken and self._executor is not None:
                # A child crash is a missing display measurement. Reset the
                # pool after backoff; never fall back to the HTTP process CPU.
                # Future callbacks execute while ProcessPoolExecutor holds its
                # shutdown lock. Calling shutdown here would deadlock every
                # subsequent card. The broken manager reaps its own children;
                # close() releases its handles from outside this callback.
                self._retired.append(self._executor)
                self._executor = None
            replacement = self._pending.get(identity)
            if saved is not None and (not replacement or replacement[0] == token):
                self._entries[identity] = saved
                self._entries.move_to_end(identity)
                while len(self._entries) > self._capacity:
                    self._entries.popitem(last=False)
            self._running = None
            self._start_locked()

    def close(self):
        with self._lock:
            self._closing = True
            self._pending.clear()
            executor = self._executor
            retired, self._retired = self._retired, []
        try:
            if executor is not None:
                executor.shutdown(wait=True, cancel_futures=True)
            for old in retired:
                old.shutdown(wait=True, cancel_futures=True)
        finally:
            with self._lock:
                self._executor = None
                self._entries.clear()
                self._running = None
                self._closing = False


CACHE = ReadingCache(isolated=True)
