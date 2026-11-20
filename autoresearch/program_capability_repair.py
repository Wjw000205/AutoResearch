"""Controller-routed isolated source repairs for authenticated capability defects.

The native WorkOrder owns implementation, CPU checks and independent review.
An accepted candidate is not deployment or scientific acceptance.
"""
from __future__ import annotations

import copy
import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from .metric_contract import signature
from . import independent_full_review as full
from . import metric_capability_evidence_recovery as recovery

SOURCE_ASSETS = frozenset({'autoresearch/ccf_catalog.json', 'autoresearch/official_model_prices.json'})


def checked_binding(store, database, run_id, receipt_id, *, allow_paused=False):
    from .engine import Engine
    engine = Engine.__new__(Engine)
    engine.store = store; engine.data_dir = Path(database).parent
    run = store.get('runs', run_id) or {}
    receipt = store.get('stage_contexts', receipt_id) or {}
    if (run.get('status') not in ({'running', 'paused'} if allow_paused else {'running'}) or run.get('stage') != 'implementation'
            or receipt.get('run_id') != run_id or receipt.get('id') != receipt_id
            or type(receipt.get('version')) is not int or receipt['version'] != 1
            or receipt.get('kind') != 'program_capability_repair' or receipt.get('scientific_approval') is not False
            or receipt.get('sha256') != signature({k: v for k, v in receipt.items() if k != 'sha256'})):
        raise ValueError('Source repair requires its controller-owned frozen defect receipt')
    root = store.get('metric_capability_reviews', receipt['root_review_id'])
    base = recovery.root_context(engine, run_id, root)
    vote = receipt['defect_vote']
    recovery.authenticate_lineage(engine, run_id, root, base, vote)
    result = full.answer(vote)
    plan = store.get('experiment_plans', run_id)
    if (result['resolution'] != 'repair_capability' or receipt['base_source_sha256'] != signature(base)
            or engine.cycle(run_id) != base['cycle'] or signature(plan) != base['plan_sha256']
            or run.get('state', {}).get('implementation_generation') != receipt['generation']):
        raise ValueError('Source repair does not bind the current controller plan/cycle and actual defect decision')
    if not allow_paused:
        from . import metric_runtime_scope as scope
        from .metric_contract_recovery import capability_sources
        if scope.KEY in base:
            unchanged = (signature(base['runtime_capability']) == base['current_capability_sha256']
                         and scope.identity(base['runtime_capability']) == base[scope.KEY]
                         and scope.identity(scope.sources()) == base[scope.KEY])
        else:
            unchanged = signature(capability_sources()) == base['current_capability_sha256']
        if not unchanged:
            raise ValueError('Source changed after the defect review; a fresh version review is required')
    return {'operation': 'metric_capability_source_repair_v1', 'run_id': run_id,
        'cycle': base['cycle'], 'generation': receipt['generation'],
        'repair_receipt': receipt_id, 'repair_receipt_sha256': receipt['sha256'],
        'capability_sha256': base['current_capability_sha256'], 'root_review_sha256': signature(root),
        'budget_limits': {k: run['config'][k] for k in ('max_calls', 'budget_usd') if k in run['config']}}


@recovery.operation
def binding(database, run_id, receipt_id):
    from .storage import _ReadSnapshot
    with sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        return checked_binding(_ReadSnapshot(SimpleNamespace(path=Path(database)), db), database, run_id, receipt_id)


def source_inventory(root):
    from .capability_deployment import source_inventory as inventory
    return inventory(root, SOURCE_ASSETS)


def intake_baseline(root, baseline, database, receipt_id):
    """An unfinished copy is never a baseline or a paid assignment."""
    from . import capability_work_orders as core
    from .codex_workspace import _plain
    with sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True) as db:
        raw = db.execute('SELECT data FROM records WHERE collection=? AND key=?', ('stage_contexts', receipt_id)).fetchone()
    receipt = json.loads(raw[0]) if raw else {}
    if receipt.get('sha256') != signature({k: v for k, v in receipt.items() if k != 'sha256'}):
        raise ValueError('Source intake has no authenticated frozen inventory')
    expected = receipt['source_inventory']
    _plain(baseline)
    if baseline.exists():
        if core._inventory(baseline) != expected: raise ValueError('Existing baseline is incomplete or belongs to another source')
        return expected
    if source_inventory(root) != expected:
        raise ValueError('Source inventory changed before intake; no copy or request was admitted')
    staging = baseline.parent / ('baseline-staging-' + uuid.uuid4().hex)
    actual = core._snapshot(root, staging)
    if actual != expected: raise ValueError('Source changed during isolated intake; unfinished source bytes retained')
    staging.rename(baseline)
    return expected


@contextmanager
def worker_lock(directory):
    """Use the same cross-process lock as CLI submit/resume, including intake."""
    from .codex_workspace import _plain
    _plain(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'worker.lock').open('a+b') as lock:
        if __import__('os').name == 'nt':
            import msvcrt
            lock.seek(0); msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            try: yield
            finally: lock.seek(0); msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try: yield
            finally: fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class BudgetOwnerView:
    """Read and mutate the same budget ledger used by actual reservations."""
    def __init__(self, local, parent, child_id, parent_id):
        self.local, self.parent, self.child_id, self.parent_id = local, parent, child_id, parent_id
    def __getattr__(self, name): return getattr(self.local, name)
    def get(self, collection, key, *args):
        if collection == 'budgets' and key == self.child_id: return self.parent.get(collection, self.parent_id, *args)
        if collection == 'usage': return self.parent.get(collection, key, *args)
        return self.local.get(collection, key, *args)
    def mutate(self, collection, key, callback):
        if collection == 'budgets' and key == self.child_id: return self.parent.mutate(collection, self.parent_id, callback)
        return self.local.mutate(collection, key, callback)


def validate_alias(work, local_store, database, run_id, receipt_id):
    alias = work['input'].get('source_run_alias')
    if (not alias or type(alias.get('version')) is not int or alias['version'] != 1
            or alias.get('run_id') != run_id or work['run_id'] != run_id
            or alias.get('parent_database') != str(Path(database).resolve()) or alias.get('work_order') != work['id']
            or alias.get('repair_receipt') != receipt_id
            or alias.get('research_binding_sha256') != signature(work['input']['research_binding'])
            or local_store.get('runs', run_id, {}).get('source_run_alias') != alias
            or not local_store.get('runs', alias['original_draft_id'])
            or any(local_store.record_keys(col, alias['original_draft_id']) for col in ('handoffs', 'decisions', 'usage'))):
        raise ValueError('Isolated source alias changed owner/binding or attempts to import an old paid draft')
    return alias
