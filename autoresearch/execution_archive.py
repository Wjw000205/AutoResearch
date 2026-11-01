"""Append-only, sanitized execution payloads, separate from working projections."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .storage import now, redact


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def sanitize(store, value):
    blocked = {'api_key', 'apikey', 'authorization', 'x-api-key', 'password',
               'secret', 'access_token', 'refresh_token', 'credentials',
               'reasoning_content', 'internal_reasoning', 'chain_of_thought', 'thinking'}
    settings = store.get('settings', 'main', {})
    names = {'OPENAI_API_KEY', 'CODEX_API_KEY', 'RUIKON_OPENAI_KEY',
             'RUIKON_ANTHROPIC_KEY', 'RUIKON_GROK_KEY'}
    names.update(p.get('api_key_env', '') for p in settings.get('providers', []))
    secrets = [os.environ[name] for name in names if name and os.environ.get(name)]
    def clean(item):
        if isinstance(item, dict):
            return {key: '[redacted]' if key.lower() in blocked or key.upper() in names
                    else clean(child) for key, child in item.items()}
        if isinstance(item, list):
            return [clean(child) for child in item]
        if isinstance(item, str):
            for secret in secrets:
                item = item.replace(secret, '[redacted]')
        return redact(item)
    return clean(value)


def save(store, run_id, role, invocation_id, payload, *, handoff_id=None, request_id=None, event_id=None):
    clean = sanitize(store, payload)
    raw = encoded(clean).encode('utf-8')
    digest = hashlib.sha256(raw).hexdigest()
    aid = f'{run_id}:execution:{invocation_id}:{digest}'
    old = store.get('execution_archives', aid)
    if old:
        load(store, run_id, role, aid, digest)
        return old
    config = store.get('runs', run_id, {}).get('config', {})
    record = {'id': aid, 'run_id': run_id, 'role': role, 'invocation_id': invocation_id,
              'handoff_id': handoff_id, 'request_id': request_id, 'sha256': digest,
              'bytes': len(raw), 'created_at': now(), 'version': 1,
              'provenance': 'sanitized_original_tool_execution', 'redacted': clean != payload}
    limit = max(1024, int(config.get('execution_archive_inline_bytes', 65536)))
    if event_id is not None:
        # The existing append-only tool journal already owns the raw result.
        # Persist a hash-bound reference, not a second copy of its large body.
        with store._read_db() as db:
            event = db.execute('SELECT run_id,kind,message,data FROM events WHERE id=?', (event_id,)).fetchone()
        invocation = store.get('tool_invocations', invocation_id)
        data = json.loads(event['data']) if event else {}
        if (not event or event['run_id'] != run_id or event['kind'] != 'tool.completed'
                or data.get('role') != role or data.get('tool_call_id') != invocation_id
                or not invocation or invocation.get('call') != clean.get('call')
                or data.get('result') != clean.get('result')):
            raise ValueError('Execution journal reference identity mismatch')
        record['event_id'] = event_id
    elif len(raw) <= limit:
        record['payload'] = clean
    else:
        # Content addressing makes an interrupted file write harmless; only an
        # atomic, hash-checked file is ever referenced by a database record.
        root = store.path.parent.resolve()
        path = root / 'runs' / run_id / 'execution-archive' / (digest + '.json')
        from .deletion import _plain_path
        _plain_path(path, root)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError('Execution archive file integrity mismatch')
        else:
            descriptor, filename = tempfile.mkstemp(dir=path.parent, prefix='.archive-', suffix='.tmp')
            temporary = Path(filename)
            try:
                with os.fdopen(descriptor, 'wb') as output:
                    output.write(raw)
                    output.flush()
                    os.fsync(output.fileno())
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        record['path'] = str(path)
    def immutable(previous):
        if previous and (previous.get('sha256') != digest or previous.get('role') != role):
            raise ValueError('Immutable execution archive changed')
        return previous or record
    return store.mutate('execution_archives', aid, immutable)


def load(store, run_id, role, archive_id, expected_sha256):
    record = store.get('execution_archives', archive_id)
    if (not record or record.get('run_id') != run_id or record.get('role') != role
            or expected_sha256 != record.get('sha256')):
        raise ValueError('Execution archive version or ownership mismatch')
    if record.get('expired_at'):
        raise ValueError('Execution archive expired under retention policy')
    if record.get('event_id') is not None:
        with store._read_db() as db:
            event = db.execute('SELECT run_id,kind,data FROM events WHERE id=?', (record['event_id'],)).fetchone()
        invocation = store.get('tool_invocations', record['invocation_id'])
        data = json.loads(event['data']) if event else {}
        if (not event or event['run_id'] != run_id or event['kind'] != 'tool.completed'
                or data.get('role') != role or data.get('tool_call_id') != record['invocation_id']
                or not invocation or invocation.get('role') != role):
            raise ValueError('Execution journal reference ownership mismatch')
        raw = encoded({'call': invocation['call'], 'result': data.get('result')}).encode('utf-8')
    elif 'payload' in record:
        raw = encoded(record['payload']).encode('utf-8')
    else:
        root = store.path.parent.resolve()
        path = Path(record['path']).absolute()
        from .deletion import _plain_path
        _plain_path(path, root)
        if not path.is_relative_to(root / 'runs' / run_id / 'execution-archive'):
            raise ValueError('Execution archive is outside this research')
        raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError('Execution archive integrity mismatch')
    return copy.deepcopy(json.loads(raw))


def retention_candidates(store, run_id, *, days=None):
    """Explicit dry-run only. Pending invocations and working references pin data.

    Default retention is indefinite. This deliberately never deletes evidence
    silently; task deletion uses the existing task-scoped cleanup mechanism.
    """
    if days is None:
        days = store.get('runs', run_id, {}).get('config', {}).get('execution_archive_retention_days')
    if days is None:
        return []
    if type(days) is not int or days < 1:
        raise ValueError('Retention days must be a positive integer or null')
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    pinned = {row.get('archive_id') for row in store.list('tool_invocations', run_id)}
    archives = store.list('execution_archives', run_id)
    # Context snapshots and handoff inputs use immutable references; retain
    # anything referenced there even after a tool invocation is retired.
    for collection in ('context_commits', 'context_snapshots', 'handoff_progress'):
        for row in store.list(collection, run_id):
            text = encoded(row)
            for archive in archives:
                if archive['id'] in text:
                    pinned.add(archive['id'])
    return [row['id'] for row in archives
            if row['id'] not in pinned and datetime.fromisoformat(row['created_at']) < cutoff]
