"""Architecture-first, evidence-bound optimization before dataset advancement.

Certificates reference real independent decisions; coverage and a stop report
are deliberately insufficient. Historical requests/decisions remain immutable.
"""
import copy
import math

from .experiment_workflow import sha
from .storage import now
from .training_adjustment import obj

PROTOCOL = 'dataset_optimization_protocol'
COLLECTION = 'dataset_optimization_certificates'
TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 1200}
CRITERIA = obj({
    'reference_unit_id': TEXT,
    'minimum_search_batches': {'type': 'integer', 'minimum': 1, 'maximum': 32},
    'max_relative_regression': {'type': 'number', 'minimum': 0, 'maximum': 1},
    'rationale': TEXT,
})
ASSESSMENT = obj({
    'architecture_rationale': TEXT,
    'remaining_architecture_issues': {'type': 'array', 'items': TEXT, 'maxItems': 8},
    'criteria': CRITERIA,
    'selected_unit_id': {'type': ['string', 'null']},
    'stability_rationale': TEXT,
})
GUIDANCE = '''
当前执行顺序：实验验证 → 架构评估/必要调整 → 新架构验证 → 参数微调 → 稳定性评审 → 下一数据集。
dataset_optimization 是当前有效门禁，优先于历史 operator_progression 中覆盖完成即转下一数据集的指引。
coverage_complete、stop、报告完成或 CPU 检查通过都不表示架构合理或参数稳定。
architecture 阶段：三方先读比较、消融、训练统计与实际源码。需要改架构时 revise_architecture，
修改后沿原实现/独立复审/CPU检查/验证通道复验；仅补日志不得冒充架构改进。
证据支持架构可进入参数验证时 accept_architecture：unit_id=null、changes=[]，
填写 optimization_assessment；remaining_architecture_issues=[]，selected_unit_id=null。
criteria 必须提前声明当前完整对照 reference_unit_id、至少一个真实新参数批次 minimum_search_batches、
相对该参考候选允许的最大验证退化 max_relative_regression 及依据；不得修改原科学验收阈值。
此时 training_response 可用 architecture_assessment，保留未定训练根因与证据局限，不声称告警已解决。
parameters 阶段：按批准范围做真实 search_parameters；每批新计划默认单种子，保留已有冻结种子。
复用现有统计设计有区分力的最小对照，不因已有日志重复插桩；有具体缺项才补读/补测。
参数稳定后 complete_optimization：同一 criteria、selected_unit_id=已完成且可比较的批次，
解释已执行参数对照及稳定依据；仍有未解决架构问题或训练退化不能声称完成。
三方必须实际核对结果。首数据集完成后冻结模型源码，后续数据集只适配数据和调参数；
发现冻结架构问题时停止并提出显式重新开启架构评审的请求，不能静默修改。
当前主流程已删除 stop 提前收尾分支。不能因为覆盖完成、未找到具体错误或证据尚未核实而结束探索。
历史 stop 及其报告只作原始记录，不是本轮指令或审批。先用现有工具核实明确的未决项，再提交下一步。
未核实不等于缺日志；先复用既有入口、解析配置、模型对象、检查点与指标产物，并核对同种子容量与初始化。
只在确认具体证据缺口后提出有界 diagnose；不得反复插桩、重跑已完成项或把未知自动改成通过。
确有资源、证据或权限阻塞时留在当前阶段说明具体原因，不进入结果报告。正式验收/独立测试另守原门禁。
即使 iteration_policy.final_tests_allowed=false，也必须完成以上架构与参数稳定性评审才能换数据集；
complete_optimization 此时仅形成验证阶段报告，不开放正式验收或留出测试。
'''


def enabled(store, run_id):
    run = store.get('runs', run_id) or {}
    return run.get('mode') == 'live' and (
        run.get('config', {}).get(PROTOCOL) == 1 or run.get('state', {}).get(PROTOCOL) == 1)


def _key(run_id, binding, kind):
    return f'{run_id}:{binding["cycle"]}:{kind}:{sha(binding)}'


def certificate(store, run_id, binding, kind):
    """Authenticate the whole decision chain on read, not just a boolean flag."""
    from .experiment_decisions import _approved
    record = store.get(COLLECTION, _key(run_id, binding, kind))
    if not record:
        return None
    body = record['body']
    decision = store.get('experiment_decisions', body['decision_id']) or {}
    archive = store.get('experiment_evidence', (decision.get('evidence') or {}).get('id', '')) or {}
    if (record.get('run_id') != run_id or record.get('sha256') != sha(body)
            or body['binding'] != binding or not _approved(decision)
            or decision.get('run_id') != run_id or decision.get('cycle') != binding['cycle']
            or decision['proposal_sha256'] != body['proposal_sha256']
            or sha(archive.get('summary')) != decision['evidence']['sha256']
            or archive['summary']['binding'] != binding
            or decision['proposal']['action'] != kind
            or body['assessment'] != decision['proposal'].get('optimization_assessment')):
        raise ValueError('Dataset optimization certificate or independent evidence changed')
    validate_proposal(decision['proposal'], archive['summary'])
    if not all(r.get('module_source_read') and
               set(r.get('analysis_kinds', [])) & {'comparison', 'ablation'}
               for r in decision['analysis_reads']):
        raise ValueError('Architecture/stability review requires all three actual source and comparison reads')
    return record


def context(engine, run_id, binding):
    if not enabled(engine.store, run_id):
        return None
    approved = certificate(engine.store, run_id, binding, 'accept_architecture')
    frozen = engine.store.get(COLLECTION, run_id + ':frozen-architecture')
    return {'protocol': 1, 'workflow_revision': 2, 'phase': 'parameters' if approved else 'architecture',
            'architecture_certificate': approved,
            'architecture_frozen': bool(frozen and frozen['body']['binding']['cycle'] != binding['cycle']),
            'required_order': ['experiments', 'architecture_tuning', 'tuning', 'stability_review'],
            'stop_advances_dataset': False, 'early_report_allowed': False,
            'new_plan_seed_policy': 'single'}


def action_schema(base, scope, field='action'):
    """New live requests cannot select the retired early-termination branch."""
    schema = copy.deepcopy(base)
    if scope:
        excluded = {'stop', 'review'}
        if scope['phase'] == 'architecture':
            excluded.update({'grid_search', 'search_parameters', 'complete_optimization', 'revise_tuning_scope'})
        schema['properties'][field]['enum'] = [
            action for action in schema['properties'][field]['enum'] if action not in excluded]
    return schema


def validate_proposal(proposal, evidence):
    from .prose_schema import validate
    scope = evidence.get('dataset_optimization')
    action = proposal['action']
    if not scope:
        if action in {'accept_architecture', 'complete_optimization'}:
            raise ValueError('Architecture-first decision requires its explicit protocol')
        return
    if action == 'stop':
        raise ValueError('Early stop/report branch is removed: resolve evidence in the current optimization stage')
    if scope['phase'] == 'architecture' and action in {'search_parameters', 'grid_search', 'review', 'complete_optimization'}:
        raise ValueError('Architecture must pass independent assessment before parameter tuning')
    if scope['architecture_frozen'] and action == 'revise_architecture':
        raise ValueError('First-dataset architecture is frozen; later datasets cannot silently change it')
    if action == 'review':
        raise ValueError('Use complete_optimization before formal validation review')
    if action not in {'accept_architecture', 'complete_optimization'}:
        return
    value = proposal.get('optimization_assessment')
    validate(value, ASSESSMENT)
    if proposal['unit_id'] is not None or proposal['changes'] or value['remaining_architecture_issues']:
        raise ValueError('An unresolved architecture or unverified code change cannot be certified')
    if evidence.get('failure_categories'):
        raise ValueError('Resolve failed execution/integrity before architecture acceptance')
    criteria = value['criteria']
    ref = evidence['comparisons'].get(criteria['reference_unit_id'], {})
    if not ref.get('comparable') or not math.isfinite(ref.get('proposed_score', float('nan'))):
        raise ValueError('Optimization criteria require a finite, complete paired reference')
    if action == 'accept_architecture':
        if scope['phase'] != 'architecture' or value['selected_unit_id'] is not None:
            raise ValueError('Architecture acceptance must precede parameter selection')
        return
    from .tuning_scope import require_complete as require_tuning_coverage
    require_tuning_coverage(evidence, proposal)
    approved = scope.get('architecture_certificate') or {}
    frozen = approved.get('body', {}).get('assessment', {}).get('criteria')
    if criteria != frozen:
        changed = [name for name in sorted(set(criteria) | set(frozen or {}))
                   if criteria.get(name) != (frozen or {}).get(name)]
        raise ValueError('Stability criteria must remain those independently approved before tuning; '
                         'changed fields: ' + ', '.join('criteria.' + name for name in changed))
    searches = [u for u in evidence['units'] if u.get('parameter_search') and u['complete']
                and evidence['comparisons'].get(u['id'], {}).get('comparable')
                and evidence['comparisons'][u['id']].get('task_id') == ref.get('task_id')]
    if len(searches) < criteria['minimum_search_batches'] or not evidence['ledger']['complete']:
        raise ValueError('Coverage alone is insufficient: completed real parameter search and frozen coverage are required')
    selected = evidence['comparisons'].get(value['selected_unit_id'], {})
    if (not selected.get('comparable') or selected.get('task_id') != ref.get('task_id')
            or selected.get('metric') != ref.get('metric') or selected.get('direction') != ref.get('direction')):
        raise ValueError('Selected result must be a complete comparable validation batch of the same task')
    score, base = selected.get('proposed_score'), ref['proposed_score']
    from .parameter_grid import stability_scores
    paired = stability_scores(evidence, value['selected_unit_id'], criteria['reference_unit_id'])
    if paired:
        score, base = paired
    if type(score) not in (int, float) or not math.isfinite(score):
        raise ValueError('Selected score is not a finite measurement')
    regression = (score - base) if ref['direction'] == 'minimize' else (base - score)
    if regression > abs(base) * criteria['max_relative_regression']:
        raise ValueError('Selected parameters violate the pre-reviewed validation stability bound')
    if not paired:
        scores = [v['proposed_score'] for v in evidence['comparisons'].values()
                  if v.get('comparable') and v.get('task_id') == ref.get('task_id')]
        best = min(scores) if ref['direction'] == 'minimize' else max(scores)
        if score != best:
            raise ValueError('Stable selection must use the best measured comparable candidate, not a worse configuration')
    response = proposal.get('training_response', {})
    if evidence.get('training_investigation', {}).get('required') and response.get('disposition') not in {'resolved', 'continue'}:
        raise ValueError('Unresolved training degradation cannot be certified as stable')


def record(engine, run_id, decision, evidence):
    from .experiment_decisions import _approved
    from .engine import ResearchBlocked
    if not _approved(decision) or sha(evidence) != decision['evidence']['sha256']:
        raise ResearchBlocked('Optimization transition requires three independent, current-evidence votes')
    validate_proposal(decision['proposal'], evidence)
    kind = decision['proposal']['action']
    body = {'binding': evidence['binding'], 'decision_id': decision['id'],
            'proposal_sha256': decision['proposal_sha256'],
            'assessment': copy.deepcopy(decision['proposal']['optimization_assessment']),
            'code_sources': evidence['code_sources']}
    key = _key(run_id, evidence['binding'], kind)
    value = {'id': key, 'run_id': run_id, 'body': body, 'sha256': sha(body), 'created_at': now()}
    def retain(old):
        if old and old['sha256'] != value['sha256']:
            raise ValueError('An existing optimization certificate is immutable')
        return old or value
    engine.store.mutate(COLLECTION, key, retain)
    certificate(engine.store, run_id, evidence['binding'], kind)
    return {'optimization_transition': True, 'decision_id': decision['id'],
            'next_node': 'tuning' if kind == 'accept_architecture' else (
                'report' if evidence.get('iteration_policy', {}).get('final_tests_allowed') is False else 'validation_review'),
            'stable': kind == 'complete_optimization', 'scientific_approval': False}


def core_files(bundle, program, *, include_configs=False):
    files = {f['path']: f['sha256'] for f in (bundle or {}).get('files', [])
             if f['path'].endswith('.py') and not f['path'].startswith(('dataset_runtime/', 'src/dataset_runtime/'))}
    # Dataset entrypoints are adapters; modular model/runtime sources stay fixed.
    if len(files) > 1:
        files.pop('experiment.py', None)
    if include_configs:
        files.update({f['path']: f['sha256'] for f in (bundle or {}).get('files', [])
            if f['path'].startswith(('configs/', 'src/configs/')) and f['path'].endswith(('.json', '.yaml', '.yml', '.toml'))})
    return files or {'experiment.py': sha(program['code'])}


def check_frozen(engine, run_id, bundle, program):
    frozen = engine.store.get(COLLECTION, run_id + ':frozen-architecture')
    if not enabled(engine.store, run_id) or not frozen:
        return
    body = frozen['body']
    certificate(engine.store, run_id, body['binding'], 'complete_optimization')
    if frozen['sha256'] != sha(body) or core_files(bundle, program, include_configs=body.get('core_contract_version', 1) >= 2) != body['core_files']:
        raise ValueError('Frozen first-dataset model/runtime source changed; later dataset training is blocked')


def require_stable(engine, run_id, *, freeze=True):
    from .engine import ResearchBlocked
    from .experiment_decisions import _binding, snapshot
    if not enabled(engine.store, run_id):
        return
    basis, binding = _binding(engine, run_id)
    done = certificate(engine.store, run_id, binding, 'complete_optimization')
    architecture = certificate(engine.store, run_id, binding, 'accept_architecture')
    if not done or not architecture:
        raise ResearchBlocked('当前数据集尚未完成架构评审和真实参数搜索的稳定性复核；不能进入正式验收或结果报告，也不能切换数据集。')
    decision = engine.store.get('experiment_decisions', done['body']['decision_id'])
    current = snapshot(engine, run_id)
    if sha(current) != decision['evidence']['sha256']:
        raise ResearchBlocked('稳定性评审之后验证证据发生变化，必须重新核验后才能切换数据集。')
    key = run_id + ':frozen-architecture'
    if freeze and not engine.store.get(COLLECTION, key):
        body = {'binding': binding, 'certificate_id': done['id'], 'core_contract_version': 2,
                'core_files': core_files(basis['bundle'], basis['program'], include_configs=True)}
        engine.store.put(COLLECTION, key, {'id': key, 'run_id': run_id, 'body': body, 'sha256': sha(body), 'created_at': now()})


def route(engine, run_id, stage):
    if not enabled(engine.store, run_id):
        return None
    from .experiment_decisions import _binding
    _, binding = _binding(engine, run_id)
    approved = certificate(engine.store, run_id, binding, 'accept_architecture')
    if stage == 'experiments' or stage == 'tuning' and not approved:
        return 'architecture_tuning'
    return None


def revision_allowed(engine, run_id):
    """Dataset queue indices and telemetry repairs are not architecture versions."""
    from .research_iteration import limit
    maximum = limit(engine.store, run_id)
    if maximum is None:
        return True
    if not enabled(engine.store, run_id):
        from .rapid_experiment_revision import count
        used = engine.cycle(run_id) + count(engine, run_id)
    else:
        changes = [r for r in engine.store.list('rapid_experiment_revisions', run_id)
                   if r.get('decision', {}).get('action') == 'revise_architecture']
        used = 1 + len(changes)  # Initial architecture plus reviewed revisions.
    return used < maximum
