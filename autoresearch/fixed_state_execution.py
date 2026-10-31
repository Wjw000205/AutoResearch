"""SHA-bound candidate review, one observation, then independent result review."""
import asyncio
import copy
import hashlib
import inspect
import json
from pathlib import Path

from .experiment_workflow import sha
from .storage import now
from .training_adjustment import obj

REVIEW = '''对已获三方协议批准的新固定状态诊断，独立审查这一份候选代码。
用 experiments.inspect_module(snapshot=candidate,view=source,limit=6000) 完整分页读取 changed_ranges，
并用 experiments.compare 完整读取 /controlled_admission 全部分页；返回所有真实read_receipts。
确认原模型与三个保存状态SHA不变、仅train/validation、完整窗口加权梯度、Adam历史矩方向不是下一更新、
两种等长方向与冻结双半径、误差规则、每个点独立恢复原状态、无fit/optimizer.step、无模型激活。
核对宿主执行代码、真实额度与CPU回归回执。最多2 CPU/4096MB RAM/600秒墙钟与CPU/256MB磁盘/无GPU，取原门禁更严格值。
这是一次新的固定状态测量，不能重跑已完成正式训练或32/64步诊断，不能从失败/未知派发自动重试。
approve只授权此SHA绑定的一批诊断，不是科学验收。存在阻断缺陷则false并列issues；
如果仅有协议已声明的解释局限，把局限写在rationale，approve=true且issues=[]，不得同时批准与留阻断项。
每个角色独立判断。根因unknown、原架构及稳定性门禁保持不变；结果允许inconclusive。'''

RESULT_PATHS=('/fixed_state_result/summary','/fixed_state_protocol')
RESULT_INSTRUCTION='''独立复核这一次真实固定状态诊断。用experiments.compare完整分页读取
/fixed_state_result/summary和/fixed_state_protocol，read_receipts仅返回这两条路径全部实际回执。
逐状态核对两种局部签名的每个预声明条件、双半径数值判据、原状态保持、完整train/validation口径。
也可按需读/fixed_state_result/report（原始点和逐参数贡献）；不得将附加回执混入read_receipts。
给每个状态独立结论：local_objective_conflict/local_adam_direction_misalignment/mixed/inconclusive。
如果发现测量或判据不可靠，valid_observation=false并列具体issues；如果仅有已声明局限，写limitations，不列为阻断。
同种子局部结果不是历史过拟合或长期数值不稳定的证明。原32/64步结论仍为inconclusive；
没有保存的正式训练末态仍然缺失。不得把这次完成视为参数稳定、激活模型、切换数据集或科学验收。
请提出依此证据可共同执行的最小下一步，不重复任何已完成检查，不放宽冻结标准。'''


def result_schema(evidence_sha):
    text={'type':'string','minLength':1}
    return obj({'evidence_sha256':{'const':evidence_sha},'valid_observation':{'type':'boolean'},
        'states':{'type':'array','minItems':3,'maxItems':3,'items':obj({
            'label':{'enum':['zero','lr-1.0-step64','lr-0.25-step64']},
            'finding':{'enum':['local_objective_conflict','local_adam_direction_misalignment','mixed','inconclusive']},
            'evidence':text})},'issues':{'type':'array','items':text},'limitations':text,'next_step':text,
        'historical_root_cause':{'const':'unknown'},'scientific_approval':{'const':False},
        'read_receipts':{'type':'array','minItems':1,'uniqueItems':True,
                         'items':{'type':'string','pattern':'^[a-f0-9]{64}$'}}})


def build(engine,record):
    from .controlled_training_checks import admission
    from .fixed_state_diagnostic import approved,verify_inputs
    from .experiment_decisions import _binding
    from .rapid_experiment_revision import _write_files,_budget
    from .experiment_analysis import freeze_sources
    from .lab import ExperimentRunner
    if not approved(record['assignments']):raise ValueError('Protocol consensus required')
    verify_inputs(engine,record)
    tests=record.get('test_evidence') or {}
    if tests.get('passed') is not True:raise ValueError('Actual CPU regression receipt required')
    for name,digest in tests['files'].items():
        if hashlib.sha256(Path(name).read_bytes()).hexdigest()!=digest:raise ValueError('CPU evidence changed')
    previous=engine.store.get('causal_training_followups',record['id'])
    if previous['status']!='completed':raise ValueError('Original diagnostic must be completed')
    binding=_binding(engine,record['run_id'])[1]
    if binding!=previous['binding']:raise ValueError('Original binding changed')
    prior=previous['candidate']['bundle'];files={}
    for row in prior['files']:
        name=row['path']
        if not (name.startswith(('dataset_runtime/','src/','configs/'))):continue
        raw=(Path(prior['root'])/name).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=row['sha256']:raise ValueError('Frozen runtime changed')
        files[name]=raw
    runtime=[{'path':p,'sha256':hashlib.sha256(v).hexdigest()} for p,v in sorted(files.items())]
    protocol=record['protocol'];states=[]
    for index,item in enumerate(protocol['states']):
        name=f'fixed_inputs/state-{index}.pt';raw=Path(item['path']).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=item['sha256']:raise ValueError('Saved state changed')
        files[name]=raw;states.append({'label':item['label'],'path':name,'sha256':item['sha256']})
    config='fixed_inputs/config.json';files[config]=Path(protocol['config']['path']).read_bytes()
    manifest={'protocol_sha256':record['protocol_sha256'],'config':{'path':config,'sha256':protocol['config']['sha256']},
        'states':states,'runtime_sources':runtime,'source_protocol':previous['protocol'],
        'source_input':previous['protocol']['inputs'][0]}
    def pack(value):return json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False).encode()
    files['diagnostic_inputs.json']=pack(manifest);files['diagnostic_protocol.json']=pack(protocol)
    for local,name in [('fixed_state_probe.py','diagnostic_probe.py'),('fixed_state_math.py','directional_math.py'),
                       ('controlled_diagnostic_guard.py','diagnostic_guard.py')]:
        files[name]=Path(__file__).with_name(local).read_bytes()
    host=json.loads((Path(prior['root'])/'host_evidence.json').read_text(encoding='utf8'))
    # Reuse verified runner excerpts, but replace old causal execution/plan claims.
    host={k:v for k,v in host.items() if k in ('budget_gate','windows_job','runner_environment','launch',
                                             'runner_binding_and_start','runner_monitor','data_call_chain')}
    host.update(scope='Actual host gates and synthetic CPU regressions, not a scientific observation',
        execution={'source':Path(__file__).name,'text':Path(__file__).read_text(encoding='utf8')},
        request=record['authorization'],protocol_sha256=record['protocol_sha256'],
        protocol_consensus={r:sha(a['clarification']['answer'] if a.get('clarification',{}).get('authenticated') else a['answer'])
                            for r,a in record['assignments'].items()},
        budget_at_registration=admission(engine,record['run_id']),test_evidence=tests,
        execution_contract='One new fixed-state observation, zero optimizer steps, no activation, no automatic retries')
    files['host_evidence.json']=pack(host)
    code='from diagnostic_guard import launch\nlaunch()\n';files['experiment.py']=code.encode()
    identity=sha({k:hashlib.sha256(v).hexdigest() for k,v in files.items()})
    bundle=_write_files(engine.run_dir(record['run_id'])/'fixed-state-diagnostic'/identity,files)
    sources=freeze_sources(engine,record['run_id'],{'code':code},bundle)
    paths=['experiment.py','diagnostic_guard.py','diagnostic_probe.py','directional_math.py',
           'diagnostic_protocol.json','diagnostic_inputs.json','host_evidence.json']
    host_paths={Path(inspect.getsourcefile(f)).resolve() for f in (build,ExperimentRunner,_budget,verify_inputs)}
    return {'binding':binding,'code':code,'base_program':previous['base_program'],
        'dataset_assets':previous['dataset_assets'],'original_files':record['protected_files'],
        'host_sources':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in host_paths},
        'admission':admission(engine,record['run_id']),'candidate':{'bundle':bundle,'code_sources':sources,
            'changed_ranges':[{'path':p,'start':0,'end':len(files[p].decode()),'sha256':hashlib.sha256(files[p]).hexdigest()} for p in paths],
            'checks_sha256':hashlib.sha256(code.encode()).hexdigest(),'amendment_sha256':identity},
        'votes':[],'review_instruction':REVIEW,'review_purpose':'rapid_review:fixed-state:'+identity}


def result_matches(record,result,params):
    return (result.get('status')=='completed' and result.get('exit_code')==0
        and result.get('code_sha256')==record['candidate']['checks_sha256'] and result.get('params')==params
        and result.get('limits',{}).get('code_bundle')==record['candidate']['bundle'])


def evidence(engine,run_id):
    """Expose only a completed, authenticated new observation to a NEW archive."""
    from .fixed_state_diagnostic import current
    from .experiment_views import _safe_file
    record=current(engine,run_id)
    if not record or record['status']!='completed':return None
    outcome=record['outcome']
    trial=engine.store.get('experiments',run_id+':'+outcome['trial_id'])
    if not trial or sha(trial)!=outcome['receipt_sha256']:raise ValueError('Fixed-state execution receipt changed')
    path=_safe_file(engine,run_id,trial,'artifacts/fixed_state_diagnostic.json')
    if path is None or hashlib.sha256(path.read_bytes()).hexdigest()!=outcome['report_sha256']:
        raise ValueError('Fixed-state result file changed')
    return {'id':record['id'],'status':'completed','protocol_sha256':record['protocol_sha256'],
        'protocol':record['protocol'],'outcome':outcome,'analyses':record['analyses'],
        'scope':'New fixed-state observations, independently reviewed; no training steps or historical root-cause approval. Reuse, never rerun.'}


def effective_limits(record,assessed,run,required):
    caps=record['protocol']['hard_limits']
    limits={**assessed['limits'],'gpu_allowed':False,'gpu_memory_mb':0,'required_packages':required,
        'execution_mode':run['config'].get('execution_mode','local'),'code_bundle':record['candidate']['bundle'],
        'experiment_binding':{'research_cycle':record['cycle'],'code_revision':record['binding']['revision'],
            'code_sha256':record['candidate']['checks_sha256'],'experiment_plan_sha256':record['binding']['plan_sha256']}}
    for field in ('cpu_cores','memory_mb','timeout_seconds','cpu_seconds','disk_mb'):
        limits[field]=min(limits.get(field) or caps[field],caps[field])
    return limits


async def execute_once(engine,record,save):
    from .engine import ResearchBlocked
    from .controlled_training_checks import unchanged,admission
    from .fixed_state_diagnostic import verify_inputs
    from .rapid_experiment_revision import _budget,_reviewed
    from .training_adjustment import _basis
    from .lab import assess_resources
    from .experiment_views import _safe_file
    run_id=record['run_id'];identifier='fixed-state-check-'+record['candidate']['amendment_sha256'][:24]
    params={'phase':'code_check','seed':29,'dataset':'ETTh1','dataset_assets':record['dataset_assets'],
            'fixed_state_protocol_sha256':record['protocol_sha256']}
    if not _reviewed(record) or not all(x['complete'] for x in record.get('review_reads',[])) or len(record.get('review_reads',[]))!=3:
        raise ResearchBlocked('固定状态候选尚未通过三方代码和实际读取门禁。')
    result=engine.store.get('experiments',run_id+':'+identifier)
    if not result:
        if record.get('trial_id'):raise ResearchBlocked('固定状态诊断已登记派发但无回执；先查明原进程，不重复执行。')
        await engine._read_consensus_check(run_id,unchanged,engine,record)
        await engine._read_consensus_check(run_id,verify_inputs,engine,record)
        resource=_basis(engine,run_id)['design']['resource_plan']
        required=sorted(set(resource.get('required_packages',[])+record['base_program'].get('dependencies',[])))
        await engine._read_consensus_check(run_id,engine._execution_gate,run_id)
        assessed=assess_resources({**resource,'required_packages':required},await engine.execution_hardware(run_id))
        if not assessed['approved']:raise ResearchBlocked('固定状态诊断未通过原资源准入。')
        limits=effective_limits(record,assessed,engine.store.get('runs',run_id),required)
        if admission(engine,run_id)!=record['admission']:raise ResearchBlocked('实际实验额度台账变化，不能沿用旧准入。')
        _budget(engine,run_id,1)
        engine.check_active(run_id)
        # Commit the unique dispatch before launch. Unknown outcomes never launch twice.
        save(status='executing',trial_id=identifier,execution_authorized=True,effective_limits=limits,
             resource_admission=assessed,dispatch_at=now())
        result=await engine.runner.run(run_id,record['code'],params,identifier,limits,engine.cancellations[run_id])
    if not result_matches(record,result,params):
        save(status='execution_failed',result_receipt_sha256=sha(result))
        raise ResearchBlocked('固定状态诊断失败或执行状态未确认；保留原回执，不重跑。')
    await engine._read_consensus_check(run_id,unchanged,engine,record)
    path=_safe_file(engine,run_id,result,'artifacts/fixed_state_diagnostic.json')
    if path is None:raise ResearchBlocked('固定状态诊断缺真实结果文件。')
    raw=path.read_bytes();report=json.loads(raw);digest=hashlib.sha256(raw).hexdigest()
    if (report.get('protocol')!=record['protocol'] or report.get('protocol_sha256')!=record['protocol_sha256']
        or report.get('execution_status')!='completed' or report.get('optimizer_steps')!=0 or report.get('fit_calls')!=0
        or report.get('input_hashes_unchanged') is not True or result.get('experiment_metadata',{}).get('diagnostic_report_sha256')!=digest
        or len(report.get('states',[]))!=3 or not all(r.get('original_state_unchanged') is True for r in report['states'])):
        raise ResearchBlocked('固定状态结果未通过协议、身份和完整性核验。')
    outcome={'trial_id':identifier,'receipt_sha256':sha(result),'report_sha256':digest,
        'summary':report['summary'],'report':report,'scientific_approval':False}
    save(status='reviewing_results',outcome=outcome)


async def review_results(engine,harness,record,save):
    from .engine import ResearchBlocked
    from .experiment_decisions import _archive
    from .fixed_state_diagnostic import ROLES,validate_reads
    if not record.get('result_context'):
        ref=_archive(engine,record['run_id'],{'binding':record['binding'],
            'fixed_state_protocol':record['protocol'],'fixed_state_result':record['outcome']})
        save(result_context={'training_context_protocol':1,'experiment_workflow_protocol':1,'research_cycle':record['cycle'],
            'validation_evidence':ref,'evidence_sha256':ref['sha256'],
            'experiment_decision':{'fixed_state_result_review':True,'scientific_approval':False}})
    context=record['result_context']
    assignments=record.setdefault('result_assignments',{})
    for role in ROLES:
        assignments.setdefault(role,{'purpose':'experiment_diagnose:fixed-state-result:'+context['evidence_sha256'],
            'instruction':RESULT_INSTRUCTION,'context':copy.deepcopy(context),'schema':result_schema(context['evidence_sha256'])})
    save()
    async def one(role):
        a=assignments[role]
        if 'answer' not in a:
            a['answer']=await harness.ask(record['run_id'],role,a['purpose'],a['instruction'],a['context'],a['schema']);save()
        if len({s['label'] for s in a['answer']['states']})!=3:raise ValueError('Each saved state needs its own finding')
        a['read']=await engine._read_consensus_check(record['run_id'],validate_reads,engine,record,role,a,RESULT_PATHS);save()
    results=await asyncio.gather(*(one(r) for r in ROLES),return_exceptions=True)
    for value in results:
        if isinstance(value,BaseException):raise value
    analyses=[{'role':r,**a['answer']} for r,a in assignments.items()]
    if any(not a['valid_observation'] or a['issues'] for a in analyses):
        save(analyses=analyses)
        raise ResearchBlocked('固定状态结果复核仍有具体异议；保留各方回执，须解决后续解释，禁止重跑。')
    signatures=[sorted((s['label'],s['finding']) for s in a['states']) for a in analyses]
    if any(s!=signatures[0] for s in signatures[1:]):
        save(analyses=analyses)
        raise ResearchBlocked('三方对局部判据解释不同；保留争议等待同证据共识，不自动扩大实验。')
    save(status='completed',analyses=analyses,completed_at=now(),scientific_approval=False)
    engine.store.event(record['run_id'],'fixed_state.completed','单次固定状态诊断及三方结果复核完成；保留未知根因和原科学门禁',
        {'trial_id':record['trial_id'],'report_sha256':record['outcome']['report_sha256']})
    return {'tuning_pending':True,'next_node':'tuning','activity':'fixed_state_diagnostic',
            'fixed_state_diagnostic_id':record['id'],'scientific_approval':False}


async def continue_diagnostic(engine,harness,record,save):
    from .fixed_state_diagnostic import ROLES
    from .controlled_training_checks import unchanged,review_context
    from .controlled_review_completion import complete
    from .rapid_experiment_revision import VOTE,_reviewed
    from .engine import ResearchBlocked
    run_id=record['run_id'];engine.check_active(run_id)
    if 'candidate' not in record:
        built=await engine._read_consensus_check(run_id,build,engine,record)
        save(**built,status='reviewing_code')
    await engine._read_consensus_check(run_id,unchanged,engine,record)
    if record['status'] in ('reviewing_code','code_rejected','code_approved'):
        context=review_context(engine,record)
        if len(record['votes'])<3:
            async def one(role):
                if any(v['role']==role for v in record['votes']):return
                answer=await harness.ask(run_id,role,record['review_purpose'],REVIEW,context,VOTE)
                record['votes'].append({'role':role,**answer});save()
            results=await asyncio.gather(*(one(r) for r in ROLES),return_exceptions=True)
            for value in results:
                if isinstance(value,BaseException):raise value
        reads=await complete(engine,harness,record,context,save)
        save(review_reads=reads)
        if not _reviewed(record) or len(reads)!=3 or not all(r['complete'] for r in reads):
            save(status='code_rejected')
            raise ResearchBlocked('固定状态候选代码未获三方完整独立批准；不执行。')
        save(status='code_approved')
    if record['status'] in ('code_approved','executing','execution_failed'):
        await execute_once(engine,record,save)
    if record['status']=='reviewing_results':return await review_results(engine,harness,record,save)
    raise ResearchBlocked('固定状态诊断状态未确认，禁止重复派发。')
