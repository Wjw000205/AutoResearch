"""Persistent, reviewed incident disposition and successor planning.

This task publishes evidence, never execution authorization. A published plan
returns to the ordinary proposal/read/vote/resource gates. Historical answers,
criteria, measurements and alerts are immutable.
"""
from __future__ import annotations

import copy

from .prose_schema import validate

from .diagnostic_planning_recovery import authenticate
from .experiment_workflow import sha
from .storage import now
from .training_adjustment import obj

COLLECTION = 'parameter_resolution_tasks'
ROLES = ('director', 'engineer', 'scout')
TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 1200}
PATHS = {'type': 'array', 'minItems': 1, 'maxItems': 12, 'uniqueItems': True,
         'items': {'type': 'string', 'minLength': 2, 'maxLength': 500}}


def incidents(evidence):
    active = evidence.get('training_investigation', {})
    observations = active.get('observations', {})
    return [r['id'] for r in evidence['records']
        if evidence['diagnostics'][r['id']].get('needs_discussion')
        or observations.get(r['id'], {}).get('validation_alignment', {}).get('signal')
            == 'training_down_validation_worse_than_best'
        or evidence['diagnostics'][r['id']].get('status') == 'insufficient_evidence'
            and r.get('provenance') == 'real_experiment_execution']


def event_sha(evidence, tid):
    row = next(r for r in evidence['records'] if r['id'] == tid)
    return sha({'trial': row, 'diagnostic': evidence['diagnostics'][tid],
                'observation': evidence['training_investigation']['observations'].get(tid)})


def assessment(evidence):
    return evidence['dataset_optimization']['architecture_certificate']['body']['assessment']


def basis(evidence):
    return sha({k: evidence.get(k) for k in ('binding', 'records', 'diagnostics', 'code_sources',
                                           'parameter_grids', 'dataset_optimization')})


def schema(evidence, ref):
    return obj({'evidence_sha256': {'const': ref['sha256']},
        'assessment_sha256': {'const': sha(assessment(evidence))},
        'events': {'type': 'array', 'maxItems': 128, 'items': obj({
            'trial_id': {'enum': incidents(evidence) or ['no_incident']},
            'state': {'enum': ['open', 'not_applicable', 'resolved']},
            'criterion_path': {'const': '/dataset_optimization/architecture_certificate/body/assessment/stability_rationale'},
            'evidence_paths': PATHS, 'rationale': TEXT,
            'verification_trial_ids': {'type': 'array', 'maxItems': 8, 'uniqueItems': True,
                                       'items': {'type': 'string'}}})},
        'next_task': obj({'kind': {'enum': ['diagnose', 'search_parameters', 'complete_optimization', 'external_dependency']},
            'objective': TEXT, 'intervention': TEXT, 'control': TEXT, 'measurement': TEXT,
            'decision_rule': TEXT, 'resource_limits': TEXT,
            'dependency': {'anyOf': [{'type': 'null'}, obj({
                'owner': {'enum': ['user', 'data_provider', 'environment']},
                'required_input': TEXT, 'why_agents_cannot_supply': TEXT})]}}),
        'execution_authorized': {'const': False}, 'scientific_approval': {'const': False}})


def validate_plan(plan, evidence, ref):
    from .experiment_decisions import _pointer
    validate(plan, schema(evidence, ref))
    ids = [e['trial_id'] for e in plan['events']]
    if len(ids) != len(set(ids)) or set(ids) != set(incidents(evidence)):
        raise ValueError('Disposition must cover every incident exactly once, including non-focus candidates')
    known = {r['id']: r for r in evidence['records']}
    alerts = set(incidents(evidence))
    for event in plan['events']:
        if not any(p.startswith(('/training_investigation/', '/diagnostics/', '/records/', '/comparisons/'))
                   for p in event['evidence_paths']):
            raise ValueError('Each incident disposition must cite measured evidence, not only a policy clause')
        for path in [event['criterion_path'], *event['evidence_paths']]:
            try:
                _pointer(evidence, path)
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise ValueError('Disposition cites missing original evidence: ' + path) from exc
        verification = event['verification_trial_ids']
        if event['state'] != 'resolved' and verification:
            raise ValueError('Only a resolved incident may claim verification trials')
        if event['state'] == 'resolved':
            control = known[event['trial_id']]
            rows = [known[t] for t in verification if t in known]
            if (len(rows) != len(verification) or not rows or event['trial_id'] in verification
                    or set(verification) & alerts or any(r['status'] != 'completed' for r in rows)
                    or {r['params'].get('variant') for r in rows} != {'baseline', 'proposed'}
                    or any(any(r['params'].get(k) != control['params'].get(k)
                               for k in ('seed', 'dataset', 'benchmark_task_id')) for r in rows)):
                raise ValueError('Resolved incident requires a distinct completed clean same-seed/task verification pair')
            # A historical or unrelated pair cannot retroactively resolve a later incident.
            from datetime import datetime
            def timestamp(row, *fields):
                raw = next((row[k] for k in fields if row.get(k)), None)
                if not isinstance(raw, str):
                    raise ValueError('Verification order requires recorded trial timestamps')
                try:
                    return datetime.fromisoformat(raw.replace('Z', '+00:00')).timestamp()
                except ValueError as exc:
                    raise ValueError('Verification order has an invalid trial timestamp') from exc
            if any(timestamp(r, 'started_at', 'created_at') <= timestamp(control, 'completed_at', 'started_at', 'created_at') for r in rows):
                raise ValueError('Verification pair must be newer than its incident')
    nxt = plan['next_task']
    if (nxt['kind'] == 'external_dependency') != (nxt['dependency'] is not None):
        raise ValueError('External waiting requires a concrete external owner/input; agent planning is not an external dependency')
    if nxt['kind'] == 'complete_optimization' and any(e['state'] == 'open' for e in plan['events']):
        raise ValueError('Open incidents require a diagnostic/search successor, not optimization completion')


def owner(engine, run_id, role, request):
    return authenticate(engine, run_id, role, request['purpose'],
                        {k: request[k] for k in ('instruction', 'context', 'schema')}, request['answer'])


def verified(engine, run_id, task):
    from .diagnostic_planning_recovery import original
    archive = engine.store.get('experiment_evidence', task['evidence']['id']) or {}
    evidence = archive.get('summary')
    if (task.get('run_id') != run_id or archive.get('run_id') != run_id
            or sha(evidence) != task['evidence']['sha256'] or basis(evidence) != task['basis_sha256']):
        raise ValueError('Parameter resolution source evidence changed')
    held = engine.store.get('experiment_decisions', task['source_decision_id']) or {}
    proposal = source_proposal(held)
    if not proposal or sha(proposal) != task['source_proposal_sha256']:
        raise ValueError('Parameter resolution source disposition changed')
    original(engine, run_id, held, proposal, handoff_id=task.get('source_handoff_id'))
    final = task['rounds'][-1]
    author = final.get('correction') or final['author']
    owner(engine, run_id, 'engineer', author)
    plan = author['answer']
    validate_plan(plan, evidence, task['evidence'])
    if task.get('plan_sha256') != sha(plan):
        raise ValueError('Parameter resolution plan changed')
    if set(final.get('votes', {})) != set(ROLES):
        raise ValueError('Parameter resolution requires all three independent votes')
    for role, request in final['votes'].items():
        owner(engine, run_id, role, request)
        vote = request['answer']
        if (vote['proposal_sha256'] != sha(plan) or vote['evidence_sha256'] != task['evidence']['sha256']
                or not vote['approve'] or vote['issues']):
            raise ValueError('Parameter resolution has unresolved independent dissent')
    return evidence, plan


def apply(engine, run_id, evidence):
    """Project authenticated dispositions, without changing raw observations."""
    tasks = [t for t in engine.store.list(COLLECTION, run_id) if t.get('status') == 'accepted']
    if not tasks:
        return
    applicable, states, successor, successor_id = [], {}, None, None
    from .tuning_progress_recovery import current as progress_recovery
    recovery = progress_recovery(engine, run_id)
    excluded_tasks = set(recovery['body']['task_ids']) if recovery else set()
    current_alerts = set(incidents(evidence))
    for task in sorted(tasks, key=lambda t: (t['created_at'], t['id'])):
        source, plan = verified(engine, run_id, task)
        if (source['binding'] != evidence['binding']
                or sha(assessment(source)) != sha(assessment(evidence))):
            continue
        matched = []
        for event in plan['events']:
            tid = event['trial_id']
            if tid in current_alerts and event_sha(source, tid) == event_sha(evidence, tid):
                # Verify referenced follow-up receipts too, not merely the old incident.
                ids = event['verification_trial_ids']
                if any(t not in {r['id'] for r in evidence['records']}
                       or event_sha(source, t) != event_sha(evidence, t) for t in ids):
                    continue
                states[tid] = {'state': event['state'], 'task_id': task['id'],
                               'plan_sha256': task['plan_sha256'], 'rationale': event['rationale']}
                matched.append(tid)
        applicable.append({'id': task['id'], 'plan_sha256': task['plan_sha256'],
                           'evidence': task['evidence'], 'matched_trial_ids': matched,
                           'plan': copy.deepcopy(plan),
                           'independent_votes': {role: copy.deepcopy(request['answer'])
                               for role, request in task['rounds'][-1]['votes'].items()}})
        if task['basis_sha256'] == basis(evidence):
            if task['id'] in excluded_tasks:
                successor, successor_id = None, None
                continue
            successor = copy.deepcopy(plan['next_task'])
            successor_id = task['id']
            completed = task.get('diagnostic_successor') or {}
            if completed.get('status') in {'completed', 'execution_failed', 'implementation_rejected', 'implementation_blocked'}:
                diagnostic = engine.store.get('bounded_diagnostics', completed['diagnostic_id']) or {}
                if (diagnostic.get('source_task_id') != task['id'] or diagnostic.get('plan_sha256') != task['plan_sha256']
                        or diagnostic.get('status') != completed['status']):
                    raise ValueError('Completed parameter diagnostic successor changed')
                # Its authenticated result is in training_investigation. Consume
                # the old work item; the next ordinary decision must assess it.
                successor = None
    if not applicable:
        return
    active = evidence['training_investigation']
    open_ids = current_alerts - {t for t, v in states.items() if v['state'] in {'not_applicable', 'resolved'}}
    active.update(incident_dispositions=states, open_incident_trial_ids=sorted(open_ids),
                  incident_review_contract=1)
    # Current searched candidate and its paired control first; preserve the full backlog.
    ranking = []
    for grid in reversed(evidence.get('parameter_grids', [])):
        unit = next((u for u in evidence['units'] if u['id'] == grid.get('best_measured_unit_id')), {})
        names = set(unit.get('jobs', []))
        for row in sorted(evidence['records'], key=lambda r: r['params'].get('variant') != 'proposed'):
            if any(evidence['job_parameters'][n] == row['params'] for n in names):
                ranking.append(row['id'])
    ranking += sorted(open_ids)
    focus = list(dict.fromkeys(t for t in ranking if t in open_ids))[:4]
    # Keep the selected comparison pair together when there is room.
    focus += [t for t in dict.fromkeys(ranking) if t not in focus][:max(0, 4-len(focus))] if focus else []
    active['focus_trial_ids'] = focus
    active['required'] = bool(open_ids or (active.get('followup') or {}).get('requires_reassessment'))
    evidence['parameter_resolution'] = {'reviews': applicable, 'next_task': successor, 'successor_task_id': successor_id,
        'execution_authorized': False, 'scientific_approval': False,
        'instruction': 'Follow the independently reviewed next task through normal proposal and execution gates. '
                       'Historical alerts remain in observations; dispositions do not change root-cause truth or scientific thresholds.'}


def compact(value):
    return {**{k: copy.deepcopy(v) for k, v in value.items() if k != 'reviews'},
            'reviews': [{k: r[k] for k in ('id', 'plan_sha256', 'evidence', 'matched_trial_ids')}
                        for r in value['reviews']], 'details_path': '/parameter_resolution'}


def track_successor(engine, run_id, evidence, decision):
    """Bind the published work item to its actual normal decision lifecycle.

    Tracking is deliberately not included in scientific evidence fingerprints.
    """
    context = evidence.get('parameter_resolution') or {}
    tid = context.get('successor_task_id')
    if not tid:
        return
    task = engine.store.get(COLLECTION, tid)
    if not task or task['run_id'] != run_id or task['status'] != 'accepted':
        raise ValueError('Reviewed parameter successor task is unavailable')
    progress = {'decision_id': decision['id'], 'status': decision['status'],
                'action': (decision.get('proposal') or {}).get('action')}
    if task.get('successor') != progress:
        task['successor'] = progress
        engine.store.put(COLLECTION, tid, task)


def source_proposal(record):
    gate = record.get('parameter_gate_followup') or {}
    if record.get('status') == 'blocked_parameter_gates' and gate.get('disposition'):
        return gate['disposition']
    planning = record.get('diagnostic_planning_recovery') or {}
    if record.get('status') == 'invalid' and planning.get('synthesis'):
        return record.get('rejected_proposal')
    return None


async def resume(engine, harness, run_id, evidence, record):
    """Resolve an authenticated hold into reviewed work, in the same stage."""
    from .engine import ResearchBlocked
    from .experiment_decisions import packet
    from .diagnostic_planning_recovery import original
    from .parameter_gate_followup import disposition_vote_schema
    source = source_proposal(record)
    if not source or (evidence.get('dataset_optimization') or {}).get('phase') != 'parameters':
        return None
    ref = record['evidence']
    if sha(evidence) != ref['sha256']:
        raise ValueError('Parameter resolution cannot reuse stale hold evidence')
    source_handoff = original(engine, run_id, record, source)
    key = run_id + ':parameter-resolution:' + ref['sha256']
    task = engine.store.get(COLLECTION, key)
    if not task:
        task = {'id': key, 'run_id': run_id, 'evidence': copy.deepcopy(ref),
            'source_decision_id': record['id'], 'source_proposal_sha256': sha(source),
            'source_handoff_id': source_handoff,
            'basis_sha256': basis(evidence), 'created_at': now(), 'status': 'planning', 'rounds': []}
    elif task['source_proposal_sha256'] != sha(source) or task['basis_sha256'] != basis(evidence):
        raise ValueError('Parameter resolution source hold changed')

    async def save(**changes):
        engine.check_active(run_id)
        task.update(changes, updated_at=now())
        engine.store.put(COLLECTION, key, task)
        engine.store.event(run_id, 'experiment.parameter_resolution',
            '参数问题处置：' + task['status'], {'task_id': key, 'status': task['status'], 'execution_authorized': False})

    async def ask(role, request, status):
        if 'answer' not in request:
            request['answer'] = await harness.ask(run_id, role, request['purpose'], request['instruction'],
                                                request['context'], request['schema'])
            await save(status=status)
        owner(engine, run_id, role, request)
        return request['answer']

    if task['status'] == 'waiting_external':
        raise ResearchBlocked(task['blocker'])
    if task['status'] == 'accepted':
        verified(engine, run_id, task)
        return {'tuning_pending': True, 'next_node': 'tuning', 'activity': 'parameter_resolution', 'task_id': key}
    context = packet(evidence, ref)
    from .bounded_diagnostic_contract import capabilities, GUIDANCE as capability_guidance
    context['experiment_decision']['bounded_diagnostic_capabilities'] = capabilities()
    context['experiment_decision']['parameter_resolution_task'] = {
        'task_id': key, 'source_proposal': source, 'frozen_assessment': assessment(evidence),
        'incident_index': [{'trial_id': t, 'observation': evidence['training_investigation']['observations'].get(t),
                            'diagnostic': evidence['diagnostics'][t]} for t in incidents(evidence)],
        'prior_resolution': evidence.get('parameter_resolution'),
        'scope': 'All incidents, not only the first four. Plan and classify; no execution or acceptance authorization.'}
    # Keep the dispatch compact; trajectories and module arrays remain accessible by pointer.
    for item in context['experiment_decision']['parameter_resolution_task']['incident_index']:
        obs = item['observation'] or {}
        item['observation'] = {k: obs.get(k) for k in ('available', 'epoch_count', 'epoch_descriptive_trend',
            'validation_alignment', 'checkpoint_alignment', 'failure_event', 'host_binding_checks')}
        item['observation_path'] = '/training_investigation/observations/' + item['trial_id']
    from .tuning_consensus import mediate, issues, pending
    round_number = max(0, len(task['rounds']) - 1)
    resolution = None
    if task['rounds'] and (task['rounds'][-1].get('needs_reconciliation') or
            task['status'] in {'review_disputed', 'contract_blocked', 'no_progress'}):
        previous = task['rounds'][-1]
        previous['needs_reconciliation'] = True
        if task['status'] in {'contract_blocked', 'no_progress'} and not previous.get('error'):
            previous['error'] = task.get('blocker')
        prior_votes = [{'role': role, **v['answer']} for role,v in previous.get('votes', {}).items()]
        resolution = await mediate(engine, harness, run_id, previous, context,
            {'task_id': key, 'round': round_number,
             'plan': (previous.get('correction') or previous['author']).get('answer'), 'votes': prior_votes},
            issues(prior_votes, previous.get('error')),
            lambda: save(status='reconciling'))
        round_number += 1
    for round_number in [round_number]:
        if len(task['rounds']) <= round_number:
            request_context = copy.deepcopy(context)
            request_context['experiment_decision']['parameter_resolution_task']['review_feedback'] = [
                {role: v['answer'] for role, v in r.get('votes', {}).items()} for r in task['rounds'][-2:]]
            if resolution:
                request_context['experiment_decision']['parameter_resolution_task']['consensus_resolution'] = resolution
            task['rounds'].append({'author': {
                'purpose': 'experiment_decide:' + ref['sha256'] + f':parameter-resolution:{round_number}',
                'context': request_context, 'schema': schema(evidence, ref),
                'instruction': '逐项处置所有训练告警，并设计一个具体后继任务。先解释原 stability_rationale 的适用范围，'
                    '不改 criteria、科学阈值或历史记录。events 覆盖全部 incident_index。open 保留未知；'
                    'not_applicable 仅用于证据支持该告警不适用原条款的情形，不能因最佳分数好、零失败或根因未知就豁免；'
                    'resolved 必须引用较新的、同种子同任务且无告警的已完成成对复验。所有处置交三方独立审批。'
                    '若仍有 open，next_task 提出一个可证伪的诊断或有限单因素搜索协议，明确干预、控制、测量、'
                    '预声明判别条件及数值资源上限；允许设计新任务，不局限于已有插桩，也不得重跑已完成检查。'
                    '协议草案不是执行授权，下一轮工程师仍须给出正常提案、三方审核并过资源门禁。'
                    'complete_optimization 仅表示申请正常稳定性评审，不是已批准稳定。'
                    '不能把需要 director/engineer/scout/宿主规划的工作写成 external_dependency；只有明确的外部输入、'
                    '权限或环境不可用才能等待，并说明为何代理不能提供。保留 unknown/inconclusive，不为推进伪造归因。'
                    '必要时按提供的原证据 pointer 定向读取；禁止使用摘要 /counts 或 /failure_count 作为原路径。' + capability_guidance}})
            await save(status='planning')
        current = task['rounds'][round_number]
        author = current.get('correction') or current['author']
        plan = await ask('engineer', author, 'validating_plan')
        try:
            validate_plan(plan, evidence, ref)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            if 'correction' in current:
                current.update(error=str(exc), needs_reconciliation=True)
                await save(status='reconciling')
                return pending(task_id=key, reason='参数处置合同需进一步定向修订')
            corrected_context = copy.deepcopy(author['context'])
            corrected_context['experiment_decision']['parameter_resolution_task']['contract_correction'] = {
                'original_answer': plan, 'original_sha256': sha(plan), 'error': str(exc)}
            current['correction'] = {**{k: copy.deepcopy(author[k]) for k in ('instruction', 'schema')},
                'context': corrected_context, 'purpose': author['purpose'] + ':contract-correction-1',
                'instruction': author['instruction'] + '\n只纠正明确的合同错误，保留原证据、判据和未知；原回答保持不变。'}
            await save(status='correcting_plan')
            plan = await ask('engineer', current['correction'], 'validating_plan')
            try:
                validate_plan(plan, evidence, ref)
            except (ValueError, KeyError, IndexError, TypeError) as second:
                current.update(error=str(second), needs_reconciliation=True)
                await save(status='reconciling')
                return pending(task_id=key, reason='参数处置合同需进一步定向修订')
        current.setdefault('votes', {})
        for role in ROLES:
            if role not in current['votes']:
                vote_context = copy.deepcopy(context)
                vote_context.update(decision_proposal=plan, proposal_sha256=sha(plan))
                current['votes'][role] = {'purpose': 'experiment_vote:' + sha(plan) + ':parameter-resolution:' + str(round_number),
                    'instruction': '独立审核每一告警处置及下一任务协议。未知根因保持未知；not_applicable 必须由原条款范围和实测证据支持，'
                        '不能借解释放松科学阈值、隐去失败或把描述性告警当根因。确认后继任务具体、非重复、可判别且资源有界。'
                        '需要代理继续设计的事项不能归给外部。批准仅发布处置证据和待办，后续仍经提案/三方/资源/预算门禁。'
                        '同意时 approve=true、issues=[]；否则明确具体异议。',
                    'context': vote_context, 'schema': disposition_vote_schema(ref['sha256'], sha(plan))}
                await save(status='reviewing_plan')
            await ask(role, current['votes'][role], 'reviewing_plan')
        votes = [v['answer'] for v in current['votes'].values()]
        if not all(v['approve'] and not v['issues'] for v in votes):
            current['needs_reconciliation'] = True
            await save(status='reconciling')
            return pending(task_id=key, reason='参数处置仍有异议，继续协调、修订与独立复审')
        task['plan_sha256'] = sha(plan)
        verified(engine, run_id, task)
        if plan['next_task']['kind'] == 'external_dependency':
            dependency = plan['next_task']['dependency']
            await save(status='waiting_external', blocker='参数任务等待外部输入：' + dependency['owner'] + '；'
                + dependency['required_input'] + '；' + dependency['why_agents_cannot_supply'])
            raise ResearchBlocked(task['blocker'])
        # Repeating the same approved work without new observations cannot spin forever.
        previous = [t for t in engine.store.list(COLLECTION, run_id)
                    if t['id'] != key and t.get('status') == 'accepted' and t['basis_sha256'] == task['basis_sha256']]
        for old in previous:
            _, old_plan = verified(engine, run_id, old)
            if old_plan['events'] == plan['events'] and old_plan['next_task'] == plan['next_task']:
                current.update(error='参数任务未产生新证据或新的可执行协议；必须解释前次方案不足并提出不同的可检验下一步。',
                               needs_reconciliation=True)
                await save(status='reconciling')
                return pending(task_id=key, reason=current['error'])
        from .experiment_decisions import snapshot
        current_evidence = await engine._read_consensus_check(run_id, snapshot, engine, run_id)
        if sha(current_evidence) != ref['sha256']:
            raise ResearchBlocked('Parameter resolution evidence changed during independent review; replan against current evidence')
        await save(status='accepted')
        return {'tuning_pending': True, 'next_node': 'tuning', 'activity': 'parameter_resolution', 'task_id': key}
