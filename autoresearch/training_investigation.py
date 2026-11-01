"""Task-independent training incidents, discriminating evidence and follow-up gates.

Raw telemetry stays in the experiment directory. Decisions receive a small index;
all diagnosis pages are immutable, SHA-bound and available to each reviewer.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math

from .training_adjustment import obj
from .training_convergence import finite, monitor_values, _series, segment_identity, failure_observation

VERSION = 2
RESPONSE_CONTRACT_VERSION = 2
MEASURED_PREFIXES = ('/training_investigation/', '/diagnostics/', '/records/', '/comparisons/')
TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 600}
STRINGS = {'type': 'array', 'items': TEXT, 'minItems': 1, 'maxItems': 8, 'uniqueItems': True}
RESPONSE = obj({
    'trial_ids': STRINGS,
    'cause': {'enum': ['batch_noise', 'learning_rate', 'gradient', 'loss_scale', 'data_pipeline',
                       'implementation', 'telemetry', 'unknown', 'regularization', 'capacity', 'optimizer_dynamics']},
    'cause_status': {'enum': ['hypothesis', 'supported', 'refuted']},
    'disposition': {'enum': ['instrument', 'controlled_diagnostic', 'adjust_parameters',
                             'repair_code', 'continue', 'coverage_validation', 'resolved', 'stop',
                             'architecture_assessment', 'scope_planning']},
    'evidence_paths': STRINGS, 'module_paths': STRINGS,
    'measurement': TEXT, 'control': TEXT, 'expected_result': TEXT, 'failure_result': TEXT,
    'rollback_condition': TEXT,
    'factor_paths': {'type': 'array', 'items': TEXT, 'maxItems': 32, 'uniqueItems': True},
    'followup_outcome': {'enum': ['not_applicable', 'supported', 'refuted', 'inconclusive', 'regressed']},
})
RESPONSE['allOf'] = [{'if': {'properties': {'disposition': {'const': 'coverage_validation'}}},
    'else': {'properties': {'factor_paths': {'maxItems': 3}}}}]
GUIDANCE = '''训练异常闭环：training_investigation 的 focus_trial_ids 是本轮必须处理的训练告警，优先包括基线。
每个角色先对这些 trial 调用 experiments.analyze(kind=training)，并 inspect_module(source) 读取相关训练/数据/日志模块。
version=2 的 training 报告是关键统计必读包：使用 limit=6000，若 next_offset 非空继续读取直至覆盖完整报告，将全部页的 receipt_sha256 写入 read_receipts。只读第一页不能通过门禁。
先核对完整 epoch 趋势、验证最佳点与末点/最终检查点、实际梯度/更新采样覆盖率、失败事件和运行身份。已有完整统计不得再次称为缺失或重复插桩；模块级观测不足仍为未知。训练目标不同不能直接比较绝对 loss。
需要核对完整轨迹时，按 training 报告的 validation_trajectory_path 与 epoch_trajectory_path 用 experiments.compare 分页读取既有验证点及逐epoch聚合；路径仅在包含这些字段的新证据中提供。保留epoch_complete与reported_summary_consistent的未知或不完整状态。不同训练长度的末点不可冒充相同步数对照，轨迹比较本身不证明根因或参数稳定。
区分 batch loss、epoch 聚合和任务配置的验证指标；阶段/fit/loss 定义不同不能直接混合。没有梯度、学习率、采样或运行路径记录就是未知。
明确观测、候选原因、模块位置、排除竞争解释的实验和成功/失败/回退条件。震荡本身既不证明架构错误，也不证明只是噪声。
提案必须填写 training_response。缺少可区分原因的观测时优先 instrument：diagnose、unit_id=null、最小插桩 changes，交给 Codex 修复后按原计划复验。
training_response.evidence_paths 只允许 /training_investigation/、/diagnostics/、/records/、/comparisons/ 下实际存在的具体 JSON pointer；不能只填目录根节点。/rapid_revision 等流程记录可放在顶层 evidence_paths，不能放入训练实测证据。
training_response.trial_ids 必须覆盖 focus_trial_ids，且只能填写当前 /records 中的训练 ID。独立受控检查 ID 用 evidence_paths 引用其 outcome，不加入 trial_ids。followup_outcome 只评价 /training_investigation/followup 中的新训练，不评价独立 causal_followup 或 controlled_checks；该 followup 无 new_trial_ids 时填写 not_applicable。
证据支持实现问题时 repair_code，复用快速修改、独立差异审查、CPU 断言、成对验证通道；参数问题优先 adjust_parameters，选已批准参数范围内一个有同种子对照的批次。
controlled_diagnostic/adjust_parameters 的 factor_paths 是 job_parameters 中实际变化参数的 JSON pointer；只改本轮区分原因所需因素，保持数据、种子、评估与公平性。
不能用只验证模块贡献的普通消融替代训练诊断。controlled_diagnostic 消融必须说明其变化因素如何区分当前训练原因，并与已有同种子控制组严格匹配。
已完成受控检查及三方结果复核后，可用 coverage_validation 补齐冻结消融：action=diagnose、unit_id=待执行消融、changes=[]。这只补模块贡献证据，不声称解决训练异常；cause=unknown、cause_status=hypothesis，明确保留未定根因、原代码及同种子控制，factor_paths 精确列出已有消融与 proposed 的差异。不得借此激活候选补丁、修改参数、宣称稳定或重跑已完成训练。CPU 候选检查是否激活不影响原代码冻结消融的独立评审。
continue 必须有支持正常批次波动/记录口径原因的聚合和验证观测；否则采集证据。仍可 stop 并准确记录未解决的问题，不宣布修复或验收成功。
followup 有上一轮修改时必须根据新运行的 loss、任务指标及失败记录给出 supported/refuted/inconclusive/regressed；未获得新运行不能声称修复成功。
followup.new_trial_ids 为空时 followup_outcome 必须为 not_applicable；inconclusive 表示已有新复验但结果无法定论，不能用于尚未复验或补丁失败。
复验后告警消失且有有效成对结果，才可 resolved；出现回归不能 continue，应回退修改或提出下一项针对性诊断。resolved 仅表示本次训练问题的局部复验，不代替科研验收。
一次只运行一个诊断或调整批次，结束即重判。保留旧证据，不因试验数量耗尽而跳过当前显式的不限次数策略；仍遵守资源和任务协议。
只在请求中保留告警索引、当前假设与下一步；详细日志/源码/历史按 SHA 定向读取并保存笔记。'''

INSTRUMENTATION = '''落实 training_response 的观测与判别条件。诊断日志使用有界数值观测：实际 step、epoch、stage、fit_id、loss_name、batch_size、learning_rate；按诊断需要记录 grad_norm（裁剪前）、clip_fraction、update_norm、parameter_norm、input_norm/target_norm 或数值异常计数。
需要定位模块时，在现有采样点记录 modules:{模块名:{grad_norm,activation_norm,update_norm,zero_gradient_fraction,nonfinite_count}} 的适用统计，最多16个相关模块；没有观测的字段不填值。临时 hooks 须释放，不改变 autograd、模型输出或随机数状态。
监控指标从当前任务配置读取，保存其名称、方向及发生验证的真实时点；没有验证值的训练事件不填虚构数值。阶段和 loss 定义必须真实明确。
不要保存样本、预测数组或完整梯度到上下文。仅保留少量统计日志并原子落盘，失败也保留诊断；不引入额外训练、留出集读取、隐式调参或随机数消耗。
CPU 断言验证观测实际计算、阶段/epoch 标记、缺失/非有限数值、配置入口和模型行为不变；真实训练的改善须由下一轮运行结果验证。'''


def scoped_repair(proposal):
    """Training instrumentation/implementation fixes are not new research cycles."""
    return proposal.get('action') == 'diagnose' and proposal.get('unit_id') is None and \
        proposal.get('training_response', {}).get('disposition') in {'instrument', 'repair_code'}


def proposal_schema(base, evidence):
    from .dataset_optimization import action_schema
    schema = action_schema(base, evidence.get('dataset_optimization'))
    if evidence.get('tuning_scope', {}).get('status') != 'accepted':
        schema['properties']['action']['enum'] = [a for a in schema['properties']['action']['enum'] if a != 'revise_tuning_scope']
    if 'parameter_search' in schema.get('properties', {}):
        required = ['validation_seed']
        if evidence.get('parameter_search_protocol', 1) >= 2:
            required += ['grid_values', 'grid_rationale']
        schema.setdefault('allOf', []).append({
            'if': {'properties': {'action': {'const': 'search_parameters'}}, 'required': ['action']},
            'then': {'properties': {'parameter_search': {'required': required}}}})
        if evidence.get('tuning_scope'):
            schema['allOf'][-1]['then']['properties']['parameter_search']['required'] += ['scope_sha256', 'variant']
        if evidence.get('hyperparameter_yaml', {}).get('ready'):
            schema['allOf'][-1]['then']['properties']['parameter_search']['required'] += ['yaml_revision_id', 'yaml_sha256']
    active = evidence.get('training_investigation', {})
    if active.get('required'):
        response = copy.deepcopy(RESPONSE)
        if evidence.get('dataset_optimization'):
            response['properties']['disposition']['enum'].remove('stop')
        response['properties']['evidence_paths'] = copy.deepcopy(STRINGS)
        response['properties']['evidence_paths']['items']['pattern'] = (
            r'^/(training_investigation|diagnostics|records|comparisons)/.+'
        )
        response['properties']['evidence_paths']['description'] = (
            'Existing specific measured-evidence JSON pointers; workflow metadata such as '
            '/rapid_revision belongs only in the proposal-level evidence_paths.'
        )
        response['properties']['followup_outcome']['enum'] = response_contract(active)['allowed_followup_outcomes']
        schema['properties']['training_response'] = response
        schema['required'].append('training_response')
    return schema


def response_contract(active):
    followup = active.get('followup') or {}
    outcomes = list(RESPONSE['properties']['followup_outcome']['enum'])
    if not followup.get('new_trial_ids'):
        outcomes = ['not_applicable']
    elif followup.get('requires_reassessment'):
        outcomes.remove('not_applicable')
    return {'version': RESPONSE_CONTRACT_VERSION, 'measured_evidence_prefixes': list(MEASURED_PREFIXES),
        'specific_existing_pointer_required': True,
        'new_verification_trial_count': len(followup.get('new_trial_ids', [])),
        'allowed_followup_outcomes': outcomes}


def validate_reference_contract(proposal, evidence, pointer):
    """Report all citation/outcome mistakes together, without rewriting an answer."""
    active = evidence.get('training_investigation') or {}
    response = proposal.get('training_response')
    if not active.get('required') or not isinstance(response, dict):
        return  # Missing/ill-typed fields are handled by the JSON schema.
    issues = []
    paths = response.get('evidence_paths')
    for path in paths if isinstance(paths, list) else []:
        if not isinstance(path, str):
            continue
        if not path.startswith(MEASURED_PREFIXES):
            issues.append('Training response requires specific measured evidence paths: '
                f'{path!r} is not allowed in training_response.evidence_paths; use an existing child '
                'under /training_investigation/, /diagnostics/, /records/ or /comparisons/. '
                'Workflow metadata belongs in proposal-level evidence_paths only.')
            continue
        try:
            pointer(evidence, path)
        except (ValueError, KeyError, IndexError, TypeError):
            issues.append(f'Training response cites missing measured evidence: {path!r}')
    outcome = response.get('followup_outcome')
    if isinstance(outcome, str) and outcome not in response_contract(active)['allowed_followup_outcomes']:
        if not (active.get('followup') or {}).get('new_trial_ids'):
            issues.append('No new verification run exists; followup.new_trial_ids is empty, '
                'so training_response.followup_outcome must be not_applicable (not inconclusive).')
        else:
            issues.append('New verification results must reassess the previous training intervention; '
                'followup_outcome cannot be not_applicable.')
    if issues:
        raise ValueError('\n'.join(issues))


def guidance(base, context):
    active = context.get('experiment_decision', {}).get('training_investigation', {})
    detail = GUIDANCE
    if context.get('experiment_decision', {}).get('dataset_optimization'):
        detail = detail.replace('仍可 stop 并准确记录未解决的问题，不宣布修复或验收成功。',
                                '未解决的问题留在当前阶段核验，不提前收尾，不宣布修复或验收成功。')
    return base + '\n' + detail if active.get('required') else base


def _stats(values):
    good = [v for v in values if finite(v)]
    tail = sorted(good[-10:])
    middle = len(tail) // 2
    recent = (tail[middle] if len(tail) % 2 else tail[middle-1] / 2 + tail[middle] / 2) if tail else None
    return {'count': len(good), 'invalid_count': sum(v is not None and not finite(v) for v in values),
            'minimum': min(good) if good else None, 'maximum': max(good) if good else None,
            'recent_median': recent}


def _association(rows, field):
    pairs = [(r['loss'], r[field]) for r in rows[-200:] if finite(r.get('loss')) and finite(r.get(field))]
    correlation = None
    if len(pairs) >= 3:
        x, y = zip(*pairs)
        x_scale, y_scale = max(map(abs, x)), max(map(abs, y))
        if x_scale and y_scale:
            x, y = [v / x_scale for v in x], [v / y_scale for v in y]
            mx, my = sum(x) / len(x), sum(y) / len(y)
            x, y = [v - mx for v in x], [v - my for v in y]
            denominator = math.sqrt(sum(v*v for v in x) * sum(v*v for v in y))
            if denominator:
                correlation = max(-1., min(1., sum(a*b for a, b in zip(x, y)) / denominator))
    return {'paired_observations': len(pairs), 'pearson_loss_correlation': correlation,
            'scope': 'Same-step observations in the last 200 batches; descriptive association, not a verified cause.'}


def observe(log, metric, direction):
    """Summaries of the latest contiguous fit only; never read held-out values."""
    events = log.get('events', []) if isinstance(log, dict) else log if isinstance(log, list) else []
    rows, identity, last = [], None, None
    for row in events if isinstance(events, list) else []:
        if not isinstance(row, dict) or row.get('phase') not in (None, 'train', 'validation'):
            continue
        step = row.get('step', row.get('epoch'))
        if not finite(step) or 'loss' not in row:
            continue
        current = segment_identity(row)
        if rows and (current != identity or step <= last):
            rows = []
        identity, last = current, step
        rows.append(row)
    epochs = {}
    for row in rows:
        epoch = row.get('epoch')
        if finite(epoch) and finite(row.get('loss')):
            epochs.setdefault(epoch, []).append(row)
    metadata = log if isinstance(log, dict) else {}
    same_fit = lambda item: isinstance(item, dict) and segment_identity(item)[:3] == (identity or [])[:3] and all(
        k not in item or str(item[k])[:120] == identity[i] for i,k in ((3,'loss_scale'),(4,'reduction'))
    ) and item.get('phase') in (None, 'train', 'validation')
    reported_epochs = {r.get('epoch'): r for r in metadata.get('epochs', [])
                       if same_fit(r) and finite(r.get('epoch'))} if isinstance(metadata.get('epochs'), list) else {}
    aggregated = []
    for epoch, batch in epochs.items():
        weighted = all(finite(r.get('batch_size')) and r['batch_size'] > 0 for r in batch)
        weights = [r['batch_size'] if weighted else 1 for r in batch]
        scale = max(weights)
        weights = [w / scale for w in weights]
        denominator = sum(weights)
        value = sum(r['loss'] * (w / denominator) for r, w in zip(batch, weights))
        reported = reported_epochs.get(epoch, {})
        count = sum(r['batch_size'] for r in batch) if weighted else None
        reported_loss = reported.get('sample_weighted_loss')
        consistent = bool(reported) and weighted and finite(count) and (
            reported.get('observed_batches') == len(batch) and reported.get('sample_count') == count
            and finite(reported_loss) and finite(value) and math.isclose(reported_loss, value, rel_tol=1e-9, abs_tol=1e-12))
        expected = reported.get('expected_batches')
        complete = (consistent and type(expected) is int and expected > 0 and expected == len(batch))
        aggregated.append({'epoch': epoch, 'loss': value if finite(value) else None,
            'batches': len(batch), 'sample_weighted': weighted,
            'sample_count': count if finite(count) else None,
            'expected_batches': expected if type(expected) is int and expected >= 0 else None,
            'epoch_complete': bool(complete) if reported.get('epoch_complete') is True else
                              False if reported.get('epoch_complete') is False else None,
            'reported_summary_consistent': consistent if reported else None})
    from .training_statistics import coverage, alignment, validation_points
    scalars = {k: coverage(rows, k, _stats) for k in
        ('learning_rate', 'grad_norm', 'grad_norm_after_clip', 'zero_gradient_fraction', 'update_norm', 'parameter_norm', 'clip_fraction',
         'input_norm', 'target_norm', 'loss_scale')}
    complete_epochs = [r for r in aggregated if r['epoch_complete'] is True]
    runtime = {}
    if same_fit(metadata.get('identity')):
        allowed = {
            'identity': {'log_version', 'entrypoint', 'phase', 'stage', 'seed', 'config_sha256', 'split_sha256', 'loss_name', 'loss_scale', 'reduction', 'fit_id'},
            'loss_definition': {'operation', 'time_axis', 'n', 'fft_norm', 'frequency_bins', 'frequency_weighting', 'reduction', 'scale', 'name'},
            'monitor': {'name', 'direction', 'configured_monitor', 'configured_direction', 'source'},
            'termination': {'reason', 'loop_reason', 'step', 'epoch', 'stage', 'exception_type'}}
        for key, fields in allowed.items():
            source = metadata.get(key)
            if isinstance(source, dict):
                runtime[key] = {k: v[:240] if isinstance(v, str) else v for k, v in source.items()
                    if k in fields and
                    (isinstance(v, (str, bool)) or v is None or finite(v))}
    module_rows = [{name: values for name, values in r['modules'].items()
        if isinstance(name, str) and len(name) <= 160 and isinstance(values, dict)}
        if isinstance(r.get('modules'), dict) else {} for r in rows]
    module_names = sorted({name for modules in module_rows for name in modules})
    modules = {name: {field: coverage([r.get(name, {}) for r in module_rows], field, _stats)
        for field in ('grad_norm', 'activation_norm', 'update_norm', 'zero_gradient_fraction', 'nonfinite_count')}
        for name in module_names[:16]}
    observed_metric = metric if isinstance(metric, str) and not metric.startswith(('test_', 'held_out')) else None
    validation = monitor_values(rows, observed_metric, direction)
    trend, aligned = alignment(rows, aggregated, validation, direction)
    return {'segment_identity': identity, 'stage_identity_complete': bool(identity) and 'unspecified' not in identity[:3],
        'batch_observations': len(rows), 'batch_trend': _series(rows, observed_metric, direction),
        'epoch_count': len(aggregated), 'epoch_trend': _series(aggregated, None, None),
        'complete_epoch_count': len(complete_epochs), 'complete_epoch_trend': _series(complete_epochs, None, None),
        'runtime': runtime, 'failure_event': failure_observation(log, identity),
        'epoch_descriptive_trend': trend, 'validation_alignment': aligned,
        'validation_trajectory': {'metric': observed_metric, 'direction': direction,
            'points': validation_points(rows, validation),
            'scope': 'Recorded finite validation observations in the latest contiguous fit only; no interpolation or causal conclusion.'},
        'epoch_trajectory': aggregated,
        'tail_associations': {k: _association(rows, k) for k in ('input_norm', 'target_norm', 'grad_norm', 'update_norm')},
        'loss_scale_labels': sorted({r['loss_scale'][:120] for r in rows if isinstance(r.get('loss_scale'), str)})[:8],
        'recent_epochs': aggregated[-8:], 'epoch_aggregation': 'sample weighted when batch_size is recorded, otherwise unweighted batch mean; last epoch may be partial',
        'validation': {'metric': observed_metric, 'direction': direction, **_stats(validation)},
        'telemetry': scalars, 'missing_telemetry': [k for k, v in scalars.items() if not v['count'] and not v['categorical_count']],
        'partial_telemetry': [k for k, v in scalars.items() if not v['coverage_complete']],
        'module_telemetry': modules, 'modules_omitted': max(0, len(module_names) - 16),
        'root_cause_verified': False, 'scope': 'Observed validation-training telemetry; no causal or scientific acceptance claim.'}


def read(engine, run_id, trial, diagnostic, metric, direction):
    from .experiment_views import _safe_file
    source = diagnostic.get('source') or {}
    result = {'source': source, 'available': False}
    path = _safe_file(engine, run_id, trial, 'artifacts/training_log.json')
    try:
        if path is None:
            return result
        with path.open('rb') as stream:
            raw = stream.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != source.get('sha256'):
            return {**result, 'reason': 'Telemetry bytes do not match the frozen diagnosis.'}
        log = json.loads(raw, parse_constant=lambda _: None)
        observed = observe(log, metric, direction)
        best = (observed['validation_alignment'].get('best') or {}).get('value')
        final = trial.get('metrics', {}).get(metric) if metric and not metric.startswith(('test_', 'held_out')) else None
        observed['checkpoint_alignment'] = {'metric': metric, 'reported_final': final if finite(final) else None,
            'matches_best_validation': math.isclose(best, final, rel_tol=1e-9, abs_tol=1e-12) if finite(best) and finite(final) else None,
            'scope': 'Metric agreement only; not proof that checkpoint bytes were restored.'}
        identity = (log.get('identity') or {}) if isinstance(log, dict) else {}
        # The training-log producer hashes compact sorted JSON, unlike workflow receipts.
        from .training_convergence import sha
        observed['host_binding_checks'] = {
            'phase_and_seed_match': all(identity.get(k) == trial.get('params', {}).get(k) for k in ('phase','seed')) if identity else None,
            'config_sha256_match': identity.get('config_sha256') == sha(trial['params']) if identity.get('config_sha256') else None,
            'split_sha256_match': identity.get('split_sha256') == sha(log.get('split_fingerprints')) if identity.get('split_sha256') else None,
            'split_matches_host': log.get('split_fingerprints') == trial.get('evaluation',{}).get('split_fingerprints') if log.get('split_fingerprints') else None}
        return {**result, 'available': True, **observed}
    except (OSError, ValueError, TypeError, OverflowError):
        return {**result, 'reason': 'Telemetry absent or unreadable; no normal-training inference.'}


def collect(engine, run_id, rows, diagnostics, program, metric, direction):
    # Fixtures without logs do not manufacture a live training incident.
    observations = {r['id']: read(engine, run_id, r, diagnostics[r['id']], metric, direction) for r in rows}
    incidents = [r for r in rows if diagnostics[r['id']].get('needs_discussion') or
        observations[r['id']].get('validation_alignment', {}).get('signal') == 'training_down_validation_worse_than_best' or
        diagnostics[r['id']].get('status') == 'insufficient_evidence' and r.get('provenance') == 'real_experiment_execution']
    incidents.sort(key=lambda r: (r.get('params', {}).get('variant') != 'baseline', r['id']))
    focus = incidents[:4]
    # Read the paired controls too, when this bounded incident packet has room.
    paired = {(r.get('params', {}).get('benchmark_task_id'), r.get('params', {}).get('dataset'),
               r.get('params', {}).get('seed')) for r in focus}
    for row in rows:
        p = row.get('params', {})
        if len(focus) < 4 and row not in focus and observations[row['id']].get('available') and (
            p.get('benchmark_task_id'), p.get('dataset'), p.get('seed')) in paired:
            focus.append(row)
    state = engine.store.get('experiment_decision_sessions', f'{run_id}:{engine.cycle(run_id)}') or {}
    prior_id = state.get('last_decision')
    if program.get('rapid_revision_id'):
        revision = engine.store.get('rapid_experiment_revisions', program['rapid_revision_id']) or {}
        prior_id = prior_id or revision.get('decision_id')
    prior = engine.store.get('experiment_decisions', prior_id or '') or {}
    response = prior.get('proposal', {}).get('training_response')
    followup = None
    if response:
        from .experiment_decisions import _approved
        from .experiment_workflow import sha
        if prior.get('run_id') != run_id or prior.get('cycle') != engine.cycle(run_id) or not _approved(prior):
            raise ValueError('Training follow-up requires the original independently approved intervention')
        ref = prior.get('evidence', {})
        archived = engine.store.get('experiment_evidence', ref.get('id', '')) or {}
        if sha(archived.get('summary')) != ref.get('sha256'):
            raise ValueError('Training follow-up source evidence changed')
        previous = archived['summary']
        controls = [r for r in previous['records'] if r['id'] in response['trial_ids']]
        new = [r for r in rows if r['id'] not in {x['id'] for x in previous['records']}]
        followup = {'decision_id': prior_id, 'proposal_sha256': prior.get('proposal_sha256'),
            'response': response, 'new_trial_ids': [r['id'] for r in new],
            'previous_observations': {r['id']: {'params': r['params'], 'metrics': r.get('metrics'),
                'status': r['status'], 'diagnosis': previous['diagnostics'].get(r['id'])} for r in controls},
            'source': ref, 'requires_reassessment': bool(new)}
    from .controlled_training_checks import current as controlled_current
    controlled = controlled_current(engine, run_id)
    causal = engine.store.get('causal_training_followups', f'{run_id}:{engine.cycle(run_id)}')
    from .fixed_state_execution import evidence as fixed_state_evidence
    fixed_state = fixed_state_evidence(engine,run_id)
    from .bounded_diagnostics import evidence as bounded_evidence
    bounded = bounded_evidence(engine, run_id)
    return {'version': VERSION, 'required': bool(focus or followup and followup['requires_reassessment']),
        'focus_trial_ids': [r['id'] for r in focus], 'incident_count': len(incidents),
        # Focus bounds mandatory reading, not the immutable evidence archive.
        # All validation observations were already read and SHA-checked above;
        # discarding non-focus controls makes on-demand reports falsely absent.
        # compact() still projects only the incident index into model context.
        'observations': observations,
        'followup': followup, 'scientific_approval': False,
        **({'bounded_diagnostics': bounded} if bounded else {}),
        **({'controlled_checks': {'id':controlled['id'],'status':controlled['status'],
             'outcome':controlled.get('outcome'),'analyses':controlled.get('analyses'),
             'scope':'Already executed diagnostic batch. Reuse results; do not repeat logging or this batch.'}}
           if controlled and controlled.get('status')=='completed' else {}),
        **({'causal_followup': {'id':causal['id'],'status':'completed','plan':causal['plan'],
            'outcome':causal['outcome'],'analyses':causal['analyses'],
            'scope':'Completed single-seed contrast and three-role result review; reuse it. Do not repeat logging or this batch.'}}
           if causal and causal.get('status')=='completed' else {}),
        **({'fixed_state_diagnostic':fixed_state} if fixed_state else {})}


def compact(value):
    followup = value.get('followup') or {}
    result = {k: value[k] for k in ('version', 'required', 'focus_trial_ids', 'incident_count')} | {
        'followup_decision_id': followup.get('decision_id'),
        'requires_reassessment': followup.get('requires_reassessment', False),
        'response_contract': response_contract(value),
        'details_path': '/training_investigation'}
    from .saved_runtime_audit import KEY as runtime_audit_key, overview as runtime_audit_overview
    if value.get(runtime_audit_key):
        result[runtime_audit_key] = runtime_audit_overview(value[runtime_audit_key])
    if value.get('incident_review_contract'):
        result.update(incident_review_contract=value['incident_review_contract'],
                      open_incident_trial_ids=value['open_incident_trial_ids'],
                      incident_dispositions=value['incident_dispositions'])
    if value.get('bounded_diagnostics'):
        result['bounded_diagnostics'] = [{
            'id': row['id'], 'status': row['status'], 'report_sha256': (row.get('outcome') or {}).get('report_sha256'),
            'details_path': '/training_investigation/bounded_diagnostics/' + str(index)}
            for index, row in enumerate(value['bounded_diagnostics'])]
    controlled=value.get('controlled_checks') or {}
    if controlled.get('status')=='completed':
        outcome=controlled.get('outcome') or {}
        result['completed_controlled_check']={'id':controlled['id'],'trial_id':outcome.get('trial_id'),
            'report_sha256':outcome.get('report_sha256'),
            'verdicts':(outcome.get('summary',{}).get('classification') or {}).get('verdicts'),
            'summary_path':'/training_investigation/controlled_checks/outcome/summary',
            'instruction':'This batch and its three-role result review are complete. Read and reuse the result; do not repeat logging or this diagnostic batch.'}
    causal=value.get('causal_followup') or {}
    if causal.get('status')=='completed':
        outcome=causal['outcome']
        result['completed_causal_followup']={'id':causal['id'],'trial_id':outcome['trial_id'],
            'report_sha256':outcome['report_sha256'],'seed':causal['plan']['seed'],
            'verdicts':outcome['summary']['classification']['verdicts'],
            'summary_path':'/training_investigation/causal_followup/outcome/summary',
            'instruction':'Read this newer single-seed contrast and its independent findings before deciding. No repeated instrumentation or completed-batch replay. Inconclusive remains inconclusive.'}
    fixed_state=value.get('fixed_state_diagnostic') or {}
    if fixed_state.get('status')=='completed':
        result['completed_fixed_state_diagnostic']={'id':fixed_state['id'],
            'report_sha256':fixed_state['outcome']['report_sha256'],
            'summary_path':'/training_investigation/fixed_state_diagnostic/outcome/summary',
            'protocol_path':'/training_investigation/fixed_state_diagnostic/protocol',
            'raw_measurements_path':'/training_investigation/fixed_state_diagnostic/outcome/report',
            'instruction':'Read the new fixed-state evidence and three independent findings before the next proposal. It adds local direction measurements, not historical roots or stability approval. Never rerun the completed probes.'}
    return result


def validate_response(proposal, evidence, pointer, *, parameter_controls=None):
    active = evidence.get('training_investigation') or {}
    if not active.get('required'):
        return
    from .prose_schema import validate
    response = proposal.get('training_response')
    response_schema = copy.deepcopy(RESPONSE)
    if proposal.get('architecture_review'):
        response_schema['properties']['disposition']['enum'].append('architecture_experiment')
    validate(response, response_schema)
    validate_reference_contract(proposal, evidence, pointer)
    known = {r['id']: r for r in evidence['records']}
    required = set(active['focus_trial_ids'])
    if not required:
        required = set((active.get('followup') or {}).get('new_trial_ids', []))
    if not required.issubset(response['trial_ids']) or not set(response['trial_ids']).issubset(known):
        raise ValueError('Training response must cover the current focus, including baseline incidents or new verification trials. '
            f'Missing required IDs: {sorted(required - set(response["trial_ids"]))}; '
            f'IDs outside current /records (remove from trial_ids): {sorted(set(response["trial_ids"]) - set(known))}. '
            f'Current focus: {sorted(required)}. Independent diagnostic IDs belong in evidence_paths, not trial_ids.')
    files = evidence['code_sources']
    if not files or any(not isinstance(p, str) or not p for p in response['module_paths']):
        raise ValueError('Training response requires affected module paths')
    action, disposition, unit_id = proposal['action'], response['disposition'], proposal['unit_id']
    followup = active.get('followup') or {}
    if followup.get('requires_reassessment') and response['followup_outcome'] == 'not_applicable':
        raise ValueError('New verification results must reassess the previous training intervention')
    if not followup.get('new_trial_ids') and response['followup_outcome'] != 'not_applicable':
        raise ValueError('No new verification run exists; do not declare an intervention outcome')
    if disposition == 'scope_planning':
        if action != 'revise_tuning_scope' or unit_id is not None or proposal['changes']:
            raise ValueError('Scope planning does not authorize an experiment or model change')
    elif disposition == 'architecture_assessment':
        from .experiment_evidence_handoff import requested
        if (action != 'accept_architecture' and not requested(proposal)) or proposal['changes'] or unit_id is not None:
            raise ValueError('Architecture assessment cannot authorize training or declare training stability')
    elif disposition == 'architecture_experiment':
        from .architecture_experiment_contract import validate as validate_architecture
        validate_architecture(proposal, evidence, pointer)
    elif disposition in {'instrument', 'repair_code'}:
        if action not in {'diagnose', 'revise_architecture'} or unit_id is not None or not proposal['changes']:
            raise ValueError('Instrumentation/code repair must dispatch concrete scoped code changes')
    elif disposition in {'adjust_parameters', 'controlled_diagnostic'}:
        needed = {'learning_rate': ['learning_rate', 'grad_norm'], 'gradient': ['grad_norm'],
            'loss_scale': ['loss_scale', 'grad_norm'], 'data_pipeline': ['input_norm'],
            'batch_noise': [], 'implementation': [], 'telemetry': [],
            'regularization': [], 'capacity': [], 'optimizer_dynamics': ['grad_norm']}
        if response['cause'] == 'unknown':
            raise ValueError('Unknown training cause needs targeted instrumentation before choosing an intervention')
        for trial in active['focus_trial_ids']:
            observed = active.get('observations', {}).get(trial, {})
            if not observed.get('stage_identity_complete') or not observed.get('validation', {}).get('count'):
                raise ValueError('Training stage/validation evidence is missing; instrument it before parameter trials or ablations')
            if any(not observed.get('telemetry', {}).get(k, {}).get('count') for k in needed[response['cause']]):
                raise ValueError('Cause-specific runtime telemetry is missing; instrument the affected module first')
            if active.get('version', 1) >= 2 and any(not observed.get('telemetry', {}).get(k, {}).get('coverage_complete')
                                                   for k in needed[response['cause']]):
                raise ValueError('Cause-specific telemetry coverage is incomplete; read and resolve missing/nonfinite observations before parameter intervention')
        unit = next((u for u in evidence['units'] if u['id'] == unit_id and u['available']), None)
        if not unit or action not in {'diagnose', 'grid_search'} or not response['factor_paths']:
            raise ValueError('Training intervention requires one controlled batch and explicit changed factor paths')
        params = [evidence['job_parameters'][n] for n in unit['jobs']]
        controls = (parameter_controls if parameter_controls is not None else
                    [known[k] for k in response['trial_ids'] if known[k]['status'] == 'completed'])
        from .experiment_analysis import _param_diff
        for candidate in params:
            selected_variant = evidence.get('parameter_intervention_variant', 'shared')
            if selected_variant != 'shared' and candidate['variant'] != selected_variant:
                if not any(r['params'] == candidate and r['status'] == 'completed' for r in controls):
                    raise ValueError('Unchanged comparison method must reuse its exact completed control')
                continue
            matches = [r for r in controls if r['params'].get('seed') == candidate.get('seed') and
                r['params'].get('benchmark_task_id') == candidate.get('benchmark_task_id') and
                r['params'].get('dataset') == candidate.get('dataset') and
                (r['params'].get('variant') == candidate.get('variant') or
                 candidate.get('variant') == 'ablation' and r['params'].get('variant') == 'proposed')]
            if not any({d['path'] for d in _param_diff(r['params'], candidate)} == set(response['factor_paths']) for r in matches):
                raise ValueError('Every diagnostic job needs a same-seed control differing only in its declared factors; generic ablation is insufficient')
        if disposition == 'adjust_parameters' and unit['kind'] not in {'pilot', 'grid'}:
            raise ValueError('Parameter adjustment requires paired baseline/candidate training')
    elif disposition == 'coverage_validation':
        from .experiment_progression import validate_coverage
        validate_coverage(proposal, evidence)
    elif disposition == 'continue':
        observations = active.get('observations', {})
        if response['cause_status'] != 'supported' or response['cause'] not in {'batch_noise', 'telemetry'}:
            raise ValueError('Continuing past training alerts requires supported noise/measurement evidence')
        if any(not observations.get(t, {}).get('available') or
               observations[t].get('epoch_count', 0) < 3 or
               observations[t].get('validation', {}).get('count', 0) < 2 for t in active['focus_trial_ids']):
            raise ValueError('Training noise dismissal lacks epoch and validation observations; collect evidence first')
        if action in {'diagnose', 'revise_architecture'} and unit_id is None:
            raise ValueError('Continue cannot conceal an implementation change')
        if response['followup_outcome'] == 'regressed':
            raise ValueError('A regressed intervention requires rollback/repair or further diagnosis, not continue')
    elif disposition == 'resolved':
        new = [known[t] for t in followup.get('new_trial_ids', []) if t in known]
        if active.get('open_incident_trial_ids', active['focus_trial_ids']) or response['followup_outcome'] != 'supported' or not new or \
                any(r['status'] != 'completed' for r in new) or \
                not {'baseline', 'proposed'}.issubset({r['params'].get('variant') for r in new}):
            raise ValueError('Resolution requires new completed paired verification with no unresolved training alerts')
        if proposal['changes']:
            raise ValueError('Resolution cannot authorize an unverified implementation change')
    elif disposition == 'stop' and action != 'stop':
        raise ValueError('Unresolved training stop cannot authorize another experiment')


def validate_reads(reads, proposal, evidence):
    active = evidence.get('training_investigation') or {}
    if not active.get('required'):
        return
    required = set(active['focus_trial_ids'] or (active.get('followup') or {}).get('new_trial_ids', []))
    modules = set(proposal['training_response']['module_paths'])
    if not all(required.issubset(r.get('training_trials_read', [])) and r.get('source_paths') for r in reads) or \
            not modules.issubset({p for r in reads for p in r.get('source_paths', [])}):
        raise ValueError('All three agents must read the affected training evidence and relevant module source before this intervention')


async def analysis_reads(engine, harness, run_id, analyses, context, instruction, checkpoint, record):
    """One same-role tool-capable repair, without replacing the original answer."""
    from .experiment_decisions import analysis_schema, analysis_receipts
    from .engine import ResearchBlocked
    scope = context.get('experiment_decision', {}).get('training_investigation') or {}
    required = set(scope.get('focus_trial_ids', []))
    if scope.get('requires_reassessment') and not required:
        ref = context['validation_evidence']
        frozen = engine.store.get('experiment_evidence', ref['id'])['summary']
        required = set(frozen['training_investigation']['followup']['new_trial_ids'])
    assignments = copy.deepcopy(record.get('training_read_repairs', {}))
    results, reads = [], []
    for analysis in analyses:
        role = analysis['role']
        repair = assignments.get(role)
        try:
            receipts = analysis_receipts(engine, run_id, analysis, context, instruction=instruction)
            if scope.get('required') and (not required.issubset(receipts.get('training_trials_read', []))
                                         or not receipts.get('source_paths')):
                raise ResearchBlocked('Read every focus trial with experiments.analyze(kind=training) and the relevant module source')
            if not repair:
                results.append(analysis)
                reads.append(receipts)
                continue
        except ResearchBlocked as exc:
            if not scope.get('required'):
                raise
            if not repair:
                repair_context = copy.deepcopy(context)
                repair_context['experiment_decision']['training_read_repair'] = {
                    'original_analysis': analysis, 'missing_requirement': str(exc),
                    'required_training_trials': sorted(required), 'source_read_required': True}
                from .saved_runtime_audit import MissingRuntimeAuditReads
                if isinstance(exc.__cause__, MissingRuntimeAuditReads):
                    repair_context['experiment_decision']['training_read_repair']['missing_runtime_audit_pages'] = copy.deepcopy(exc.__cause__.pages)
                from .training_read_continuation import origin
                retained = origin(engine, run_id, role, context, instruction, analysis_schema(context))
                if retained:
                    repair_context['experiment_decision']['training_read_repair']['retained_reads'] = retained
                repair = {'purpose': 'experiment_diagnose:' + context['evidence_sha256'] + ':training-read-repair',
                    'instruction': instruction + '\n补齐本角色缺少的真实工具读取后重新提交分析，不重做方案讨论、不编造回执。'
                        '\nretained_reads 若存在，是本角色在同一冻结证据上实际已读的回执索引，可在本次答案中引用；'
                        '漏列引用时补上原 receipt_sha256 即可，不必重读。只补未读页或缺失源码，未读内容不能算已读。'
                        'missing_runtime_audit_pages若存在，逐项按path、offset、limit补齐全部缺页；不能在next_offset非空时停止。',
                    'context': repair_context}
                assignments[role] = repair
                await checkpoint(training_read_repairs=assignments)
        answer = await harness.ask(run_id, role, repair['purpose'], repair['instruction'], repair['context'],
                                   analysis_schema(repair['context']))
        updated = {'role': role, **answer}
        if updated['evidence_sha256'] != context['evidence_sha256']:
            raise ResearchBlocked('Training read repair changed the frozen evidence')
        try:
            receipt = analysis_receipts(engine, run_id, updated, repair['context'],
                purpose=repair['purpose'], instruction=repair['instruction'])
        except ResearchBlocked as exc:
            from .saved_runtime_audit import MissingRuntimeAuditReads
            if not isinstance(exc.__cause__, MissingRuntimeAuditReads):
                raise
            from .runtime_audit_read_recovery import recover as recover_audit_pages
            recovered = await recover_audit_pages(engine, harness, run_id, role, repair, answer, record, checkpoint)
            if recovered is None:
                raise
            updated, receipt = recovered
        if not required.issubset(receipt.get('training_trials_read', [])) or not receipt.get('source_paths'):
            from .training_citation_recovery import recover as recover_citation
            recovered = await recover_citation(engine, harness, run_id, role, repair, answer,
                                                required, record, checkpoint)
            if recovered is None:
                missing = sorted(required - set(receipt.get('training_trials_read', [])))
                raise ResearchBlocked('Training read repair still lacks actual source or affected-trial reads: '
                    f'missing_training_trials={missing}; source_read={bool(receipt.get("source_paths"))}')
            updated, receipt = recovered
        results.append(updated)
        reads.append(receipt)
    return results, reads
