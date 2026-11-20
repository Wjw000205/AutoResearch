"""Deploy accepted SOURCE work orders, from an accepted isolated candidate.

Dedicated CLI only: --root ABSOLUTE_CHECKOUT --order UUID [--order UUID]
and subsequently the same command with --check-installed. No Store, scheduler,
model, research mutation, reset or resume is used here. Installation is not
scientific acceptance. Unknown historical delivery formats fail closed.

Journal v1 is an immutable plan followed by immutable hash-chained events.
Prepared preimages precede every source mutation; a crash after replace and
before its event is reconciled only against that exact prepared postimage.
Failed/unknown checks are retained and never automatically rerun.

Impact: this is an opt-in, dedicated-process CLI, not a scheduler transition.
Existing input/review schemas and finite receipts are unchanged. Normal and
final-only completed review receipts are authenticated read-only; unknown
delivery/storage versions require a reviewed adapter. JUnit coverage v3 maps
actual cases to unique frozen files, with ancestor-root prefixes bound to the
host-created execution workspace; ambiguous/unsupported roots fail closed.
This version gets a new combined-check identity, retaining earlier receipts.
Win32 extended paths affect I/O only, not journal layout or canonical identity.
Failed-draft feedback carries accepted baseline ancestry, never draft approval.
Runtime check hooks are process-local
and must not be embedded in a concurrent server. Installation never restarts
the server or grants reset/resume. Host tests and independent review establish
capability acceptance separately from scientific or production acceptance.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager, ExitStack
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import tempfile
from types import FunctionType, SimpleNamespace
import uuid
from xml.etree import ElementTree

from jsonschema import ValidationError

from . import capability_checks as policy, capability_work_orders as core
from . import working_context
from .code_verification import checked_path as core_checked_path
from .codex_workspace import _plain as plain_path
from .delivery_files import io_path
from .engine import ResearchBlocked
from .storage import _ReadSnapshot

VERSION = 'source_deployment_v1'
CHECK_VERSION = 'installed_source_check_v1'
JUNIT = 'host-junit.xml'
JUNIT_ROOT = 'host-junit-root.json'
JUNIT_ROOT_VERSION = 'host_junit_execution_root_v1'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def _plain(path):
    # Keep canonical paths in contracts. Extended paths are only an I/O adapter,
    # including link checks: MAX_PATH must never look like an absent preimage.
    plain_path(io_path(path))
    for part in (path, *path.parents):
        part = io_path(part)
        if part.exists():
            info = part.lstat()
            require(not getattr(info, 'st_file_attributes', 0) & 0x400, 'Reparse path is not permitted')
            require(not part.is_file() or info.st_nlink == 1, 'Hard-linked file is not permitted')


def canonical_path(path):
    """Remove only the Win32 I/O prefix, never resolve away a link."""
    value = str(path)
    if os.name == 'nt':
        if value.startswith('\\\\?\\UNC\\'):
            value = '\\\\' + value[8:]
        elif value.startswith('\\\\?\\'):
            value = value[4:]
    return Path(value)


def checked_path(root, name):
    core_checked_path(io_path(root), name)
    path = root / name
    _plain(path)
    return path


def inventory_at(root):
    _plain(root)
    return core._inventory(io_path(root))


def file_hash(path):
    _plain(path)
    path = io_path(path)
    if not path.exists():
        return None
    require(path.is_file(), 'Expected a regular file: ' + str(path))
    return core._sha(path)


def python_path(name):
    p = PurePosixPath(name)
    require(isinstance(name, str) and p.as_posix() == name and len(p.parts) >= 2
            and p.parts[0] in {'autoresearch', 'tests'} and p.suffix == '.py'
            and all(re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', s) for s in p.parts)
            and all(not s.endswith('.') and not re.fullmatch(r'(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])',
                                                             s.split('.')[0]) for s in p.parts)
            and not any(s in {'.', '..', 'data', 'credentials', '.git'} for s in p.parts)
            and name not in {'autoresearch/config.py', 'autoresearch/capability_work_orders.py', 'tests/conftest.py'},
            'Deployment requires an exact allowed Python path')
    return name


def test_paths(tests, inventory):
    require(isinstance(tests, list) and tests and len(tests) == len(set(tests)), 'Missing/duplicate selected tests')
    for selector in tests:
        name, *nodes = selector.split('::')
        python_path(name)
        require(name.startswith('tests/test_') and name in inventory
                and all(n and '\\' not in n and '/' not in n for n in nodes), 'Missing or unsafe selected test')


def target_root(value):
    # Never use config.ROOT: the importing candidate is deliberately elsewhere.
    # Reuse the accepted root validator with extended I/O, then keep its resolved
    # canonical spelling in all frozen inputs and journal identities.
    path = canonical_path(value)
    require(path.is_absolute() and '..' not in path.parts, 'Expected an explicit absolute checkout')
    return canonical_path(policy.project_root(io_path(path)))


@contextmanager
def read_database(path):
    _plain(path)
    require(io_path(path).is_file(), 'Missing read-only database')
    # Same DB+WAL stability rule as the accepted adapter. Cleanup is deliberately
    # exact-file-only here: deployment never recursively removes a directory.
    workspace = Path(tempfile.mkdtemp(prefix='autoresearch-deployment-read-')).absolute()
    _plain(workspace)
    identity = io_path(workspace).stat()
    target = workspace / 'read.sqlite3'
    try:
        before = {}
        for source in (path, Path(str(path) + '-wal')):
            _plain(source)
            if io_path(source).exists():
                info = io_path(source).stat()
                before[source] = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                shutil.copyfile(io_path(source), io_path(target if source == path else Path(str(target) + '-wal')))
            else:
                before[source] = None
        for source, saved in before.items():
            _plain(source)
            if not io_path(source).exists():
                require(saved is None, 'Read-only database snapshot changed')
                continue
            info = io_path(source).stat()
            copied = target if source == path else Path(str(target) + '-wal')
            require(saved == (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                    and file_hash(source) == file_hash(copied), 'Read-only database snapshot changed')
        with closing(sqlite3.connect(target.as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            yield _ReadSnapshot(SimpleNamespace(path=path), db)
    finally:
        _plain(workspace)
        info = io_path(workspace).stat()
        require((info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino), 'Read snapshot workspace ownership changed')
        for name in ('read.sqlite3-shm', 'read.sqlite3-wal', 'read.sqlite3'):
            owned = workspace / name
            _plain(owned)
            if io_path(owned).is_file():
                io_path(owned).unlink()
        io_path(workspace).rmdir()  # Unknown contents are retained, never recursively cleaned.


def paused_binding(root, binding, *, journal=None):
    database = root / 'data/research.sqlite3'
    with read_database(database) as store:
        require(store.get('runs', binding['run_id'], {}).get('status') == 'paused', 'Research is not paused')
        require(not store.get('runtime', 'worker', {}).get('held'), 'Research runtime lease is held')
        if binding.get('operation') == 'metric_capability_source_repair_v1':
            from .program_capability_repair import checked_binding
            require(checked_binding(store, database, binding['run_id'], binding['repair_receipt'], allow_paused=True) == binding,
                    'Paused capability source-repair binding changed')
            receipt = store.get('stage_contexts', binding['repair_receipt'])
            expected = receipt['source_inventory']
            from .program_capability_repair import SOURCE_ASSETS
            actual = source_inventory(root, SOURCE_ASSETS)
            if journal is not None:
                require(journal.root == root and journal.spec['research_binding'] == binding,
                        'Capability repair postimages require their exact authenticated deployment journal')
            changes = journal.spec['changes'] if journal is not None else {}
            for name in set(expected) | set(actual):
                if actual.get(name) == expected.get(name): continue
                require(name in changes and journal.has('prepared', name)
                        and actual.get(name) == changes[name]['after'],
                        'Capability source changed outside its frozen intake or prepared deployment: ' + name)
            return
        # Reuse the owning controller authentication against the SAME snapshot.
        # A cloned globals dict avoids process-global sqlite monkeypatching.
        class Connection:
            def __getattr__(self, key):
                return getattr(store._connection, key)
            def __setattr__(self, key, value):
                setattr(store._connection, key, value)
            def close(self):
                pass
            def execute(self, sql, *args):
                if sql == 'BEGIN':
                    return store._connection.execute('SELECT 1')
                return store._connection.execute(sql, *args)
        globals_ = {**core.research_binding.__globals__,
                    'sqlite3': SimpleNamespace(connect=lambda *a, **k: Connection(), Row=sqlite3.Row)}
        read = FunctionType(core.research_binding.__code__, globals_)
        read.__kwdefaults__ = core.research_binding.__kwdefaults__
        require(read(database, binding['run_id']) == binding, 'Paused controller failure binding changed')


def _paused_for_journal(journal):
    binding = journal.spec['research_binding']
    if binding.get('operation') == 'metric_capability_source_repair_v1':
        return paused_binding(journal.root, binding, journal=journal)
    return paused_binding(journal.root, binding)


def database_signature(root):
    """Cheap mutation sentinel between full, authenticated read snapshots."""
    database = root / 'data/research.sqlite3'
    result = []
    for path in (database, Path(str(database) + '-wal')):
        _plain(path)
        info = io_path(path).stat() if io_path(path).exists() else None
        result.append((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) if info else None)
    return result


def storage_path(directory):
    found = []
    for found_path in io_path(directory).glob('*.sqlite3'):
        path = directory / found_path.name
        _plain(path)
        require(path.name in {'work.sqlite3', policy.STATE_DATABASE}, 'Unknown work-order storage')
        found.append(path)
    require(len(found) == 1, 'Missing or ambiguous work-order storage')
    return found[0]


def native_receipt(store, work, attempt, candidate):
    from .codex_accounting import usage_record
    receipt = attempt['receipt']
    instruction = attempt['instruction']
    native = work['input']['implementation']
    require(native.get('backend') == 'local_codex' and native.get('contract') ==
            'isolated_source_tree_with_git_diff_and_host_selected_tests_v1', 'Unknown implementation contract')
    require(attempt['instruction_sha256'] == core.digest(instruction), 'Implementation instruction seal changed')
    frozen, _ = json.JSONDecoder().raw_decode(instruction.split('\n', 1)[1])
    for field, source in [('requirement', 'operator_requirement'), ('research_binding', 'research_binding'),
                          ('allowed_paths', 'allowed_paths'), ('tests', 'tests'),
                          ('dependency_result', 'dependency_result'), ('dependency_feedback', 'dependency_feedback')]:
        require(frozen.get(field) == work['input'].get(source), 'Implementation frozen input changed: ' + field)
    expected = {'run_id': work['run_id'], 'operation_id': attempt['operation_id'],
                'workspace': str(candidate), 'project_root': str(candidate),
                'instruction_sha256': hashlib.sha256(instruction.encode()).hexdigest(),
                'requested_model': native['model'], 'requested_effort': native['effort'], 'status': 'completed'}
    require(all(receipt.get(k) == v for k, v in expected.items()) and receipt.get('thread_id')
            and receipt.get('turn_id'), 'Original completed native implementation receipt mismatch')
    observed = usage_record(work['run_id'], receipt)
    saved = store.get('codex_usage', observed['id'], {})
    require(all(saved.get(k) == v for k, v in observed.items()), 'Native receipt differs from original usage observation')
    ledger = store.get('budgets', work['run_id'], {})
    settled = [r for r in ledger.get('records', []) if r.get('operation_id') == attempt['operation_id']]
    require(len(settled) == 1 and not any(r.get('operation_id') == attempt['operation_id']
                for r in ledger.get('pending', {}).values()), 'Native implementation settlement is not terminal')
    settlement = settled[0]
    require(all(settlement.get(k) == v for k, v in {'run_id': work['run_id'], 'role': 'engineer',
                'provider': 'local_codex', 'model': native['model'], 'status': 'completed',
                'thread_id': receipt['thread_id'], 'turn_id': receipt['turn_id'],
                'provenance': 'local_codex_chatgpt', 'billing_scope': 'chatgpt_subscription'}.items())
            and store.get('usage', settlement['id']) == settlement, 'Native receipt settlement identity changed')
    runtime = attempt.get('execution_runtime', {})
    require(runtime.get('executable') and re.fullmatch('[0-9a-f]{64}', runtime.get('sha256', '')),
            'Missing frozen native runtime identity')
    executable = Path(runtime['executable'])
    if io_path(executable).exists():
        require(file_hash(executable) == runtime['sha256'], 'Frozen native executable changed')


def authenticate_checks(checks, inventory, tests, directory, contract=None, *, passing=True):
    require(checks.get('candidate_sha256') == core.digest(inventory) and checks.get('test_paths') == tests,
            'Host check source or selected tests changed')
    test_paths(tests, inventory)
    limits = checks.get('limits', {})
    require(limits.get('gpu_allowed') is False and limits.get('cpu_cores') == 2
            and limits.get('memory_mb') == 1536 and limits.get('disk_mb') == 256,
            'Host CPU resource gates changed')
    if contract:
        require(checks.get('receipt_version') == policy.VERSION and checks.get(policy.FIELD) == contract
                and checks.get('receipt_sha256') == core.digest({k: v for k, v in checks.items() if k != 'receipt_sha256'})
                and limits.get('cpu_seconds', 'missing') is None, 'No-duration check contract changed')
        require(checks.get('python_sha256') == contract['runtime']['python']['sha256']
                and checks.get('python_version') == contract['runtime']['python']['version']
                and Path(limits['python_executable']).resolve() == Path(contract['runtime']['python']['path']).resolve(),
                'Check Python runtime changed')
    else:
        require(policy.FIELD not in checks and 'receipt_version' not in checks
                and limits.get('cpu_seconds') == 600, 'Unknown legacy finite check contract')
    require(isinstance(checks.get('test_dependencies'), dict) and checks['test_dependencies']
            and re.fullmatch('[0-9a-f]{64}', checks.get('python_sha256', '')), 'Missing host dependency/runtime provenance')
    host = checks['host_receipt']
    require(host.get('command') == [limits.get('python_executable'), '-I', core.CHECK_PATH],
            'Host check command differs from fixed CPU driver')
    require(not contract or host.get('process_cleanup_confirmed') is True, 'Check process cleanup is unconfirmed')
    require('process_cleanup_confirmed' not in host or host['process_cleanup_confirmed'] is True,
            'Check process cleanup failed')
    artifacts, reports = set(), []
    for item in host.get('artifacts', []):
        path = canonical_path(item['path'])
        _plain(path)
        require(path.is_absolute() and path.is_relative_to(canonical_path(directory)) and str(path) not in artifacts
                and file_hash(path) == item['sha256'], 'Host check artifact changed or escaped order')
        artifacts.add(str(path))
        if path.name == Path(core.REPORT_PATH).name:
            reports.append(json.loads(io_path(path).read_bytes()))
    require(any(Path(p).name == 'stdout.txt' for p in artifacts)
            and any(Path(p).name == 'stderr.txt' for p in artifacts), 'Missing original CPU output artifacts')
    if passing:
        require(host.get('passed') is True and host.get('status') == 'completed' and host.get('exit_code') == 0
                and not host.get('error') and not host.get('cleanup_diagnostics'), 'Host check did not pass')
        require(len(reports) == 1 and reports[0].get('passed') is True and reports[0].get('exit_code') == 0
                and reports[0].get('tests_collected', 0) > 0, 'Missing genuinely passing CPU report')
        report = reports[0]
        require(host.get('test_reports', {}).get(core.REPORT_PATH) == report
                and all(report.get('source_sha256', {}).get(k) == v for k, v in inventory.items())
                and core.CHECK_PATH in report.get('source_sha256', {}), 'Host report source inventory changed')
    return core.digest(checks)


def authenticate_review(store, work, attempt, role):
    from jsonschema import Draft202012Validator
    row = attempt['reviews'][role]
    key, result = row['handoff_id'], row['result']
    context = core.authenticate(SimpleNamespace(store=store), work['run_id'], role, key, result)
    expected = {'work_order': work['id'], 'work_order_input_sha256': work['input_sha256'],
                'requirement': work['input']['operator_requirement'], 'research_binding': work['input']['research_binding'],
                'baseline_sha256': core.digest(work['input']['baseline_inventory']),
                'candidate_sha256': attempt['candidate_sha256'], 'changed_paths': attempt['changed_paths'],
                'unified_diff': attempt['diff'], 'checks': attempt['checks']}
    require(context == expected, 'Independent review does not bind the full source/check input')
    hand = store.get('handoffs', key)
    require(hand['purpose'] == f"source_capability_review:{work['id']}:{attempt['index']}:{attempt['candidate_sha256']}",
            'Wrong independent review phase')
    schema = store.get('handoff_inputs', key)['schema']
    fields = {'candidate_sha256': {'const': attempt['candidate_sha256']}, 'approve': {'type': 'boolean'},
              'rationale': {'type': 'string', 'minLength': 1},
              'required_changes': {'type': 'array', 'items': {'type': 'string', 'minLength': 1}}}
    require(schema == {'type': 'object', 'properties': fields, 'required': list(fields), 'additionalProperties': False},
            'Unknown review output schema')
    Draft202012Validator(schema).validate(result)
    progress = store.get('handoff_progress', key, {})
    slot = progress.get('finalization' if progress.get('mode') == 'final_only' else 'normal_request', {})
    prepared, response = slot.get('prepared', {}), slot.get('response', {})
    # Historical final-only slots bind prepared bytes via the working commit;
    # ordinary request slots additionally carry their own prepared seal.
    require(progress.get('mode') == 'final_only' or slot.get('prepared_sha256') == core.digest(prepared),
            'Normal review request is missing its prepared seal')
    require(progress.get('id') == key and progress.get('run_id') == work['run_id']
            and slot.get('status') == 'response_saved' and response.get('json') == result
            and response.get('request_id') and prepared.get('role') == role
            and slot.get('prepared_sha256', core.digest(prepared)) == core.digest(prepared),
            'Missing completed review request/response binding')
    require(slot.get('provider_id') == work['input']['review']['backend'] == 'local_codex'
            and slot.get('model') == work['input']['review']['model'], 'Review frozen routing changed')
    calls = [c for c in hand.get('model_calls', []) if c.get('request_id') == response['request_id']]
    require(len(calls) == 1 and all(calls[0].get(k) == response.get(k) for k in
            ('provider', 'model', 'transcript_id', 'provenance')) and response.get('provenance') == 'local_codex_chatgpt',
            'Review model-call receipt changed')
    transcript = store.get('transcripts', response.get('transcript_id'), {})
    require(all(transcript.get(k) == v for k, v in {'id': response.get('transcript_id'),
                'run_id': work['run_id'], 'role': role, 'kind': 'output', 'request_id': response['request_id'],
                'provider': 'local_codex', 'model': slot['model'], 'text': response.get('text')}.items())
            and isinstance(transcript.get('text'), str), 'Original review output transcript changed')
    from .intelligence import parse_provider_json
    raw, _ = parse_provider_json(transcript['text'], prepared['schema'], transcript.get('response_metadata', {}))
    require(raw == result, 'Original review response does not parse to the saved verdict')
    require(all(prepared.get('context', {}).get(k) == v for k, v in expected.items()),
            'Review prepared source context changed')
    Draft202012Validator(prepared['schema']).validate(result)
    saved = working_context.validate_commit(store, work['run_id'], role,
                                            working_context.marker(work['run_id'], role, key, progress))
    require(saved and saved['id'] == f"{work['run_id']}:context-commit:{saved['sha256']}"
            and saved['payload']['prepared_sha256'] == working_context.sha(prepared)
            and saved['payload']['output'] == result and saved['payload']['response_id'] == response['request_id']
            and saved['payload']['receipt_ids'] == [working_context.receipt_id(work['run_id'], r)
                                                  for r in prepared['context'].get('tool_results', [])],
            'Review working-context receipt binding changed')
    require(result['approve'] is True and result['required_changes'] == []
            and result['candidate_sha256'] == attempt['candidate_sha256'], 'Independent review did not approve')
    return {'handoff_id': key, 'input_sha256': hand['input_hash'], 'decision_sha256': core.digest(result),
            'progress_sha256': core.digest(progress), 'commit_sha256': saved['sha256'],
            'transcript_sha256': core.digest(transcript)}


class Sources:
    """Read-only graph authentication. Feedback ancestors never enter adopted."""
    def __init__(self, root):
        self.root = root
        self.nodes = {}
        self.adopted = []
        self.visiting = set()

    def load(self, order_id, *, accepted=True):
        require(isinstance(order_id, str) and re.fullmatch('[0-9a-f]{32}', order_id), 'Expected a source order UUID')
        require(order_id not in self.visiting, 'Cyclic source dependency')
        if order_id in self.nodes:
            node = self.nodes[order_id]
            require(not accepted or node['work']['status'] == 'accepted_candidate', 'Nonaccepted adopted source')
            if accepted and order_id not in self.adopted:
                self.adopted.append(order_id)
            return node
        self.visiting.add(order_id)
        directory = self.root / core.ROOT_DIRECTORY / order_id
        _plain(directory)
        dbpath = storage_path(directory)
        with read_database(dbpath) as store, ExitStack() as stack:
            work = store.get(core.COLLECTION, order_id)
            require(work and work.get('id') == order_id and core.digest(work['input']) == work['input_sha256'],
                    'Operator input seal changed')
            inputs = work['input']
            if inputs.get('source_run_alias'):
                from .program_capability_repair import BudgetOwnerView, validate_alias
                database = self.root / 'data/research.sqlite3'
                validate_alias(work, store, database, work['run_id'], inputs['research_binding']['repair_receipt'])
                parent = stack.enter_context(read_database(database))
                store = BudgetOwnerView(store, parent, work['run_id'], work['run_id'])
            require(inputs.get('version') == 1 and not inputs.get('review_only_source'),
                    'Unsupported source intake; original native implementation required')
            require(inputs.get('review', {}).get('roles') == list(core.REVIEWERS)
                    and inputs['review'].get('contract') == 'source_bound_harness_verdict_v1', 'Unknown review contract')
            allowed = inputs['allowed_paths']
            require(allowed and len(allowed) == len({n.casefold() for n in allowed}), 'Invalid allowed scope')
            for name in allowed:
                python_path(name)
            baseline = inputs['baseline_inventory']
            require(baseline and inventory_at(directory / 'baseline') == baseline, 'Original baseline changed')
            require(baseline.get('autoresearch/capability_work_orders.py') in policy.SUPPORTED_CORE_SHA256S,
                    'Unsupported owning work-order runtime')
            for name, value in baseline.items():
                require(file_hash(checked_path(directory / 'baseline', name)) == value, 'Baseline file changed')
            contract = inputs.get(policy.FIELD)
            require((dbpath.name == policy.STATE_DATABASE) == (contract is not None), 'Storage/version mismatch')
            if contract:
                require(contract.get('version') == policy.VERSION and contract.get('state_database') == policy.STATE_DATABASE
                        and contract.get('policy') == {'version': policy.POLICY, 'wall_seconds': None, 'cpu_seconds': None}
                        and contract.get('execution_platform') == 'windows_job_v1'
                        and contract.get('project_root') == str(self.root)
                        and contract.get('database') == str(self.root / 'data/research.sqlite3'), 'Unknown frozen check policy/target')
                runtime = contract['runtime']
                runtime_root = Path(runtime['source_root'])
                _plain(runtime_root)
                require(runtime_root.is_relative_to(self.root / core.ROOT_DIRECTORY), 'Check runtime is not isolated source')
                runtime_inventory = inventory_at(runtime_root / 'autoresearch')
                require(core.digest({k: v for k, v in runtime_inventory.items() if k.endswith('.py')}) ==
                        runtime['source_tree_sha256'], 'Frozen check runtime source changed')
                require(set(runtime['modules']) == {'capability_checks.py', 'capability_work_orders.py',
                        'code_verification.py', 'disk_usage.py', 'lab.py', 'async_ownership.py',
                        'harness_delivery.py', 'engine.py', 'storage.py', 'config.py', 'local_codex.py',
                        'codex_accounting.py'}, 'Incomplete frozen check runtime modules')
                for name, identity in runtime['modules'].items():
                    require(Path(identity['path']) == runtime_root / 'autoresearch' / name
                            and file_hash(Path(identity['path'])) == identity['sha256'], 'Frozen check runtime module changed')
                policy._validate_check_receipts(work, contract)
            require(work.get('attempts'), 'No completed implementation')
            require([a.get('index') for a in work['attempts']] == list(range(len(work['attempts'])))
                    and len(work['attempts']) <= core.MAX_ATTEMPTS, 'Unknown implementation attempt sequence')
            attempt = work['attempts'][-1]
            require(type(attempt.get('index')) is int and attempt['index'] >= 0, 'Invalid candidate index')
            candidate = directory / f"candidate-{attempt['index']}"
            inventory = inventory_at(candidate)
            require(inventory == attempt['inventory'] and core.digest(inventory) == attempt['candidate_sha256'],
                    'Accepted candidate inventory changed')
            for name, value in inventory.items():
                require(file_hash(checked_path(candidate, name)) == value, 'Candidate file changed')
            changed, patch = core._review_diff(io_path(directory / 'baseline'), io_path(candidate), baseline, inventory, allowed)
            require(changed == attempt['changed_paths'] and patch == attempt['diff'], 'Reviewed source diff changed')
            native_receipt(store, work, attempt, candidate)
            reviews = {}
            if accepted:
                require(work['status'] == attempt.get('outcome') == 'accepted_candidate', 'Nonaccepted adopted source')
                authenticate_checks(attempt['checks'], inventory, inputs['tests'], directory, contract)
                require(store.get('runs', work['run_id'], {}).get('status') == 'completed', 'Work-order run is not terminal')
                require(set(attempt.get('reviews', {})) == set(core.REVIEWERS), 'Both independent reviews are required')
                reviews = {role: authenticate_review(store, work, attempt, role) for role in core.REVIEWERS}
            else:
                require(work['status'] in {'revision_required', 'blocked'}
                        and attempt.get('outcome') != 'accepted_candidate', 'Unknown failed-draft provenance')
                require(attempt.get('checks', {}).get('candidate_sha256') == attempt['candidate_sha256'],
                        'Unsealed failed draft check identity')
            node = {'work': work, 'attempt': attempt, 'candidate': candidate, 'directory': directory,
                    'link': {'order': order_id, 'storage': dbpath.name, 'input_sha256': work['input_sha256'],
                             'candidate_sha256': attempt['candidate_sha256'], 'native_receipt_sha256': core.digest(attempt['receipt']),
                             'checks_sha256': core.digest(attempt.get('checks')), 'reviews': reviews}, 'ancestors': set()}
        predecessor = inputs.get('after_work_order')
        if predecessor:
            result = inputs['dependency_result']
            adopted = result.get('candidate_sha256') is not None
            parent = self.load(predecessor, accepted=adopted)
            previous = parent['work']
            require(previous['input']['research_binding'] == inputs['research_binding']
                    and result == {'status': previous['status'], 'input_sha256': previous['input_sha256'],
                                   'candidate_sha256': parent['attempt']['candidate_sha256'] if adopted else None,
                                   'error': previous.get('error')}, 'Dependency result provenance changed')
            require(baseline == (parent['attempt']['inventory'] if adopted else previous['input']['baseline_inventory']),
                    'Dependency baseline is not the adopted source inventory')
            feedback_attempts = previous['attempts'][-1:] if inputs.get('revise_draft') else previous['attempts']
            feedback = [{k: a.get(k) for k in ('candidate_sha256', 'changed_paths', 'reviews', 'outcome')} |
                        {'implementation_receipt_sha256': core.digest(a['receipt'])} for a in feedback_attempts]
            require(inputs.get('dependency_feedback') == feedback, 'Dependency feedback provenance changed')
            if inputs.get('revise_draft'):
                draft = parent['attempt']
                require(not adopted and previous['status'] == 'revision_required' and draft.get('outcome') == 'revise'
                        and inputs.get('draft') == {k: draft[k] for k in ('index', 'inventory', 'candidate_sha256', 'checks')}
                        and set(draft['changed_paths']) <= set(allowed), 'Failed draft revision binding changed')
            require(not inputs.get('require_accepted_dependency') or adopted, 'Required dependency was not accepted')
            if adopted:
                node['ancestors'] = parent['ancestors'] | {predecessor}
            else:
                # The failed implementation itself supplies feedback, not code.
                # Its ORIGINAL baseline may nevertheless contain accepted source.
                # load() authenticated that chain and the exact inventory above;
                # carry only those accepted ancestors across the feedback edge.
                node['ancestors'] = set(parent['ancestors'])
        self.visiting.remove(order_id)
        self.nodes[order_id] = node
        if accepted:
            self.adopted.append(order_id)
        return node

    def composition(self):
        changes = {}
        bindings = []
        for oid in self.adopted:
            node = self.nodes[oid]
            inputs, attempt = node['work']['input'], node['attempt']
            bindings.append(inputs['research_binding'])
            for name in attempt['changed_paths']:
                old, new = inputs['baseline_inventory'].get(name), attempt['inventory'].get(name)
                if name in changes:
                    prior = changes[name]
                    require(prior['after'] == new and prior['before'] == old
                            or prior['owner'] in node['ancestors'] and prior['after'] == old,
                            'Conflicting reviewed source overlap: ' + name)
                    prior['admissible'].append(new)
                    prior.update(after=new, owner=oid)
                else:
                    changes[name] = {'before': old, 'after': new, 'owner': oid, 'admissible': [old, new]}
        require(bindings and all(b == bindings[0] for b in bindings), 'Source orders bind different research failures')
        require(len(changes) == len({n.casefold() for n in changes}), 'Case-aliased deployment paths')
        return changes, bindings[0]

    def revalidate(self):
        fresh = Sources(self.root)
        for oid in self.adopted:
            fresh.load(oid)
        require(fresh.adopted == self.adopted and fresh.composition() == self.composition()
                and all(fresh.nodes[oid]['link'] == self.nodes[oid]['link'] for oid in self.nodes),
                'Accepted source authorization changed during deployment/check')


@contextmanager
def deployment_lock(root):
    path = root / core.ROOT_DIRECTORY / 'deployment.lock'
    _plain(path)
    with io_path(path).open('a+b') as handle:
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def sync_directory(path):
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def replace(source, target):
    _plain(source)
    _plain(target)
    if os.name == 'nt':
        # MOVEFILE_WRITE_THROUGH makes metadata publication durable on Windows.
        import ctypes
        move = ctypes.WinDLL('kernel32', use_last_error=True).MoveFileExW
        move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        move.restype = ctypes.c_int
        if not move(str(io_path(source)), str(io_path(target)), 0x1 | 0x8):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.replace(source, target)
        sync_directory(target.parent)


def publish_bytes(path, data):
    """Never overwrite a published artifact, including preimages and receipts."""
    _plain(path)
    if io_path(path).exists():
        require(io_path(path).read_bytes() == data, 'Immutable deployment evidence changed: ' + str(path))
        return
    io_path(path.parent).mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + '.pending-' + uuid.uuid4().hex)
    with io_path(pending).open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    replace(pending, path)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode('utf-8')


def publish(path, value):
    publish_bytes(path, encode(value))


def read_json(path):
    _plain(path)
    return json.loads(io_path(path).read_bytes())


class Journal:
    def __init__(self, directory, spec, root):
        self.directory, self.spec, self.root = directory, spec, root
        _plain(directory)
        plan_path = directory / 'plan.json'
        if io_path(plan_path).exists():
            self.plan = read_json(plan_path)
            require(self.plan.get('spec') == spec, 'Deployment replay source/target binding changed')
        else:
            preimages = {name: file_hash(checked_path(root, name)) for name in spec['changes']}
            for name, value in preimages.items():
                require(value in spec['changes'][name]['admissible'], 'Target baseline drift: ' + name)
            self.plan = {'spec': spec, 'preimages': preimages}
            publish(plan_path, self.plan)
        self.plan_sha = core.digest(self.plan)
        require(set(self.plan.get('preimages', {})) == set(spec['changes']) and all(
                value in spec['changes'][name]['admissible'] for name, value in self.plan['preimages'].items()),
                'Deployment preimage contract changed')
        self.events = []
        events_dir = directory / 'events'
        _plain(events_dir)
        for path in sorted(io_path(events_dir).glob('*.json')) if io_path(events_dir).exists() else []:
            event = read_json(path)
            require(path.name == f'{len(self.events):06}.json' and event.get('plan_sha256') == self.plan_sha
                    and event.get('previous') == (core.digest(self.events[-1]) if self.events else None),
                    'Deployment journal event chain changed')
            phase = event.get('phase')
            require(phase in {'prepared', 'applied', 'installed', 'failure'}, 'Unknown journal phase')
            if phase in {'prepared', 'applied'}:
                name = event.get('path')
                require(name in spec['changes'] and event.get('after') == spec['changes'][name]['after'],
                        'Journal source hash binding changed')
                require(phase != 'prepared' or event.get('before') == self.plan['preimages'][name],
                        'Journal preimage binding changed')
                require(phase != 'applied' or self.has('prepared', name), 'Unprepared source publication')
            if phase == 'installed':
                require(all(self.has('applied', n) for n in spec['changes']) and event.get('installed_sha256') ==
                        core.digest({n: c['after'] for n, c in spec['changes'].items()}), 'Premature installation event')
            self.events.append(event)
        if (io_path(directory / 'audit.json')).exists():
            require(self.has('installed') and read_json(directory / 'audit.json') == self.audit(),
                    'Immutable installation audit changed')

    def audit(self):
        return {'version': VERSION, 'plan_sha256': self.plan_sha, 'spec_sha256': core.digest(self.spec),
                'installed_event_sha256': core.digest(next(e for e in self.events if e['phase'] == 'installed')),
                'status': 'installed', 'scientific_acceptance': False}

    def event(self, phase, **fields):
        event = {'phase': phase, 'plan_sha256': self.plan_sha,
                 'previous': core.digest(self.events[-1]) if self.events else None, **fields}
        publish(self.directory / 'events' / f'{len(self.events):06}.json', event)
        self.events.append(event)

    def has(self, phase, name=None):
        return any(e['phase'] == phase and (name is None or e.get('path') == name) for e in self.events)

    def verify_files(self):
        for name, change in self.spec['changes'].items():
            require(file_hash(checked_path(self.root, name)) == change['after'], 'Installed source drift: ' + name)
            for category, value in [('preimages', self.plan['preimages'][name]), ('staged', change['after'])]:
                if value is not None:
                    require(file_hash(self.directory / category / value) == value, 'Deployment backup/staged artifact changed')

    def install(self, sources):
        research_files = database_signature(self.root)
        _paused_for_journal(self)
        require(database_signature(self.root) == research_files, 'Research changed during installation preflight')
        # Prepare ALL immutable bytes before changing even one target file.
        for name, change in self.spec['changes'].items():
            current = file_hash(checked_path(self.root, name))
            pre = self.plan['preimages'][name]
            require(not self.has('applied', name) or current == change['after'],
                    'Previously installed source drift: ' + name)
            require(current == pre or self.has('prepared', name) and current == change['after'],
                    'Target changed outside this journal: ' + name)
            if pre is not None:
                backup = self.directory / 'preimages' / pre
                if not io_path(backup).exists():
                    require(current == pre, 'Missing original preimage')
                    publish_bytes(backup, io_path(checked_path(self.root, name)).read_bytes())
                require(file_hash(backup) == pre, 'Preimage backup changed')
            if change['after'] is not None:
                source = checked_path(sources.nodes[change['owner']]['candidate'], name)
                data = io_path(source).read_bytes()
                require(hashlib.sha256(data).hexdigest() == change['after'], 'Accepted source changed during staging')
                publish_bytes(self.directory / 'staged' / change['after'], data)
        for name, change in self.spec['changes'].items():
            require(database_signature(self.root) == research_files, 'Research changed during installation')
            path = checked_path(self.root, name)
            if not self.has('prepared', name):
                self.event('prepared', path=name, before=self.plan['preimages'][name], after=change['after'])
            current = file_hash(path)
            require(current in {self.plan['preimages'][name], change['after']}, 'Target drift before atomic replacement')
            if current != change['after']:
                if change['after'] is None:
                    io_path(path).unlink()  # One explicitly reviewed deletion, never recursive.
                    sync_directory(path.parent)
                else:
                    io_path(path.parent).mkdir(parents=True, exist_ok=True)
                    temp = self.directory / ('replace-' + uuid.uuid4().hex)
                    publish_bytes(temp, (io_path(self.directory / 'staged' / change['after'])).read_bytes())
                    replace(temp, path)
            require(file_hash(path) == change['after'], 'Installed hash mismatch')
            if not self.has('applied', name):
                self.event('applied', path=name, after=change['after'])
        self.verify_files()
        _paused_for_journal(self)
        sources.revalidate()
        if not self.has('installed'):
            self.event('installed', installed_sha256=core.digest({n: c['after'] for n, c in self.spec['changes'].items()}))
        audit = self.audit()
        publish(self.directory / 'audit.json', audit)
        return audit


def source_inventory(root, assets=()):
    """Only the source snapshot surface, never a recursive checkout inventory."""
    result = {}
    for name in core._source_paths(root):
        p = PurePosixPath(name)
        require(name in {'AGENTS.md', 'pyproject.toml'} or p.parts[0] in {'autoresearch', 'tests'},
                'Snapshot helper returned a non-source path')
        require(not any(part.startswith('.') or part in {'data', 'credentials', '__pycache__'} for part in p.parts)
                and (name in {'AGENTS.md', 'pyproject.toml'} or p.suffix == '.py'
                     or name in assets and p.suffix == '.json' and p.stem.lower() not in
                     {'credentials', 'secrets', 'api_keys', 'api_key', 'tokens', 'auth', 'accounts'}),
                'Sensitive/non-source snapshot path')
        path = checked_path(root, name)
        value = file_hash(path)
        if value is not None:
            result[name] = value
    require(result, 'Empty target source snapshot')
    return dict(sorted(result.items()))


def run_installed_check(snapshot, output, inventory, tests, contract, cancelled):
    """Export the JUnit already produced by core; no test/command substitution.

    Dedicated process, like capability_checks.run_check itself. The accepted
    runner owns limits, staging, driver bytes, dependency discovery and cleanup.
    Retain the original JUnit and host-owned workspace identity. Pytest can
    discover an ancestor's configuration in nested checks; its dotted node
    paths then contain workspace components, not just Python identifiers.
    Capture the actual workspace, never infer it from a test's declarations.
    """
    execute = policy.checker._execute
    def collect(directory, manifest, hashes, entrypoint, limits, seconds, destination, stop):
        require(manifest == {'test_result_files': [core.REPORT_PATH]} and entrypoint == core.CHECK_PATH
                and seconds is None and limits['cpu_seconds'] is None, 'Unknown combined check execution boundary')
        workspace = execute.__globals__['_check_workspace']
        roots = []

        @contextmanager
        def capture(result, output):
            with workspace(result, output) as work:
                _plain(work)
                roots.append(str(canonical_path(work)))
                yield work

        # Only observe the existing workspace lifecycle. Keep driver, command,
        # test selection, staging, resource/cancel gates and cleanup unchanged.
        observed_execute = FunctionType(execute.__code__, {
            **execute.__globals__, '_check_workspace': capture,
        }, execute.__name__, execute.__defaults__, execute.__closure__)
        host = observed_execute(directory, {'test_result_files': [core.REPORT_PATH, JUNIT]}, hashes,
                                entrypoint, limits, seconds, destination, stop)
        require(len(roots) == 1, 'Unknown combined check workspace lifecycle')
        junit = [a for a in host['artifacts'] if Path(a['path']).name == JUNIT]
        binding = {'version': JUNIT_ROOT_VERSION, 'execution_root': roots[0],
                   'source_sha256': core.digest(hashes), 'test_paths': list(tests),
                   'junit_sha256': junit[0]['sha256'] if len(junit) == 1 else None}
        path = canonical_path(destination) / JUNIT_ROOT
        publish(path, binding)
        host['junit_root_binding'] = binding
        host['artifacts'].append({'path': str(path), 'sha256': file_hash(path)})
        return host
    try:
        policy.checker._execute = collect
        return policy.run_check(io_path(snapshot), io_path(output), inventory, tests, contract, cancelled)
    finally:
        policy.checker._execute = execute


def dependency_identity(root):
    """Freeze the same installed dependency closure core selects, without installs.

    Version strings alone cannot admit reuse after in-place package changes.
    Preserve actual file hashes, including native extensions, for that closure.
    """
    from importlib.metadata import distribution
    from packaging.requirements import Requirement
    import tomllib
    project = tomllib.loads((io_path(root / 'pyproject.toml')).read_text(encoding='utf-8'))['project']
    pending = [Requirement(r) for r in [*project['dependencies'], *project['optional-dependencies']['test']]]
    selected = {}
    while pending:
        req = pending.pop()
        name = req.name.lower().replace('_', '-')
        if req.marker and not req.marker.evaluate() or name in selected:
            continue
        package = distribution(req.name)
        files = {}
        for file in package.files or ():
            relative = file.as_posix()
            if '..' in file.parts or '__pycache__' in file.parts or relative.endswith(('.pyc', '.pth')):
                continue
            path = Path(package.locate_file(file))
            plain_path(io_path(path))
            require(io_path(path).is_file(), 'Installed check dependency file is missing: ' + name)
            files[relative] = core._sha(io_path(path))
        require(files, 'Installed check dependency has no verifiable file inventory: ' + name)
        selected[name] = {'version': package.version, 'files': dict(sorted(files.items()))}
        pending.extend(Requirement(r) for r in package.requires or ())
    return dict(sorted(selected.items()))


def freeze_installed_contract(root):
    """Accepted contract builder, with I/O paths separate from durable identity.

    No policy defaults or time limits are changed. Clone its read-only globals
    so long target paths work without patching a shared runtime or config.ROOT.
    """
    builder = FunctionType(policy.freeze_contract.__code__, {
        **policy.freeze_contract.__globals__, 'project_root': target_root,
        'core': SimpleNamespace(_sha=file_hash, _source_paths=core._source_paths, digest=core.digest),
        'checker': SimpleNamespace(checked_path=lambda root, name: io_path(checked_path(root, name))),
    })
    return builder(root)


def junit_execution_prefixes(checks, reported, junit):
    """Read a host observation, not a JUnit property or a guessed root suffix.

    Old unprefixed JUnit remains supported without this extra evidence. If
    evidence exists it must authenticate even when no prefix is needed. Root
    directories may already have been removed by the normal runner cleanup;
    do not inspect/recreate them during replay.
    """
    host = checks['host_receipt']
    artifacts = [a for a in host['artifacts'] if Path(a['path']).name == JUNIT_ROOT]
    binding = host.get('junit_root_binding')
    if not artifacts and binding is None:
        return set()
    require(len(artifacts) == 1 and isinstance(binding, dict), 'Missing JUnit execution root binding')
    path = Path(artifacts[0]['path'])
    require(file_hash(path) == artifacts[0]['sha256'] and read_json(path) == binding,
            'JUnit execution root artifact changed')
    require(binding.get('version') == JUNIT_ROOT_VERSION
            and binding.get('source_sha256') == core.digest(reported)
            and binding.get('test_paths') == checks['test_paths']
            and binding.get('junit_sha256') == junit['sha256'], 'JUnit execution root binding changed')
    value = binding.get('execution_root')
    require(isinstance(value, str) and value, 'Unsupported JUnit execution root')
    root = canonical_path(value)
    require(root.is_absolute() and str(root) == value and '..' not in root.parts,
            'Unsupported JUnit execution root')
    # Pytest emits paths relative to rootdir. An ancestor rootdir must produce
    # exactly one of these prefixes, followed by the FULL frozen source path.
    # Never strip an arbitrary prefix and then match a convenient basename.
    return {root.relative_to(parent).as_posix() for parent in root.parents}


def selected_test_coverage(checks, tests, inventory=None):
    """Map actual JUnit cases to frozen files, never infer collected node IDs.

    Pytest xunit1 may include a relative file; xunit2 commonly omits it. Module
    classnames may be relative to the project, test root or import package.
    Admit component suffixes only when they identify ONE file in the entire
    frozen test inventory (including unselected files), then match class/name
    boundaries exactly. Ancestor-root representations additionally require the
    host workspace binding. Unknown/ambiguous spellings fail closed.
    """
    report = checks['host_receipt'].get('test_reports', {}).get(core.REPORT_PATH, {})
    reported = report.get('source_sha256', {})
    require(inventory is None or checks.get('candidate_sha256') == core.digest(inventory),
            'JUnit frozen candidate inventory changed')
    inventory = reported if inventory is None else inventory
    require(all(reported.get(n) == h for n, h in inventory.items()), 'JUnit source inventory changed')
    test_paths(tests, inventory)
    artifacts = [a for a in checks['host_receipt']['artifacts'] if Path(a['path']).name == JUNIT]
    require(len(artifacts) == 1, 'Missing original combined JUnit collection evidence')
    path = Path(artifacts[0]['path'])
    require(file_hash(path) == artifacts[0]['sha256'], 'Combined JUnit evidence changed')
    prefixes = junit_execution_prefixes(checks, reported, artifacts[0])
    document = ElementTree.fromstring(io_path(path).read_bytes())
    require(document.tag in {'testsuite', 'testsuites'}, 'Unsupported JUnit document root')
    cases = list(document.iter('testcase'))
    require(cases, 'Combined JUnit collected no cases')
    require(not any(list(c.iter('failure')) or list(c.iter('error')) for c in cases),
            'Combined JUnit contains failed tests')
    require(report.get('tests_collected') == len(cases), 'JUnit collection count differs from host report')
    files = sorted(n for n in inventory if n.startswith('tests/') and n.endswith('.py'))
    aliases = {}
    for filename in files:
        parts = PurePosixPath(filename).with_suffix('').parts
        for start in range(len(parts)):
            aliases.setdefault('.'.join(parts[start:]), set()).add(filename)
        for prefix in prefixes:
            aliases.setdefault(prefix.replace('/', '.') + '.' + '.'.join(parts), set()).add(filename)
    observed = []
    for case in cases:
        classname, name = case.get('classname', ''), case.get('name', '')
        require(classname and name,
                'Unsupported JUnit classname/name representation')
        explicit = case.get('file')
        file_matches = set(files)
        if explicit is not None:
            normalized = explicit.replace('\\', '/')
            if normalized.startswith('./'):
                normalized = normalized[2:]
            parts = PurePosixPath(normalized)
            require(normalized and not parts.is_absolute() and ':' not in normalized
                    and '..' not in parts.parts and parts.as_posix() == normalized,
                    'Unsupported JUnit file/root representation: ' + explicit)
            file_matches = {f for f in files if f == normalized or f.endswith('/' + normalized)
                            or any(normalized == prefix + '/' + f for prefix in prefixes)}
        possibilities = set()
        for module, paths in aliases.items():
            if classname == module or classname.startswith(module + '.'):
                classes = tuple(classname[len(module) + 1:].split('.')) if classname != module else ()
                require(not classes or all(p.isidentifier() for p in classes),
                        'Unsupported JUnit classname/name representation: ' + repr((classname, name)))
                possibilities.update((f, classes) for f in paths & file_matches)
        require(len(possibilities) == 1,
                ('Ambiguous' if possibilities else 'Unsupported') +
                ' JUnit file/classname mapping: ' + repr((explicit, classname)))
        filename, classes = next(iter(possibilities))
        observed.append((filename, classes, name))
    # Overlapping authentic file/node selectors can make pytest run a case
    # twice. Retain the actual count/bytes; repetitions give no extra coverage.
    for selector in tests:
        filename, *nodes = selector.split('::')
        def matches(case):
            file, classes, name = case
            if file != filename:
                return False
            if not nodes:
                return True
            if tuple(nodes) == classes[:len(nodes)]:
                return True
            return (classes == tuple(nodes[:-1])
                    and (name == nodes[-1] or '[' not in nodes[-1] and name.startswith(nodes[-1] + '[')))
        require(any(matches(c) for c in observed), 'Selected test collected no cases: ' + selector)


def check_installed(journal, sources, *, cancelled=lambda: False):
    root = journal.root
    journal.verify_files()
    require(journal.has('installed') and (io_path(journal.directory / 'audit.json')).is_file(), 'Installation audit incomplete')
    assets = {name for oid in sources.adopted for name in sources.nodes[oid]['attempt']['inventory']
              if name.endswith('.json')}
    inventory = source_inventory(root, assets)
    tests = sorted({t for oid in sources.adopted for t in sources.nodes[oid]['work']['input']['tests']})
    test_paths(tests, inventory)
    for test in tests:
        name = test.split('::', 1)[0]
        accepted = ({journal.spec['changes'][name]['after']} if name in journal.spec['changes'] else
                    {sources.nodes[oid]['attempt']['inventory'][name] for oid in sources.adopted
                     if test in sources.nodes[oid]['work']['input']['tests']})
        require(accepted == {inventory[name]}, 'Selected test source differs from accepted source: ' + name)
    contract = freeze_installed_contract(root)
    require(contract['target_source_sha256'] == core.digest(inventory), 'Target snapshot policy inventory mismatch')
    # The running checker must itself be part of accepted adopted source.
    runtime = contract['runtime']
    require(any(str(n['candidate']) == runtime['source_root'] for oid, n in sources.nodes.items()
                if oid in sources.adopted), 'Checker runtime is not an accepted adopted candidate')
    identity = {'version': CHECK_VERSION, 'deployment_plan_sha256': journal.plan_sha,
                'report_adapter': 'core_junit_export_v2',
                'coverage_adapter': 'frozen_file_junit_mapping_v3',
                'dependencies': dependency_identity(root),
                'installation_audit_sha256': core.digest(read_json(journal.directory / 'audit.json')),
                'parents': journal.spec['sources'], 'source_inventory': inventory, 'tests': tests,
                policy.FIELD: contract}
    directory = journal.directory / 'post-install' / core.digest(identity)
    receipt_path = directory / 'receipt.json'
    if io_path(receipt_path).exists():
        receipt = read_json(receipt_path)
        require(receipt.get('identity') == identity and receipt.get('sha256') == core.digest(
                    {k: v for k, v in receipt.items() if k != 'sha256'}), 'Combined check receipt changed')
        require(receipt.get('status') == 'passed', 'Previous combined check failed/inconclusive; normal repair required')
        require(read_json(directory / 'identity.json') == identity
                and read_json(directory / 'started.json') == {'identity_sha256': core.digest(identity), 'state': 'running'}
                and read_json(directory / 'raw-check.json') == receipt['checks']
                and inventory_at(directory / 'source') == inventory, 'Combined check immutable audit changed')
        authenticate_checks(receipt['checks'], inventory, tests, directory, contract)
        require(receipt['checks']['test_dependencies'] == {k: v['version'] for k, v in identity['dependencies'].items()},
                'Combined dependency selection changed')
        selected_test_coverage(receipt['checks'], tests, inventory)
        require(source_inventory(root, assets) == inventory and not cancelled(), 'Target source changed/cancelled during replay')
        _paused_for_journal(journal)
        return receipt
    # A lost execution acknowledgement is never permission to rerun.
    require(not (io_path(directory / 'started.json')).exists(), 'Combined check completion unknown; normal repair required')
    publish(directory / 'identity.json', identity)
    snapshot = directory / 'source'
    for name, value in inventory.items():
        data = io_path(checked_path(root, name)).read_bytes()
        require(hashlib.sha256(data).hexdigest() == value, 'Target source changed during snapshot')
        publish_bytes(checked_path(snapshot, name), data)
    require(inventory_at(snapshot) == inventory and source_inventory(root, assets) == inventory, 'Source snapshot drift')
    research_files = database_signature(root)
    _paused_for_journal(journal)
    require(database_signature(root) == research_files, 'Research changed during combined check preflight')
    publish(directory / 'started.json', {'identity_sha256': core.digest(identity), 'state': 'running'})
    checks = None
    try:
        def stop():
            if cancelled():
                return True
            try:
                if source_inventory(root, assets) != inventory:
                    return True
                return database_signature(root) != research_files
            except (ValueError, OSError):
                return True
        checks = run_installed_check(snapshot, directory / 'output', inventory, tests, contract, stop)
        # Persist raw result BEFORE any validation, including failed/unknown ones.
        publish(directory / 'raw-check.json', checks)
        authenticate_checks(checks, inventory, tests, directory, contract)
        require(checks['test_dependencies'] == {k: v['version'] for k, v in identity['dependencies'].items()}
                and dependency_identity(root) == identity['dependencies'], 'Combined dependency files changed')
        selected_test_coverage(checks, tests, inventory)
        require(not stop(), 'Target source changed/cancelled during combined check')
        require(freeze_installed_contract(root) == contract, 'Frozen combined check runtime changed')
        sources.revalidate()
        journal.verify_files()
        _paused_for_journal(journal)
        receipt = {'identity': identity, 'status': 'passed', 'checks': checks, 'scientific_acceptance': False}
    except BaseException as exc:
        receipt = {'identity': identity, 'status': 'failed' if checks else 'inconclusive', 'checks': checks,
                   'error': type(exc).__name__ + ': ' + str(exc), 'scientific_acceptance': False}
        receipt['sha256'] = core.digest(receipt)
        publish(receipt_path, receipt)
        raise
    receipt['sha256'] = core.digest(receipt)
    publish(receipt_path, receipt)
    return receipt


def deploy(root, order_ids, *, check=False, cancelled=lambda: False):
    root = target_root(root)
    require(order_ids and len(order_ids) == len(set(order_ids)), 'Supply distinct accepted source orders')
    with deployment_lock(root):
        sources = Sources(root)
        # Invocation authority is always the module's own accepted candidate.
        candidate = canonical_path(Path(__file__).absolute()).parent.parent
        require(candidate.parent.parent == root / core.ROOT_DIRECTORY, 'Run CLI from its accepted isolated candidate')
        authority = sources.load(candidate.parent.name)
        require(authority['candidate'] == candidate and 'autoresearch/capability_deployment.py' in
                authority['work']['input']['allowed_paths'], 'Deployment CLI lacks accepted self source authority')
        for oid in sorted(order_ids):
            sources.load(oid)
        changes, binding = sources.composition()
        if binding.get('operation') != 'metric_capability_source_repair_v1':
            paused_binding(root, binding)
        spec = {'version': VERSION, 'target': str(root), 'authority': authority['link'],
                'sources': [sources.nodes[o]['link'] for o in sources.adopted],
                'changes': dict(sorted(changes.items())), 'research_binding': binding}
        directory = authority['directory'] / 'deployments' / core.digest(spec)
        journal = Journal(directory, spec, root)
        if binding.get('operation') == 'metric_capability_source_repair_v1':
            _paused_for_journal(journal)
        try:
            audit = journal.install(sources)
            if check:
                return check_installed(journal, sources, cancelled=cancelled)
            return {**audit, 'journal': str(directory), 'combined_source_checked': False}
        except BaseException as exc:
            # A publication may have succeeded just before its caller crashed.
            # Do not reuse the in-memory event cursor or overwrite that event.
            publish(journal.directory / 'failures' / (uuid.uuid4().hex + '.json'),
                    {'plan_sha256': journal.plan_sha, 'status': 'failed_or_inconclusive',
                     'error': type(exc).__name__ + ': ' + str(exc)})
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--order', action='append', required=True)
    parser.add_argument('--check-installed', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = deploy(args.root, args.order, check=args.check_installed)
    except (ValueError, OSError, KeyError, TypeError, ResearchBlocked, ValidationError) as exc:
        parser.exit(1, type(exc).__name__ + ': ' + str(exc) + '\n')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
