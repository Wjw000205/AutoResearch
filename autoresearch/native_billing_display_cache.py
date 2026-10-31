"""Bounded reuse of safe native billing operands for read-only UI projections.

This is not an accounting ledger or an execution-admission cache. Every view
reads current record heads in its own SQLite snapshot. Historical rows and
pricing estimates are reused only for that exact persisted version.
"""
from collections import OrderedDict
import hashlib
import json
import os
import threading


class DisplayCache:
    def __init__(self, *, max_entries=32768, max_bytes=64 * 1024 * 1024):
        self._lock = threading.Lock()
        self._rows = OrderedDict()
        self._estimates = OrderedDict()
        self._resident = {}
        self._estimate_keys = {}
        self._bytes = 0
        self.max_entries, self.max_bytes = max_entries, max_bytes

    def rows(self, store, db, projection, decode):
        if not db.in_transaction:
            db.execute('BEGIN')
        # Both selectors are covered metadata reads. A new snapshot must never
        # borrow a value from a different version of the same record.
        heads = list(db.execute("SELECT key,updated_at FROM records INDEXED BY records_recent "
                                "WHERE collection='codex_usage'"))
        stat = store.path.stat()
        database = (os.path.normcase(str(store.path.resolve())), stat.st_dev, stat.st_ino)
        identities = [(database, row['key'], row['updated_at']) for row in heads]
        current = {}
        # Coalesce shared overview/detail loads without decoding the same new
        # rows in each HTTP worker. No writer or execution gate uses this lock.
        with self._lock:
            missing = []
            for identity in identities:
                entry = self._rows.get(identity)
                if entry is None:
                    missing.append(identity)
                else:
                    current[identity] = entry[0]
                    self._rows.move_to_end(identity)
            for start in range(0, len(missing), 400):
                batch = missing[start:start + 400]
                placeholders = ','.join('?' for _ in batch)
                for raw in db.execute(f"SELECT key,updated_at,json_object({projection}) AS projection "
                        f"FROM records WHERE collection='codex_usage' AND key IN ({placeholders})",
                        [identity[1] for identity in batch]):
                    identity = (database, raw['key'], raw['updated_at'])
                    value = decode(raw['projection'])
                    current[identity] = value
                    size = len(raw['projection'].encode('utf-8'))
                    if size > self.max_bytes or self.max_entries < 1:
                        continue
                    while self._rows and (len(self._rows) >= self.max_entries or self._bytes + size > self.max_bytes):
                        _, (removed, removed_size) = self._rows.popitem(last=False)
                        self._bytes -= removed_size
                        self._resident.pop(id(removed), None)
                        for estimate_key in self._estimate_keys.pop(id(removed), set()):
                            self._estimates.pop(estimate_key, None)
                    self._rows[identity] = (value, size)
                    self._resident[id(value)] = value
                    self._bytes += size
        return [current[identity] for identity in identities]

    def estimate(self, row, totals, pricing, pricing_revision, calculate):
        identity = (id(row), pricing_revision)
        with self._lock:
            entry = self._estimates.get(identity)
            if entry is not None and entry[0] is row:
                self._estimates.move_to_end(identity)
                return dict(entry[1])
            value = calculate(row, totals, pricing)
            # Retain the exact immutable row operand, so an object-id reuse
            # after row eviction can never collide with an old estimate.
            if self.max_entries > 0 and self._resident.get(id(row)) is row:
                self._estimates[identity] = (row, value)
                self._estimate_keys.setdefault(id(row), set()).add(identity)
                while len(self._estimates) > self.max_entries:
                    removed_key, _ = self._estimates.popitem(last=False)
                    keys = self._estimate_keys.get(removed_key[0], set())
                    keys.discard(removed_key)
                    if not keys:
                        self._estimate_keys.pop(removed_key[0], None)
            return dict(value)


def pricing_revision(pricing):
    return hashlib.sha256(json.dumps(pricing, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


CACHE = DisplayCache()
