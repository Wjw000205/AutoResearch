"""One bounded three-role discussion for each immutable validation-loss incident."""
from __future__ import annotations

import asyncio
import copy
import json

from .prose_schema import validate

from .storage import now
from .training_context import KEY
from .training_convergence import read_evidence, sha
from .architecture_validation import operation

COLLECTION = 'training_adjustments'
ROLES = ('director', 'engineer', 'scout')
PROTECTED_DESIGN = ('dataset', 'evaluation', 'primary_metric', 'direction', 'acceptance', 'iteration_policy',
                    'resource_plan', 'validation_seeds', 'final_test_seeds')
TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 1200}
TEXTS = {'type': 'array', 'items': TEXT, 'maxItems': 5}


def obj(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


ANALYSIS = obj({'evidence_sha256': TEXT, 'assessment': TEXT,
    'hypotheses': {'type': 'array', 'minItems': 1, 'maxItems': 3, 'items': obj({
        'cause': TEXT, 'evidence': TEXT, 'verification': TEXT})},
    'suggested_action': {'enum': ['continue', 'adjust', 'collect_evidence', 'stop']},
    'limitations': TEXTS})
PROPOSAL = obj({'evidence_sha256': TEXT, 'action': {'enum': ['continue', 'adjust', 'collect_evidence', 'stop']},
    'rationale': TEXT, 'changes': {'type': 'array', 'maxItems': 3, 'items': obj({
        'target': TEXT, 'before': TEXT, 'after': TEXT, 'reason': TEXT})},
    'validation': {'type': 'array', 'items': TEXT, 'minItems': 1, 'maxItems': 4},
    'rollback_condition': TEXT, 'preserve_data_splits': {'const': True},
    'preserve_acceptance_criteria': {'const': True}, 'requires_new_execution_approval': {'const': True}})
VOTE = obj({'evidence_sha256': TEXT, 'proposal_sha256': TEXT, 'approve': {'type': 'boolean'},
            'rationale': TEXT, 'issues': TEXTS})

GUIDANCE = '''你正在进行实验阶段的快速 loss 诊断，仅使用当前冻结的训练/验证证据。
自动趋势分类只是启发式告警，不是收敛证明或已验证根因。明确区分观测、原因假设和待验证项。
director 负责目标与最小调整范围；engineer 负责梯度、优化器、学习率、数值尺度和架构路径；
scout 负责反证、批次噪声、阶段切换、过拟合、基线公平性与数据泄漏。
只讨论受影响的训练阶段，不重写整个架构，不扩大实验搜索，不修改数据划分、验收阈值和资源预算。
不得根据测试集反馈调参。缺少梯度/代码证据时承认未知，不声称已检查未提供的实现或已修复。
本轮仅有三方诊断、一个最小方案和三方复核，不循环自由辩论。返回严格 JSON。'''

TELEMETRY_GUIDANCE = ('For iterative validation training, atomically save bounded observations to '
    'artifacts/training_log.json as {events:[{step,epoch,stage,fit_id,loss_name,loss,learning_rate,'
    'metrics?:{configured_validation_metric:finite_value}}], '
    'monitor:{metric:configured_validation_metric,direction:configured_direction}}, including a final flush on failure. '
    'Record the actual epoch independently from batch update counters. Use real finite training losses, '
    'distinct stage/fit/loss identities and only already permitted validation monitoring; do not add fits '
    'or read test data for diagnostics. For a non-iterative solver save {applicable:false,reason:"..."}. '
    'Telemetry never changes the approved algorithm, checkpoints, stopping rule or resource limits.')

REVISION_GUIDANCE = ('Apply only the unanimously reviewed training_adjustment.proposal to the current '
    'proposal. Preserve the hypothesis, dataset/splits, baseline fairness, acceptance thresholds and all '
    'unaffected architecture. State the exact changed module/parameter and its before/after definition, '
    'validation pass conditions and rollback condition; do not merely reword the original. These are '
    'unverified changes, not measured improvements. Carry them to the next design/SPEC/code cycle for '
    'the existing independent approvals, resource and budget gates. Never use held-out scores or broaden '
    'the search. The saved loss analyses are hypotheses; do not claim they establish a root cause.')


@operation
def _basis(engine, run_id):
    """Read only the design/program/plan; tuning outputs may contain huge arrays."""
    from .architecture_revisions import effective, plan_key, validate_plan
    step = engine.store.get('steps', run_id + ':design')
    if not step or step.get('status') != 'completed':
        # Legacy adapters and isolated fixtures can supply their own stage view.
        outputs = engine.outputs(run_id)
        return {'design': outputs['design'],
            'program': engine.store.get('programs', run_id) or outputs['implementation'],
            'plan': engine.experiment_plan(run_id)}
    design = effective(engine, run_id, step['output'])
    program = engine.store.get('programs', run_id)
    if not program:
        program = (engine.store.get('steps', run_id + ':implementation') or {}).get('output')
    key = plan_key(engine, run_id)
    plan = engine.store.get('experiment_plans', key)
    if plan and key != run_id:
        validate_plan(engine, run_id, plan, design)
    return {'design': design, 'program': program, 'plan': plan}


def _binding(engine, run_id, trial, *, basis=None, saved_trials=None):
    from .engine import digest
    from .python_training import code_hash
    from .experiment_views import trials
    from .engine import ResearchBlocked
    saved = next((r for r in (saved_trials if saved_trials is not None else trials(engine.store, run_id)[0])
                  if r['id'] == trial['id']), None)
    if (not saved or saved.get('superseded') or saved.get('research_cycle', 1) not in (None, engine.cycle(run_id))
            or any(saved.get(k) != trial.get(k) for k in ('status', 'attempt', 'code_revision'))):
        raise ResearchBlocked('loss 诊断没有当前实验的持久化执行记录。')
    trial = saved
    basis = basis if basis is not None else _basis(engine, run_id)
    program = basis['program'] or {}
    if program.get('revision', 0) != trial.get('code_revision', 0):
        raise ResearchBlocked('loss 诊断的实验代码版本已被替换。')
    if trial.get('experiment_plan_sha256') and trial['experiment_plan_sha256'] != digest(basis['plan']):
        raise ResearchBlocked('loss 诊断的已执行实验计划与当前计划不一致。')
    from .experiment_views import _json_file
    environment, _ = _json_file(engine, run_id, trial, 'environment.json', 1024 * 1024)
    actual_code = environment.get('code_sha256') if isinstance(environment, dict) else None
    if (actual_code and actual_code != code_hash(program)
            or trial.get('provenance') == 'real_experiment_execution' and not actual_code):
        raise ResearchBlocked('loss 诊断缺少与当前代码匹配的原执行环境记录。')
    params = {k: v for k, v in trial.get('params', {}).items() if k != 'comparison_inputs'}
    evaluation = trial.get('evaluation') or {}
    return {'run_id': run_id, 'cycle': engine.cycle(run_id), 'trial_id': trial['id'],
        'attempt': trial.get('attempt'), 'status': trial['status'], 'params': params,
        'code_revision': trial.get('code_revision'), 'code_sha256': code_hash(program),
        'delivery_hash': program.get('delivery_hash'), 'bundle_sha256': trial.get('bundle_sha256'),
        'attempt_fingerprint': trial.get('fingerprint'), 'artifact_path': trial.get('artifact_path'),
        'dataset_id': evaluation.get('dataset_id'),
        'split_fingerprints': {k: v for k, v in (evaluation.get('split_fingerprints') or {}).items() if k in {'train', 'validation'}},
        'design_sha256': digest(basis['design']), 'plan_sha256': digest(basis['plan']),
        'trial_plan_sha256': trial.get('experiment_plan_sha256'),
        'provenance': trial.get('provenance', 'unverified')}


async def _group(harness, run_id, purpose, instruction, context, schema):
    tasks = [asyncio.create_task(harness.ask(run_id, role, purpose, instruction, context, schema)) for role in ROLES]
    try:
        results = await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    for result in results:
        validate(result, schema)
    return [{'role': role, **result} for role, result in zip(ROLES, results)]


def _route(record, plan):
    from .engine import ResearchBlocked, ResearchRevisionRequired
    if record.get('review_only'):
        return record
    if record['status'] == 'observed' or record['status'] == 'continue':
        return record
    if record['status'] == 'accepted_for_revision':
        if (plan or {}).get('iteration_policy', {}).get('on_validation_failure') == 'stop':
            raise ResearchBlocked('loss 微调方案已保存；冻结的验证停止规则禁止自动改架构重跑。')
        raise ResearchRevisionRequired('训练 loss 告警经三 agent 讨论，需要最小架构/训练调整后重新验证。',
            kind='training_nonconvergence', details={'adjustment_id': record['id'],
                'evidence_sha256': record['evidence_sha256'], 'proposal_sha256': record['proposal_sha256']})
    raise ResearchBlocked('loss 快速讨论尚未批准可执行的调整；查看三方原因分析、异议和补证据要求。')


def _scope(design):
    result = {}
    for name in ('architecture', 'dataset', 'evaluation', 'resource_plan', 'baseline_params', 'proposed_params'):
        if name not in design:
            continue
        value = design[name]
        if len(json.dumps(value, ensure_ascii=False)) <= 8000:
            result[name] = copy.deepcopy(value)
        elif isinstance(value, str):
            result[name] = {'excerpt': value[:6000], 'truncated': True,
                            'limitation': 'Excerpt only; omitted definitions are not verified evidence.'}
        else:
            result[name] = {'omitted': True, 'limitation': 'Definition exceeds the quick discussion packet; request evidence if needed.'}
    return result


async def after_trial(engine, harness, run_id, trial, *, review_context=None):
    """Runs between validation jobs. Never mutates an approved plan/program in place."""
    if (trial.get('params', {}).get('phase') != 'validation' or trial.get('superseded')
            or trial.get('status') not in {'completed', 'failed'}):
        return None
    engine.check_active(run_id)
    diagnosis = await engine._read_consensus_check(run_id, read_evidence, engine, run_id, trial)
    basis_reader = _basis
    if review_context is not None:
        from .training_review_recovery import saved_basis, verify_current
        await engine._read_consensus_check(run_id, verify_current, engine, run_id, review_context)
        basis_reader = saved_basis
    basis = await engine._read_consensus_check(run_id, basis_reader, engine, run_id)
    binding = await engine._read_consensus_check(run_id, _binding, engine, run_id, trial, basis=basis)
    if review_context is not None:
        binding = {**binding, 'review_context': copy.deepcopy(review_context)}
    evidence_hash = sha({'binding': binding, 'diagnosis': diagnosis})
    identifier = f'{run_id}:loss:{evidence_hash}'
    record = engine.store.get(COLLECTION, identifier)
    plan = basis['plan']
    if record and record['status'] not in {'diagnosing', 'proposing', 'reviewing'}:
        return _route(record, plan)
    if not record:
        record = {'id': identifier, 'run_id': run_id, 'trial_id': trial['id'], 'cycle': engine.cycle(run_id),
            'binding': binding, 'diagnosis': diagnosis, 'evidence_sha256': evidence_hash,
            'status': 'diagnosing' if diagnosis['needs_discussion'] else 'observed',
            'analyses': [], 'votes': [], 'created_at': now(), 'updated_at': now(),
            'provenance': engine.store.get('runs', run_id)['mode'],
            'execution_authorized': False, 'scientific_approval': False}
        if review_context is not None:
            record.update(review_only=True, review_context=copy.deepcopy(review_context))
        record['protected_design'] = {k: copy.deepcopy(basis['design'].get(k)) for k in PROTECTED_DESIGN}
        revision = engine.store.get('revision_plans', f'{run_id}:{record["cycle"]}') or {}
        previous = engine.store.get(COLLECTION, revision.get('training_adjustment_id', '')) if revision.get('training_adjustment_id') else None
        if previous and all(previous['binding']['params'].get(k) == trial['params'].get(k)
                            for k in ('variant', 'method', 'algorithm', 'seed')):
            record['followup'] = {'adjustment_id': previous['id'], 'previous_trial_id': previous['trial_id'],
                'previous_status': previous['diagnosis']['status'], 'current_status': diagnosis['status'],
                'validation': previous['proposal']['validation'],
                'rollback_condition': previous['proposal']['rollback_condition'],
                'status': 'observed_not_accepted'}
        engine.store.put(COLLECTION, identifier, record)
        engine.store.event(run_id, 'training.loss_diagnosed', diagnosis['label'],
            {'adjustment_id': identifier, 'trial_id': trial['id'], 'status': diagnosis['status']})
    if not diagnosis['needs_discussion']:
        return record
    # Demo has no live loss evidence/model verdict. Never manufacture consensus.
    if engine.store.get('runs', run_id)['mode'] != 'live':
        record.update(status='observed', discussion_skipped='非 live 模式，未派发真实三方讨论。')
        engine.store.put(COLLECTION, identifier, record)
        return record
    design = basis['design']
    from .rapid_continuation import trial_limit
    context = {KEY: 1, 'research_cycle': record['cycle'], 'evidence_sha256': evidence_hash,
        'loss_evidence': {'binding': binding, 'diagnosis': diagnosis},
        'training_scope': _scope(design),
        'iteration_policy': (plan or {}).get('iteration_policy'),
        'trial_budget': trial_limit(engine.store, run_id)}
    guidance = GUIDANCE
    if review_context is not None:
        context['loss_review_context'] = copy.deepcopy(review_context)
        guidance += ('\n本轮是停止训练后的只读证据复核。原负验证结果与停止规则保持有效；'
            '不得授权继续原训练、重跑已完成实验或进入独立测试。所有调整仅为后续另行预注册、'
            '独立审核的建议。明确区分科学目标未达标与训练 loss 趋势异常；不要把震荡当作已证实实现错误。')

    async def checkpoint(**changes):
        # A pause or changed release cannot acquire a review or revision on return.
        current_basis = await engine._read_consensus_check(run_id, basis_reader, engine, run_id)
        current = await engine._read_consensus_check(run_id, _binding, engine, run_id, trial, basis=current_basis)
        if review_context is not None:
            current = {**current, 'review_context': copy.deepcopy(review_context)}
            from .training_review_recovery import verify_current
            await engine._read_consensus_check(run_id, verify_current, engine, run_id, review_context)
        latest = await engine._read_consensus_check(run_id, read_evidence, engine, run_id, trial)
        from .engine import ResearchBlocked
        if current != binding or latest != diagnosis:
            raise ResearchBlocked('loss 讨论期间代码、计划或训练日志变化；原讨论保留，需按新证据重新判断。')
        record.update(**changes, updated_at=now())
        engine.store.put(COLLECTION, identifier, record)

    if not record['analyses']:
        analyses = await _group(harness, run_id, f'loss_diagnose:{evidence_hash}', guidance +
            '\n分别给出最多三个原因假设、对应观测和最小验证方法。保持 evidence_sha256 不变。', context, ANALYSIS)
        if any(row['evidence_sha256'] != evidence_hash for row in analyses):
            from .engine import ResearchBlocked
            raise ResearchBlocked('三方 loss 原因分析没有绑定同一份证据。')
        await checkpoint(analyses=analyses, status='proposing')
    context['loss_analyses'] = record['analyses']
    if not record.get('proposal'):
        proposal = await harness.ask(run_id, 'engineer', f'loss_propose:{evidence_hash}', guidance +
            '\n综合三方意见给出一个最多三处修改的最小方案；目标需指出模块/参数路径、修改前后定义。'
            '每项验证给出明确通过条件，rollback_condition 给出回退条件。只提出建议，尚未改代码或重新执行。'
            '若无法确定可行修复，选 collect_evidence 或 stop；若告警可解释且无需调整，选 continue 并给出依据。',
            context, PROPOSAL)
        validate(proposal, PROPOSAL)
        from .engine import ResearchBlocked
        if (proposal['evidence_sha256'] != evidence_hash
                or (proposal['action'] == 'adjust') != bool(proposal['changes'])
                or any(row['before'] == row['after'] for row in proposal['changes'])):
            raise ResearchBlocked('loss 调整方案缺少匹配证据或实质性的最小修改。')
        await checkpoint(proposal=proposal, proposal_sha256=sha(proposal), status='reviewing')
    context.update(loss_proposal=record['proposal'], proposal_sha256=record['proposal_sha256'])
    if not record['votes']:
        votes = await _group(harness, run_id, f'loss_vote:{record["proposal_sha256"]}', guidance +
            '\n复核同一最小方案是否合理且可验证；检查是否解决本人的原因假设与异议。'
            'approve=true 时 issues 必须为空；不能为结束流程而同意。保持两个 SHA 不变。', context, VOTE)
        unanimous = all(v['approve'] is True and not v['issues'] and v['evidence_sha256'] == evidence_hash
                        and v['proposal_sha256'] == record['proposal_sha256'] for v in votes)
        action = record['proposal']['action']
        status = ('accepted_for_revision' if action == 'adjust' else 'continue' if action == 'continue'
                  else 'needs_evidence' if action == 'collect_evidence' else 'stopped') if unanimous else 'disputed'
        if review_context is not None and unanimous and action in {'adjust', 'continue'}:
            status = 'reviewed_advisory'
        await checkpoint(votes=votes, unanimous=unanimous, status=status)
        engine.store.event(run_id, 'training.adjustment_reviewed', 'loss 快速讨论：' + status,
            {'adjustment_id': identifier, 'trial_id': trial['id'], 'unanimous': unanimous, 'action': action})
    return _route(record, plan)


def revision_context(engine, run_id, review):
    """Revalidate saved consensus before handing a scoped change to the normal gates."""
    from .engine import ResearchBlocked
    failure = review.get('failure', {})
    if failure.get('kind') != 'training_nonconvergence':
        return None
    details = failure.get('details', {})
    record = engine.store.get(COLLECTION, details.get('adjustment_id', '')) or {}
    proposal = record.get('proposal') or {}
    votes = record.get('votes', [])
    if (record.get('run_id') != run_id or record.get('cycle') != engine.cycle(run_id)
            or record.get('review_only')
            or record.get('status') != 'accepted_for_revision' or proposal.get('action') != 'adjust'
            or record.get('evidence_sha256') != details.get('evidence_sha256')
            or sha(proposal) != details.get('proposal_sha256') or len(votes) != 3
            or {v.get('role') for v in votes} != set(ROLES)
            or any(v.get('approve') is not True or v.get('issues') != []
                or v.get('proposal_sha256') != details.get('proposal_sha256')
                or v.get('evidence_sha256') != details.get('evidence_sha256') for v in votes)):
        raise ResearchBlocked('loss 微调缺少同一证据/方案 SHA 的三方复核。')
    plan = engine.experiment_plan(run_id)
    if (plan or {}).get('iteration_policy', {}).get('on_validation_failure') == 'stop':
        raise ResearchBlocked('冻结停止规则不允许自动架构微调。')
    from .experiment_views import trials
    trial = next((r for r in trials(engine.store, run_id)[0] if r['id'] == record['trial_id']), None)
    if (trial is None or _binding(engine, run_id, trial) != record['binding']
            or read_evidence(engine, run_id, trial) != record['diagnosis']
            or sha({'binding': record['binding'], 'diagnosis': record['diagnosis']}) != record['evidence_sha256']):
        raise ResearchBlocked('loss 微调的原始代码、计划或执行证据已经变化。')
    return copy.deepcopy(record)


def records(store, run_id, *, trial_id=None, cycle=None):
    filters, args = ["collection=?", "json_extract(data,'$.run_id')=?"], [COLLECTION, run_id]
    if trial_id is not None:
        filters.append("json_extract(data,'$.trial_id')=?")
        args.append(trial_id)
    if cycle is not None:
        filters.append("json_extract(data,'$.cycle')=?")
        args.append(cycle)
    with store._read_db() as db:
        rows = db.execute('SELECT data FROM records WHERE ' + ' AND '.join(filters)
                          + ' ORDER BY updated_at DESC,key DESC LIMIT 64', args).fetchall()
    return [json.loads(row[0]) for row in rows]


def cycle_diagnostics(store, run_id, cycle):
    return [{k: row.get(k) for k in ('id', 'trial_id', 'diagnosis', 'status', 'proposal', 'votes',
                                   'evidence_sha256', 'proposal_sha256')}
            for row in records(store, run_id, cycle=cycle)]


def design_context(engine, run_id):
    revision = engine.store.get('revision_plans', f'{run_id}:{engine.cycle(run_id)}') or {}
    identifier = revision.get('training_adjustment_id')
    if not identifier:
        return {}
    record = engine.store.get(COLLECTION, identifier) or {}
    from .engine import ResearchBlocked
    if (record.get('run_id') != run_id or record.get('cycle') != revision.get('previous_cycle')
            or record.get('status') != 'accepted_for_revision'
            or sha(record.get('proposal')) != record.get('proposal_sha256')):
        raise ResearchBlocked('新架构未绑定原三方 loss 微调方案。')
    return {'training_adjustment': {'id': identifier, 'proposal': record['proposal'],
        'proposal_sha256': record['proposal_sha256'], 'instruction': REVISION_GUIDANCE}}


def validate_design(engine, run_id, design):
    if not design_context(engine, run_id):
        return
    revision = engine.store.get('revision_plans', f'{run_id}:{engine.cycle(run_id)}')
    record = engine.store.get(COLLECTION, revision['training_adjustment_id']) or {}
    previous = record.get('protected_design') or {}
    from .engine import ResearchBlocked
    if not previous or any(design.get(k) != previous.get(k) for k in PROTECTED_DESIGN):
        raise ResearchBlocked('快速 loss 微调不得改变数据划分、评价标准、种子、停止规则或资源限制。')
