"""Resume a failed validation batch with an operator-scoped implementation correction."""
from .engine import ResearchBlocked, digest
from .experiment_decisions import SESSIONS, _binding


def failed_input(engine, run_id, session):
    from .experiment_decisions import units
    from .comparison_transport import runtime_transport
    batch = (session or {}).get('last_batch') or {}
    name = batch.get('stopped_at_job')
    if not name or session.get('pending') or batch.get('status') != 'failed':
        raise ResearchBlocked('当前批次没有已落盘的验证失败。')
    basis, binding = _binding(engine, run_id)
    unit = next((u for u in units(basis['plan'], runtime_transport(engine,run_id))
                 if u['id'] == batch['unit_id']), None)
    params = dict(batch['jobs']).get(name)
    if (session['binding'] != binding or not unit or dict(unit['jobs']) != dict(batch['jobs'])
            or not params or params.get('phase') != 'validation'):
        raise ResearchBlocked('失败批次与当前计划、代码或参数不匹配。')
    cycle, revision = binding['cycle'], binding['revision']
    trial_id = (f'c{cycle}-' if cycle > 1 else '') + f'v{revision}-{name}'
    result = engine.store.get('experiments', run_id + ':' + trial_id, {})
    if (result.get('status') != 'failed' or result.get('run_id') != run_id or result.get('params') != params
            or result.get('code_revision') != revision or result.get('code_sha256') != binding['code_sha256']
            or result.get('experiment_plan_sha256') != binding['plan_sha256'] or result.get('research_cycle') != cycle):
        raise ResearchBlocked('缺少同版本失败实验的执行回执。')
    return basis, result


def register(engine, run_id, instruction):
    from .experiment_decisions import session
    run = engine.store.get('runs', run_id)
    if run['stage'] != 'experiments' or run['status'] not in ('blocked', 'paused'):
        raise ResearchBlocked('验证修订需在已暂停或阻断的实验节点登记。')
    state = session(engine, run_id)
    basis, result = failed_input(engine, run_id, state)
    program = basis['program']
    parent = engine.store.get('codex_workflows',program.get('delivery_id',''),{})
    if (program.get('implementation_backend')!='local_codex' or program.get('rapid_revision_id')
            or program.get('dataset_runtime_id') or parent.get('status')!='completed'
            or parent.get('implementation_review_strategy')!='document_code'
            or parent['result'].get('delivery_hash')!=program.get('delivery_hash')):
        raise ResearchBlocked('本次运行修订需要可继承的已审核 Codex 交付。')
    old = state.get('repair_request')
    yaml = result['limits'].get('hyperparameter_yaml') or {}
    request = {'instruction':instruction, 'trial_id':result['id'], 'receipt_sha256':digest(result),
               'binding':state['binding']}
    if yaml and (not old or 'invocation' in old):
        request['invocation'] = {k:yaml[k] for k in ('entry_arguments','config_file','yaml_sha256') if k in yaml}
    if old and old != request:
        raise ResearchBlocked('当前失败已有绑定的修订请求。')
    if not old:
        engine.store.batch(puts=[(SESSIONS,state['id'],{**state,'repair_request':request})],
                           expected=[('runs',run_id,run),(SESSIONS,state['id'],state)])
    return request


def invocation(engine, run_id, repair):
    if not repair or 'receipt_sha256' not in repair:
        return None
    result = engine.store.get('experiments',run_id+':'+repair['trial_id'])
    if digest(result) != repair['receipt_sha256']:
        raise ResearchBlocked('原命令行的失败执行回执不匹配。')
    yaml = result['limits'].get('hyperparameter_yaml') or {}
    return {k:yaml[k] for k in ('entry_arguments','config_file','yaml_sha256') if k in yaml}


def review_inputs(engine, run_id, state, attempt=None):
    """The failed invocation was executed. The other validation inputs are plan projections."""
    import copy
    from .python_training import jobs
    from .hyperparameter_yaml import prepare_trial, trial_receipt
    if not getattr(engine.store,'is_read_snapshot',False):
        with engine.store.read_snapshot() as snapshot:
            owner=copy.copy(engine)
            owner.store=snapshot
            return review_inputs(owner,run_id,state,attempt)
    repair=state['repair']
    failed=engine.store.get('experiments',run_id+':'+repair['trial_id'])
    plan=state['common']['experiment_plan']
    if (digest(failed)!=repair['receipt_sha256'] or failed['params']['phase']!='validation'
            or failed['experiment_plan_sha256']!=digest(plan) or engine.experiment_plan(run_id)!=plan):
        raise ResearchBlocked('验证代码审查无法绑定原失败命令与冻结计划。')
    def config_view(config):
        if config:
            return {'entry_arguments':config.get('entry_arguments',['params.json']),
                    'yaml':config['yaml'],'receipt':trial_receipt(config)}
    invocation=failed['limits'].get('hyperparameter_yaml')
    actual={'trial_id':failed['id'],'params':failed['params'],'config':config_view(invocation)}
    projected_program=repair['program']
    projection_source='parent_delivery'
    if attempt and state.get('hyperparameter_yaml_protocol')==1:
        from .yaml_migration import inspect_contract
        contract=inspect_contract(attempt['acceptance']['runtime_bundle'])
        if 'entry_arguments' in contract['contract']:
            projected_program={'code':attempt['acceptance']['code'],'delivery_id':state['id'],
                'delivery_hash':attempt['delivery_hash'],'hyperparameter_yaml_protocol':1,
                'hyperparameter_contract':contract,'revision':repair['program'].get('revision',0)+1}
            projection_source='candidate_delivery'
    planned=[]
    for stage in ('experiments','tuning'):
        for name,params in jobs(plan,stage):
            config=prepare_trial(engine,run_id,projected_program,params,trial_name=name,use_pending=False,persist=False)
            planned.append({'name':name,'params':params,'config':config_view(config)})
    return {'phase':'validation','source_receipt_sha256':repair['receipt_sha256'],
            'operator_instruction':repair['operator_instruction'],'actual_failed_invocation':actual,
            'planned_validation_inputs':planned,'projection_source':projection_source,
            'projection_delivery_hash':projected_program['delivery_hash']}


async def execute(engine, harness, run_id, state):
    from .codex_workflow import implement, validate_program
    request = state['repair_request']
    basis, result = await engine._read_consensus_check(run_id, failed_input, engine, run_id, state)
    if (request['trial_id'] != result['id'] or request['receipt_sha256'] != digest(result)
            or request['binding'] != state['binding']):
        raise ResearchBlocked('已登记的验证修订与原始失败回执不一致。')
    program = basis['program']
    parent = engine.store.get('codex_workflows', program['delivery_id'])
    fix = await implement(engine, harness, run_id, parent['common'], repair={
        'trial_id':result['id'], 'program':program, 'error':str(result.get('stderr') or result['error'])[-3000:],
        'operator_instruction':request['instruction'], 'receipt_sha256':request['receipt_sha256'],
        **({'invocation':request['invocation']} if 'invocation' in request else {})})
    engine.check_active(run_id)
    bundle = await engine._read_consensus_check(run_id,validate_program,engine,run_id,fix,source_only=True)
    if bundle['sha256'] == state['binding']['bundle_sha256']:
        raise ResearchBlocked('运行修订没有改变已失败的可执行文件。')
    revision = state['binding']['revision'] + 1
    active = {**fix,'id':run_id,'run_id':run_id,'revision':revision}
    binding = {**state['binding'],'code_sha256':fix['code_sha256'],'bundle_sha256':bundle['sha256'],'revision':revision}
    from .rapid_experiment_revision import _budget
    batch = state['last_batch']
    _budget(engine,run_id,len(batch['jobs']))
    pending = {k:v for k,v in batch.items() if k not in ('completed_jobs','stopped_at_job')}
    if batch.get('decision_id') and not engine.store.get('experiment_decisions',batch['decision_id']):
        raise ResearchBlocked('原验证批次的批准决议缺失。')
    updated = {**state,'binding':binding,'pending':{**pending,'status':'executing'}}
    del updated['repair_request']
    archive_id = state['id'] + ':repair:' + digest(request)
    engine.store.batch(puts=[('validation_repair_history',archive_id,state),('programs',run_id,active),
        (SESSIONS,state['id'],updated)], expected=[('programs',run_id,program),(SESSIONS,state['id'],state),
        ('experiments',result['storage_key'],result)])
    return updated
