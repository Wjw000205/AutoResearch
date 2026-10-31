"""Reuse identical successful checks only inside one read-only validation.

No approval cache survives the call. Complete argument fingerprints preserve
scope, and subsequent operations recheck paid records and original documents.
"""
from contextvars import ContextVar
from collections import OrderedDict
from copy import deepcopy
from functools import wraps

_operation = ContextVar('architecture_read_validation', default=None)
SOURCE_CACHE_BYTES = 32 * 1024 * 1024
SOURCE_CACHE_ENTRIES = 64


def operation(function):
    @wraps(function)
    def checked(*args, **kwargs):
        if _operation.get() is not None:
            return function(*args, **kwargs)
        token = _operation.set({'values': OrderedDict(), 'reads': OrderedDict(), 'active': set(),
                                'sources': OrderedDict(), 'source_sizes': {}, 'source_bytes': 0})
        try:
            return function(*args, **kwargs)
        finally:
            _operation.reset(token)
    return checked


def _memoized(function, engine, args, kwargs, bucket):
    from .engine import ResearchBlocked, digest
    state = _operation.get()
    key = (id(engine), function.__module__, function.__name__, digest([args, kwargs]))
    values = state[bucket]
    if key in values:
        values.move_to_end(key)
        return deepcopy(values[key])
    if key in state['active']:
        raise ResearchBlocked('架构校验的前驱引用形成循环，不能登记批准。')
    state['active'].add(key)
    try:
        result = function(engine, *args, **kwargs)
        values[key] = deepcopy(result)
        # Keep completed parent proofs even when their recursive children filled
        # the cache. A full first-in cache otherwise recomputes every late parent.
        while len(values) > 256:
            values.popitem(last=False)
        return result
    finally:
        state['active'].remove(key)


def once(function):
    @wraps(function)
    @operation
    def checked(engine, *args, **kwargs):
        return _memoized(function, engine, args, kwargs, 'values')
    return checked


def existing_only(function):
    """Reuse exact reads only inside an existing synchronous architecture check.

    Ordinary document tools and other callers still validate on every call.
    Document results use a separate bounded cache so they cannot evict proofs.
    """
    @wraps(function)
    def checked(engine, *args, **kwargs):
        if _operation.get() is None:
            return function(engine, *args, **kwargs)
        return _memoized(function, engine, args, kwargs, 'reads')
    return checked


def source_read(store, identity, builder, *, borrowed=False):
    """Reuse pure source metadata only within the current read-only operation.

    Private readers may borrow an unchanged catalogue to select one paper;
    public callers receive a detached copy. File bytes and SHA checks are not
    cached here. Ordinary tools and subsequent operations always build afresh.
    """
    state = _operation.get()
    if state is None:
        return builder()
    key = (id(store), identity)
    values = state['sources']
    if key in values:
        values.move_to_end(key)
        return values[key] if borrowed else deepcopy(values[key])
    value = builder()
    # A bibliographic catalogue may be larger than one ordinary document. The
    # operation still has one hard aggregate bound and drops all entries on exit.
    from .storage import _ReadSnapshot
    # A recovered definition chain contains several independently authenticated
    # predecessor summaries. The four current summaries total about 13 MiB;
    # an 8 MiB LRU evicts the chain before its next reader, rebuilding it for
    # each unit. This is host-only metadata, scoped to one validation operation.
    maximum = SOURCE_CACHE_BYTES
    size = _ReadSnapshot._size((key, value), maximum) + 128
    if size <= maximum:
        while values and (len(values) >= SOURCE_CACHE_ENTRIES or state['source_bytes'] + size > maximum):
            old_key, _ = values.popitem(last=False)
            state['source_bytes'] -= state['source_sizes'].pop(old_key)
        values[key] = value
        state['source_sizes'][key] = size
        state['source_bytes'] += size
        return value if borrowed else deepcopy(value)
    return value
