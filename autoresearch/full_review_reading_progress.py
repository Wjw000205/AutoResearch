"""Read-only card coverage for exact full-review assignments.

The HTTP path schedules an isolated display calculation. Original acquisition,
submitted delivery and same-role frozen reuse remain distinct. This module is
never imported by an execution or acceptance gate and writes no research state.
"""
from __future__ import annotations

import hashlib
import json
import threading
from time import monotonic as _clock
from collections import OrderedDict
from functools import partial
from types import SimpleNamespace

from . import independent_full_review as full, review_material_packet as packets
from . import assignment_reading_cache as cache
from .architecture_validation import operation

_REUSE = OrderedDict()
_REFRESH = OrderedDict()
_REFRESH_LOCK = threading.RLock()


def revision(store, hid):
    """Conservative display invalidation, never evidence authentication.

    Historical parent heads also matter. A bounded refresh rechecks raw bytes
    even if an out-of-band edit preserves timestamps. Unrelated WAL writes do
    not prevent an isolated calculation from ever publishing its result.
    """
    with store._read_db() as db:
        heads = [tuple(db.execute('SELECT count(*),max(updated_at) FROM records INDEXED BY records_recent WHERE collection=?',
            (collection,)).fetchone()) for collection in ('handoffs', 'handoff_inputs', 'handoff_progress',
                'decisions', 'independent_full_review_notes', 'tool_invocations')]
    return full.sha([cache.revision(store, hid, None, []), heads])


def available(store, run_id, hid, role):
    with store._read_db() as db:
        row = db.execute("""SELECT json_type(i.data,'$.context.independent_full_review'),
            json_type(i.data,'$.context.independent_full_review.version'),
            json_extract(i.data,'$.context.independent_full_review.version'),
            json_extract(h.data,'$.to_role'),json_extract(h.data,'$.run_id'),
            json_type(i.data,'$.context.review_material_protocol'),
            json_extract(i.data,'$.context.review_material_protocol'),
            json_extract(i.data,'$.context.review_scope'),
            json_extract(i.data,'$.context.whole_code_review.version')
            FROM records i JOIN records h ON h.collection='handoffs' AND h.key=i.key
            WHERE i.collection='handoff_inputs' AND i.key=?
                AND json_extract(i.data,'$.run_id')=?""", (hid, run_id)).fetchone()
    if not row or row[0] is None or row[5] is None and row[8] is None:
        return None
    return (row[0] == 'object' and row[1] == 'integer' and row[2] == 1 and row[3] == role
        and row[4] == run_id and (row[5] is None and row[8] == 1 or row[5] == 'integer'
            and row[6] in packets.SCOPES and row[7] == packets.SCOPES[row[6]]))


def project(store, run_id, hid, role, *, deferred=True):
    supported = available(store, run_id, hid, role)
    if supported is None:
        return None
    if not supported:
        return {**cache.pending('unavailable'), 'scope': 'full_review_sources'}
    if not deferred:
        from .engine import ResearchBlocked
        try: return measure(store, run_id, hid, role)
        except (ValueError, TypeError, KeyError, ResearchBlocked):
            return {**cache.pending('unavailable'), 'scope': 'full_review_sources'}
    base = revision(store, hid)
    path = cache.database_identity(store.path)
    identity = (path, run_id, hid, role, 'full_review_sources')
    with _REFRESH_LOCK:
        state = _REFRESH.get(identity)
        if state is None or state['base'] != base:
            state = {'base': base, 'generation': 0, 'ready_at': None}
            _REFRESH[identity] = state
        if state['ready_at'] is not None and _clock() - state['ready_at'] >= 30:
            state.update(generation=state['generation'] + 1, ready_at=None)
        token = (base, state['generation'])
        value = cache.CACHE.get(identity, token, partial(calculate, path, run_id, hid, role, token))
        if value.get('reported') and state['ready_at'] is None: state['ready_at'] = _clock()
        _REFRESH.move_to_end(identity)
        while len(_REFRESH) > 128: _REFRESH.popitem(last=False)
    return {**value, 'scope': 'full_review_sources'}


def calculate(path, run_id, hid, role, token):
    with cache.read_snapshot(path) as store:
        if revision(store, hid) != token[0]:
            return False, None
        value = measure(store, run_id, hid, role)
    with cache.read_snapshot(path) as store:
        return revision(store, hid) == token[0], value


def _reuse_dependencies(store, run_id, role, claimed):
    """Fingerprint every record read by frozen-reuse authentication.

    This small presentation cache retains no source body or physical-file
    integrity proof. A changed/deleted dependency (including an unchanged
    timestamp) invalidates it. New requests in the current handoff do not
    repeatedly reauthenticate unchanged historical deliveries.
    """
    # Navigation only: reading_handoffs follows exactly this frozen parent
    # pointer. Discover it in SQLite without decoding/authenticating every
    # historical prompt twice. The cache miss below still runs the ORIGINAL
    # validator on all source, role, decision, failed-call and delivery bytes.
    owners = set()
    with store._read_db() as db:
        for row in claimed['parents']:
            current, chain = row['handoff_id'], set()
            while current:
                if not isinstance(current, str) or current in chain or len(chain) >= 32:
                    raise ValueError('Invalid full-review display reading ancestry')
                chain.add(current); owners.add(current)
                parent = db.execute("SELECT json_type(data,'$.context.independent_full_review_reading.parent_handoff_id'),json_extract(data,'$.context.independent_full_review_reading.parent_handoff_id') FROM records WHERE collection='handoff_inputs' AND key=?", (current,)).fetchone()
                if not parent: raise ValueError('Missing full-review display ancestor')
                if parent[0] not in (None, 'null', 'text'):
                    raise ValueError('Invalid full-review display parent pointer')
                current = parent[1]
    heads = []
    with store._read_db() as db:
        for owner in sorted(owners):
            for collection in ('handoffs', 'handoff_inputs', 'handoff_progress', 'decisions'):
                row = db.execute('SELECT data FROM records WHERE collection=? AND key=?', (collection, owner)).fetchone()
                heads.append((collection, owner, hashlib.sha256(row[0].encode()).hexdigest() if row else None))
            # These scopes match the validator's same-role, same-owner reads.
            for collection in ('context_manifests', 'evidence_deliveries', 'independent_full_review_notes', 'tool_invocations'):
                rows = db.execute("""SELECT key,data FROM records WHERE collection=?
                    AND coalesce(json_extract(data,'$.run_id'),json_extract(data,'$.research_id'))=?
                    AND coalesce(json_extract(data,'$.handoff_id'),json_extract(data,'$.operation_id'))=?
                    ORDER BY key""", (collection, run_id, owner)).fetchall()
                for key, raw in rows:
                    heads.append((collection, key, hashlib.sha256(raw.encode()).hexdigest()))
                    if collection == 'context_manifests':
                        manifest = json.loads(raw)
                        for dep, identity in (('context_manifests', manifest.get('compiled_manifest_id')),
                                              ('context_snapshots', manifest.get('snapshot_id'))):
                            if not identity: continue
                            if dep == 'context_snapshots':
                                fields = ('id', 'run_id', 'role', 'original_transcript_id', 'context_manifest_id', 'context_manifest_sha256')
                                projection = ','.join("'" + name + "',json_extract(data,'$." + name + "')" for name in fields)
                                record = db.execute('SELECT json_object(' + projection + ') FROM records WHERE collection=? AND key=?', (dep, identity)).fetchone()
                            else:
                                record = db.execute('SELECT data FROM records WHERE collection=? AND key=?', (dep, identity)).fetchone()
                            heads.append((dep, identity, hashlib.sha256(record[0].encode()).hexdigest() if record else None))
            for key, kind, message, raw in db.execute("SELECT id,kind,message,data FROM events WHERE run_id=? AND json_extract(data,'$.handoff_id')=? ORDER BY id", (run_id, owner)):
                heads.append(('events', key, kind, message, hashlib.sha256(raw.encode()).hexdigest()))
    return full.sha(heads)


def _reuse(store, run_id, hid, role, context):
    claimed = context.get(packets.REUSE, {}).get(role)
    if claimed is None:
        return {}
    from .review_material_reuse import validate_certificate
    key = (cache.database_identity(store.path), run_id, hid, role, full.sha(claimed),
        full.sha(context[full.KEY]), _reuse_dependencies(store, run_id, role, claimed))
    if key not in _REUSE:
        validate_certificate(store, run_id, context, role, claimed)
        _REUSE[key] = True
        while len(_REUSE) > 16: _REUSE.popitem(last=False)
    _REUSE.move_to_end(key)
    return claimed['ranges']


def _code_delivery_scope(store, run_id, hid, role, context, schema, binding, observed_at):
    from pathlib import Path
    from . import code_whole_review as whole
    from .evidence_delivery import delivery_spans, tracking_required
    from .implementation_alignment_evidence import _code_text, _full_handoffs, _own_code_spans
    from .implementation_reading import coverage
    from .implementation_whole_reading import required
    from .review_material_reuse import union
    marker = context[whole.KEY]
    if (type(marker.get('version')) is not int or marker['version'] != whole.VERSION
            or binding != whole.binding(context, schema)
            or marker['directory_sha256'] != whole.digest(context['delivery_artifacts'])):
        raise ValueError('Code coverage lacks its exact whole-delivery binding')
    data_dir = Path(store.path).parent
    engine = SimpleNamespace(store=store, data_dir=data_dir, run_dir=lambda rid: data_dir / 'runs' / rid)
    owners = _full_handoffs(engine, run_id, role, hid, context)
    total = completed = materials = complete = pending = 0
    if not tracking_required(store, run_id, hid):
        return {**cache.pending('unavailable'), 'scope': 'full_review_sources'}
    for doc in whole.reading_documents(context, required=True):
        pages = required(doc)
        length = sum(p['result']['char_end'] - p['result']['char_start'] for p in pages)
        checked = coverage(engine, run_id, doc, pages, owners)
        total += length
        completed += checked['covered_chars']
        materials += 1
        complete += checked['complete']
    for source in context['delivery_artifacts']:
        aid, source_sha = source['artifact_id'], source['sha256']
        text = _code_text(engine, run_id, aid, source, allow_binary=True)
        if text is None:
            continue  # Binary files retain their separate manifest gate.
        acquired, acknowledged = [], []
        for owner in owners:
            spans = _own_code_spans(engine, owner, aid, source_sha, text)
            if not spans:
                continue
            acquired.extend(spans)
            delivered = delivery_spans(store, run_id, role, aid, source_sha,
                identity_key='artifact_id', handoff_id=owner, text=text)
            acknowledged.extend((max(a, r['char_start']), min(b, r['char_end']))
                for a, b in spans for r in delivered)
        read = sum(end - start for start, end in union(acknowledged))
        pending += sum(end - start for start, end in union(acquired)) - read
        total += len(text)
        completed += read
        materials += 1
        complete += read == len(text)
    if total <= 0:
        raise ValueError('Code coverage has no fixed reading scope')
    return {'scope': 'full_review_sources', 'source': 'authenticated_code_delivery_and_approved_documents',
        'reported': True, 'percent': 100 if completed == total else min(99.9, round(completed * 100 / total, 1)),
        'completed': completed, 'total': total, 'unit': 'characters', 'remaining': total-completed,
        'pending_chars': pending,
        **({'architecture_reading': 'on_demand'} if full.code_delivery_scope(context) else {}),
        'materials_completed': complete, 'materials_total': materials,
        'observed_at': observed_at, 'scientific_approval': False}


@operation
def measure(store, run_id, hid, role):
    if not getattr(store, 'is_read_snapshot', False):
        with store.read_snapshot() as snapshot:
            return measure(snapshot, run_id, hid, role)
    from .engine import Harness, digest
    from .full_review_receipts import acquired_material
    from .implementation_alignment_evidence import _full_handoffs
    from .evidence_delivery import delivery_spans
    from .review_material_reuse import union
    from .storage import now
    observed_at = now()
    frozen, owner = store.get('handoff_inputs', hid) or {}, store.get('handoffs', hid) or {}
    payload = {k: frozen.get(k) for k in ('instruction', 'context', 'schema')}
    context = payload['context'] or {}
    binding = full.validate_binding(context.get(full.KEY))
    if (role not in full.REVIEWERS or frozen.get('run_id') != run_id or owner.get('run_id') != run_id
            or owner.get('to_role') != role or owner.get('input_hash') != digest(payload)
            or Harness.decision_key(run_id, role, owner.get('purpose'), **payload) != hid):
        raise ValueError('Full-review display lacks its exact frozen role/assignment identity')
    from .code_whole_review import KEY as WHOLE_CODE
    if binding['domain'] == 'complete_code_delivery' and WHOLE_CODE in context and not packets.enabled(context):
        return _code_delivery_scope(store, run_id, hid, role, context, payload['schema'], binding, observed_at)
    if (binding['source_identity'].get('context_sha256') != full.sha(full.source_context(context))
            or binding['source_identity'].get('instruction_sha256') != full.sha(payload['instruction'])
            or binding['source_identity'].get('schema_sha256') != full.sha(payload['schema'])
            or not packets.supported(context)
            or not full.purpose_matches(owner.get('purpose', ''), binding['domain'])):
        raise ValueError('Full-review display lacks its exact frozen role/assignment identity')
    reader = full.MaterialReader(context)
    mids = ['root', *packets.required(context)] if packets.enabled(context) else ['root']
    required = {mid: reader.text(mid) for mid in dict.fromkeys(mids)}
    reuse = _reuse(store, run_id, hid, role, context) if packets.enabled(context) else {}
    covered = {mid: list(reuse.get(mid, [])) for mid in required}
    acquired = {mid: list(ranges) for mid, ranges in covered.items()}
    owners = _full_handoffs(SimpleNamespace(store=store), run_id, role, hid, context)
    for current in owners:
        prior = store.get('handoff_inputs', current)['context']
        rows = (store.get('handoff_progress', current) or {}).get('tool_results', [])
        _, pages = acquired_material(store, run_id, role, current, prior, rows)
        delivered = {}
        for call, page in pages:
            mid = call.get('arguments', {}).get('material_id') or 'root'
            if mid not in required: continue
            text = required[mid]
            if page['sha256'] != hashlib.sha256(text.encode()).hexdigest():
                raise ValueError('Full-review display continuation changed source version')
            start, end = page['char_start'], page['char_end']
            acquired[mid].append((start, end))
            if mid not in delivered:
                delivered[mid] = delivery_spans(store, run_id, role, page['document_id'], page['sha256'],
                    identity_key='document_id', handoff_id=current, text=text)
            covered[mid].extend((max(start, r['char_start']), min(end, r['char_end']))
                for r in delivered[mid] if max(start, r['char_start']) < min(end, r['char_end']))
    amounts = {mid: sum(b-a for a,b in union(ranges)) for mid, ranges in covered.items()}
    total = sum(map(len, required.values())); completed = sum(amounts.values())
    if total <= 0: raise ValueError('Full-review display has no fixed reading scope')
    reused = sum(b-a for mid in required for a,b in union(reuse.get(mid, [])))
    taken = sum(b-a for ranges in acquired.values() for a,b in union(ranges))
    return {'scope': 'full_review_sources', 'source': 'authenticated_full_review_deliveries_and_reuse',
        'reported': True, 'percent': 100 if completed == total else min(99.9, round(completed * 100 / total, 1)),
        'completed': completed, 'total': total, 'unit': 'characters', 'remaining': total-completed,
        'new_delivered_chars': completed-reused, 'reused_chars': reused, 'pending_chars': taken-completed,
        'materials_completed': sum(amounts[mid] == len(text) for mid, text in required.items()),
        'materials_total': len(required), 'observed_at': observed_at, 'scientific_approval': False}
