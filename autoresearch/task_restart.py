"""Explicit, scoped restart of a failed Harness assignment, for any role/stage.

Preview binds exact records and owned files. Apply archives them, clears active
context/output, and installs a fresh assignment at its owner's existing cursor.
Billing, events, sources, notes, experiments and other assignments are untouched.
The ordinary Harness performs dispatch and all budget/resource checks on resume.
"""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import gzip
import hashlib
import json
from pathlib import Path
import shutil

from .deletion import _plain_path
from .delivery_files import io_path
from .storage import now, uid

COLLECTIONS = frozenset({'handoffs', 'handoff_inputs', 'handoff_progress', 'decisions',
    'codex_reasoning', 'transcripts', 'context_snapshots', 'context_manifests',
    'context_commits', 'context_receipts', 'context_documents', 'contexts', 'context_evidence',
    'execution_archives', 'innovation_readings', 'tool_invocations', 'tool_tasks', 'response_cache',
    'memory_trees', 'memory', 'memory_documents', 'memory_chunks'})
GUIDANCE = ('\n本次为原失败工作项的全新调度，原失败回复和工作上下文已退出活动状态。'
    '仅依据当前任务输入、已有成功交付及可复核的源文件重新完成指定工作，不重复生成合格笔记。'
    '目录和历史摘要属于导航记录；涉及日期、检索命中或方法内容时，读取对应归档字段或原文并保留定位。'
    '不得将目录引用升级为原文阅读、科学结论或执行授权。')


def _digest(value):
    from .engine import digest
    return digest(value)


def prepare_failed(harness, run_id, *, reason):
    """Resolve a declared failure owner, never guess from the latest timestamp."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('Explicit restart reason required')
    engine = harness.engine
    from . import architecture_review_restart as document_review
    prepared = document_review.prepared(engine, run_id)
    if prepared:
        return prepared
    if document_review.current(engine, run_id):
        return document_review.prepare(harness, run_id, reason)
    candidates = []
    for owner in engine.store.list('stage_contexts', run_id):
        if owner.get('status') == 'completed' or not (owner.get('evidence_issue') or {}).get('handoff_id'):
            continue
        for pointer, task in [('/assignment', owner.get('assignment')),
                *[('/assignments/' + role, task) for role, task in owner.get('assignments', {}).items()]]:
            if isinstance(task, dict) and all(k in task for k in ('purpose', 'instruction', 'context', 'schema')):
                hid = owner['evidence_issue']['handoff_id']
                handoff = engine.store.get('handoffs', hid) or {}
                from .engine import Harness
                if handoff.get('stage') == engine.store.get('runs', run_id)['stage'] and Harness.decision_key(
                        run_id, handoff.get('to_role'), **task) == hid:
                    candidates.append((hid, owner['id'], pointer))
    if len(candidates) != 1:
        raise ValueError('No unique declared failed task owner; nothing was cleared')
    hid, owner, pointer = candidates[0]
    return apply(engine, preview(engine, run_id, hid, owner, assignment_pointer=pointer,
                                clear_events=True), reason=reason)


def register(app, engine, task_runtime, require_run):
    from fastapi import Body
    from .async_ownership import to_thread

    @app.post('/api/runs/{run_id}/failed-task-restarts')
    @task_runtime.control
    async def restart_failed_task(run_id: str, payload: dict = Body(...)):
        require_run(run_id)
        if set(payload) != {'reason'}:
            raise ValueError('Restart accepts an explicit reason; the Harness resolves its own failure owner')
        from .engine import Harness
        async with engine._lifecycle_lock:
            return await to_thread(Harness(engine, None).restart_failed_task, run_id, reason=payload['reason'])


def _at(value, pointer):
    if not pointer.startswith('/'):
        raise ValueError('Owner pointer must be an absolute JSON pointer')
    for key in pointer[1:].split('/'):
        value = value[key.replace('~1', '/').replace('~0', '~')]
    return value


def _set(value, pointer, replacement, *, remove=False):
    parts = [k.replace('~1', '/').replace('~0', '~') for k in pointer[1:].split('/')]
    for key in parts[:-1]: value = value[key]
    if remove: value.pop(parts[-1])
    else: value[parts[-1]] = replacement


def _scope(engine, run_id, handoff_id, owner_key, assignment_pointer, failure_pointer):
    from .engine import Harness
    store = engine.store
    run = store.get('runs', run_id, {})
    handoff = store.get('handoffs', handoff_id, {})
    owner = store.get('stage_contexts', owner_key, {})
    try:
        assignment = _at(owner, assignment_pointer)
        failure = _at(owner, failure_pointer)
    except (KeyError, TypeError) as exc:
        raise ValueError('Failed assignment owner or failure pointer is missing; nothing cleared') from exc
    inputs = store.get('handoff_inputs', handoff_id, {})
    worker = store.get('runtime', 'worker', {})
    if (run.get('status') != 'blocked' or store.get('run_deletions', run_id)
            or not run.get('error') or owner.get('run_id') != run_id or owner.get('status') == 'completed'
            or handoff.get('run_id') != run_id or handoff.get('stage') != run.get('stage')
            or not isinstance(failure, dict) or failure.get('handoff_id') != handoff_id
            or worker.get('held') and worker.get('run_id') == run_id
            or not all(k in assignment for k in ('purpose', 'instruction', 'context', 'schema'))
            or handoff.get('purpose') != assignment['purpose']
            or Harness.decision_key(run_id, handoff.get('to_role'), **assignment) != handoff_id
            or any(inputs.get(k) != assignment[k] for k in ('instruction', 'context', 'schema'))):
        raise ValueError('An inactive blocked run and exact failed owner assignment are required')
    if any(d.get('handoff_id') == handoff_id for d in owner.get('deliveries', [])):
        raise ValueError('An accepted delivery cannot be cleared as a failed task')
    return run, handoff, owner, assignment


def _records(store, run_id, handoff_id, *, cancelled_native_ids=()):
    return owned_records(store, run_id, {handoff_id}, cancelled_native_ids=cancelled_native_ids)


def owned_records(store, run_id, handoff_ids, *, cancelled_native_ids=()):
    if not handoff_ids:
        return []
    selected = {}
    def query(predicate, values):
        placeholders = ','.join('?' for _ in COLLECTIONS)
        with store._read_db() as db:
            rows = db.execute(f'SELECT collection,key,data,updated_at FROM records '
                f'WHERE collection IN ({placeholders}) AND ({predicate})', (*COLLECTIONS, *values))
            for row in rows:
                item = dict(row); data = json.loads(item['data'])
                if data.get('run_id') != run_id:
                    raise ValueError('Task context ownership changed; nothing cleared')
                selected[(item['collection'], item['key'])] = item
    fields = ('key', "json_extract(data,'$.handoff_id')", "json_extract(data,'$.parent_handoff_id')",
              "json_extract(data,'$.task_id')", "json_extract(data,'$.memory_capture_binding.handoff_id')",
              "json_extract(data,'$.metadata.handoff_id')")
    placeholders = ','.join('?' for _ in handoff_ids)
    query(' OR '.join(f'{field} IN ({placeholders})' for field in fields), sorted(handoff_ids) * len(fields))
    ids, operations, requests, originals = set(), set(), set(), set()
    cancelled_reservations = set()
    while True:
        before = len(selected)
        for row in list(selected.values()):
            data = json.loads(row['data']); ids.add(row['key'])
            if data.get('operation_id'): operations.add(data['operation_id'])
            if data.get('request_id'): requests.add(data['request_id'])
            binding = data.get('memory_capture_binding', {})
            ids.update(binding[k] for k in ('context_snapshot_id', 'context_manifest_id', 'input_transcript_id') if binding.get(k))
            if binding.get('input_transcript_id'): originals.add(binding['input_transcript_id'])
            if data.get('original_transcript_id'): originals.add(data['original_transcript_id'])
            if row['collection'] == 'codex_reasoning':
                reservation = data.get('reservation', {})
                if reservation.get('id'):
                    requests.add(reservation['id'])
                checkpoint = data.get('checkpoint', {})
                if checkpoint.get('status') not in {'completed', 'failed', 'interrupted'}:
                    if (row['key'] not in cancelled_native_ids or data.get('result')
                            or checkpoint.get('status') != 'unknown'
                            or checkpoint.get('error_code') != 'codex_cancelled_result_unknown'
                            or data.get('memory_capture_binding', {}).get('handoff_id') not in handoff_ids
                            or not checkpoint.get('turn_dispatched_at') or not reservation.get('id')
                            or not data.get('operation_id')
                            or reservation.get('operation_id') != data['operation_id']):
                        raise ValueError('Native outcome is not terminal; reconcile before restart')
                    cancelled_reservations.add((reservation['id'], reservation['operation_id']))
            if row['collection'] == 'tool_invocations' and data.get('status') not in {'succeeded', 'failed'}:
                raise ValueError('Tool outcome is unresolved; reconcile before restart')
        predicates, values = [], []
        for field, wanted in [('key', ids), ("json_extract(data,'$.parent_handoff_id')", ids),
                ("json_extract(data,'$.handoff_id')", ids), ("json_extract(data,'$.operation_id')", operations),
                ("json_extract(data,'$.request_id')", requests),
                ("json_extract(data,'$.result.request_id')", requests),
                ("json_extract(data,'$.metadata.operation_id')", operations),
                ("json_extract(data,'$.document_id')", ids),
                ("json_extract(data,'$.snapshot_id')", ids),
                ("json_extract(data,'$.transcript_id')", ids),
                ("json_extract(data,'$.original_transcript_id')", originals)]:
            if wanted:
                predicates.append(field + ' IN (' + ','.join('?' for _ in wanted) + ')')
                values.extend(sorted(wanted))
        query(' OR '.join(predicates), values)
        if len(selected) == before: break
    ledger = store.get('budgets', run_id, {})
    if any((row.get('operation_id') in operations or row.get('id') in requests)
            and (row.get('id'), row.get('operation_id')) not in cancelled_reservations
            for row in ledger.get('pending', {}).values()):
        raise ValueError('Request accounting is unresolved; nothing cleared')
    return sorted(selected.values(), key=lambda r: (r['collection'], r['key']))


def _file_manifest(path, root):
    _plain_path(path, root)
    native = io_path(path)
    if native.is_file():
        return [{'path': '.', 'bytes': native.stat().st_size, 'sha256': hashlib.sha256(native.read_bytes()).hexdigest()}]
    files, pending = [], [path]
    while pending:
        directory = pending.pop()
        _plain_path(directory, root)
        for entry in io_path(directory).iterdir():
            item = directory / entry.name
            _plain_path(item, root)
            if entry.is_dir(): pending.append(item)
            else: files.append({'path': item.relative_to(path).as_posix(), 'bytes': entry.stat().st_size,
                'sha256': hashlib.sha256(entry.read_bytes()).hexdigest()})
    return sorted(files, key=lambda f: f['path'])


def _paths(engine, run_id, rows):
    root = engine.data_dir.resolve(); paths = set()
    for row in rows:
        data = json.loads(row['data'])
        if row['collection'] == 'codex_reasoning':
            path = root / 'codex-reasoning' / row['key']
        elif row['collection'] == 'execution_archives' and data.get('path'):
            path = Path(data['path'])
            if not path.absolute().is_relative_to(root / 'runs' / run_id / 'execution-archive'):
                raise ValueError('Execution output is outside its owned archive directory')
        else: continue
        _plain_path(path, root)
        if io_path(path).exists(): paths.add(path.absolute())
    return [{'path': str(p), 'files': _file_manifest(p, root)} for p in sorted(paths)]


def preview(engine, run_id, handoff_id, owner_key, *, assignment_pointer, failure_pointer='/evidence_issue',
            replacement_assignment=None, clear_events=False, cancelled_native_ids=(), controller_transition=None):
    run, handoff, owner, assignment = _scope(engine, run_id, handoff_id, owner_key, assignment_pointer, failure_pointer)
    cancelled_native_ids = sorted(set(cancelled_native_ids))
    rows = _records(engine.store, run_id, handoff_id, cancelled_native_ids=cancelled_native_ids)
    body = {'version': 1, 'run_id': run_id, 'handoff_id': handoff_id, 'owner_key': owner_key,
        'assignment_pointer': assignment_pointer, 'failure_pointer': failure_pointer,
        'run_sha256': _digest(run), 'owner_sha256': _digest(owner), 'assignment_sha256': _digest(assignment),
        'records': [{'collection': r['collection'], 'key': r['key'], 'updated_at': r['updated_at'],
            'sha256': hashlib.sha256(r['data'].encode()).hexdigest()} for r in rows],
        'paths': _paths(engine, run_id, rows),
        'preserved': ['billing', 'events', 'sources', 'reading notes', 'experiments', 'other assignments']}
    if cancelled_native_ids:
        owned_cancelled = {r['key'] for r in rows if r['collection'] == 'codex_reasoning'
            and json.loads(r['data']).get('checkpoint', {}).get('status') == 'unknown'
            and json.loads(r['data']).get('checkpoint', {}).get('error_code') == 'codex_cancelled_result_unknown'}
        if set(cancelled_native_ids) != owned_cancelled:
            raise ValueError('Explicit cancelled native scope must match the owned unknown request receipts')
        body['cancelled_native_ids'] = cancelled_native_ids
    if replacement_assignment is not None:
        if (set(replacement_assignment) != {'purpose', 'instruction', 'context', 'schema'}
                or replacement_assignment['purpose'] != assignment['purpose']
                or not isinstance(replacement_assignment['instruction'], str)
                or not replacement_assignment['instruction'].strip()
                or not isinstance(replacement_assignment['context'], dict)
                or not isinstance(replacement_assignment['schema'], dict)):
            raise ValueError('Replacement assignment must preserve purpose and supply a complete request')
        from jsonschema import Draft202012Validator
        Draft202012Validator.check_schema(replacement_assignment['schema'])
        body['replacement_assignment'] = copy.deepcopy(replacement_assignment)
    if controller_transition is not None:
        transition = copy.deepcopy(controller_transition)
        ref = owner.get('controller_owner') or {}
        if (set(transition) != {'collection', 'key', 'before', 'after'}
                or {k: transition[k] for k in ('collection', 'key')} != ref
                or any(row.get('run_id') != run_id or row.get('id') != transition['key']
                       for row in (transition['before'], transition['after']))
                or engine.store.get(transition['collection'], transition['key']) != transition['before']):
            raise ValueError('Controller transition must bind its declared same-run owner')
        body['controller_transition'] = transition
    if clear_events:
        snapshots = {r['key'] for r in rows if r['collection'] == 'context_snapshots'}
        removed_archives = {r['key'] for r in rows if r['collection'] == 'execution_archives'}
        original_result = engine.store.get('decisions', handoff_id, {}).get('result')
        with engine.store._read_db() as db:
            completion = db.execute(
                "SELECT max(created_at) FROM events WHERE run_id=? AND kind='handoff.completed' "
                "AND json_extract(data,'$.handoff_id')=?", (run_id, handoff_id)).fetchone()[0]
            end_time = max(handoff['updated_at'], completion or handoff['updated_at'])
            retained_ids = {r[0] for r in db.execute(
                "SELECT json_extract(data,'$.event_id') FROM records WHERE collection='execution_archives' "
                "AND json_extract(data,'$.run_id')=? AND key NOT IN (SELECT value FROM json_each(?))",
                (run_id, json.dumps(sorted(removed_archives))))}
            events = []
            for event in db.execute('SELECT * FROM events WHERE run_id=? AND created_at>=? AND created_at<=? ORDER BY id',
                                    (run_id, handoff['created_at'], end_time)):
                data = json.loads(event['data'])
                if event['id'] in retained_ids:
                    continue
                scoped = (data.get('handoff_id') == handoff_id or data.get('snapshot_id') in snapshots
                    or event['kind'] == 'agent.started' and event['message'] == handoff['purpose']
                    and data.get('role') == handoff['to_role']
                    or event['kind'] == 'agent.completed' and data.get('role') == handoff['to_role']
                    and data.get('result') == original_result)
                if scoped:
                    events.append(dict(event))
        body['cleared_events'] = events
        body['preserved'].remove('events')
    return {**body, 'preview_sha256': _digest(body)}


def apply(engine, plan, *, reason):
    if not reason.strip(): raise ValueError('Explicit restart reason required')
    body = {k: v for k, v in plan.items() if k != 'preview_sha256'}
    if _digest(body) != plan.get('preview_sha256'):
        raise ValueError('Restart preview hash changed')
    args = {k: plan[k] for k in ('assignment_pointer', 'failure_pointer')}
    current = preview(engine, plan['run_id'], plan['handoff_id'], plan['owner_key'],
                      replacement_assignment=plan.get('replacement_assignment'),
                      clear_events='cleared_events' in plan,
                      cancelled_native_ids=plan.get('cancelled_native_ids', ()),
                      controller_transition=plan.get('controller_transition'), **args)
    if current != plan: raise ValueError('Restart scope changed after preview; nothing cleared')
    run_id, handoff_id, owner_key = (plan[k] for k in ('run_id', 'handoff_id', 'owner_key'))
    run, handoff, owner, assignment = _scope(engine, run_id, handoff_id, owner_key, **args)
    rows = _records(engine.store, run_id, handoff_id, cancelled_native_ids=plan.get('cancelled_native_ids', ()))
    root = engine.data_dir.resolve(); generation = uid()
    archive = root / 'diagnostics' / 'task-restarts' / run_id / generation
    _plain_path(archive, root); archive.mkdir(parents=True)
    snapshot = archive / 'records.jsonl.gz'
    with gzip.open(snapshot, 'wt', encoding='utf-8') as stream:
        for row in [*rows, {'collection': 'stage_contexts', 'key': owner_key,
                'data': json.dumps(owner, ensure_ascii=False), 'updated_at': now()}]:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
    with gzip.open(snapshot, 'rt', encoding='utf-8') as stream:
        restored = [json.loads(line) for line in stream]
    if restored[:-1] != rows: raise ValueError('Restart archive verification failed')
    if plan.get('controller_transition'):
        controller_archive = archive / 'controller-owner.json'
        controller_archive.write_text(json.dumps(plan['controller_transition'], ensure_ascii=False, indent=2), encoding='utf-8')
        if json.loads(controller_archive.read_text(encoding='utf-8')) != plan['controller_transition']:
            raise ValueError('Controller owner archive verification failed')
    if 'cleared_events' in plan:
        event_archive = archive / 'events.json.gz'
        with gzip.open(event_archive, 'wt', encoding='utf-8') as stream:
            json.dump(plan['cleared_events'], stream, ensure_ascii=False)
        with gzip.open(event_archive, 'rt', encoding='utf-8') as stream:
            if json.load(stream) != plan['cleared_events']:
                raise ValueError('Restart event archive verification failed')
    from .engine import Harness
    from .request_hash_policy import instruction
    fresh = copy.deepcopy(plan.get('replacement_assignment') or assignment)
    fresh['context'].pop('tool_results', None)
    fresh['context']['task_retry_generation'] = generation
    fresh['instruction'] = instruction(fresh['instruction'] + GUIDANCE)
    new_handoff = Harness.decision_key(run_id, handoff['to_role'], **fresh)
    replacement = copy.deepcopy(owner)
    _set(replacement, plan['assignment_pointer'], fresh)
    _set(replacement, plan['failure_pointer'], None, remove=True)
    receipt = {'id': generation, 'run_id': run_id, 'created_at': now(), 'reason': reason,
        'source_handoff_id': handoff_id, 'new_handoff_id': new_handoff,
        'preview_sha256': plan['preview_sha256'], 'archive_path': str(snapshot),
        'archive_sha256': hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        'removed_records': dict(Counter(r['collection'] for r in rows)), 'removed_paths': plan['paths'],
        'assignment_sha256': _digest(fresh), 'preserved': plan['preserved'], 'status': 'ready_for_harness'}
    if plan.get('cancelled_native_ids'):
        receipt['cancelled_native_ids'] = plan['cancelled_native_ids']
        receipt['unknown_usage_retained'] = True
    if 'cleared_events' in plan:
        receipt.update(removed_events=len(plan['cleared_events']), event_archive_path=str(event_archive),
                       event_archive_sha256=hashlib.sha256(event_archive.read_bytes()).hexdigest())
    replacement.setdefault('task_restarts', []).append({k: receipt[k] for k in
        ('id', 'source_handoff_id', 'new_handoff_id', 'preview_sha256', 'created_at')})
    moved = []
    transition = plan.get('controller_transition')
    extra_puts = [(transition['collection'], transition['key'], transition['after'])] if transition else []
    extra_expected = [(transition['collection'], transition['key'], transition['before'])] if transition else []
    def move_outputs():
        if _records(engine.store, run_id, handoff_id,
                    cancelled_native_ids=plan.get('cancelled_native_ids', ())) != rows:
            raise ValueError('Task record scope changed before commit')
        for entry in plan['paths']:
            source = Path(entry['path']); _plain_path(source, root)
            if _file_manifest(source, root) != entry['files']: raise ValueError('Task files changed before clearing')
            dest = archive / 'files' / source.relative_to(root)
            _plain_path(dest, root); io_path(dest.parent).mkdir(parents=True, exist_ok=True)
            shutil.move(str(io_path(source)), str(io_path(dest))); moved.append((source, dest))
    try:
        engine.store.batch(puts=[('stage_contexts', owner_key, replacement), ('task_restarts', generation, receipt), *extra_puts],
            deletes=[(r['collection'], r['key']) for r in rows],
            expected=[('runs', run_id, run), ('stage_contexts', owner_key, owner),
                *[(r['collection'], r['key'], json.loads(r['data'])) for r in rows], *extra_expected],
            delete_events=plan.get('cleared_events', []), before_commit=move_outputs)
    except BaseException:
        for source, dest in reversed(moved):
            io_path(source.parent).mkdir(parents=True, exist_ok=True)
            shutil.move(str(io_path(dest)), str(io_path(source)))
        raise
    engine.store.event(run_id, 'task.restart_prepared',
        'Archived failed task context and output, cleared active records and prepared a fresh Harness assignment',
        {k: receipt[k] for k in ('id', 'source_handoff_id', 'new_handoff_id', 'preview_sha256')})
    (archive / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('preview', 'apply'))
    parser.add_argument('--data-dir', type=Path, default=Path(__file__).resolve().parent.parent / 'data')
    parser.add_argument('--run-id', required=True); parser.add_argument('--handoff-id', required=True)
    parser.add_argument('--owner-key', required=True); parser.add_argument('--assignment-pointer', required=True)
    parser.add_argument('--failure-pointer', default='/evidence_issue')
    parser.add_argument('--expected-preview-sha256'); parser.add_argument('--reason', default='')
    args = parser.parse_args()
    from .engine import Engine
    from .storage import Store
    engine = Engine(Store(args.data_dir.resolve() / 'research.sqlite3'), args.data_dir.resolve())
    try:
        plan = preview(engine, args.run_id, args.handoff_id, args.owner_key,
            assignment_pointer=args.assignment_pointer, failure_pointer=args.failure_pointer)
        if args.action == 'apply':
            if plan['preview_sha256'] != args.expected_preview_sha256:
                parser.error('An unchanged --expected-preview-sha256 from preview is required')
            result = apply(engine, plan, reason=args.reason)
        else: result = plan
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == '__main__': main()
