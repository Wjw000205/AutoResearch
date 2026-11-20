"""Versioned CPU check CLI adapter; run only in a dedicated Harness process.

Example: python -m autoresearch.capability_checks --project-root C:/checkout
  --check-time-policy no_fixed_duration_v1 submit ORDER --run-id RUN ...

All ordinary CLI arguments, review authentication and routing belong to
capability_work_orders. Legacy intakes must use that entry or a NEW successor;
this adapter never upgrades an existing intake, even via a supplement.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3
import shutil
import sys
import tempfile
import threading
from types import SimpleNamespace
from urllib.parse import urlsplit
from urllib.request import url2pathname

from . import capability_work_orders as core, code_verification as checker
from .async_ownership import finish
from .codex_workspace import _plain
from .storage import _ReadSnapshot

VERSION = 'capability_check_adapter_v2'
POLICY = 'no_fixed_duration_v1'
FIELD = 'capability_check_contract'
STATE_DATABASE = 'check-work-v2.sqlite3'
# This adapter relies on the exact dependency/save/check boundaries below.
# A new core requires a reviewed adapter version, not best-effort patching.
CORE_SHA256 = '32055760050059890d6e9bb5d2f68c2bc5b6acda10dc9269394f022c8494a9a8'
# Explicit compatibility: the added program-owned repair intake preserves the
# original CLI/default binding and all implementation/check/review boundaries.
SUPPORTED_CORE_SHA256S = {CORE_SHA256, '8d5a1c2f177528ebafa439a5c0c90bb692631d678dfc0b2a4a59a0e15b96ec08'}
BaseWorkOrder = core.WorkOrder


def _readonly_connect(uri, *, uri_mode=True, **kwargs):
    """Read a stable DB/WAL copy without SQLite touching source sidecars.

    mode=ro alone may create/update -shm/-wal on the source. immutable=1 on
    the source is unsafe while a writer exists. Copy both DB and WAL, verify
    their identities and bytes again, and let SQLite read only the owned copy.
    A concurrent write fails preflight; it is never guessed to be a snapshot.
    """
    parsed = urlsplit(uri)
    if not uri_mode or parsed.scheme != 'file' or parsed.query != 'mode=ro' or parsed.netloc:
        raise ValueError('Expected a local read-only SQLite URI')
    path = Path(url2pathname(parsed.path))
    temporary = tempfile.TemporaryDirectory(prefix='autoresearch-code-check-read-')
    workspace = Path(temporary.name).absolute()
    info = workspace.lstat()
    workspace_identity = (info.st_dev, info.st_ino)
    parent = workspace.parent.resolve()
    temporary._finalizer.detach()
    def cleanup():
        try:
            checker._remove_owned_workspace(workspace, workspace_identity, parent)
        except FileNotFoundError:
            pass
    db = None
    try:
        _plain(workspace)
        target = workspace / 'read.sqlite3'
        originals = [path, Path(str(path) + '-wal')]
        before = {}
        for source in originals:
            _plain(source)
            try:
                info = source.stat()
            except FileNotFoundError:
                if source == path:
                    raise
                before[source] = None
                continue
            before[source] = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            copied = target if source == path else Path(str(target) + '-wal')
            shutil.copyfile(source, copied)
        for source, source_identity in before.items():
            _plain(source)
            try:
                info = source.stat()
            except FileNotFoundError:
                if source_identity is None:
                    continue
                raise ValueError('Read-only database snapshot changed') from None
            copied = target if source == path else Path(str(target) + '-wal')
            if (source_identity != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                    or core._sha(source) != core._sha(copied)):
                raise ValueError('Read-only database snapshot changed')
        class SnapshotConnection(sqlite3.Connection):
            def close(self):
                try:
                    super().close()
                finally:
                    cleanup()  # Verify ownership; never follow replaced paths.
        db = sqlite3.connect(target.as_uri() + '?mode=ro', uri=True,
                             factory=SnapshotConnection, **kwargs)
        db.execute('PRAGMA query_only=ON')
        return db
    except BaseException:
        if db is not None:
            db.close()
        cleanup()
        raise


def _core_readonly_connect(database, *, uri=False, **kwargs):
    return _readonly_connect(database, uri_mode=uri, **kwargs)


class _OrderPath(type(Path())):
    """Translate only core's fixed state filename inside the selected checkout.

    Core's CLI, Store, locks, receipts and dependency routing remain unchanged.
    No work.sqlite3 is created for a new order, so the unadapted CLI rejects it
    before opening a Store or worker lock. Legacy dependencies stay read-only.
    This path type is confined to the dedicated adapter process/context.
    """
    order_root: Path
    primary: Path

    def __truediv__(self, key):
        if key == 'work.sqlite3' and self.parent == self.order_root:
            legacy = Path(self) / key
            versioned = Path(self) / STATE_DATABASE
            _plain(legacy)
            _plain(versioned)
            if legacy.exists() and (Path(self) == self.primary or versioned.exists()):
                raise ValueError('Legacy or mixed state storage requires a new successor intake')
            if Path(self) == self.primary or versioned.is_file():
                return super().__truediv__(STATE_DATABASE)
        return super().__truediv__(key)


def project_root(value):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('--project-root must be an explicit absolute checkout')
    _plain(path)
    if not path.is_dir():
        raise ValueError('Target checkout does not exist')
    path = path.resolve(strict=True)
    if not path.is_dir() or path == Path(path.anchor) or '.autoresearch' in path.parts:
        raise ValueError('Target must be a checkout, not an isolated work-order candidate')
    for name in ('pyproject.toml', 'autoresearch/capability_work_orders.py', 'data/research.sqlite3'):
        if not checker.checked_path(path, name).is_file():
            raise ValueError('Target checkout is missing ' + name)
    _plain(path / core.ROOT_DIRECTORY)
    return path


def runtime_identity():
    root = Path(__file__).resolve().parent
    files = {}
    # Includes transitive application imports, not just the immediate wrapper.
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d != '__pycache__')
        for name in dirs:
            _plain(Path(directory) / name)
        for name in sorted(names):
            if name.endswith('.py'):
                path = Path(directory) / name
                _plain(path)
                files[path.relative_to(root).as_posix()] = core._sha(path)
    if files.get('capability_work_orders.py') not in SUPPORTED_CORE_SHA256S:
        raise ValueError('Unsupported source work-order adapter runtime')
    selected = ('capability_checks.py', 'capability_work_orders.py', 'code_verification.py',
                'disk_usage.py', 'lab.py', 'async_ownership.py', 'harness_delivery.py',
                'engine.py', 'storage.py', 'config.py', 'local_codex.py', 'codex_accounting.py')
    return {'source_root': str(root.parent), 'source_tree_sha256': core.digest(files),
            'modules': {name: {'path': str(root / name), 'sha256': files[name]} for name in selected},
            'python': {'path': str(Path(sys.executable).resolve()), 'sha256': core._sha(Path(sys.executable)),
                       'version': sys.version}}


def freeze_contract(root):
    root = project_root(root)
    sources = {name: core._sha(checker.checked_path(root, name)) for name in core._source_paths(root)
               if checker.checked_path(root, name).is_file()}
    if not sources or 'autoresearch/capability_work_orders.py' not in sources:
        raise ValueError('Target source inventory is unavailable')
    return {'version': VERSION, 'policy': {'version': POLICY, 'wall_seconds': None, 'cpu_seconds': None},
            'state_database': STATE_DATABASE, 'execution_platform': 'windows_job_v1',
            'project_root': str(root), 'database': str(root / 'data/research.sqlite3'),
            'target_source_sha256': core.digest(sources), 'runtime': runtime_identity()}


def validate_contract(work, root):
    contract = work['input'].get(FIELD)
    if not contract:
        raise ValueError('Legacy intake requires a new successor; policy upgrade is not supported')
    if core.digest(work['input']) != work['input_sha256'] or contract != freeze_contract(root):
        raise ValueError('Frozen check policy, target or runtime drift; submit a new work order')
    _validate_check_receipts(work, contract)
    return contract


def _validate_check_receipts(work, contract):
    """Shared replay/dependency validation without executing historical work."""
    for attempt in work['attempts']:
        checks = attempt.get('checks')
        execution = attempt.get('check_execution')
        if (checks and not execution) or execution and (execution.get('state') != 'terminal' or not checks):
            raise ValueError('Previous host check completion is unconfirmed; execution blocked')
        if checks and checks.get('host_receipt', {}).get('process_cleanup_confirmed') is not True:
            raise ValueError('Previous host check process cleanup is unconfirmed; execution blocked')
        if execution and (execution.get('version') != VERSION or
                execution.get('input_sha256') != work['input_sha256'] or
                execution.get('contract_sha256') != core.digest(contract) or
                execution.get('candidate_sha256') != attempt['candidate_sha256'] or
                execution.get('receipt_sha256') != checks.get('receipt_sha256')):
            raise ValueError('Host check execution receipt identity changed')
        if checks and (checks.get(FIELD) != contract or checks.get('receipt_version') != VERSION
                       or checks.get('receipt_sha256') != core.digest(
                           {k: v for k, v in checks.items() if k != 'receipt_sha256'})):
            raise ValueError('Frozen host check receipt policy or runtime changed')


def run_check(candidate, output, inventory, tests, contract, cancelled):
    """Narrow process-local adapter; no scheduler or model execution here."""
    if contract['runtime'] != runtime_identity() or contract['policy'] != {
            'version': POLICY, 'wall_seconds': None, 'cpu_seconds': None}:
        raise ValueError('Check runtime or policy drift')
    execute, seconds = checker._execute, core.CHECK_SECONDS
    captured = {}

    def admitted(directory, manifest, hashes, entrypoint, limits, duration, destination, ignored_cancel):
        if duration is not None or limits['cpu_seconds'] is not None:
            raise ValueError('No-duration adapter did not reach the host check boundary')
        captured['limits'] = copy.deepcopy(limits)
        captured['host_receipt'] = execute(directory, manifest, hashes, entrypoint, limits, None,
                                           destination, cancelled)
        return captured['host_receipt']

    try:
        core.CHECK_SECONDS, checker._execute = None, admitted
        try:
            checks = core._run_check(candidate, output, inventory, tests)
        except OSError as exc:
            # The outer staging TemporaryDirectory in the legacy adapter can
            # fail AFTER _execute returned. Retain that result and its artifacts.
            if 'host_receipt' not in captured:
                raise
            host = captured['host_receipt']
            checker._cleanup_diagnostic(host, output, 'capability_staging_cleanup', exc)
            diagnostic = output / 'cleanup-error.json'
            host['artifacts'] = [a for a in host['artifacts'] if a['path'] != str(diagnostic)]
            if diagnostic.is_file():
                host['artifacts'].append({'path': str(diagnostic), 'sha256': core._sha(diagnostic)})
            checks = {'candidate_sha256': core.digest(inventory), 'test_paths': tests,
                      **captured, 'staging_finalization': 'failed'}
    finally:
        core.CHECK_SECONDS, checker._execute = seconds, execute
    checks.update(receipt_version=VERSION, **{FIELD: copy.deepcopy(contract)})
    checks['receipt_sha256'] = core.digest(checks)
    return checks


class WorkOrder(BaseWorkOrder):
    """Reuse core intake/implementation/review; authenticate dependencies read-only."""
    target_root: Path
    primary_directory: Path

    def __init__(self, directory, database):
        self.directory, self.database = self.path_type(directory), Path(database)
        _plain(self.directory)
        _plain(self.database)
        if self.database != self.target_root / 'data/research.sqlite3':
            raise ValueError('Work-order database differs from the explicit target')
        if self.directory.parent != self.target_root / core.ROOT_DIRECTORY:
            raise ValueError('Work-order directory escaped the explicit target')
        self.read_only_dependency = self.directory != self.primary_directory
        if not self.read_only_dependency:
            super().__init__(self.directory, self.database)

    def submit(self, *args, **kwargs):
        if self.read_only_dependency:
            raise ValueError('Dependency records are read-only')
        self._intake_contract = freeze_contract(self.target_root)
        try:
            return super().submit(*args, **kwargs)
        finally:
            self._intake_contract = None

    def save(self, work, **fields):
        if self.read_only_dependency:
            raise ValueError('Dependency records are read-only')
        if getattr(self, '_intake_contract', None) and FIELD not in work['input']:
            work['input'][FIELD] = copy.deepcopy(self._intake_contract)
            work['input_sha256'] = core.digest(work['input'])
        elif FIELD not in work['input']:
            raise ValueError('Legacy intake requires a new successor')
        super().save(work, **fields)

    def authenticate_dependency(self, work):
        if core.digest(work['input']) != work['input_sha256']:
            raise ValueError('Dependency input seal changed')
        if core.research_binding(self.database, work['input']['research_binding']['run_id']) != work['input']['research_binding']:
            raise ValueError('Dependency paused research binding changed')
        if core._inventory(self.directory / 'baseline') != work['input']['baseline_inventory']:
            raise ValueError('Dependency baseline changed')
        with closing(_core_readonly_connect((self.directory / 'work.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN')
            engine = SimpleNamespace(store=_ReadSnapshot(SimpleNamespace(path=self.directory / 'work.sqlite3'), db))
            stored = engine.store.get(core.COLLECTION, work['id'])
            if stored != work:
                raise ValueError('Dependency record changed during authentication')
            attempt = work['attempts'][-1]
            checks = attempt['checks']
            if (core._inventory(self.directory / f"candidate-{attempt['index']}") != attempt['inventory']
                    or core.digest(attempt['inventory']) != attempt['candidate_sha256']
                    or checks['candidate_sha256'] != attempt['candidate_sha256']):
                raise ValueError('Dependency candidate/check identity changed')
            if FIELD in work['input']:
                _validate_check_receipts(work, work['input'][FIELD])
            for item in checks['host_receipt']['artifacts']:
                path = Path(item['path'])
                _plain(path)
                if not path.is_relative_to(self.directory) or core._sha(path) != item['sha256']:
                    raise ValueError('Dependency check artifact changed')
            if checks['host_receipt']['passed']:
                reports = [json.loads(Path(item['path']).read_bytes()) for item in checks['host_receipt']['artifacts']
                           if Path(item['path']).name == Path(core.REPORT_PATH).name]
                if (len(reports) != 1 or reports[0].get('passed') is not True or
                        any(reports[0].get('source_sha256', {}).get(k) != v for k, v in attempt['inventory'].items())):
                    raise ValueError('Dependency passing report is not source-bound')
            ballots = attempt.get('reviews', {})
            for role, row in ballots.items():
                if role not in core.REVIEWERS:
                    raise ValueError('Foreign dependency reviewer')
                bound = core.authenticate(engine, work['run_id'], role, row['handoff_id'], row['result'])
                if (bound.get('work_order') != work['id'] or bound.get('work_order_input_sha256') != work['input_sha256']
                        or bound.get('candidate_sha256') != attempt['candidate_sha256'] or bound.get('checks') != checks
                        or row['result']['candidate_sha256'] != attempt['candidate_sha256']):
                    raise ValueError('Dependency independent review binding changed')
            approved = (checks['host_receipt']['passed'] is True and set(ballots) == set(core.REVIEWERS)
                        and all(r['result']['approve'] and not r['result']['required_changes'] for r in ballots.values()))
            if work['status'] == 'accepted_candidate':
                if not approved or attempt.get('outcome') != 'accepted_candidate':
                    raise ValueError('Dependency acceptance is not authenticated')
                return attempt
            if work['status'] != 'revision_required' or approved or attempt.get('outcome') != 'revise':
                raise ValueError('Dependency has no terminal reviewed outcome')
            return None

    def verify_external_feedback(self, work):
        if not self.read_only_dependency:
            return super().verify_external_feedback(work)
        if len(work['attempts']) != 1 or work['attempts'][0]['index'] != 0:
            raise ValueError('External dependency must have one original candidate')
        return self.authenticate_dependency(work)

    async def implement(self, work, attempt, candidate):
        contract = validate_contract(work, self.target_root)
        if not attempt.get('receipt'):
            suffix = '\nFrozen host CPU check contract:\n' + json.dumps(contract, ensure_ascii=False)
            if not attempt['instruction'].endswith(suffix):
                attempt['instruction'] += suffix
                attempt['instruction_sha256'] = core.digest(attempt['instruction'])
                self.save(work)
        await super().implement(work, attempt, candidate)

    async def review(self, work, attempt):
        validate_contract(work, self.target_root)
        return await super().review(work, attempt)

    async def run(self, work):
        if self.read_only_dependency:
            self.authenticate_dependency(work)
            return  # No Engine, Store initialization, execution or state writes.
        contract = validate_contract(work, self.target_root)
        original = core.to_thread

        async def checked_thread(function, *args, **kwargs):
            if function is not core._run_check:
                return await original(function, *args, **kwargs)
            stop = threading.Event()
            attempt = next(a for a in work['attempts']
                           if self.directory / f"candidate-{a['index']}" == args[0])
            attempt['check_execution'] = {'version': VERSION, 'state': 'running',
                'input_sha256': work['input_sha256'], 'contract_sha256': core.digest(contract),
                'candidate_sha256': core.digest(args[2]), 'output': str(args[1])}
            self.save(work)  # Crash/unknown completion is not a new execution grant.
            def retain(checks):
                attempt['checks'] = checks
                confirmed = checks['host_receipt'].get('process_cleanup_confirmed') is True
                attempt['check_execution'].update(state='terminal' if confirmed else 'unknown',
                                                  receipt_sha256=checks['receipt_sha256'])
                self.save(work)
                return confirmed
            def unknown(exc):
                attempt['check_execution'].update(state='unknown', error=type(exc).__name__ + ': ' + str(exc)[:1000])
                self.save(work)
            def cancelled():
                event = self.engine.cancellations.get(work['run_id'])
                return stop.is_set() or bool(event and event.is_set())
            task = asyncio.create_task(asyncio.to_thread(run_check, *args, contract, cancelled))
            try:
                checks = await asyncio.shield(task)
                if not retain(checks):
                    raise ValueError('Host check process cleanup is unconfirmed; execution blocked')
                return checks
            except asyncio.CancelledError as cancellation:
                stop.set()
                try:
                    checks = await finish(task, propagate_cancel=False)
                except BaseException as exc:
                    unknown(exc)
                    raise cancellation
                # The core assignment never receives a cancelled await's value.
                # Save its actual terminal check before propagating cancellation.
                retain(checks)
                raise
            except Exception as exc:
                unknown(exc)
                raise
        try:
            core.to_thread = checked_thread
            return await super().run(work)
        finally:
            core.to_thread = original


@contextmanager
def installed(root, order_id):
    """Scoped globals are supported only in this dedicated CLI process."""
    root = project_root(root)
    old = core.ROOT, core.DATA_DIR, core.WorkOrder, core.sqlite3
    class OrderPath(_OrderPath):
        order_root = root / core.ROOT_DIRECTORY
        primary = order_root / order_id
    class TargetWorkOrder(WorkOrder):
        target_root = root
        primary_directory = root / core.ROOT_DIRECTORY / order_id
        path_type = OrderPath
    try:
        core.ROOT, core.DATA_DIR, core.WorkOrder = OrderPath(root), root / 'data', TargetWorkOrder
        # Do not monkeypatch sqlite3 globally: Store retains ordinary writable
        # transactions only after CLI preflight. Core's explicit read paths use
        # the snapshot adapter, including status, bindings and dependencies.
        core.sqlite3 = SimpleNamespace(connect=_core_readonly_connect, Row=sqlite3.Row)
        yield TargetWorkOrder
    finally:
        core.ROOT, core.DATA_DIR, core.WorkOrder, core.sqlite3 = old


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, required=True)
    parser.add_argument('--check-time-policy', choices=(POLICY,), required=True)
    parser.add_argument('--worker-log', type=Path)
    options, remaining = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    import uuid
    if len(remaining) < 2 or remaining[0] not in ('submit', 'resume', 'status'):
        parser.error('Expected the ordinary submit/resume/status action and order UUID')
    order_id = uuid.UUID(remaining[1]).hex
    root = project_root(options.project_root)
    if options.worker_log:
        if remaining[0] == 'status':
            raise ValueError('Worker log is not supported for read-only status')
        log = options.worker_log
        _plain(log)
        if (not log.is_absolute() or '..' in log.parts or
                log.parent != root / core.ROOT_DIRECTORY / order_id):
            raise ValueError('Worker log must belong to the selected work-order directory')
        remaining += ['--worker-log', str(log)]
    if remaining[0] == 'resume':
        directory = root / core.ROOT_DIRECTORY / order_id
        if (directory / 'work.sqlite3').exists():
            raise ValueError('Legacy intake requires a new successor; policy upgrade is not supported')
        path = directory / STATE_DATABASE
        _plain(path)
        with closing(_core_readonly_connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            row = db.execute('SELECT data FROM records WHERE collection=? AND key=?',
                             (core.COLLECTION, order_id)).fetchone()
        if not row:
            raise ValueError('Work-order intake is missing')
        work = json.loads(row[0])
        validate_contract(work, root)  # Before Store or supplement writes.
        with installed(root, order_id):
            if core.research_binding(root / 'data/research.sqlite3',
                    work['input']['research_binding']['run_id']) != work['input']['research_binding']:
                raise ValueError('Frozen paused source assignment changed')
    original = sys.argv
    try:
        sys.argv = [original[0], *remaining]
        with installed(root, order_id):
            return core.main()
    finally:
        sys.argv = original


if __name__ == '__main__':
    main()
