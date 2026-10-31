"""Reviewed full-acceptance time budgets, separate from 60-second smoke checks."""
import copy
import hashlib
import json
from pathlib import Path

from .experiment_workflow import sha
from .delivery_files import io_path

STATE_KEY='full_acceptance_check_budget'
PREFIX='# Host CPU-check resource contract SHA256: '
CAPS={'cpu_cores':1,'memory_mb':1024,'disk_mb':256}


def review_schema(candidate,schema):
    result=copy.deepcopy(schema)
    value=checked(candidate)
    if value:
        result['properties']['check_budget_sha256']={'const':sha(value)}
        if 'check_budget_sha256' not in result['required']:result['required'].append('check_budget_sha256')
    return result


def policy(seconds,instruction):
    if type(seconds) is not int or not 1<=seconds<=86400 or not isinstance(instruction,str) or not instruction.strip():
        raise ValueError('A finite explicitly requested check budget is required')
    return {'version':1,'kind':'full_delivery_acceptance','timeout_seconds':seconds,
        'cpu_seconds':seconds,**CAPS,'gpu_allowed':False,'authorization':instruction}


def checked(candidate):
    value=candidate.get('check_budget')
    if value is None:return None
    expected=policy(value.get('timeout_seconds'),value.get('authorization',''))
    if value!=expected:raise ValueError('CPU check budget contract changed')
    marker=PREFIX+sha(value)+'\n'
    from .check_instrumentation import split_future_prefix
    if not split_future_prefix(candidate['check_code'])[1].startswith(marker):
        raise ValueError('CPU check budget is not bound to the launcher hash')
    raw=io_path(Path(candidate['check_bundle']['root'])/'host_check_budget.json').read_bytes()
    if json.loads(raw)!=value:raise ValueError('CPU check budget differs from its reviewed file')
    return value


def caps(candidate,run):
    value=checked(candidate)
    seconds=min((value or {}).get('timeout_seconds',60),run['config']['experiment_timeout'])
    return {'timeout_seconds':seconds,'cpu_seconds':seconds,**CAPS}


def resource_request(resource,candidate,run):
    result=copy.deepcopy(resource)
    if checked(candidate):
        budget=caps(candidate,run)
        times={k:budget[k] for k in ('timeout_seconds','cpu_seconds')}
        result.update(times)
        if isinstance(result.get('resources'),dict):result['resources'].update(times)
    return result


def attach(engine,run_id,candidate,value):
    from .rapid_experiment_revision import _write_files
    from .experiment_analysis import freeze_sources
    from .check_instrumentation import split_future_prefix
    c=copy.deepcopy(candidate)
    old=checked(c)
    future,body=split_future_prefix(c['check_code'])
    if old:
        marker=PREFIX+sha(old)+'\n'
        if not body.startswith(marker):raise ValueError('Unexpected check budget launcher position')
        body=body[len(marker):]
    c['check_code']=future+PREFIX+sha(value)+'\n'+body
    c['checks_sha256']=hashlib.sha256(c['check_code'].encode()).hexdigest();c['check_budget']=copy.deepcopy(value)
    raw=(json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode()
    root=engine.run_dir(run_id)/'host-check-budgets'/c['checks_sha256'][:20]
    files={r['path']:io_path(Path(c['check_bundle']['root'])/r['path']).read_bytes() for r in c['check_bundle']['files']}
    files.update({'experiment.py':c['check_code'].encode(),'host_check_budget.json':raw})
    c['check_bundle']=_write_files(root/'check',files)
    review={r['path']:io_path(Path(c['review_bundle']['root'])/r['path']).read_bytes() for r in c['review_bundle']['files']}
    review['host_check_budget.json']=raw;c['review_bundle']=_write_files(root/'review',review)
    c['code_sources']=freeze_sources(engine,run_id,c['program'],c['review_bundle'],delivery_support=True)
    c['changed_ranges']=[r for r in c['changed_ranges'] if r['path']!='host_check_budget.json']
    c['changed_ranges'].append({'path':'host_check_budget.json','start':0,'end':len(raw.decode()),
        'sha256':hashlib.sha256(raw).hexdigest(),'scope':'check_resource_authorization'})
    return c


def future_policy(engine,run_id,record):
    value=(engine.store.get('runs',run_id) or {}).get('state',{}).get(STATE_KEY)
    if value and (record.get('yaml_migration') or record.get('decision',{}).get('architecture_review')) and record.get('native_delivery_protocol')==2:
        if value != policy(value.get('timeout_seconds'),value.get('authorization','')):
            raise ValueError('Future full acceptance budget changed')
        return copy.deepcopy(value)
    return None


def apply_future(engine,run_id,record,candidate):
    value=future_policy(engine,run_id,record)
    if value:
        return attach(engine,run_id,candidate,value)
    return candidate


def verify_timeout(engine,run_id,record,seconds):
    from .rapid_experiment_revision import _candidate_unchanged
    trial=engine.store.get('experiments',run_id+':'+(record.get('check') or {}).get('id','')) or {}
    run=engine.store.get('runs',run_id) or {}
    if (type(seconds) is not int or record.get('run_id')!=run_id or trial.get('run_id')!=run_id
            or record.get('status')!='check_failed' or trial.get('status')!='timeout' or not trial.get('finished_at')
            or sha(trial)!=(record.get('check') or {}).get('receipt_sha256')
            or trial.get('params',{}).get('phase')!='code_check'
            or trial.get('code_sha256')!=record['candidate']['checks_sha256']
            or trial.get('limits',{}).get('code_bundle')!=record['candidate']['check_bundle']
            or not record.get('yaml_migration') or record.get('native_delivery_protocol')!=2
            or not trial['limits']['timeout_seconds']<seconds<=run['config']['experiment_timeout']):
        raise ValueError('Budget recovery requires the exact timed-out full acceptance check and task cap')
    _candidate_unchanged(record)
    ref=record.get('post_exit_verification') or {};saved=engine.store.get('rapid_post_exit_verifications',ref.get('id','')) or {}
    binding=saved.get('binding',{})
    if (sha(saved)!=ref.get('sha256') or saved.get('run_id')!=run_id or not saved.get('result',{}).get('post_exit_verified')
            or binding.get('runner_receipt_sha256')!=sha(trial) or binding.get('revision_id')!=record['id']
            or binding.get('trial_id')!=trial['id'] or binding.get('checks_sha256')!=record['candidate']['checks_sha256']):
        raise ValueError('Timed-out evidence must be verified after actual process exit')
    return trial,'full_acceptance_budget'
