"""Isolated Harness source work, with a durable CLI independent of the live server."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
import difflib
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import uuid

from .async_ownership import gather, to_thread
from .codex_accounting import capture_usage
from .config import DATA_DIR, ROOT, get_settings
from .engine import Engine, Harness, digest
from .harness_delivery import authenticate
from .local_codex import LocalCodexClient
from .storage import Store, now, redact


COLLECTION = 'source_capability_work_orders'
ROOT_DIRECTORY = '.autoresearch/capability-work-orders'
REVIEWERS = ('director', 'scout')
CHECK_SECONDS = 600
MAX_ATTEMPTS = 2
MAX_REVIEW_CHARS = 200_000
MAX_EXTERNAL_REVIEW_CHARS = 250_000
CHECK_PATH = 'capability_check.py'
REPORT_PATH = 'test-results/capability-check.json'
_DESKTOP_SESSION_ENV = {'CODEX_APP_RUNTIME_CLI_PATH', 'CODEX_APP_TOOLS_PIPE_PATH',
    'CODEX_WINDOWS_REGISTERED_CORE', 'CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY',
    'CODEX_PERMISSION_PROFILE', 'CODEX_SESSION_ID', 'CODEX_THREAD_ID', 'CODEX_TOOL_CALL_ID'}


def _sha(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def _source_paths(root):
    listing = subprocess.check_output(['git', 'ls-files', '-z', '--cached', '--others',
        '--exclude-standard', '--', 'autoresearch', 'tests', 'AGENTS.md', 'pyproject.toml'], cwd=root)
    return sorted(set(listing.decode('utf-8').split('\0')) - {''})


def _inventory(root):
    from .code_verification import checked_path
    result = {}
    for top in root.iterdir():
        pending = [top]
        while pending:
            path = pending.pop()
            relative = path.relative_to(root).as_posix()
            checked_path(root, relative)
            if path.name in {'__pycache__', '.pytest_cache'}:
                continue
            if path.is_dir():
                pending.extend(path.iterdir())
            elif path.is_file():
                result[relative] = _sha(path)
    return dict(sorted(result.items()))


def _snapshot(root, target):
    from .code_verification import checked_path
    target.mkdir()
    for name in _source_paths(root):
        source = checked_path(root, name)
        if not source.is_file():
            continue  # Preserve tracked deletions in the current source view.
        dest = checked_path(target, name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        if _sha(source) != _sha(dest):
            raise ValueError('Source changed while freezing work-order inputs')
    return _inventory(target)


def _assignment_binding(read, run_id, purpose, hand):
    key, role = hand['id'], hand['to_role']
    frozen = read('handoff_inputs', key)
    fields = {name: frozen.get(name) for name in ('instruction', 'context', 'schema')}
    if (hand.get('run_id') != run_id or hand.get('purpose') != purpose
            or frozen.get('id') != key or frozen.get('run_id') != run_id
            or digest(fields) != hand.get('input_hash')
            or Harness.decision_key(run_id, role, purpose, **fields) != key):
        raise ValueError('The original review assignment cannot be authenticated')
    progress = read('handoff_progress', key)
    if progress and (progress.get('id'), progress.get('run_id')) != (key, run_id):
        raise ValueError('Review assignment progress belongs to another request')
    saved = progress.get('normal_request', {})
    return frozen['context'], {'handoff_id': key, 'role': role, 'status': hand['status'],
        'input_sha256': hand['input_hash'], 'progress_sha256': digest(progress),
        'turn': progress.get('turn'), 'tool_receipts': len(progress.get('tool_results', [])),
        'previous_request': {name: saved.get(name) for name in ('operation_id', 'prepared_sha256', 'status', 'turn')}}


def _document_review_binding(db, read, run_id, cycle, generation, workflow):
    from . import implementation_acceptance as policy
    from . import implementation_scope_review as partition

    revision = workflow['current_revision']
    specs = [row for row in workflow['specifications'] if row['index'] == revision]
    ledger = workflow.get(policy.FIELD, {})
    rounds = [row for row in ledger.get('rounds', []) if row['revision'] == revision]
    scope = workflow.get(partition.ARCHITECTURE_FIELD, {})
    if (len(specs) != 1 or len(rounds) != 1 or not policy.enabled(workflow)
            or not scope or scope.get('sha256') != partition.sha({k: v for k, v in scope.items() if k != 'sha256'})):
        raise ValueError('Expected one current review round with a frozen document and architecture scope')
    policy.check_seal(ledger.get('contract', {}))
    row, slot = specs[0], rounds[0]
    if row['spec_hash'] != workflow.get('spec_hash') or row.get('approved') is not None or slot.get('complete'):
        raise ValueError('The selected document is no longer in active acceptance')
    author_key = row['authoring_handoff_id']
    author = read('handoffs', author_key)
    author_ctx, author_binding = _assignment_binding(read, run_id, author.get('purpose'), author)
    author_reply = read('decisions', author_key)
    author_result = author_reply.get('result', {})
    if (author_binding['role'] != 'engineer' or author.get('status') != 'completed'
            or not author.get('purpose', '').startswith('implementation_spec:')
            or author_ctx.get(partition.ARCHITECTURE_FIELD) != scope
            or author.get('output_hash') != digest(author_result)
            or (author_reply.get('id'), author_reply.get('run_id'), author_reply.get('role'), author_reply.get('purpose'))
            != (author_key, run_id, 'engineer', author['purpose'])
            or (author_result.get('document_id'), author_result.get('document_sha256'), author_result.get('spec_hash'))
            != (row['document']['document_id'], row['document']['sha256'], row['spec_hash'])):
        raise ValueError('Document authoring receipt cannot be authenticated against this round')
    from .implementation_review_routing import digest as workflow_digest
    purpose = f'implementation_acceptance_review:{workflow_digest(workflow["id"])}:{revision}'
    bindings = {}
    for raw, in db.execute("SELECT data FROM records WHERE collection='handoffs' "
            "AND json_extract(data,'$.run_id')=? AND json_extract(data,'$.purpose')=?", (run_id, purpose)):
        hand = json.loads(raw)
        context, binding = _assignment_binding(read, run_id, purpose, hand)
        review, role = context.get(policy.CONTEXT, {}), hand['to_role']
        if (role in bindings or role not in scope['responsibilities']
                or context.get('acceptance_workflow_id') != workflow['id']
                or review.get('document') != row['document']
                or review.get('spec_hash') != row['spec_hash']
                or review.get('contract') != ledger['contract']):
            raise ValueError('Document review assignment differs from the current frozen round')
        decision = read('decisions', hand['id'])
        if decision and (hand['status'] != 'completed' or hand.get('output_hash') != digest(decision.get('result'))
                or (decision.get('id'), decision.get('run_id'), decision.get('role'), decision.get('purpose'))
                != (hand['id'], run_id, role, purpose)):
            raise ValueError('Document review decision does not belong to its original assignment')
        bindings[role] = {**binding, 'decision_sha256': digest(decision) if decision else None,
            'assigned_criteria': review['assigned_criteria'], 'self_check': review['self_check']}
    if not bindings:
        raise ValueError('Expected at least one original document review assignment')
    return {'run_id': run_id, 'cycle': cycle, 'generation': generation, 'workflow_id': workflow['id'],
        'operation': 'implementation_document_review', 'revision': revision, 'spec_sha256': row['spec_hash'],
        'document_sha256': row['document']['sha256'], 'architecture_scope_sha256': scope['sha256'],
        'acceptance_contract_sha256': ledger['contract']['sha256'], 'round_sha256': digest(slot),
        'author': author_binding, 'handoffs': [bindings[role] for role in sorted(bindings)]}


def _document_author_binding(path, db, read, run_id, cycle, generation, workflow, run):
    """Freeze a failed current-revision submission without changing its draft."""
    from . import implementation_acceptance as policy, implementation_acceptance_author as continuation
    from . import implementation_authoring as author
    from .storage import _ReadSnapshot
    from types import SimpleNamespace

    revision = workflow['current_revision']
    ledger = workflow.get(policy.FIELD, {})
    failure = db.execute("SELECT id,message FROM events WHERE run_id=? AND kind='run.blocked' ORDER BY id DESC LIMIT 1",
                         (run_id,)).fetchone()
    if (not policy.enabled(workflow) or revision <= 0 or ledger.get('outcome') != 'REPAIR'
            or any(row['index'] == revision for row in workflow['specifications'])
            or not failure or failure[1] != run.get('error') or read('runtime', 'worker').get('held')):
        raise ValueError('Expected a stopped current-document author submission failure')

    purpose = f'implementation_spec:{digest(workflow["id"])}:{revision}'
    rows = db.execute("SELECT data FROM records WHERE collection='handoffs' AND json_extract(data,'$.run_id')=? "
                      "AND json_extract(data,'$.purpose')=? AND json_extract(data,'$.to_role')='engineer'",
                      (run_id, purpose)).fetchall()
    if len(rows) != 1:
        raise ValueError('Expected one frozen author assignment for the current revision')
    hand = json.loads(rows[0][0])
    db.row_factory = sqlite3.Row
    engine = Engine.__new__(Engine)
    engine.data_dir = path.parent
    engine.store = _ReadSnapshot(SimpleNamespace(path=path), db)
    chain, seen = [], set()
    while hand and hand['id'] not in seen:
        hid = hand['id']
        seen.add(hid)
        context, binding = _assignment_binding(read, run_id, hand['purpose'], hand)
        result = read('decisions', hid).get('result')
        if (hand.get('to_role') != 'engineer' or not result
                or context.get('implementation_document_authoring', {}).get('workflow_id') != workflow['id']
                or context['implementation_document_authoring'].get('revision') != revision):
            raise ValueError('Author continuation differs from its controller revision')
        authenticate(engine, run_id, 'engineer', hid, result)
        chain.append({**binding, 'decision_sha256': digest(result),
                      'draft_sha256': digest(read(author.COLLECTION, hid))})
        edge = continuation._existing_continuation(engine, run_id, workflow, hid, result)
        if edge is None:
            break
        chain[-1]['continuation_sha256'] = edge['sha256']
        hand = read('handoffs', read('stage_contexts', edge['audit_id'])['successor_id'])
    else:
        raise ValueError('Author continuation chain is missing or cyclic')

    if len(chain) < 2 or read(author.COLLECTION, chain[-1]['handoff_id']):
        raise ValueError('Expected a completed continuation without its own save')
    parent_id = chain[-2]['handoff_id']
    draft = read(author.COLLECTION, parent_id)
    saved = author.pointer(draft)
    if (not any(r.get('call', {}).get('name') == draft['write_tool']
                and r.get('call', {}).get('arguments') == draft['write_arguments']
                and r.get('result', {}).get('saved') is True
                and r['result'].get('submission') == saved
                for r in read('handoff_progress', parent_id).get('tool_results', []))
            or {key: result.get(key) for key in saved} != saved):
        raise ValueError('Original author save or continuation pointer is not authenticated')
    return {'run_id': run_id, 'cycle': cycle, 'generation': generation, 'workflow_id': workflow['id'],
            'operation': 'implementation_document_author_submission', 'revision': revision,
            'controller_sha256': digest(continuation._workflow_scope(workflow)),
            'failure_event': failure[0], 'failure': failure[1], 'draft': saved, 'handoffs': chain}


def research_binding(path, run_id, *, repair_receipt=None):
    """Capture controller identity and its actual blocked pre-dispatch operation read-only."""
    path = Path(path).resolve()
    if repair_receipt is not None:
        from .program_capability_repair import binding
        return binding(path, run_id, repair_receipt)
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('BEGIN')
        def read(collection, key):
            row = db.execute('SELECT data FROM records WHERE collection=? AND key=?', (collection, key)).fetchone()
            return json.loads(row[0]) if row else {}
        run = read('runs', run_id)
        if run.get('status') not in {'paused', 'failed'} or run.get('stage') != 'implementation':
            raise ValueError('The selected implementation run must be paused')
        generation = run['state'].get('implementation_generation')
        cycle = run['state'].get('research_cycle', 1)
        workflows = db.execute("SELECT data FROM records WHERE collection='codex_workflows' "
            "AND json_extract(data,'$.run_id')=? AND json_extract(data,'$.kind')='implementation' "
            "AND json_extract(data,'$.cycle')=? AND json_extract(data,'$.implementation_generation') IS ?",
            (run_id, cycle, generation)).fetchall()
        if not workflows:
            from .implementation_restart import COLLECTION as RESTARTS, MARKER, _validate
            from .storage import _ReadSnapshot
            restart = read(RESTARTS, run['state'].get(MARKER))
            plan = read('experiment_plans', run_id)
            failure = db.execute("SELECT id,message FROM events WHERE run_id=? AND kind='run.failed' ORDER BY id DESC LIMIT 1",
                                 (run_id,)).fetchone()
            if (run.get('status') != 'failed' or not failure or failure[1] != run.get('error')
                    or restart.get('mode') != 'before_document' or not plan.get('metric_contract')
                    or read('runtime', 'worker').get('held')):
                raise ValueError('Expected one controller-owned implementation workflow or a stopped pre-author failure')
            db.row_factory = sqlite3.Row
            engine = Engine.__new__(Engine)
            engine.data_dir = path.parent
            engine.store = _ReadSnapshot(type('DatabasePath', (), {'path': path})(), db)
            _validate(engine, db, run_id, restart)
            return {'run_id': run_id, 'cycle': cycle, 'generation': generation, 'restart_id': restart['id'],
                'restart_sha256': restart['sha256'], 'experiment_plan_sha256': digest(plan),
                'failure_event': failure[0], 'failure': failure[1]}
        if len(workflows) != 1 or run['status'] != 'paused':
            raise ValueError('Expected one controller-owned implementation workflow')
        workflow = json.loads(workflows[0][0])
        if workflow.get('status') == 'spec_review':
            if read('runtime', 'worker').get('held'):
                raise ValueError('Wait for the paused document-review worker to release its lease')
            return _document_review_binding(db, read, run_id, cycle, generation, workflow)
        if workflow.get('status') == 'specification':
            return _document_author_binding(path, db, read, run_id, cycle, generation, workflow, run)
        index = workflow['current_revision']
        attempt = next(row for row in workflow['attempts'] if row['index'] == index)
        purpose = attempt.get('review_purpose') or f'code_review:{digest(workflow["id"])}:{index}'
        bindings = []
        for raw, in db.execute("SELECT data FROM records WHERE collection='handoffs' "
                "AND json_extract(data,'$.run_id')=? AND json_extract(data,'$.purpose')=?", (run_id, purpose)):
            hand = json.loads(raw)
            key = hand['id']
            frozen = read('handoff_inputs', key)
            fields = {name: frozen[name] for name in ('instruction', 'context', 'schema')}
            if (frozen['run_id'] != run_id or digest(fields) != hand['input_hash']
                    or Harness.decision_key(run_id, hand['to_role'], purpose, **fields) != key):
                raise ValueError('The original review assignment cannot be authenticated')
            progress = read('handoff_progress', key)
            saved = progress.get('normal_request', {})
            event = db.execute("SELECT id,created_at,data FROM events WHERE run_id=? "
                "AND kind='context.preparation_stalled' AND json_extract(data,'$.handoff_id')=? ORDER BY id DESC LIMIT 1",
                (run_id, key)).fetchone()
            bindings.append({'handoff_id': key, 'role': hand['to_role'], 'status': hand['status'],
                'input_sha256': hand['input_hash'], 'progress_sha256': digest(progress),
                'turn': progress.get('turn'), 'tool_receipts': len(progress.get('tool_results', [])),
                'previous_request': {name: saved.get(name) for name in ('operation_id', 'prepared_sha256', 'status', 'turn')},
                'stalled_projection': ({'event_id': event[0], 'created_at': event[1], **json.loads(event[2])}
                                       if event else None)})
        if {row['role'] for row in bindings} != {'director', 'engineer', 'scout'} or len(bindings) != 3:
            raise ValueError('Expected all three original code reviewers')
        return {'run_id': run_id, 'cycle': cycle, 'generation': generation, 'workflow_id': workflow['id'],
            'attempt_index': index, 'delivery_sha256': attempt['delivery_hash'],
            'spec_sha256': workflow['spec_hash'], 'handoffs': sorted(bindings, key=lambda row: row['role'])}


def _review_diff(baseline, candidate, before, after, allowed, *, maximum=MAX_REVIEW_CHARS, context_lines=5):
    changed = sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
    if not changed or any(name not in allowed for name in changed):
        raise ValueError('Candidate has no source change or changed paths outside the frozen scope')
    sources = []
    for name in changed:
        old = (baseline / name).read_text(encoding='utf-8').splitlines(True) if name in before else []
        new = (candidate / name).read_text(encoding='utf-8').splitlines(True) if name in after else []
        sources.append((name, old, new))
    # Keep every changed line. Reduce only unchanged surroundings if needed
    # to fit the same independent review input bound.
    for context in range(context_lines, -1, -1):
        patch = ''.join(line for name, old, new in sources for line in
                        difflib.unified_diff(old, new, fromfile='baseline/' + name,
                                             tofile='candidate/' + name, n=context))
        if len(patch) <= maximum:
            return changed, patch
    raise ValueError('Candidate diff is too large for the frozen independent review contract')


def _settings(store, live_database):
    # Read only the public routing configuration. Do not copy provider credentials.
    with closing(sqlite3.connect(Path(live_database).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        row = db.execute("SELECT data FROM records WHERE collection='settings' AND key='main'").fetchone()
    settings = get_settings(store)
    live = json.loads(row[0]) if row else {}
    names = [p['api_key_env'] for p in settings['providers'] + live.get('providers', []) if p.get('api_key_env')]
    native = next(p for p in settings['providers'] if p['id'] == 'local_codex')
    configured = next((p for p in live.get('providers', []) if p['id'] == 'local_codex'), {})
    for name in ('model', 'reasoning_effort', 'deep_reasoning_effort', 'timeout_seconds', 'deep_timeout_seconds'):
        if name in configured:
            native[name] = configured[name]
    settings['roles'] = dict.fromkeys(('director', 'engineer', 'scout'), 'local_codex')
    return settings, native, sorted(set(names + ['OPENAI_API_KEY', 'CODEX_API_KEY']))


def _run_check(candidate, output, inventory, tests):
    from importlib.metadata import distribution, distributions
    import site
    from tempfile import TemporaryDirectory
    from .code_verification import _execute
    from .lab import hardware_profile
    hardware = hardware_profile()
    if hardware['ram_available_gb'] < 2 or hardware['disk_free_gb'] < 1:
        raise ValueError('Insufficient free host resources for the isolated CPU check')
    limits = {'python_executable': sys.executable, 'gpu_allowed': False, 'memory_mb': 1536,
              'disk_mb': 256, 'cpu_seconds': CHECK_SECONDS, 'cpu_cores': 2}
    from packaging.requirements import Requirement
    import tomllib
    source_config = candidate / 'pyproject.toml'
    if source_config.exists():
        project = tomllib.loads(source_config.read_text(encoding='utf-8'))['project']
        initial = [*project['dependencies'], *project['optional-dependencies']['test']]
    else:
        initial = ['pytest', 'pytest-asyncio']
    selected = {}
    isolated = {d.metadata['Name'].lower().replace('_', '-') : d.version
                for d in distributions(path=[*site.getsitepackages(), str(Path(sys.base_prefix) / 'Lib/site-packages')])
                if d.metadata.get('Name')}
    pending = [Requirement(name) for name in initial]
    while pending:
        req = pending.pop()
        name = req.name.lower().replace('_', '-')
        if req.marker and not req.marker.evaluate() or name in selected:
            continue
        item = distribution(req.name)
        selected[name] = item
        pending.extend(Requirement(dependency) for dependency in item.requires or ())
    dependencies = {name: item for name, item in selected.items() if isolated.get(name) != item.version}
    driver = r'''import hashlib,json,os,shutil,sys
from pathlib import Path
from xml.etree import ElementTree
root=Path.cwd()
sys.path.insert(0,str(root))
os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD']='1'
import gc
import pytest
temp_root=root/'.pytest_cache'/'tmp'
temp_root.mkdir(parents=True,exist_ok=True)
test_temp=temp_root/'t'
try:
    status=pytest.main(['-q','-p','pytest_asyncio.plugin','-p','no:cacheprovider',f'--basetemp={test_temp}',
                        '--junitxml=host-junit.xml',*TESTS])
finally:
    # SQLite fixture connections may remain in cycles until collection.
    # The host removes the isolated directory after this child exits.
    gc.collect()
    if os.name=='nt':
        test_temp=Path('\\\\?\\'+str(test_temp))
    if test_temp.is_dir():
        shutil.rmtree(test_temp)
suites=list(ElementTree.parse('host-junit.xml').iter('testsuite'))
count=sum(int(s.attrib['tests']) for s in suites)
report={'source_sha256':{name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in INVENTORY},
        'passed':status==0 and count>0,'tests_collected':count,'exit_code':int(status)}
(root/'test-results').mkdir(exist_ok=True)
(root/REPORT).write_text(json.dumps(report),encoding='utf-8')
'''
    with TemporaryDirectory(prefix='autoresearch-capability-check-') as temp:
        work = Path(temp) / 'source'
        shutil.copytree(candidate, work, ignore=shutil.ignore_patterns('__pycache__', '.pytest_cache'))
        # -I excludes the user site. Stage only the installed test stack with
        # its exact bytes bound to the check, instead of reopening the user site.
        hashes = dict(inventory)
        for package in dependencies.values():
            for file in package.files or ():
                name = file.as_posix()
                if '..' in file.parts or '__pycache__' in file.parts or name.endswith(('.pyc', '.pth')):
                    continue
                from .code_verification import checked_path
                target = checked_path(work, name)
                if target.exists():
                    raise ValueError('Test dependency overlaps the candidate: ' + name)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(package.locate_file(file), target)
                hashes[name] = _sha(target)
        check = work / CHECK_PATH
        with check.open('x', encoding='utf-8') as stream:
            stream.write(f'TESTS={tests!r}\nINVENTORY={sorted([*hashes, CHECK_PATH])!r}\nREPORT={REPORT_PATH!r}\n' + driver)
        hashes[CHECK_PATH] = _sha(check)
        receipt = _execute(work, {'test_result_files': [REPORT_PATH]}, hashes, CHECK_PATH, limits,
                           CHECK_SECONDS, output, lambda: False)
    return {'candidate_sha256': digest(inventory), 'test_paths': tests, 'limits': limits,
        'python_version': sys.version, 'python_sha256': _sha(Path(sys.executable)),
        'test_dependencies': {name: package.version for name, package in selected.items()},
        'host_receipt': receipt}


class CapabilityHarness(Harness):
    @staticmethod
    def prepare(role, purpose, instruction, context, schema, tool_results=None, *, final_control=None,
                tool_contract_version=1):
        if role not in REVIEWERS or not purpose.startswith('source_capability_review:') or tool_results:
            raise ValueError('Independent capability review only accepts a source-bound final verdict')
        return {'role': role, 'task': instruction + '\nReturn only the frozen JSON verdict. No tools are authorized.',
            'context': {**context, '_purpose': purpose, '_output_token_limit': 5000, 'tool_results': [],
                        'current_date': now()[:10]}, 'schema': schema}


class WorkOrder:
    def __init__(self, directory, database):
        self.directory = directory
        self.database = database
        self.engine = Engine(Store(directory / 'work.sqlite3'), directory / 'records')
        self.store = self.engine.store
        self.settings, self.provider, self.secret_names = _settings(self.store, database)
        self.harness = CapabilityHarness(self.engine, self.engine.intelligence_factory(self.store, self.settings))

    def submit(self, order_id, source, requirement, paths, tests, *, after=None,
               revise_draft=False, require_accepted=False, review_from=None, repair_receipt=None):
        if repair_receipt is not None and (after or revise_draft or require_accepted or review_from):
            raise ValueError('Program source repair requires its own fresh native intake')
        if self.store.get(COLLECTION, order_id):
            raise ValueError('Use resume or status for an existing work order')
        binding = (research_binding(self.database, source) if repair_receipt is None else
                   research_binding(self.database, source, repair_receipt=repair_receipt))
        baseline = self.directory / 'baseline'
        if review_from:
            reference_order = self.directory.parent / review_from
            reference = reference_order / 'baseline'
            from .codex_workspace import _plain
            _plain(reference)
            reference_db = reference_order / 'work.sqlite3'
            _plain(reference_db)
            with closing(sqlite3.connect(reference_db.as_uri() + '?mode=ro', uri=True)) as db:
                saved = db.execute('SELECT data FROM records WHERE collection=? AND key=?',
                                   (COLLECTION, review_from)).fetchone()
            previous = json.loads(saved[0]) if saved else {}
            inventory = _inventory(reference)
            if (not previous or digest(previous['input']) != previous['input_sha256']
                    or inventory != previous['input']['baseline_inventory']):
                raise ValueError('Referenced baseline no longer matches the original work-order intake')
            source_intake = previous['input_sha256']
            shutil.copytree(reference, baseline)
            if _inventory(baseline) != inventory:
                raise ValueError('Referenced baseline changed while copying it')
        else:
            if repair_receipt is not None and not after:
                from .program_capability_repair import intake_baseline
                inventory = intake_baseline(ROOT, baseline, self.database, repair_receipt)
            else:
                inventory = None if after else _snapshot(ROOT, baseline)
        from .code_verification import checked_path
        for name in [*paths, *tests]:
            checked_path(baseline, name.split('::', 1)[0])
        if (not paths or not tests or len(set(paths)) != len(paths) or len(set(tests)) != len(tests)
                or any(not name.startswith(('autoresearch/', 'tests/')) or not name.endswith('.py') for name in paths)
                or any(not name.startswith('tests/test_') or not name.split('::', 1)[0].endswith('.py') for name in tests)
                or inventory is not None and not {name.split('::', 1)[0] for name in tests} <= set(inventory) | set(paths)
                or not review_from and 'autoresearch/capability_work_orders.py' in paths or 'tests/conftest.py' in paths):
            raise ValueError('Work order requires exact Python source scope and fixed test paths')
        run = self.engine.create({'title': 'Harness source capability', 'objective': requirement,
            'mode': 'live', 'max_calls': MAX_ATTEMPTS * (1 + len(REVIEWERS)), 'budget_usd': None,
            'allow_local_execution': False, 'tool_call_limit_policy': 'bounded'})
        alias = None
        if repair_receipt is not None:
            if self.store.get('runs', source):
                raise ValueError('Program source-run alias already exists; resume the frozen work order')
            alias = {'version': 1, 'original_draft_id': run['id'], 'run_id': source, 'work_order': order_id,
                'parent_database': str(self.database.resolve()), 'repair_receipt': repair_receipt,
                'research_binding_sha256': digest(binding)}
            # This is a NEW isolated source-controller record. The original
            # empty draft and every live research record remain untouched.
            run = {**run, 'id': source, 'source_run_alias': alias,
                   'config': {**run['config'], **binding['budget_limits']}}
        work = {'id': order_id, 'run_id': run['id'], 'status': 'queued', 'attempts': [], 'created_at': now(),
            'input': {'version': 1, 'operator_requirement': requirement, 'research_binding': binding,
                'baseline_inventory': inventory, 'after_work_order': after, 'allowed_paths': paths, 'tests': tests,
                **({'source_run_alias': alias} if alias else {}),
                **({'revise_draft': True} if revise_draft else {}),
                **({'require_accepted_dependency': True} if require_accepted else {}),
                **({'review_only_source': review_from, 'review_only_source_input_sha256': source_intake,
                    'candidate_provenance': 'operator_authorized_external_capability'}
                   if review_from else {}),
                'implementation': {'backend': 'local_codex', 'model': self.provider['model'],
                    'effort': self.provider['reasoning_effort'], 'timeout': self.provider['timeout_seconds'],
                    'contract': 'isolated_source_tree_with_git_diff_and_host_selected_tests_v1'},
                'review': {'backend': 'local_codex', 'model': self.provider['model'],
                    'effort': self.provider['deep_reasoning_effort'], 'timeout': self.provider['deep_timeout_seconds'],
                    'roles': list(REVIEWERS), 'contract': 'source_bound_harness_verdict_v1'}}}
        work['input_sha256'] = digest(work['input'])
        if review_from:
            candidate = self.directory / 'candidate-0'
            shutil.copytree(baseline, candidate)
            for name in paths:
                current = checked_path(ROOT, name)
                target = checked_path(candidate, name)
                if current.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(current, target)
                    if _sha(current) != _sha(target):
                        raise ValueError('External candidate changed during intake')
                else:
                    target.unlink(missing_ok=True)
            snapshot = _inventory(candidate)
            changed, patch = _review_diff(baseline, candidate, inventory, snapshot, paths,
                                          maximum=MAX_EXTERNAL_REVIEW_CHARS, context_lines=0)
            work['attempts'] = [{'index': 0, 'operation_id': uuid.uuid4().hex,
                'inventory': snapshot, 'candidate_sha256': digest(snapshot), 'diff': patch, 'changed_paths': changed}]
        if alias is not None:
            work['updated_at'] = now()
            self.store.batch(puts=[('runs', source, run), (COLLECTION, work['id'], work)])
        else:
            self.save(work)
        return work

    def save(self, work, **fields):
        if 'status' in fields and 'error' not in fields:
            fields['error'] = None
        work.update(fields, updated_at=now())
        self.store.put(COLLECTION, work['id'], work)

    async def implement(self, work, attempt, candidate):
        from .intelligence import normalize_usage
        native = work['input']['implementation']
        run_id, operation = work['run_id'], attempt['operation_id']
        def persist(receipt):
            attempt['receipt'] = receipt
            usage = capture_usage(self.store, run_id, receipt)
            work['updated_at'] = now()
            self.store.batch([(COLLECTION, work['id'], work),
                *([('codex_usage', usage['id'], usage)] if usage else [])])
        ledger = self.store.get('budgets', run_id, {})
        intelligence = self.harness.intelligence
        reservation = next((row for row in [*ledger.get('pending', {}).values(), *ledger.get('records', [])]
                            if row.get('operation_id') == operation), None)
        if reservation is None:
            reservation = intelligence._reserve(run_id, {**self.provider, 'model': native['model']}, 'engineer',
                attempt['instruction'], 16000, operation_id=operation)
        receipt = attempt.get('receipt')
        if not receipt or receipt['status'] != 'completed':
            self.engine.check_active(run_id)
            runtime = attempt['execution_runtime']
            executable = runtime['executable']
            if _sha(Path(executable)) != runtime['sha256']:
                raise ValueError('Frozen Codex execution binary changed')
            receipt = await LocalCodexClient(timeout=native['timeout'], blocked_env_names=self.secret_names,
                executable=executable).run(
                run_id, candidate, attempt['instruction'], operation_id=operation, checkpoint=receipt,
                model=native['model'], effort=native['effort'], on_progress=persist,
                check_active=lambda: self.engine.check_active(run_id))
        if receipt['status'] != 'completed' or not receipt.get('thread_id') or not receipt.get('turn_id'):
            raise ValueError('Implementation lacks a completed Codex receipt: ' + receipt['status'])
        raw = (receipt.get('usage') or {}).get('last') or {}
        usage = normalize_usage('chat', {'input_tokens': raw.get('inputTokens'), 'output_tokens': raw.get('outputTokens')})
        intelligence._settle(reservation, usage, 0, verified=False, status='completed',
            provenance='local_codex_chatgpt', billing_scope='chatgpt_subscription', cost_usd=None,
            cost_estimated=False, cost_status='not_reported_by_local_codex',
            thread_id=receipt['thread_id'], turn_id=receipt['turn_id'])

    def failed_launch(self, work, attempt, inventory):
        """Recognize a terminal no-change Windows launch failure; preserve the paid turn."""
        receipt = attempt['receipt']
        if inventory != work['input']['baseline_inventory'] or receipt['status'] != 'completed':
            return False
        messages = receipt.get('public_messages', [])
        return (any('registered Core launch requires an installed app' in row.get('text', '')
                    for row in messages if row.get('phase') == 'final_answer')
                and any(row.get('type') == 'commandExecution' and row.get('status') == 'failed'
                        and row.get('exit_code') == -1 for row in receipt.get('activities', [])))

    async def review(self, work, attempt):
        inputs = work['input']
        candidate = attempt['candidate_sha256']
        if _inventory(self.directory / f"candidate-{attempt['index']}") != attempt['inventory']:
            raise ValueError('Candidate changed before independent review')
        fields = {'candidate_sha256': {'const': candidate}, 'approve': {'type': 'boolean'},
            'rationale': {'type': 'string', 'minLength': 1}, 'required_changes': {'type': 'array',
                'items': {'type': 'string', 'minLength': 1}}}
        task = {'purpose': f'source_capability_review:{work["id"]}:{attempt["index"]}:{candidate}',
            'instruction': 'Independently review the actual diff and bounded host check receipts. Verify all operator '
                'requirements, compatibility for frozen and continuing requests, original evidence and independent '
                'review protection, and producer/Schema/parser/consumer agreement. Identify any remaining defect '
                'with exact file locations and conditions. Approve only if justified. This is capability review only.',
            'context': {'work_order': work['id'], 'work_order_input_sha256': work['input_sha256'],
                'requirement': inputs['operator_requirement'], 'research_binding': inputs['research_binding'],
                **({'candidate_provenance': inputs['candidate_provenance']} if inputs.get('review_only_source') else {}),
                'baseline_sha256': digest(inputs['baseline_inventory']), 'candidate_sha256': candidate,
                'changed_paths': attempt['changed_paths'], 'unified_diff': attempt['diff'], 'checks': attempt['checks']},
            'schema': {'type': 'object', 'properties': fields, 'required': list(fields), 'additionalProperties': False}}
        run_id = work['run_id']
        for role in REVIEWERS:
            self.harness._handoff(run_id, role, **task)
        self.save(work, status='reviewing')
        results = await gather(*(self.harness.ask(run_id, role, **task) for role in REVIEWERS), return_exceptions=True)
        for role, result in zip(REVIEWERS, results):
            if isinstance(result, BaseException):
                continue
            key = self.harness.decision_key(run_id, role, **task)
            authenticate(self.engine, run_id, role, key, result)
            attempt.setdefault('reviews', {})[role] = {'handoff_id': key, 'result': result}
        self.save(work)
        for result in results:
            if isinstance(result, BaseException):
                raise result
        return all(result['approve'] and not result['required_changes'] for result in results)

    def verify_external_feedback(self, work):
        if len(work['attempts']) != 1 or work['attempts'][0].get('index') != 0:
            raise ValueError('External dependency must contain exactly one candidate at index zero')
        attempt = work['attempts'][0]
        inventory = _inventory(self.directory / 'candidate-0')
        checks = attempt.get('checks', {})
        if (inventory != attempt['inventory'] or digest(inventory) != attempt['candidate_sha256']
                or checks.get('candidate_sha256') != attempt['candidate_sha256']):
            raise ValueError('External dependency candidate or check binding changed')
        host = checks['host_receipt']
        report = None
        for artifact in host['artifacts']:
            if _sha(Path(artifact['path'])) != artifact['sha256']:
                raise ValueError('External dependency check artifact changed')
            if Path(artifact['path']).name == Path(REPORT_PATH).name:
                report = json.loads(Path(artifact['path']).read_bytes())
        if report is None or report.get('passed') is not host['passed'] or any(
                report.get('source_sha256', {}).get(path) != value for path, value in inventory.items()):
            raise ValueError('External dependency host-check result changed or is unavailable')
        ballots = attempt.get('reviews', {})
        for role, ballot in ballots.items():
            bound = authenticate(self.engine, work['run_id'], role, ballot['handoff_id'], ballot['result'])
            if (bound['candidate_sha256'] != attempt['candidate_sha256'] or bound['checks'] != checks
                    or bound['work_order'] != work['id'] or bound['work_order_input_sha256'] != work['input_sha256']
                    or ballot['result']['candidate_sha256'] != attempt['candidate_sha256']):
                raise ValueError('External dependency independent review binding changed')
        if host['passed'] and set(ballots) != set(REVIEWERS):
            raise ValueError('External dependency independent reviews are incomplete')
        approved = host['passed'] and all(ballot['result']['approve'] and not ballot['result']['required_changes']
                                          for ballot in ballots.values())
        if (work['status'], attempt.get('outcome')) != (('accepted_candidate', 'accepted_candidate') if approved
                                                       else ('revision_required', 'revise')):
            raise ValueError('External dependency outcome differs from its host check and independent reviews')
        return attempt if approved else None

    async def run(self, work):
        inputs = work['input']
        program = inputs.get('research_binding', {}).get('operation') == 'metric_capability_source_repair_v1'
        if program or inputs.get('source_run_alias') is not None:
            if not program or not inputs.get('source_run_alias') or getattr(self, 'program_budget_owner', None) != {
                    'version': 1, 'run_id': work['run_id'], 'input_sha256': work['input_sha256']}:
                raise ValueError('Program source work must resume through its active parent controller, not the standalone CLI')
        baseline = self.directory / 'baseline'
        source = inputs['research_binding']
        current_binding = (research_binding(self.database, source['run_id'], repair_receipt=source['repair_receipt'])
                           if source.get('repair_receipt') else research_binding(self.database, source['run_id']))
        if digest(inputs) != work['input_sha256'] or current_binding != source:
            raise ValueError('Frozen work-order input or paused source assignment changed')
        if inputs['baseline_inventory'] is None:
            previous_dir = self.directory.parent / inputs['after_work_order']
            path = previous_dir / 'work.sqlite3'
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
                row = db.execute('SELECT data FROM records WHERE collection=? AND key=?',
                                 (COLLECTION, inputs['after_work_order'])).fetchone()
            previous = json.loads(row[0]) if row else None
            if not previous or previous['status'] not in {'accepted_candidate', 'revision_required', 'blocked'}:
                return
            if previous['status'] == 'blocked' and (
                    not previous.get('error') or not previous['attempts']
                    or any(a.get('receipt', {}).get('status') != 'completed' for a in previous['attempts'])):
                return  # The previous worker still has an unresolved implementation receipt.
            if previous['input']['research_binding'] != inputs['research_binding']:
                raise ValueError('Dependent work must preserve the same paused source binding')
            if digest(previous['input']) != previous['input_sha256']:
                raise ValueError('The dependency has inconsistent frozen inputs')
            adopted = None
            if previous['input'].get('review_only_source'):
                adopted = WorkOrder(previous_dir, self.database).verify_external_feedback(previous)
            elif previous['status'] == 'accepted_candidate':
                await WorkOrder(previous_dir, self.database).run(previous)
                if previous['status'] == 'accepted_candidate':
                    adopted = previous['attempts'][-1]
            if inputs.get('require_accepted_dependency') and adopted is None:
                return
            if inputs.get('revise_draft'):
                if previous['input'].get('review_only_source'):
                    raise ValueError('External candidates require a fresh review-from intake after editing the source')
                draft = previous['attempts'][-1]
                checks = draft.get('checks', {})
                if (previous['status'] != 'revision_required' or draft.get('outcome') != 'revise'
                        or draft.get('receipt', {}).get('status') != 'completed'
                        or checks.get('candidate_sha256') != draft.get('candidate_sha256')
                        or not draft.get('inventory') or digest(draft['inventory']) != draft['candidate_sha256']
                        or _inventory(previous_dir / f"candidate-{draft['index']}") != draft['inventory']
                        or not set(draft.get('changed_paths', [])) <= set(inputs['allowed_paths'])):
                    raise ValueError('No sealed revision draft is available within the authorized source scope')
                inputs['draft'] = {'index': draft['index'], 'inventory': draft['inventory'],
                    'candidate_sha256': draft['candidate_sha256'], 'checks': checks}
            source = previous_dir / (f"candidate-{adopted['index']}" if adopted else 'baseline')
            inventory = adopted['inventory'] if adopted else previous['input']['baseline_inventory']
            if not baseline.exists():
                staging = self.directory / f'baseline-staging-{uuid.uuid4().hex}'
                shutil.copytree(source, staging)
                for retry in range(5):
                    try:
                        staging.rename(baseline)
                        break
                    except PermissionError as exc:
                        if getattr(exc, 'winerror', None) not in {5, 32} or retry == 4:
                            raise
                        await asyncio.sleep(.1 * (retry + 1))
            test_files = {name.split('::', 1)[0] for name in inputs['tests']}
            if _inventory(baseline) != inventory or not test_files <= set(inventory) | set(inputs['allowed_paths']):
                raise ValueError('The dependency source does not match the new source scope')
            work['queued_input_sha256'] = work['input_sha256']
            inputs['baseline_inventory'] = inventory
            inputs['dependency_result'] = {'status': previous['status'], 'input_sha256': previous['input_sha256'],
                'candidate_sha256': adopted['candidate_sha256'] if adopted else None, 'error': previous.get('error')}
            evidence_attempts = previous['attempts'][-1:] if inputs.get('revise_draft') else previous['attempts']
            inputs['dependency_feedback'] = [{key: attempt.get(key) for key in ('candidate_sha256', 'changed_paths',
                    'reviews', 'outcome')} | ({'implementation_receipt_sha256': digest(attempt['receipt'])}
                        if attempt.get('receipt') else {'candidate_provenance': previous['input'].get('candidate_provenance'),
                                                        'host_check_sha256': digest(attempt.get('checks'))})
                for attempt in evidence_attempts]
            work['input_sha256'] = digest(inputs)
            self.save(work)
        if _inventory(baseline) != inputs['baseline_inventory']:
            raise ValueError('Frozen baseline source changed')
        for field, profile_field in (('model', 'model'), ('effort', 'deep_reasoning_effort'), ('timeout', 'deep_timeout_seconds')):
            if self.provider[profile_field] != inputs['review'][field]:
                raise ValueError('Independent review routing changed after intake')
        if work['status'] == 'accepted_candidate':
            attempt = work['attempts'][-1]
            if _inventory(self.directory / f"candidate-{attempt['index']}") != attempt['inventory']:
                raise ValueError('Previously accepted candidate source changed')
            for role in REVIEWERS:
                row = attempt['reviews'][role]
                bound = authenticate(self.engine, work['run_id'], role, row['handoff_id'], row['result'])
                if bound['candidate_sha256'] != attempt['candidate_sha256'] or bound['checks'] != attempt['checks']:
                    raise ValueError('Previously accepted candidate review binding changed')
            return
        if work['status'] == 'revision_required':
            return
        run_id = work['run_id']
        self.engine.cancellations[run_id] = asyncio.Event()
        self.engine.update_run(run_id, status='running')
        try:
            if inputs.get('review_only_source'):
                attempt = work['attempts'][0]
                candidate = self.directory / 'candidate-0'
                actual = _inventory(candidate)
                if actual != attempt['inventory'] or digest(actual) != attempt['candidate_sha256']:
                    raise ValueError('External source candidate changed after intake')
                if 'checks' not in attempt:
                    self.save(work, status='checking')
                    attempt['checks'] = await to_thread(_run_check, candidate,
                        self.directory / f'check-0-{uuid.uuid4().hex}', actual, inputs['tests'])
                    self.save(work)
                checks = attempt['checks']
                if (checks['candidate_sha256'] != attempt['candidate_sha256'] or any(
                        _sha(Path(row['path'])) != row['sha256'] for row in checks['host_receipt']['artifacts'])):
                    raise ValueError('External candidate host-check binding changed')
                accepted = checks['host_receipt']['passed'] and await self.review(work, attempt)
                if _inventory(candidate) != actual:
                    raise ValueError('External candidate changed during review')
                attempt['outcome'] = 'accepted_candidate' if accepted else 'revise'
                self.save(work, status='accepted_candidate' if accepted else 'revision_required')
                return
            for index in range(MAX_ATTEMPTS):
                if index < len(work['attempts']) and work['attempts'][index].get('outcome') == 'revise':
                    continue
                candidate = self.directory / f'candidate-{index}'
                if index == len(work['attempts']):
                    previous = work['attempts'][-1] if index else None
                    feedback = ({'checks': previous.get('checks'), 'reviews': previous.get('reviews'),
                                 'runtime_failure': previous.get('runtime_failure')}
                                if previous else inputs.get('draft', {}).get('checks'))
                    instruction = ('Implement this Harness work order in the isolated working directory. Edit only the '
                        'frozen allowed files. Preserve all scientific criteria, independent reviewers, original frozen '
                        'inputs, raw responses and receipts. Do not access or edit the live run or its data. Do not '
                        'install dependencies, train, execute experiments, or use network. The host independently runs '
                        'the fixed CPU tests. Keep the patch focused and include tests for affected paths. Report '
                        'schema, replay, pause/resume, recovery and scheduling impact, including limitations.\n'
                        + json.dumps({'requirement': inputs['operator_requirement'], 'research_binding': inputs['research_binding'],
                            'allowed_paths': inputs['allowed_paths'], 'tests': inputs['tests'], 'feedback': feedback,
                            'dependency_result': inputs.get('dependency_result'),
                            'dependency_feedback': inputs.get('dependency_feedback')}, ensure_ascii=False))
                    attempt = {'index': index, 'operation_id': uuid.uuid4().hex, 'instruction': instruction,
                               'instruction_sha256': digest(instruction)}
                    from .codex_executable import resolve_codex
                    runtime = resolve_codex()
                    if not runtime['available']:
                        raise ValueError('The local Codex execution worker is unavailable')
                    attempt['execution_runtime'] = {**runtime, 'sha256': _sha(Path(runtime['executable']))}
                    if previous and previous.get('runtime_failure') and (
                            attempt['execution_runtime']['executable'] == previous['runtime_failure']['executable']):
                        raise ValueError('The same Codex executable cannot retry the confirmed tool-launch failure')
                    work['attempts'].append(attempt)
                    self.save(work, status='implementing')
                else:
                    attempt = work['attempts'][index]
                if not candidate.exists():
                    if attempt.get('receipt'):
                        raise ValueError('Previously dispatched candidate workspace is missing')
                    staging = self.directory / f'candidate-staging-{uuid.uuid4().hex}'
                    source = self.directory / f'candidate-{index - 1}' if index else baseline
                    expected = work['attempts'][index - 1]['inventory'] if index else inputs['baseline_inventory']
                    if index == 0 and inputs.get('draft'):
                        draft = inputs['draft']
                        source = self.directory.parent / inputs['after_work_order'] / f"candidate-{draft['index']}"
                        expected = draft['inventory']
                    if _inventory(source) != expected:
                        raise ValueError('Previous candidate changed before revision')
                    shutil.copytree(source, staging)
                    staging.rename(candidate)
                await self.implement(work, attempt, candidate)
                after = _inventory(candidate)
                if self.failed_launch(work, attempt, after):
                    runtime = attempt.get('execution_runtime', {})
                    if not runtime:
                        # Older bootstrap records lacked a binary receipt. Use
                        # the separately observed PATH resolution as a diagnosis.
                        from .codex_executable import resolve_codex
                        import os
                        runtime = resolve_codex(environ={k: v for k, v in os.environ.items()
                            if k != 'AUTORESEARCH_CODEX_EXECUTABLE'})
                        if runtime.get('source') != 'path' or 'WindowsApps' not in runtime.get('executable', ''):
                            raise ValueError('Original launcher identity cannot be established')
                    attempt.update(inventory=after, outcome='revise', runtime_failure={
                        'code': 'registered_core_tool_launch_failed', 'executable': runtime['executable'],
                        'launcher_identity_source': 'frozen_executable' if 'execution_runtime' in attempt else 'observed_path_resolution',
                        'receipt_sha256': digest(attempt['receipt'])})
                    self.save(work, status='implementation_revision_pending')
                    continue
                changed, patch = _review_diff(baseline, candidate, inputs['baseline_inventory'], after, inputs['allowed_paths'])
                if 'inventory' in attempt and attempt['inventory'] != after:
                    raise ValueError('Candidate source changed after sealing')
                attempt.update(inventory=after, candidate_sha256=digest(after), diff=patch, changed_paths=changed)
                self.save(work, status='checking')
                if 'checks' not in attempt:
                    output = self.directory / f'check-{index}-{uuid.uuid4().hex}'
                    attempt['checks'] = await to_thread(_run_check, candidate, output, after, inputs['tests'])
                    self.save(work)
                checks = attempt['checks']
                if checks['candidate_sha256'] != digest(_inventory(candidate)):
                    raise ValueError('Candidate changed across host verification')
                for artifact in checks['host_receipt']['artifacts']:
                    if _sha(Path(artifact['path'])) != artifact['sha256']:
                        raise ValueError('Host check artifact changed')
                if checks['host_receipt']['passed'] and await self.review(work, attempt):
                    if _inventory(candidate) != after:
                        raise ValueError('Candidate changed during independent review')
                    attempt['outcome'] = 'accepted_candidate'
                    self.save(work, status='accepted_candidate')
                    return
                attempt['outcome'] = 'revise'
                self.save(work, status='implementation_revision_pending')
            self.save(work, status='revision_required')
        except BaseException as exc:
            self.save(work, status='interrupted' if isinstance(exc, asyncio.CancelledError) else 'blocked',
                      error=redact(str(exc))[:1200])
            raise
        finally:
            self.engine.update_run(run_id, status='completed' if work['status'] == 'accepted_candidate' else 'paused')


def summary(work):
    return {'work_order': work['id'], 'status': work['status'], 'run_id': work['run_id'],
        'input_sha256': work['input_sha256'], 'error': work.get('error'), 'attempts': [
            {'index': row['index'], 'operation_id': row['operation_id'], 'codex': {
                key: row.get('receipt', {}).get(key) for key in ('status', 'thread_id', 'turn_id')},
             'checks_passed': row.get('checks', {}).get('host_receipt', {}).get('passed'),
             'candidate_sha256': row.get('candidate_sha256'), 'reviews': row.get('reviews', {})}
            for row in work['attempts']]}


def main():
    # An independent app-server worker cannot use the enclosing desktop tool
    # session's pipes, registration or approval metadata. Keep the normal
    # native workspace sandbox and native login, as the service launcher does.
    for name in _DESKTOP_SESSION_ENV:
        os.environ.pop(name, None)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('submit', 'resume', 'status'))
    parser.add_argument('order_id', type=lambda value: uuid.UUID(value).hex)
    parser.add_argument('--run-id')
    parser.add_argument('--requirement-file', type=Path)
    parser.add_argument('--predecessor', type=lambda value: uuid.UUID(value).hex)
    parser.add_argument('--after', type=lambda value: uuid.UUID(value).hex)
    parser.add_argument('--revise-draft', action='store_true')
    parser.add_argument('--require-accepted', action='store_true')
    parser.add_argument('--review-from', type=lambda value: uuid.UUID(value).hex)
    parser.add_argument('--queue-only', action='store_true')
    parser.add_argument('--wait-dependency', action='store_true')
    parser.add_argument('--supplement-file', type=Path)
    parser.add_argument('--replace-dependency', type=lambda value: uuid.UUID(value).hex)
    parser.add_argument('--worker-log', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--path', action='append', default=[])
    parser.add_argument('--test', action='append', default=[])
    args = parser.parse_args()
    if args.worker_log:
        from contextlib import redirect_stdout, redirect_stderr
        with args.worker_log.open('x', encoding='utf-8', buffering=1) as stream, redirect_stdout(stream), redirect_stderr(stream):
            return run_cli(parser, args)
    return run_cli(parser, args)


def run_cli(parser, args):
    from .codex_workspace import _plain
    if args.supplement_file and args.action != 'resume':
        parser.error('Only a queued order can accept a supplement at resume')
    if args.replace_dependency and (args.action != 'resume' or not args.supplement_file):
        parser.error('Changing a queued dependency requires an operator supplement')
    if args.revise_draft and (args.action != 'submit' or not args.after):
        parser.error('Revising a draft requires submitting against a source order')
    if args.require_accepted and (args.action != 'submit' or not args.after or args.revise_draft):
        parser.error('An accepted dependency requires submitting against a source order without draft reuse')
    if args.review_from and (args.action != 'submit' or args.after or args.predecessor):
        parser.error('External candidate review requires its own submission and baseline')
    directory = ROOT / ROOT_DIRECTORY / args.order_id
    _plain(directory)
    if args.action == 'submit':
        if not args.run_id or not args.requirement_file:
            parser.error('submit requires --run-id and --requirement-file')
        directory.mkdir(parents=True, exist_ok=False)
    elif not (directory / 'work.sqlite3').is_file():
        parser.error('work order does not exist')
    if args.action == 'status':
        with closing(sqlite3.connect((directory / 'work.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
            row = db.execute('SELECT data FROM records WHERE collection=? AND key=?', (COLLECTION, args.order_id)).fetchone()
        if row is None:
            parser.error('work order intake did not complete')
        print(json.dumps(summary(json.loads(row[0])), ensure_ascii=False), flush=True)
        return
    if os.name == 'nt':
        import tempfile
        system_temp = Path(os.environ['SystemRoot']) / 'Temp'
        try:
            with tempfile.TemporaryDirectory(dir=system_temp):
                pass
        except OSError:
            pass
        else:
            # This is the dedicated worker process. Keep all test data inside
            # the bounded host check, but shorten the OS-owned staging prefix.
            tempfile.tempdir = str(system_temp)
    import msvcrt
    with (directory / 'worker.lock').open('a+b') as lock:
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        worker = WorkOrder(directory, DATA_DIR / 'research.sqlite3')
        if args.action == 'submit':
            requirement = args.requirement_file.read_text(encoding='utf-8-sig').strip()
            if not requirement or len(requirement) > 16000:
                parser.error('operator requirement must contain 1 to 16000 characters')
            predecessor = None
            if args.predecessor:
                path = directory.parent / args.predecessor / 'work.sqlite3'
                _plain(path)
                with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
                    row = db.execute('SELECT data FROM records WHERE collection=? AND key=?',
                                     (COLLECTION, args.predecessor)).fetchone()
                predecessor = json.loads(row[0]) if row else None
                if (not predecessor or predecessor['status'] != 'revision_required'
                        or any(not a.get('runtime_failure') for a in predecessor['attempts'])):
                    raise ValueError('Predecessor does not have terminal tool-launch failure receipts')
                prior = predecessor['input']
                if (prior['operator_requirement'] != requirement or prior['research_binding']['run_id'] != args.run_id
                        or prior['allowed_paths'] != args.path or prior['tests'] != args.test):
                    raise ValueError('Successor scope must match the terminal predecessor')
            if args.after and (args.after == args.order_id or args.predecessor):
                raise ValueError('A dependent order requires a different source order')
            if args.after and not (directory.parent / args.after / 'work.sqlite3').is_file():
                raise ValueError('Dependent source work order does not exist')
            if args.revise_draft:
                dependency = directory.parent / args.after / 'work.sqlite3'
                with closing(sqlite3.connect(dependency.as_uri() + '?mode=ro', uri=True)) as db:
                    row = db.execute('SELECT data FROM records WHERE collection=? AND key=?',
                                     (COLLECTION, args.after)).fetchone()
                if row and json.loads(row[0])['input'].get('review_only_source'):
                    raise ValueError('For an external source candidate, edit the source and submit a new review-from order')
            work = worker.submit(args.order_id, args.run_id, requirement, args.path, args.test, after=args.after,
                                 revise_draft=args.revise_draft, require_accepted=args.require_accepted,
                                 review_from=args.review_from)
            if predecessor:
                work['input']['predecessor'] = {'work_order_id': predecessor['id'], 'input_sha256': predecessor['input_sha256'],
                    'attempt_receipts': [a['runtime_failure']['receipt_sha256'] for a in predecessor['attempts']]}
                work['input_sha256'] = digest(work['input'])
                worker.save(work)
        else:
            work = worker.store.get(COLLECTION, args.order_id)
            if not work:
                parser.error('work order intake did not complete')
            if args.supplement_file:
                inputs = work['input']
                if (work['status'] != 'queued' or work['attempts']
                        or inputs['baseline_inventory'] is not None
                        or digest(inputs) != work['input_sha256']):
                    raise ValueError('The supplement requires an intact, undispatched queued order')
                supplement = args.supplement_file.read_text(encoding='utf-8-sig').strip()
                updated = inputs['operator_requirement'] + '\n\n' + supplement
                if not supplement or len(updated) > 16000:
                    parser.error('The combined operator requirement must contain at most 16000 characters')
                work.setdefault('prior_intakes', []).append(
                    {'input': inputs, 'input_sha256': work['input_sha256'], 'updated_at': work['updated_at']})
                work['input'] = {**inputs, 'operator_requirement': updated,
                                 'prior_intake_sha256': work['input_sha256']}
                if args.replace_dependency:
                    path = directory.parent / args.replace_dependency / 'work.sqlite3'
                    _plain(path)
                    if args.replace_dependency == args.order_id or not inputs['after_work_order'] or not path.is_file():
                        raise ValueError('Replacement dependency must be an existing different source order')
                    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
                        row = db.execute('SELECT data FROM records WHERE collection=? AND key=?',
                            (COLLECTION, args.replace_dependency)).fetchone()
                    predecessor = json.loads(row[0]) if row else {}
                    if (predecessor.get('input', {}).get('after_work_order') != inputs['after_work_order']
                            or predecessor['input'].get('research_binding') != inputs['research_binding']):
                        raise ValueError('Replacement dependency must continue the same previous order and paused source')
                    work['input']['after_work_order'] = args.replace_dependency
                work['input_sha256'] = digest(work['input'])
                worker.save(work)
        print(json.dumps(summary(work), ensure_ascii=False), flush=True)
        if not args.queue_only:
            async def execute():
                for _ in range(121 if args.wait_dependency else 1):
                    await worker.run(work)
                    if work['status'] != 'queued' or not args.wait_dependency:
                        return
                    await asyncio.sleep(15)
            asyncio.run(execute())
            print(json.dumps(summary(work), ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
