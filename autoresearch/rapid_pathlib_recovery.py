"""Recover a bound CPU-check API/path mismatch without changing model code."""
import copy
import ast
import hashlib
import json
from pathlib import Path

from .experiment_workflow import sha
from .storage import now

LEGACY_PREFIX = 'from host_pathlib_compat import install as __rapid_install_pathlib_compat\n__rapid_install_pathlib_compat()\n'
PREFIX = LEGACY_PREFIX + 'from host_pathlib_compat import prepare_delivery_invocation as __rapid_prepare_delivery_invocation\n__rapid_prepare_delivery_invocation()\n'
ERROR = "AttributeError: 'WindowsPath' object has no attribute 'is_junction'"
RECOVERIES = 'host_check_recoveries'


def graph_entry(engine, run_id):
    key=(engine.store.get('runs',run_id) or {}).get('state',{}).get('host_check_recovery')
    if not key:return None
    saved=engine.store.get(RECOVERIES,key) or {};body=saved.get('body',{})
    if saved.get('run_id')!=run_id or body.get('run_id')!=run_id or saved.get('sha256')!=sha(body):
        raise ValueError('Host check recovery graph identity changed')
    if body['cycle']!=engine.cycle(run_id):return None
    return key+':graph','architecture_tuning'


def prefix(source):
    return '# Host check runtime SHA256: ' + hashlib.sha256(source).hexdigest() + '\n' + PREFIX


def verify_failure(engine, run_id, record):
    from .rapid_post_exit import safe
    trial=engine.store.get('experiments',run_id+':'+(record.get('check') or {}).get('id','')) or {}
    candidate=record.get('candidate') or {}
    if (record.get('run_id')!=run_id or record.get('status')!='check_failed'
            or sha(trial)!=(record.get('check') or {}).get('receipt_sha256')
            or trial.get('run_id')!=run_id or trial.get('status')!='failed'
            or trial.get('params',{}).get('phase')!='code_check'
            or trial.get('code_sha256')!=candidate.get('checks_sha256')
            or trial.get('limits',{}).get('code_bundle')!=candidate.get('check_bundle')):
        raise ValueError('Expected the exact saved pathlib compatibility failure')
    root=Path(trial['artifact_path'])
    if not root.resolve().is_relative_to(engine.data_dir.resolve()/'experiments'/run_id):
        raise ValueError('Failed check artifact belongs to another task')
    source=safe(root,'_delivery_snapshot/_delivery/evidence.py').read_bytes()
    expected=next(f for f in candidate['check_bundle']['files'] if f['path']=='_delivery_snapshot/_delivery/evidence.py')
    environment=json.loads(safe(root,'environment.json').read_bytes())
    version=tuple(int(x) for x in environment['software']['python_version'].split('.')[:2])
    if (hashlib.sha256(source).hexdigest()!=expected['sha256'] or b'.is_junction()' not in source
            or environment['code_sha256']!=candidate['checks_sha256']
            or version not in {(3,10),(3,11)}
            or environment['software']['python_executable']!=trial['limits']['python_executable']):
        raise ValueError('Compatibility failure lacks matching source/interpreter evidence')
    reason='pathlib_api_compatibility'
    stderr=trial.get('stderr','')
    if stderr.splitlines()[-1].startswith('FileNotFoundError: [WinError 206]'):
        failed=Path(ast.literal_eval(stderr.splitlines()[-1].rsplit(': ',1)[-1]))
        if (len(str(failed))<260 or failed.parent.name!='fixtures' or not failed.name.startswith('fixture-')
                or not failed.is_relative_to(safe(root,'_delivery_snapshot/_delivery/check_runs'))
                or not failed.is_dir() or 'in working_directory' not in stderr
                or 'def working_directory(path):' not in candidate['check_code']):
            raise ValueError('Failure is not a bound overlong check working directory')
        reason='windows_cwd_compatibility'
    elif not stderr.rstrip().endswith(ERROR):
        marker='FileNotFoundError: [Errno 2] No such file or directory: '
        tail=trial.get('stderr','').splitlines()[-1]
        if not tail.startswith(marker):raise ValueError('Unrecognized check runtime failure')
        failed=Path(ast.literal_eval(tail[len(marker):]))
        if ('dataset_runtime\\integrity.py' in stderr and 'verify_invocation' in stderr
                and failed==safe(root,'_delivery_snapshot/params.json') and not failed.exists()):
            integrity=safe(root,'_delivery_snapshot/dataset_runtime/integrity.py').read_bytes()
            bound=next(f for f in candidate['check_bundle']['files'] if f['path']=='_delivery_snapshot/dataset_runtime/integrity.py')
            params=json.loads(safe(root,'params.json').read_bytes())
            if (hashlib.sha256(integrity).hexdigest()!=bound['sha256']
                    or params.get('phase')!='code_check' or sha(params)!=environment.get('params_sha256')
                    or 'os.chdir(Path.cwd() / "_delivery_snapshot")' not in candidate['check_code']):
                raise ValueError('Missing invocation receipt lacks an authenticated host relocation')
            reason='delivery_invocation_receipts'
        elif (len(str(failed))<260 or not failed.name.endswith('.pending')
                or not failed.parent.is_dir() or not failed.resolve().is_relative_to(root.resolve())
                or 'exclusive_bytes' not in trial['stderr']):
            raise ValueError('Failure is not a bound long atomic evidence path')
        else:reason='windows_path_compatibility'
    if reason not in {'delivery_invocation_receipts','windows_cwd_compatibility'} and '_delivery\\evidence.py' not in stderr:
        raise ValueError('Compatibility failure did not originate in the bound evidence helper')
    return trial,environment['software']['python_version'],reason


def build(engine,run_id,record):
    from . import pathlib_check_runtime
    from .code_bundle import validate
    from .delivery_files import io_path
    from .rapid_experiment_revision import _write_files
    from .experiment_analysis import freeze_sources
    from .check_instrumentation import split_future_prefix
    c=copy.deepcopy(record['candidate'])
    validate(c['check_bundle'],c['check_code']);validate(c['review_bundle'],c['program']['code'])
    future,body=split_future_prefix(c['check_code'])
    support=Path(pathlib_check_runtime.__file__).read_bytes()
    files={f['path']:io_path(Path(c['check_bundle']['root'])/f['path']).read_bytes() for f in c['check_bundle']['files']}
    if 'host_pathlib_compat.py' in files:
        previous_prefix=prefix(files['host_pathlib_compat.py'])
        if body.startswith(previous_prefix):body=body[len(previous_prefix):]
        elif body.startswith('# Host check runtime SHA256: '+hashlib.sha256(files['host_pathlib_compat.py']).hexdigest()+'\n'+LEGACY_PREFIX):
            body=body[len('# Host check runtime SHA256: '+hashlib.sha256(files['host_pathlib_compat.py']).hexdigest()+'\n'+LEGACY_PREFIX):]
        elif body.startswith(LEGACY_PREFIX):body=body[len(LEGACY_PREFIX):]
        else:raise ValueError('Previous host check launcher binding changed')
    code=future+prefix(support)+body
    identity=sha({'prior':c['checks_sha256'],'compat':hashlib.sha256(support).hexdigest(),'code':hashlib.sha256(code.encode()).hexdigest()})
    root=engine.run_dir(run_id)/'host-check-repairs'/identity
    files.update({'host_pathlib_compat.py':support,'experiment.py':code.encode()})
    c['check_bundle']=_write_files(root/'check',files)
    review={f['path']:io_path(Path(c['review_bundle']['root'])/f['path']).read_bytes() for f in c['review_bundle']['files']}
    review['host_pathlib_compat.py']=support
    c['review_bundle']=_write_files(root/'review',review)
    c['code_sources']=freeze_sources(engine,run_id,c['program'],c['review_bundle'],delivery_support=True)
    c['check_code']=code;c['checks_sha256']=hashlib.sha256(code.encode()).hexdigest()
    c['check_workspace_layout']='rapid-v1'
    c['changed_ranges']=[r for r in c['changed_ranges'] if r['path']!='host_pathlib_compat.py']
    c['changed_ranges'].append({'path':'host_pathlib_compat.py','start':0,'end':len(support.decode()),
        'sha256':hashlib.sha256(support).hexdigest(),'scope':'host_check_compatibility'})
    return c


def prepare(engine,run_id,revision_id,*,instruction,budget_seconds=None):
    from . import rapid_experiment_revision as rapid,record_archive
    from .experiment_decisions import _archive,revision_context
    from .optimization_loop import current
    run=engine.store.get('runs',run_id) or {}
    if run.get('status') not in {'blocked','paused'} or not instruction.strip():
        raise ValueError('Compatibility recovery requires a stopped task and operator instruction')
    record=engine.store.get(rapid.COLLECTION,revision_id)
    if (record.get('host_check_repair') or {}).get('reason') in {'pathlib_api_compatibility','windows_path_compatibility','delivery_invocation_receipts','windows_cwd_compatibility','full_acceptance_budget'} and record['status']!='check_failed':
        rapid._validate_base(engine,run_id,record);rapid._candidate_unchanged(record)
        if not record.get('structured_review'):
            from .rapid_review_plan import prepare as prepare_review
            prepare_review(engine,run_id,revision_id,instruction)
        return record['host_check_repair']
    if budget_seconds is None:
        trial,version,reason=verify_failure(engine,run_id,record)
        check_budget=None
    else:
        from .rapid_check_budget import verify_timeout,policy
        trial,reason=verify_timeout(engine,run_id,record,budget_seconds)
        version=None;check_budget=policy(budget_seconds,instruction)
    rapid._validate_base(engine,run_id,record);rapid._candidate_unchanged(record)
    if not rapid._reviewed(record):raise ValueError('Original candidate was not independently approved')
    context=rapid._review_context(engine,run_id,record)
    for vote in record['votes']:rapid.review_receipts(engine,run_id,record,vote,context)
    review={'failure':{'kind':'experiment_decision_revision','details':{
        'decision_id':record['decision_id'],'proposal_sha256':record['decision_sha256']}}}
    if not revision_context(engine,run_id,review):raise ValueError('Original change authorization is unavailable')
    loop=current(engine.store,run_id,record['cycle']) or {}
    pending_id=run_id+':rapid:'+sha(loop.get('decision_id'))
    pending=engine.store.get(rapid.COLLECTION,pending_id)
    if pending and pending_id!=revision_id:
        if pending.get('codex_work_item') or pending.get('candidate') or pending.get('patch') or pending.get('status')!='implementing':
            raise ValueError('A later dispatched candidate must be reconciled independently')
    keys=[('runs',run_id),('optimization_loops',f'{run_id}:{record["cycle"]}'),('graph_status',run_id),
          (rapid.COLLECTION,revision_id)]+([(rapid.COLLECTION,pending_id)] if pending and pending_id!=revision_id else [])
    cursors=[(c,run_id+':'+stage) for c in ('steps','stage_contexts') for stage in ('tuning','architecture_tuning')]
    archives=[record_archive.save(engine.store,run_id,f) for c,k in keys+cursors
              if (f:=record_archive.fingerprint(engine.store,c,k))]
    original=copy.deepcopy(record)
    evidence=_archive(engine,run_id,{'rapid_revision_attempt':original,'failed_check':trial})
    if check_budget:
        from .rapid_check_budget import attach
        candidate=attach(engine,run_id,record['candidate'],check_budget)
    else:candidate=build(engine,run_id,record)
    if candidate['bundle']!=original['candidate']['bundle'] or candidate['assertion_count']!=original['candidate']['assertion_count']:
        raise ValueError('Compatibility recovery changed model bytes or assertions')
    audit={'reason':reason,'original':evidence,'failed_trial_id':trial['id'],
        'failed_receipt_sha256':sha(trial),'execution_python_version':version,'original_checks_sha256':original['candidate']['checks_sha256'],
        'checks_sha256':candidate['checks_sha256'],'instruction':instruction,'original_records':archives,
        'approval_inherited':False,'created_at':now()}
    for field in ('check','post_exit_verification','review_reads','review_purpose','review_clarifications',
        'source_read_recovery','review_history','review_contexts','structured_review','citation_corrections',
        'citation_diagnostics','error','completed_at'):
        record.pop(field,None)
    record.update(candidate=candidate,host_check_repair=audit,check_runtime_compatibility=1,
                  votes=[],status='reviewing',updated_at=now())
    loop={**loop,'status':'architecture_pending','decision_id':record['decision_id'],'review':review,
          'result':None,'updated_at':now(),'scientific_approval':False,'stable':False}
    body={'run_id':run_id,'cycle':record['cycle'],'revision_id':revision_id,'checks_sha256':candidate['checks_sha256'],'original':evidence}
    recovery_key=run_id+':host-check:'+sha(body)
    recovery={'id':recovery_key,'run_id':run_id,'body':body,'sha256':sha(body),'created_at':now()}
    run={**run,'status':'paused','stage':'architecture_tuning','error':None,'updated_at':now(),
        'state':{**run.get('state',{}),'host_check_recovery':recovery_key}}
    if check_budget:
        from .rapid_check_budget import STATE_KEY
        run['state'][STATE_KEY]=check_budget
    puts=[(rapid.COLLECTION,revision_id,record),('optimization_loops',f'{run_id}:{record["cycle"]}',loop),
        ('runs',run_id,run),(RECOVERIES,recovery_key,recovery)]
    if pending and pending_id!=revision_id:
        pending={**pending,'status':'superseded_before_dispatch','superseded_by_recovery':revision_id,
            'superseded_reason':'Frozen proposal referenced an older empty failure rather than the recovered CPU result', 'updated_at':now()}
        puts.append((rapid.COLLECTION,pending_id,pending))
    engine.store.batch(puts,deletes=cursors)
    from .rapid_review_plan import prepare as prepare_review
    plan=prepare_review(engine,run_id,revision_id,instruction)
    engine.store.event(run_id,'experiment.pathlib_check_recovered',
        '保留失败回执，恢复实际受检候选；仅修复 CPU 检查的运行时兼容性，重新独立审核。',
        {'revision_id':revision_id,'checks_sha256':candidate['checks_sha256'],'plan_sha256':plan['sha256'],
         'reason':reason,'runtime_unchanged':True,'assertions_unchanged':True,'training_executed':False})
    return audit
