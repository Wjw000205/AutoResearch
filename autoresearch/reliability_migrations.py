"""Additive, repeatable migrations; never rebuild existing research records."""
from __future__ import annotations

from .storage import now


def migrate(db):
    db.execute('CREATE TABLE IF NOT EXISTS schema_migrations '
               '(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL, description TEXT NOT NULL)')
    db.execute('INSERT OR IGNORE INTO schema_migrations VALUES (?,?,?)', (
        'memory-reliability-v1', now(),
        'Versioned JSON collections: context_manifests, evidence_deliveries, '
        'execution_archives, tool_invocations; existing records and indexes retained'))


def backup_database(path, destination):
    """Explicit SQLite online backup, including committed WAL, without replacement."""
    import sqlite3
    from pathlib import Path
    source, target = Path(path).resolve(), Path(destination).absolute()
    if target.exists() or target.resolve() == source:
        raise ValueError('Backup destination must be new')
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as origin:
        with sqlite3.connect(target) as output:
            origin.backup(output, pages=1024)
            if output.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('Backup integrity check failed')
    return str(target)
