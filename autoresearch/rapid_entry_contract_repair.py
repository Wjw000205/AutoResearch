"""One author repair of a check/validation parameter mix-up, with a fresh review."""
import copy
import hashlib
from pathlib import Path

from .experiment_workflow import sha
from .storage import now


def local_counter(checks):
    """Alpha-rename an author's local set; never touch the host counter."""
    import ast
    import io
    import tokenize
    names={n.id for n in ast.walk(ast.parse(checks)) if isinstance(n,ast.Name)}
    if not any(n.startswith('__rapid_') for n in names): return checks
    if {n for n in names if n.startswith('__rapid_')} != {'__rapid_passed'} or 'entry_conditions_checked' in names:
        raise ValueError('Unexpected use of reserved host assertion names')
    tokens=[]
    for token in tokenize.generate_tokens(io.StringIO(checks).readline):
        if token.type==tokenize.NAME and token.string=='__rapid_passed':
            token=token._replace(string='entry_conditions_checked')
        tokens.append(token)
    return tokenize.untokenize(tokens)


def eligible(record):
    return (record.get('status') in {'checking','rejected','check_failed'} and not record.get('entry_contract_repair')
        and "base = entry.load([str(params_path)])" in record.get('patch',{}).get('checks','')
        and "base['phase'] == 'validation'" in record.get('patch',{}).get('checks',''))


async def repair(engine, harness, run_id, record, authorization):
    from . import rapid_experiment_revision as rapid
    from .experiment_decisions import _archive
    from .training_adjustment import obj
    rapid._base_unchanged(engine,run_id,record)
    if not record.get('entry_contract_repair'):
        if not authorization or not eligible(record): raise ValueError('No authorized bounded entry repair')
        rapid._candidate_unchanged(record)
        failed = engine.store.get('experiments',run_id+':'+record.get('check',{}).get('id',''))
        if record.get('check') and (not failed or sha(failed)!=record['check']['receipt_sha256']):
            raise ValueError('Original failed check receipt changed')
        original = copy.deepcopy(record)
        ref = _archive(engine,run_id,{'rapid_revision_attempt':original,'failed_check':failed})
        config = Path(record['candidate']['bundle']['root'])/'dataset_runtime/configurations.json'
        contract = {'actual_host_params':{'phase':'code_check','rapid_revision_id':record['id']},
            'resource_caps':{'timeout_seconds':60,'cpu_seconds':60,'cpu_cores':1,'memory_mb':1024,'disk_mb':256},
            'frozen_config_path':'dataset_runtime/configurations.json',
            'frozen_config_sha256':hashlib.sha256(config.read_bytes()).hexdigest(),
            'selectors':{'dataset':'ETTh1','variant':'proposed','seed':17,'phase':'validation'},
            'actual_loader_contract':'load requires explicit seed, variant and validation/test phase. Host params are a code_check invocation, not a validation config.'}
        context=rapid._review_context(engine,run_id,record)
        context['rapid_revision']['entry_contract_repair']={'original':ref,'authorization':authorization,
            'host_contract':contract,'original_checks':record['patch']['checks'],
            'scope':'Only fix check fixture/entry plumbing and its deadline; preserve candidate runtime, all scientific settings and behavioral assertions.'}
        record['entry_contract_repair']={'original':ref,'context':context,'created_at':now(),
            'purpose':'rapid_implement:entry-contract:'+ref['sha256']}
        record['status']='entry_repairing';engine.store.put(rapid.COLLECTION,record['id'],record)
    audit=record['entry_contract_repair']
    archived=engine.store.get('experiment_evidence',audit['original']['id'])
    if sha(archived['summary'])!=audit['original']['sha256']: raise ValueError('Entry repair history changed')
    original=archived['summary']['rapid_revision_attempt']
    from .rapid_output_length import ask
    schema=obj({'checks':{'type':'string','minLength':1},'rationale':rapid.TEXT})
    instruction='''只修正CPU检查入口契约，不再补日志或改候选源码。entry_contract_repair 给出了原checks和真实宿主参数/资源上限。
sys.argv[1] 的原params.json只含phase=code_check与rapid_revision_id，不能传给验证配置loader；原始文件必须保持不变。
从现有冻结dataset_runtime/configurations.json按给定SHA核验，使用现有entry.load的--config/--dataset/--variant/--seed/--phase解析冻结ETTh1 proposed验证配置；另写隔离CPU夹具配置文件来验证legacy入口与command_arguments，两种入口必须解析一致。
保留原19项行为检查的实际含义，包括原版SHA逆向校验、24例/96次微型更新、三版本逐步对照、四类故障、RNG与配置不变。只调整引用了错误文件的入口断言，不能删除行为检查或把候选当原版；检查资源从紧遵守60秒/1 CPU/1 GiB，不要求扩大宿主限制。不能读取真实数据或重新训练原任务。
返回完整checks与修复理由；不嵌入整份配置，不重复读取旧补丁，不声称已执行。随后重新三方审阅同一新检查SHA。'''
    answer=await ask(engine,harness,run_id,audit['purpose'],instruction,audit['context'],schema)
    adapted=local_counter(answer['checks'])
    if adapted!=answer['checks']:
        audit['host_name_adaptation']={'source_response_sha256':sha(answer),
            'original_checks_sha256':hashlib.sha256(answer['checks'].encode()).hexdigest(),
            'adapted_checks_sha256':hashlib.sha256(adapted.encode()).hexdigest(),
            'scope':'Token-only alpha rename of author-local __rapid_passed; no assertion or runtime semantics changed.'}
    patch={**copy.deepcopy(original['patch']),'checks':adapted,
           'check_rationale':answer['rationale']}
    candidate=await engine._read_consensus_check(run_id,rapid.build_candidate,engine,run_id,record,patch)
    if candidate['bundle']['sha256']!=original['candidate']['bundle']['sha256']:
        raise ValueError('Entry repair changed candidate runtime')
    if "base = entry.load([str(params_path)])" in patch['checks']:
        raise ValueError('Check still treats host invocation as validation configuration')
    rapid._base_unchanged(engine,run_id,record)
    for k in ('check','review_reads','review_purpose','review_clarifications','source_read_recovery','review_history','error','completed_at'):
        record.pop(k,None)
    record.update(patch=patch,candidate=candidate,votes=[],status='reviewing',
        review_purpose='rapid_review:'+candidate['amendment_sha256']+':entry-contract')
    audit.update(completed_at=now(),response_sha256=sha(answer))
    engine.store.put(rapid.COLLECTION,record['id'],record)
    engine.store.event(run_id,'rapid.entry_contract_repaired','工程师已分离宿主检查参数与冻结验证配置；保留原补丁和回执，重新独立复审',
                       {'revision_id':record['id'],'original':audit['original']})
