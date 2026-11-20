"""Value-free CSV record identity for explicitly authorized validation jobs."""
import csv
import hashlib
from pathlib import Path

from .engine import ResearchBlocked, digest

COLLECTION = 'dataset_row_identity_authorizations'
FORMAT = 'csv-source-records-v1'
FIELD = 'dataset_row_identity'


def handoff_sources():
    """Publish the actual host functions and the lines connecting them to the child."""
    import inspect
    from .engine import Engine
    from .lab import ExperimentRunner
    refs = []
    for function, anchors in (
        (Engine._trial, ('code_bundle = validate_program','code, revision =','trial_id =','row_identity =',
                       'self.runner.run(', "'code_sha256': hashlib.sha256(code.encode")),
        (admission, ()), (staging_metadata, ()),
        (ExperimentRunner.run, ('stage_trial(path','transport_io_call(stage_assets',
             '"code_sha256": hashlib.sha256(code.encode()).hexdigest()',
             'environment.update(await transport_io_call(dataset_row_identity.staging_metadata',
             '(path / "environment.json").write_text','target=/workspace','cwd=str(path)'))):
        lines, first = inspect.getsourcelines(function)
        selected = set(range(len(lines))) if not anchors else {
            n for i,line in enumerate(lines) if any(a in line for a in anchors)
            for n in range(max(0,i-1),min(len(lines),i+3))}
        from itertools import groupby
        for _,group in groupby(enumerate(sorted(selected)),key=lambda item:item[1]-item[0]):
            indices = [n for _,n in group]
            refs.append({'source':function.__module__.replace('.','/')+'.py','function':function.__qualname__,
                         'first_line':first+indices[0],'text':''.join(lines[indices[0]:indices[-1]+1])})
    return refs


def interface():
    return {'format':FORMAT,'field':'environment.json.'+FIELD,'phase':'validation',
        'purpose':'split_membership_only','file_fields':['asset_id','manifest_sha256','trial_path','sha256',
            'bytes','staged_stat','row_count','first_row','order'],
        'csv':'UTF-8 (optional BOM), comma delimiter, CSV double quotes, one nonempty header record. '
              'Only the header is inspected for columns. Count logical data records for split membership. '
              'Original zero-based record order is preserved, without filtering or sorting.',
        'invocation_fields':['run_id','cycle','workflow_id','plan_sha256','trial_id','trial_name','params_sha256',
            'code_sha256','phase','purpose','authorization_sha256','hyperparameter_yaml'],
        'file_values':{'first_row':0,'order':'source_record_order','row_count':'positive integer'},
        'yaml_receipt':{'source':'autoresearch.hyperparameter_yaml.stage_trial / trial_receipt',
            'when_disabled':'row receipt hyperparameter_yaml=null; no training YAML entry in environment',
            'required_when_enabled':{'revision_id':'string: admitted YAML version ID','profile':'string: actual variant',
                'yaml_sha256':'hex SHA256 of exact UTF-8 YAML bytes','params_sha256':'hex SHA256 of exact invocation params',
                'effective_profile_sha256':'hex SHA256 of compact sorted UTF-8 JSON of selected YAML profile'},
            'optional':{'configuration_scope':'string, present as "experiment" only for the experiment configuration scope'}},
        'binding_sources':{'trial_id':'environment.json.trial_id (actual host trial)',
            'trial_name':'environment.json.trial_name (approved job name bound by the controller)',
            'code_sha256':'environment.json.code_sha256','params_sha256':'environment.json.params_sha256',
            'authorization_sha256':'environment.json.dataset_row_identity_grant.sha256',
            'scope':'run_id, cycle, workflow_id, plan_sha256, phase, purpose and selected file bindings come from dataset_row_identity_grant',
            'hyperparameter_yaml':'Equal to environment.json.hyperparameter_yaml, or null when YAML is absent. '
                'Compare revision_id, yaml_sha256, profile, params_sha256, effective_profile_sha256 and any configuration_scope. '
                'The host checks the actual YAML bytes, profile and exact params before launch.',
            'files':'Match the corresponding environment.json.dataset_assets file, including trial_path, raw SHA, bytes and staged_stat.'},
        'host_handoff':[
            'autoresearch.engine.Engine._trial receives the controller job name and frozen parameters. It validates the approved program, '
            'assigns the trial ID from research cycle, code revision and job name, and binds SHA256 of the exact UTF-8 experiment.py '
            'source sent to the runner. These values originate outside the dataset receipt.',
            'autoresearch.dataset_row_identity.admission matches that job name and pre-transport parameters to the frozen inventory '
            'or admitted search job, and checks the approved program document, plan and grant. It passes the admission directly '
            'to ExperimentRunner.run for this invocation.',
            'autoresearch.lab.ExperimentRunner.run stages YAML via hyperparameter_yaml.stage_trial and the files via datasets.stage_assets. '
            'Before starting the child it calls dataset_row_identity.staging_metadata with the actual trial ID, code, parameters, '
            'staged files and YAML receipt. This function validates those inputs against the admission and stored grant.',
            'ExperimentRunner.run updates the current environment with exactly that returned mapping, writes environment.json under '
            'the trial root and launches experiment.py with that working directory. The delivered adapter must read '
            'Path.cwd()/environment.json once, pass that object to its data loader and check the published field bindings '
            'before parsing authorized values. Code review and execution checks verify this adapter behavior.'],
        'use':'Match the receipt to the staged asset entry, actual params, YAML receipt and phase before deriving '
              'split IDs by the approved task rules. It contains no targets, split assignment or scores.'}


def current(store,run_id,cycle):
    row = store.get(COLLECTION,f'{run_id}:{cycle}')
    if row is None:
        return None
    if (row.get('run_id')!=run_id or row.get('cycle')!=cycle or row.get('format')!=FORMAT
            or row.get('phase')!='validation' or row.get('purpose')!='split_membership_only'
            or row.get('sha256')!=digest({k:v for k,v in row.items() if k!='sha256'})):
        raise ResearchBlocked('数据行身份用途授权与当前任务不一致。')
    return row


def workflow_grant(engine, run_id, state, grant):
    """Keep a repair on its original grant through verified deliveries of the same plan."""
    from .codex_workflow import validate_program
    if not grant:
        return None
    seen = set()
    ancestors = []
    while state['id'] != grant['workflow_id']:
        if state.get('kind') != 'runtime_repair':
            return None
        if state['id'] in seen:
            raise ResearchBlocked('运行修复的实现来源循环引用。')
        seen.add(state['id'])
        parent = state['repair']['program']
        if not parent.get('delivery_id') and parent.get('implementation_backend') != 'local_codex':
            return None
        source = engine.store.get('codex_workflows',parent.get('delivery_id',''),{})
        if (source.get('run_id')!=run_id or source.get('cycle')!=grant['cycle']
                or source.get('status')!='completed' or not source.get('result')
                or any(parent.get(k)!=v for k,v in source['result'].items())):
            raise ResearchBlocked('无法核对运行修复所继承的已批准交付。')
        ancestors.append((source, state['common']['experiment_plan']))
        state = source
    if digest(state['common']['experiment_plan']) != grant['plan_sha256']:
        raise ResearchBlocked('行成员用途授权未覆盖当前实验计划。')
    for source, plan in ancestors:
        if digest(plan)!=grant['plan_sha256']:
            raise ResearchBlocked('运行修复的冻结计划与行身份授权不一致。')
        parent = source['result']
        validate_program(engine,run_id,parent,source_cycle=grant['cycle'],source_only=True)
        document = next((r for r in source['specifications'] if r.get('approved')
                         and r['spec_hash']==parent['spec_hash']),{})
        handoff = engine.store.get('handoff_inputs',document.get('authoring_handoff_id',''),{})
        materials = handoff.get('context',{}).get('implementation_work',{}).get('materials',{})
        if materials.get('validation_row_identity',{}).get('authorization')!=grant:
            raise ResearchBlocked('运行修复的原批准文档没有绑定本次行成员用途。')
    return grant


def authorize(engine,run_id,proposal_sha256,selected_files):
    from . import host_asset_definition_recovery as assets, implementation_cycle_transition as transition
    from .implementation_cycle import prior_votes
    from .datasets import checked_manifest
    run = engine.store.get('runs',run_id)
    if run['status'] not in ('paused','blocked') or run['stage']!='implementation':
        raise ResearchBlocked('限定用途授权须绑定当前停止的实现任务。')
    cycle = engine.cycle(run_id)
    workflows = [w for w in engine.store.list('codex_workflows',run_id) if w.get('kind')=='implementation'
        and w.get('cycle')==cycle and w.get('implementation_generation')==run.get('state',{}).get('implementation_generation')]
    if len(workflows)!=1:
        raise ResearchBlocked('无法唯一定位当前实现工作流。')
    state, = workflows
    scope = transition.source(engine,run_id,state)
    plan = state['common']['experiment_plan']
    if scope:
        prior = next(r for r in state['specifications'] if r['index']==scope['previous_index'])
        evidence = assets.author_evidence(engine,run_id,state,prior)
        if not proposal_sha256 or not evidence or evidence.get('operator_review_proposal',{}).get('sha256')!=proposal_sha256:
            raise ResearchBlocked('用途授权没有绑定原送审方案。')
        votes = prior_votes(engine,run_id,prior)
        if not all(v.get(assets.ASSESSMENT,{}).get('decision')=='accept'
                   and v[assets.ASSESSMENT]['proposal_sha256']==proposal_sha256 for v in votes):
            raise ResearchBlocked('该候选接口尚未获得同版本的独立可行性意见。')
        bindings = {(a['asset_id'],a['manifest_sha256']) for a in evidence['registered_plan_assets']}
    else:
        from .implementation_cycle import active
        from .python_training import jobs
        from .datasets import bindings as asset_bindings
        if proposal_sha256 is not None or not active(state):
            raise ResearchBlocked('用途授权须进入对应的实现文档和独立审核。')
        bindings = {(a['asset_id'],a['manifest_sha256']) for stage in ('experiments','tuning')
                    for _,params in jobs(plan,stage) for a in asset_bindings(params)}
    files = []
    for asset_id,sha in sorted(bindings):
        manifest = checked_manifest(engine.store,run_id,asset_id,sha)['manifest']
        if manifest['asset_id']!=asset_id:
            raise ResearchBlocked('获得授权的资产身份不符。')
        for file in manifest['files']:
            ref = {'asset_id':asset_id,'manifest_sha256':sha,'trial_path':file['trial_path'],'sha256':file['sha256']}
            if ref in selected_files:
                files.append(ref)
    if not files or len(files)!=len(selected_files) or any(f not in selected_files for f in files):
        raise ResearchBlocked('只能对已送审计划中明确选择的真实文件授予行身份用途。')
    body = {'run_id':run_id,'cycle':cycle,'workflow_id':state['id'],'definition_id':scope['definition_id'] if scope else None,
        'proposal_sha256':proposal_sha256,'review_handoffs':scope['original_reviews'] if scope else [],'plan_sha256':digest(plan),
        'format':FORMAT,'phase':'validation','purpose':'split_membership_only','files':files}
    row = {**body,'sha256':digest(body)}
    old = current(engine.store,run_id,cycle)
    if old is not None:
        if old!=row:
            raise ResearchBlocked('已有不同范围的数据行用途授权。')
        return old
    engine.store.batch(puts=[(COLLECTION,f'{run_id}:{cycle}',row)],expected=[
        ('runs',run_id,run),('codex_workflows',state['id'],state),(COLLECTION,f'{run_id}:{cycle}',None)])
    return row


def admission(engine,run_id,program,plan,params,trial_id,*,trial_name,planned_params,transport=None,yaml_contract=None):
    if params.get('phase')!='validation' or not params.get('dataset_assets'):
        return None
    grant = current(engine.store,run_id,engine.cycle(run_id))
    if not grant:
        return None
    if not any({'asset_id':f['asset_id'],'manifest_sha256':f['manifest_sha256']} in params['dataset_assets'] for f in grant['files']):
        return None
    from .implementation_fixture_runner import FixtureStore
    if isinstance(engine.store,FixtureStore):
        engine.store.check_job(run_id,program,plan,params,trial_id,trial_name,planned_params,grant)
    else:
        state = engine.store.get('codex_workflows',program.get('delivery_id',''),{})
        if not state or not workflow_grant(engine,run_id,state,grant):
            return None
        if grant['plan_sha256']!=digest(plan):
            raise ResearchBlocked('行成员用途授权未覆盖当前实验计划。')
        from .python_training import jobs
        from .parameter_search import jobs as search_jobs
        from .fair_comparison import strict_equal
        def matches(inventory):
            return any(name==trial_name and strict_equal(frozen,planned_params) for name,frozen in inventory)
        if not (matches(jobs(plan,'experiments',transport)) or matches(jobs(plan,'tuning',transport))
                or matches(search_jobs(engine,run_id,plan))):
            raise ResearchBlocked('行成员用途仅覆盖已批准的验证任务及其参数。')
        row = next((r for r in state['specifications'] if r['spec_hash']==program['spec_hash'] and r.get('approved')),None)
        author = engine.store.get('handoff_inputs',row['authoring_handoff_id'],{}) if row else {}
        materials = author.get('context',{}).get('implementation_work',{}).get('materials',{})
        if materials.get('validation_row_identity',{}).get('authorization')!=grant:
            raise ResearchBlocked('批准的实现文档尚未绑定本次数据行身份用途。')
    from .hyperparameter_yaml import trial_receipt
    return {'run_id':run_id,'cycle':grant['cycle'],'workflow_id':grant['workflow_id'],'plan_sha256':grant['plan_sha256'],
        'trial_id':trial_id,'trial_name':trial_name,'code_sha256':program['code_sha256'],'params_sha256':digest(params),
        'phase':'validation','purpose':grant['purpose'],'authorization_sha256':grant['sha256'],
        'hyperparameter_yaml':trial_receipt(yaml_contract)}


def staging_metadata(store,run_id,trial_id,code,params,root,staged,yaml_receipt,admitted):
    from .dataset_file_identity import portable_stat
    from .deletion import _plain_path
    grant = current(store,run_id,admitted['cycle'])
    if (not grant or grant['sha256']!=admitted['authorization_sha256'] or admitted['run_id']!=run_id
            or admitted['trial_id']!=trial_id or admitted['params_sha256']!=digest(params)
            or admitted['code_sha256']!=hashlib.sha256(code.encode('utf-8')).hexdigest()
            or params.get('phase')!='validation' or admitted['phase']!='validation'
            or any(admitted[k]!=grant[k] for k in ('purpose','workflow_id','plan_sha256'))):
        raise ValueError('Validation row identity does not match this admission')
    if [{'asset_id':a['asset_id'],'manifest_sha256':a['manifest_sha256']} for a in staged]!=params['dataset_assets']:
        raise ValueError('Staged assets differ from admitted ordered assets')
    if yaml_receipt != admitted['hyperparameter_yaml'] or yaml_receipt and yaml_receipt.get('params_sha256')!=admitted['params_sha256']:
        raise ValueError('Staged YAML differs from the row identity invocation')
    files = []
    for asset in staged:
        for item in asset['files']:
            identity = {'asset_id':asset['asset_id'],'manifest_sha256':asset['manifest_sha256'],
                        'trial_path':item['trial_path'],'sha256':item['sha256']}
            if identity not in grant['files']:
                continue
            path = Path(root)/item['trial_path']
            _plain_path(path,Path(root))
            if portable_stat(path.stat())!=item['staged_stat']:
                raise ValueError('Staged CSV identity changed before reading record boundaries')
            cache_key = f"{run_id}:{FORMAT}:{item['sha256']}"
            cached = store.get('dataset_record_counts', cache_key)
            with path.open(encoding='utf-8-sig',newline='') as stream:
                reader = csv.reader(stream,strict=True)
                header = next(reader,[])
                if not header or not all(header):
                    raise ValueError('CSV structural identity requires a nonempty header')
                count = sum(1 for _ in reader) if cached is None else cached['row_count']
            if type(count) is not int or count<=0 or portable_stat(path.stat())!=item['staged_stat']:
                raise ValueError('Staged CSV is empty or changed while counting logical records')
            if cached is None:
                store.put('dataset_record_counts', cache_key, {'run_id':run_id,'row_count':count})
            files.append({**identity,'bytes':item['bytes'],'staged_stat':item['staged_stat'],
                          'row_count':count,'first_row':0,'order':'source_record_order'})
    required = [f for f in grant['files'] if {'asset_id':f['asset_id'],'manifest_sha256':f['manifest_sha256']} in params['dataset_assets']]
    if not files or len(files)!=len(required) or any(not any(all(actual[k]==v for k,v in f.items()) for actual in files) for f in required):
        raise ValueError('The staged CSV inventory differs from the selected files')
    return {'trial_id':trial_id,'trial_name':admitted['trial_name'],'dataset_row_identity_grant':grant,
            FIELD:{**admitted,'format':FORMAT,'files':files,'hyperparameter_yaml':yaml_receipt}}
