"""Storage dependency protection for an exact author cleanup candidate set.

Original receipts are history, not active ownership. Other consumers retain
their reachable records and complete readable document/index/file bundles.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import ntpath
import os

ACTIVE = ('handoffs', 'handoff_inputs', 'handoff_progress', 'contexts', 'context_snapshots',
    'context_manifests', 'context_documents', 'memory_documents', 'artifacts', 'memory',
    'context_commits', 'memory_trees', 'decisions', 'steps', 'consensus', 'stage_contexts',
    'research_states', 'research_state_sources', 'literature_notes', 'experiment_notes',
    'independent_full_review_notes')
FORCED = {'handoff_inputs', 'handoff_progress', 'implementation_acceptance_budgets'}


def token(value):
    if len(value) < 32768 and (ntpath.isabs(value) or os.path.isabs(value)):
        return os.path.normcase(os.path.abspath(value))
    return value


def matches(value, wanted, found, field=None):
    if isinstance(value, str):
        key = token(value)
        if key in wanted:
            found.update(ref for ref in wanted[key] if not (
                ref[0] == 'implementation_document_drafts' and field in {'source_handoff_id', 'handoff_id', 'operation_id'}))
        if value.lstrip().startswith(('{', '[')):
            try:
                nested = json.loads(value)
            except (ValueError, RecursionError):
                return
            matches(nested, wanted, found)
    elif isinstance(value, dict):
        for key, child in value.items():
            matches(key, wanted, found)
            matches(child, wanted, found, key)
    elif isinstance(value, list):
        for child in value:
            matches(child, wanted, found)


def targets(references, files):
    wanted = defaultdict(set)
    for ref in references:
        if ref[0] not in FORCED:
            wanted[token(ref[1])].add(ref)
    for f in files:
        ref = ('artifacts', f['artifact_id'])
        wanted[token(f['path'])].add(ref)
    return wanted


def graph(db, references, files):
    wanted = targets(references, files)
    edges, hashes, documents = {}, {}, []
    for ref in sorted(references):
        if ref[0] in FORCED:
            continue
        raw = db.execute('SELECT data FROM records WHERE collection=? AND key=?', ref).fetchone()
        if not raw:
            continue
        value = json.loads(raw[0])
        found = set()
        matches(value, wanted, found)
        edges[ref] = found
        hashes[ref] = hashlib.sha256(raw[0].encode('utf-8')).hexdigest()
        if ref[0] == 'memory_documents':
            documents.append((ref, value))
    # A file alias or canonical chunk must keep the complete readable bundle,
    # including its document manifest and all necessary index records.
    for ref, doc in documents:
        bundle = {ref}
        bundle.update(('memory', key) for key in doc.get('chunk_ids', []))
        bundle.update(('artifacts', key) for key in doc.get('artifact_ids') or [doc.get('artifact_id')])
        bundle &= references
        for member in bundle:
            edges.setdefault(member, set()).update(bundle)
    return [{'record': list(ref), 'sha256': hashes.get(ref), 'dependencies': [list(v) for v in sorted(values)]}
            for ref, values in sorted(edges.items())]


def protected(db, run_id, source_id, references, files, dependency_graph, *, archived_handoffs=(), archived_snapshots=()):
    wanted = targets(references, files)
    if not wanted:
        return set()
    result = set()
    excluded = set(archived_handoffs)
    if source_id:
        excluded.add(source_id)
    for collection in ACTIVE:
        query = "SELECT key,data FROM records WHERE collection=? AND json_extract(data,'$.run_id')=?"
        args = (collection, run_id)
        if collection == 'context_snapshots' and archived_snapshots:
            query += ' AND key NOT IN (SELECT value FROM json_each(?))'
            args += (json.dumps(sorted(archived_snapshots)),)
        for key, raw in db.execute(query, args):
            if (collection, key) in references:
                continue
            if key in excluded and collection in {'handoffs', 'decisions', *FORCED}:
                continue
            row = json.loads(raw)
            if collection == 'context_manifests' and (row.get('handoff_id') or row.get('operation_id')) in excluded:
                continue  # Immutable source request receipt; retained separately.
            if collection == 'stage_contexts' and (':document_efficiency:' in key or key.endswith(':save-predecessor:v1')):
                continue  # Immutable code work orders and normal-save audits.
            matches(row, wanted, result)
    edges = {tuple(row['record']): {tuple(ref) for ref in row['dependencies']} for row in dependency_graph}
    pending = list(result)
    while pending:
        ref = pending.pop()
        for dependency in edges.get(ref, ()):
            if dependency not in result:
                result.add(dependency)
                pending.append(dependency)
    return result & (references | {('artifacts', file['artifact_id']) for file in files})


def freeze(engine, run_id, source_id, references, files, *, archived_handoffs=(), archived_snapshots=()):
    from .record_archive import fingerprint
    from .engine import ResearchBlocked
    with engine.store._read_db() as db:
        dependency_graph = graph(db, references, files)
        retained = protected(db, run_id, source_id, references, files, dependency_graph,
            archived_handoffs=archived_handoffs, archived_snapshots=archived_snapshots)
    records = [fingerprint(engine.store, *ref) for ref in sorted(references)]
    records = [row for row in records if row]
    by_ref = {(row['collection'], row['key']): row for row in records}
    for node in dependency_graph:
        ref = tuple(node['record'])
        if ref in by_ref and by_ref[ref]['sha256'] != node['sha256']:
            raise ResearchBlocked('Author storage changed during dependency ownership freeze')
    return {'records': records, 'files': files, 'dependency_graph': dependency_graph,
        'retained_shared_records': [list(ref) for ref in sorted(retained)]}


def deletion_scope(db, run_id, source_id, manifest, *, archived_handoffs=(), archived_snapshots=()):
    refs = {(row['collection'], row['key']) for row in manifest['records']}
    retained = protected(db, run_id, source_id, refs, manifest['files'], manifest['dependency_graph'],
        archived_handoffs=archived_handoffs, archived_snapshots=archived_snapshots)
    retained.update(tuple(ref) for ref in manifest['retained_shared_records'])
    return {'records': [row for row in manifest['records'] if (row['collection'], row['key']) not in retained],
        'files': [f for f in manifest['files'] if ('artifacts', f['artifact_id']) not in retained],
        'retained_shared_records': [list(ref) for ref in sorted(retained)]}
