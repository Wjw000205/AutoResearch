"""Agent-selected, manifest-bound cleanup of reconstructible attempt caches.

Unknown files are evidence until proven otherwise. Never remove a directory,
checkpoint, dataset, log, source bundle or archival record through this tool.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
import re
import stat

import psutil

from .deletion import _plain_path
from .experiment_workflow import sha
from .storage import now

COLLECTION = 'experiment_cleanups'
MAX_FILES, MAX_SCAN, MAX_BYTES = 20, 5000, 64 * 1024 * 1024
PROTECTED_DIRS = {'data', 'datasets', 'artifacts', 'logs', 'checkpoints', 'weights',
                  'evidence', 'record-archive', '.git', '.venv', 'venv'}


async def dispatch(harness, run_id, role, owner, args):
    # Share the actual runner lock, so cleanup cannot race launch/recovery or
    # another role's deletion of the same attempt. Drain on cancellation.
    from .experiment_tools import execute
    lock = harness.engine.runner._locks.setdefault(run_id + ':' + args['trial_id'], asyncio.Lock())
    async with lock:
        task = asyncio.create_task(asyncio.to_thread(execute, harness, run_id, role, owner,
                                                    'experiments.cleanup', args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise


def _attempt(engine, run_id, trial_id, cycle):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', trial_id):
        raise ValueError('Cleanup requires a saved trial identifier, never a path')
    key = run_id + ':' + trial_id
    with engine.store._read_db() as db:
        row = db.execute("SELECT json_extract(data,'$.status'),json_extract(data,'$.attempt'),"
            "json_extract(data,'$.artifact_path'),json_extract(data,'$.pid'),"
            "json_extract(data,'$.process_started'),json_extract(data,'$.fingerprint') "
            "FROM records WHERE collection='experiments' AND key=? AND json_extract(data,'$.run_id')=? "
            "AND coalesce(json_extract(data,'$.research_cycle'),1)=?", (key, run_id, cycle)).fetchone()
        if not row or row[0] != 'completed' or type(row[1]) is not int or row[1] < 1:
            raise ValueError('Only a completed, saved experiment in this cycle can be cleaned')
        # Source manifests are protected even if someone called a source file a cache.
        protected = db.execute("SELECT json_extract(f.value,'$.path') FROM records r, "
            "json_each(r.data,'$.limits.code_bundle.files') f "
            "WHERE r.collection='experiments' AND r.key=? LIMIT 5001", (key,)).fetchall()
    if len(protected) > MAX_SCAN:
        raise ValueError('Oversized source manifest; retain files for manual inspection')
    if row[3]:
        try:
            process = psutil.Process(row[3])
            if row[4] is None or abs(process.create_time() - row[4]) < 0.01:
                raise ValueError('Experiment process is still present; retain its files')
        except psutil.NoSuchProcess:
            pass
    root = engine.runner.root.absolute()
    base = Path(row[2] or '')
    expected = root / run_id / trial_id / f'attempt-{row[1]:03d}'
    if not base.is_absolute() or base != expected:
        raise ValueError('Cleanup requires the exact runner-owned attempt directory')
    _plain_path(base, root)
    if not base.is_dir():
        raise ValueError('Completed attempt directory is unavailable')
    binding = {'trial_id': trial_id, 'attempt': row[1], 'research_cycle': cycle,
               'artifact_path': str(base), 'fingerprint': row[5]}
    return root, base, binding, {str(item[0]).casefold() for item in protected}


def _eligible(relative, base, protected):
    name = relative.as_posix()
    if name.casefold() in protected or any(p.casefold() in PROTECTED_DIRS for p in relative.parts):
        return False
    if relative.parent.name == '__pycache__':
        match = re.fullmatch(r'(.+)\.(?:cpython|pypy)-[\w-]+(?:\.opt-[0-9]+)?\.pyc', relative.name)
        if match:
            source = base / relative.parent.parent / (match[1] + '.py')
            _plain_path(source, base)
            return source.is_file()
    if '.pytest_cache' in relative.parts:
        suffix = relative.parts[relative.parts.index('.pytest_cache') + 1:]
        return suffix in {('README.md',), ('.gitignore',), ('CACHEDIR.TAG',),
            ('v', 'cache', 'nodeids'), ('v', 'cache', 'lastfailed'),
            ('v', 'cache', 'stepwise'), ('v', 'cache', 'durations')}
    return False


def _identity(path, root):
    _plain_path(path, root)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_BYTES:
        raise ValueError('Cleanup only accepts bounded, regular, unshared cache files')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError('Cache file changed while opening')
        count = 0
        while chunk := stream.read(1024 * 1024):
            count += len(chunk)
            if count > MAX_BYTES:
                raise ValueError('Cache file exceeded cleanup byte limit')
            digest.update(chunk)
    after = path.lstat()
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink')
    if any(getattr(before, k) != getattr(after, k) for k in fields):
        raise ValueError('Cache file changed during preview')
    return {'sha256': digest.hexdigest(), 'bytes': count,
            'stat': {k: getattr(after, k) for k in fields}}


def _preview(engine, run_id, role, owner, root, base, binding, protected):
    files, scanned, skipped, total = [], 0, 0, 0
    stack = [base]
    limited = False
    while stack and not limited:
        directory = stack.pop()
        _plain_path(directory, root)
        with os.scandir(directory) as entries:
            for entry in entries:
                scanned += 1
                if scanned > MAX_SCAN or len(files) >= MAX_FILES or total >= MAX_BYTES:
                    limited = True
                    break
                path = Path(entry.path)
                try:
                    _plain_path(path, root)
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name.casefold() not in PROTECTED_DIRS:
                            stack.append(path)
                        continue
                    relative = path.relative_to(base)
                    if not _eligible(relative, base, protected):
                        continue
                    identity = _identity(path, root)
                    if total + identity['bytes'] > MAX_BYTES:
                        limited = True
                        break
                    item = {'path': relative.as_posix(), **identity}
                    files.append({'file_id': sha(item), **item})
                    total += identity['bytes']
                except (OSError, ValueError):
                    skipped += 1  # Links/unknown/in-use files remain untouched.
    manifest = {'run_id': run_id, 'role': role, 'handoff_id': owner, 'binding': binding,
        'policy': 'reconstructible_cache_v1', 'files': sorted(files, key=lambda row: row['path']),
        'scan_limited': limited, 'unsafe_or_changed_skipped': skipped}
    fingerprint = sha(manifest)
    key = run_id + ':' + fingerprint
    record = engine.store.get(COLLECTION, key)
    if record is None:
        record = {'id': key, 'run_id': run_id, 'sha256': fingerprint, 'manifest': manifest,
                  'created_at': now(), 'results': {}}
        engine.store.put(COLLECTION, key, record)
    return {'preview_id': key, 'sha256': fingerprint, 'trial_id': binding['trial_id'],
        'files': [{k: row[k] for k in ('file_id', 'path', 'bytes', 'sha256')} for row in manifest['files']],
        'candidate_bytes': total, 'scan_limited': limited, 'unsafe_or_changed_skipped': skipped,
        'policy': manifest['policy'], 'deleted': False}


def execute(engine, run_id, role, owner, cycle, args):
    from .tool_contracts import validate_arguments
    validate_arguments('experiments.cleanup', args)
    root, base, binding, protected = _attempt(engine, run_id, args['trial_id'], cycle)
    if args['action'] == 'preview':
        return _preview(engine, run_id, role, owner, root, base, binding, protected)
    record = engine.store.get(COLLECTION, args['preview_id'])
    manifest = (record or {}).get('manifest', {})
    if (not record or record.get('run_id') != run_id or record.get('sha256') != args['expected_sha256']
            or sha(manifest) != args['expected_sha256'] or manifest.get('binding') != binding
            or manifest.get('role') != role or manifest.get('handoff_id') != owner):
        raise ValueError('Cleanup requires this role and assignment\'s unchanged preview')
    candidates = {row['file_id']: row for row in manifest['files']}
    if not set(args['file_ids']).issubset(candidates):
        raise ValueError('Cleanup only accepts file IDs from the saved preview')
    results = record['results']
    for file_id in args['file_ids']:
        item = candidates[file_id]
        path = base / item['path']
        previous = results.get(file_id)
        if previous:
            if previous['status'] == 'pending':
                # A crash does not prove who removed the file; never retry an
                # uncertain deletion against a recreated file.
                results[file_id] = {**previous, 'status': 'outcome_unknown', 'completed_at': now()}
                engine.store.put(COLLECTION, record['id'], record)
            continue
        result = {'file_id': file_id, 'status': 'pending', 'started_at': now(), 'bytes': item['bytes']}
        results[file_id] = result
        engine.store.put(COLLECTION, record['id'], record)  # Durable intent before unlink.
        try:
            _, _, current, protected = _attempt(engine, run_id, args['trial_id'], cycle)
            if current != binding or not _eligible(Path(item['path']), base, protected):
                raise ValueError('Attempt or protected-file policy changed')
            if _identity(path, root) != {k: item[k] for k in ('sha256', 'bytes', 'stat')}:
                raise ValueError('File changed after preview; retain it and preview again')
            _plain_path(path, root)
            path.unlink()  # Individual regular file only; never recursive removal.
            result['status'] = 'deleted'
        except FileNotFoundError:
            result['status'] = 'already_missing'
        except (OSError, ValueError, psutil.Error) as exc:
            result.update(status='retained', reason=type(exc).__name__)
        result['completed_at'] = now()
        engine.store.put(COLLECTION, record['id'], record)
    selected = [results[key] for key in args['file_ids']]
    return {'preview_id': record['id'], 'sha256': record['sha256'], 'trial_id': args['trial_id'],
        'results': selected, 'deleted_bytes': sum(row['bytes'] for row in selected if row['status'] == 'deleted'),
        'audit_collection': COLLECTION, 'scientific_approval': False}
