"""Explicit scope-stage facts for new requests; no approval or execution claims."""
import copy

from .experiment_workflow import sha

GUIDANCE = '''
调参范围审查的宿主阶段合同见 tuning_scope_design.stage_contract：
1. 本轮只批准有限单因素范围。fixed 表示本轮保持控制值；硬编码或未被消费的字段可以保持 fixed，
   在 runtime_support 如实解释。只有本轮确实要改变且当前不支持的候选才列 requires_implementation；
   不能要求为不会改变的固定项新增实现，也不能把未实现候选当作完成搜索。
2. runtime_audit 状态为 not_supplied 时，表示本次没有附带匹配的独立动态能力审计。
   runtime_audit_path/runtime_route_paths 是请求上下文里的导航字段，不是 frozen evidence 中同名 JSON 指针。
   仅当状态为 available 才读取给出的 evidence_path；不要反复查询不存在的路径来证明“不适用”。
   source_route_paths 提供当前快照中确实存在的入口文件，按具体核验需要读取；目录不代替正文。
3. 静态保存审计、动态 CPU 行为检查、候选训练资源检查各自保留证据范围。
   范围批准不宣称全部候选已运行；每个候选仍需独立批次、YAML、资源及执行门禁。
   具体历史检查的限制以其真实回执为准；task_limits 与 full_acceptance_policy 是不同适用范围。
   task_limits 只是任务上界，不替换既有计划中更严格的资源额度。另行提出的新CPU检查预算，
   不能用来追溯判定另一类已完成检查失败；也不能把完整验收预算自动借给新检查。
   historical_cpu_check 若 available，是宿主按冻结回执SHA核验的原检查摘要。旧 experiments.read(metrics)
   只展示val_*，对code_check返回空表并不代表原回执没有断言结果；按本摘要的指标与原资源额度核对。
   可用其check_source导航检查脚本，但这些候选/验收源码不能替代当前运行源码必读范围。
   上下文准备的120秒保护不是实验或完整CPU验收时限，不得用它改写任务7200秒上限。
4. compare/inspect_module/read/analyze/hyperparameters.read 的真实回执均可引用。
   单个实验记录与分析是补充证据，不替代必读 inventory、当前计划与引用源码的完整分页。
   宿主读取错误只修具体回执和缺页，不作为重写105项参数计划的科研理由。
保留真实异议，不能因为合同澄清就自动批准；已解决的问题不重复要求全量盘点或重跑已有实验。
'''


def historical_check(engine, run_id, evidence):
    revision = evidence.get('rapid_revision') or {}
    ref = revision.get('check') or {}
    if not ref.get('id') or not ref.get('receipt_sha256'):
        return {'status': 'not_supplied'}
    trial = engine.store.get('experiments', run_id + ':' + ref['id']) or {}
    if (trial.get('run_id') != run_id or trial.get('params', {}).get('phase') != 'code_check'
            or sha(trial) != ref['receipt_sha256']):
        raise ValueError('Frozen CPU check receipt changed')
    from .rapid_check_budget import CAPS
    limits = trial.get('limits', {})
    result = {'status': 'available', 'trial_id': ref['id'], 'revision_id': revision.get('id'),
        'receipt_sha256': ref['receipt_sha256'], 'trial_status': trial.get('status'),
        'exit_code': trial.get('exit_code'), 'code_sha256': trial.get('code_sha256'),
        'metrics': {k: trial.get('metrics', {}).get(k) for k in ('rapid_check_passed', 'rapid_assertions')},
        'limits': {k: limits.get(k) for k in ('timeout_seconds', 'cpu_seconds', *CAPS)},
        'observed': {k: trial.get(k) for k in ('duration_seconds', 'cpu_seconds', 'peak_memory_mb')},
        'check_source': {'snapshot': 'candidate', 'path': '_delivery/checks.py',
            'source_ref': revision.get('code_sources')},
        'scope': 'Saved host CPU acceptance only; not a fresh parameter trial or complete candidate-grid verification',
        'scientific_approval': False}
    # This lookup is bounded to one revision and returns only its small audit ref.
    import json
    with engine.store._read_db() as db:
        row = db.execute("SELECT json_extract(data,'$.post_exit_verification') FROM records "
            "WHERE collection='rapid_experiment_revisions' AND key=?", (revision.get('id'),)).fetchone()
    post_ref = json.loads(row[0]) if row and row[0] else None
    if post_ref:
        post = engine.store.get('rapid_post_exit_verifications', post_ref['id']) or {}
        binding = post.get('binding', {})
        valid = (sha(post) == post_ref.get('sha256') and post.get('run_id') == run_id
            and binding.get('revision_id') == revision.get('id') and binding.get('trial_id') == ref['id']
            and binding.get('runner_receipt_sha256') == ref['receipt_sha256']
            and binding.get('checks_sha256') == trial.get('code_sha256'))
        result['post_exit_verification'] = {'reference': post_ref, 'identity_verified': valid,
            'post_exit_verified': bool(valid and post.get('result', {}).get('post_exit_verified'))}
    return result


def context(engine, run_id, value):
    if not value.get('experiment_decision', {}).get('tuning_scope_design'):
        return value
    from .tuning_scope_reads import _snapshot
    evidence = _snapshot(engine, run_id, value)
    run = engine.store.get('runs', run_id) or {}
    if evidence.get('binding', {}).get('config_sha256') not in (None, sha(run.get('config'))):
        raise ValueError('Scope contract task configuration changed')
    source_ref = evidence.get('code_sources') or {}
    source = engine.store.get('experiment_sources', source_ref.get('id', '')) or {}
    if source.get('run_id') != run_id or sha(source.get('files')) != source_ref.get('sha256'):
        raise ValueError('Scope contract source snapshot changed')
    audit = evidence.get('tuning_runtime_audit')
    route = [p for p in ('experiment.py', 'dataset_runtime/config.py', 'dataset_runtime/adapter.py',
        'dataset_runtime/hyperparameters.py', 'dataset_runtime/contracts.py', 'dataset_runtime/training.py')
        if p in source['files']]
    body = {'version': 1, 'scope': 'parameter_range_review_only', 'binding': evidence['binding'],
        'runtime_audit': {'status': 'available' if audit else 'not_supplied',
            'evidence_path': '/tuning_runtime_audit' if audit else None,
            'reason': 'Only a matching frozen dynamic audit is exposed; absence is not a claim of failure or execution.'},
        'source_route_paths': route, 'source_ref': source_ref,
        'task_limits': {k: run.get('config', {}).get(k) for k in
            ('experiment_timeout', 'cpu_limit_cores', 'ram_limit_gb', 'vram_limit_gb')},
        'full_acceptance_policy': copy.deepcopy(run.get('state', {}).get('full_acceptance_check_budget')),
        'historical_cpu_check': historical_check(engine, run_id, evidence),
        'fixed_unsupported_parameters_need_implementation': False,
        'per_candidate_checks_remain_required': True, 'execution_authorized': False, 'scientific_approval': False}
    result = copy.deepcopy(value)
    result['experiment_decision']['tuning_scope_design']['stage_contract'] = {**body, 'sha256': sha(body)}
    return result


def prepare(engine, run_id, prepared):
    if not prepared['context'].get('experiment_decision', {}).get('tuning_scope_design'):
        return prepared
    # Called only while building an unsent continuation. Paid packets are
    # recovered before this projection and stay byte-identical.
    result = copy.deepcopy(prepared)
    result['context'] = context(engine, run_id, result['context'])
    if GUIDANCE not in result['task']:
        result['task'] += GUIDANCE
    return result


def cited_check_summary(engine, run_id, context, refs):
    """An actually delivered host checksum is a citation, never source coverage."""
    stage = context.get('experiment_decision', {}).get('tuning_scope_design', {}).get('stage_contract') or {}
    check = stage.get('historical_cpu_check') or {}
    checksum = check.get('receipt_sha256')
    if check.get('status') != 'available' or checksum not in refs:
        return []
    from .tuning_scope_reads import _snapshot
    if (stage.get('sha256') != sha({k:v for k,v in stage.items() if k != 'sha256'})
            or check != historical_check(engine, run_id, _snapshot(engine, run_id, context))):
        raise ValueError('Delivered CPU check summary changed')
    return [{'receipt_sha256': checksum, 'trial_id': check['trial_id'],
        'source_kind': 'delivered_host_cpu_receipt_summary',
        'tool': None, 'path': '/experiment_decision/tuning_scope_design/stage_contract/historical_cpu_check',
        'coverage_credit': False, 'inherited_read': False, 'scientific_approval': False}]
