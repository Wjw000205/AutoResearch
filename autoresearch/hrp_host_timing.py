"""Host transport for a task-declared once-only supplemental timing entry.

The authorization, not a code hash, is the execution key. A failure consumes it.
No wall or CPU deadline is introduced. Memory, disk, process and cancellation
limits remain enforced. Stdout/stderr are drained concurrently to durable files;
the capture interval overlaps the producer interval and must not be added to it.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import uuid

from .code_repair_contract import sha
from .code_verification import checked_path, _inventory, _sealed_receipt
from .storage import now

RECEIPTS = 'definition_execution_receipts'
CLAIMS = 'implementation_timing_claims'


def task_contract(spec):
    value = copy.deepcopy(spec.get('supplemental_timing'))
    required = {'entrypoint', 'report_path', 'deadline_check_entrypoint', 'transport_reserve_bytes',
                'resource_caps', 'environment'}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError('Approved SPEC must declare the supplemental timing interface')
    for key in ('entrypoint', 'report_path', 'deadline_check_entrypoint'):
        checked_path(Path.cwd(), value[key])
    if (not value['entrypoint'].endswith('.py') or not value['deadline_check_entrypoint'].endswith('.py')
            or not value['report_path'].endswith('.json')
            or type(value['transport_reserve_bytes']) is not int or value['transport_reserve_bytes'] < 0):
        raise ValueError('Invalid supplemental timing paths or reserve')
    caps = value['resource_caps']
    if (not isinstance(caps, dict) or set(caps) != {'memory_mb', 'disk_mb', 'cpu_cores', 'gpu_memory_mb'}
            or any(type(v) is not int or v <= 0 for v in caps.values())
            or not isinstance(value['environment'], dict) or not value['environment']
            or any(not isinstance(k, str) or not isinstance(v, str) or not v for k, v in value['environment'].items())):
        raise ValueError('Supplemental timing requires explicit resource caps and environment versions')
    return value


def file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def seal(engine, run_id, identity, value, paths=()):
    record = {'id':run_id+':'+sha(identity), 'run_id':run_id, 'identity':identity,
              'created_at':now(), **value,
              'artifacts':[{'path':str(Path(p).resolve()),'sha256':file_sha(p),
                            'bytes':Path(p).stat().st_size} for p in paths]}
    root=engine.run_dir(run_id).resolve()
    for item in record['artifacts']:
        name=Path(item['path']).relative_to(root).as_posix()
        item['artifact_id']=run_id+':'+name
        engine.store.put('artifacts',item['artifact_id'],{'id':item['artifact_id'],'run_id':run_id,
            'name':name,'path':item['path'],'sha256':item['sha256'],'created_at':now(),'kind':'host_execution_evidence'})
    record['sha256'] = sha(record)
    engine.store.put(RECEIPTS,record['id'],record)
    return record


def view(record, attempt):
    keys = ('id','sha256','status','passed','identity','summary','test_reports','transport',
            'artifacts','error','entrypoint','duration_seconds','exit_code','limitations')
    result = {k:copy.deepcopy(record[k]) for k in keys if k in record}
    # Frozen project sources are already bound by snapshot_sha256. Keep the
    # captured bytes on disk and expose only useful report/adapter locators.
    result['artifacts']=[a for a in result.get('artifacts',[]) if '/workspace/' not in a['path'].replace('\\','/')]
    report=result.get('test_reports',{}).get('supplemental_timing')
    if report:
        result['test_reports']['supplemental_timing']={k:report.get(k) for k in
            ('outcome','completed','failure','missing','resource_passed','metrics','evidence_wall_seconds')}
        result['test_reports']['supplemental_timing']['records']=[{k:r[k] for k in
            ('operation','name','wall_seconds','peak_bytes','W_solver','missing_evidence') if k in r}
            for r in report.get('records',[])]
    result['receipt_collection'] = RECEIPTS
    result['document_memory_handoffs'] = [{k:v[k] for k in ('role','handoff_id')}
        for v in attempt.get('review_votes',[]) if v.get('handoff_id')]
    return result


def validate(engine, run_id, attempt, value):
    record = engine.store.get(RECEIPTS,value['id'])
    if not record or record['run_id'] != run_id:
        raise ValueError('Missing definition execution receipt')
    _sealed_receipt(engine,record,record['identity'])
    if record['identity']['snapshot_sha256'] != attempt['snapshot']['files_hash'] or view(record,attempt) != value:
        raise ValueError('Definition execution evidence changed')
    return record


def artifact_binding(engine,run_id,context,artifact_id):
    """Authorize a host receipt file separately from the code-delivery scope."""
    frozen=context.get('host_verification',{})
    if frozen.get('receipt_collection')!=RECEIPTS: return None
    record=engine.store.get(RECEIPTS,frozen.get('id',''))
    scope=context.get('definition_code_followup',{})
    if (not record or record.get('run_id')!=run_id or record.get('sha256')!=frozen.get('sha256')
            or record.get('sha256')!=sha({k:v for k,v in record.items() if k!='sha256'})
            or record.get('identity',{}).get('snapshot_sha256')!=scope.get('snapshot_sha256')):
        raise ValueError('Host evidence receipt is not bound to this frozen review')
    matches=[a for a in record.get('artifacts',[]) if a.get('artifact_id')==artifact_id]
    declared=[a for a in frozen.get('artifacts',[]) if a.get('artifact_id')==artifact_id]
    if len(matches)!=1 or declared!=matches: return None
    return copy.deepcopy(matches[0])


def plan(engine, run_id, state, spec, attempt):
    from .deadline_contract_checks import check
    from .implementation_definition_repair import timing_authorization
    authorization = timing_authorization(engine,state)
    contract = task_contract(spec['spec'])
    if (authorization['maximum_executions'] != 1 or authorization['additional_wall_seconds'] is not None
            or authorization['training_authorized'] or authorization['repeat_original_probe_authorized']
            or authorization['reset_original_allowance']):
        raise ValueError('Unsupported supplemental timing authorization')
    identity = {'kind':'supplemental_transport_plan_v2','snapshot_sha256':attempt['snapshot']['files_hash'],
        'spec_hash':spec['spec_hash'],'document_sha256':spec['document']['sha256'],
        'authorization_sha256':authorization['sha256'],
        'adapter_sha256':file_sha(__file__),
        'deadline_checker_sha256':file_sha(Path(__file__).with_name('deadline_contract_checks.py'))}
    saved = engine.store.get(RECEIPTS,run_id+':'+sha(identity))
    if saved: return _sealed_receipt(engine,saved,identity)
    result = check(checked_path(Path(attempt['snapshot']['workspace']), contract['deadline_check_entrypoint']))
    output = engine.run_dir(run_id)/'definition-execution'/sha(identity)
    output.mkdir(parents=True,exist_ok=True)
    paths = []
    for source in (Path(__file__),Path(__file__).with_name('deadline_contract_checks.py')):
        p = output/source.name
        shutil.copyfile(source,p)
        # Expose the exact host code to artifacts.read without injecting its body.
        paths.append(p)
    description = {'entrypoint':contract['entrypoint'], 'task_contract':contract,
        'wall_seconds':None,'cpu_seconds':None,'maximum_executions':1,
        'hardware_admission':'Use the unchanged approved resource plan, including required CUDA availability, '
            'selected GPU identity and its current stricter VRAM limit; no implicit CPU fallback.',
        'environment_initialization':'Windows version tuple is measured by the selected interpreter before the claim; '
            'the child reuses that OS metadata for platform._syscmd_ver so runtime imports need no shell. '
            'Numeric operators are not warmed up or invoked during this metadata read.',
        'deadline_checks':result,
        'transport':{'stdout':'PIPE -> dedicated reader -> binary file; 64 KiB chunks',
            'stderr':'separate PIPE and reader; same flush/fsync/close contract',
            'measurement':'perf_counter_ns from first read attempt through EOF, flush, fsync and close',
            'overlap':'capture contains producer execution; record separately, never sum as disjoint costs',
            'memory':'bounded chunk buffers; complete output is not retained or parsed in host memory',
            'completion':'child exit plus both drains joined plus source hash readback',
            'disk_limit_bytes':contract['resource_caps']['disk_mb']*1024**2,
            'ram_limit_bytes':contract['resource_caps']['memory_mb']*1024**2,
            'transport_reserve_bytes':contract['transport_reserve_bytes']},
        'scope':'Only frozen dedicated non-training entry; no old suite, original probe, fit or cache reset.'}
    p=output/'plan.json';p.write_text(json.dumps(description,ensure_ascii=False,indent=2),encoding='utf-8');paths.append(p)
    return seal(engine,run_id,identity,{'status':'prepared','passed':result['passed'],'entrypoint':contract['entrypoint'],
        'summary':description, 'test_reports':{'deadline_difference':result},
        'limitations':['Controlled-clock unit checks are not research execution or scientific acceptance.',
                       'Supplemental timing has not yet run.']},paths)


def claim(store, authorization, identity):
    nonce = uuid.uuid4().hex
    candidate = {'id':authorization['id'],'identity':identity,'nonce':nonce,'status':'claimed',
                 'run_id':authorization['run_id'],'created_at':now(),'owner_pid':os.getpid()}
    import psutil
    candidate['owner_started'] = psutil.Process().create_time()
    value = store.mutate(CLAIMS,authorization['id'],lambda old: old or candidate)
    return value, value.get('nonce') == nonce


def drain(pipe, target, outcome):
    start = time.perf_counter_ns()
    count = 0
    try:
        with target.open('wb') as stream:
            while True:
                data = pipe.read(64*1024)
                if not data: break
                stream.write(data);count += len(data)
            stream.flush();os.fsync(stream.fileno())
        outcome.update(completed=True,bytes=count)
    except Exception as exc:
        outcome.update(completed=False,bytes=count,error=type(exc).__name__+': '+str(exc))
    finally:
        pipe.close()
        outcome.update(start_ns=start,end_ns=time.perf_counter_ns())
        outcome['seconds']=(outcome['end_ns']-start)/1e9


BOOTSTRAP = '''import json,os,runpy,sys,time,platform
from pathlib import Path
c=json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
while not Path(c['gate']).exists(): time.sleep(.02)
if c.get('windows_version_probe') is not None:
    observed_version=tuple(c['windows_version_probe'])
    platform._syscmd_ver=lambda *args,**kwargs: observed_version
def deny(event,args):
    if event in {'subprocess.Popen','os.system','os.posix_spawn','socket.connect','socket.bind'}:
        raise RuntimeError('Supplemental timing forbids subprocesses and network: '+event)
sys.addaudithook(deny)
sys.argv=[c['entrypoint'],c['manifest']]
runpy.run_path(c['entrypoint'],run_name='__main__')
'''


def execute(directory, hashes, manifest, limits, output, cancelled):
    from .lab import _WindowsJob, ExperimentRunner
    import psutil
    contract = task_contract({'supplemental_timing': manifest['task_contract']})
    work=output/'workspace';work.mkdir(parents=True,exist_ok=False)
    for name in hashes:
        target=checked_path(work,name);target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(checked_path(directory,name),target)
    trial=output/'trial';trial.mkdir()
    manifest={**manifest,'token_file':str(trial/'token.json')}
    config=trial/'manifest.json';config.write_text(json.dumps(manifest,ensure_ascii=False),encoding='utf-8')
    (trial/'token.json').write_text(json.dumps({'id':manifest['invocation_id'],
        'manifest_sha256':file_sha(config),'authorization_sha256':manifest['authorization']['sha256']}),encoding='utf-8')
    bootstrap=work/'.host-timing.py';bootstrap.write_text(BOOTSTRAP,encoding='utf-8')
    control={'gate':str(work/'.start'),'entrypoint':str(checked_path(work,contract['entrypoint'])),'manifest':str(config),
             'windows_version_probe':manifest.get('windows_version_probe')}
    controlfile=work/'.host-timing.json';controlfile.write_text(json.dumps(control),encoding='utf-8')
    env=ExperimentRunner._environment(work,limits)
    env.update(PYTHONDONTWRITEBYTECODE='1',PYTHONUTF8='1')
    result={'status':'failed','passed':False,'exit_code':None,'transport':{},'test_reports':{}}
    process=job=None;threads=[];started=time.perf_counter_ns()
    try:
        process=subprocess.Popen([limits['python_executable'],'-I',str(bootstrap),str(controlfile)],
            cwd=work,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW)
        job=_WindowsJob(process.pid,limits['memory_mb'],None,2,process_limit=1)
        for name,pipe in [('stdout',process.stdout),('stderr',process.stderr)]:
            outcome={};result['transport'][name]=outcome
            thread=threading.Thread(target=drain,args=(pipe,output/(name+'.txt'),outcome),daemon=True)
            thread.start();threads.append(thread)
        Path(control['gate']).touch()
        peak_private=peak_rss=peak_disk=0
        while process.poll() is None:
            if cancelled(): result['error']='host_cancelled';break
            if any(t.get('completed') is False for t in result['transport'].values()):
                result['error']='output_capture_failed';break
            size=sum(p.stat().st_size for p in output.rglob('*') if p.is_file() and not p.is_symlink())
            peak_disk=max(peak_disk,size)
            if size>limits['disk_mb']*1024**2: result['error']='disk_limit';break
            try:
                mem=psutil.Process(process.pid).memory_info()
                peak_rss=max(peak_rss,mem.rss);peak_private=max(peak_private,getattr(mem,'private',mem.rss))
                if max(mem.rss,getattr(mem,'private',mem.rss))+contract['transport_reserve_bytes']>limits['memory_mb']*1024**2:
                    result['error']='ram_transport_limit';break
            except psutil.NoSuchProcess: pass
            time.sleep(.05)
        if process.poll() is None:
            job.close();process.kill()
        result['exit_code']=process.wait()
        for thread in threads: thread.join()
        result['observed_resources']={'peak_child_rss':peak_rss,'peak_child_private':peak_private,
                                      'peak_disk_bytes':peak_disk,'solver_workspace_upper_bound':None}
        if any(file_sha(checked_path(work,n))!=digest for n,digest in hashes.items()):
            result['error']='source_changed_during_timing'
        report=checked_path(trial,contract['report_path'])
        if report.is_file() and report.stat().st_size<4*1024**2:
            result['test_reports']['supplemental_timing']=json.loads(report.read_bytes())
        child=result['test_reports'].get('supplemental_timing',{})
        result['passed']=(result['exit_code']==0 and not result.get('error')
            and all(t.get('completed') for t in result['transport'].values())
            and child.get('outcome')=='completed' and child.get('completed')==1)
        if result['passed']: result['status']='completed'
        elif not result.get('error'): result['error']='supplemental_timing_not_completed'
    except Exception as exc:
        result['error']=type(exc).__name__+': '+str(exc)[:1000]
    finally:
        if job: job.close()
        if process and process.poll() is None:
            process.kill();process.wait()
        for thread in threads: thread.join()
        result['duration_seconds']=(time.perf_counter_ns()-started)/1e9
    result['transport']['accounting']='Producer and drain overlap; no sum asserted. Host close/fsync included.'
    result['limitations']=['Measured deterministic fixtures are not training or scientific validation.',
        'Resource coverage, applicability and solver workspace still require evidence; no resource pass is inferred.']
    return result


async def run(engine,run_id,state,spec,attempt,plan_record,entry_votes):
    from .implementation_definition_repair import timing_authorization
    from .codex_workflow import _read_execution_gate, _verify_snapshot
    from .lab import hardware_profile, assess_resources, ExperimentRunner
    from .python_environments import apply_execution_preferences
    await _read_execution_gate(engine,run_id)
    await engine._read_consensus_check(run_id,_verify_snapshot,engine,run_id,attempt['snapshot'])
    authorization=timing_authorization(engine,state)
    contract=task_contract(spec['spec'])
    _sealed_receipt(engine,plan_record,plan_record['identity'])
    if plan_record['summary'].get('task_contract') != contract:
        raise ValueError('Timing interface differs from the reviewed plan')
    if plan_record['identity']['adapter_sha256']!=file_sha(__file__):
        raise ValueError('Host adapter changed after transport review')
    if {v['role'] for v in entry_votes}!={'director','engineer','scout'} or len(entry_votes)!=3 or not all(v.get('timing_entry_approved') is True for v in entry_votes):
        raise ValueError('Three independent timing entry approvals required')
    identity={'kind':'supplemental_timing_v2','snapshot_sha256':attempt['snapshot']['files_hash'],
        'spec_hash':spec['spec_hash'],'document_sha256':spec['document']['sha256'],
        'authorization_sha256':authorization['sha256'],'plan_receipt_sha256':plan_record['sha256'],
        'entry_votes_sha256':sha(entry_votes)}
    existing=engine.store.get(CLAIMS,authorization['id'])
    if existing:
        receipt=engine.store.get(RECEIPTS,existing.get('receipt_id',''))
        if receipt:
            _sealed_receipt(engine,receipt,receipt['identity'])
            if receipt['identity']!=identity: raise ValueError('Once-only timing belongs to another frozen delivery')
            return receipt
        raise ValueError('Once-only timing already claimed; inspect preserved output, never relaunch')
    config=engine.store.get('runs',run_id)['config']
    if os.name!='nt' or config.get('execution_mode','local')!='local':
        raise ValueError('This approved timing transport requires Windows local execution')
    design=(await asyncio.to_thread(engine.outputs,run_id))['design']
    profile=await asyncio.to_thread(hardware_profile)
    profile=await asyncio.to_thread(apply_execution_preferences,config,profile)
    assessment=assess_resources(design['resource_plan'],profile)
    if not assessment['approved']: raise ValueError('Supplemental timing resources unavailable: '+str(assessment.get('reasons')))
    limits={**assessment['limits'],'cpu_seconds':None,'timeout_seconds':None}
    for key, cap in contract['resource_caps'].items():
        limits[key] = min(cap, limits.get(key) or cap)
    if limits['memory_mb']*1024**2<=contract['transport_reserve_bytes']: raise ValueError('Insufficient memory after approved transport reserve')
    directory=Path(attempt['snapshot']['workspace'])
    hashes=await asyncio.to_thread(_inventory,directory,json.loads((directory/'implementation.json').read_bytes()))
    output=engine.run_dir(run_id)/'supplemental-timing'/sha(authorization['id'])
    output.mkdir(parents=True,exist_ok=True)
    # Read-only environment probe is not a timing operation or a training test.
    env=ExperimentRunner._environment(output,limits)
    packages = [k for k in contract['environment'] if k != 'python']
    probe=('import json,platform,importlib.metadata;'
        'v={k:importlib.metadata.version(k) for k in '+repr(packages)+'};'
        'v.update(python=platform.python_version(),platform=platform.platform(),windows_version_probe=platform._syscmd_ver());'
        'print(json.dumps(v))')
    checked=await asyncio.to_thread(subprocess.run,[limits['python_executable'],'-I','-c',probe],
        env=env,capture_output=True,text=True,check=True,timeout=60,creationflags=subprocess.CREATE_NO_WINDOW)
    environment=json.loads(checked.stdout)
    windows_version=environment.pop('windows_version_probe')
    if design['resource_plan'].get('gpu_required') and not limits.get('gpu_allowed'):
        raise ValueError('Required CUDA unavailable; supplemental timing cannot fall back to CPU')
    if any(environment.get(k) != v for k, v in contract['environment'].items()):
        raise ValueError('Frozen timing environment mismatch before execution claim')
    runtime=dict(hashes)
    manifest={'authorization':{k:authorization[k] for k in ('id','sha256','version','source','additional_wall_seconds')},
        'implementation_document_sha256':spec['document']['sha256'],'spec_hash':spec['spec_hash'],
        'code_review':{'approved':True,'runtime_sha256':runtime,'votes':entry_votes},'runtime_sha256':runtime,
        'environment':environment,'task_contract':contract,'windows_version_probe':windows_version,
        'threads':limits['cpu_cores'],'invocation_id':sha(identity),
        'limits':{'ram_bytes':limits['memory_mb']*1024**2,'disk_bytes':limits['disk_mb']*1024**2,
                  'vram_bytes':int(limits['gpu_memory_mb'])*1024**2,
                  'transport_reserve_bytes':contract['transport_reserve_bytes']},
        'io_conditions':{'stdout':'concurrent pipe-to-disk drain','fsync':True,'path':str(output)},
        'host_gpu_admission':{k:limits.get(k) for k in ('gpu_allowed','gpu_uuid','gpu_index','gpu_memory_mb')}}
    engine.check_active(run_id)
    admitted,owns=claim(engine.store,authorization,identity)
    if not owns: raise ValueError('Timing already claimed by another worker')
    stop=threading.Event();cancel=engine.cancellations.get(run_id)
    task=asyncio.create_task(asyncio.to_thread(execute,directory,hashes,manifest,limits,output,
        lambda:stop.is_set() or bool(cancel and cancel.is_set())))
    cancelled=False
    try: result=await asyncio.shield(task)
    except asyncio.CancelledError:
        stop.set();result=await task;cancelled=True
    except Exception as exc:
        result={'status':'failed','passed':False,'error':type(exc).__name__+': '+str(exc)[:1000]}
    paths=[p for p in output.rglob('*') if p.is_file() and not p.is_symlink()]
    receipt=await asyncio.to_thread(seal,engine,run_id,identity,result,paths)
    admitted.update(status='finished',receipt_id=receipt['id'],finished_at=now())
    engine.store.put(CLAIMS,authorization['id'],admitted)
    engine.store.event(run_id,'implementation.supplemental_timing_finished',
        '一次补充计时已保存真实回执；未重跑原探针或训练。',{'receipt_id':receipt['id'],'passed':receipt['passed']})
    if cancelled: raise asyncio.CancelledError
    return receipt
