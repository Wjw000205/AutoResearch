"""Remove owned native reasoning chats only after durable complete output capture."""
from __future__ import annotations

import hashlib
import asyncio
from pathlib import Path
from datetime import datetime, timedelta, timezone

from .local_codex import LocalCodexClient, LocalCodexError
from .storage import now

_locks = {}


def due(record):
    audit = record.get('thread_cleanup', {})
    return (audit.get('status') != 'deleted' and audit.get('attempts', 0) < 3
            and audit.get('retry_after', '') <= now())


def eligible(store, record):
    receipt, result = record.get('checkpoint', {}), record.get('result', {})
    transcript = store.get('transcripts', result.get('transcript_id', '')) or {}
    workspace = (store.path.parent / 'codex-reasoning' / record['id'] / record['role']).resolve()
    return (receipt.get('status') == 'completed' and receipt.get('reasoning_only') is True
        and receipt.get('auth_mode') == 'chatgpt' and not receipt.get('public_output_truncated')
        and not receipt.get('native_tool_violation') and receipt.get('operation_id') == record['id']
        and receipt.get('run_id') == record['run_id']
        and receipt.get('workspace') == str(workspace) and receipt.get('project_root') == str(workspace)
        and result.get('thread_id') == receipt.get('thread_id') and result.get('turn_id') == receipt.get('turn_id')
        and bool(result.get('text')) and transcript.get('text') == result['text']
        and transcript.get('run_id') == record['run_id'] and transcript.get('role') == record['role'])


def _eligible_record(store, key):
    record = store.get('codex_reasoning', key)
    return record if (isinstance(record, dict) and record.get('id') == key
                      and due(record) and eligible(store, record)) else None


def _candidate_keys(store, run_id, limit):
    """Bound Python decoding to possible captures; never load all frozen prompts.

    The SQL predicate only selects candidates. Exact ownership, workspace and
    retained-transcript checks remain in eligible(), and are repeated just before
    cleanup. Keyset pages let an invalid capture be skipped without starving later
    valid captures or accumulating an unbounded list in memory.
    """
    keys, after = [], ''
    query = """SELECT key FROM records
        WHERE collection='codex_reasoning' AND key>?
          AND coalesce(json_extract(data,'$.thread_cleanup.status'),'')!='deleted'
          AND coalesce(json_extract(data,'$.thread_cleanup.attempts'),0)<3
          AND coalesce(json_extract(data,'$.thread_cleanup.retry_after'),'')<=?
          AND json_extract(data,'$.checkpoint.status')='completed'
          AND json_type(data,'$.checkpoint.reasoning_only')='true'
          AND json_extract(data,'$.checkpoint.auth_mode')='chatgpt'
          AND coalesce(json_extract(data,'$.checkpoint.public_output_truncated'),0)=0
          AND coalesce(json_extract(data,'$.checkpoint.native_tool_violation'),0)=0
          AND json_type(data,'$.result')='object'
          AND json_type(data,'$.result.text')='text'
          AND length(json_extract(data,'$.result.text'))>0"""
    parameters = [now()]
    if run_id is not None:
        query += " AND json_extract(data,'$.run_id')=?"
        parameters.append(run_id)
    query += ' ORDER BY key LIMIT ?'
    while len(keys) < limit:
        remaining = limit - len(keys)
        with store._read_db() as db:
            page = [row['key'] for row in db.execute(query, [after, *parameters, remaining]).fetchall()]
        if not page:
            break
        for key in page:
            if _eligible_record(store, key) is not None:
                keys.append(key)
        after = page[-1]
    return keys


async def cleanup_record(store, key, *, client=None):
    lock = _locks.setdefault((str(store.path), key), asyncio.Lock())
    async with lock:
        return await _cleanup_record(store, key, client=client)


def _prepare_cleanup(store, key):
    record = _eligible_record(store, key)
    if record is None:
        return None
    audit = {'status': 'pending', 'thread_id': record['checkpoint']['thread_id'],
        'captured_output_sha256': hashlib.sha256(record['result']['text'].encode()).hexdigest(),
        'attempted_at': now(), 'attempts': record.get('thread_cleanup', {}).get('attempts', 0) + 1}
    def claim(old):
        if old != record:
            raise LocalCodexError('codex_cleanup_capture_changed')
        return {**old, 'thread_cleanup': audit}
    try:
        store.mutate('codex_reasoning', key, claim)
    except LocalCodexError:
        return None
    return record, audit


async def _cleanup_record(store, key, *, client=None):
    prepared = await asyncio.to_thread(_prepare_cleanup, store, key)
    if prepared is None:
        return False
    record, audit = prepared
    workspace = Path(record['checkpoint']['workspace'])
    try:
        result = await (client or LocalCodexClient(request_timeout=10)).delete_completed_reasoning_thread(
            record['checkpoint'], workspace)
    except (LocalCodexError, OSError, ValueError, TimeoutError) as exc:
        # Cleanup cannot invalidate a saved model reply or trigger inference again.
        code = getattr(exc, 'code', type(exc).__name__)
        if code == 'codex_thread_writer_busy':
            audit.update(attempts=audit['attempts'] - 1,
                retry_after=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        await asyncio.to_thread(store.mutate, 'codex_reasoning', key, lambda old: {**old, 'thread_cleanup': {
            **audit, 'error_code': code}})
        return False
    await asyncio.to_thread(store.mutate, 'codex_reasoning', key, lambda old: {**old, 'thread_cleanup': {**audit, **result}})
    return True


async def cleanup_completed(store, *, run_id=None, limit=100):
    """Explicit maintenance/startup sweep; never enumerate or delete other chats."""
    if type(limit) is not int or limit < 0:
        raise ValueError('Cleanup limit must be a non-negative integer')
    counts = {'deleted': 0, 'retained': 0}
    if limit == 0:
        return counts
    candidates = await asyncio.to_thread(_candidate_keys, store, run_id, limit)
    for key in candidates:
        deleted = await cleanup_record(store, key)
        counts['deleted' if deleted else 'retained'] += 1
    return counts
