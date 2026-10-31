"""Explicit, independently reviewed diagnostic batches, separate from training."""
import hashlib
import json
from pathlib import Path
from .storage import now
from .experiment_workflow import sha, encoded
from .training_adjustment import obj, _group

COLLECTION = 'controlled_training_checks'
REVIEW = '''独立评审受控训练诊断方案和可执行代码，不能沿用旧补日志提案的批准。
必须用 experiments.inspect_module(view=source,snapshot=candidate) 完整读取 changed_ranges 中的检查脚本和协议；必要时核对已有模型/检查点/数据入口源码。
若证据目录含 controlled_admission，必须用 experiments.compare 完整读取 /controlled_admission（limit=6000，读取全部分页）并引用回执；这是绑定本候选的宿主实际台账和剩余预算，不能用候选文件中的文字代替。
核查三个竞争解释的区分能力：保存检查点的模型摘要和重算验证指标；同一已保存权重/Adam状态/随机状态、同一新训练批次序列的原学习率与1/4学习率对照；不更新控制、固定全训练集/验证集在0/8/16/24/32步的同口径指标。
本次只从种子17和29已保存最佳检查点派生新短程反事实，每个学习率分支32次更新，共最多128次更新；不是重跑原训练，不恢复历史中途游标，不改原模型、日志、配置或激活新参数。不能读取test分区。
检查应能识别恢复/验证不一致、局部更新敏感性和局部泛化权衡；有限短程结果允许inconclusive，不能声称排除了慢性不稳定、分布差异或已证明全局过拟合。诊断阈值不是验收阈值，不进行参数选择。
仅生成摘要、日志格式/异常保持断言不足以通过本次评审。原方案的fit→persist检查不替代这组三因对照；不重复插桩。
批准只授权这个SHA绑定的单次CPU诊断批次，资源上限见协议；存在实现、身份、对照或资源缺口则给出具体issues并拒绝。不得为了得到阳性结论修改判别阈值。'''
TEXT = {'type':'string','minLength':1,'maxLength':900}
AFTER = obj({'evidence_sha256':{'type':'string','pattern':'^[a-f0-9]{64}$'},
    'findings':{'type':'array','minItems':3,'maxItems':3,'items':obj({
        'hypothesis':{'enum':['overfitting','update_instability','checkpoint']},
        'verdict':{'enum':['supported_locally','not_supported_in_this_check','inconclusive']},
        'evidence':TEXT,'limitations':TEXT})},
    'next_step':TEXT,'read_receipts':{'type':'array','minItems':1,'maxItems':16,'uniqueItems':True,
                                    'items':{'type':'string','pattern':'^[a-f0-9]{64}$'}}})
AFTER_INSTRUCTION = '''根据这次真实受控诊断结果重新判断，不重复补日志，不自动运行训练。
先用 experiments.compare 完整读取 /controlled_result/summary 的所有分页（limit=6000），返回实际receipt_sha256。
分别评价overfitting、update_instability、checkpoint，不得遗漏或重复。严格遵守预注册协议的判别范围。
检查点一致仅支持保存的最终最佳状态恢复一致，不能证明未保存的历史末步状态；CPU/GPU漂移已单独给定容差。
原学习率与降低学习率都出现平滑的全训练集改善而验证退化，且无更新尖峰，支持局部泛化权衡；不等于证明全局过拟合。
只有原学习率出现训练/验证同步尖峰、非有限值或突变且降学习率缓解，才支持局部更新不稳定；单纯较低学习率的验证更好不足以定因。
所有观察以实际字段为准，没有轨迹或结果不具区分力就inconclusive。说明下一项最小必要工作，不选择或激活任何候选。'''


def current(engine, run_id):
    return engine.store.get(COLLECTION, f'{run_id}:{engine.cycle(run_id)}')


def admission(engine,run_id,ignore=None):
    """Actual host ledger, independent of candidate-authored files."""
    from .rapid_continuation import trial_limit
    with engine.store._read_db() as db:
        rows=db.execute("SELECT key,json_extract(data,'$.id'),json_extract(data,'$.status'),"
            "coalesce(json_extract(data,'$.attempt'),1),coalesce(json_extract(data,'$.research_cycle'),1),"
            "json_extract(data,'$.params.phase'),json_extract(data,'$.code_sha256') "
            "FROM records WHERE collection='experiments' AND json_extract(data,'$.run_id')=? ORDER BY key",(run_id,)).fetchall()
    ledger=[{'key':r[0],'id':r[1],'status':r[2],'attempt':r[3],'cycle':r[4],'phase':r[5],'code_sha256':r[6]}
            for r in rows if r[1]!=ignore]
    used=sum(r['attempt'] for r in ledger if r['cycle']==engine.cycle(run_id))
    limit=trial_limit(engine.store,run_id)
    return {'ledger':ledger,'ledger_sha256':sha(ledger),'attempts_used_current_cycle':used,'attempt_limit':limit,
            'one_batch_admissible':limit is None or used+1<=limit,'needed_attempts':1}


def register(engine, run_id, evidence_id, authorization, *, supersedes=None, test_evidence=None):
    """Called only for an explicit user request while paused; no inference of approval."""
    if engine.store.get('runs',run_id)['status'] != 'paused':
        raise ValueError('Pause the research before binding a new diagnostic request')
    old=current(engine,run_id)
    if old and (old['status']!='rejected' or old['candidate']['amendment_sha256']!=supersedes or old.get('trial_id')):
        raise ValueError('Only an explicitly superseded, rejected and unexecuted proposal may be revised')
    evidence = engine.store.get('experiment_evidence',evidence_id)
    if not evidence or evidence['run_id'] != run_id or sha(evidence['summary']) != evidence['sha256']:
        raise ValueError('Diagnostic evidence changed')
    source = engine.store.get('experiment_sources',evidence['summary']['code_sources']['id'])
    if sha(source['files']) != evidence['summary']['code_sources']['sha256']:
        raise ValueError('Original source archive changed')
    files = {p:r['text'].encode('utf-8') for p,r in source['files'].items() if 'text' in r}
    program = engine.store.get('programs',run_id)
    inputs, originals, assets = [], {}, None
    for trial_id in ('c2-v3-dataset-1--initial-proposed','c2-v3-dataset-1--grid-1-proposed'):
        trial = engine.store.get('experiments',run_id+':'+trial_id)
        if not trial or trial['status'] != 'completed' or trial['params']['phase'] != 'validation':
            raise ValueError('Requires completed saved validation checkpoints')
        base = Path(trial['artifact_path'])
        checkpoint = base/'artifacts/best.pt'
        result = json.loads((base/'artifacts/result.json').read_text(encoding='utf-8'))
        training = result['evaluation']['details']['training']
        raw = checkpoint.read_bytes();digest = hashlib.sha256(raw).hexdigest()
        if digest != training['best_checkpoint_sha256']:
            raise ValueError('Saved checkpoint differs from original execution receipt')
        seed=trial['params']['seed'];config_path=f'controlled_inputs/{seed}/config.json'
        checkpoint_path=f'controlled_inputs/{seed}/best.pt'
        config = json.dumps(trial['params'],ensure_ascii=False,sort_keys=True).encode('utf-8')
        files[checkpoint_path]=raw;files[config_path]=config
        originals[str(checkpoint.resolve())]=digest
        originals[str((base/'artifacts/result.json').resolve())]=hashlib.sha256((base/'artifacts/result.json').read_bytes()).hexdigest()
        inputs.append({'trial_id':trial_id,'seed':seed,'config_path':config_path,'config_sha256':hashlib.sha256(config).hexdigest(),
            'checkpoint_path':checkpoint_path,'checkpoint_sha256':digest,'final_model_sha256':training['model_sha256'],
            'reported_val_mse':result['metrics']['val_mse'],'split_fingerprints':result['evaluation']['split_fingerprints']})
        if assets is not None and assets != trial['params']['dataset_assets']:
            raise ValueError('Controlled seeds use different dataset assets')
        assets=trial['params']['dataset_assets']
    limits={'timeout_seconds':600,'cpu_seconds':600,'cpu_cores':2,'memory_mb':4096,'disk_mb':256}
    protocol={'version':3,'seeds':[17,29],'steps':32,'lr_factors':[1.,.25],'evaluation_steps':[8,16,24,32],
        'entrypoint':'experiment.py -> diagnostic_guard.launch -> verified nested Windows affinity Job -> diagnostic_probe.main; no numerical imports before the diagnostic gate',
        'historical_metric_rtol':1e-4,'historical_metric_atol':1e-6,'limits':limits,'inputs':inputs,
        'discriminators':{'checkpoint':'Exact saved final-model digest plus full validation reevaluation within declared CPU/GPU tolerance; stop update probes if mismatch.',
            'overfitting':'Local support only: full training MSE improves >0.1% while validation worsens >1% at >=3 of 4 checkpoints, in both seeds and both LR branches, without update spikes. Otherwise inconclusive.',
            'update_instability':'Local support only: original-LR train AND validation rise >5% at >=2 checkpoints with update norm spikes >5x branch median, and lower LR mitigates both rises by >=50% in both seeds; or reproducible nonfinite updates. LR sensitivity alone is not instability.'},
        'restrictions':['No test data','No full training replay','No edits to original sources/logs/configs',
                        'No parameter selection or active model replacement','No automatic retries or additional batches']}
    files['diagnostic_protocol.json']=json.dumps(protocol,ensure_ascii=False,indent=2).encode('utf-8')
    files['diagnostic_probe.py']=Path(__file__).with_name('controlled_training_probe.py').read_bytes()
    files['diagnostic_verdicts.py']=Path(__file__).with_name('controlled_diagnostic_verdicts.py').read_bytes()
    files['diagnostic_guard.py']=Path(__file__).with_name('controlled_diagnostic_guard.py').read_bytes()
    from .rapid_experiment_revision import _budget
    _budget(engine,run_id,1)
    import inspect
    from .lab import ExperimentRunner, _WindowsJob
    from .rapid_continuation import trial_limit
    with engine.store._read_db() as db:
        attempts=db.execute("SELECT coalesce(sum(coalesce(json_extract(data,'$.attempt'),1)),0) FROM records "
            "WHERE collection='experiments' AND json_extract(data,'$.run_id')=? AND coalesce(json_extract(data,'$.research_cycle'),1)=?",
            (run_id,engine.cycle(run_id))).fetchone()[0]
    def excerpt(obj, start=None, end=None):
        lines,line=inspect.getsourcelines(obj)
        text=''.join(lines)
        left=text.index(start) if start else 0
        right=text.index(end,left) if end else len(text)
        source=Path(inspect.getsourcefile(obj))
        return {'source':source.name,'file_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
                'first_line':line+text[:left].count('\n'),'text':text[left:right]}
    host={'scope':'Actual host source excerpts; CPU fixtures test gates, not scientific results.',
        'execution':excerpt(execute),'budget_gate':excerpt(_budget),'windows_job':excerpt(_WindowsJob),
        'runner_environment':excerpt(ExperimentRunner._environment),
        'launch':excerpt(ExperimentRunner.run,'        normalized =','        key ='),
        'runner_binding_and_start':excerpt(ExperimentRunner.run,'                launch = asyncio.create_task(asyncio.create_subprocess_exec','                output_exceeded ='),
        'runner_monitor':excerpt(ExperimentRunner.run,'                reason, peak_memory','                if output_exceeded.is_set():\n                    reason'),
        'budget_at_registration':{'attempts_used':attempts,'attempt_limit':trial_limit(engine.store,run_id),
            'one_additional_attempt_admitted':True,'rechecked_immediately_before_execution':True},
        'test_evidence':test_evidence,
        'data_call_chain':['diagnostic_probe.main -> load_data(phase=validation, check_config=invocation)',
             'dataset_runtime/data.py: load_data -> read_prefix(stop=validation end), unbuffered binary IO',
             'Full-asset byte hashing/staging checks integrity; no test values reach training/evaluation.'],
        'prior_review_issues':[{k:v[k] for k in ('role','issues')} for v in old['votes']] if old else []}
    files['host_evidence.json']=json.dumps(host,ensure_ascii=False,indent=2).encode('utf-8')
    code='from diagnostic_guard import launch\nlaunch()\n';files['experiment.py']=code.encode()
    from .rapid_experiment_revision import _write_files
    identity=sha({'protocol':protocol,'diagnostic_sources':{p:hashlib.sha256(files[p]).hexdigest()
        for p in ('diagnostic_probe.py','diagnostic_verdicts.py','diagnostic_guard.py','host_evidence.json')}})
    bundle=_write_files(engine.run_dir(run_id)/'controlled-diagnostics'/identity,files)
    from .experiment_analysis import freeze_sources
    sources=freeze_sources(engine,run_id,{'code':code},bundle)
    ranges=[{'path':p,'start':0,'end':len(files[p].decode('utf-8')),'sha256':hashlib.sha256(files[p]).hexdigest()}
            for p in ('experiment.py','diagnostic_probe.py','diagnostic_protocol.json','diagnostic_verdicts.py','diagnostic_guard.py','host_evidence.json')]
    record={'id':f'{run_id}:{engine.cycle(run_id)}','run_id':run_id,'cycle':engine.cycle(run_id),
        'status':'reviewing','created_at':now(),'authorization':authorization,'protocol':protocol,
        'original_evidence':{'id':evidence_id,'sha256':evidence['sha256']},'binding':evidence['summary']['binding'],
        'original_files':originals,'base_program':program,'dataset_assets':assets,
        'host_sources':{str(Path(inspect.getsourcefile(fn)).resolve()):hashlib.sha256(Path(inspect.getsourcefile(fn)).read_bytes()).hexdigest()
                        for fn in (execute,ExperimentRunner,_budget)},
        'test_evidence':test_evidence,
        'admission':admission(engine,run_id),
        'candidate':{'bundle':bundle,'code_sources':sources,'changed_ranges':ranges,
            'checks_sha256':hashlib.sha256(code.encode()).hexdigest(),'amendment_sha256':identity},
        'code':code,'votes':[],'scientific_approval':False}
    if old:
        archive=old['id']+':revision:'+old['candidate']['amendment_sha256']
        engine.store.put(COLLECTION,archive,old)
        record['supersedes']={'id':archive,'sha256':sha(old)}
    engine.store.put(COLLECTION,record['id'],record)
    engine.store.event(run_id,'controlled_diagnostic.requested','用户要求三因对照；保留旧补日志提案，单次诊断须独立评审',{'id':record['id'],'sha256':identity})
    return record


def unchanged(engine, record):
    from .experiment_decisions import _binding
    from .code_bundle import validate
    if _binding(engine,record['run_id'])[1] != record['binding']:
        raise ValueError('Original experiment binding changed')
    validate(record['candidate']['bundle'],record['code'])
    for p,digest in record['original_files'].items():
        if hashlib.sha256(Path(p).read_bytes()).hexdigest()!=digest:
            raise ValueError('Original checkpoint/result changed')
    for p,digest in record.get('host_sources',{}).items():
        if hashlib.sha256(Path(p).read_bytes()).hexdigest()!=digest:
            raise ValueError('Reviewed host execution gate changed')


def review_context(engine, record, result=None):
    from .experiment_decisions import _archive
    summary={'binding':record['binding'],'code_sources':record['candidate']['code_sources'],
        'candidate_not_executed':result is None,'controlled_protocol':record['protocol'],
        'original_validation_evidence':record['original_evidence'],
        'acceptance_checks':{'scope':'Offline code/gate checks only, not scientific acceptance',
                             'test_evidence':record.get('test_evidence'),'host_source_path':'host_evidence.json'}}
    if record.get('admission'):
        summary['controlled_admission']={**record['admission'],'candidate_sha256':record['candidate']['bundle']['sha256'],
            'source':'Host SQLite experiment ledger; revalidated after independent votes and immediately before execution'}
    if result is not None:summary['controlled_result']=result
    ref=_archive(engine,record['run_id'],summary)
    return {'training_context_protocol':1,'experiment_workflow_protocol':1,'research_cycle':record['cycle'],
        'evidence_sha256':ref['sha256'],'validation_evidence':ref,'experiment_decision':{'controlled_check':True},
        'rapid_revision':{'id':record['id'],'amendment_sha256':record['candidate']['amendment_sha256'],
            'candidate_sha256':record['candidate']['bundle']['sha256'],'checks_sha256':record['candidate']['checks_sha256'],
            'changed_ranges':record['candidate']['changed_ranges'],'controlled_protocol':record['protocol'],
            'scope':'New bounded diagnosis only; existing training remains immutable.'}}


def verify_result_reads(engine, record, answer, context):
    from .engine import Harness
    from .experiment_tools import compare
    purpose='experiment_diagnose:controlled-result:'+context['evidence_sha256']
    key=Harness.decision_key(record['run_id'],answer['role'],purpose,AFTER_INSTRUCTION,context,AFTER)
    frozen=engine.store.get('handoff_inputs',key) or {}
    owner=engine.store.get('handoffs',key) or {}
    saved=(engine.store.get('decisions',key) or {}).get('result')
    if (frozen.get('context')!=context or answer['evidence_sha256']!=context['evidence_sha256']
            or owner.get('run_id')!=record['run_id'] or owner.get('to_role')!=answer['role']
            or saved!={k:v for k,v in answer.items() if k!='role'}):
        raise ValueError('Result review is not owned by this frozen role')
    valid=set();ranges=[];total=None
    for receipt in (engine.store.get('handoff_progress',key) or {}).get('tool_results',[]):
        call,result=receipt.get('call',{}),receipt.get('result',{})
        if result.get('receipt_sha256') not in answer['read_receipts']:continue
        body={k:v for k,v in result.items() if k!='receipt_sha256'}
        if result['receipt_sha256']!=sha({'call':call,'result':body}) or call.get('name')!='experiments.compare':continue
        if compare(engine,record['run_id'],context,call['arguments'])!=body:continue
        valid.add(result['receipt_sha256'])
        if call['arguments'].get('path')=='/controlled_result/summary':
            ranges.append((result['offset'],result['end']));total=result['total_chars']
    end=0
    for start,stop in sorted(ranges):
        if start>end:break
        end=max(end,stop)
    if valid!=set(answer['read_receipts']) or total is None or end!=total:
        raise ValueError('Every result reviewer must read the whole bounded result summary')
    if {f['hypothesis'] for f in answer['findings']}!={'overfitting','update_instability','checkpoint'}:
        raise ValueError('Result review omitted a competing hypothesis')
    return {'role':answer['role'],'handoff_id':key,'complete':True,'total_chars':total,'receipts':sorted(valid)}


async def execute(engine,harness,run_id):
    record=current(engine,run_id)
    if not record or record['status']=='completed':return None
    from .engine import ResearchBlocked
    from .rapid_experiment_revision import VOTE,review_receipts,_reviewed,_budget
    engine.check_active(run_id)
    def save(**kw):
        record.update(kw,updated_at=now());engine.store.put(COLLECTION,record['id'],record)
    await engine._read_consensus_check(run_id,unchanged,engine,record)
    context=review_context(engine,record)
    purpose='rapid_review:controlled:'+record['candidate']['amendment_sha256']
    if not record['votes']:
        votes=await _group(harness,run_id,purpose,REVIEW,context,VOTE)
        save(votes=votes)
    from .controlled_review_completion import complete
    reads=await complete(engine,harness,record,context,save)
    if not _reviewed(record):
        save(status='rejected',review_reads=reads)
        raise ResearchBlocked('受控诊断方案未获三方独立批准；保留具体异议，不执行或重跑。')
    save(status='approved',review_reads=reads)
    from .lab import assess_resources
    from .training_adjustment import _basis
    resource=_basis(engine,run_id)['design']['resource_plan']
    required=sorted(set(resource.get('required_packages',[])+record['base_program'].get('dependencies',[])))
    await engine._read_consensus_check(run_id,engine._execution_gate,run_id)
    assessed=assess_resources({**resource,'required_packages':required},await engine.execution_hardware(run_id))
    if not assessed['approved']:raise ResearchBlocked('受控诊断未通过当前资源准入。')
    run=engine.store.get('runs',run_id)
    limits={**assessed['limits'],'gpu_allowed':False,'required_packages':required,'execution_mode':run['config'].get('execution_mode','local'),
        'code_bundle':record['candidate']['bundle'],'experiment_binding':{'research_cycle':record['cycle'],
            'code_revision':record['binding']['revision'],'code_sha256':record['candidate']['checks_sha256'],
            'experiment_plan_sha256':record['binding']['plan_sha256']}}
    for field,cap in record['protocol']['limits'].items():limits[field]=min(limits.get(field) or cap,cap)
    identifier='controlled-check-'+record['candidate']['amendment_sha256'][:24]
    params={'phase':'code_check','dataset':'ETTh1','dataset_assets':record['dataset_assets'],
            'controlled_check_id':record['id']}
    result=engine.store.get('experiments',run_id+':'+identifier)
    if not result:
        await engine._read_consensus_check(run_id,unchanged,engine,record)
        if record.get('admission') and admission(engine,run_id)!=record['admission']:
            raise ResearchBlocked('受控检查的宿主台账或剩余额度已变化，须重新审阅；不执行。')
        _budget(engine,run_id,1)
        save(status='executing',resource_admission=assessed,effective_limits=limits,trial_id=identifier)
        result=await engine.runner.run(run_id,record['code'],params,identifier,limits,engine.cancellations[run_id])
    if (result.get('status')!='completed' or result.get('code_sha256')!=record['candidate']['checks_sha256']
            or result.get('params')!=params or result.get('limits',{}).get('code_bundle')!=record['candidate']['bundle']):
        save(status='execution_failed',trial_id=identifier,result_receipt_sha256=sha(result))
        raise ResearchBlocked('受控诊断失败或执行状态不明，原回执已保留；不自动重跑。')
    await engine._read_consensus_check(run_id,unchanged,engine,record)
    from .experiment_views import _safe_file
    path=_safe_file(engine,run_id,result,'artifacts/controlled_diagnostic.json')
    if path is None:raise ResearchBlocked('受控诊断缺少实际结果文件。')
    raw=path.read_bytes();report=json.loads(raw)
    if (report.get('protocol')!=record['protocol'] or not report.get('input_hashes_unchanged')
            or result.get('experiment_metadata',{}).get('diagnostic_report_sha256')!=hashlib.sha256(raw).hexdigest()):
        raise ResearchBlocked('受控诊断结果未绑定原协议和输入身份。')
    outcome={'trial_id':identifier,'receipt_sha256':sha(result),'report_sha256':hashlib.sha256(raw).hexdigest(),
        'summary':report['summary'],'scientific_approval':False}
    save(status='reviewing_results',outcome=outcome)
    result_context=review_context(engine,record,outcome)
    if not record.get('analyses'):
        analyses=await _group(harness,run_id,'experiment_diagnose:controlled-result:'+result_context['evidence_sha256'],
                              AFTER_INSTRUCTION,result_context,AFTER)
        result_reads=[verify_result_reads(engine,record,a,result_context) for a in analyses]
        save(analyses=analyses,result_reads=result_reads)
    save(status='completed',completed_at=now())
    engine.store.event(run_id,'controlled_diagnostic.completed','已执行单次受控诊断并完成三方结果复核；未激活参数或重复原训练',
        {'trial_id':identifier,'report_sha256':outcome['report_sha256']})
    return {'tuning_pending':True,'next_node':'tuning','activity':'controlled_diagnostic','controlled_check_id':record['id'],
            'scientific_approval':False}
