"""Reuse display totals only for an identical read-only ledger snapshot.

No TTL, persisted state, budget admission or authorization uses this cache.
Global native heads are required because thread ownership crosses run scopes.
"""
from collections import OrderedDict
from concurrent.futures import Future
from copy import deepcopy
import hashlib
import json
import os
import threading


def revision(store):
    with store._read_db() as db:
        heads = list(db.execute("SELECT collection,key,updated_at FROM records INDEXED BY records_recent "
            "WHERE collection IN ('budgets','usage','codex_usage','codex_workflows') "
            "OR (collection='settings' AND key='model_pricing') "
            "ORDER BY collection,updated_at,key"))
    return hashlib.sha256(json.dumps([tuple(row) for row in heads]).encode()).hexdigest()


class TotalsCache:
    def __init__(self, capacity=32):
        self._lock = threading.Lock()
        self._entries = OrderedDict()
        self._pending = {}
        self._capacity = capacity

    def get(self, key, build):
        with self._lock:
            if key in self._entries:
                self._entries.move_to_end(key)
                return deepcopy(self._entries[key])
            future = self._pending.get(key)
            owner = future is None
            if owner:
                future = self._pending[key] = Future()
        if not owner:
            return deepcopy(future.result())
        try:
            value = build()
            with self._lock:
                self._entries[key] = deepcopy(value)
                while len(self._entries) > self._capacity:
                    self._entries.popitem(last=False)
            future.set_result(value)
            return deepcopy(value)
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._lock:
                self._pending.pop(key, None)


CACHE = TotalsCache()


def totals(store, run_id, build, *, variant='totals'):
    # The token and totals must observe the same transaction, including when
    # a caller already holds an older snapshot while another request commits.
    with store.read_snapshot() as snapshot:
        key = (os.path.normcase(str(store.path.resolve())), run_id, variant, revision(snapshot))
        return CACHE.get(key, lambda: build(snapshot))
