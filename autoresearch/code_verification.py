"""Host-owned bounded checks of frozen deliveries; never a training retry.

Policies are explicit, source-bound operator admissions. A terminal receipt is
replayed for the same source and policy, including failures. Reports produced by
the child are evidence, not authority to approve code or scientific outcomes.
"""
from __future__ import annotations

import asyncio
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

from .storage import now
from .code_repair_contract import sha

POLICIES = 'code_verification_policies'
RECEIPTS = 'code_verification_receipts'
VERSION = 1


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


def policy(engine, run_id):
    value = engine.store.get(POLICIES, run_id)
    if value and (value.get('version') != VERSION or value.get('run_id') != run_id
            or value.get('sha256') != sha({k: v for k, v in value.items() if k != 'sha256'})):
        raise ValueError('Verification policy identity changed')
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
    resource.setrlimit(resource.RLIMIT_CPU,(c['cpu_seconds'],c['cpu_seconds']+1))
    resource.setrlimit(resource.RLIMIT_FSIZE,(c['disk_mb']*1024*1024,)*2)
os.environ[c['deadline_environment_variable']]=str(c['deadline'])
sys.argv=[c['entrypoint']]
runpy.run_path(c['entrypoint'],run_name='__main__')
'''


def _execute(directory, manifest, hashes, entrypoint, limits, seconds, output, cancelled):
    from .lab import _WindowsJob, ExperimentRunner
    import psutil
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    result = {'status': 'failed', 'passed': False, 'command': [limits['python_executable'], '-I', entrypoint],
              'test_reports': {}, 'artifacts': [], 'exit_code': None}
    with tempfile.TemporaryDirectory(prefix='autoresearch-code-check-') as temporary:
        work = Path(temporary)
        for name in hashes:
            target = checked_path(work, name); target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(checked_path(directory, name), target)
        bootstrap = work / '.host-check.py'; bootstrap.write_text(_BOOTSTRAP, encoding='utf-8')
        configuration = {**limits, 'entrypoint': entrypoint, 'gate': str(work/'.host-start'),
                         'startup_deadline': time.monotonic()+10, 'deadline': started+seconds}
        configuration['deadline_environment_variable'] = deadline_variable(
            checked_path(directory, entrypoint), limits.get('deadline_environment_variable'))
        config = work / '.host-check.json'; config.write_text(json.dumps(configuration), encoding='utf-8')
        env = ExperimentRunner._environment(work, limits)
        env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1')
        env[configuration['deadline_environment_variable']] = str(configuration['deadline'])
        process = job = None
        try:
            with (output/'stdout.txt').open('wb') as out, (output/'stderr.txt').open('wb') as err:
                process = subprocess.Popen([limits['python_executable'], '-I', str(bootstrap), str(config)],
                    cwd=work, env=env, stdout=out, stderr=err,
                    **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {'start_new_session': True}))
                if os.name == 'nt':
                    job = _WindowsJob(process.pid, limits['memory_mb'], limits['cpu_seconds'], limits['cpu_cores'])
                Path(configuration['gate']).touch()
                while process.poll() is None:
                    if cancelled(): result['status'] = 'interrupted'; break
                    if time.monotonic() >= configuration['deadline']: result['status'] = 'timeout'; break
                    size = sum(p.stat().st_size for folder in (work, output) for p in folder.rglob('*')
                               if p.is_file() and not p.is_symlink())
                    if size > limits['disk_mb']*1024*1024: result['error'] = 'disk_limit'; break
                    time.sleep(.05)
                if process.poll() is None:
                    if job: job.close()
                    else:
                        try:
                            children = psutil.Process(process.pid).children(recursive=True)
                            for child in reversed(children): child.kill()
                        except psutil.Error: pass
                    process.kill()
                result['exit_code'] = process.wait(timeout=5)
            after = {name:hashlib.sha256(checked_path(work,name).read_bytes()).hexdigest() for name in hashes}
            if after != hashes: result['error'] = 'source_changed_during_test'
            for name in manifest.get('test_result_files', []):
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
            result['passed'] = (result['exit_code'] == 0 and not result.get('error')
                and result['status'] not in {'timeout','interrupted'} and time.monotonic() < configuration['deadline']
                and bool(matching) and len(matching) == len(reports)
                and all(r.get('passed') is True for r in matching))
            if result['passed']: result['status'] = 'completed'
            elif not result.get('error'): result['error'] = 'bounded_check_not_passed'
        except Exception as exc:
            result.update(passed=False, error=type(exc).__name__+': '+str(exc)[:1000])
        finally:
            if job: job.close()
            if process and process.poll() is None:
                process.kill(); process.wait(timeout=5)
            # Preserve reports already written before a crash, timeout or bad
            # report. They remain evidence of the failed prefix, never a pass.
            for name in manifest.get('test_result_files', []):
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


async def run(engine, run_id, directory, *, cancel=None):
    """One durable batch per source/environment; replay failures without retry."""
    from .codex_workflow import _read_execution_gate
    from .lab import hardware_profile, assess_resources
    from .python_environments import apply_execution_preferences
    directory = Path(directory)
    admission = policy(engine, run_id)
    if not admission: raise ValueError('Missing host verification admission')
    manifest = json.loads((directory/'implementation.json').read_bytes())
    if manifest.get('implementation_document_sha256') != admission['implementation_document_sha256']:
        from .implementation_definition_repair import admits_document
        admitted = await engine._read_consensus_check(run_id, admits_document, engine, run_id,
            admission, manifest.get('implementation_document_sha256'))
        if not admitted:
            raise ValueError('Host verification requires a separately admitted document version')
    hashes = await asyncio.to_thread(_inventory, directory, manifest)
    entry = admission['entrypoint']
    if entry not in hashes: raise ValueError('Admitted test entrypoint is missing')
    await _read_execution_gate(engine, run_id)
    config = engine.store.get('runs', run_id)['config']
    if config.get('execution_mode', 'local') != 'local':
        raise ValueError('This bounded host-test adapter requires admitted local execution')
    design = (await asyncio.to_thread(engine.outputs, run_id))['design']
    profile = await asyncio.to_thread(hardware_profile)
    profile = await asyncio.to_thread(apply_execution_preferences, config, profile)
    resource = {**design['resource_plan'], 'gpu_required': False}
    assessment = assess_resources(resource, profile)
    if not assessment['approved']: raise ValueError('Host verification resources unavailable: '+str(assessment.get('reasons')))
    limits = {**assessment['limits'], 'gpu_allowed': False,
              'python_executable': assessment['limits'].get('python_executable') or sys.executable}
    limits['deadline_environment_variable'] = deadline_variable(checked_path(directory, entry), admission.get('deadline_environment_variable'))
    limits['cpu_cores'] = min(2, limits['cpu_cores'])
    limits['disk_mb'] = min(512, limits['disk_mb'])
    limit = min(admission['timeout_seconds'], float(limits.get('timeout_seconds',60)), float(config.get('experiment_timeout',60)))
    identity = {'policy_sha256': admission['sha256'], 'source_sha256': hashes,
                'entrypoint': entry, 'python_executable': limits['python_executable'],
                'resource_limits': {k:limits.get(k) for k in ('memory_mb','cpu_cores','disk_mb')},
                'timeout_seconds': limit}
    key = run_id+':'+sha(identity)
    saved = engine.store.get(RECEIPTS, key)
    if saved:
        if saved.get('status') == 'running':
            # Unknown completion is never a fresh execution grant after restart.
            saved.update(status='interrupted', passed=False, error='host_test_completion_unconfirmed',
                         duration_seconds=saved['allowance_seconds'], finished_at=now())
            saved['sha256'] = sha({k:v for k,v in saved.items() if k != 'sha256'})
            engine.store.put(RECEIPTS, key, saved)
        return _sealed_receipt(engine, saved, identity)
    inherited = 0.0
    for ref in admission['inherited_executions']:
        original = engine.store.get('codex_checks', ref['storage_key'])
        if not original or sha(original) != ref['sha256']: raise ValueError('Original shared-quota receipt changed')
        inherited += ref['seconds']
    # A source edit allows a new check batch, but not a reset of the admitted
    # shared allowance through changed code, reports, documents or a restart.
    consumed = sum(r.get('duration_seconds',r.get('allowance_seconds',0)) for r in engine.store.list(RECEIPTS,run_id)
                   if r.get('identity',{}).get('policy_sha256') == admission['sha256'])
    remaining = limit-inherited-consumed
    if remaining <= 0: raise ValueError('Shared bounded-check allowance is exhausted')
    limits['cpu_seconds'] = max(1, math.ceil(remaining*limits['cpu_cores']))
    output = engine.run_dir(run_id)/'code-verification'/sha(identity)
    record = {'id': key, 'run_id': run_id, 'identity': identity, 'status':'running',
        'passed':False, 'started_at':now(), 'allowance_seconds':remaining,
        'shared_allowance_seconds':limit, 'inherited_seconds':inherited,
        'formal_acceptance':False, 'scientific_success':False, 'training_authorized':False}
    engine.store.put(RECEIPTS,key,record)
    stop = __import__('threading').Event()
    task = asyncio.create_task(asyncio.to_thread(_execute,directory,manifest,hashes,entry,limits,remaining,output,
                          lambda:stop.is_set() or bool(cancel and cancel.is_set())))
    try:
        result = await asyncio.shield(task)
    except asyncio.CancelledError:
        stop.set(); result = await task
        record.update(result,finished_at=now());record['sha256']=sha(record)
        engine.store.put(RECEIPTS,key,record)
        raise
    record.update(result,finished_at=now());record['sha256']=sha(record)
    engine.store.put(RECEIPTS,key,record)
    engine.store.event(run_id,'implementation.bounded_checks_finished','宿主有界测试完成；记录真实断言结果，未执行模型训练。',
                       {'receipt_id':key,'status':record['status'],'passed':record['passed']})
    return record
