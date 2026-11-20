"""Harness-owned capability adoption and author recovery for reading stalls.

The external capability candidate has no research authority. Only actual peer
deliveries adopt it. Old inputs, observations, receipts, drafts and billing are
retained; a successor is a new ordinary author assignment, never an old answer.
"""
from __future__ import annotations

import ast
import copy
import json
from pathlib import Path

from .async_ownership import gather, to_thread
from .document_read_convergence import PROTOCOL, VERSION, ReadingStalled, analyze, diagnose, digest
from .harness_delivery import authenticate
from .storage import now

KIND = 'document_efficiency'
STAGE_RESTART = 'restart_implementation_stage'
COMPACT_MATERIALS = 'compact_experiment_materials'
ROOT = Path(__file__).resolve().parents[1]
REQUIREMENT = ('Repair generic content deduplication/references, task document projections, '
    'read convergence and stall detection. Preserve all original evidence and successful history. '
    'Require isolated checks and independent review, then Harness-owned recovery and dispatch. '
    'Roll back unfinished document authoring to the end of approved experiment preparation. '
    'Archive and clear its exact owned writing memory, context and unfinished products, '
    'then have the assigned agent write a concise but complete new specification. '
    'Never manually edit memory, alter scientific criteria or bypass a blocker.')
GUIDANCE = ('\n资料与收敛协议：先按 task_document_briefs 阅读当前任务字段，reference 指向 '
    'task_document_definitions 中完整哈希的精确定义。需要时才定向补读原文。视图逐字段可还原，'
    '不是新科学定义或批准。已取回相同版本的范围不要反复读取；仅改变分页大小不算新证据。'
    '阅读资料是为落实当前实现规范，证据足够后使用原保存工具真正保存、检查和提交。'
    '从已批准架构和冻结实验计划重新编写，不沿用旧撰写记忆或未完成稿。正文精简、全面：'
    '完整覆盖方法定义、输入输出、模块接口、YAML 参数消费、训练与评估隔离、资源和验收检查。'
    '共同定义集中写一次，其他位置引用其版本和路径；不要搬运整段来源全文或重复机器 JSON。'
    '未知问题须指出具体缺失字段、原文位置和对当前工作影响，不能以不断重读代替交付。')
FOCUSED_DRAFT_GUIDANCE = ('\n根据当前批准架构编写并尽早保存实现规范。共同定义集中写一次，'
    '覆盖方法、接口、运行配置和可执行的验收检查。只有需要解决具体实现缺口时才按精确位置补读。'
    '对缺失值或冲突指出具体字段，不要重读整份实验计划或等待后续实验结果。')


# Exact source slices, not regenerated code. Full-file hashes remain authoritative.
READBACK_SELECTION = {
    'autoresearch/local_codex.py': {
        'run', '_unsubmitted_initialization', '_recover', '_validate_readback_items', '_verify_thread', '_read',
        'delete_completed_reasoning_thread',
        '_turn', '_item', '_activity', '_save', '_open', '_rpc', '_receive', '_write',
        '_event', '_deny', '_interrupt_owned', '_initialize_reasoning_connection',
        '_reasoning_account', '_can_retry_account', '_can_retry_initialization_connection',
        '_valid_account_retries', '_valid_initialization_connection_retries',
        '_valid_initialization_diagnostic', '_initialization_diagnostic', '_invoke',
        '_directory', '_hash', '_public_text', '_server_error', '_close',
        '__init__', '_now', '_drain_stderr', '_await_cleanup', 'close_owned_process',
        'spawn_codex', 'child_environment', '_account_failure_category',
        '_initialize_failure_category', '_account_retry_wait', '_initialization_connection_retry_wait',
    },
    'tests/test_local_codex.py': {'Writer', 'Process', 'methods', 'readback_transport',
        'trace_readback', 'assert_readback_transport', 'test_real_run_readback_terminal_matrix',
        'test_completed_reasoning_cleanup_readback'},
}


def selected_source(text, symbols):
    lines = text.splitlines(keepends=True)
    nodes = [n for n in ast.walk(ast.parse(text))
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
             and n.name in symbols]
    found = {n.name for n in nodes}
    if found != symbols:
        raise ValueError('Review source symbols missing: ' + repr(sorted(symbols - found)))
    spans = [{'symbol': n.name,
              'start_line': min([n.lineno, *[d.lineno for d in n.decorator_list]]),
              'end_line': n.end_lineno} for n in nodes]
    # Module constants/imports define receipt versions, enums and identity gates.
    spans += [{'symbol': '<module>', 'start_line': n.lineno, 'end_line': n.end_lineno}
              for n in ast.parse(text).body
              if isinstance(n, (ast.Assign, ast.AnnAssign, ast.Import, ast.ImportFrom))]
    spans.sort(key=lambda n: n['start_line'])
    return '\n'.join(''.join(lines[n['start_line'] - 1:n['end_line']]) for n in spans), spans


def sources():
    root = ROOT
    selected = {
        'autoresearch/api.py': set(),
        'autoresearch/engine.py': {'prepare', '_document_prepare', '_working_request', '_resume_pending_tools'},
        'autoresearch/codex_workflow.py': {'implement', '_frozen_spec_assignment', 'compile_author_assignment'},
        'autoresearch/document_working_notes.py': {'prepare', 'capture'},
        'autoresearch/implementation_documents.py': {'original_plan_text', '_save_planning_context', 'planning_context',
                                                    'planning_context_async', 'codex_instruction'},
        'autoresearch/implementation_authoring_completion.py': {'ask'},
        'autoresearch/document_read_window.py': {'project', '_range_project'},
        'autoresearch/implementation_phase_context.py': {'project_author'},
    }
    selected.update(READBACK_SELECTION)
    names = [*selected, 'autoresearch/task_document_projection.py', 'autoresearch/document_read_convergence.py',
        'autoresearch/document_efficiency_recovery.py', 'autoresearch/document_author_cleanup.py',
        'autoresearch/document_recovery_receipts.py', 'tests/test_document_efficiency.py',
        'tests/test_document_efficiency_replay.py',
        'tests/test_document_efficiency_ownership.py',
        'autoresearch/implementation_phase_context.py',
        'autoresearch/document_author_lineage.py', 'autoresearch/document_ownership.py',
        'autoresearch/implementation_restart.py', 'autoresearch/implementation_stage_cleanup.py',
        'autoresearch/experiment_materials.py', 'autoresearch/experiment_materials_review.py',
        'autoresearch/implementation_acceptance_flow.py',
        'autoresearch/run_control_requests.py',
        'tests/test_implementation_stage_cleanup.py', 'tests/test_experiment_materials.py',
        'tests/test_implementation_phase_context.py', 'autoresearch/implementation_stage_readback.py',
        'docs/DOCUMENT_EFFICIENCY.md', 'scripts/run_source_bound_checks.py']
    result = []
    for name in names:
        text = (root / name).read_text(encoding='utf-8-sig')
        content = text
        ranges = None
        if name in READBACK_SELECTION:
            content, ranges = selected_source(text, selected[name])
        elif name in selected:
            lines = text.splitlines(keepends=True)
            content = '\n'.join(''.join(lines[n.lineno - 1:n.end_lineno]) for n in ast.walk(ast.parse(text))
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in selected[name])
            if name == 'autoresearch/api.py':
                content = ''.join(line for line in lines if 'register_document_efficiency' in line)
                content += '\n'.join(''.join(lines[n.lineno - 1:n.end_lineno]) for n in ast.walk(ast.parse(text))
                    if isinstance(n, ast.AsyncFunctionDef) and n.name in {'implementation_restart_preview', 'implementation_restart'})
            if name == 'autoresearch/codex_workflow.py':
                content += '\n' + '\n'.join(''.join(lines[n.lineno - 1:n.end_lineno]) for n in ast.parse(text).body
                    if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in {
                        'SPEC', 'SPEC_TASK_V2', 'FORMAT_CONTRACT'} for t in n.targets))
        result.append({'path': name, 'sha256': __import__('hashlib').sha256((root / name).read_bytes()).hexdigest(),
            'source': content, 'provenance': 'external_operator_authorized_capability_candidate',
            **({'selection_version': 1, 'source_ranges': ranges} if ranges is not None else {})})
    # Conservative dependency closure: dynamic tool/routing imports make a
    # short manual whitelist unsafe. Fingerprints do not inject all modules
    # into the model context, but any runtime change requires fresh checks.
    dependencies = [*root.joinpath('autoresearch').rglob('*.py'), root / 'AGENTS.md', root / 'pyproject.toml']
    for path in sorted(dependencies):
        name = path.relative_to(root).as_posix()
        if name not in names:
            result.append({'path': name, 'sha256': __import__('hashlib').sha256(path.read_bytes()).hexdigest(),
                'provenance': 'runtime_dependency_fingerprint'})
    return result


def scope(state):
    # Freeze unknown future semantic fields too. Only declared controller
    # progress/output fields are excluded from the reusable code-adoption key.
    control = {'status', 'current_revision', 'specifications', 'attempts', 'updated_at', 'blocked_kind', 'reason',
               'thread_id', 'turn_id', 'result', 'input_bundle', 'acceptance_review', 'spec_hash'}
    return copy.deepcopy({k: v for k, v in state.items() if k not in control})


def enabled(engine, run_id, state):
    """Adopt for a durable operator work request or a deployed capability.

    A clean legacy store does not silently change frozen fixture/paid flows.
    After real adoption the deployment applies to future task domains too.
    """
    deployed = engine.store.get('harness_capabilities', KIND)
    if deployed:
        work = engine.store.get('stage_contexts', deployed['work_order_id'], {})
        if (deployed.get('status') == 'accepted' and work.get('status') == 'accepted'
                and deployed.get('capability_sha256') == work.get('source', {}).get('capability_sha256')):
            return True
    if engine.store.get('runs', run_id, {}).get('config', {}).get('document_efficiency_protocol') == VERSION:
        return True
    return any(r.get('capability') == KIND and r.get('status') == 'accepted'
               for r in engine.store.list('harness_capability_requests', run_id))


def enqueue(engine, run_id, payload):
    """Ordinary operator intake; no model, target cursor or research state edit."""
    import uuid
    if (not isinstance(payload, dict) or set(payload) != {'request_id', 'capability', 'requirement', 'scope'}
            or payload['capability'] != KIND
            or payload['scope'] not in {'restart_unfinished_document_authoring', STAGE_RESTART, COMPACT_MATERIALS}
            or not isinstance(payload['request_id'], str)
            or not isinstance(payload['requirement'], str) or not payload['requirement'].strip()):
        raise ValueError('Capability intake requires an exact supported scope and operator requirement')
    request_id = str(uuid.UUID(payload['request_id']))
    key = run_id + ':capability-request:' + request_id
    previous = engine.store.get('harness_capability_requests', key)
    if previous:
        if previous['operator_payload'] != payload:
            raise ValueError('Capability request id already binds a different requirement')
        return previous
    run = engine.store.get('runs', run_id)
    if not run or run['mode'] != 'live' or run['stage'] != 'implementation' or run['status'] not in {'paused', 'blocked'}:
        raise ValueError('Pause the live implementation worker before queuing its capability repair')
    row = {'id': key, 'run_id': run_id, 'capability': KIND, 'status': 'accepted',
        'operator_payload': copy.deepcopy(payload), 'created_at': now(), 'model_dispatched': False,
        'instruction': 'Ordinary start/resume lets the Harness freeze the actual controller cursor and dispatch review.'}
    engine.store.put('harness_capability_requests', key, row)
    engine.store.event(run_id, KIND + '.requested', 'Harness 已接收通用能力修复与文档重写要求，尚未派发模型',
        {'request_id': key, 'scope': payload['scope']})
    return row


def register(app, engine, require_run):
    from fastapi import HTTPException
    @app.post('/api/runs/{run_id}/harness-capability-requests', status_code=202)
    def document_efficiency_work_order(run_id: str, payload: dict):
        require_run(run_id)
        try:
            return enqueue(engine, run_id, payload)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    reviews = {}
    async def review_paused_materials(run_id: str):
        require_run(run_id)
        import asyncio
        from .storage import redact
        if run_id in reviews and not reviews[run_id].done():
            return {'status': 'reviewing', 'run_id': run_id}
        async def execute():
            try:
                await review_paused(engine, run_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                engine.store.event(run_id, KIND + '.review_failed', redact(str(exc))[:2000])
        reviews[run_id] = asyncio.create_task(execute(), name=f'compact-materials-review:{run_id}')
        return {'status': 'reviewing', 'run_id': run_id}
    runtime = getattr(app.state, 'task_runtime', None)
    app.post('/api/runs/{run_id}/experiment-materials-review', status_code=202)(
        runtime.control(review_paused_materials) if runtime else review_paused_materials)


async def review_paused(engine, run_id):
    """Run capability review under a separate execution lease while research stays paused."""
    import asyncio
    import os
    import psutil
    from .config import get_settings
    from .engine import Harness
    from .storage import uid

    async with engine._lifecycle_lock:
        run = engine.store.get('runs', run_id, {})
        if run.get('status') != 'paused' or run.get('stage') != 'implementation' or run.get('mode') != 'live':
            raise ValueError('Pause the live implementation task before reviewing compact materials')
        if any(not t.done() for t in engine.tasks.values()):
            raise ValueError('An active research worker must finish before capability review')
        from .run_interruption import draining
        if draining(engine.store, run_id):
            raise ValueError('Research pause is still draining')
        requests = [r for r in engine.store.list('harness_capability_requests', run_id)
                    if r.get('capability') == KIND and r.get('status') == 'accepted']
        if not materials_only({'operator_requests': requests}):
            raise ValueError('A forward-only compact materials work request is required')
        generation = run.get('state', {}).get('implementation_generation')
        candidates = [row for row in engine.store.list('codex_workflows', run_id)
                      if row.get('cycle') == engine.cycle(run_id)
                      and row.get('implementation_generation') == generation and row.get('kind') == 'implementation']
        if len(candidates) != 1:
            raise ValueError('Paused implementation requires one current workflow')
        # The review worker uses the ordinary Harness and accounting store.
        # Its run reader exposes execution status only to its own named
        # operation, and does not write or resume the paused research run.
        from .experiment_materials_review import execute
        lease = {'id': 'worker', 'held': True, 'run_id': run_id, 'owner_instance': uid(),
            'owner_pid': os.getpid(), 'owner_started': psutil.Process().create_time()}
        def claim(old):
            if old and old.get('held') and engine.owner_alive(old):
                raise ValueError('A workstation execution lease is already held')
            return lease
        engine.store.mutate('runtime', 'worker', claim)
        try:
            return await execute(engine, run_id, candidates[0], run, Harness, get_settings(engine.store))
        finally:
            engine.store.mutate('runtime', 'worker',
                lambda old: {**old, 'held': False} if old == lease else old)


def _packet(engine, run_id, hid):
    return _validated_packet(engine, run_id, hid, engine.store.get('handoff_inputs', hid) or {},
        engine.store.get('handoff_progress', hid) or {})


def _validated_packet(engine, run_id, hid, inputs, progress):
    from .engine import Harness, ResearchBlocked
    handoff = engine.store.get('handoffs', hid) or {}
    task = {k: inputs.get(k) for k in ('instruction', 'context', 'schema')}
    if (handoff.get('run_id') != run_id or inputs.get('run_id') != run_id
            or handoff.get('input_hash') != digest(task)
            or Harness.decision_key(run_id, handoff.get('to_role'), handoff.get('purpose'), **task) != hid
            or progress and (progress.get('id') != hid or progress.get('run_id') != run_id)):
        raise ResearchBlocked('Document recovery source does not match its actual frozen assignment')
    return {'handoff': handoff, 'input': inputs, 'progress': progress}



# Versioned CPU acceptance matrix; these are transport cases, not scientific jobs.
# v2 records stack-qualified calls. v1 symbol-only traces cannot distinguish
# the preflight guard from the run guard and are not new admission evidence.
# Existing frozen packets and receipts remain immutable.
READBACK_ERRORS = {
    'missing_items': 'codex_readback_items_missing',
    'nonarray_items': 'codex_readback_items_not_array',
    'missing_message_id': 'codex_readback_public_id_invalid',
    'nonstring_text': 'codex_readback_public_text_invalid',
    'invalid_item': 'codex_readback_item_invalid',
    'invalid_phase': 'codex_readback_public_phase_invalid',
    'duplicate_message_id': 'codex_readback_public_id_duplicate',
}
READBACK_EVIDENCE_VERSION = 2
LOCAL_READBACK_CASES = [(terminal, mismatch, output)
    for terminal, mismatch in [('failed', None), ('interrupted', None), ('cancelled', None),
        ('completed', None), ('inProgress', None), ('unknown', None),
        ('interrupted', 'thread'), ('interrupted', 'turn'), ('interrupted', 'operation'),
        ('interrupted', 'workspace')] for output in (False, True)] + [
    ('failed', defect, output) for defect in READBACK_ERRORS for output in (False, True)]
RECONCILE_READBACK_CASES = [
    ('unknown', 'interrupted', False), ('interrupted', 'interrupted', False),
    ('failed', 'failed', False), ('cancelled', 'cancelled', False), ('unknown', 'completed', False),
    ('interrupted', 'interrupted', True), ('unknown', 'unknown', False),
    ('failed', 'failed', True), ('cancelled', 'cancelled', True), ('unknown', 'completed', True),
    ('unknown', 'inProgress', False), ('unknown', 'mismatched', False),
    ('unknown', 'foreign_thread', False), ('unknown', 'foreign_operation', False),
    ('unknown', 'foreign_workspace', False)] + [
    ('failed', defect, output) for defect in READBACK_ERRORS for output in (False, True)]


def validate_transport_evidence(boundary, code):
    """Validate host-emitted properties, never promote parser fixtures to execution.

    Authentic execution still requires the host-owned report and source hashes;
    this validator cannot establish the origin of an arbitrary supplied file.
    """
    hashes = {s['path']: s['sha256'] for s in code}
    local = 'autoresearch/local_codex.py'
    reconcile = 'autoresearch/implementation_stage_readback.py'
    required_hashes = {local, reconcile, 'tests/test_local_codex.py', 'tests/test_document_efficiency.py'}
    required_calls = ['LocalCodexClient.run', 'LocalCodexClient._unsubmitted_initialization',
        'LocalCodexClient._initialize_reasoning_connection', 'LocalCodexClient._recover',
        'LocalCodexClient._read', 'LocalCodexClient._verify_thread']
    expected = {
        'LocalCodexClient.run': {json.dumps(list(c)) for c in LOCAL_READBACK_CASES},
        'implementation_stage_readback.reconcile': {json.dumps(list(c)) for c in RECONCILE_READBACK_CASES}}
    seen = {entry: set() for entry in expected}
    for row in boundary:
        def reject():
            raise ValueError('Document checks contain invalid transport evidence')
        if not isinstance(row, dict):
            reject()
        entry = row.get('entry')
        if (type(row.get('version')) is not int or row['version'] != READBACK_EVIDENCE_VERSION
                or row.get('fixture_only', False) is not False
                or row.get('provenance') != 'offline_process_factory_real_run'
                or not isinstance(entry, str) or entry not in expected
                or type(row.get('mutation_count')) is not int or row['mutation_count'] != 0
                or row.get('methods') != ['initialize', 'initialized', 'account/read', 'thread/read']
                or row.get('source_path') != local or row.get('source_sha256') != hashes.get(local)):
            reject()
        bindings = row.get('source_hashes')
        if (not isinstance(bindings, dict) or not required_hashes <= bindings.keys()
                or any(path not in hashes or hashes[path] != sha for path, sha in bindings.items())):
            reject()
        case = row.get('case')
        if (not isinstance(case, list) or len(case) != 3 or type(case[-1]) is not bool
                or json.dumps(case) not in expected[entry]):
            reject()
        calls = row.get('observed_calls')
        if not isinstance(calls, list) or not calls:
            reject()
        for call in calls:
            if (not isinstance(call, dict) or not isinstance(call.get('symbol'), str)
                    or call.get('source_path') not in {local, reconcile}
                    or type(call.get('line')) is not int or call['line'] < 1
                    or not isinstance(call.get('call_path'), list) or not call['call_path']
                    or any(not isinstance(s, str) for s in call['call_path'])
                    or call['call_path'][-1] != call['symbol']):
                reject()
        symbols = [c['symbol'] for c in calls if c['source_path'] == local]
        if any(symbol not in symbols for symbol in required_calls + [
                'LocalCodexClient._open', 'LocalCodexClient._rpc',
                'LocalCodexClient._receive', 'LocalCodexClient._write']):
            reject()
        # Bind each required call to its actual run ancestry, not the first
        # occurrence of its name anywhere in the trace. Reconcile invokes the
        # same guard earlier from _read_inputs; that is independent evidence.
        prefix = ['reconcile'] if entry.endswith('.reconcile') else []
        run_path = prefix + [required_calls[0]]
        client_paths = [run_path, run_path + [required_calls[1]],
            run_path + [required_calls[2]], run_path + [required_calls[3]],
            run_path + [required_calls[3], required_calls[4]],
            run_path + [required_calls[3], required_calls[4], required_calls[5]]]
        def position(source, path):
            matches = [i for i, call in enumerate(calls)
                       if call['source_path'] == source and call['call_path'] == path]
            if len(matches) != 1:
                reject()
            return matches[0]
        positions = [position(local, path) for path in client_paths]
        if positions != sorted(positions):
            reject()
        for symbol in ('LocalCodexClient._open', 'LocalCodexClient._rpc',
                       'LocalCodexClient._receive', 'LocalCodexClient._write'):
            if not any(i > positions[0] and call['source_path'] == local
                       and call['symbol'] == symbol
                       and call['call_path'][:len(run_path)] == run_path
                       for i, call in enumerate(calls)):
                reject()
        if entry.endswith('.reconcile'):
            preflight = [position(reconcile, ['reconcile']),
                position(reconcile, ['reconcile', '_read_inputs']),
                position(local, ['reconcile', '_read_inputs', required_calls[1]]), positions[0]]
            if preflight != sorted(preflight):
                reject()
            mismatch = case[1] in {'mismatched', 'foreign_thread', 'foreign_operation', 'foreign_workspace'}
        else:
            mismatch = case[1] is not None
        malformed = case[1] in READBACK_ERRORS
        if malformed and (row.get('status') != 'unknown' or row.get('recovered_read_only') is not False
                          or row.get('error_code') != READBACK_ERRORS[case[1]]):
            reject()
        if malformed:
            if position(local, run_path + [required_calls[3], 'LocalCodexClient._validate_readback_items']) <= positions[-1]:
                reject()
        mismatch = mismatch or malformed
        if ('LocalCodexClient._turn' in symbols) is mismatch:
            reject()
        if not mismatch:
            if position(local, run_path + [required_calls[3], 'LocalCodexClient._turn']) <= positions[-1]:
                reject()
        path = [c['symbol'] for c in calls if c['symbol'] in required_calls + ['LocalCodexClient._turn']]
        if entry.endswith('.reconcile'):
            path.insert(0, entry)
        if row.get('runtime_path') != ' -> '.join(path):
            reject()
        seen[entry].add(json.dumps(case))
    if seen != expected:
        raise ValueError('Document checks lack complete source-bound transport evidence matrix')


def _checks(code):
    root = ROOT
    report = json.loads((root / 'outputs/diagnostics/document-efficiency-checks.json').read_text(encoding='utf-8'))
    if (report['exit_code'] != 0 or not report['source_unchanged_during_checks'] or report.get('timed_out')
            or any(report['source_hashes'].get(s['path']) != s['sha256'] for s in code)):
        raise ValueError('Document efficiency candidate lacks passing checks for its exact source')
    for path_key, sha_key in (('junit_path', 'junit_sha256'), ('log_path', 'log_sha256')):
        path = Path(report[path_key]).resolve()
        if not path.is_relative_to(root / 'outputs/diagnostics') or __import__('hashlib').sha256(path.read_bytes()).hexdigest() != report[sha_key]:
            raise ValueError('Document check evidence file changed or escaped its report directory')
    from xml.etree import ElementTree as ET
    suites = list(ET.parse(report['junit_path']).iter('testsuite'))
    if not suites or sum(int(s.get('tests', 0)) for s in suites) < 1 or any(int(s.get(k, 0)) for s in suites for k in ('failures', 'errors')):
        raise ValueError('Document check JUnit does not contain a completed passing suite')
    boundary, case_bindings = [], []
    owners = {
        'LocalCodexClient.run': ('tests.test_local_codex', 'test_real_run_readback_terminal_matrix['),
        'implementation_stage_readback.reconcile': ('tests.test_document_efficiency',
            'test_forward_only_recovery_preserves_frozen_unlimited_author[')}
    for suite in suites:
        for case in suite.findall('testcase'):
            for prop in case.findall('properties/property'):
                if prop.get('name') != 'readback_transport':
                    continue
                try:
                    row = json.loads(prop.get('value', '{}'))
                except (TypeError, ValueError) as exc:
                    raise ValueError('Malformed transport evidence JSON') from exc
                entry = row.get('entry') if isinstance(row, dict) else None
                owner = owners.get(entry) if isinstance(entry, str) else None
                if (owner is None or case.get('classname') != owner[0]
                        or not case.get('name', '').startswith(owner[1])
                        or any(case.find(tag) is not None for tag in ('skipped', 'failure', 'error'))):
                    raise ValueError('Document checks contain unbound transport evidence testcase')
                boundary.append(row)
                case_bindings.append({'classname': case.get('classname'), 'name': case.get('name'),
                                      'evidence_sha256': digest(row)})
    validate_transport_evidence(boundary, code)
    return {**{k: report[k] for k in ('provenance', 'exit_code', 'summary', 'source_unchanged_during_checks', 'junit_sha256', 'log_sha256')},
        'known_incomplete_checks': report.get('known_incomplete_checks', []),
        'runtime_boundary_evidence': boundary, 'runtime_boundary_cases': case_bindings}


async def adopt(engine, h, run_id, state):
    from .engine import ResearchBlocked
    code = await to_thread(sources)
    operator_requests = [r for r in engine.store.list('harness_capability_requests', run_id)
                         if r.get('capability') == KIND and r.get('status') == 'accepted']
    if not operator_requests:
        cert = engine.store.get('harness_capabilities', KIND, {})
        accepted = engine.store.get('stage_contexts', cert.get('work_order_id', ''), {})
        if (cert.get('status') == 'accepted' and cert.get('capability_sha256') == digest(code)
                and materials_only(accepted)
                and accepted.get('source', {}).get('capability_sha256') == cert['capability_sha256']):
            validate_approvals(engine, accepted['run_id'], accepted, accepted['source'])
            return accepted
    source = {'run_id': run_id, 'workflow_scope_sha256': digest(scope(state)), 'capability_sha256': digest(code),
              'operator_requests_sha256': digest(operator_requests)}
    key = run_id + ':' + KIND + ':' + digest(source)
    work = engine.store.get('stage_contexts', key)
    if work is None:
        prior_reviews = []
        for previous in engine.store.list('stage_contexts', run_id):
            if (previous.get('id', '').startswith(run_id + ':' + KIND + ':')
                    and previous.get('status') == 'capability_revision_required'):
                for role, vote in previous.get('reviews', {}).items():
                    authenticate(engine, run_id, role, vote['handoff_id'], vote['result'])
                prior_reviews.append({'work_order_id': previous['id'], 'source': previous['source'],
                    'reviews': copy.deepcopy(previous['reviews'])})
        from .codex_workflow import _frozen_spec_author, _digest
        purpose = f'implementation_spec:{_digest(state["id"])}:{state["current_revision"]}'
        current = engine.store.get('document_author_cursors', state['id'] + ':document-author:' + str(state['current_revision']))
        frozen = (engine.store.get('handoff_inputs', current['handoff_id']) if current
                  else _frozen_spec_author(engine, run_id, purpose))
        cursor = None
        if frozen:
            from .document_author_lineage import resolve
            packet = await to_thread(resolve, engine, run_id, frozen['id'])
            cursor = {'handoff_id': packet['handoff']['id'], 'input_sha256': digest(packet['input']),
                'progress_sha256': digest(packet['progress']), 'status': packet['handoff']['status'],
                'diagnostic': diagnose(engine, run_id, 'engineer', packet['handoff']['id'], packet['progress'], packet['input']['context'])}
        if state.get('implementation_generation'):
            restart_id = engine.store.get('runs', run_id, {}).get('state', {}).get('implementation_restart')
            restarted = engine.store.get('implementation_restarts', restart_id, {}) if restart_id else {}
            adopted_id = restarted.get('stage_cleanup', {}).get('work_order_id')
            adopted = engine.store.get('stage_contexts', adopted_id, {}) if adopted_id else {}
            if (restarted.get('generation') == state['implementation_generation']
                    and restarted.get('stage_cleanup', {}).get('status') == 'completed'
                    and adopted.get('source', {}).get('capability_sha256') == source['capability_sha256']
                    and adopted.get('operator_requests') == operator_requests
                    and not stage_restart_pending(engine, run_id, adopted)):
                validate_approvals(engine, run_id, adopted, adopted['source'])
                return adopted
        requirement = ('Provide lossless compact materials for future implementation requests. '
                       'Use one exact protocol definition per content hash, a shared template, complete combination '
                       'table and exact differences. Preserve all existing handoffs, original evidence and approved plan. '
                       'Use the approved architecture as the primary implementation source. Provide exact optional locations '
                       'for missing frozen values, differences or conflicts by criterion. Preserve existing architecture '
                       'and specification evidence gates and authenticate any original ranges actually cited. '
                       'No blanket experiment-plan or paper-protocol reading. Preserve stalled legacy author requests, '
                       'read back their exact native turns, and use ordinary Harness clarification before freezing a '
                       'new assignment. No rollback, cleanup, or replay of the stalled frozen request.'
                       if materials_only({'operator_requests': operator_requests}) else REQUIREMENT)
        work = {'id': key, 'run_id': run_id, 'source': source, 'status': 'reviewing',
            'operator_requirement': requirement, 'operator_requests': operator_requests,
            'prior_capability_reviews': prior_reviews,
            'implementation_provenance': 'external_capability_bootstrap',
            'controller_cursor': {'workflow_id': state['id'], 'revision': state['current_revision'], 'assignment': cursor},
            'code': code, 'checks': await to_thread(_checks, code), 'reviews': {}, 'created_at': now()}
        engine.store.put('stage_contexts', key, work)
        engine.store.event(run_id, KIND + '.prepared', 'Harness 已冻结通用资料与阅读收敛能力工单',
            {'work_order_id': key, 'source': source, 'controller_cursor': work['controller_cursor']})
    if work['source'] != source:
        raise ResearchBlocked('Document capability work order source changed')
    if work['status'] != 'accepted':
        compact = materials_only(work)
        review_focus = (
            'Review the forward-only experiment materials request. Check exact reconstruction, strict types, full '
            'task coverage, version and provenance of paper protocol references, and preservation of frozen packets. '
            'The latest operator requirement narrows the reading scope to architecture-first implementation. '
            'Check that exact plan locations are available on demand, cited original ranges are still authenticated, '
            'and prior review feedback about coverage is addressed for this revised scope. A blanket plan-reading gate '
            'would contradict the new requirement. Review the forward-only stalled-author successor, exact native '
            'readback and immutable source and usage retention. Stage cleanup and rollback are outside this scope. '
            if compact else
            'Check complete lossless source reconstruction, typed reference safety, deduplication, task views, '
            'original-version compatibility, actual source/request/receipt ownership, active clarification on '
            'read stagnation, ordinary save/review gates, pause/resume and replay. Check impact on other '
            'consumers, unknown fields, job/seed/metric/phase preservation, successful history and billing. '
            'Review the optional whole implementation restart: exact stage ownership, pre-code admission, '
            'immutable archive, independent adoption, idempotent file cleanup, preserved architecture and billing, '
            'and a fresh implementation generation with the same review limits. ')
        relevant_paths = set(READBACK_SELECTION) | {'autoresearch/experiment_materials.py', 'autoresearch/experiment_materials_review.py',
            'autoresearch/implementation_acceptance_flow.py', 'autoresearch/implementation_documents.py',
            'autoresearch/codex_workflow.py', 'autoresearch/implementation_phase_context.py',
            'tests/test_experiment_materials.py', 'tests/test_implementation_phase_context.py',
            'tests/test_document_efficiency.py', 'autoresearch/document_author_lineage.py',
            'autoresearch/implementation_stage_readback.py',
            'docs/DOCUMENT_EFFICIENCY.md'}
        review_code = []
        for item in code:
            if compact and item['path'] == 'autoresearch/document_efficiency_recovery.py':
                excerpt, ranges = selected_source(item['source'], {
                    'selected_source', 'sources', '_checks', 'validate_transport_evidence', 'enabled', 'enqueue', 'review_paused',
                    'adopt', 'validate_approvals', 'materials_only', 'ask', '_audit', '_recovery_edge', 'current_author'})
                review_code.append({**item, 'source': excerpt, 'selection_version': 1, 'source_ranges': ranges})
            elif compact and item['path'] not in relevant_paths:
                review_code.append({k: v for k, v in item.items() if k != 'source'})
            else:
                review_code.append(item)
        task = {'purpose': KIND + '_review:' + digest(source),
            'instruction': ('Independently review the supplied generic capability source and actual checks. '
                + review_focus +
                'Do not approve merely because tests pass. Reject concrete code defects with locations and checks. '
                'This approves program capability only, never research success or experimental execution.'),
            'context': {'work_order_id': key, 'source': source, 'requirement': work['operator_requirement'],
                'operator_requests': work.get('operator_requests', []),
                'prior_capability_reviews': work.get('prior_capability_reviews', []),
                'controller_cursor': work['controller_cursor'], 'actual_source': review_code, 'isolated_checks': work['checks']},
            'schema': {'type': 'object', 'properties': {'source_sha256': {'const': digest(source)},
                'approve': {'type': 'boolean'}, 'rationale': {'type': 'string'},
                'required_changes': {'type': 'array', 'items': {'type': 'string'}}},
                'required': ['source_sha256', 'approve', 'rationale', 'required_changes'], 'additionalProperties': False}}
        results = await gather(*(h.ask(run_id, role, **task) for role in ('director', 'scout')))
        for role, result in zip(('director', 'scout'), results):
            hid = h.decision_key(run_id, role, **task)
            authenticate(engine, run_id, role, hid, result)
            work['reviews'][role] = {'handoff_id': hid, 'result': result}
        if (digest(await to_thread(sources)) != source['capability_sha256']
                or any(not r['approve'] or r['required_changes'] for r in results)):
            work.update(status='capability_revision_required', updated_at=now())
            engine.store.put('stage_contexts', key, work)
            raise ResearchBlocked('Document capability requires revision: ' + key + '; '
                + '; '.join(x for r in results for x in r['required_changes']))
        work.update(status='accepted', updated_at=now())
        engine.store.put('stage_contexts', key, work)
        engine.store.event(run_id, KIND + '.accepted', '通用资料与阅读收敛能力通过独立评审', {'work_order_id': key})
    validate_approvals(engine, run_id, work, source)
    engine.store.put('harness_capabilities', KIND, {'id': KIND, 'status': 'accepted',
        'work_order_id': key, 'capability_sha256': source['capability_sha256'], 'version': VERSION})
    return work


def validate_approvals(engine, run_id, work, source):
    from .engine import ResearchBlocked
    if work.get('status') != 'accepted' or work.get('run_id') != run_id or work.get('source') != source:
        raise ResearchBlocked('Document capability work order is not accepted for this source')
    if set(work['reviews']) != {'director', 'scout'}:
        raise ResearchBlocked('Document capability is missing independent approvals')
    for role, row in work['reviews'].items():
        context = authenticate(engine, run_id, role, row['handoff_id'], row['result'])
        if (context.get('source') != source or context.get('work_order_id') != work['id']
                or row['result'].get('source_sha256') != digest(source)
                or row['result'].get('approve') is not True or row['result'].get('required_changes')):
            raise ResearchBlocked('Document capability approval belongs to a different source')


def stage_restart_pending(engine, run_id, work):
    """Only an explicit stage restart intake can supersede author-only recovery."""
    requests = [row['id'] for row in work.get('operator_requests', [])
        if row.get('operator_payload', {}).get('scope') == STAGE_RESTART]
    if not requests:
        return False
    marker = engine.store.get('runs', run_id, {}).get('state', {}).get('implementation_restart')
    receipt = engine.store.get('implementation_restarts', marker, {}) if marker else {}
    return any(key not in receipt.get('capability_request_ids', []) for key in requests)


def materials_only(work):
    scopes = {row.get('operator_payload', {}).get('scope') for row in work.get('operator_requests', [])}
    return scopes == {COMPACT_MATERIALS}


def project_context(engine, h, run_id, state, context):
    from .task_document_projection import brief, render, restore
    from .implementation_documents import _save
    result = copy.deepcopy(context)
    for name in ('document_working_notes', 'predecessor_document_notes', 'prior_author_observations'):
        result.pop(name, None)
    briefs, definitions = [], []
    for field, title in (('proposal', '已批准提案任务字段'), ('design', '架构任务字段'), ('experiment_plan', '冻结实验计划任务字段')):
        value = state['common'].get(field)
        if value is None:
            continue
        view, text = brief(value, title)
        assert restore(view) == value
        base = f'cycle-{engine.cycle(run_id)}/task-documents/v1/{view["source_sha256"]}'
        # Original source bytes remain separately auditable, including fields
        # unknown to this projection version. No existing artifact is replaced.
        name = base + '.json'
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        old = engine.store.get('artifacts', run_id + ':' + name)
        if old is None:
            engine.artifact(run_id, name, raw, 'task_document_original')
        elif Path(old['path']).read_text(encoding='utf-8') != raw:
            raise ValueError('Task source archive changed')
        manifest_name = base + '.projection.json'
        serialized = json.dumps(view, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        existing = engine.store.get('artifacts', run_id + ':' + manifest_name)
        if existing is None:
            engine.artifact(run_id, manifest_name, serialized, 'task_document_projection')
        elif Path(existing['path']).read_text(encoding='utf-8') != serialized:
            raise ValueError('Task projection manifest changed')
        definitions.append(_save(engine, h, run_id, base + '-definitions.md', render(view, '任务资料精确定义'), 'task_document_definitions'))
        briefs.append(_save(engine, h, run_id, base + '-' + field + '-brief.md', text,
            'frozen_experiment_plan' if field == 'experiment_plan' else 'task_document_brief'))
    seen = {d['document_id'] for d in result['indexed_documents']}
    for doc in [*briefs, *definitions]:
        if doc['document_id'] not in seen:
            result['indexed_documents'].append(doc)
            seen.add(doc['document_id'])
    result.update(task_document_projection_protocol=1, task_document_briefs=briefs,
        task_document_definitions=list({d['document_id']: d for d in definitions}.values()),
        document_read_window_protocol=3, **{PROTOCOL: VERSION})
    return result


def current_author(engine, run_id, state, revision):
    """Controller-owned successor wins over legacy purpose lookup after cleanup."""
    from .engine import ResearchBlocked, Harness
    key = state['id'] + ':document-author:' + str(revision)
    cursor = engine.store.get('document_author_cursors', key)
    if not cursor:
        return None
    if (cursor.get('run_id') != run_id or cursor['workflow_scope_sha256'] != digest(scope(state))
            or cursor['revision'] != revision):
        raise ResearchBlocked('Document author recovery cursor changed')
    inputs = engine.store.get('handoff_inputs', cursor['handoff_id'], {})
    handoff = engine.store.get('handoffs', cursor['handoff_id'], {})
    task = cursor['assignment']
    if (digest(task) != cursor['assignment_sha256'] or any(inputs.get(k) != task[k] for k in ('instruction', 'context'))
            or Harness.decision_key(run_id, 'engineer', task['purpose'], task['instruction'], task['context'], inputs.get('schema')) != cursor['handoff_id']
            or handoff.get('input_hash') != digest({k: inputs.get(k) for k in ('instruction', 'context', 'schema')})):
        raise ResearchBlocked('Frozen document successor differs from its recovery cursor')
    work = engine.store.get('stage_contexts', cursor['work_order_id'], {})
    audit = engine.store.get('capability_audits', cursor['source_audit_id'], {})
    if (work.get('status') != 'accepted'
            or not materials_only(work) and work.get('source', {}).get('workflow_scope_sha256') != digest(scope(state))
            or audit.get('sha256') != cursor['source_audit_sha256'] or digest(audit.get('packet')) != audit.get('sha256')):
        raise ResearchBlocked('Document author cursor lacks its accepted capability and original archive')
    if materials_only(work):
        validate_approvals(engine, work['run_id'], work, work['source'])
    from .document_author_lineage import trace
    def edge(hid):
        return _recovery_edge(engine, run_id, state, work, hid)
    _, entry = trace(engine, run_id, cursor['source_handoff_id'], recovery_edge=edge)
    if entry['input']['id'] != cursor['handoff_id']:
        raise ResearchBlocked('Author cursor does not match its authenticated lineage entry')
    return entry['input']


def _audit(engine, run_id, state, hid, *, preserve_source=False):
    from .engine import ResearchBlocked
    packet = _packet(engine, run_id, hid)
    from .document_author_lineage import resolve, unsettled
    if resolve(engine, run_id, hid)['handoff']['id'] != hid:
        raise ResearchBlocked('Recovery must target the actual ordinary save successor')
    context = packet['input']['context']
    binding = context.get('implementation_document_authoring', {})
    actual = engine.store.get('codex_workflows', state['id'], {})
    if (packet['handoff'].get('to_role') != 'engineer'
            or not packet['handoff'].get('purpose', '').startswith('implementation_spec:')
            or binding.get('workflow_id') != state['id'] or binding.get('revision') != state['current_revision']
            or scope(actual) != scope(state) or actual.get('current_revision') != state['current_revision']
            or state['status'] != 'specification' or engine.store.get('decisions', hid)
            or any(r.get('authoring_handoff_id') == hid for r in actual.get('specifications', []))):
        raise ResearchBlocked('Reading recovery cursor is not the current unfinished author assignment')
    if context.get('acceptance_contract') is not None and context['acceptance_contract'] != actual.get('acceptance_review', {}).get('contract'):
        raise ResearchBlocked('Author acceptance contract changed before document recovery')
    progress = packet['progress']
    if unsettled(engine, run_id, packet, preserve_source=preserve_source):
        raise ResearchBlocked('Unsettled actual author receipts require ordinary reconciliation')
    from .document_working_notes import checked_tool_directories
    from .engine import Harness
    allowed = {d['document_id']: d for d in checked_tool_directories(Harness(engine, object()), run_id,
        'engineer', hid, progress, context)}
    from .implementation_authoring import scoped_context, COLLECTION, _checked
    scoped = scoped_context(engine, run_id, 'engineer', hid, {**context, 'indexed_documents': list(allowed.values())})
    allowed.update({d['document_id']: d for d in scoped['indexed_documents']})
    draft = engine.store.get(COLLECTION, hid)
    if draft:
        _checked(engine, run_id, draft)
    from .document_recovery_receipts import authenticate_reads
    authenticate_reads(engine, run_id, 'engineer', hid, progress, allowed)
    aid = hid + ':document-efficiency-audit:' + digest(packet)
    saved = engine.store.get('capability_audits', aid)
    audit = {'id': aid, 'run_id': run_id, 'sha256': digest(packet), 'packet': packet}
    if saved is not None and saved != audit:
        raise ResearchBlocked('Reading recovery immutable audit changed')
    if saved is None:
        engine.store.put('capability_audits', aid, audit)
    return audit


def operator_restart(work, source_id):
    requests = work.get('operator_requests', [])
    original = (work.get('controller_cursor', {}).get('assignment') or {}).get('handoff_id')
    return (bool(requests) and source_id == original and digest(requests) == work['source'].get('operator_requests_sha256')
        and all(r.get('operator_payload', {}).get('scope') == 'restart_unfinished_document_authoring' for r in requests))


def _recovery_edge(engine, run_id, state, work, source_id):
    """Authenticate the archived source, reviewed route and completed cleanup."""
    from .engine import Harness, ResearchBlocked
    from .implementation_authoring import submission_schema
    key = source_id + ':document-efficiency:' + digest(work['source'])
    link = engine.store.get('document_recovery_assignments', key)
    if not link:
        return None
    audit = engine.store.get('capability_audits', link['source_audit_id'], {})
    archived = audit.get('packet', {})
    owner = archived.get('input', {}).get('context', {}).get('implementation_document_authoring', {})
    if (link.get('run_id') != run_id or link.get('id') != key
            or link.get('work_order_id') != work['id'] or link.get('workflow_scope_sha256') != digest(scope(state))
            or audit.get('id') != link['source_audit_id'] or audit.get('run_id') != run_id
            or archived.get('handoff', {}).get('id') != source_id
            or owner.get('workflow_id') != state['id'] or owner.get('revision') != state['current_revision']
            or digest(archived) != audit.get('sha256') or engine.store.get('decisions', source_id)):
        raise ResearchBlocked('Reading recovery predecessor or cursor changed on replay')
    preserve_source = materials_only(work)
    if preserve_source and link.get('preserve_source') is not True:
        raise ResearchBlocked('Forward-only author recovery must preserve the original assignment')
    if engine.store.get('handoff_inputs', source_id):
        if _packet(engine, run_id, source_id) != archived:
            raise ResearchBlocked('Reading recovery predecessor or cursor changed on replay')
    else:
        if preserve_source:
            raise ResearchBlocked('Forward-only author recovery lost the original assignment')
        cleanup = engine.store.get('document_author_cleanups', source_id + ':author-cleanup:' + audit['sha256'], {})
        if cleanup.get('status') != 'completed' or engine.store.get('handoff_progress', source_id):
            raise ResearchBlocked('Cleared author has no completed immutable cleanup receipt')
        if _validated_packet(engine, run_id, source_id, archived['input'], archived['progress']) != archived:
            raise ResearchBlocked('Archived author differs from its original handoff receipt')
    if link['route_handoff_id']:
        route_context = authenticate(engine, run_id, 'director', link['route_handoff_id'], link['route'])
    elif operator_restart(work, source_id) and link.get('route_origin') == 'controller_operator_intake':
        route_context = {'source_audit_sha256': audit['sha256']}
    else:
        raise ResearchBlocked('Document recovery route lacks operator or actual model authority')
    task = link['assignment']
    context = task['context']
    binding = context['document_efficiency_recovery']
    if (route_context.get('source_audit_sha256') != audit['sha256']
            or digest(task) != link['assignment_sha256'] or binding.get('route') != link['route']
            or binding.get('source_handoff_id') != source_id or binding.get('source_audit_id') != audit['id']
            or binding.get('source_audit_sha256') != audit['sha256'] or binding.get('work_order_id') != work['id']):
        raise ResearchBlocked('Reading recovery route or frozen successor changed')
    child_id = Harness.decision_key(run_id, 'engineer', task['purpose'], task['instruction'],
        context, submission_schema(context))
    return {'packet': archived, 'audit': audit, 'successor_id': child_id, 'preserve_source': preserve_source}


def commit_successor(engine, run_id, state, link_key, link, cursor):
    """One durable transition; a replay must finish cleanup before dispatch."""
    from .engine import ResearchBlocked
    with engine.store._db() as db:
        db.execute('BEGIN IMMEDIATE')
        engine.check_active(run_id)
        engine.store._guard_run(db, run_id)
        previous = db.execute("SELECT data FROM records WHERE collection='document_recovery_assignments' AND key=?", (link_key,)).fetchone()
        if previous and json.loads(previous[0]) != link:
            raise ResearchBlocked('An immutable author recovery edge already has a different successor')
        for collection, key, row in (('document_recovery_assignments', link_key, link),
                                      ('document_author_cursors', cursor['id'], cursor)):
            db.execute('INSERT OR REPLACE INTO records(collection,key,data,updated_at) VALUES(?,?,?,?)',
                (collection, key, json.dumps(row, ensure_ascii=False), now()))


async def ask(engine, h, run_id, state, purpose, instruction, context, delegate, *, fresh_packet=None):
    from .engine import Harness, ResearchBlocked
    from .implementation_authoring import submission_schema
    from .implementation_document_readability import new_assignment
    work = await adopt(engine, h, run_id, state)
    preserve_source = materials_only(work)
    instruction, context = new_assignment(h, run_id, purpose, instruction, context, submission_schema(context))
    source_id = Harness.decision_key(run_id, 'engineer', purpose, instruction, context, submission_schema(context))
    from .document_author_lineage import trace, unsettled
    def edge(hid):
        return _recovery_edge(engine, run_id, state, work, hid)

    for _ in range(12):
        packet = None
        ordinary_task = (purpose, instruction, context)
        if engine.store.get('handoffs', source_id):
            packet, entry = await to_thread(trace, engine, run_id, source_id, recovery_edge=edge)
            source_id = packet['handoff']['id']
            purpose = packet['handoff']['purpose']
            instruction, context = packet['input']['instruction'], packet['input']['context']
            ordinary_task = (entry['handoff']['purpose'], entry['input']['instruction'], entry['input']['context'])
        link_key = source_id + ':document-efficiency:' + digest(work['source'])
        if packet and engine.store.get('decisions', source_id):
            return await delegate(engine, h, run_id, state, *ordinary_task)
        existing = packet['progress'] if packet else {}
        pending = packet and await to_thread(unsettled, engine, run_id, packet, preserve_source=preserve_source)
        diagnostic = (await to_thread(diagnose, engine, run_id, 'engineer', source_id, existing, context)
                      if preserve_source or not pending else None)
        if pending and preserve_source and diagnostic['stalled']:
            from .implementation_stage_readback import reconcile
            await reconcile(engine, h, run_id, [source_id], verify_empty_terminal=True)
            pending = await to_thread(unsettled, engine, run_id, packet, preserve_source=True)
            if pending:
                raise ResearchBlocked('Stalled author has an unresolved original response; no replacement was dispatched')
        if pending:
            diagnostic = None
        explicit_restart = not pending and operator_restart(work, source_id)
        if diagnostic and (diagnostic['stalled'] or explicit_restart):
            generation = context.get('document_efficiency_recovery', {}).get('generation', 0) + 1
            if generation > 4:
                raise ResearchBlocked('Repeated author recovery requires a new Harness capability repair')
            audit = await to_thread(_audit, engine, run_id, state, source_id, preserve_source=preserve_source)
            parent = audit['packet']['input']
            next_base = fresh_packet['context'] if fresh_packet else parent['context']
            for identity in ('proposal_hash', 'design_hash', 'experiment_plan_hash'):
                if identity in parent['context'] and next_base.get(identity) != parent['context'][identity]:
                    raise ResearchBlocked('Fresh author compiler changed an approved scientific source identity')
            review_context = {'work_order_id': work['id'], 'source_handoff_id': source_id,
                'source_audit_id': audit['id'], 'source_audit_sha256': audit['sha256'],
                'diagnostic': diagnostic, 'indexed_documents': next_base['indexed_documents'],
                'stalled_assignment_documents': parent['context']['indexed_documents'],
                'document_index_protocol': 1, 'indexed_document_context': True, 'source_tool_protocol': 1,
                'author_task': parent['instruction'], 'author_submission_schema': parent['schema'],
                'author_obligation': parent['context'].get('implementation_document_authoring'),
                'scientific_approval': False}
            task = {'purpose': KIND + '_clarify:' + digest(review_context),
                'instruction': ('Diagnose the stalled author work from its exact frozen source and read ledger summary. '
                    'Choose a concrete next action: draft using existing evidence, targeted missing-source reading, '
                    'a genuinely missing external resource/operator requirement, or a program capability repair. '
                    'Ambiguity alone requires targeted clarification, not a passive block. Do not alter scientific '
                    'criteria or approve a document. Explain the exact unresolved question and necessary source ranges. '
                    'This stage authors an implementation specification; future measurements and already preserved '
                    'scientific limitations are not prerequisites to writing it. Do not request another full reading '
                    'of the same material.'), 'context': review_context,
                'schema': {'type': 'object', 'properties': {'source_audit_sha256': {'const': audit['sha256']},
                    'action': {'enum': ['draft', 'targeted_read', 'external_blocker', 'repair_capability']},
                    'question': {'type': 'string'}, 'rationale': {'type': 'string'},
                    'targets': {'type': 'array', 'items': {'type': 'object', 'properties': {
                        'document_id': {'type': 'string'}, 'sha256': {'type': 'string'}, 'section_id': {'type': 'string'},
                        'reason': {'type': 'string'}}, 'required': ['document_id', 'sha256', 'section_id', 'reason'], 'additionalProperties': False}},
                    'missing_external_requirement': {'type': ['string', 'null']}},
                    'required': ['source_audit_sha256', 'action', 'question', 'rationale', 'targets', 'missing_external_requirement'],
                    'additionalProperties': False}}
            target_choices = []
            for doc in next_base['indexed_documents']:
                target_choices.append({'type': 'object', 'properties': {
                    'document_id': {'const': doc['document_id']}, 'sha256': {'const': doc['sha256']},
                    'section_id': {'enum': [s['id'] for s in doc['sections']]},
                    'reason': {'type': 'string', 'minLength': 1}},
                    'required': ['document_id', 'sha256', 'section_id', 'reason'], 'additionalProperties': False})
            if target_choices:
                task['schema']['properties']['targets']['items'] = {'oneOf': target_choices}
            task['schema']['allOf'] = [
                {'if': {'properties': {'action': {'const': 'targeted_read'}}},
                 'then': {'properties': {'targets': {'minItems': 1}}}},
                {'if': {'properties': {'action': {'const': 'external_blocker'}}},
                 'then': {'properties': {'missing_external_requirement': {'type': 'string', 'minLength': 1}}}}]
            if explicit_restart:
                # This is the operator's bounded controller action, never an
                # invented model reply or independent scientific approval.
                route = {'source_audit_sha256': audit['sha256'], 'action': 'draft',
                    'question': 'Write a new concise complete specification after approved experiment preparation.',
                    'rationale': 'Explicit operator restart scope; preserve successful preparation and original evidence.',
                    'targets': [], 'missing_external_requirement': None}
                route_id, route_origin = None, 'controller_operator_intake'
            else:
                route = await h.ask(run_id, 'director', **task)
                route_id = h.decision_key(run_id, 'director', **task)
                authenticate(engine, run_id, 'director', route_id, route)
                route_origin = 'actual_director_clarification'
            allowed = {d['document_id']: d for d in next_base['indexed_documents']}
            for target in route['targets']:
                doc = allowed.get(target['document_id'])
                if not doc or doc['sha256'] != target['sha256'] or target['section_id'] not in {s['id'] for s in doc['sections']}:
                    raise ResearchBlocked('Convergence clarification selected an unauthenticated source')
            if route['action'] == 'external_blocker' and not route['missing_external_requirement']:
                raise ResearchBlocked('Convergence clarification lacks a concrete external requirement')
            if route['action'] in {'external_blocker', 'repair_capability'}:
                engine.store.event(run_id, KIND + '.repair_required', route['rationale'],
                    {'work_order_id': work['id'], 'source_handoff_id': source_id, 'route_handoff_id': route_id, 'route': route})
                raise ResearchBlocked('Harness document recovery requires ' + route['action'] + ': ' + route['rationale'])
            if route['action'] == 'targeted_read' and not route['targets']:
                raise ResearchBlocked('Targeted convergence clarification omitted the required source ranges')
            next_context = ({**next_base, PROTOCOL: VERSION} if preserve_source else
                            await to_thread(project_context, engine, h, run_id, state, next_base))
            next_context['document_efficiency_recovery'] = {'work_order_id': work['id'], 'source_handoff_id': source_id,
                'source_audit_id': audit['id'], 'source_audit_sha256': audit['sha256'],
                'route_handoff_id': route_id, 'route_origin': route_origin, 'route': route,
                'generation': generation, 'scientific_approval': False}
            purpose = parent['purpose'] if 'purpose' in parent else audit['packet']['handoff']['purpose']
            purpose += ':document-efficiency:' + digest(next_context)
            base_instruction = fresh_packet['instruction'] if fresh_packet else parent['instruction']
            instruction, context = base_instruction + (FOCUSED_DRAFT_GUIDANCE if preserve_source else GUIDANCE), next_context
            instruction, context = new_assignment(h, run_id, purpose, instruction, context, submission_schema(context))
            task = {'purpose': purpose, 'instruction': instruction, 'context': context}
            child = h._handoff(run_id, 'engineer', purpose, instruction, context, submission_schema(context))
            link = {'id': link_key, 'run_id': run_id,
                'work_order_id': work['id'], 'workflow_scope_sha256': digest(scope(state)),
                'source_audit_id': audit['id'], 'route_handoff_id': route_id, 'route_origin': route_origin, 'route': route,
                'assignment': task, 'assignment_sha256': digest(task), 'created_at': now()}
            if preserve_source:
                link['preserve_source'] = True
            cursor_key = state['id'] + ':document-author:' + str(state['current_revision'])
            cursor = {'id': cursor_key, 'run_id': run_id,
                'workflow_scope_sha256': digest(scope(state)), 'revision': state['current_revision'],
                'handoff_id': child['id'], 'assignment': task, 'assignment_sha256': digest(task),
                'source_handoff_id': source_id, 'source_audit_id': audit['id'], 'source_audit_sha256': audit['sha256'],
                'work_order_id': work['id'], 'created_at': now()}
            await to_thread(commit_successor, engine, run_id, state, link_key, link, cursor)
            if not preserve_source:
                from .document_author_cleanup import clear
                await to_thread(clear, engine, run_id, source_id, audit, child['id'])
            engine.store.event(run_id, KIND + '.replacement_prepared', 'Harness 已按独立诊断准备新的作者请求，原交接保持归档',
                {'work_order_id': work['id'], 'source_handoff_id': source_id, 'source_audit_id': audit['id'], 'purpose': purpose})
            ordinary_task = (purpose, instruction, context)
        elif packet is None:
            context = ({**context, PROTOCOL: VERSION} if preserve_source else
                       await to_thread(project_context, engine, h, run_id, state, context))
            instruction += FOCUSED_DRAFT_GUIDANCE if preserve_source else GUIDANCE
            ordinary_task = (purpose, instruction, context)
        try:
            return await delegate(engine, h, run_id, state, *ordinary_task)
        except ReadingStalled as exc:
            source_id = exc.handoff_id
    raise ResearchBlocked('Repeated author recovery made no delivery progress; Harness capability repair required')
