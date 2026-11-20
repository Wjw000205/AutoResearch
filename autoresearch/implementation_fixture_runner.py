"""Run reviewed synthetic assets in an isolated implementation-check store."""
import asyncio
import csv
from datetime import datetime, timedelta
import hashlib
import io
import json
import math
from pathlib import Path
import re
import time
from types import SimpleNamespace

from . import code_bundle, dataset_row_identity as rows, datasets
from .deletion import _plain_path
from .delivery_files import identity, io_path
from .engine import ResearchBlocked, digest
from .hyperparameter_yaml import prepare_trial, RUNTIME_FILE, RECEIPT_FILE
from .implementation_documents import _write_immutable
from .implementation_fixture import OBSERVATION, RECEIPTS
from .lab import ExperimentRunner
from .storage import Store


def _ids(items):
    ids = [item['id'] for item in items]
    if (not 1 <= len(ids) <= 64 or len(ids)!=len(set(ids))
            or any(not isinstance(v,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',v) for v in ids)):
        raise ValueError('Fixture IDs must be unique bounded identifiers')
    return set(ids)


def validate(plan,files,obligations):
    if (set(plan)!={'version','entrypoint','assets','params','cases','comparisons'} or type(plan['version']) is not int or plan['version'] not in (1,2)
            or plan['entrypoint'] not in files or not plan['entrypoint'].startswith('tests/')
            or not plan['entrypoint'].endswith('.py') or plan['params'].get('phase')!='validation'
            or 'dataset_assets' in plan['params'] or not isinstance(plan['comparisons'],list)):
        raise ValueError('Invalid implementation fixture manifest or probe')
    assets,cases=_ids(plan['assets']),_ids(plan['cases'])
    for asset in plan['assets']:
        if (set(asset)!={'id','filename','rows','columns'} or not re.fullmatch(r'[A-Za-z0-9_-]+\.csv',asset['filename'])
                or type(asset['rows']) is not int or not 1<=asset['rows']<=4096
                or not 1<=len(asset['columns'])<=128):
            raise ValueError('Invalid bounded CSV fixture')
        names=set()
        for c in asset['columns']:
            if (set(c)!={'name','start','step'} or not isinstance(c['name'],str) or not 0<len(c['name'])<=256
                    or c['name'] in names or type(c['step']) is not int
                    or not (type(c['start']) is int or isinstance(c['start'],str))):
                raise ValueError('Invalid deterministic CSV column')
            names.add(c['name'])
            if isinstance(c['start'],str): datetime.fromisoformat(c['start'])
    successful=set()
    recipes={a['id']:a for a in plan['assets']}
    for c in plan['cases']:
        required={'id','obligation','asset','expect_exit','expect'}
        host_case=plan['version']==2 and 'integrity_edit' in c
        rejected=bool(c.get('expect_exit')) or host_case
        fields=set(c)-({'changed_inputs'} if c.get('expect_exit') else set())
        if plan['version']==2 and rejected:
            fields-= {'bindings','replay_from','source_edit','integrity_edit'}
        if (fields!=(required|{'failure'} if rejected else required) or c['asset'] not in assets
                or (c['expect_exit'] is not None if host_case else
                    type(c['expect_exit']) is not int or not 0<=c['expect_exit']<=255)
                or not isinstance(c['expect'],dict) or not c['expect'] or c['obligation'] not in obligations):
            raise ValueError('Invalid fixture case or missing expected observations')
        if rejected and (set(c['failure'])!={'error_type','message_contains'}
                or not all(isinstance(v,str) and v for v in c['failure'].values())):
            raise ValueError('A rejection needs its expected error type and message field')
        if not isinstance(c.get('changed_inputs',[]),list) or set(c.get('changed_inputs',[]))-{'csv','yaml'}:
            raise ValueError('Declare any staged CSV or YAML changed by a rejection case')
        ids=c.get('bindings',[c['asset']])
        if (not isinstance(ids,list) or len(ids)!=len(set(ids)) or not set(ids)<=assets or c['asset'] not in ids
                or ('replay_from' in c and c['replay_from'] not in successful)
                or host_case and (len(ids)!=1 or 'source_edit' in c or 'replay_from' in c)):
            raise ValueError('Invalid ordered fixture bindings, source mutation or prior successful case')
        for field in ('source_edit','integrity_edit'):
            if field in c: _check_edit(c[field],recipes[c['asset']])
        if not rejected: successful.add(c['id'])
    if (len(assets)!=2 or {c['obligation'] for c in plan['cases']}!=set(obligations) or not plan['comparisons']
            or not any(c['expect_exit'] for c in plan['cases'])):
        raise ValueError('Paired fixture verification must cover both assets, comparisons and every frozen obligation')
    by_id={c['id']:c for c in plan['cases']}
    for c in plan['comparisons']:
        if (set(c)!={'left','right','field','delta','result_field'} or {c['left'],c['right']}-cases
                or c['left']==c['right'] or not isinstance(c['field'],str)
                or not isinstance(c['result_field'],str) or not c['result_field']
                or c['left'] not in successful or c['right'] not in successful
                or by_id[c['left']]['asset']==by_id[c['right']]['asset']
                or by_id[c['left']]['obligation']!=by_id[c['right']]['obligation']
                or type(c['delta']) not in (int,float) or not math.isfinite(c['delta'])):
            raise ValueError('Invalid paired fixture assertion')


def _check_edit(edit,recipe):
    names=[c['name'] for c in recipe['columns']]
    forms={'drop_column':{'op','column'},'set_cell':{'op','column','row','value'},
           'copy_cell':{'op','column','row','from_row'}}
    if (not isinstance(edit,dict) or set(edit)!=forms.get(edit.get('op')) or edit['column'] not in names
            or any(type(edit[r]) is not int or not 0<=edit[r]<recipe['rows'] for r in ('row','from_row') if r in edit)
            or ('value' in edit and (not isinstance(edit['value'],str) or len(edit['value'])>256))):
        raise ValueError('Invalid bounded fixture CSV mutation')


def _edit_csv(raw,edit):
    records=list(csv.reader(io.StringIO(raw.decode('utf8'),newline=''),strict=True))
    col=records[0].index(edit['column'])
    if edit['op']=='drop_column':
        for record in records: del record[col]
    else:
        records[edit['row']+1][col]=(edit['value'] if edit['op']=='set_cell' else records[edit['from_row']+1][col])
    text=io.StringIO(newline='')
    csv.writer(text,lineterminator='\n').writerows(records)
    changed=text.getvalue().encode('utf8')
    if changed==raw: raise ValueError('The fixture mutation did not change the supplied bytes')
    return changed


def register(store,root,rid,recipe,scope,edit=None):
    """Register generated bytes with local fixture provenance and verify them at staging."""
    text=io.StringIO(newline='')
    writer=csv.writer(text,lineterminator='\n')
    columns=recipe['columns']
    writer.writerow([c['name'] for c in columns])
    starts=[datetime.fromisoformat(c['start']) if isinstance(c['start'],str) else c['start'] for c in columns]
    for i in range(recipe['rows']):
        writer.writerow([(v+timedelta(seconds=i*c['step'])).isoformat(sep=' ') if isinstance(v,datetime)
                         else v+i*c['step'] for c,v in zip(columns,starts)])
    raw=text.getvalue().encode('utf8')
    if len(raw)>4*1024**2: raise ValueError('Fixture exceeds its bounded size')
    if edit: raw=_edit_csv(raw,edit)
    key=digest([scope,recipe] if edit is None else [scope,recipe,edit])[:32]
    name=recipe['filename']
    data={'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}
    download={**data,'path':'original/'+name}
    item={**data,'path':'files/'+name,'trial_path':f'datasets/{key}/files/{name}'}
    source={'kind':'implementation_fixture','scope_sha256':scope,'recipe_sha256':digest(recipe)}
    if edit: source['source_edit']=edit
    manifest={'version':1,'origin':source,'download':download,'files':[item]}
    sha=datasets.digest(manifest)
    folder=root/'runs'/rid/'datasets'/key/sha
    for row in (download,item):
        path=folder/row['path']
        path.parent.mkdir(parents=True,exist_ok=True)
        _write_immutable(path,raw)
    _write_immutable(folder/'manifest.json',json.dumps(manifest,ensure_ascii=False).encode())
    aid=rid+':dataset:'+key
    store.put('dataset_assets',aid,{'id':aid,'asset_id':aid,'run_id':rid,'status':'fixture_registered',
        'manifest_sha256':sha,'manifest':manifest,'provenance':source})
    return {'asset_id':aid,'manifest_sha256':sha,'trial_path':item['trial_path'],'sha256':data['sha256']}


class FixtureStore(Store):
    """Dedicated store under a host-owned implementation fixture directory."""

    def check_job(self,rid,program,plan,params,trial_id,trial_name,planned_params,grant):
        scope=self.get('fixture_scope',rid,{})
        if (scope.get('grant_sha256')!=grant['sha256'] or program.get('delivery_id')!=rid
                or scope.get('plan')!=plan or grant['plan_sha256']!=digest(plan)
                or scope['document_sha256']!=program.get('implementation_document_sha256')
                or grant['definition_id']!=scope['spec_hash'] or not scope['review_handoffs']
                or trial_id!=trial_name):
            raise ResearchBlocked('隔离核验未绑定已审文档、独立意见和本次任务。')
        case=next((c for c in plan['cases'] if c['id']==trial_name),None)
        if not case: raise ResearchBlocked('隔离核验未获准执行此用例。')
        assets=scope['case_assets'][case['id']]
        expected=_case_params(plan,case,assets)
        entry=_entry(trial_name,grant['sha256'])
        if (digest(planned_params)!=digest(expected) or digest(params)!=digest(expected) or any(a not in grant['files'] for a in assets.values())
                or program['code_sha256']!=hashlib.sha256(entry.encode()).hexdigest()):
            raise ResearchBlocked('隔离核验的实际参数、资产或测试入口与批准用例不一致。')


def _params(template,path):
    if template=={'$fixture':'trial_path'}: return path
    if isinstance(template,dict): return {k:_params(v,path) for k,v in template.items()}
    if isinstance(template,list): return [_params(x,path) for x in template]
    return template


def _entry(case,pin):
    # The host uses extended Windows paths for archival I/O. Give the probe
    # the same canonical working directory spelling as the production entry.
    return ("import os,sys\nfrom pathlib import Path\nos.chdir(Path('params.json').resolve(strict=True).parent)\n"
           "sys.path.insert(0,str(Path(__file__).resolve().parent))\n"
           "from fixture_probe import run_case\n"
           f"run_case({case!r},{pin!r},sys.argv[1])\n")


def _case_params(plan,case,assets):
    params=_params(plan['params'],assets[case['asset']]['trial_path'])
    params['dataset_assets']=[{k:assets[i][k] for k in ('asset_id','manifest_sha256')}
                              for i in case.get('bindings',[case['asset']])]
    return params


def _bundle(root,original,probe,case,pin,controls=None):
    folder=root/'bundles'/case
    entry=_entry(case,pin)
    paths={r['path']:io_path(Path(original['root'])/r['path']).read_bytes()
           for r in original['files'] if r['path']!='experiment.py'}
    if any(hashlib.sha256(paths[r['path']]).hexdigest()!=r['sha256']
           for r in original['files'] if r['path'] in paths):
        raise ValueError('Candidate runtime module changed during fixture staging')
    if 'fixture_probe.py' in paths: raise ValueError('Reserved fixture probe name is already in the runtime bundle')
    paths.update({'fixture_probe.py':probe.encode(),'experiment.py':entry.encode()})
    if controls:
        if 'fixture-controls.json' in paths: raise ValueError('Reserved fixture controls name')
        paths['fixture-controls.json']=json.dumps(controls,ensure_ascii=False).encode()
    files=[]
    for name,raw in sorted(paths.items()):
        target=folder/name
        target.parent.mkdir(parents=True,exist_ok=True)
        _write_immutable(target,raw)
        files.append({'path':name,**identity(target)})
    bundle={'version':1,'root':str(folder),'files':files,'sha256':code_bundle.digest(files)}
    return entry,bundle


def _delta(left,right,expected):
    if isinstance(left,list):
        return (isinstance(right,list) and bool(left) and len(left)==len(right)
                and all(_delta(a,b,expected) for a,b in zip(left,right)))
    return type(left) in (int,float) and type(right) in (int,float) and math.isfinite(right-left) and right-left==expected


def _read_report(path):
    if not path.is_file() or path.stat().st_size>4*1024**2: return {}
    try:
        value=json.loads(path.read_bytes())
    except (ValueError,UnicodeError):
        return {}
    return value if isinstance(value,dict) else {}


def _success_line(output):
    for line in output.splitlines():
        try: value=json.loads(line)
        except ValueError: continue
        if isinstance(value,dict) and isinstance(value.get('metrics'),dict) and value['metrics']:
            return True
    return False


def _integrity_case(store,root,rid,params,case,asset,original,registry,limits):
    """Exercise the host's real pre-copy integrity check on a private registry copy."""
    started=time.monotonic()
    scratch=root/'integrity'/case['id']
    target=scratch/'trial'
    target.mkdir(parents=True,exist_ok=True)
    manifest=store.get('dataset_assets',asset['asset_id'])['manifest']
    raw=original.read_bytes()
    changed=_edit_csv(raw,case['integrity_edit'])
    if len(raw)!=len(changed):
        raise ValueError('An integrity mutation must preserve the byte length')
    directory=scratch/registry.relative_to(root)
    archives=[]
    def retain(path,data):
        path.parent.mkdir(parents=True,exist_ok=True)
        _write_immutable(path,data)
        archives.append({'path':path.relative_to(root).as_posix(),**identity(path)})
    retain(directory/'manifest.json',(registry/'manifest.json').read_bytes())
    retain(directory/manifest['download']['path'],raw)
    retain(directory/manifest['files'][0]['path'],changed)
    seen={'process_started':False,'exit_code':None,'success_output':False,'failure_archived':False,
          'model_calls':0,'optimizer_calls':0,'window_calls':0,'value_parse_calls':0,'record_requests':[],
          'expected_sha256':asset['sha256'],'actual_sha256':hashlib.sha256(changed).hexdigest(),
          'expected_bytes':len(raw),'actual_bytes':len(changed)}
    refused=False
    try:
        datasets.stage_assets(store,scratch,rid,params,target,limits['disk_mb']*1024**2)
    except ValueError as exc:
        seen.update(stage='asset_integrity',field='sha256',error_type=type(exc).__name__,message=str(exc))
        refused=(str(exc)=='Dataset file is missing or its size/SHA256 changed'
                 and seen['error_type']==case['failure']['error_type'] and case['failure']['message_contains'] in str(exc))
        seen['failure_archived']=True
        retain(scratch/'artifacts/failure.json',json.dumps(seen,ensure_ascii=False).encode())
    proof={'before':hashlib.sha256(raw).hexdigest(),'after':hashlib.sha256(changed).hexdigest(),
           'bytes':len(raw),'edit':case['integrity_edit']}
    return {'id':case['id'],'boundary':'host_before_launch','exit_code':None,'expected_exit':None,
            'status':'rejected_before_launch' if refused else 'unexpected_staging_result','duration_seconds':time.monotonic()-started,
            'passed':refused and all(k in seen and digest(seen[k])==digest(v) for k,v in case['expect'].items()),
            'asset':asset,'input_mutations':proof,'observed':seen},archives


async def execute(engine,run_id,state,attempt,packet):
    from .codex_workflow import _verify_snapshot
    from .yaml_migration import inspect_contract
    key=packet['sha256']
    from .implementation_fixture import read_receipt,runtime_binding
    archive_root=engine.run_dir(run_id)/'implementation-fixtures'/key[:20]
    root=io_path(archive_root)
    old=read_receipt(engine,run_id,packet)
    if old: return old
    if packet['binding']['runtime']!=runtime_binding():
        raise ResearchBlocked('待执行的隔离夹具已绑定其他宿主检查器版本，须重新审核后核验。')
    engine.check_active(run_id)
    config=engine.store.get('runs',run_id)['config']
    if not config.get('allow_local_execution') or config.get('execution_mode')!='local':
        raise ResearchBlocked('本机夹具检查需要已启用的本地执行环境。')
    _verify_snapshot(engine,run_id,attempt['snapshot'])
    plan=packet['manifest']
    directory=Path(attempt['snapshot']['workspace'])
    original=attempt['acceptance']['runtime_bundle']
    code_bundle.validate(original,io_path(directory/'experiment.py').read_bytes().decode('utf8'))
    if identity(directory/plan['entrypoint'])['sha256']!=packet['binding']['probe_sha256']:
        raise ResearchBlocked('夹具入口与批准的探针不一致。')
    resources=state['common']['design']['resource_plan']
    cpu={k:resources[k] for k in ('ram_gb','disk_gb','cpu_cores','cpu_seconds','required_packages','timeout_seconds') if k in resources}
    cpu.update(gpu_required=False,estimated_vram_gb=0)
    assessment=await engine._trial_resources(run_id,cpu)
    if not assessment['approved']: raise ResearchBlocked('夹具 CPU 资源不可用：'+str(assessment['reasons']))
    limits={**assessment['limits'],'execution_mode':'local','gpu_allowed':False,'required_packages':cpu.get('required_packages',[])}
    budget=min(float(limits['timeout_seconds']),float(config['experiment_timeout']),float(limits['cpu_seconds'])/limits['cpu_cores'])
    root.mkdir(parents=True,exist_ok=True)
    store=FixtureStore(archive_root/'fixture.sqlite3')
    rid='fixture'+key[:20]
    isolated=SimpleNamespace(store=store,cycle=lambda _:1,experiment_plan=lambda _:plan)
    runner=ExperimentRunner(store,root)
    assets={a['id']:register(store,root,rid,a,key) for a in plan['assets']}
    recipes={a['id']:a for a in plan['assets']}
    case_assets={}
    files=list(assets.values())
    for case in plan['cases']:
        primary=case['asset']
        selected={i:assets[i] for i in case.get('bindings',[primary])}
        if 'source_edit' in case:
            derived=register(store,root,rid,recipes[primary],key,case['source_edit'])
            selected[primary]=derived
            if derived not in files: files.append(derived)
        case_assets[case['id']]=selected
    votes=attempt['fixture_admission_votes']
    if (len(votes)!=2 or {v['role'] for v in votes}!={'director','scout'}
            or any(v['result']['fixture_check']['approve'] is not True or
                   v['result']['fixture_check']['sha256']!=key for v in votes)):
        raise ResearchBlocked('隔离核验缺少本次探针的完整独立准入。')
    handoffs=[v['handoff_id'] for v in votes]
    grant={'run_id':rid,'cycle':1,'workflow_id':rid,'definition_id':packet['grant']['spec_hash'],
        'proposal_sha256':None,'review_handoffs':handoffs,'plan_sha256':digest(plan),'format':rows.FORMAT,
        'phase':'validation','purpose':'split_membership_only','files':files}
    grant['sha256']=digest(grant)
    store.put(rows.COLLECTION,rid+':1',grant)
    store.put('fixture_scope',rid,{'grant_sha256':grant['sha256'],'case_assets':case_assets,'plan':plan,
        'document_sha256':packet['binding']['document_sha256'],'review_handoffs':handoffs,'spec_hash':grant['definition_id']})
    contract=inspect_contract(original)
    results=[]; observed={}; archives=[]
    prior_receipts={}
    for case in plan['cases']:
        engine.check_active(run_id)
        remaining=budget-sum(r['duration_seconds'] for r in results)
        if remaining<=0: break
        cid=case['id']; selected=case_assets[cid]; asset=selected[case['asset']]
        params=_case_params(plan,case,selected)
        controls={}
        if 'source_edit' in case:
            controls['source_edit']={'target':case['source_edit'],'before':assets[case['asset']]['sha256'],'after':asset['sha256']}
        if 'replay_from' in case: controls['replay']=prior_receipts[case['replay_from']]
        entry,bundle=_bundle(root,original,packet['probe'],cid,grant['sha256'],controls)
        code_sha=hashlib.sha256(entry.encode()).hexdigest()
        program={'code':entry,'revision':attempt['index'],'delivery_id':rid,'code_sha256':code_sha,
            'implementation_document_sha256':packet['binding']['document_sha256'],
            'hyperparameter_yaml_protocol':1,'hyperparameter_contract':contract}
        yaml=prepare_trial(isolated,rid,program,params,use_pending=False)
        baseline=root/'originals'/cid
        baseline.mkdir(parents=True,exist_ok=True)
        _write_immutable(baseline/RUNTIME_FILE,yaml['yaml'].encode())
        asset_key=asset['asset_id'].rsplit(':',1)[-1]
        registry=root/'runs'/rid/'datasets'/asset_key/asset['manifest_sha256']
        stored_asset=store.get('dataset_assets',asset['asset_id'])['manifest']
        original_csv=registry/stored_asset['download']['path']
        for f,expected_sha in ((original_csv,asset['sha256']),(registry/'manifest.json',None),(baseline/RUNTIME_FILE,yaml['yaml_sha256'])):
            stamped=identity(f)
            if expected_sha and stamped['sha256']!=expected_sha: raise ResearchBlocked('隔离输入原件发生变化。')
            archives.append({'path':f.relative_to(root).as_posix(),**stamped})
        admitted=rows.admission(isolated,rid,program,plan,params,cid,trial_name=cid,planned_params=params,yaml_contract=yaml)
        if not admitted: raise ResearchBlocked('隔离数据行用途未获得准入。')
        if 'integrity_edit' in case:
            outcome,evidence=_integrity_case(store,root,rid,params,case,asset,original_csv,registry,limits)
            fields=packet['grant']['catalog']['obligations'][case['obligation']]['observation_fields']
            outcome['passed'] &= set(fields)<=set(outcome['observed'])
            results.append(outcome)
            archives.extend(evidence)
            if not outcome['passed']: break
            continue
        binding={'research_cycle':1,'code_revision':attempt['index'],'code_sha256':code_sha,'experiment_plan_sha256':digest(plan)}
        result=store.get('experiments',rid+':'+cid)
        if result and (result.get('params')!=params or result.get('code_sha256')!=code_sha
                       or result.get('experiment_plan_sha256')!=digest(plan) or result.get('status') not in ('completed','failed')):
            raise ResearchBlocked('原隔离夹具试次身份或终态未能确认。')
        if not result:
            result=await runner.run(rid,entry,params,cid,{**limits,'timeout_seconds':remaining,
                'cpu_seconds':min(limits['cpu_seconds'],remaining*limits['cpu_cores']),
                'code_bundle':bundle,'experiment_binding':binding,'hyperparameter_yaml':yaml},
                engine.cancellations[run_id],row_identity=admitted)
        path=Path(result['artifact_path']); _plain_path(path,root)
        report=path/OBSERVATION; _plain_path(report,root)
        value=_read_report(report)
        seen=value.get('observed',{})
        status_ok=result['status']=='completed'
        if case['expect_exit']:
            failure_path=path/'artifacts/failure.json'
            _plain_path(failure_path,root)
            failure=_read_report(failure_path)
            mutation=value.get('mutation',{})
            successful_output=_success_line(result.get('stdout') or '')
            actual_fields={'exit_code':result.get('exit_code'),'success_output':successful_output,'failure_archived':bool(failure),
                'error_type':failure.get('error_type'),'message':failure.get('message')}
            for field in ('stage','field'):
                actual_fields[field]=failure.get(field,(failure.get('details') or {}).get(field)) if isinstance(failure.get('details',{}),dict) else failure.get(field)
            status_ok=(result['status']=='failed' and not result.get('metrics')
                and not successful_output and failure.get('error_type')==case['failure']['error_type']
                and case['failure']['message_contains'] in str(failure.get('message',''))
                and isinstance(seen,dict) and all(v is not None and digest(v)==digest(seen[k]) for k,v in actual_fields.items() if k in seen)
                and isinstance(mutation,dict) and bool(mutation.get('target'))
                and 'before' in mutation and 'after' in mutation and digest(mutation['before'])!=digest(mutation['after']))
            if plan['version']==2 and len(selected)>1:
                target=mutation.get('target') if isinstance(mutation,dict) else None
                order_fields=('dataset_assets','/environment.json/dataset_assets','/params.json/dataset_assets')
                if target in order_fields or case['expect'].get('field') in order_fields:
                    before,after=(mutation.get(k) for k in ('before','after')) if isinstance(mutation,dict) else (None,None)
                    status_ok &= (isinstance(before,list) and isinstance(after,list) and before!=after
                                  and before in (params['dataset_assets'],result.get('dataset_assets'))
                                  and sorted(map(digest,before))==sorted(map(digest,after)))
        computed={p['result_field'] for p in plan['comparisons'] if cid in (p['left'],p['right'])}
        fields=set(packet['grant']['catalog']['obligations'][case['obligation']]['observation_fields'])
        yaml_name=yaml.get('config_file',RUNTIME_FILE)
        required={yaml_name:yaml['yaml_sha256'],**{a['trial_path']:a['sha256'] for a in selected.values()}}
        inputs_ok=True
        mutations={}
        changed_files={}
        for name,sha in required.items():
            original_path=path/name
            _plain_path(original_path,root)
            after_sha=identity(original_path)['sha256'] if original_path.is_file() else None
            if after_sha!=sha:
                label='yaml' if name==yaml_name else 'csv'
                mutations[label]={'before':sha,'after':after_sha}
                changed_files[name]=mutations[label]
                if label not in case.get('changed_inputs',[]): inputs_ok=False
                if plan['version']==2 and label=='csv' and name!=asset['trial_path']: inputs_ok=False
        inputs_ok = inputs_ok and set(mutations)==set(case.get('changed_inputs',[]))
        ok=(inputs_ok and status_ok and result.get('exit_code')==case['expect_exit']
            and value.get('case_id')==cid and isinstance(seen,dict)
            and fields<=set(seen)|computed
            and all(k in seen and digest(seen[k])==digest(v) for k,v in case['expect'].items()))
        if ok and any(c.get('replay_from')==cid for c in plan['cases']):
            issued=rows.staging_metadata(store,rid,cid,entry,params,path,result['dataset_assets'],admitted['hyperparameter_yaml'],admitted)[rows.FIELD]
            if issued!=_read_report(path/'environment.json').get(rows.FIELD): raise ResearchBlocked('先前试次的行身份回执与宿主签发内容不一致。')
            prior_receipts[cid]={'case_id':cid,'row_identity':issued,'sha256':digest(issued)}
        for name in ('params.json','environment.json','fixture-controls.json',RECEIPT_FILE,*required,'stdout.txt','stderr.txt',OBSERVATION,'artifacts/failure.json'):
            f=path/name; _plain_path(f,root)
            if f.is_file(): archives.append({'path':f.relative_to(root).as_posix(),**identity(f)})
        observed[cid]=seen
        results.append({'id':cid,'passed':ok,'exit_code':result.get('exit_code'),'expected_exit':case['expect_exit'],
            'status':result['status'],'duration_seconds':result['duration_seconds'],'error':result.get('error'),
            'driver_sha256':code_sha,'params_sha256':digest(params),'asset':asset,'yaml_sha256':yaml['yaml_sha256'],
            'input_mutations':mutations})
        if plan['version']==2: results[-1]['changed_files']=changed_files
        if not ok:
            results[-1]['diagnostic']={'inputs_match':inputs_ok,'status_matches':status_ok,'missing_observations':
                sorted(fields-set(seen)-computed) if isinstance(seen,dict) else sorted(fields),
                'mismatched_expectations':[k for k,v in case['expect'].items()
                    if not isinstance(seen,dict) or k not in seen or digest(seen[k])!=digest(v)]}
        if not ok: break
    pairs=[{**pair,'passed':_delta(observed.get(pair['left'],{}).get(pair['field']),
        observed.get(pair['right'],{}).get(pair['field']),pair['delta'])} for pair in plan['comparisons']]
    record={'run_id':run_id,'binding':packet['binding'],'isolated_run_id':rid,'fixture_grant':grant['sha256'],
        'provenance':'implementation_fixture_verification','scientific_approval':False,'limits':{
            k:limits[k] for k in ('python_executable','cpu_cores','memory_mb','disk_mb','gpu_allowed')},
        'wall_budget':budget,'cases':results,'comparisons':pairs,'archives':archives,'not_run':[
            c['id'] for c in plan['cases'][len(results):]],'passed':len(results)==len(plan['cases'])
            and all(r['passed'] for r in [*results,*pairs])}
    record['sha256']=digest(record)
    engine.store.batch(puts=[(RECEIPTS,key,record)],expected=[(RECEIPTS,key,None)])
    return record
