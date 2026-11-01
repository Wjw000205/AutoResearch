"""Task-scoped cleanup; no shared database or directory is recursively removed."""
from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


class DeletionError(ValueError):
    """Deletion did not finish and may be retried after its cause is resolved."""


def _plain_path(path: Path, root: Path) -> None:
    """Reject every reparse point, including Windows directory junctions."""
    if not path.is_relative_to(root):
        raise DeletionError('Task cleanup path is outside the data directory')
    for part in (root, *[root.joinpath(*path.relative_to(root).parts[:i])
                        for i in range(1, len(path.relative_to(root).parts) + 1)]):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise DeletionError('Task cleanup refuses symbolic links or directory junctions')
        if not part.resolve().is_relative_to(root):
            raise DeletionError('Task cleanup path resolves outside the data directory')


def validate_tree(path: Path, root: Path) -> None:
    _plain_path(path, root)
    if not path.exists():
        return
    if not path.is_dir():
        raise DeletionError('Task cleanup expected a task directory')
    def failed_scan(error):
        raise error
    for directory, dirs, files in os.walk(path, followlinks=False, onerror=failed_scan):
        for name in [*dirs, *files]:
            entry = Path(directory) / name
            _plain_path(entry, root)
            if not (entry.is_file() or entry.is_dir()):
                raise DeletionError('Task cleanup refuses non-regular filesystem entries')


def cleanup_paths(data_dir: Path, run_id: str) -> tuple[Path, Path, Path]:
    if not isinstance(run_id, str) or not run_id.isascii() or not run_id.isalnum():
        raise DeletionError('Invalid research task identifier')
    root = Path(data_dir).absolute()
    if root.resolve() != root:
        raise DeletionError('Task cleanup data directory must not contain links')
    paths = (root / 'runs' / run_id, root / 'experiments' / run_id)
    # Preflight both trees before removing either one.
    for path in paths:
        validate_tree(path, root)
    graph = root / 'graph.sqlite3'
    validate_graph(graph, root)
    return *paths, graph


def remove_task_tree(path: Path, data_dir: Path) -> None:
    if (path.parent not in (data_dir / 'runs', data_dir / 'experiments')
            or not path.name.isascii() or not path.name.isalnum()):
        raise DeletionError('Cleanup requires an exact task directory')
    validate_tree(path, data_dir)
    if path.exists():
        shutil.rmtree(path)
    if path.exists():
        raise DeletionError('Task directory remains after cleanup')


def validate_graph(path: Path, root: Path) -> None:
    for candidate in (path, *(Path(str(path) + suffix) for suffix in ('-wal', '-shm', '-journal'))):
        _plain_path(candidate, root)
        if candidate.exists() and not candidate.is_file():
            raise DeletionError('Task checkpoint cleanup expected a database file')


async def remove_graph_thread(path: Path, data_dir: Path, run_id: str) -> None:
    validate_graph(path, data_dir)
    if not path.exists():
        return
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        await saver.setup()
        await saver.adelete_thread(run_id)
