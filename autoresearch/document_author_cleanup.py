"""Archive and clear only an authenticated unfinished author's owned material.

Called by the Harness after capability acceptance and successor freezing.
Original evidence, successful records, native transcripts and billing remain.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import zipfile

from .document_read_convergence import digest
from .storage import now

CHUNK = 1024 * 1024


def _plain(engine, path):
    from .deletion import _plain_path
    path = Path(path).absolute()
    _plain_path(path, engine.data_dir)
    if not path.resolve().is_relative_to(engine.data_dir.resolve()):
        raise ValueError('Author cleanup path escapes application data')
    return path


def _verify_file(path, expected):
    from .engine import ResearchBlocked
    try:
        with path.open('rb') as source:
            if (os.fstat(source.fileno()).st_size != expected['bytes']
                    or hashlib.file_digest(source, 'sha256').hexdigest() != expected['sha256']):
                raise ResearchBlocked('Author artifact changed after archive; cleanup halted')
    except FileNotFoundError as error:
        raise ResearchBlocked('Author artifact is missing after archive reconciliation') from error


def _restore_file(engine, check, path, expected, archive, index, *, resolve_file=None):
    """Publish verified original bytes without replacing an existing file."""
    from .engine import ResearchBlocked
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='.author-restore-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as output, archive.open(f'files/{index}') as source:
            while block := source.read(CHUNK):
                check()
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        _verify_file(temporary, expected)
        check()
        try:
            os.link(temporary, resolve_file(expected) if resolve_file else _plain(engine, path))
        except FileExistsError:
            # Another consumer may have restored the exact bytes. A different
            # version stays untouched and prevents the cleanup from committing.
            _verify_file(resolve_file(expected) if resolve_file else _plain(engine, path), expected)
            return False
        except OSError as error:
            raise ResearchBlocked('Cannot safely restore the shared author artifact') from error
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _reconcile_files(engine, target, archive_sha, manifest, active_scope, *, check, resolve_file=None):
    """Restore any retained file lost before a prior SQLite transaction rolled back."""
    from .engine import ResearchBlocked
    deleting = {f['artifact_id'] for f in active_scope['files']}
    restored = []
    with target.open('rb') as original:
        if hashlib.file_digest(original, 'sha256').hexdigest() != archive_sha:
            raise ResearchBlocked('Author archive changed before artifact reconciliation')
        with zipfile.ZipFile(original) as archive:
            for index, f in enumerate(manifest['files']):
                check()
                path = resolve_file(f) if resolve_file else _plain(engine, f['path'])
                if path.exists():
                    _verify_file(path, f)
                elif f['artifact_id'] not in deleting:
                    if _restore_file(engine, check, path, f, archive, index, resolve_file=resolve_file):
                        restored.append(f['artifact_id'])
                if f['artifact_id'] not in deleting:
                    _verify_file(path, f)
    return restored


def inventory(engine, run_id, source_id, audit):
    from .engine import ResearchBlocked
    if (audit['run_id'] != run_id or digest(audit['packet']) != audit['sha256']
            or audit['packet']['handoff']['id'] != source_id
            or engine.store.get('decisions', source_id)):
        raise ResearchBlocked('Only an authenticated unfinished author can be cleared')
    references = {('handoff_inputs', source_id), ('handoff_progress', source_id)}
    documents, artifacts, files = set(), set(), []
    from .document_working_notes import render
    for doc in engine.store.list('memory_documents', run_id):
        if doc.get('document_kind') != 'document_working_notes':
            continue
        artifact = engine.store.get('artifacts', doc.get('artifact_id', ''), {})
        path = _plain(engine, artifact.get('path', ''))
        text = path.read_text(encoding='utf-8')
        if hashlib.sha256(text.encode()).hexdigest() != doc['text_sha256']:
            raise ResearchBlocked('Author note version changed before cleanup')
        payload = json.loads(text.split('```json\n', 1)[1].rsplit('\n```\n', 1)[0])
        if payload.get('handoff_id') != source_id:
            continue
        if payload.get('run_id') != run_id or payload.get('role') != 'engineer' or text != render(payload):
            raise ResearchBlocked('Author note ownership does not match its original payload')
        documents.add(doc['id'])
        for aid in doc.get('artifact_ids') or [doc['artifact_id']]:
            a = engine.store.get('artifacts', aid, {})
            if a.get('run_id') != run_id or a.get('sha256') != doc['text_sha256']:
                raise ResearchBlocked('A shared note alias cannot be cleared')
            artifacts.add(aid)
    from .implementation_authoring import COLLECTION, _checked
    draft = engine.store.get(COLLECTION, source_id)
    if draft:
        _checked(engine, run_id, draft)
        if draft['row'].get('approved'):
            raise ResearchBlocked('An approved document cannot be cleared as an unfinished draft')
        documents.add(draft['row']['document']['document_id'])
        artifacts.add(draft['row']['document']['artifact_id'])
        references.add((COLLECTION, source_id))
    canonical_chunks = set()
    for did in documents:
        canonical_chunks.update(engine.store.get('memory_documents', did, {}).get('chunk_ids', []))
    references.update(('memory_documents', did) for did in documents)
    references.update(('artifacts', aid) for aid in artifacts)
    with engine.store._read_db() as db:
        for col in ('memory', 'context_commits'):
            for key, raw in db.execute("SELECT key,data FROM records WHERE collection=? AND json_extract(data,'$.run_id')=?",
                                       (col, run_id)):
                row = json.loads(raw)
                owner = row.get('handoff_id') or row.get('metadata', {}).get('handoff_id')
                if (owner == source_id or col == 'memory' and key in canonical_chunks and owner in {None, source_id}):
                    references.add((col, key))
        snapshots = {row[0] for row in db.execute("SELECT json_extract(data,'$.snapshot_id') FROM records "
            "WHERE collection='context_manifests' AND json_extract(data,'$.run_id')=? "
            "AND json_extract(data,'$.handoff_id')=?", (run_id, source_id)) if row[0]}
        snapshots = {sid for sid in snapshots if not db.execute(
            "SELECT 1 FROM records WHERE collection='context_manifests' AND json_extract(data,'$.snapshot_id')=? "
            "AND coalesce(json_extract(data,'$.handoff_id'),json_extract(data,'$.operation_id'),'')<>? LIMIT 1",
            (sid, source_id)).fetchone()}
        for key, raw in db.execute("SELECT key,data FROM records WHERE collection='contexts' AND json_extract(data,'$.run_id')=?", (run_id,)):
            row = json.loads(raw)
            if row.get('snapshot_id') in snapshots and row.get('role') == audit['packet']['handoff']['to_role']:
                references.add(('contexts', key))
        for sid in snapshots:
            row = db.execute("SELECT json_extract(data,'$.run_id') FROM records WHERE collection='context_snapshots' AND key=?", (sid,)).fetchone()
            if row and row[0] == run_id:
                references.add(('context_snapshots', sid))
            tree = db.execute("SELECT json_extract(data,'$.run_id'),json_extract(data,'$.snapshot_id') FROM records "
                              "WHERE collection='memory_trees' AND key=?", (sid,)).fetchone()
            if tree and tuple(tree) == (run_id, sid):
                references.add(('memory_trees', sid))
    for aid in sorted(artifacts):
        a = engine.store.get('artifacts', aid)
        path = _plain(engine, a['path'])
        if not path.resolve().is_relative_to(engine.run_dir(run_id).resolve()):
            raise ResearchBlocked('Author artifact is outside its run')
        with path.open('rb') as source:
            raw_sha = hashlib.file_digest(source, 'sha256').hexdigest()
        files.append({'artifact_id': aid, 'path': str(path), 'sha256': raw_sha,
            'artifact_text_sha256': a['sha256'], 'bytes': path.stat().st_size})
    from .document_ownership import freeze
    return freeze(engine, run_id, source_id, references, files)


def clear(engine, run_id, source_id, audit, successor_id):
    from .engine import ResearchBlocked
    from .record_archive import fingerprint
    key = source_id + ':author-cleanup:' + audit['sha256']
    previous = engine.store.get('document_author_cleanups', key)
    if previous and (previous.get('run_id') != run_id or previous.get('source_handoff_id') != source_id
            or previous.get('source_audit_id') != audit['id'] or previous.get('successor_id') != successor_id):
        raise ResearchBlocked('Author cleanup receipt has a different source, audit or successor')
    if previous and previous['status'] == 'completed':
        with _plain(engine, previous['archive_path']).open('rb') as source:
            if hashlib.file_digest(source, 'sha256').hexdigest() != previous['archive_sha256']:
                raise ResearchBlocked('Completed author cleanup archive changed')
        return previous
    child = engine.store.get('handoff_inputs', successor_id, {})
    binding = child.get('context', {}).get('document_efficiency_recovery', {})
    if (child.get('run_id') != run_id or binding.get('source_handoff_id') != source_id
            or binding.get('source_audit_sha256') != audit['sha256']):
        raise ResearchBlocked('Freeze the ordinary successor before clearing author context')
    if previous:
        manifest = previous['inventory']
    else:
        manifest = inventory(engine, run_id, source_id, audit)
    target = _plain(engine, engine.run_dir(run_id) / 'author-archives' / (digest(manifest) + '.zip'))
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        temporary = _plain(engine, target.with_suffix('.pending'))
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
            with engine.store._read_db() as db:
                for ref in manifest['records']:
                    engine.check_active(run_id)
                    current = fingerprint(engine.store, ref['collection'], ref['key'])
                    if current != ref:
                        raise ResearchBlocked('Author record changed before immutable archive')
                    rowid = db.execute('SELECT rowid FROM records WHERE collection=? AND key=?', (ref['collection'], ref['key'])).fetchone()[0]
                    with db.blobopen('records', 'data', rowid, readonly=True) as source, archive.open('records/' + digest([ref['collection'], ref['key']]) + '.json', 'w', force_zip64=True) as out:
                        while block := source.read(CHUNK):
                            out.write(block)
            for index, f in enumerate(manifest['files']):
                engine.check_active(run_id)
                archive.write(_plain(engine, f['path']), 'files/' + str(index))
            archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, sort_keys=True))
        temporary.replace(target)
    # Verify each archived source before deleting any active material.
    with zipfile.ZipFile(target) as archive:
        if json.loads(archive.read('manifest.json')) != manifest:
            raise ResearchBlocked('Author archive manifest changed')
        for ref in manifest['records']:
            hashed, count = hashlib.sha256(), 0
            with archive.open('records/' + digest([ref['collection'], ref['key']]) + '.json') as source:
                while block := source.read(CHUNK):
                    count += len(block)
                    hashed.update(block)
            if count != ref['bytes'] or hashed.hexdigest() != ref['sha256']:
                raise ResearchBlocked('Author archive does not preserve original record bytes')
        for index, f in enumerate(manifest['files']):
            with archive.open('files/' + str(index)) as source:
                hashed = hashlib.file_digest(source, 'sha256').hexdigest()
            if hashed != f['sha256']:
                raise ResearchBlocked('Author archive does not preserve original artifact bytes')
    with target.open('rb') as source:
        archive_sha = hashlib.file_digest(source, 'sha256').hexdigest()
    if previous and previous['archive_sha256'] != archive_sha:
        raise ResearchBlocked('Author archive changed since its durable cleanup receipt')
    receipt = previous or {'id': key, 'run_id': run_id, 'source_handoff_id': source_id,
        'source_audit_id': audit['id'], 'successor_id': successor_id, 'inventory': manifest,
        'archive_path': str(target), 'archive_sha256': archive_sha,
        'rollback_boundary': 'approved_architecture_and_frozen_experiment_plan', 'created_at': now()}
    # Record the verified archive before filesystem deletion, making partial
    # cleanup replayable without selecting a different target.
    engine.store.put('document_author_cleanups', key, {**receipt, 'status': 'archived'})
    with engine.store._db() as db:
        db.execute('BEGIN IMMEDIATE')
        engine.store._guard_run(db, run_id)
        from .document_ownership import deletion_scope
        active_scope = deletion_scope(db, run_id, source_id, manifest)
        for ref in manifest['records']:
            engine.check_active(run_id)
            row = db.execute('SELECT updated_at,length(CAST(data AS BLOB)) FROM records WHERE collection=? AND key=?',
                             (ref['collection'], ref['key'])).fetchone()
            if not row or tuple(row) != (ref['updated_at'], ref['bytes']):
                raise ResearchBlocked('Author context changed after archive; cleanup halted')
            rowid = db.execute('SELECT rowid FROM records WHERE collection=? AND key=?',
                (ref['collection'], ref['key'])).fetchone()[0]
            with db.blobopen('records', 'data', rowid, readonly=True) as source:
                hashed = hashlib.sha256()
                while block := source.read(CHUNK):
                    hashed.update(block)
                if hashed.hexdigest() != ref['sha256']:
                    raise ResearchBlocked('Author context bytes changed after archive')
        restored = _reconcile_files(engine, target, archive_sha, manifest, active_scope,
            check=lambda: engine.check_active(run_id))
        for f in active_scope['files']:
            engine.check_active(run_id)
            path = _plain(engine, f['path'])
            if path.exists():
                _verify_file(path, f)
                path.unlink()
        for ref in active_scope['records']:
            engine.check_active(run_id)
            db.execute('DELETE FROM records WHERE collection=? AND key=?', (ref['collection'], ref['key']))
            if ref['collection'] == 'memory':
                db.execute('DELETE FROM memory_terms WHERE memory_id=?', (ref['key'],))
        receipt.update(status='completed', completed_at=now(), deletion_scope=active_scope,
            restored_shared_artifacts=restored)
        db.execute('INSERT OR REPLACE INTO records(collection,key,data,updated_at) VALUES(?,?,?,?)',
            ('document_author_cleanups', key, json.dumps(receipt, ensure_ascii=False), now()))
    engine.store.event(run_id, 'document_efficiency.author_cleared', 'Harness 已归档并清空旧文档撰写记忆、上下文和未完成产物',
        {'cleanup_id': key, 'source_handoff_id': source_id, 'successor_id': successor_id,
         'records': len(active_scope['records']), 'files': len(active_scope['files']),
         'retained_shared_records': len(active_scope['retained_shared_records']),
         'archive_sha256': receipt['archive_sha256']})
    return receipt
