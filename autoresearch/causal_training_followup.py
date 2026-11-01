"""One explicitly requested, independently planned single-seed causal contrast."""
import copy
import hashlib
import json
from pathlib import Path

from .prose_schema import validate
from .experiment_workflow import sha
from .storage import now
from .training_adjustment import obj, _group

COLLECTION = 'causal_training_followups'
TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 1800}
PLAN = obj({'seed': {'enum': [17, 29]}, 'steps': {'enum': [64, 96, 128, 192, 256]},
    'evaluation_every': {'enum': [8, 16, 32, 64]}, 'lower_lr_factor': {'enum': [.1, .25, .5]},
    'rationale': TEXT, 'overfitting_prediction': TEXT, 'instability_prediction': TEXT,
    'inconclusive_rule': TEXT, 'limitations': TEXT})
READS = {'type':'array','minItems':1,'maxItems':32,'uniqueItems':True,
         'items':{'type':'string','pattern':'^[a-f0-9]{64}$'}}
SUGGEST = obj({'plan': PLAN, 'necessary_evidence': TEXT, 'why_not_more_logging': TEXT, 'read_receipts': READS})
DESIGN = '''为用户要求制定一次最小、可判别的单种子受控训练对照；不是再补日志。
先用 experiments.compare 读 /causal_basis 的全部分页并返回真实 read_receipts，复用完整训练统计、上次32步反事实与已通过CPU集成检查。
宿主支持的精确模板：选一个已保存最佳检查点及其原Adam/随机状态；固定原模型、训练/验证划分，
跳过上一受控检查已执行的32批，核验原32批哈希，使用之后的同一新批次序列，比较原学习率与一个较低学习率；
两个分支同起点、同批次、同原参数，唯一差异为学习率。零更新对照贯穿全程。
选择64–256步及等间隔完整训练集/验证集评估；至少4个非零观察点。新计划仅一个种子，不重跑原训练或原32步对照，不读test。
固定判别模板：两更新分支至少75%且至少3个评估点训练MSE比起点下降>0.1%、验证MSE上升>1%，
同时无>5倍更新范数中位数的尖峰，支持局部泛化权衡；原学习率在至少2点训练/验证同时上升>5%
并伴同期更新尖峰，而降学习率将两项升幅均压至一半以内，支持局部更新不稳定；
原分支更新后非有限且更新前有限、降学习率和零更新稳定，支持该单种子数值不稳定。
缺少完整观测、对照不稳、证据混合或均不满足，结论必须inconclusive，不能改阈值迎合结果。
这些是本次诊断判据，不修改原科研验收。较低学习率最终MSE较好本身不是根因证据。
选择最小有信息量的步数，并说明相对上次无定论结果的增量、可证伪预测与局限。
CPU集成检查通过只证明接口可靠，不能当作本次结果。工具无须重复已覆盖读取，方案随后由三方独立评审代码和协议。'''
REVIEW = '''独立评审本次单种子最小因果对照的协议与全部 changed_ranges 源码，实际使用 experiments.inspect_module(snapshot=candidate,view=source) 读完并返回回执。
用 experiments.compare 读取 /controlled_admission 全部分页，核对宿主真实台账和资源；不继承提案批准。
核对原检查点/Adam/RNG/划分身份、跳过原32批及其哈希、相同新批次序列、唯一学习率变化、零更新对照、全训练/验证同口径测量、预注册判断及允许inconclusive。
不得重跑完成的正式训练或原32步对照，不使用test，不激活参数或改冻结计划。新计划严格一个种子。
每分支最多256次新更新，资源上限2 CPU、4 GiB、600秒并受宿主原门禁限制。每分支保存续接状态与原始曲线。
只有具备区分局部泛化权衡与更新不稳定的对照能力才批准；仅补日志或日志格式检查应拒绝。一个种子不能声称跨种子稳定、全局根因已证明或最终科研验收通过。
批准只授权这个SHA绑定的单批诊断。拒绝需给出具体阻断缺项，不因为结果可能无定论而要求重复已完成工作。'''


def register(engine, run_id, cpu_revision_id, authorization):
    key = f'{run_id}:{engine.cycle(run_id)}'
    old = engine.store.get(COLLECTION, key)
    if old:
        if old['authorization'] != authorization or old['cpu_revision_id'] != cpu_revision_id:
            raise ValueError('Do not replace an existing follow-up request')
        return old
    revision = engine.store.get('rapid_experiment_revisions', cpu_revision_id)
    if not authorization or not revision or revision['run_id'] != run_id:
        raise ValueError('An explicit request and original CPU revision are required')
    r = {'id': key, 'run_id': run_id, 'cycle': engine.cycle(run_id), 'status': 'waiting_for_cpu',
        'cpu_revision_id': cpu_revision_id, 'authorization': authorization, 'binding': revision['binding'],
        'created_at': now(), 'scientific_approval': False}
    engine.store.put(COLLECTION, key, r)
    return r


def protocol_for(plan, inputs):
    validate(plan, PLAN)
    if plan['steps'] % plan['evaluation_every'] or plan['steps'] // plan['evaluation_every'] < 4:
        raise ValueError('Need at least four evenly spaced nonzero evaluations')
    return {'version': 4, 'seeds': [plan['seed']], 'steps': plan['steps'], 'batch_offset': 32,
        'lr_factors': [1., plan['lower_lr_factor']], 'evaluation_every': plan['evaluation_every'],
        'evaluation_steps': list(range(plan['evaluation_every'], plan['steps'] + 1, plan['evaluation_every'])),
        'historical_metric_atol': 1e-6, 'historical_metric_rtol': 1e-4, 'inputs': inputs,
        'limits': {'timeout_seconds': 600, 'cpu_seconds': 600, 'cpu_cores': 2, 'memory_mb': 4096, 'disk_mb': 256},
        'agent_plan': copy.deepcopy(plan), 'scope': 'One single-seed follow-up; no old training/probe replay or parameter activation.'}


def _build(engine, record):
    from .controlled_training_checks import admission
    from .rapid_experiment_revision import _write_files
    from .experiment_analysis import freeze_sources
    from .causal_probe_template import sources
    previous = engine.store.get('controlled_training_checks', record['id'])
    if not previous or previous['status'] != 'completed':
        raise ValueError('The prior completed control must remain available')
    plan = record['plan']; prior = previous['candidate']['bundle']; files = {}
    # Exact original runtime and one checkpoint; never copy held-out outputs.
    for row in prior['files']:
        name = row['path']
        if name.startswith('controlled_inputs/') and not name.startswith(f'controlled_inputs/{plan["seed"]}/'):
            continue
        raw = (Path(prior['root']) / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != row['sha256']:
            raise ValueError('Previously reviewed source/checkpoint changed')
        files[name] = raw
    item = copy.deepcopy(next(x for x in previous['protocol']['inputs'] if x['seed'] == plan['seed']))
    trial = next(t for t in previous['outcome']['summary']['trials'] if t['seed'] == plan['seed'])
    prefixes = {b['batch_sequence_sha256'] for b in trial['branches']}
    if len(prefixes) != 1:
        raise ValueError('Original diagnostic batches are not matched')
    item['prior_probe_batch_sha256'] = prefixes.pop()
    protocol = protocol_for(plan, [item]); files.update(sources(protocol))
    files['diagnostic_protocol.json'] = json.dumps(protocol, ensure_ascii=False, indent=2).encode()
    # The runner writes bootstrap.json from the final admitted limits. It is a
    # reserved host control file and must never be supplied by a code bundle.
    import inspect
    from .lab import ExperimentRunner
    from .rapid_experiment_revision import _budget
    host = json.loads(files['host_evidence.json'])
    host.update({'scope': 'Current protocol and actual host limits; not scientific results',
        'request': record['authorization'], 'prerequisite_cpu_check': record['cpu_check'],
        'prior_result_sha256': previous['outcome']['report_sha256'], 'new_plan_single_seed': True,
        'execution_contract': 'CPU-only, bound code/plan/data, independently reviewed once; existing result is never relaunched.',
        'execution': {'source': Path(__file__).name, 'text': inspect.getsource(_execute_reviewed),
                      'file_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
        'budget_at_registration': admission(engine, record['run_id']),
        'test_evidence': record.get('test_evidence')})
    files['host_evidence.json'] = json.dumps(host, ensure_ascii=False, indent=2).encode()
    code = 'from diagnostic_guard import launch\nlaunch()\n'; files['experiment.py'] = code.encode()
    identity = sha({k: hashlib.sha256(v).hexdigest() for k, v in files.items()})
    bundle = _write_files(engine.run_dir(record['run_id']) / 'causal-followup' / identity, files)
    source = freeze_sources(engine, record['run_id'], {'code': code}, bundle)
    changed = ['experiment.py', 'diagnostic_probe.py', 'diagnostic_protocol.json', 'diagnostic_verdicts.py', 'diagnostic_guard.py', 'diagnostic_persistence.py', 'host_evidence.json']
    return {'protocol': protocol, 'code': code, 'admission': admission(engine, record['run_id']),
        'original_evidence': previous['original_evidence'], 'original_files': previous['original_files'],
        'base_program': previous['base_program'], 'dataset_assets': previous['dataset_assets'],
        'host_sources': {str(Path(inspect.getsourcefile(fn)).resolve()): hashlib.sha256(Path(inspect.getsourcefile(fn)).read_bytes()).hexdigest()
                         for fn in (_execute_reviewed, ExperimentRunner, _budget)},
        'candidate': {'bundle': bundle, 'code_sources': source, 'changed_ranges': [
            {'path': p, 'start': 0, 'end': len(files[p].decode()), 'sha256': hashlib.sha256(files[p]).hexdigest()} for p in changed],
            'checks_sha256': hashlib.sha256(code.encode()).hexdigest(), 'amendment_sha256': identity}}


def _plan_reads(engine, record, answer, context, purpose):
    from .engine import Harness
    from .experiment_tools import compare
    key = Harness.decision_key(record['run_id'], answer['role'], purpose, DESIGN, context, SUGGEST)
    frozen = engine.store.get('handoff_inputs', key) or {}
    owner = engine.store.get('handoffs', key) or {}
    saved = (engine.store.get('decisions', key) or {}).get('result')
    if (frozen.get('context') != context or owner.get('to_role') != answer['role']
            or owner.get('run_id') != record['run_id'] or saved != {k:v for k,v in answer.items() if k != 'role'}):
        raise ValueError('Plan must belong to the exact role and frozen request')
    refs = set(answer['read_receipts']); valid = set(); windows = []
    for row in (engine.store.get('handoff_progress', key) or {}).get('tool_results', []):
        call, result = row.get('call', {}), row.get('result', {})
        receipt = result.get('receipt_sha256'); body = {k:v for k,v in result.items() if k != 'receipt_sha256'}
        if receipt not in refs or receipt != sha({'call':call,'result':body}): continue
        if call.get('name') != 'experiments.compare' or compare(engine,record['run_id'],context,call['arguments']) != body: continue
        valid.add(receipt)
        if call['arguments'].get('path') == '/causal_basis': windows.append(result)
    end = 0
    for window in sorted(windows, key=lambda w:w['offset']):
        if window['offset'] > end: break
        end = max(end, window['end'])
    if valid != refs or not windows or end != windows[0]['total_chars']:
        raise ValueError('Every planner must read the full causal basis')
    return {'role': answer['role'], 'handoff_id': key, 'receipts': sorted(valid), 'complete': True}


def _basis_context(engine, record):
    from .experiment_decisions import _archive
    from .training_statistics import critical_packet
    previous = engine.store.get('controlled_training_checks', record['id'])
    if not previous or previous['status'] != 'completed': raise ValueError('Prior diagnostic is not complete')
    original = engine.store.get('experiment_evidence', previous['original_evidence']['id'])
    if sha(original['summary']) != previous['original_evidence']['sha256']: raise ValueError('Prior evidence changed')
    # Original evidence remains paged and inspectable; no full transcript copies.
    summary = copy.deepcopy(original['summary'])
    summary['causal_basis'] = {'request': record['authorization'], 'current_binding': record['binding'],
        'prior_controlled_result': previous['outcome']['summary'], 'prior_three_role_findings': previous.get('analyses'),
        'cpu_integration_check': record['cpu_check'], 'original_training_evidence': previous['original_evidence'],
        'critical_training_statistics': [critical_packet(summary,t) for t in
            summary.get('training_investigation',{}).get('focus_trial_ids',[])],
        'constraints': {'new_plan_seed_count':1, 'old_probe_steps_to_skip':32, 'no_held_out':True,
                        'no_training_replay':True,'frozen_plan_unchanged':True},
        'supported_plan_schema': PLAN}
    ref = _archive(engine, record['run_id'], summary)
    return {'training_context_protocol':1,'experiment_workflow_protocol':1,'research_cycle':record['cycle'],
        'validation_evidence':ref,'evidence_sha256':ref['sha256'],
        'experiment_decision':{'causal_followup':True,'scientific_approval':False}}


async def execute(engine, harness, run_id):
    record = engine.store.get(COLLECTION, f'{run_id}:{engine.cycle(run_id)}')
    if not record or record['status'] == 'completed': return None
    from .rapid_experiment_revision import _check_passed
    from .experiment_decisions import _binding, _archive
    from .engine import ResearchBlocked
    revision = engine.store.get('rapid_experiment_revisions', record['cpu_revision_id'])
    from .rapid_entry_contract_repair import eligible, repair
    if revision and (eligible(revision) or revision.get('status') == 'entry_repairing'):
        await repair(engine,harness,run_id,revision,record['authorization'])
    if revision and revision.get('entry_contract_repair') and revision['status'] in {'reviewing','checking'}:
        from .rapid_experiment_revision import run as complete_patch
        diagnostic=engine.store.get('tuning_diagnostics',record['id'])
        result=await complete_patch(engine,harness,run_id,diagnostic['review'])
        return {**result,'tuning_pending':True,'next_node':'tuning','activity':'diagnostic_repair'}
    if revision and revision['status'] in {'rejected','check_failed','invalid'}:
        raise ResearchBlocked('前置补丁仍有具体审阅或CPU检查缺项；保留回执，不重复补日志或启动训练。')
    if not revision or revision['status'] != 'diagnostic_checked': return None
    if not _check_passed(engine,run_id,revision): raise ResearchBlocked('因果对照前置CPU检查回执不完整；不执行。')
    if _binding(engine,run_id)[1] != record['binding']: raise ResearchBlocked('因果对照的原冻结计划或源码绑定变化；不执行。')
    engine.check_active(run_id)
    def save(**kw):
        record.update(kw,updated_at=now()); engine.store.put(COLLECTION,record['id'],record)
    if 'cpu_check' not in record:
        save(cpu_check={'revision_id':revision['id'],**revision['check']}, status='planning')
    if 'plan' not in record:
        context = _basis_context(engine,record)
        purpose = 'experiment_diagnose:causal-plan:' + context['evidence_sha256']
        if not record.get('suggestions'):
            answers = await _group(harness,run_id,purpose,DESIGN,context,SUGGEST)
            reads = [_plan_reads(engine,record,a,context,purpose) for a in answers]
            save(suggestions=answers,planning_reads=reads,planning_context=context)
        # The engineer consolidates independent predictions; nobody's plan authorizes execution.
        consolidated = copy.deepcopy(engine.store.get('experiment_evidence',context['validation_evidence']['id'])['summary'])
        consolidated['causal_basis']['independent_proposals'] = record['suggestions']
        ref = _archive(engine,run_id,consolidated)
        c = {**context,'validation_evidence':ref,'evidence_sha256':ref['sha256']}
        response = await harness.ask(run_id,'engineer','experiment_decide:causal-plan:'+ref['sha256'],
            '综合三方独立方案，选择最小单种子对照；保留预注册判据和允许无定论，不增加日志任务。先读取 /causal_basis。返回计划。',c,PLAN)
        protocol_for(response, [])
        save(plan=response)
    if 'candidate' not in record:
        built = await engine._read_consensus_check(run_id,_build,engine,record)
        from .causal_review_repair import GUIDANCE
        save(**built, votes=[],status='reviewing',review_instruction=(GUIDANCE+'\n'+REVIEW if record.get('prior_review') else REVIEW),
             review_purpose='rapid_review:causal:'+built['candidate']['amendment_sha256'])
    return await _execute_reviewed(engine,harness,run_id,record,save)


async def _execute_reviewed(engine, harness, run_id, record, save):
    from .engine import ResearchBlocked
    from .rapid_experiment_revision import VOTE, _reviewed, _budget
    from .controlled_training_checks import (unchanged, review_context, admission,
        AFTER, AFTER_INSTRUCTION, verify_result_reads)
    await engine._read_consensus_check(run_id,unchanged,engine,record)
    context=review_context(engine,record)
    purpose=record['review_purpose']
    if not record['votes']:
        if record.get('prior_review'):
            import asyncio
            from .causal_review_repair import context_for
            contexts={role:context_for(engine,record,context,role) for role in ('director','engineer','scout')}
            save(review_contexts=contexts)
            async def ask(role):
                return {'role':role,**await harness.ask(run_id,role,purpose,record['review_instruction'],contexts[role],VOTE)}
            votes=await asyncio.gather(*(ask(role) for role in contexts))
        else:
            votes=await _group(harness,run_id,purpose,record['review_instruction'],context,VOTE)
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
    identifier='causal-check-'+record['candidate']['amendment_sha256'][:24]
    params={'phase':'code_check','dataset':'ETTh1','dataset_assets':record['dataset_assets'],
            'causal_followup_id':record['id']}
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
    engine.store.event(run_id,'causal_followup.completed','已执行单次受控诊断并完成三方结果复核；未激活参数或重复原训练',
        {'trial_id':identifier,'report_sha256':outcome['report_sha256']})
    return {'tuning_pending':True,'next_node':'tuning','activity':'causal_diagnostic','causal_followup_id':record['id'],
            'scientific_approval':False}
