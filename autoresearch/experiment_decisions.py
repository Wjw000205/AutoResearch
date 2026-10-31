"""Evidence-bound, one-batch-at-a-time research decisions.

The frozen plan is an execution envelope, not a command to exhaust its grid.
Stopping early is inconclusive, never permission to omit required acceptance
evidence. Scoped code/diagnostics use a separately reviewed rapid amendment.
"""
from __future__ import annotations

import copy
from collections import defaultdict

from jsonschema import ValidationError

from .prose_schema import validate

from .experiment_workflow import sha, encoded, validation_records, ledger
from .performance_policy import assessment_context
from .storage import now
from .research_iteration import limit as research_cycle_limit
from .training_adjustment import obj, _group

# Retain historical schema bytes for handoff identities. Active provider and
# host validation remove narrative caps through prose_schema, without editing receipts.
TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 600}
TEXTS = {'type': 'array', 'items': {'type': 'string', 'minLength': 1, 'maxLength': 400}, 'maxItems': 3}

SESSIONS = 'experiment_decision_sessions'
DECISIONS = 'experiment_decisions'
ACTIONS = ['revise_architecture', 'grid_search', 'search_parameters', 'diagnose', 'stop', 'review',
           'accept_architecture', 'complete_optimization', 'revise_tuning_scope']
ANALYSIS = obj({'evidence_sha256': TEXT, 'observations': TEXTS, 'hypotheses': TEXTS,
    'suggested_action': {'enum': ACTIONS}, 'missing_evidence': TEXTS,
    'read_receipts': {'type': 'array', 'items': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
                      'minItems': 1, 'maxItems': 16, 'uniqueItems': True}})
PROPOSAL = obj({'evidence_sha256': TEXT, 'action': {'enum': ACTIONS},
    'diagnosis': {'enum': ['architecture', 'training', 'implementation', 'data', 'resources',
                         'insufficient_evidence', 'no_architecture_evidence']},
    'unit_id': {'type': ['string', 'null'], 'maxLength': 100}, 'rationale': TEXT,
    'evidence_paths': {'type': 'array', 'items': TEXT, 'minItems': 1, 'maxItems': 5},
    'hypothesis': TEXT, 'verification': TEXT, 'stop_condition': TEXT,
    'changes': {'type': 'array', 'maxItems': 3, 'items': obj({
        'target': TEXT, 'before': TEXT, 'after': TEXT, 'reason': TEXT})}})
VOTE = obj({'evidence_sha256': TEXT, 'proposal_sha256': TEXT,
            'approve': {'type': 'boolean'}, 'rationale': TEXT, 'issues': TEXTS})
from .parameter_search import SCHEMA as SEARCH_SCHEMA
PROPOSAL['properties']['parameter_search'] = SEARCH_SCHEMA
from .dataset_optimization import ASSESSMENT
PROPOSAL['properties']['optimization_assessment'] = ASSESSMENT
PROPOSAL['properties']['parameter_search_stops'] = {'type': 'array', 'maxItems': 160, 'items': obj({
    'path': {'type': 'string', 'minLength': 1}, 'variant': {'enum': ['shared', 'baseline', 'proposed']},
    'rationale': {'type': 'string', 'minLength': 1, 'maxLength': 1200}})}
GUIDANCE = '''根据当前 baseline 对比、训练诊断和反证决定下一步，不要机械耗尽网格。
director 判断研究假设与最小下一步；engineer 核查实现、优化与架构；scout 核查反证、公平性和泄漏。
指标落后不自动证明架构错误，loss 告警仅是假设；日志缺失不能解释为训练正常。
grid_search：暂无支持架构问题的证据且已有有效成对比较，从可用批次选择一个共享参数批次。
diagnose：提出可证伪的原因假设和通过/失败条件，选一个已批准批次（可含消融）；
若需局部架构修改或诊断插桩，unit_id=null 并明确 changes，转快速代码修订和小批验证。
快速修订复用已有方案，不重新立项；仅修改既有模块，保持参数范围、baseline、数据划分、验收和预算。
超出这些边界不能伪装为局部修改，应停止并明确需要的新协议。
revise_architecture：具体列出修改前后定义、证据与验证条件，不能把低分直接当作根因。
stop：保存当前结果并停止，不宣称科学成功；review 仅在所有冻结种子/配置/消融齐备后进入独立验收。
configuration_queue 存在时，决策只针对 active_dataset；stop 仅停止探索，不代表稳定，不授权下一数据集。切换由宿主核验架构与参数稳定门禁。实验次数预算按数据集独立计数；任务费用、模型调用上限和资源准入持续生效。
若 iteration_policy.final_tests_allowed=false，本轮仅验证局部方案可行性，不选择 review；有足够可行性证据后选择 stop 并记明仍需正式验收。
每次只批准一个批次，完成后重新判断；不得重跑已完成任务、暗中扩大预算、改变数据划分/验收阈值，
不得使用留出集。完整定义、批次目录、比较和诊断在 validation_evidence，按需 experiments.compare 读取。
每个角色必须先调用 experiments.analyze 获得对比/消融/训练/完整性分析；需优化架构或搜索参数时，
还必须 experiments.inspect_module 读取相关模块 source。分析返回真实 read_receipts，不得编造。
用 inspect_module 的目录、符号与调用关系定位模块；静态调用关系不能证明运行路径或梯度正确。
记录“观测→可能原因→模块位置→区分原因的诊断”，证据不足应选择 diagnose 或 stop。
evidence_paths 必须指向该冻结证据内的具体 JSON pointer。返回严格 JSON。
search_parameters：已有完整成对对照而冻结目录没有合适参数点时，提出一个真实的新优化器参数候选。
unit_id=null、changes=[]，另填 parameter_search={base_unit_id,factor_path,value,lower_bound,upper_bound,rationale}。
base_unit_id 必须是 /units 中 complete 且 /comparisons 中 comparable 的成对批次；只允许现有 lr、learning_rate、weight_decay 或梯度裁剪标量，路径来自 /job_parameters。
新参数搜索须填写 validation_seed，从该批次已有完整成对控制中固定选择一个种子；不自动复制旧冻结计划的全部种子。上下界需同时包含原值和候选值并说明依据；两种方法共用同一改变，保持该控制种子、模型、数据、目标、训练步数与验收。换种子不算参数搜索。
三方独立评审后仅追加该批次，不修改原计划或重跑已有配置；训练异常的因果证据要求仍适用。每批结束重新判断，不自动耗尽候选范围。
rapid_revision.status=diagnostic_checked 仅说明候选补修通过 CPU 检查，activation_scope=candidate_only；旧训练日志没有因此增加观测，不能声称修复已在真实训练中生效。'''


def enabled(engine, run_id):
    return (engine.store.get('runs', run_id) or {}).get('mode') == 'live'


def session(engine, run_id):
    return engine.store.get(SESSIONS, f'{run_id}:{engine.cycle(run_id)}')


def _binding(engine, run_id):
    from .training_adjustment import _basis
    from .python_training import code_hash
    basis = _basis(engine, run_id)
    run = engine.store.get('runs', run_id)
    bundle = None
    if basis['program'].get('rapid_revision_id'):
        from .rapid_experiment_revision import validate_program
        bundle = validate_program(engine, run_id, basis['program'])
    elif basis['program'].get('implementation_backend') == 'local_codex':
        from .codex_workflow import validate_program
        bundle = validate_program(engine, run_id, basis['program'])
    basis['bundle'] = bundle
    from .performance_policy import current as performance_policy
    policy = performance_policy(engine.store, run_id)
    from .dataset_optimization import check_frozen
    check_frozen(engine, run_id, bundle, basis['program'])
    return basis, {'cycle': engine.cycle(run_id), 'plan_sha256': sha(basis['plan']),
        'code_sha256': code_hash(basis['program']), 'revision': basis['program'].get('revision', 0),
        'bundle_sha256': bundle['sha256'] if bundle else None,
        'config_sha256': sha(run['config']), **({'performance_policy_sha256': policy['sha256']} if policy else {})}


def units(plan, transport=None):
    """Keep paired variants and all model seeds together, tasks separate.

    A transport consumer is a separate last job: its inputs depend on all
    producers. Dependency closure is visible; it never runs an implicit grid.
    """
    from .python_training import jobs
    from .benchmark_matrix import enabled as matrix
    rows = [row for stage in ('experiments', 'tuning') for row in jobs(plan, stage, transport)]
    by_params = {sha(p): name for name, p in rows}
    groups = []
    plans = [(t['task_id'], t['plan']) for t in plan['task_plans']] if matrix(plan) else [(None, plan)]
    for task, child in plans:
        pairs = {}
        for pair in child['validation_pairs']:
            key = sha({v: {k: x for k, x in pair[v].items() if k not in {'seed', 'phase'}}
                       for v in ('baseline', 'proposed')})
            bucket = pairs.setdefault(key, [])
            for variant in ('baseline', 'proposed'):
                p = {**pair[variant], 'phase': 'validation', 'seed': pair.get('seed', child['selection_seed'])}
                bucket.append((by_params[sha(p)], p))
        for index, group in enumerate(pairs.values()):
            groups.append({'kind': 'pilot' if index == 0 else 'grid', 'task_id': task, 'jobs': group})
        ablations = defaultdict(list)
        for name, p in rows:
            if p.get('variant') == 'ablation' and p.get('benchmark_task_id') == task:
                ablations[sha({k: v for k, v in p.items() if k != 'seed'})].append((name, p))
        groups.extend({'kind': 'ablation', 'task_id': task, 'jobs': group} for group in ablations.values())
    consumer = rows[-1][0] if transport else None
    final = None
    for group in groups:
        if consumer and any(n == consumer for n, _ in group['jobs']):
            final = {'kind': 'transport_finalize', 'task_id': group['task_id'],
                     'jobs': [(n, p) for n, p in group['jobs'] if n == consumer]}
            group['jobs'] = [(n, p) for n, p in group['jobs'] if n != consumer]
    groups = [g for g in groups if g['jobs']] + ([final] if final else [])
    for group in groups:
        group['id'] = 'batch-' + sha(group)[:24]
    return groups


def _safe_rows(rows):
    result = []
    for row in rows:
        value = {k: copy.deepcopy(v) for k, v in row.items()
                 if k not in {'source_updated_at', 'stderr', 'stdout', 'error', 'evaluation_errors', 'failure_diagnostic'}}
        value['metrics'] = {k: v for k, v in value.get('metrics', {}).items() if k.startswith('val_')}
        evaluation = value.get('evaluation', {})
        evaluation['split_fingerprints'] = {k: v for k, v in (evaluation.get('split_fingerprints') or {}).items()
                                            if k in {'train', 'validation'}}
        evaluation.pop('split_materialization', None)
        result.append(value)
    return result


def snapshot(engine, run_id, transport=None):
    from .research_cycle import validation_summary
    from .training_convergence import read_evidence
    basis, binding = _binding(engine, run_id)
    from .performance_policy import current as performance_policy, compact, effective, assess
    policy = performance_policy(engine.store, run_id)
    metric, direction = effective(engine.store, run_id, basis['design'])
    rows = [r for r in validation_records(engine.store, run_id)
            if (r.get('research_cycle') or 1) == binding['cycle'] and not r.get('superseded')]
    state = ledger(engine, run_id, plan=basis['plan'], rows=rows)
    by_name = {j['name']: j for j in state['jobs']}
    from .parameter_search import catalog as search_catalog
    catalog = units(basis['plan'], transport) + search_catalog(engine, run_id, basis['plan'])
    comparisons, diagnostics, manifests = {}, {}, []
    for row in rows:
        diagnostic = read_evidence(engine, run_id, row, metric=metric, direction=direction)
        diagnostics[row['id']] = diagnostic
    for unit in catalog:
        heads = [by_name[n] for n, _ in unit['jobs']]
        done = all(h['state'] == 'completed' for h in heads)
        available = all(h['state'] in {'missing', 'completed'} for h in heads) and not done
        if unit['kind'] == 'transport_finalize':
            own = {h['name'] for h in heads}
            available &= all(h['state'] == 'completed' for h in state['jobs'] if h['name'] not in own)
        ids = {tid for h in heads for tid in h['trial_ids']}
        records = [r for r in rows if r['id'] in ids]
        summary = validation_summary(records, metric, direction)
        # A partial seed group or transport producer cannot establish the paired comparison.
        comparable = done and unit['kind'] in {'pilot', 'grid'} and summary.get('comparable') is True
        comparisons[unit['id']] = {k: summary.get(k) for k in ('metric', 'direction', 'n_pairs', 'pairs', 'paired_seeds',
            'baseline_score', 'proposed_score', 'relative_improvement', 'issues')}
        comparisons[unit['id']].update(comparable=comparable, task_id=unit['task_id'],
            full_seed_group=done, scope='Descriptive same-configuration paired validation; not final acceptance.')
        manifests.append({k: unit[k] for k in ('id', 'kind', 'task_id')} | {
            'available': available, 'complete': done, 'jobs': [n for n, _ in unit['jobs']],
            **({'parameter_search': True} if unit['id'].startswith('search-') else {}),
            **({'parameter_grid': unit['parameter_grid']} if unit.get('parameter_grid') else {}),
            'states': [h['state'] for h in heads], 'params_sha256': [sha(p) for _, p in unit['jobs']]})
    with engine.store._read_db() as db:
        attempts = db.execute("SELECT coalesce(sum(coalesce(json_extract(data,'$.attempt'),1)),0) FROM records "
            "WHERE collection='experiments' AND json_extract(data,'$.run_id')=? "
            "AND coalesce(json_extract(data,'$.research_cycle'),1)=?", (run_id, binding['cycle'])).fetchone()[0]
    run = engine.store.get('runs', run_id)
    from .experiment_analysis import freeze_sources
    sources = freeze_sources(engine, run_id, basis['program'], basis.get('bundle'))
    from .rapid_experiment_revision import latest
    recent = latest(engine, run_id)
    from .rapid_continuation import effective_policy, trial_limit
    from .dataset_configuration_queue import context_view as configuration_queue
    from .experiment_attempt_policy import current as attempt_policy
    execution_policy = attempt_policy(engine.store, run_id)
    from .training_investigation import collect
    investigation = collect(engine, run_id, _safe_rows(rows), diagnostics, basis['program'], metric, direction)
    from .dataset_optimization import context as optimization_context
    optimization = optimization_context(engine, run_id, binding)
    evidence = {'binding': binding, 'ledger': state, 'units': manifests,
        **({'dataset_optimization': optimization} if optimization else {}),
        'training_investigation': investigation,
        'configuration_queue': configuration_queue(engine.store, run_id),
        'rapid_revision': ({k: recent.get(k) for k in ('id', 'status', 'decision_id', 'patch', 'votes', 'check', 'error', 'activation_scope', 'training_authorized')}
            | {'code_sources': (recent.get('candidate') or {}).get('code_sources')}
            | ({'review_reads': recent['review_reads'], 'review_outcome': recent['review_outcome']}
               if recent.get('review_outcome') == 'authenticated_veto' else {}) if recent else None),
        'code_sources': sources,
        'comparisons': comparisons, 'diagnostics': diagnostics, 'records': _safe_rows(rows),
        'definitions': {k: basis['design'].get(k) for k in ('architecture', 'dataset', 'evaluation',
            'baseline_params', 'proposed_params', 'resource_plan')},
        'job_parameters': {n: p for u in catalog for n, p in u['jobs']},
        'acceptance': ({'policy': 'aggregate_primary', 'criteria': [policy['policy']['criterion']],
            'complete': True, 'diagnostic_policy': policy['policy']['diagnostic_policy']}
            if policy else basis['plan'].get('acceptance')),
        **({'performance_policy': compact(policy), 'performance_assessment': assess(rows, basis['plan'], policy)} if policy else {}),
        'iteration_policy': effective_policy(engine.store, run_id, basis['plan'].get('iteration_policy')),
        **({'completion_policy': execution_policy['body']} if execution_policy else {}),
        'budget': {'attempts_used': attempts, 'trial_limit': trial_limit(engine.store, run_id),
            'cycle_limit': research_cycle_limit(engine.store, run_id)},
        'failure_categories': {r['id']: (r.get('failure_diagnostic') or {}).get('category', r['status'])
                               for r in rows if r['status'] != 'completed'}}
    from .parameter_grid import PROTOCOL, summaries
    evidence['parameter_search_protocol'] = PROTOCOL
    evidence['parameter_grids'] = summaries(evidence)
    from .parameter_resolution import apply as apply_resolution
    apply_resolution(engine, run_id, evidence)
    from .yaml_migration import describe as yaml_status
    yaml_state = yaml_status(engine, run_id, basis['program'])
    if yaml_state:
        evidence['hyperparameter_yaml'] = yaml_state
    from .tuning_scope import attach as attach_tuning_scope
    attach_tuning_scope(engine, run_id, evidence)
    if evidence.get('tuning_scope'):
        from .bounded_diagnostic_contract import capabilities
        evidence['diagnostic_host_capabilities'] = capabilities()
    from .saved_runtime_audit import attach as attach_runtime_audit
    attach_runtime_audit(engine, run_id, evidence)
    from .tuning_progress_recovery import attach as attach_progress_recovery
    attach_progress_recovery(engine, run_id, evidence)
    return evidence


def _archive(engine, run_id, evidence):
    digest = sha(evidence)
    key = f'{run_id}:experiment-decision:{digest}'
    record = {'id': key, 'run_id': run_id, 'sha256': digest, 'summary': evidence,
              'created_at': now(), 'provenance': 'host_computed_validation_decision_evidence'}
    def retain(old):
        if old and (old.get('sha256') != digest or sha(old['summary']) != digest):
            raise ValueError('Experiment decision archive changed')
        return old or record
    engine.store.mutate('experiment_evidence', key, retain)
    return {'id': key, 'sha256': digest, 'read_with': 'experiments.compare'}


def packet(evidence, ref, previous=None):
    """Fixed-size overview; no architecture text, parameter tables or full history."""
    available = [u for u in evidence['units'] if u['available']]
    comparable = {k: v for k, v in evidence['comparisons'].items() if v['comparable']}
    alerts = [(k, v) for k, v in evidence['diagnostics'].items()
              if v.get('needs_discussion') or v.get('status') == 'insufficient_evidence']
    from .training_investigation import compact as training_scope
    from .parameter_resolution import compact as resolution_scope
    from .tuning_scope import compact as compact_tuning_scope
    return {'training_context_protocol': 1, 'experiment_workflow_protocol': 1,
        **assessment_context(evidence),
        'research_cycle': evidence['binding']['cycle'], 'evidence_sha256': ref['sha256'],
        'validation_evidence': ref, 'experiment_decision': {
            'binding': evidence['binding'], 'counts': evidence['ledger']['counts'],
            'coverage_complete': evidence['ledger']['complete'], 'budget': evidence['budget'],
            **({'training_investigation': training_scope(evidence['training_investigation'])}
                if evidence.get('training_investigation') else {}),
            'configuration_queue': evidence.get('configuration_queue'),
            'parameter_search_protocol': evidence.get('parameter_search_protocol', 1),
            **({'bounded_diagnostic_capabilities': evidence['diagnostic_host_capabilities']}
               if evidence.get('diagnostic_host_capabilities') else {}),
            **({'tuning_progress_recovery': evidence['tuning_progress_recovery']}
               if evidence.get('tuning_progress_recovery') else {}),
            **({'parameter_resolution': resolution_scope(evidence['parameter_resolution'])} if evidence.get('parameter_resolution') else {}),
            **({'tuning_scope': compact_tuning_scope(evidence['tuning_scope'])}
               if evidence.get('tuning_scope') else {}),
            **({'hyperparameter_yaml': {k:v for k,v in evidence['hyperparameter_yaml'].items() if k!='schema'}} if evidence.get('hyperparameter_yaml') else {}),
            'parameter_grids': [{k: g[k] for k in ('id', 'factor_path', 'grid_values', 'validation_seed',
                'completed_new_points', 'new_trials_needed', 'best_measured_value', 'complete')}
                for g in evidence.get('parameter_grids', [])],
            **({'dataset_optimization': evidence['dataset_optimization']} if evidence.get('dataset_optimization') else {}),
            **({'completion_policy': evidence['completion_policy']} if evidence.get('completion_policy') else {}),
            'iteration_policy': evidence['iteration_policy'],
            'available_batches': [{k: u[k] for k in ('id', 'kind', 'task_id', 'available')} |
                {'job_count': len(u['jobs']), 'details_path': '/units'} for u in available[:6]],
            'available_batch_count': len(available),
            'comparisons': {k: {f: x for f, x in v.items() if f != 'issues'}
                for k, v in list(comparable.items())[-4:]}, 'comparable_batch_count': len(comparable),
            'diagnostic_alerts': {k: {f: v[f] for f in ('status', 'reason') if f in v} for k, v in alerts[-6:]},
            'diagnostic_alert_count': len(alerts), 'code_sources': evidence['code_sources'],
            'failure_count': len(evidence['failure_categories']),
            'previous_decision': previous,
            'recent_revision': ({k: evidence['rapid_revision'].get(k) for k in ('id', 'status', 'decision_id')}
                if evidence.get('rapid_revision') else None),
            'paths': ['/units', '/comparisons', '/diagnostics', '/failure_categories', '/definitions',
                      '/acceptance', '/records', '/job_parameters', '/rapid_revision', '/parameter_grids',
                      *(['/training_investigation'] if evidence.get('training_investigation') else []),
                      *(['/performance_policy', '/performance_assessment'] if evidence.get('performance_policy') else [])],
            'history_is_archived': True, 'scientific_approval': False}}


def _pointer(value, path):
    if not path.startswith('/'):
        raise ValueError('Evidence paths must be JSON pointers')
    for token in path[1:].split('/'):
        token = token.replace('~1', '/').replace('~0', '~')
        value = value[int(token)] if isinstance(value, list) else value[token]
    return value


def validate_proposal(proposal, evidence, digest, *, artifact_evidence=None, reference_context=None):
    from .training_investigation import proposal_schema, validate_response, validate_reference_contract
    validate_reference_contract(proposal, evidence, _pointer)
    schema = proposal_schema(PROPOSAL, evidence)
    if proposal.get('architecture_review'):
        from .architecture_experiment_contract import schema as architecture_schema
        schema = architecture_schema(schema, evidence)
    if 'artifact_trial_ids' in proposal:
        from .experiment_evidence_handoff import SCOPE_SCHEMA, validate_scope
        schema['properties']['artifact_trial_ids'] = SCOPE_SCHEMA
        validate_scope(proposal, evidence)
    for field in ('artifact_evidence_sha256', 'artifact_review_sha256'):
        # Only the new artifact handoff requests this field. Keep previously
        # dispatched author schemas/instructions byte-identical for replay.
        if field in proposal:
            schema['properties'][field] = {'type': 'string', 'pattern': '^[a-f0-9]{64}$'}
    validate(proposal, schema)
    if proposal['evidence_sha256'] != digest:
        raise ValueError('Decision is not bound to the current evidence')
    if artifact_evidence is not None and (
            sha(artifact_evidence) != proposal.get('artifact_evidence_sha256')
            or artifact_evidence.get('source_evidence', {}).get('sha256') != digest
            or artifact_evidence.get('binding') != evidence['binding']):
        raise ValueError('Supplemental proposal evidence does not match the original decision')
    resolved_paths = {}
    if reference_context is not None:
        from .decision_reference_resolution import references
        resolved_paths = {row['cited_path']: row['evidence_path']
                          for row in references(proposal, evidence, digest, reference_context)}
    for path in proposal['evidence_paths']:
        if path == '/consensus_resolution' or path.startswith('/consensus_resolution/'):
            raise ValueError('Decision cites workflow context instead of experimental evidence: ' + path
                + '; describe the mediation instruction in rationale and cite an actual original or supplemental evidence path')
        try:
            supplemental = path.split('/')[1] in {
                'trials', 'parameter_search_audit', 'checkpoint_identity',
                'host_checkpoint_audit', 'fact_review_scope', 'pair_scope', 'pair_comparisons', 'yaml_candidate_audit'} if path.startswith('/') else False
            _pointer(artifact_evidence if supplemental else evidence, resolved_paths.get(path, path))
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ValueError(f'Decision cites missing evidence: {path} in '
                             f'{"supplemental" if supplemental else "original"} snapshot') from exc
    action, unit_id = proposal['action'], proposal['unit_id']
    from .architecture_experiment_contract import validate as validate_architecture
    validate_architecture(proposal, evidence, _pointer)
    from .dataset_optimization import validate_proposal as validate_optimization
    validate_optimization(proposal, evidence)
    if evidence.get('hyperparameter_yaml', {}).get('migration_required') and action != 'revise_architecture':
        raise ValueError('First dispatch the user-requested YAML implementation migration; no new tuning/training before it is verified')
    if action == 'revise_tuning_scope':
        if unit_id is not None or proposal['changes'] or evidence.get('tuning_scope', {}).get('status') != 'accepted':
            raise ValueError('Refine an accepted scope without authorizing a training batch or source changes')
        validate_response(proposal, evidence, _pointer)
        return
    if action in {'accept_architecture', 'complete_optimization'}:
        validate_response(proposal, evidence, _pointer)
        return
    from .experiment_evidence_handoff import requested as read_only_request
    if read_only_request(proposal):
        validate_response(proposal, evidence, _pointer)
        return
    if action == 'search_parameters':
        if unit_id is not None or proposal['changes'] or not proposal.get('parameter_search'):
            raise ValueError('A supplemental search requires its scalar candidate and no code change')
        if proposal['diagnosis'] not in {'training', 'no_architecture_evidence'} or evidence['failure_categories']:
            raise ValueError('Resolve execution/architecture problems before parameter search')
        if (evidence.get('iteration_policy') or {}).get('on_validation_failure') == 'stop':
            raise ValueError('Frozen stop policy forbids new parameter candidates')
        from .parameter_search import response_evidence, control_records
        controlled, augmented = response_evidence(proposal, evidence)
        validate_response(controlled, augmented, _pointer,
                          parameter_controls=control_records(proposal['parameter_search'], evidence))
        return
    if proposal.get('parameter_search'):
        raise ValueError('Parameter candidate is allowed only for search_parameters')
    if action in {'stop', 'review', 'revise_architecture'} and unit_id is not None:
        raise ValueError('Only grid/diagnostic actions may execute a batch')
    if action == 'review' and not evidence['ledger']['complete']:
        raise ValueError('Independent acceptance still requires every frozen configuration, seed and ablation')
    if action == 'review' and (evidence.get('iteration_policy') or {}).get('final_tests_allowed') is False:
        raise ValueError('This bounded feasibility continuation requires a stop decision, not formal acceptance or held-out tests')
    if action == 'revise_architecture' or action == 'diagnose' and unit_id is None:
        if not proposal['changes'] or any(c['before'] == c['after'] for c in proposal['changes']):
            raise ValueError('A new architecture/diagnostic needs concrete reviewed changes')
        if (evidence.get('iteration_policy') or {}).get('on_validation_failure') == 'stop':
            raise ValueError('Frozen stop policy forbids automatic revision or new diagnostic execution')
    elif proposal['changes']:
        raise ValueError('A frozen execution batch cannot modify code or parameters')
    if action == 'grid_search' or action == 'diagnose' and unit_id is not None:
        unit = next((u for u in evidence['units'] if u['id'] == unit_id and u['available']), None)
        if not unit:
            raise ValueError('Choose one available frozen batch; completed/failed/dependent batches cannot be replayed')
        if action == 'grid_search':
            paired = any(v['comparable'] and v['task_id'] == unit['task_id'] for v in evidence['comparisons'].values())
            if unit['kind'] not in {'pilot', 'grid'} or not paired or proposal['diagnosis'] != 'no_architecture_evidence':
                raise ValueError('Grid search needs a valid baseline comparison and no supported architecture concern')
            if evidence['failure_categories']:
                raise ValueError('Resolve execution failures before continuing grid search')
    validate_response(proposal, evidence, _pointer)


async def decide(engine, harness, run_id, transport=None):
    from .experiment_consensus import decide as reconcile
    return await reconcile(engine, harness, run_id, transport, _decide)


async def _decide(engine, harness, run_id, transport=None, *, evidence=None, record_key=None):
    from .engine import ResearchBlocked
    from . import proposal_request_contract as request_contract
    engine.check_active(run_id)
    if evidence is None:
        evidence = await engine._read_consensus_check(run_id, snapshot, engine, run_id, transport)
    ref = _archive(engine, run_id, evidence)
    key = record_key or ref['id']
    record = engine.store.get(DECISIONS, key) or {'id': key, 'run_id': run_id,
        'cycle': engine.cycle(run_id), 'evidence': ref, 'status': 'analyzing', 'analyses': [],
        'votes': [], 'created_at': now(), 'scientific_approval': False, 'provenance': 'live',
        'parameter_path_contract_version': 1, 'proposal_request_contract_version': request_contract.VERSION,
        'proposal_reference_contract_version': 2}
    from .parameter_resolution import track_successor
    track_successor(engine, run_id, evidence, record)
    from .yaml_migration import track as track_yaml_migration
    track_yaml_migration(engine, run_id, record)
    state = session(engine, run_id) or {}
    previous = engine.store.get(DECISIONS, state.get('last_decision', '')) or {}
    context = packet(evidence, ref, {k: previous.get('proposal', {}).get(k)
        for k in ('action', 'hypothesis', 'verification', 'stop_condition')} if previous else None)
    if record.get('proposal_request_contract_version', 0) >= 3:
        from .architecture_experiment_contract import PROTOCOL, enabled
        if enabled(evidence):
            context['experiment_decision'][PROTOCOL] = 1
    from .experiment_memory import recall, remember
    context['experiment_memory'] = recall(engine, run_id)
    if record.get('consensus_resolution'):
        context['experiment_decision']['consensus_resolution'] = copy.deepcopy(record['consensus_resolution'])
    if record.get('yaml_review_recovery'):
        from .yaml_review_recovery import review_packet
        context['yaml_candidate_review'] = await engine._read_consensus_check(run_id, review_packet, engine, run_id, record)
    from .training_investigation import guidance, proposal_schema as historical_schema, validate_reads, RESPONSE_CONTRACT_VERSION
    def proposal_schema(base, evidence):
        return request_contract.schema(historical_schema(base, evidence), evidence,
                                       record.get('proposal_request_contract_version', 0))
    base_guidance = GUIDANCE
    if record.get('yaml_review_recovery'):
        from .yaml_review_recovery import GUIDANCE as yaml_review_guidance
        base_guidance += '\n' + yaml_review_guidance
    if evidence.get('dataset_optimization'):
        from .dataset_optimization import GUIDANCE as optimization_guidance
        # Do not send the retired stop instructions alongside the mainline policy.
        base_guidance = '\n'.join(line for line in GUIDANCE.splitlines()
                                 if not any(word in line for word in ('stop', '停止并', '停止探索')))
        base_guidance += '\n' + optimization_guidance
        if record.get('yaml_review_recovery'):
            base_guidance += '\n' + yaml_review_guidance
    if record.get('consensus_resolution'):
        base_guidance += ('\n本轮必须逐项落实 consensus_resolution：修订具体分歧或补齐可区分证据，不能仅重复上一提案。'
            '原异议保留，director 协调不是批准；你的新提案必须重新由三方独立投票。不得降低门禁以求表面一致。')
    if evidence.get('parameter_resolution', {}).get('next_task'):
        base_guidance += ('\nparameter_resolution.next_task 是已独立审核的具体待办，本轮应将它落实为正常提案；'
            '复用其逐项事件处置，不重复交付同一阻塞登记。协议规划未授权执行，仍需完整提案、读取、三方投票及执行门禁。'
            '若不能落实，明确指出该协议的具体缺陷及所需修订，不能仅重述原告警或把代理规划职责转交用户。')
    if evidence.get('tuning_progress_recovery'):
        base_guidance += ('\n宿主已按用户修复指令保留并退出旧诊断实现循环；tuning_progress_recovery只是路由维修证据。'
            '先核对tuning_scope.acceptance的生效三方票与CPU原回执，再独立提出一个实际下一批或具体代码修订。'
            '旧strategy里的待认证文字不覆盖后续批准；保留真实训练告警，但不能因未知根因自动要求重复同一诊断。'
            '不得重跑已完成训练、放宽门禁或把维修当成选参成功。新方案仍经原三方、资源和科学条件审核。')
    if evidence.get('diagnostic_host_capabilities'):
        from .bounded_diagnostic_contract import GUIDANCE as capability_guidance
        base_guidance += '\n' + capability_guidance
    if evidence.get('parameter_search_protocol', 1) >= 2:
        from .parameter_grid import GUIDANCE as grid_guidance
        base_guidance += '\n' + grid_guidance
    if evidence.get('tuning_scope'):
        from .tuning_scope import GUIDANCE as scope_guidance
        base_guidance = base_guidance.replace('只允许现有 lr、learning_rate、weight_decay 或梯度裁剪标量',
            '只允许本次 tuning_scope 中已审且可执行的因素').replace('两种方法共用同一改变',
            'shared 因素同步改变；方法专有因素保留另一侧对照').replace(
            '保持该控制种子、模型、数据、目标、训练步数与验收',
            '保持该控制种子、数据、目标和验收，仅修改本次获批的一个因素').replace(
            'baseline/proposed 同步修改', 'shared 同步修改；方法专有参数分别调整并复用另一侧对照')
        base_guidance += '\n' + scope_guidance
        if evidence['tuning_scope'].get('acceptance'):
            from .tuning_scope_acceptance import GUIDANCE as acceptance_guidance
            base_guidance += '\n' + acceptance_guidance
    if evidence.get('hyperparameter_yaml', {}).get('migration_required'):
        from .yaml_migration import INSTRUCTION as yaml_contract
        base_guidance += ('\n用户要求优先插入 YAML 超参数迁移任务。本轮 action=revise_architecture、diagnosis=implementation，'
            'unit_id=null，给出最多三项可直接交给 Codex 的具体修改单。保留未知训练根因，training_response 可用 repair_code。'
            '这是已授权的配置接口/参数化迁移，不是要求证明旧架构错误；不要再次交付泛化调参清单。'
            '代码由独立本机 Codex 工作项落盘，交付后仍经三方代码审核和 CPU 检查。\n' + yaml_contract)
    instruction = guidance(base_guidance, context)
    if context['experiment_decision'].get('architecture_review_protocol') == 1:
        from .architecture_experiment_contract import instruction as architecture_instruction
        instruction = architecture_instruction(instruction)
    if record.get('proposal_request_contract_version') and evidence.get('dataset_optimization'):
        instruction += request_contract.guidance(record['proposal_request_contract_version'])
    elif record.get('proposal_request_contract_version', 0) >= 2:
        instruction += request_contract.GUIDANCE_V2
    if record.get('parameter_path_contract_version', 0) >= 1:
        from .parameter_path_contract import guidance as parameter_path_guidance
        instruction = parameter_path_guidance(instruction, method_local=bool(evidence.get('tuning_scope')))
    if record.get('proposal_reference_contract_version', 0) >= 1:
        from .decision_reference_resolution import GUIDANCE as reference_guidance
        instruction += reference_guidance
        if record['proposal_reference_contract_version'] >= 2:
            from .decision_reference_resolution import WORKFLOW_GUIDANCE
            instruction += WORKFLOW_GUIDANCE
    from . import experiment_evidence_handoff as artifact_handoff
    instruction = artifact_handoff.retained_analysis_instruction(engine, run_id, context, instruction, analysis_schema(context))
    async def checkpoint(**changes):
        current = await engine._read_consensus_check(run_id, snapshot, engine, run_id, transport)
        if sha(current) != ref['sha256']:
            raise ResearchBlocked('Experiment evidence changed during decision; retained reviews cannot authorize execution')
        record.update(changes, updated_at=now())
        engine.store.put(DECISIONS, key, record)
        track_successor(engine, run_id, evidence, record)
        track_yaml_migration(engine, run_id, record)
    from .research_anchor import for_phase as anchor_for_phase
    context, instruction = await anchor_for_phase(engine, run_id, record, context, instruction, checkpoint, 'analysis')
    if not record['analyses']:
        analyses = await _group(harness, run_id, 'experiment_diagnose:' + ref['sha256'], instruction, context, analysis_schema(context))
        if any(a['evidence_sha256'] != ref['sha256'] for a in analyses):
            raise ResearchBlocked('Experiment analyses refer to different evidence')
        from .training_investigation import analysis_reads
        analyses, reads = await analysis_reads(engine, harness, run_id, analyses, context, instruction, checkpoint, record)
        await checkpoint(analyses=analyses, analysis_reads=reads, status='proposing')
    context['decision_analyses'] = record['analyses']
    context, instruction = await anchor_for_phase(engine, run_id, record, context, instruction, checkpoint, 'proposal')
    from .experiment_progression import decision_context
    context, instruction, continuation_suffix = decision_context(engine, run_id, evidence, context, instruction)
    if not record.get('proposal'):
        from . import experiment_artifact_review as artifact_review
        from . import optimization_read_recovery as optimization_reads
        from . import training_proposal_reads as proposal_reads
        from . import diagnostic_planning_recovery as diagnostic_planning
        from . import parameter_gate_followup as gate_followup
        retained_read_proposal = (optimization_reads.retained_proposal(record)
                                  or proposal_reads.retained_proposal(record))
        from .parameter_control_recovery import recover as recover_parameter_controls
        retained_read_proposal = retained_read_proposal or await recover_parameter_controls(
            engine, run_id, record, evidence, ref, checkpoint)
        from .parameter_reference_recovery import recover as recover_parameter_reference
        await recover_parameter_reference(engine, run_id, record, evidence, ref, checkpoint)
        if artifact_handoff.recoverable(record):
            validate_proposal(record['rejected_proposal'], evidence, ref['sha256'])
            await artifact_handoff.prepare(engine, run_id, record, record['rejected_proposal'], evidence, ref, checkpoint)
        recovery = (artifact_handoff.request(record, context, instruction, proposal_schema(PROPOSAL, evidence))
                    if record.get(artifact_handoff.KEY) else
                    record.get('proposal_trial_reference_recovery') or record.get('proposal_contract_recovery'))
        if record.get(artifact_handoff.KEY, {}).get('independent_review'):
            try:
                recovery = await artifact_review.advance(engine, harness, run_id, record, context,
                    instruction, proposal_schema(PROPOSAL, evidence), checkpoint)
                if record.get(gate_followup.KEY):
                    recovery = await gate_followup.advance(engine, harness, run_id, record,
                        evidence, context, instruction, checkpoint)
            except (ValueError, KeyError, IndexError, TypeError, ValidationError) as exc:
                await checkpoint(status='invalid', rejection=str(exc))
                raise ResearchBlocked(str(exc)) from exc
        from .training_trial_reference import eligible as trial_reference_eligible, request as trial_reference_request
        if not recovery and not retained_read_proposal:
            try:
                recovery = await diagnostic_planning.advance(engine, harness, run_id, record,
                    evidence, context, instruction, proposal_schema(PROPOSAL, evidence), checkpoint)
            except (ValueError, KeyError, IndexError, TypeError, ValidationError) as exc:
                await checkpoint(status='invalid', rejection=str(exc))
                raise ResearchBlocked(str(exc)) from exc
        trial_reference_repair = trial_reference_eligible(record)
        training_required = evidence.get('training_investigation', {}).get('required')
        if training_required and record.get('status') == 'invalid' and record.get('proposal_contract_version') == RESPONSE_CONTRACT_VERSION and not trial_reference_repair and not recovery and not retained_read_proposal:
            raise ResearchBlocked(record['rejection'])
        if training_required and trial_reference_repair:
            recovery = trial_reference_request(record, evidence, context, instruction, PROPOSAL)
            await checkpoint(proposal_trial_reference_recovery=recovery, status='correcting')
        if training_required and record.get('status') == 'invalid' and not recovery and not retained_read_proposal:
            # One same-author recovery for the old, underspecified reference contract.
            # Preserve the last rejected answer, all earlier attempts and the frozen request.
            eligible = ('Training response requires specific measured evidence paths',
                        'No new verification run exists')
            if (not evidence.get('training_investigation', {}).get('required')
                    or not str(record.get('rejection', '')).startswith(eligible)
                    or not record.get('rejected_proposal')):
                raise ResearchBlocked(record.get('rejection', 'Experiment proposal remains invalid'))
            rejected = copy.deepcopy(record['rejected_proposal'])
            recovery_context = copy.deepcopy(context)
            recovery_context['experiment_decision']['proposal_correction'] = {
                'rejected_proposal': rejected, 'validation_error': record['rejection'],
                'instruction': '按本次明确的引用与复验字段约束纠正最后被拒的提案。保留证据哈希、原始答复和训练告警；不替换实测结果，不继承批准。'}
            recovery = {'version': RESPONSE_CONTRACT_VERSION, 'rejected_proposal_sha256': sha(rejected),
                'purpose': 'experiment_decide:' + ref['sha256'] + ':reference-contract-2:' + sha(rejected),
                'instruction': instruction + '\n只纠正最后被拒提案的证据引用与复验字段；仍须独立三方评审。',
                'context': recovery_context, 'schema': proposal_schema(PROPOSAL, evidence)}
            # This recovery can repair references/outcome labels only, never the intervention.
            for field, value in rejected.items():
                if field not in {'evidence_paths', 'training_response'}:
                    recovery['schema']['properties'][field] = {'const': value}
            for field, value in rejected['training_response'].items():
                if field not in {'evidence_paths', 'followup_outcome'}:
                    recovery['schema']['properties']['training_response']['properties'][field] = {'const': value}
            await checkpoint(proposal_contract_recovery=recovery)
        attempts = list(record.get('proposal_corrections', []))
        while True:
            correction = {'rejected_proposal': attempts[-1]['proposal'], 'validation_error': attempts[-1]['error'],
                'instruction': '只纠正本次诊断提案的具体问题；不能删除训练告警、改变证据或伪造已读回执。先核对既有运行产物；未读取不等于缺日志，只有确认具体缺项才能提出最小补测。'} if attempts else None
            if (correction and correction['rejected_proposal'].get('training_response', {}).get('disposition') == 'coverage_validation'
                    and correction['validation_error'].startswith('Frozen ablation requires')):
                candidates = context.get('operator_progression', {}).get('frozen_ablation_candidates', [])
                candidate = next((u for u in candidates if u['unit_id'] == correction['rejected_proposal']['unit_id']), None)
                if candidate:
                    correction['exact_frozen_differences'] = candidate
                    correction['instruction'] = ('仅纠正 training_response.factor_paths 的引用格式与漏项，其余方案字段保持原答复。'
                        '使用 exact_frozen_differences 中同种子控制的 parameter-relative factor_paths，包含已有契约字段差异；'
                        '不要加 /job_parameters/<job> 前缀，不列 seed/phase/variant，不按两个种子重复路径。'
                        '这是宿主对既有冻结参数的精确差异，不是新增实验或缺少日志。复用本角色已完成分析，'
                        '无需重做诊断、重复读取参数或补日志；纠正后仍由三方独立评审。')
            request = copy.deepcopy(context)
            if correction:
                request['experiment_decision']['proposal_correction'] = correction
            purpose = 'experiment_decide:' + ref['sha256'] + continuation_suffix + (f':training-correction-{len(attempts)}' if attempts else '')
            from .artifact_fact_review import KEY as FACT_REVIEW
            fact_spec = record.get(artifact_handoff.KEY, {}).get(FACT_REVIEW)
            pending_facts = bool(fact_spec and not record[artifact_handoff.KEY].get('independent_review'))
            if pending_facts:
                # Reuse the completed paid answer verbatim; do not redispatch it.
                recovery = fact_spec['source_assignment']
                proposal = copy.deepcopy(record['rejected_proposal'])
            else:
                assignment_sha = sha({k: recovery[k] for k in ('purpose', 'instruction', 'context', 'schema')}) if recovery else None
                delivered = record.get('recovery_answer') or {}
                cached_answer = (copy.deepcopy(delivered['proposal'])
                    if assignment_sha and delivered.get('assignment_sha256') == assignment_sha else None)
                proposal = retained_read_proposal or cached_answer or await harness.ask(run_id, 'engineer',
                recovery['purpose'] if recovery else purpose,
                recovery['instruction'] if recovery else instruction + '\n综合三方分析选择唯一下一步；不要把“未找到问题”写成“已证明没有问题”。'
                    + ('\n本次为字段纠正：' + correction['instruction'] if correction else ''),
                recovery['context'] if recovery else request,
                recovery['schema'] if recovery else proposal_schema(PROPOSAL, evidence))
                if recovery and not retained_read_proposal and not cached_answer:
                    # Retain delivery before validation. Resume revalidates the
                    # same answer; it must not redispatch a failed correction.
                    await checkpoint(recovery_answer={'assignment_sha256': assignment_sha,
                                                      'proposal': copy.deepcopy(proposal)})
            try:
                if recovery:
                    validate(proposal, recovery['schema'])
                if pending_facts:
                    recovery = await artifact_review.advance(engine, harness, run_id, record, context,
                        instruction, proposal_schema(PROPOSAL, evidence), checkpoint,
                        proposal=proposal, assignment=recovery)
                    continue
                if (record.get(diagnostic_planning.KEY) and recovery
                        and recovery == record[diagnostic_planning.KEY].get('synthesis')
                        and diagnostic_planning.is_hold(proposal)):
                    raise ValueError(diagnostic_planning.hold_message(proposal))
                from . import decision_reference_resolution as reference_resolution
                reference_context, reference_proof = reference_resolution.authenticate(
                    engine, run_id, record, proposal, evidence)
                if reference_proof and sha(proposal) not in record.get(reference_resolution.KEY, {}):
                    await checkpoint(**{reference_resolution.KEY: {
                        **record.get(reference_resolution.KEY, {}), sha(proposal): reference_proof}})
                validate_proposal(proposal, evidence, ref['sha256'],
                    artifact_evidence=artifact_handoff.bound_evidence(engine, run_id, record, proposal),
                    reference_context=reference_context)
                if proposal.get('architecture_review'):
                    from .architecture_experiment_contract import validate as validate_architecture
                    from .source_read_identity import source_files
                    validate_architecture(proposal, evidence, _pointer,
                        source_files=source_files(engine.store, run_id, context))
                if proposal['action'] == 'search_parameters' and evidence.get('hyperparameter_yaml', {}).get('ready'):
                    from .hyperparameter_yaml import validate_candidate as validate_yaml_candidate
                    from .parameter_search import propose as proposed_unit
                    validate_yaml_candidate(engine, run_id, proposal['parameter_search'], evidence, proposed_unit(proposal['parameter_search'], evidence))
                if artifact_handoff.requested(proposal):
                    if not record.get(artifact_handoff.KEY):
                        await artifact_handoff.prepare(engine, run_id, record, proposal, evidence, ref, checkpoint)
                        recovery = artifact_handoff.request(record, context, instruction, proposal_schema(PROPOSAL, evidence))
                    elif not record[artifact_handoff.KEY].get('independent_review'):
                        recovery = await artifact_review.advance(engine, harness, run_id, record, context,
                            instruction, proposal_schema(PROPOSAL, evidence), checkpoint,
                            proposal=proposal, assignment=recovery)
                    else:
                        if record.get(gate_followup.KEY) and recovery == record[gate_followup.KEY].get('synthesis'):
                            await gate_followup.finish_hold(engine, harness, run_id, record,
                                proposal, recovery, checkpoint)
                        followup = await gate_followup.advance(engine, harness, run_id, record,
                            evidence, context, instruction, checkpoint, proposal=proposal, assignment=recovery)
                        if followup:
                            recovery = followup
                            continue
                        await checkpoint(**{artifact_handoff.KEY: {**record[artifact_handoff.KEY],
                            'status': 'unresolved', 'owner': 'engineer', 'blocker': {
                                'reason': proposal['rationale'], 'required_evidence': proposal['verification']}}})
                        raise ValueError('产物独立复核后仍存在具体证据缺口：' + proposal['rationale']
                                         + '\n下一步所需：' + proposal['verification'])
                    continue
                if record.get(artifact_handoff.KEY):
                    read = artifact_review.author_reads(engine, run_id, record, recovery)
                    effective_reads = artifact_handoff.merge_reads(record['analysis_reads'], read)
                elif proposal.get('artifact_evidence_sha256') or proposal.get('artifact_review_sha256'):
                    raise ValueError('Artifact evidence hash requires its host-owned handoff')
                else:
                    effective_reads = record['analysis_reads']
                effective_reads = await proposal_reads.ensure_reads(
                    engine, run_id, record, proposal, effective_reads, context, checkpoint, harness=harness)
                validate_reads(effective_reads, proposal, evidence)
                effective_reads = await optimization_reads.ensure_reads(
                    engine, harness, run_id, record, proposal, effective_reads, context, checkpoint)
                if record.get(artifact_handoff.KEY):
                    await checkpoint(**{artifact_handoff.KEY: {**record[artifact_handoff.KEY],
                        'status': 'reviewing', 'author_read': read}}, analysis_reads=effective_reads)
                break
            except (ValueError, KeyError, IndexError, TypeError, ValidationError) as exc:
                if not recovery and evidence.get('training_investigation', {}).get('required') and len(attempts) < 2:
                    attempts.append({'proposal': proposal, 'error': str(exc)[:1200]})
                    await checkpoint(status='correcting', proposal_corrections=attempts)
                    continue
                await checkpoint(status='invalid', rejected_proposal=proposal, rejection=str(exc),
                                 proposal_contract_version=RESPONSE_CONTRACT_VERSION)
                raise ResearchBlocked(str(exc)) from exc
        await checkpoint(proposal=proposal, proposal_sha256=sha(proposal), status='reviewing')
    if record.get(artifact_handoff.KEY):
        from .experiment_artifact_review import require_binding
        if not require_binding(record):
            raise ResearchBlocked('Proposal is not bound to the completed independent artifact review')
        saved = record[artifact_handoff.KEY]
        from .artifact_fact_review import reference as artifact_reference
        if record['proposal'].get('artifact_evidence_sha256') != artifact_reference(saved)['sha256']:
            raise ResearchBlocked('Proposal is not bound to the supplied runtime artifacts')
        context['experiment_decision'][artifact_handoff.KEY] = artifact_handoff.packet(saved, for_review=True)
        if saved.get('independent_review'):
            results = saved['independent_review']['results']
            context['artifact_review_results'] = {'sha256': sha(results), 'results': results,
                'execution_authorized': False, 'scientific_approval': False}
    context, instruction = await anchor_for_phase(engine, run_id, record, context, instruction, checkpoint, 'vote')
    context.update(decision_proposal=record['proposal'], proposal_sha256=record['proposal_sha256'])
    from .optimization_read_recovery import vote_context
    context = vote_context(record, context)
    from .training_proposal_reads import vote_context as proposal_read_context
    context = proposal_read_context(record, context)
    from .diagnostic_planning_recovery import vote_context as diagnostic_planning_context
    context = diagnostic_planning_context(record, context)
    from .parameter_gate_followup import context_for as parameter_gate_context
    context = parameter_gate_context(record, context)
    if record.get('parameter_control_recovery'):
        context['experiment_decision']['parameter_control_recovery'] = copy.deepcopy(record['parameter_control_recovery'])
    if not record['votes']:
        vote_purpose = 'experiment_vote:' + record['proposal_sha256']
        vote_instruction = (instruction + '\n独立核对本批次必要性、假设、预算、停止条件和证据。只有无阻塞异议才批准。'
            'issues 仅填写阻止当前 action 的具体问题；批准时 issues=[]。已知局限与后续门禁保留在 rationale。')
        if record.get(artifact_handoff.KEY):
            from .experiment_artifact_review import vote_instruction as artifact_vote_instruction
            try:
                vote_instruction = artifact_vote_instruction(
                    engine, run_id, vote_purpose, vote_instruction, context, VOTE)
            except (ValueError, KeyError, TypeError) as exc:
                await checkpoint(status='blocked_evidence', rejection=str(exc))
                raise ResearchBlocked(str(exc)) from exc
        votes = await _group(harness, run_id, vote_purpose, vote_instruction, context, VOTE)
        if record.get(artifact_handoff.KEY):
            from .experiment_artifact_review import vote_reads
            try:
                reads = [vote_reads(engine, run_id, record, v['role'], vote_purpose,
                    vote_instruction, context, VOTE) for v in votes]
            except (ValueError, KeyError, TypeError, StopIteration) as exc:
                await checkpoint(status='blocked_evidence', rejection=str(exc))
                raise ResearchBlocked(str(exc)) from exc
            await checkpoint(**{artifact_handoff.KEY: {**record[artifact_handoff.KEY], 'review_reads': reads}})
        accepted = all(v['approve'] is True and not v['issues'] and v['evidence_sha256'] == ref['sha256']
                       and v['proposal_sha256'] == record['proposal_sha256'] for v in votes)
        await checkpoint(votes=votes, status='approved' if accepted else 'disputed')
        engine.store.event(run_id, 'experiment.decision', record['proposal']['action'],
            {'decision_id': key, 'evidence_sha256': ref['sha256'], 'status': record['status']})
    if record.get('vote_clarifications') or any(v.get('approve') is True and v.get('issues') for v in record['votes']):
        from .experiment_vote_clarification import clarify
        votes = await clarify(engine, harness, run_id, record, checkpoint)
        accepted = all(v['approve'] is True and not v['issues'] and v['evidence_sha256'] == ref['sha256']
                       and v['proposal_sha256'] == record['proposal_sha256'] for v in votes)
        await checkpoint(votes=votes, status='approved' if accepted else 'disputed')
        engine.store.event(run_id, 'experiment.decision_clarified', record['proposal']['action'],
            {'decision_id': key, 'status': record['status'], 'approval_inherited': False})
    from .parameter_path_contract import reconsider as reconsider_parameter_paths
    votes = await reconsider_parameter_paths(engine, harness, run_id, record, evidence, checkpoint)
    if record.get('parameter_path_reviews'):
        accepted = all(v['approve'] is True and not v['issues'] and v['evidence_sha256'] == ref['sha256']
                       and v['proposal_sha256'] == record['proposal_sha256'] for v in votes)
        await checkpoint(votes=votes, status='approved' if accepted else 'disputed')
    await checkpoint()
    validate_proposal(record['proposal'], evidence, ref['sha256'],
        artifact_evidence=artifact_handoff.bound_evidence(engine, run_id, record, record['proposal']))
    if record['proposal']['action'] == 'search_parameters' and evidence.get('hyperparameter_yaml', {}).get('ready'):
        from .hyperparameter_yaml import validate_candidate as validate_yaml_candidate
        from .parameter_search import propose as proposed_unit
        validate_yaml_candidate(engine, run_id, record['proposal']['parameter_search'], evidence, proposed_unit(record['proposal']['parameter_search'], evidence))
    validate_reads(record['analysis_reads'], record['proposal'], evidence)
    if not _approved(record):
        from .experiment_consensus import DecisionDissent
        raise DecisionDissent('实验下一步决策尚未获三方一致批准；异议和证据已保存。')
    if record.get(artifact_handoff.KEY):
        await checkpoint(**{artifact_handoff.KEY: {**record[artifact_handoff.KEY], 'status': 'completed'}})
    remember(engine, run_id, evidence, ref, decision=record)
    return record, evidence


def analysis_schema(context):
    from .dataset_optimization import action_schema
    result = action_schema(ANALYSIS, context.get('experiment_decision', {}).get('dataset_optimization'), 'suggested_action')
    if context.get('experiment_decision', {}).get('tuning_scope', {}).get('status') != 'accepted':
        result['properties']['suggested_action']['enum'] = [a for a in result['properties']['suggested_action']['enum'] if a != 'revise_tuning_scope']
    return result


def analysis_receipts(engine, run_id, analysis, context, *, purpose=None, instruction=None):
    from .engine import Harness, ResearchBlocked
    purpose = purpose or 'experiment_diagnose:' + context['evidence_sha256']
    from .training_investigation import guidance
    key = Harness.decision_key(run_id, analysis['role'], purpose, instruction or guidance(GUIDANCE, context),
                               context, analysis_schema(context))
    handoff = engine.store.get('handoffs', key) or {}
    frozen_input = engine.store.get('handoff_inputs', key) or {}
    if (handoff.get('run_id') != run_id or handoff.get('to_role') != analysis['role']
            or frozen_input.get('context', {}).get('validation_evidence') != context['validation_evidence']):
        raise ResearchBlocked('Analysis receipts are not owned by this exact role and handoff')
    progress = engine.store.get('handoff_progress', key) or {}
    valid, kinds, source = set(), set(), False
    training_trials, source_paths = set(), set()
    coverage = {}
    strict = context.get('experiment_decision', {}).get('training_investigation', {}).get('version', 1) >= 2
    from .training_read_continuation import retained_rows, retained_handoff
    prior_reads = retained_rows(engine, run_id, analysis['role'], context)
    for receipt in prior_reads + progress.get('tool_results', []):
        call, result = receipt.get('call') or {}, receipt.get('result') or {}
        identity = result.get('receipt_sha256')
        if (not result.get('text')
                or identity != sha({'call': call, 'result': {k: v for k, v in result.items() if k != 'receipt_sha256'}})
                or identity not in analysis['read_receipts']):
            continue
        if call.get('name') in {'experiments.analyze', 'experiments.inspect_module'}:
            if result.get('evidence_sha256') != context['evidence_sha256']:
                continue
        elif not _supplementary_analysis_read(engine, run_id, context, call, result):
            continue
        valid.add(identity)
        if call['name'] == 'experiments.analyze':
            kinds.add(result['kind'])
            if result['kind'] == 'training':
                trial = call['arguments']['trial_id']
                if not strict:
                    training_trials.add(trial)
                else:
                    archived = engine.store.get('experiment_analyses', result.get('analysis_id', '')) or {}
                    report = archived.get('result') or {}
                    text = encoded(report)
                    start, end = result.get('offset'), result.get('end')
                    if (archived.get('run_id') == run_id and archived.get('source_evidence_id') == context['validation_evidence']['id']
                        and archived.get('source_evidence_sha256') == context['evidence_sha256']
                        and archived.get('sha256') == result.get('content_sha256') == sha(report)
                        and report.get('critical_training_packet', {}).get('version') == 2
                        and report['critical_training_packet'].get('trial_id') == trial
                        and type(start) is int and type(end) is int and 0 <= start < end <= len(text)
                        and result.get('total_chars') == len(text) and result['text'] == text[start:end]):
                        item = coverage.setdefault(trial, {'analysis_sha256': sha(report), 'total_chars': len(text), 'ranges': []})
                        if item['analysis_sha256'] == sha(report):
                            item['ranges'].append([start,end])
        if result.get('source_read') is True:
            source = True
            source_paths.add(result['path'])
    if set(analysis['read_receipts']) != valid or not kinds:
        raise ResearchBlocked('Agent analysis lacks actual same-role, same-evidence tool read receipts')
    from .saved_runtime_audit import require_reads as require_runtime_audit_reads
    try:
        require_runtime_audit_reads(engine, run_id, context, prior_reads + progress.get('tool_results', []))
    except ValueError as exc:
        raise ResearchBlocked(str(exc)) from exc
    for trial, item in coverage.items():
        end = 0
        for start, stop in sorted(item['ranges']):
            if start > end:
                break
            end = max(end, stop)
        item['complete'] = end == item['total_chars']
        if item['complete']:
            training_trials.add(trial)
    return {'role': analysis['role'], 'handoff_id': key, 'analysis_kinds': sorted(kinds),
            'module_source_read': source, 'receipt_sha256s': sorted(valid),
            'training_trials_read': sorted(training_trials), 'source_paths': sorted(source_paths),
            'critical_training_reads': coverage,
            **({'retained_read_origin': {
                'handoff_id': retained_handoff(context),
                'receipt_sha256s': sorted({r['result']['receipt_sha256'] for r in prior_reads} & valid)}}
               if prior_reads else {})}


def _supplementary_analysis_read(engine, run_id, context, call, result):
    """Accept authentic diagnostic/compare reads without substituting for analysis."""
    from . import experiment_tools
    args = call.get('arguments') or {}
    body = {k:v for k,v in result.items() if k != 'receipt_sha256'}
    try:
        if call.get('name') == 'hyperparameters.read':
            from .hyperparameter_yaml import replay_read
            return replay_read(engine, run_id, context, args) == body
        if call.get('name') == 'experiments.compare':
            return experiment_tools.compare(engine, run_id, context, args) == body
        if call.get('name') != 'experiments.read':
            return False
        ref = context['validation_evidence']
        record = engine.store.get('experiment_evidence', ref['id']) or {}
        evidence = record.get('summary') or {}
        if record.get('run_id') != run_id or sha(evidence) != ref['sha256']:
            return False
        row = next((r for r in evidence.get('records',[]) if r['id'] == args.get('trial_id')), None)
        if row is None:
            return False
        cycle = evidence['binding']['cycle']
        trial = experiment_tools._trial(engine, run_id, args['trial_id'], cycle)
        fields = {'status':'status', 'attempt':'attempt', 'code_revision':'code_revision',
                  'code_sha256':'code_sha256', 'experiment_plan_sha256':'plan_sha256', 'params':'params'}
        if any(row.get(old) != trial.get(new) for old,new in fields.items()):
            return False
        return experiment_tools.read(engine, run_id, cycle, args) == body
    except (KeyError, ValueError, TypeError):
        return False


def _assert_binding(engine, run_id, state):
    from .engine import ResearchBlocked
    if not state or _binding(engine, run_id)[1] != state['binding']:
        raise ResearchBlocked('Experiment decision code, plan, cycle or resource configuration changed')


def _approved(record):
    votes = record.get('votes', [])
    reads = record.get('analysis_reads', [])
    from .experiment_evidence_handoff import KEY, requested
    from .experiment_artifact_review import require_binding
    from .optimization_read_recovery import complete
    from .training_read_dispatch import complete as training_reads_complete
    from .artifact_fact_review import reference as artifact_reference
    handoff = record.get(KEY)
    if (requested(record.get('proposal') or {}) or not require_binding(record)
            or not complete(reads, record.get('proposal') or {}) or not training_reads_complete(record)):
        return False  # A request for evidence is never execution authorization.
    if handoff and (record.get('proposal', {}).get('artifact_evidence_sha256') != artifact_reference(handoff)['sha256']
            or not handoff.get('author_read')
            or {r.get('role') for r in handoff.get('review_reads', [])} != {'director', 'engineer', 'scout'}):
        return False
    return (record.get('status') == 'approved' and record.get('proposal_sha256') == sha(record.get('proposal'))
        and len(reads) == 3 and {r.get('role') for r in reads} == {'director', 'engineer', 'scout'}
        and len(votes) == 3 and {v.get('role') for v in votes} == {'director', 'engineer', 'scout'}
        and all(v.get('approve') is True and v.get('issues') == []
            and v.get('proposal_sha256') == record['proposal_sha256']
            and v.get('evidence_sha256') == record.get('evidence', {}).get('sha256') for v in votes))


def authorize_job(engine, run_id, name, params):
    """Independent execution boundary, including direct controller invocations."""
    from .engine import ResearchBlocked
    state = session(engine, run_id)
    if not state:
        return False
    _assert_binding(engine, run_id, state)
    pending = state.get('pending') or {}
    if pending.get('status') != 'executing' or dict(pending.get('jobs', [])).get(name) != params:
        raise ResearchBlocked('This validation job has no current one-batch decision authorization')
    if pending.get('decision_id'):
        decision = engine.store.get(DECISIONS, pending['decision_id']) or {}
        if (not _approved(decision) or decision.get('run_id') != run_id
                or decision.get('proposal_sha256') != pending['proposal_sha256']
                or decision['proposal']['action'] != 'search_parameters' and decision['proposal']['unit_id'] != pending['unit_id']):
            raise ResearchBlocked('The batch decision approval is missing or changed')
        ref = decision['evidence']
        archived = engine.store.get('experiment_evidence', ref['id']) or {}
        if sha(archived.get('summary')) != ref['sha256']:
            raise ResearchBlocked('The batch evidence archive is missing or changed')
        if decision['proposal']['action'] == 'search_parameters':
            from .dataset_optimization import route
            if route(engine, run_id, 'tuning'):
                raise ResearchBlocked('Architecture assessment is required before parameter execution')
            from .parameter_search import catalog
            unit = next((u for u in catalog(engine, run_id) if u['id'] == pending['unit_id']), None)
            if not unit or dict(unit['jobs']).get(name) != params:
                raise ResearchBlocked('Supplemental parameters differ from the approved manifest')
        elif archived['summary']['job_parameters'].get(name) != params:
            raise ResearchBlocked('Batch parameters differ from the decision evidence')
    return True


async def _execute(engine, harness, run_id, state, transport=None):
    from .engine import ResearchBlocked
    from .python_training import run as train
    pending = state['pending']
    _assert_binding(engine, run_id, state)
    # Only this batch may add results between approval and execution/resume.
    # No prior comparison can silently disappear/change while its decision runs.
    own = {sha(p) for _, p in pending['jobs']}
    current = _safe_rows([r for r in validation_records(engine.store, run_id)
        if (r.get('research_cycle') or 1) == engine.cycle(run_id) and not r.get('superseded')])
    outside = {r['id']: sha(r) for r in current if sha(r['params']) not in own}
    if outside != pending['outside_records']:
        raise ResearchBlocked('Prior experiment evidence changed after batch authorization')
    for name, params in pending['jobs']:
        engine.check_active(run_id)
        view = await engine._read_consensus_check(run_id, ledger, engine, run_id)
        head = next(j for j in view['jobs'] if j['name'] == name)
        if head['state'] == 'completed':
            continue
        if head['state'] != 'missing':
            pending.update(status='failed', stopped_at_job=name)
            break
        authorize_job(engine, run_id, name, params)
        try:
            await train(engine, harness, run_id, name, copy.deepcopy(params))
        except ResearchBlocked:
            engine.check_active(run_id)
            current = await engine._read_consensus_check(run_id, ledger, engine, run_id)
            failed = next(j for j in current['jobs'] if j['name'] == name)
            if failed['state'] in {'missing', 'completed', 'running'}:
                raise
            pending.update(status='failed', stopped_at_job=name)
            break
        after = await engine._read_consensus_check(run_id, ledger, engine, run_id)
        if next(j for j in after['jobs'] if j['name'] == name)['state'] != 'completed':
            raise ResearchBlocked('Training completion has no matching committed experiment receipt')
        # Commit progress after every job. Runner receipts remain authoritative after a crash.
        pending.setdefault('completed_jobs', []).append(name)
        engine.store.put(SESSIONS, state['id'], state)
    else:
        pending['status'] = 'completed'
    state.update(last_batch=copy.deepcopy(pending), pending=None, updated_at=now())
    engine.store.put(SESSIONS, state['id'], state)
    engine.store.event(run_id, 'experiment.batch_finished', pending['status'],
        {'decision_id': pending.get('decision_id'), 'unit_id': pending['unit_id'],
         'jobs': [n for n, _ in pending['jobs']]})
    from .experiment_memory import remember
    evidence = await engine._read_consensus_check(run_id, snapshot, engine, run_id, transport)
    ref = _archive(engine, run_id, evidence)
    remember(engine, run_id, evidence, ref, batch=pending,
        decision=engine.store.get(DECISIONS, pending.get('decision_id', '')))


def _commit_batch(engine, run_id, state, unit, decision=None):
    own = {sha(p) for _, p in unit['jobs']}
    rows = _safe_rows([r for r in validation_records(engine.store, run_id)
        if (r.get('research_cycle') or 1) == engine.cycle(run_id) and not r.get('superseded')])
    from .rapid_experiment_revision import _budget
    view = ledger(engine, run_id)
    remaining = sum(j['state'] == 'missing' and j['name'] in dict(unit['jobs']) for j in view['jobs'])
    _budget(engine, run_id, remaining)
    state['pending'] = {'unit_id': unit['id'], 'jobs': unit['jobs'], 'status': 'executing',
        'outside_records': {r['id']: sha(r) for r in rows if sha(r['params']) not in own},
        'decision_id': decision['id'] if decision else None,
        'proposal_sha256': decision['proposal_sha256'] if decision else None,
        'authorization': 'three_agent_decision' if decision else 'approved_plan_initial_baseline_comparison'}
    if decision:
        state['last_decision'] = decision['id']
    engine.store.put(SESSIONS, state['id'], state)


async def run_stage(engine, harness, run_id, stage, plan, transport=None):
    """Initial paired pilot, then one decision after each indivisible batch."""
    from .engine import ResearchBlocked, ResearchRevisionRequired, ResearchExperimentStopped
    from .experiment_evidence_handoff import requested as artifact_handoff_requested
    scope_checked = False
    if stage == 'tuning':
        from .yaml_migration import required as yaml_migration_required
        migration_due = yaml_migration_required(engine, run_id)
        # Plan the newly requested scope before starting another diagnostic;
        # an already committed experiment batch finishes first.
        if not migration_due and not (session(engine, run_id) or {}).get('pending'):
            from .tuning_scope import ensure as ensure_tuning_scope
            scoped = await ensure_tuning_scope(engine, harness, run_id)
            scope_checked = True
            if scoped:
                return scoped
        from .tuning_diagnostics import execute
        repair = await execute(engine, harness, run_id) if not migration_due else None
        if repair:
            return repair
    state = session(engine, run_id)
    if not state:
        _, binding = await engine._read_consensus_check(run_id, _binding, engine, run_id)
        state = {'id': f'{run_id}:{engine.cycle(run_id)}', 'run_id': run_id,
                 'binding': binding, 'created_at': now(), 'pending': None}
        engine.store.put(SESSIONS, state['id'], state)
        evidence = await engine._read_consensus_check(run_id, snapshot, engine, run_id, transport)
        if not evidence['records']:
            initial = next(u for u in units(plan, transport) if u['kind'] == 'pilot')
            _commit_batch(engine, run_id, state, initial)
    _assert_binding(engine, run_id, state)
    if state.get('pending'):
        if stage == 'architecture_tuning':
            return _dispatch_architecture_validation(engine, run_id)
        await _execute(engine, harness, run_id, state, transport)
    if stage == 'experiments':
        return {'adaptive_decisions': True, 'session_id': state['id'], 'plan_hash': sha(plan)}
    from .dataset_optimization import route
    redirect = route(engine, run_id, stage)
    if redirect:
        return {'optimization_transition': True, 'next_node': redirect, 'stable': False}
    if stage == 'tuning' and not scope_checked and not migration_due:
        from .tuning_scope import ensure as ensure_tuning_scope
        planned = await ensure_tuning_scope(engine, harness, run_id)
        if planned:
            return planned
    # The finite frozen pool bounds the number of execution decisions; a decision
    # with no executable batch must terminate or enter a newly reviewed cycle.
    for _ in range(len(units(plan, transport)) + 1):
        from .experiment_consensus import ConsensusPending
        try:
            decision, evidence = await decide(engine, harness, run_id, transport)
        except ConsensusPending:
            from .tuning_consensus import pending
            return pending(stage=stage)
        except ResearchBlocked:
            # A delivered read-only hold is an input to a planning task, not a
            # terminal stage. The new task cannot inherit execution approval.
            from .parameter_resolution import resume as resolve_parameters
            evidence = await engine._read_consensus_check(run_id, snapshot, engine, run_id, transport)
            ref = _archive(engine, run_id, evidence)
            held = engine.store.get(DECISIONS, ref['id']) or {}
            successor = await resolve_parameters(engine, harness, run_id, evidence, held)
            if successor:
                return successor
            raise
        proposal = decision['proposal']
        if proposal['action'] == 'revise_tuning_scope':
            from .tuning_scope import reopen as refine_tuning_scope
            return refine_tuning_scope(engine, run_id, decision, evidence)
        if artifact_handoff_requested(proposal):
            raise ResearchBlocked('Read-only evidence requests must finish their engineer handoff before dispatch')
        state['last_decision'] = decision['id']
        engine.store.put(SESSIONS, state['id'], state)
        if proposal['action'] in {'accept_architecture', 'complete_optimization'}:
            from .dataset_optimization import record
            return record(engine, run_id, decision, evidence)
        if proposal['action'] == 'review':
            return {'adaptive_decisions': True, 'decision_id': decision['id']}
        if proposal['action'] == 'stop':
            raise ResearchExperimentStopped(decision['id'], proposal['rationale'])
        if proposal['action'] == 'search_parameters':
            from .parameter_search import register
            unit = register(engine, run_id, decision, evidence)
            _commit_batch(engine, run_id, state, unit, decision)
            await _execute(engine, harness, run_id, state, transport)
            return {'adaptive_decisions': True, 'tuning_pending': True, 'next_node': 'tuning',
                    'decision_id': decision['id'], 'activity': 'parameter_search'}
        if proposal['action'] == 'revise_architecture' or proposal['unit_id'] is None:
            raise ResearchRevisionRequired(proposal['rationale'], kind='experiment_decision_revision',
                details={'decision_id': decision['id'], 'proposal_sha256': decision['proposal_sha256'],
                         'evidence_sha256': decision['evidence']['sha256']})
        from .parameter_search import catalog as search_catalog
        unit = next(u for u in units(plan, transport) + search_catalog(engine, run_id, plan) if u['id'] == proposal['unit_id'])
        _commit_batch(engine, run_id, state, unit, decision)
        if stage == 'architecture_tuning':
            return _dispatch_architecture_validation(engine, run_id)
        await _execute(engine, harness, run_id, state, transport)
    raise ResearchBlocked('实验决策没有取得进展，已停止派发；检查持久化决策记录。')


def _dispatch_architecture_validation(engine, run_id):
    """Run an approved discriminating batch in the validation node, then return
    to architecture assessment. Old completed validation receipts stay saved."""
    key = run_id + ':experiments'
    old = engine.store.get('steps', key)
    if old:
        engine.store.batch([('cycle_steps', key + ':before-architecture-probe:' + sha(old), old)],
                           deletes=[('steps', key)])
    return {'optimization_pending': True, 'next_node': 'experiments',
            'activity': 'architecture_validation', 'semantic_completion': False, 'stable': False}


def revision_context(engine, run_id, review):
    failure = review.get('failure', {})
    if failure.get('kind') != 'experiment_decision_revision':
        return None
    from .engine import ResearchBlocked
    record = engine.store.get(DECISIONS, failure.get('details', {}).get('decision_id', '')) or {}
    if (not _approved(record) or record.get('run_id') != run_id
            or record.get('cycle') != engine.cycle(run_id)
            or record.get('proposal_sha256') != sha(record.get('proposal'))
            or record.get('proposal_sha256') != failure['details'].get('proposal_sha256')):
        raise ResearchBlocked('The experiment revision has no matching approved decision')
    _assert_binding(engine, run_id, session(engine, run_id))
    return {'decision_id': record['id'], 'proposal': record['proposal'], 'evidence': record['evidence'],
            'votes': record['votes'], 'scientific_approval': False}


def record_stop(engine, run_id, stage, step, decision_id):
    """Report-only termination, retaining unexecuted jobs as missing, not passed."""
    record = engine.store.get(DECISIONS, decision_id)
    from .engine import ResearchBlocked
    if not _approved(record) or record['run_id'] != run_id or record['proposal']['action'] != 'stop':
        raise ResearchBlocked('Exploration stop lacks an approved current decision')
    from .dataset_optimization import enabled as architecture_first
    if architecture_first(engine.store, run_id):
        # An old paid decision must never recreate the removed report branch.
        raise ResearchBlocked('提前收尾分支已删除；保留原停止意见，须在当前阶段完成架构核验与参数优化。')
    evidence = engine.store.get('experiment_evidence', record['evidence']['id'])['summary']
    outcome = {'research_outcome': 'inconclusive', 'phase': 'validation', 'checks': [],
        **{k: evidence[k] for k in ('performance_policy', 'performance_assessment') if k in evidence},
        'reason': record['proposal']['rationale'], 'stop_kind': 'agent_exploration_stop',
        'decision_id': decision_id, 'evidence': record['evidence'],
        'coverage': evidence['ledger']['counts'], 'final_tests_performed': False,
        'publication_ready': False, 'scientific_approval': False,
        'scope': 'Exploration stopped by reviewed decision; missing experiments remain unexecuted. No method inferiority or success claim.'}
    cycle = engine.cycle(run_id)
    review = {'id': f'{run_id}:{cycle}', 'run_id': run_id, 'cycle': cycle, 'decision': 'stop',
        'status': 'reviewed', 'review_type': 'experiment_decision', 'rationale': outcome['reason'],
        'outcome': outcome, 'experiment_decision_id': decision_id, 'review_votes': record['votes'],
        'validation_summary': {'phase': 'validation', 'comparable': False},
        'completed_at': now(), 'provenance': 'live'}
    stop_step = {'id': run_id + ':validation_review', 'run_id': run_id, 'stage': 'validation_review',
        'cycle': cycle, 'status': 'completed', 'output': review, 'next_node': 'report', 'completed_at': now()}
    engine.store.batch([('research_cycles', review['id'], review), ('steps', stop_step['id'], stop_step),
        ('steps', step['id'], {**step, 'status': 'completed',
            'output': {'decision_id': decision_id, 'stopped_early': True},
            'next_node': 'report', 'completed_at': now()})])
    engine.artifact(run_id, f'cycle-{cycle}/exploration-stop.json', encoded(outcome), 'evaluation')
    engine.store.event(run_id, 'experiment.exploration_stopped', outcome['reason'],
        {'decision_id': decision_id, 'scientific_approval': False})
    return {'cycle': cycle, 'next_node': 'report'}


def record_revision(engine, run_id, stage, step, failure):
    from .engine import ResearchBlocked
    reviewed = revision_context(engine, run_id, {'failure': failure})
    from .training_investigation import scoped_repair
    if stage in {'tuning', 'architecture_tuning'} and scoped_repair(reviewed['proposal']):
        from .tuning_diagnostics import begin
        begin(engine, run_id, {'failure': failure}, step)
        return {'cycle': engine.cycle(run_id), 'next_node': 'tuning'}
    cycle = engine.cycle(run_id)
    run = engine.store.get('runs', run_id)
    from .rapid_experiment_revision import count
    from .training_investigation import scoped_repair
    from .dataset_optimization import revision_allowed
    allowed = scoped_repair(reviewed['proposal']) or revision_allowed(engine, run_id)
    record = {'id': f'{run_id}:{cycle}', 'run_id': run_id, 'cycle': cycle,
        'decision': 'iterate' if allowed else 'blocked', 'status': 'revision_required' if allowed else 'blocked',
        'review_type': 'experiment_decision', 'failure_stage': stage, 'failure': failure,
        'rationale': failure['message'], 'experiment_decision_id': reviewed['decision_id'],
        'evidence': reviewed['evidence'], 'review_votes': reviewed['votes'],
        'validation_summary': {'phase': 'validation', 'comparable': False,
                               'scope': 'Partial exploration; retrieve frozen decision evidence for exact comparisons.'},
        'scientific_approval': False, 'started_at': step['started_at'], 'completed_at': now(), 'provenance': 'live'}
    engine.store.batch([('research_cycles', record['id'], record), ('steps', step['id'],
        {**step, 'status': 'completed', 'output': record, 'next_node': 'revision' if allowed else None,
         'completed_at': now()})])
    engine.store.event(run_id, 'experiment.revision_requested', failure['message'],
        {'decision_id': reviewed['decision_id'], 'cycle': cycle, 'allowed': allowed})
    if not allowed:
        raise ResearchBlocked('Research cycle limit exhausted; reviewed adjustment retained without new execution')
    if stage in {'experiments','tuning','architecture_tuning'} and reviewed['proposal']['action']=='revise_architecture':
        from .optimization_loop import begin
        return begin(engine,run_id,{'failure':failure},step)
    return {'cycle': cycle, 'next_node': 'revision'}


def history(store, run_id, *, before=None, limit=8):
    """Bounded audit projection; large evidence is retrieved separately by SHA."""
    import json
    with store._read_db() as db:
        rows = db.execute("SELECT key,updated_at,json_object('status',json_extract(data,'$.status'),"
            "'cycle',json_extract(data,'$.cycle'),'proposal',json_extract(data,'$.proposal'),"
            "'votes',json_extract(data,'$.votes'),'evidence',json_extract(data,'$.evidence')) "
            "FROM records WHERE collection=? AND json_extract(data,'$.run_id')=? "
            + ("AND (updated_at,key)<(?,?) " if before else '')
            + 'ORDER BY updated_at DESC,key DESC LIMIT ?',
            (DECISIONS, run_id, *((before or [])), limit + 1)).fetchall()
    return {'items': [{'id': key, 'updated_at': updated, **json.loads(data)} for key, updated, data in rows[:limit]],
        'next_cursor': [rows[limit-1][1], rows[limit-1][0]] if len(rows) > limit else None}
