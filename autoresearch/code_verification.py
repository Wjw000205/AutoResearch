"""Host-owned bounded checks of frozen deliveries; never a training retry.

Policies are explicit, source-bound operator admissions. A terminal receipt is
replayed for the same source and policy, including failures. Reports produced by
the child are evidence, not authority to approve code or scientific outcomes.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import copy
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import time
from .disk_usage import tree_bytes

from .storage import now
from .code_repair_contract import sha

POLICIES = 'code_verification_policies'
RECEIPTS = 'code_verification_receipts'
VERSION = 1


def _sampling_recovery(receipt, *, artifact_reports=()):
    """Recognize the legacy host stat race, never a child's failed assertion.

    No successful, timed-out, or unconfirmed execution is retried. The new
    identity binds this exact failed receipt and retains its quota charge.
    """
    if (receipt.get('status') != 'failed' or receipt.get('exit_code') is not None
            or not receipt.get('error','').startswith('FileNotFoundError:')):
        return None
    reports=[*receipt.get('test_reports',{}).values(),*artifact_reports]
    if any(isinstance(report,dict) and (report.get('passed') is False
            or report.get('status')=='failed'
            or any(isinstance(test,dict) and (test.get('status')=='failed' or test.get('passed') is False)
                   for test in report.get('tests',[]))) for report in reports):
        return None  # A failed assertion already requires a source/review response.
    import ast
    try:
        missing = Path(ast.literal_eval(receipt['error'].rsplit(': ',1)[1]))
        relative = missing.relative_to(Path(tempfile.gettempdir()))
    except (ValueError,SyntaxError,TypeError):
        return None
    if (len(relative.parts) < 2 or not relative.parts[0].startswith('autoresearch-code-check-')
            or Path(*relative.parts[1:]).as_posix() in receipt['identity']['source_sha256']):
        return None
    return {'kind':'disappearing_temporary_file_sample','version':1,
            'receipt_id':receipt['id'],'receipt_sha256':receipt['sha256']}


def _recovery_artifact_reports(receipt):
    """Inspect already-sealed partial JSON, including files copied in finally."""
    for item in receipt.get('artifacts',[]):
        path=Path(item['path'])
        if path.suffix.lower() != '.json':
            continue
        try:
            yield json.loads(path.read_bytes())
        except (ValueError,UnicodeError):
            yield {'passed':False}  # An unreadable report cannot grant a retry.


def deadline_variable(path, configured=None):
    """Read the deadline interface from the frozen entry, not the research domain."""
    import ast
    import re
    candidates = {node.value for node in ast.walk(ast.parse(Path(path).read_bytes()))
                  if isinstance(node, ast.Constant) and isinstance(node.value, str)
                  and re.fullmatch(r'[A-Z][A-Z0-9_]{0,64}_CHECK_DEADLINE', node.value)}
    if configured is not None:
        if (not isinstance(configured, str)
                or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,64}_CHECK_DEADLINE', configured)
                or candidates and configured not in candidates):
            raise ValueError('Deadline environment variable differs from the frozen entry')
        return configured
    if len(candidates) > 1:
        raise ValueError('Frozen entry has ambiguous deadline interfaces; declare the task interface')
    return next(iter(candidates), 'AUTORESEARCH_CHECK_DEADLINE')


def checked_path(root, name):
    p = PurePosixPath(name)
    if (not name or p.is_absolute() or p.as_posix() != name or '..' in p.parts
            or '\\' in name or ':' in name):
        raise ValueError('Invalid verification path')
    path = Path(root) / name
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part, 'is_junction', lambda: False)():
            raise ValueError('Verification paths cannot contain links')
        if part == Path(root):
            break
    return path


def policy(engine, run_id, *, workflow_id=None):
    value = (engine.store.get(POLICIES, workflow_id) if workflow_id else None) or engine.store.get(POLICIES, run_id)
    if value and (value.get('version') != VERSION or value.get('run_id') != run_id
            or value.get('sha256') != sha({k: v for k, v in value.items() if k != 'sha256'})):
        raise ValueError('Verification policy identity changed')
    # A run can contain several independently approved implementation workflows.
    # The original operator admission belongs to one of them, not every later
    # architecture/dataset delivery that happens to share the run ID.
    if value and workflow_id is not None and value['workflow_id'] != workflow_id:
        return None
    return value


def prepare_routed(engine, run_id, state, spec, attempt, source, task, plan):
    """Admit the original CPU checks after an independent entry read.

    The project defaults authorize CPU development checks, not research fits.
    A new workflow gets its own bounded policy; legacy admissions and consumed
    allowances are preserved. Resource/budget/cancellation gates still run.
    """
    from .codex_workflow import _verify_snapshot
    from .implementation_alignment_evidence import _code_text, _own_code_spans
    from .code_evidence_queue import COLLECTION, PLAN_SCHEMA, PLAN_SCHEMA_V2
    from .engine import Harness
    saved = engine.store.get(COLLECTION, task['id'])
    hand = engine.store.get('handoffs',task['handoff_id'],{})
    frozen = engine.store.get('handoff_inputs',task['handoff_id'],{})
    decision = engine.store.get('decisions',task['handoff_id'],{})
    payload={k:frozen.get(k) for k in ('instruction','context','schema')}
    invocation_version=frozen.get('context',{}).get('implementation_evidence_task',{}).get('invocation_contract_version')
    expected_schema=PLAN_SCHEMA_V2 if invocation_version==2 else PLAN_SCHEMA
    if (not saved or saved.get('id')!=task['id'] or saved.get('run_id')!=run_id
            or saved.get('binding')!=task['binding'] or hand.get('run_id')!=run_id
            or hand.get('to_role')!='engineer' or hand.get('status')!='completed'
            or hand.get('input_hash')!=sha(payload) or hand.get('output_hash')!=sha(plan)
            or decision.get('id')!=task['handoff_id'] or decision.get('run_id')!=run_id
            or decision.get('role')!='engineer' or decision.get('purpose')!=hand.get('purpose')
            or decision.get('result')!=plan or frozen.get('schema')!=expected_schema
            or frozen.get('context',{}).get('implementation_evidence_task',{}).get('id')!=task['id']
            or Harness.decision_key(run_id,'engineer',hand.get('purpose'),**payload)!=task['handoff_id']
            or task['binding']['workflow_id']!=state['id'] or state.get('run_id')!=run_id
            or source['delivery_hash']!=attempt['delivery_hash'] or source['spec_hash']!=spec['spec_hash']
            or not spec.get('approved') or plan.get('ready') is not True):
        raise ValueError('CPU admission requires the exact current independently reviewed task')
    declared={r['id']:r for r in spec['spec']['acceptance_checks']}
    if not plan['requirement_ids'] or set(plan['requirement_ids'])-declared.keys():
        raise ValueError('CPU plan references an unapproved requirement')
    import re
    timeout=plan['timeout_seconds'];reference=plan['allowance_reference'];quote=reference['quote'].strip()
    check=declared.get(reference['requirement_id'],{})
    approved_text='\n'.join(str(check.get(k,'')) for k in ('requirement','verification'))
    number=format(float(timeout),'g') if type(timeout) in (int,float) and math.isfinite(timeout) else ''
    if (not number or timeout<=0 or not quote or quote not in approved_text
            or not re.search(r'(?<![\d.])'+re.escape(number)+r'(?:\.0)?\s*[- ]?\s*(?:秒|seconds?\b|s\b)',quote,re.I)
            or not re.search(r'截止|预算|上限|限时|限额|deadline|timeout|budget|limit',quote,re.I)):
        raise ValueError('CPU allowance must quote the exact current approved check budget')
    entry=plan['entrypoint']
    checked_path(Path(attempt['snapshot']['workspace']),entry)
    if not entry.startswith('tests/') or not entry.endswith('.py'):
        raise ValueError('Only an existing frozen CPU test entry can be admitted')
    manifest=json.loads(checked_path(Path(attempt['snapshot']['workspace']),'implementation.json').read_bytes())
    report_paths=plan['report_paths']
    if invocation_version==2:
        from .code_check_invocation import validate as validate_invocation
        validate_invocation(plan,manifest)
    elif (not report_paths or set(report_paths)-set(manifest.get('test_result_files',[]))
            or any(not p.endswith('.json') for p in report_paths)):
        raise ValueError('Select the exact declared JSON reports produced by this CPU entry')
    artifacts=[f for f in attempt.get('delivery_artifacts',[]) if f.get('path')==entry]
    if len(artifacts)!=1:
        raise ValueError('CPU test entry is absent or ambiguous in the actual delivery')
    artifact=artifacts[0]
    artifact_id=artifact.get('artifact_id') or artifact.get('id')
    text=_code_text(engine,run_id,artifact_id,artifact)
    spans=_own_code_spans(engine,task['handoff_id'],artifact_id,artifact['sha256'],text)
    invocation_recovery=task.get('invocation_recovery')
    if invocation_recovery:
        from .implementation_evidence_recovery import sealed
        prior,old_plan=sealed(engine,run_id,'engineer',invocation_recovery['handoff_id'])
        if (frozen['context']['implementation_evidence_task'].get('invocation_recovery')!=invocation_recovery
                or sha(old_plan)!=invocation_recovery['plan_sha256']
                or prior['context']['implementation_evidence_task']['binding']!=task['binding']
                or old_plan.get('entrypoint')!=entry):
            raise ValueError('Retained invocation reading belongs to another frozen plan')
        spans+=_own_code_spans(engine,invocation_recovery['handoff_id'],artifact_id,artifact['sha256'],text)
        spans.sort()
    inherited=task.get('read_binding_recovery')
    if inherited:
        prior_id=inherited['handoff_id'];prior=engine.store.get('handoff_inputs',prior_id,{})
        prior_hand=engine.store.get('handoffs',prior_id,{})
        prior_vote=engine.store.get('decisions',prior_id,{})
        if (frozen.get('context',{}).get('implementation_evidence_task',{}).get('read_binding_recovery')!=inherited
                or prior_hand.get('run_id')!=run_id or prior_hand.get('to_role')!='engineer'
                or prior_hand.get('status')!='completed' or prior_vote.get('run_id')!=run_id
                or prior_vote.get('role')!='engineer' or prior_hand.get('output_hash')!=sha(prior_vote.get('result'))
                or inherited.get('decision_sha256')!=sha(prior_vote.get('result'))
                or prior_hand.get('input_hash')!=sha({k:prior.get(k) for k in ('instruction','context','schema')})
                or prior.get('context',{}).get('implementation_evidence_task',{}).get('binding')!=task['binding']
                or inherited.get('entrypoint')!=entry):
            raise ValueError('Inherited CPU entry reading requires the exact same-role frozen acquisition')
        spans+=_own_code_spans(engine,prior_id,artifact_id,artifact['sha256'],text)
        spans.sort()
    cursor=0
    for begin,end in spans:
        if begin<=cursor: cursor=max(cursor,end)
    if cursor<len(text):
        raise ValueError('Independent CPU plan has not read the selected frozen test entry')
    _verify_snapshot(engine,run_id,attempt['snapshot'])
    prior=policy(engine,run_id,workflow_id=state['id'])
    if invocation_version==2:
        from .code_check_invocation import admit
        return admit(engine,run_id,state,spec,attempt,source,task,plan,prior)
    if prior:
        if (prior['entrypoint']!=entry or prior['implementation_document_sha256']!=spec['document']['sha256']
                or prior.get('report_paths',report_paths)!=report_paths):
            raise ValueError('Existing CPU allowance cannot be replaced or reset')
        return prior
    value={'version':VERSION,'run_id':run_id,'workflow_id':state['id'],
        'base_snapshot_sha256':attempt['snapshot']['files_hash'],
        'implementation_document_sha256':spec['document']['sha256'],'source_sha256':sha(source),
        'entrypoint':entry,'requirement_ids':plan['requirement_ids'],
        'report_paths':report_paths,'report_contract':'development_report_v1',
        'issue_routes':{i['issue_id']:i['binding']['disposition'] for i in source['issues']},
        'timeout_seconds':float(timeout),'timeout_reference':copy.deepcopy(reference),
        'inherited_executions':[], 'code_acceptance_mode':'bounded_checks',
        'training_during_implementation':False,'routed_task_id':task['id'],
        'routed_handoff_id':task['handoff_id'],'authorization':'project_cpu_checks_default_and_user_routing_repair',
        'deadline_environment_variable':deadline_variable(checked_path(Path(attempt['snapshot']['workspace']),entry)),
        'created_at':now()}
    value['sha256']=sha(value)
    # A foreign legacy policy is kept under run_id, byte for byte.
    engine.store.put(POLICIES,state['id'],value)
    engine.store.event(run_id,'implementation.verification_prepared','当前任务组的 CPU 有界验收已准入，未授权科研训练。',
        {'workflow_id':state['id'],'task_id':task['id'],'policy_sha256':value['sha256']})
    return value


def prepare(engine, run_id, *, workflow_id, snapshot_sha256, document_sha256,
            entrypoint, requirement_ids, issue_routes, timeout_seconds=60, deadline_environment_variable=None):
    """Explicit control route; does not execute or modify a saved review."""
    from .codex_workflow import _verify_snapshot
    from . import code_repair_contract as repair, code_repair_clarification as clarification
    run = engine.store.get('runs', run_id)
    if run['status'] not in {'blocked', 'paused'} or run['stage'] != 'implementation':
        raise ValueError('Prepare verification only for a stopped implementation')
    state = engine.store.get('codex_workflows', workflow_id)
    if not state or state['run_id'] != run_id:
        raise ValueError('Foreign implementation workflow')
    attempt = max((a for a in state['attempts'] if a['status'] != 'superseded'), key=lambda a: a['index'])
    spec = next((s for s in reversed(state['specifications']) if s.get('approved')), None)
    if (not spec or spec['document']['sha256'] != document_sha256
            or attempt.get('snapshot', {}).get('files_hash') != snapshot_sha256):
        raise ValueError('Verification admission must bind the current approved document and delivery')
    _verify_snapshot(engine, run_id, attempt['snapshot'])
    source = repair.build_source(engine, run_id, state, spec, attempt)
    if attempt.get('repair_clarification', {}).get('status') == 'completed':
        source = clarification.verify(engine, run_id, state, spec, attempt, source, attempt['repair_clarification'])
    packet = repair.materialize(engine, run_id, source, clarification.mapping(source),
                                allow_deferred=True, allow_empty=True)
    issues = {i['issue_id']: i for i in [*packet['items'], *packet.get('deferred_items', [])]}
    if (not isinstance(issue_routes, dict) or set(issue_routes) != set(issues) or any(not isinstance(v, str) or v not in {'repair_code', 'needs_execution', 'needs_definition', 'needs_review'}
                                               for v in issue_routes.values())):
        raise ValueError('Route every original issue exactly once')
    if any(issue_routes[k] == 'repair_code' and v['disposition'] != 'repair_code' for k, v in issues.items()):
        raise ValueError('Routing cannot grant new code-edit authority')
    declared = {r['id'] for r in spec['spec']['acceptance_checks']}
    if (not isinstance(requirement_ids, list) or not requirement_ids
            or any(not isinstance(v, str) for v in requirement_ids)
            or len(set(requirement_ids)) != len(requirement_ids) or set(requirement_ids) - declared):
        raise ValueError('Verification requires exact approved requirement IDs')
    files = {f['path'] for f in attempt['snapshot']['files']}
    checked_path(Path(attempt['snapshot']['workspace']), entrypoint)
    if entrypoint not in files or not entrypoint.startswith('tests/') or not entrypoint.endswith('.py'):
        raise ValueError('Only an existing frozen Python test entrypoint is executable')
    deadline_name = deadline_variable(checked_path(Path(attempt['snapshot']['workspace']), entrypoint),
                                      deadline_environment_variable)
    # The selected entry must have been independently read for a routed issue.
    evidence_paths = {e['path'] for i in issues.values() if issue_routes[i['issue_id']] == 'needs_execution'
                      for e in i['host_evidence']}
    if entrypoint not in evidence_paths:
        raise ValueError('Test entrypoint lacks original independent-review evidence')
    if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 60:
        raise ValueError('Bounded verification must fit the existing at-most-60-second allowance')
    # Conservatively charge the entire failed host invocation containing the
    # existing resource probe, rather than grant a second 60-second allowance.
    inherited = []
    marker = attempt.get('execution_failure_review', {})
    check_key = marker.get('diagnostic', {}).get('source_check')
    if check_key:
        row = engine.store.get('codex_checks', check_key)
        if not row or row.get('run_id') != run_id or row.get('status') != 'failed':
            raise ValueError('Original resource-probe receipt is unavailable')
        seconds = row.get('duration_seconds')
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
            raise ValueError('Original execution has no measurable elapsed allowance')
        inherited.append({'storage_key': check_key, 'sha256': sha(row), 'seconds': seconds})
    value = {'version': VERSION, 'run_id': run_id, 'workflow_id': workflow_id,
        'base_snapshot_sha256': snapshot_sha256, 'implementation_document_sha256': document_sha256,
        'source_sha256': sha(source), 'entrypoint': entrypoint, 'requirement_ids': requirement_ids,
        'issue_routes': issue_routes, 'timeout_seconds': float(timeout_seconds),
        'inherited_executions': inherited, 'code_acceptance_mode': 'bounded_checks',
        'training_during_implementation': False, 'created_at': now()}
    value['deadline_environment_variable'] = deadline_name
    previous = policy(engine, run_id)
    if previous:
        if {k:v for k,v in previous.items() if k not in {'created_at','sha256'}} != {k:v for k,v in value.items() if k != 'created_at'}:
            raise ValueError('An admitted verification policy cannot be silently replaced or reset')
        return previous
    value['sha256'] = sha(value)
    engine.store.put(POLICIES, run_id, value)
    engine.store.event(run_id, 'implementation.verification_prepared', '已安排宿主有界测试与分类处理；未启动训练。',
                       {'workflow_id': workflow_id, 'policy_sha256': value['sha256']})
    return value


def _inventory(directory, manifest):
    reports = set(manifest.get('test_result_files', []))
    files = manifest.get('delivery_files', [])
    if (not files or len(files) != len(set(files)) or not reports <= set(files)
            or reports & set(manifest.get('runtime_files', []))):
        raise ValueError('Bounded checks require a complete delivery manifest')
    hashes = {}
    for name in files:
        path = checked_path(directory, name)
        if name in reports:
            if ((not name.startswith('test-results/') and name != 'repair-results.json')
                    or path.suffix.lower() in {'.py', '.pyc', '.pyd', '.dll', '.exe', '.bat', '.cmd', '.ps1'}):
                raise ValueError('Reports must be separate from source')
        else:
            hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _sealed_receipt(engine, receipt, expected):
    if receipt.get('identity') != expected or receipt.get('sha256') != sha({k:v for k,v in receipt.items() if k != 'sha256'}):
        raise ValueError('Host test receipt binding changed')
    for item in receipt.get('artifacts', []):
        p = Path(item['path'])
        root = engine.run_dir(receipt['run_id']).resolve()
        if not p.resolve().is_relative_to(root) or checked_path(root, p.relative_to(root).as_posix()) != p:
            raise ValueError('Host test artifact escaped the run')
        with p.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != item['sha256']:
            raise ValueError('Host test artifact changed')
    return copy.deepcopy(receipt)


_BOOTSTRAP = '''import json,os,runpy,sys,time
from pathlib import Path
c=json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
while not Path(c['gate']).exists():
    if time.monotonic()>c['startup_deadline']: raise RuntimeError('Host did not admit test execution')
    time.sleep(.02)
if os.name!='nt':
    import resource
    resource.setrlimit(resource.RLIMIT_AS,(c['memory_mb']*1024*1024,)*2)
    if c['cpu_seconds'] is not None:
        resource.setrlimit(resource.RLIMIT_CPU,(c['cpu_seconds'],c['cpu_seconds']+1))
    resource.setrlimit(resource.RLIMIT_FSIZE,(c['disk_mb']*1024*1024,)*2)
if c['deadline'] is not None:
    os.environ[c['deadline_environment_variable']]=str(c['deadline'])
else:
    os.environ.pop(c['deadline_environment_variable'],None)
sys.argv=[c['entrypoint'],*c.get('arguments',[])]
runpy.run_path(c['entrypoint'],run_name='__main__')
'''


def _development_report_passed(report, hashes, manifest, *, input_scope=None):
    if not isinstance(report,dict):
        return False
    if report.get('source_sha256')==hashes and report.get('passed') is True:
        return True  # Existing host report contract.
    required=set(manifest.get('runtime_files',[])) | {p for p in hashes if p.startswith('tests/') and p.endswith('.py')}
    expected={p:hashes[p] for p in required if p in hashes}
    tests=report.get('tests')
    return (bool(required) and set(expected)==required and report.get('source_sha256')==expected
        and report.get('status')=='passed' and isinstance(tests,list) and bool(tests)
        and all(isinstance(t,dict) and isinstance(t.get('id'),str) and t['id'] and t.get('status')=='passed' for t in tests)
        and len({t['id'] for t in tests})==len(tests) and report.get('scientific_approval') is False
        and (report.get('frozen_environment_matches',True) is True
             or input_scope=='frozen_delivery' and report.get('frozen_environment_matches') is None))


def _cleanup_diagnostic(result, output, phase, exc):
    """Cleanup cannot erase a terminal execution result or authorize a pass."""
    import traceback
    result.setdefault('execution_outcome', {key: result.get(key) for key in
        ('status', 'passed', 'exit_code', 'error', 'error_phase', 'pre_cleanup_exit_code')})
    diagnostic = {'phase': phase, 'type': type(exc).__name__, 'message': str(exc)[:1000],
                  'traceback': traceback.format_exc(limit=12)[-12000:]}
    result.setdefault('cleanup_diagnostics', []).append(diagnostic)
    result['passed'] = False
    if result['status'] == 'completed':
        result['status'] = 'failed'
    result.setdefault('error', 'host_cleanup_failed')
    result.setdefault('error_phase', phase)
    try:
        (output / 'cleanup-error.json').write_text(
            json.dumps(result['cleanup_diagnostics'], ensure_ascii=False), encoding='utf-8')
    except OSError as write_error:
        diagnostic['artifact_error'] = type(write_error).__name__ + ': ' + str(write_error)[:1000]


def _remove_owned_workspace(path, identity, parent):
    """Only remove the exact OS-created directory, never a replaced root/link."""
    import stat
    current = path.lstat()
    if (path.parent != parent or path.parent.resolve() != parent or
            not path.name.startswith('autoresearch-code-check-') or
            (current.st_dev, current.st_ino) != identity or not stat.S_ISDIR(current.st_mode) or
            path.is_symlink() or getattr(path, 'is_junction', lambda: False)()):
        raise ValueError('Owned check workspace identity changed; cleanup refused')
    # Python 3.12 rmtree removes child links/junctions without following them.
    shutil.rmtree(path)


@contextmanager
def _check_workspace(result, output):
    temporary = tempfile.TemporaryDirectory(prefix='autoresearch-code-check-')
    work = Path(temporary.name).absolute()
    info = work.lstat()
    identity, parent = (info.st_dev, info.st_ino), work.parent.resolve()
    # We own cleanup and retain failures; a later weakref finalizer must not
    # delete a replaced path or raise over the check's terminal evidence.
    temporary._finalizer.detach()
    try:
        yield work
    except Exception as exc:
        import traceback
        result.update(passed=False, error=type(exc).__name__ + ': ' + str(exc)[:1000],
                      error_phase='prepare_or_collect', error_traceback=traceback.format_exc(limit=12))
        (output / 'host-error.json').write_text(json.dumps(result), encoding='utf-8')
    finally:
        try:
            if result.get('process_cleanup_confirmed') is not True:
                raise RuntimeError('Workspace retained because process cleanup is unconfirmed')
            _remove_owned_workspace(work, identity, parent)
        except Exception as exc:
            result['retained_workspace'] = {'path': str(work), 'device': identity[0], 'inode': identity[1]}
            _cleanup_diagnostic(result, output, 'workspace_cleanup', exc)


def _reap_check(process, job, children):
    import psutil
    if job:
        job.close_and_wait()
    elif process:
        if os.name != 'nt':
            import signal
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for child in children:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
    if process:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
    _, alive = psutil.wait_procs(list(children), timeout=5)
    if alive:
        raise RuntimeError('Owned check descendants have not exited')


def _execute(directory, manifest, hashes, entrypoint, limits, seconds, output, cancelled):
    from .lab import _WindowsJob, ExperimentRunner
    import psutil
    # An escaped POSIX session can outlive both its parent and the sampling
    # window. Only Windows Job ownership currently guarantees that an
    # unbounded check cannot leave such descendants behind. Reject before
    # creating a workspace or launching even the authorization bootstrap.
    if os.name != 'nt' and (seconds is None or limits['cpu_seconds'] is None):
        raise ValueError('No-duration checks require Windows Job process ownership')
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    result = {'status': 'failed', 'passed': False, 'command': [limits['python_executable'], '-I', entrypoint],
              'test_reports': {}, 'artifacts': [], 'exit_code': None,
              'process_cleanup_confirmed': True}
    for value in (seconds, limits['cpu_seconds']):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                  or not math.isfinite(value) or value <= 0):
            raise ValueError('Check duration must be positive and finite, or None')
    with _check_workspace(result, output) as work:
        for name in hashes:
            target = checked_path(work, name); target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(checked_path(directory, name), target)
        bootstrap = work / '.host-check.py'; bootstrap.write_text(_BOOTSTRAP, encoding='utf-8')
        configuration = {**limits, 'entrypoint': entrypoint, 'gate': str(work/'.host-start'),
                         'startup_deadline': time.monotonic()+10,
                         'deadline': None if seconds is None else started+seconds}
        configuration['deadline_environment_variable'] = deadline_variable(
            checked_path(directory, entrypoint), limits.get('deadline_environment_variable'))
        config = work / '.host-check.json'; config.write_text(json.dumps(configuration), encoding='utf-8')
        env = ExperimentRunner._environment(work, limits)
        env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1')
        if configuration['deadline'] is not None:
            env[configuration['deadline_environment_variable']] = str(configuration['deadline'])
        else:
            env.pop(configuration['deadline_environment_variable'], None)
        process = job = None
        reaped = False
        children = set()
        phase='prepare_inputs'
        try:
            if limits.get('invocation'):
                from .code_check_invocation import stage
                configuration['arguments'],result['input_staging']=stage(directory,work,manifest,limits)
                result['command']+=copy.deepcopy(limits['invocation']['arguments'])
                config.write_text(json.dumps(configuration),encoding='utf-8')
            phase='launch'
            with (output/'stdout.txt').open('wb') as out, (output/'stderr.txt').open('wb') as err:
                result['process_cleanup_confirmed'] = False
                process = subprocess.Popen([limits['python_executable'], '-I', str(bootstrap), str(config)],
                    cwd=work, env=env, stdout=out, stderr=err,
                    **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {'start_new_session': True}))
                if os.name == 'nt':
                    job = _WindowsJob(process.pid, limits['memory_mb'], limits['cpu_seconds'], limits['cpu_cores'])
                else:
                    _finite_cpu_affinity(psutil.Process(process.pid), limits['cpu_cores'])
                Path(configuration['gate']).touch()
                phase='monitor'
                while process.poll() is None:
                    if cancelled(): result['status'] = 'interrupted'; break
                    if configuration['deadline'] is not None and time.monotonic() >= configuration['deadline']:
                        result['status'] = 'timeout'; break
                    try:
                        children.update(psutil.Process(process.pid).children(recursive=True))
                    except psutil.NoSuchProcess:
                        pass
                    size = tree_bytes(work,output)
                    if size > limits['disk_mb']*1024*1024:
                        result.update(error='disk_limit', error_phase=phase)
                        break
                    time.sleep(.05)
                # Diagnostic only: None means still running at the first reap.
                # Never replace the actual final exit code or acceptance checks.
                result['pre_cleanup_exit_code'] = process.poll()
                phase='process_reaping'
                try:
                    _reap_check(process, job, children)
                    reaped = True
                except Exception as exc:
                    result['exit_code'] = process.poll()
                    _cleanup_diagnostic(result, output, 'process_reaping', exc)
                    raise
                result['exit_code'] = process.wait(timeout=5)
            phase='post_execution_disk_sample'
            if tree_bytes(work, output) > limits['disk_mb']*1024*1024:
                result.update(error='disk_limit', error_phase=phase)
            phase='verify_sources'
            after = {name:hashlib.sha256(checked_path(work,name).read_bytes()).hexdigest() for name in hashes}
            if after != hashes: result.update(error='source_changed_during_test', error_phase=phase)
            if limits.get('invocation'):
                from .code_check_invocation import unchanged
                if not unchanged(work,manifest,limits):
                    result.update(error='frozen_delivery_input_changed_during_test', error_phase=phase)
            phase='collect_reports'
            for name in limits.get('report_paths',manifest.get('test_result_files', [])):
                path = checked_path(work, name)
                if not path.is_file(): continue
                if path.stat().st_size > 4*1024*1024: raise ValueError('Host test report is too large')
                target = output / name; target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
                if path.suffix == '.json':
                    report = json.loads(path.read_bytes())
                    result['test_reports'][name] = report
            reports = list(result['test_reports'].values())
            matching = [r for r in reports if isinstance(r, dict) and r.get('source_sha256') == hashes]
            reports_passed = (bool(matching) and len(matching)==len(reports)
                and all(r.get('passed') is True for r in matching))
            if limits.get('report_contract')=='development_report_v1':
                reports_passed = (set(result['test_reports'])==set(limits['report_paths'])
                    and bool(reports) and all(_development_report_passed(r,hashes,manifest,
                        input_scope=limits.get('invocation',{}).get('input_scope')) for r in reports))
            result['passed'] = (result['exit_code'] == 0 and not result.get('error')
                and result['status'] not in {'timeout','interrupted'}
                and (configuration['deadline'] is None or time.monotonic() < configuration['deadline'])
                and reports_passed)
            if result['passed']: result['status'] = 'completed'
            elif not result.get('error'):
                result['error'] = 'bounded_check_not_passed'
                result['error_phase'] = 'validate_result'
        except Exception as exc:
            import traceback
            result.update(passed=False,error=type(exc).__name__+': '+str(exc)[:1000],
                error_phase=phase,error_traceback=traceback.format_exc(limit=12)[-12000:])
            (output/'host-error.json').write_text(json.dumps({k:result[k] for k in
                ('error','error_phase','error_traceback')},ensure_ascii=False),encoding='utf-8')
        finally:
            if not reaped:
                try:
                    if process and 'pre_cleanup_exit_code' not in result:
                        result['pre_cleanup_exit_code'] = process.poll()
                    _reap_check(process, job, children)
                    reaped = True
                except Exception as exc:
                    if process:
                        result['exit_code'] = process.poll()
                    _cleanup_diagnostic(result, output, 'process_reaping', exc)
                    if job:
                        # Release the kill-on-close handle after the final
                        # retry; release alone is not proof of process exit.
                        try:
                            job.close()
                        except Exception as close_error:
                            _cleanup_diagnostic(result, output, 'job_handle_close', close_error)
            result['process_cleanup_confirmed'] = reaped
            if process:
                result['exit_code'] = process.poll()
            # Preserve reports already written before a crash, timeout or bad
            # report. They remain evidence of the failed prefix, never a pass.
            for name in set(manifest.get('test_result_files', [])) | set(limits.get('report_paths',[])):
                try:
                    path = checked_path(work, name)
                    if not path.is_file() or path.stat().st_size > 4*1024*1024: continue
                    target = output/name
                    if not target.exists():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(path, target)
                except (OSError, ValueError): pass
    result['duration_seconds'] = time.monotonic()-started
    for path in sorted(output.rglob('*')):
        if path.is_file():
            result['artifacts'].append({'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    for stream, size in [('stdout',6000), ('stderr',12000)]:
        path = output/(stream+'.txt')
        result[stream+'_tail'] = ''
        if path.exists():
            with path.open('rb') as handle:
                handle.seek(max(0,path.stat().st_size-size))
                result[stream+'_tail'] = handle.read(size).decode('utf-8',errors='replace')
    return result


def _finite_cpu_affinity(process, cores):
    """Keep affinity where supported, preserving legacy finite POSIX support."""
    affinity = getattr(process, 'cpu_affinity', None)
    if callable(affinity):
        affinity(affinity()[:cores])


async def run(engine, run_id, directory, *, cancel=None, workflow_id=None, admission_key=None):
    """Replay terminal checks; recover a known host-only interruption within quota."""
    from .codex_workflow import _read_execution_gate
    from .lab import hardware_profile, assess_resources
    from .python_environments import apply_execution_preferences
    directory = Path(directory)
    admission = policy(engine, run_id, workflow_id=workflow_id)
    if admission_key:
        selected=engine.store.get(POLICIES,admission_key)
        if (not selected or selected.get('run_id')!=run_id or selected.get('workflow_id')!=workflow_id
                or selected.get('id')!=admission_key or selected.get('invocation_version')!=2
                or selected.get('sha256')!=sha({k:v for k,v in selected.items() if k!='sha256'})
                or (admission and selected.get('allowance_policy_sha256')!=admission['sha256'])):
            raise ValueError('Checker step does not belong to the sealed current workflow admission')
        admission=selected
    if not admission: raise ValueError('Missing host verification admission')
    if admission.get('invocation_only'):
        raise ValueError('Select an independently admitted checker invocation before execution')
    manifest = json.loads((directory/'implementation.json').read_bytes())
    if manifest.get('implementation_document_sha256') != admission['implementation_document_sha256']:
        from .implementation_definition_repair import admits_document
        admitted = await engine._read_consensus_check(run_id, admits_document, engine, run_id,
            admission, manifest.get('implementation_document_sha256'))
        if not admitted:
            raise ValueError('Host verification requires a separately admitted document version')
    hashes = await asyncio.to_thread(_inventory, directory, manifest)
    if admission.get('invocation_version')==2 and hashes!=admission['checker_input_sha256']:
        raise ValueError('Checker source differs from its independently admitted invocation')
    entry = admission['entrypoint']
    if entry not in hashes: raise ValueError('Admitted test entrypoint is missing')
    await _read_execution_gate(engine, run_id)
    config = engine.store.get('runs', run_id)['config']
    if config.get('execution_mode', 'local') != 'local':
        raise ValueError('This bounded host-test adapter requires admitted local execution')
    design = (await asyncio.to_thread(engine.outputs, run_id))['design']
    profile = await asyncio.to_thread(hardware_profile)
    profile = await asyncio.to_thread(apply_execution_preferences, config, profile)
    # This adapter runs the approved CPU entry, not the training job. Retain
    # its CPU/dependency ceilings without importing training VRAM or model-size
    # estimates (including nested resource aliases).
    planned = design['resource_plan']
    planned = {**planned, **(planned.get('resources') if isinstance(planned.get('resources'),dict) else {})}
    resource = {k:planned[k] for k in ('ram_gb','memory_gb','disk_gb','cpu_cores',
        'cpu_seconds','required_packages','framework') if k in planned}
    resource.update(gpu_required=False, estimated_vram_gb=0,
        timeout_seconds=min(admission['timeout_seconds'],
            float(planned.get('timeout_seconds',planned.get('timeout',admission['timeout_seconds'])))))
    assessment = assess_resources(resource, profile)
    if not assessment['approved']: raise ValueError('Host verification resources unavailable: '+str(assessment.get('reasons')))
    limits = {**assessment['limits'], 'gpu_allowed': False,
              'python_executable': assessment['limits'].get('python_executable') or sys.executable}
    limits['deadline_environment_variable'] = deadline_variable(checked_path(directory, entry), admission.get('deadline_environment_variable'))
    if admission.get('report_contract')=='development_report_v1':
        limits['report_contract']=admission['report_contract']
        limits['report_paths']=copy.deepcopy(admission['report_paths'])
    limits['cpu_cores'] = min(2, limits['cpu_cores'])
    limits['disk_mb'] = min(512, limits['disk_mb'])
    limit = min(admission['timeout_seconds'], float(limits.get('timeout_seconds',admission['timeout_seconds'])),
                float(config.get('experiment_timeout',admission['timeout_seconds'])))
    identity = {'policy_sha256': admission['sha256'], 'source_sha256': hashes,
                'entrypoint': entry, 'python_executable': limits['python_executable'],
                'resource_limits': {k:limits.get(k) for k in ('memory_mb','cpu_cores','disk_mb')},
                'timeout_seconds': limit}
    if admission.get('invocation_version')==2:
        from .code_check_invocation import inventory
        limits['invocation']=copy.deepcopy(admission['invocation'])
        identity.update(invocation_version=2,invocation=copy.deepcopy(admission['invocation']))
        if admission['invocation']['input_scope']=='frozen_delivery':
            limits['frozen_delivery_sha256']=await asyncio.to_thread(inventory,directory,manifest)
            if limits['frozen_delivery_sha256']!=admission['frozen_delivery_sha256']:
                raise ValueError('Complete frozen input differs from the admitted checker invocation')
            identity['frozen_delivery_sha256']=copy.deepcopy(limits['frozen_delivery_sha256'])
        if admission.get('allowance_policy_sha256'):
            identity['allowance_policy_sha256']=admission['allowance_policy_sha256']
    key = run_id+':'+sha(identity)
    saved = engine.store.get(RECEIPTS, key)
    if saved and _sampling_recovery(saved):
        saved=_sealed_receipt(engine,saved,identity)
        recovery=_sampling_recovery(saved,artifact_reports=_recovery_artifact_reports(saved))
        if not recovery:
            return saved
        identity = {**identity,'executor_recovery':recovery}
        key = run_id+':'+sha(identity)
        saved = engine.store.get(RECEIPTS,key)
    if saved:
        if saved.get('status') == 'running':
            # Unknown completion is never a fresh execution grant after restart.
            saved.update(status='interrupted', passed=False, error='host_test_completion_unconfirmed',
                         duration_seconds=saved['allowance_seconds'], finished_at=now())
            saved['sha256'] = sha({k:v for k,v in saved.items() if k != 'sha256'})
            engine.store.put(RECEIPTS, key, saved)
        return _sealed_receipt(engine, saved, identity)
    if admission.get('invocation_version')==2:
        from .code_check_invocation import retained
        previous=retained(engine,run_id,identity,admission['report_paths'])
        if previous is not None:return previous
    inherited = 0.0
    for ref in admission['inherited_executions']:
        original = engine.store.get('codex_checks', ref['storage_key'])
        if not original or sha(original) != ref['sha256']: raise ValueError('Original shared-quota receipt changed')
        inherited += ref['seconds']
    # A source edit allows a new check batch, but not a reset of the admitted
    # shared allowance through changed code, reports, documents or a restart.
    allowance=admission.get('allowance_policy_sha256',admission['sha256'])
    executions=[r for r in engine.store.list(RECEIPTS,run_id)
        if r.get('identity',{}).get('allowance_policy_sha256',r.get('identity',{}).get('policy_sha256'))==allowance]
    if admission.get('invocation_version')==2:
        for receipt in executions:_sealed_receipt(engine,receipt,receipt['identity'])
    consumed=sum(r.get('duration_seconds',r.get('allowance_seconds',0)) for r in executions)
    shared_limit=min(float(admission.get('shared_timeout_seconds',limit)),
        float(config.get('experiment_timeout',admission['timeout_seconds'])))
    remaining = min(limit,shared_limit-inherited-consumed)
    if remaining <= 0: raise ValueError('Shared bounded-check allowance is exhausted')
    limits['cpu_seconds'] = min(limits.get('cpu_seconds', math.inf),
        max(1, math.ceil(remaining*limits['cpu_cores'])))
    output = engine.run_dir(run_id)/'code-verification'/sha(identity)
    record = {'id': key, 'run_id': run_id, 'identity': identity, 'status':'running',
        'passed':False, 'started_at':now(), 'allowance_seconds':remaining,
        'shared_allowance_seconds':shared_limit, 'inherited_seconds':inherited,
        'formal_acceptance':False, 'scientific_success':False, 'training_authorized':False}
    engine.store.put(RECEIPTS,key,record)
    stop = __import__('threading').Event()
    task = asyncio.create_task(asyncio.to_thread(_execute,directory,manifest,hashes,entry,limits,remaining,output,
                          lambda:stop.is_set() or bool(cancel and cancel.is_set())))
    try:
        result = await asyncio.shield(task)
    except asyncio.CancelledError:
        from .async_ownership import finish
        stop.set()
        result = await finish(task, propagate_cancel=False)
        record.update(result,finished_at=now());record['sha256']=sha(record)
        engine.store.put(RECEIPTS,key,record)
        raise
    record.update(result,finished_at=now());record['sha256']=sha(record)
    engine.store.put(RECEIPTS,key,record)
    engine.store.event(run_id,'implementation.bounded_checks_finished','宿主有界测试完成；记录真实断言结果，未执行模型训练。',
                       {'receipt_id':key,'status':record['status'],'passed':record['passed']})
    return record
