"""Authenticate exact read requests against immutable host completion records."""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json

from .execution_archive import encoded, load


def inherited_rows(engine, run_id, role, handoff_id, rows, *, inputs=None):
    """Only the ordinary, hash-bound save successor may inherit read receipts."""
    from .engine import ResearchBlocked, Harness, digest
    if inputs is None:
        inputs = engine.store.get('handoff_inputs', handoff_id, {})
    continuation = inputs.get('context', {}).get('implementation_document_completion', {})
    aid = continuation.get('predecessor_audit_id')
    audit = engine.store.get('stage_contexts', aid, {})
    payload = audit.get('payload', {})
    parent = payload.get('input', {})
    owner = continuation.get('source_handoff_id')
    actual = engine.store.get('handoffs', owner, {})
    task = {k: parent.get(k) for k in ('instruction', 'context', 'schema')}
    if (not owner or audit.get('run_id') != run_id or audit.get('successor_id') != handoff_id
            or digest(payload) != audit.get('sha256') or audit['sha256'] != continuation.get('predecessor_sha256')
            or parent.get('id') != owner or parent.get('run_id') != run_id
            or actual.get('to_role') != role or actual.get('input_hash') != digest(task)
            or Harness.decision_key(run_id, role, actual.get('purpose'), **task) != owner):
        raise ResearchBlocked('Inherited reads lack the exact ordinary save-successor audit')
    available = Counter(encoded(row) for row in payload.get('progress', {}).get('tool_results', []))
    stripped = []
    for row in rows:
        source = {k: v for k, v in row.items() if k != 'predecessor_handoff_id'}
        identity = encoded(source)
        if row.get('predecessor_handoff_id') != owner or not available[identity]:
            raise ResearchBlocked('Inherited read was transplanted from a different handoff or range')
        available[identity] -= 1
        stripped.append(source)
    return owner, stripped


def authenticate_reads(engine, run_id, role, handoff_id, progress, allowed, *, ancestors=()):
    from .document_index import read
    from .engine import ResearchBlocked
    if handoff_id in ancestors or len(ancestors) >= 8:
        raise ResearchBlocked('Inherited read ownership is cyclic or exceeds its supported chain')
    rows = progress.get('tool_results', [])
    inherited = [row for row in rows if row.get('predecessor_handoff_id')]
    if inherited:
        owner, original = inherited_rows(engine, run_id, role, handoff_id, inherited)
        authenticate_reads(engine, run_id, role, owner, {'tool_results': original}, allowed,
            ancestors=(*ancestors, handoff_id))
    candidates = defaultdict(list)
    with engine.store._read_db() as db:
        for raw, in db.execute("SELECT data FROM records WHERE collection='tool_invocations' "
                "AND json_extract(data,'$.run_id')=? AND json_extract(data,'$.handoff_id')=? "
                "AND json_extract(data,'$.role')=? AND json_extract(data,'$.tool')='documents.read' "
                "AND json_extract(data,'$.status')='succeeded'", (run_id, handoff_id, role)):
            receipt = json.loads(raw)
            if hashlib.sha256(encoded(receipt['call']).encode()).hexdigest() != receipt['call_sha256']:
                raise ResearchBlocked('Read completion call fingerprint changed')
            candidates[receipt['call_sha256']].append(receipt)
    fields = ('document_id', 'sha256', 'section_id', 'char_start', 'char_end', 'text', 'offset', 'total_chars', 'next_offset')
    for row in rows:
        if row.get('predecessor_handoff_id'):
            continue
        call, page = row.get('call', {}), row.get('result', {})
        if call.get('name') != 'documents.read' or not isinstance(page, dict) or page.get('error') or not page.get('text'):
            continue
        args = call.get('arguments', {})
        doc = allowed.get(args.get('document_id'))
        if not doc or doc['sha256'] != args.get('expected_sha256'):
            raise ResearchBlocked('Reading predecessor receipt has foreign input identity')
        expected = read(engine, run_id, args)  # Normal version-1 offset/limit defaults and exact end.
        if any(page.get(k) != expected.get(k) for k in fields):
            raise ResearchBlocked('Reading predecessor differs from the exact original request range')
        identity = hashlib.sha256(encoded(call).encode()).hexdigest()
        matched = False
        for index, receipt in enumerate(candidates[identity]):
            archive = engine.store.get('execution_archives', receipt.get('archive_id'), {})
            if archive.get('handoff_id') != handoff_id or archive.get('invocation_id') != receipt['id']:
                raise ResearchBlocked('Read completion archive belongs to a different handoff')
            actual = load(engine.store, run_id, role, receipt['archive_id'], receipt['archive_sha256'])
            if actual.get('call') == call and all(actual.get('result', {}).get(k) == page.get(k) for k in fields):
                candidates[identity].pop(index)
                matched = True
                break
        if not matched:
            raise ResearchBlocked('Read ledger lacks its own exact completed invocation receipt')
