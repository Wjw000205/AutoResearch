"""Archive and remove working data for an authorized pre-code stage restart."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import zipfile

from .document_author_cleanup import CHUNK, _plain, _reconcile_files, _verify_file
from .document_read_convergence import digest
from .storage import now

SCOPE = 'restart_implementation_stage'
TERMINAL = {'completed', 'failed', 'interrupted', 'cancelled'}
DERIVED_KINDS = {'implementation_specification', 'implementation_review_response',
    'implementation_revision_diff', 'implementation_review_history', 'host_implementation_contract',
    'task_document_original', 'task_document_projection', 'task_document_brief', 'task_document_definitions'}


def approval(engine, db, run_id, *, required=False):
    """Read-only authorization check. Revalidate the exact reviewed source."""
    from .implementation_restart import _get, _rows, _fail
    from .document_efficiency_recovery import KIND, sources, validate_approvals
    requests = [key for key, row in _rows(db, 'harness_capability_requests', run_id)
        if row.get('status') == 'accepted' and row.get('capability') == KIND
        and row.get('operator_payload', {}).get('scope') == SCOPE]
    if not requests:
        if required:
            _fail('整阶段回退缺少对应的操作请求。')
        return None
    run = _get(db, 'runs', run_id, {})
    previous = _get(db, 'implementation_restarts', run.get('state', {}).get('implementation_restart'), {})
    pending = sorted(set(requests) - set(previous.get('capability_request_ids', [])))
    if not pending:
        if required:
            _fail('整阶段回退要求已经应用。')
        return None
    cert = _get(db, 'harness_capabilities', KIND, {})
    work = _get(db, 'stage_contexts', cert.get('work_order_id'), {})
    reviewed = {row['id']: row for row in work.get('operator_requests', [])}
    if (cert.get('status') != 'accepted' or work.get('run_id') != run_id
            or cert.get('capability_sha256') != work.get('source', {}).get('capability_sha256')
            or digest(sources()) != cert.get('capability_sha256')
            or any(reviewed.get(key) != _get(db, 'harness_capability_requests', key) for key in pending)):
        _fail('整阶段回退须先通过绑定当前请求和源码的独立评审。')
    validate_approvals(engine, run_id, work, work['source'])
    return {'work_order_id': work['id'], 'request_ids': pending}


def settlement_guard(db, run_id, handoff_ids):
    from .implementation_restart import _fail, _get, _rows
    owners = set(handoff_ids)
    for _, row in _rows(db, 'codex_reasoning', run_id):
        owner = (row.get('memory_capture_binding') or {}).get('handoff_id')
        if owner in owners and (row.get('checkpoint') or {}).get('status') not in TERMINAL:
            _fail('实现阶段仍有未结的原生请求，不能回退。')
    for _, row in _rows(db, 'tool_invocations', run_id):
        if row.get('handoff_id') in owners and row.get('status') in {'running', 'unknown'}:
            _fail('实现阶段仍有未结的工具调用，不能回退。')
    budget = _get(db, 'budgets', run_id, {})
    if budget.get('pending'):
        _fail('仍有待结算的模型调用，不能回退实现阶段。')
    for key in owners:
        row = _get(db, 'handoff_progress', key, {})
        calls = row.get('pending_tool_calls', [])[row.get('tool_cursor', 0):]
        if any(call.get('name') not in {'documents.read', 'documents.search', 'documents.list'} for call in calls):
            _fail('实现阶段仍有已接收的写入意图，须完成对账再回退。')


def inventory(engine, db, run_id, handoff_ids, active_records, *, code_files=None):
    """Bind only working objects owned by the retired implementation stage."""
    from .document_ownership import graph, protected
    from .document_working_notes import render
    from .implementation_restart import _fail, _get, _rows
    owners = set(handoff_ids)
    if code_files is None:
        settlement_guard(db, run_id, owners)
    refs = {(row['collection'], row['key']) for row in active_records}
    if code_files is None:
        refs.add(('implementation_acceptance_budgets', run_id))
    refs = {ref for ref in refs if _get(db, *ref) is not None}
    artifacts = {}
    for key, row in _rows(db, 'artifacts', run_id):
        name = row.get('name', '')
        if code_files is not None:
            if row.get('path') in code_files:
                artifacts[key] = row
            continue
        if (row.get('kind') in DERIVED_KINDS
                or row.get('kind') == 'frozen_experiment_plan'
                and any(part in '/' + name for part in ('/implementation/plans/', '/task-documents/'))
                or row.get('kind') in {'stage_report', 'stage_results'} and name in {'blocked-report.md', 'blocked-results.json'}):
            artifacts[key] = row
    docs = {}
    for key, row in _rows(db, 'memory_documents', run_id):
        aliases = set(row.get('artifact_ids') or [row.get('artifact_id')])
        if row.get('document_kind') == 'document_working_notes':
            artifact = _get(db, 'artifacts', row.get('artifact_id'))
            if not artifact:
                _fail('工作笔记缺少原始文件绑定。')
            path = _plain(engine, artifact['path'])
            text = path.read_text(encoding='utf-8')
            payload = json.loads(text.split('```json\n', 1)[1].rsplit('\n```\n', 1)[0])
            if payload.get('handoff_id') not in owners:
                continue
            if (payload.get('run_id') != run_id or text != render(payload)
                    or hashlib.sha256(text.encode()).hexdigest() != row['text_sha256']):
                _fail('实现阶段工作笔记与原始归属不一致。')
            for alias in aliases:
                source = _get(db, 'artifacts', alias, {})
                if source.get('run_id') != run_id or source.get('sha256') != row['text_sha256']:
                    _fail('实现阶段工作笔记的文件别名不匹配。')
                artifacts[alias] = source
        if aliases and aliases <= artifacts.keys():
            docs[key] = row
    for key, row in _rows(db, 'implementation_document_drafts', run_id):
        if key in owners:
            refs.add(('implementation_document_drafts', key))
    for collection in ('document_author_cursors', 'document_recovery_assignments'):
        for key, row in _rows(db, collection, run_id):
            if (row.get('handoff_id') in owners or row.get('source_handoff_id') in owners
                    or (row.get('assignment') or {}).get('context', {}).get('document_efficiency_recovery', {}).get('source_handoff_id') in owners):
                refs.add((collection, key))
    refs.update(('memory_documents', key) for key in docs)
    chunks = {chunk for row in docs.values() for chunk in row.get('chunk_ids', [])}
    for collection in ('memory', 'context_commits'):
        for key, row in _rows(db, collection, run_id):
            owner = row.get('handoff_id') or (row.get('metadata') or {}).get('handoff_id')
            if owner in owners or collection == 'memory' and key in chunks and owner is None:
                refs.add((collection, key))
    snapshots = {}
    for _, row in _rows(db, 'context_manifests', run_id):
        if row.get('snapshot_id'):
            snapshots.setdefault(row['snapshot_id'], set()).add(row.get('handoff_id') or row.get('operation_id'))
    for key, references in snapshots.items():
        if references <= owners:
            for collection in ('context_snapshots', 'memory_trees'):
                row = _get(db, collection, key, {})
                if row.get('run_id') == run_id:
                    refs.add((collection, key))
    files = {row['path']: row for row in code_files.values()} if code_files is not None else {}
    for key, row in sorted(artifacts.items()):
        path = _plain(engine, row['path'])
        if not path.is_file() or not path.is_relative_to(engine.run_dir(run_id).resolve()):
            _fail('实现阶段产物缺失或不属于本任务。')
        with path.open('rb') as source:
            raw = hashlib.file_digest(source, 'sha256').hexdigest()
        canonical = hashlib.sha256(path.read_text(encoding='utf-8').encode()).hexdigest() if path.suffix in {'.md', '.json'} else raw
        if row.get('sha256') not in {raw, canonical}:
            _fail('实现阶段产物与已保存的版本不一致。')
        files[str(path)] = {**files.get(str(path), {}), 'artifact_id': key, 'path': str(path),
                            'bytes': path.stat().st_size, 'sha256': raw}
        refs.add(('artifacts', key))
    files = list(files.values())
    dependency_graph = graph(db, refs, files)
    retained = protected(db, run_id, None, refs, files, dependency_graph, archived_handoffs=owners)
    records = []
    for collection, key in sorted(refs):
        row = db.execute('SELECT rowid,updated_at FROM records WHERE collection=? AND key=?', (collection, key)).fetchone()
        hashed = hashlib.sha256()
        with db.blobopen('records', 'data', row['rowid'], readonly=True) as stream:
            size = len(stream)
            while block := stream.read(CHUNK):
                hashed.update(block)
        records.append({'collection': collection, 'key': key, 'updated_at': row['updated_at'],
            'sha256': hashed.hexdigest(), 'bytes': size, 'encoding': 'utf-8-json'})
    manifest = {'records': records, 'files': files, 'dependency_graph': dependency_graph,
        'retained_shared_records': [list(ref) for ref in sorted(retained)]}
    manifest['archived_handoff_ids'] = sorted(owners)
    return manifest


def _verify_entry(stream, ref):
    from .implementation_restart import _fail
    hashed, count = hashlib.sha256(), 0
    while block := stream.read(CHUNK):
        count += len(block)
        hashed.update(block)
    if count != ref['bytes'] or hashed.hexdigest() != ref['sha256']:
        _fail('实现阶段归档的原始字节校验失败。')


def _members(manifest):
    for ref in manifest['records']:
        yield 'records/' + digest([ref['collection'], ref['key']]) + '.json', ref, True
    for index, ref in enumerate(manifest['files']):
        yield f'files/{index}', ref, False


def _archive(engine, db, target, manifest, *, resolve_file=None):
    from .implementation_restart import _fail
    temporary = _plain(engine, target.with_suffix('.pending'))
    if not target.exists():
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
            for name, ref, is_record in _members(manifest):
                if not is_record:
                    path = resolve_file(ref) if resolve_file else _plain(engine, ref['path'])
                    archive.write(path, name)
                    continue
                rowid = db.execute('SELECT rowid FROM records WHERE collection=? AND key=?',
                    (ref['collection'], ref['key'])).fetchone()[0]
                with db.blobopen('records', 'data', rowid, readonly=True) as source, archive.open(name, 'w', force_zip64=True) as output:
                    while block := source.read(CHUNK):
                        output.write(block)
            archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, sort_keys=True))
        with temporary.open('ab') as output:
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(target)
    return verify_archive(target, manifest)


def verify_archive(target, manifest):
    from .implementation_restart import _fail
    if not target.is_file():
        _fail('已准备的实现阶段工作归档缺失，不能继续删除。')
    with zipfile.ZipFile(target) as archive:
        if json.loads(archive.read('manifest.json')) != manifest:
            _fail('实现阶段归档清单发生变化。')
        for name, ref, _ in _members(manifest):
            with archive.open(name) as source:
                _verify_entry(source, ref)
    with target.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def stage_archive_path(engine, snapshot_path):
    return _plain(engine, snapshot_path.with_name('working-data.zip'))


def archive_working_data(engine, db, snapshot_path, manifest, *, replay=False, resolve_file=None):
    target = stage_archive_path(engine, snapshot_path)
    sha = verify_archive(target, manifest) if replay else _archive(engine, db, target, manifest, resolve_file=resolve_file)
    return {'path': str(target), 'sha256': sha}


def apply(engine, db, run_id, snapshot_path, archive, manifest, *, resolve_file=None):
    from .document_ownership import deletion_scope
    from .implementation_restart import _fail
    active = deletion_scope(db, run_id, None, manifest, archived_handoffs=manifest['archived_handoff_ids'])
    target = stage_archive_path(engine, snapshot_path)
    if archive.get('path') != str(target):
        _fail('实现阶段归档路径与原任务不符。')
    for ref in manifest['records']:
        row = db.execute('SELECT rowid,updated_at FROM records WHERE collection=? AND key=?',
            (ref['collection'], ref['key'])).fetchone()
        if not row or row['updated_at'] != ref['updated_at']:
            _fail('实现阶段记录在归档后发生变化。')
        with db.blobopen('records', 'data', row['rowid'], readonly=True) as body:
            _verify_entry(body, ref)
    restored = _reconcile_files(engine, target, archive['sha256'], manifest, active, check=lambda: None,
                                resolve_file=resolve_file)
    for ref in active['files']:
        path = resolve_file(ref) if resolve_file else _plain(engine, ref['path'])
        if path.exists():
            _verify_file(path, ref)
            path.unlink()
    for ref in active['records']:
        db.execute('DELETE FROM records WHERE collection=? AND key=?', (ref['collection'], ref['key']))
        if ref['collection'] == 'memory':
            db.execute('DELETE FROM memory_terms WHERE memory_id=?', (ref['key'],))
    return {'archive': copy.deepcopy(archive), 'deleted_records': len(active['records']),
        'deleted_artifacts': [ref['artifact_id'] for ref in active['files']],
        'retained_shared_records': len(active['retained_shared_records']),
        'restored_shared_artifacts': restored, 'status': 'completed'}
