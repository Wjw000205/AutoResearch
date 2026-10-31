"""Same-version protocol readback, separate from scientific cycle revision."""
import asyncio
import copy

from .storage import now


COLLECTION = 'protocol_reviews'


def key(run_id, revision_hash):
    return f'{run_id}:protocol:{revision_hash}'


def accepted(vote, context):
    return (vote.get('approve') is True and not vote.get('required_changes')
            and vote.get('proposal_hash') == context['proposal_hash']
            and vote.get('revision_hash') == context['revision_hash'])


def checked_votes(engine, record):
    from .engine import ROLES, ResearchBlocked
    votes = record['votes']
    if len(votes) != 3 or {v.get('role') for v in votes} != set(ROLES):
        raise ResearchBlocked('实验协议补读缺少完整原始三方意见。')
    for vote in votes:
        hid = record['handoffs'].get(vote['role'], '')
        decision = engine.store.get('decisions', hid, {})
        handoff = engine.store.get('handoffs', hid, {})
        source = engine.store.get('handoff_inputs', hid, {}).get('context', {})
        if (decision.get('run_id') != record['run_id'] or decision.get('role') != vote['role']
                or decision.get('result') != {k: v for k, v in vote.items() if k != 'role'}
                or handoff.get('status') != 'completed' or handoff.get('to_role') != vote['role']
                or any(source.get(k) != record[k] for k in ('design_hash', 'proposal_hash', 'revision_hash'))):
            raise ResearchBlocked('实验协议原始意见与实际交付回执不一致。')
    return votes


def reject(engine, run_id, purpose, instruction, context, votes, handoffs):
    """Persist dissent without claiming a failed experiment or changing design."""
    from .engine import ResearchBlocked
    identity = key(run_id, context['revision_hash'])
    previous = engine.store.get(COLLECTION, identity)
    round_number = previous.get('recovery_round', 0) if previous else 0
    record = {**copy.deepcopy(previous or {}), 'id': identity, 'run_id': run_id, 'cycle': engine.cycle(run_id),
        'design_hash': context['design_hash'], 'revision_hash': context['revision_hash'],
        'proposal_hash': context['proposal_hash'], 'purpose': purpose,
        'instruction': instruction, 'context': copy.deepcopy(context),
        'votes': copy.deepcopy(votes), 'handoffs': copy.deepcopy(handoffs),
        'status': 'blocked', 'recovery_round': round_number, 'updated_at': now(),
        'history': copy.deepcopy(previous.get('history', [])) if previous else []}
    if previous and previous.get('status') == 'reading':
        record['history'].append({k: copy.deepcopy(previous[k]) for k in
            ('votes', 'handoffs', 'context', 'purpose', 'instruction')})
    checked_votes(engine, record)
    engine.store.put(COLLECTION, identity, record)
    position = engine.store.get('graph_status', run_id, {})
    engine.store.put('graph_status', run_id, {**position, 'id': run_id, 'run_id': run_id,
        'node': 'implementation', 'cycle': engine.cycle(run_id), 'status': 'blocked',
        'waiting_for': 'experiment_protocol_review', 'protocol_review_id': identity})
    engine.store.event(run_id, 'experiment.protocol_blocked',
        '代码实现等待同版本实验协议审查；未改写架构或启动新科研周期。',
        {'protocol_review_id': identity, 'votes': votes})
    raise ResearchBlocked('实验协议尚未获得同版本三方批准；代码实现等待补读或处理具体协议意见。')


def activate(engine, run_id, *, expected_failure_hash=None):
    """Explicit, once-only readback of recorded dissent; keep all paid inputs."""
    from .engine import digest, ResearchBlocked
    run = engine.store.get('runs', run_id)
    if run['status'] not in {'blocked', 'paused'}:
        raise ResearchBlocked('协议补读须在任务停止后准备。')
    cycle = engine.cycle(run_id)
    failure = engine.store.get('research_cycles', f'{run_id}:{cycle}')
    legacy = expected_failure_hash is not None
    if legacy:
        step = engine.store.get('steps', run_id + ':implementation', {})
        if (not failure or digest(failure) != expected_failure_hash
                or failure.get('failure_stage') != 'implementation'
                or failure.get('failure', {}).get('kind') != 'protocol_review_rejected'
                or step.get('failure_review') != failure['failure']
                or engine.store.get('revision_plans', f'{run_id}:{cycle + 1}')
                or any(x.get('research_cycle', 1) == cycle and not x.get('superseded')
                       for x in engine.store.list('experiments', run_id))):
            raise ResearchBlocked('原协议失败记录已变化或科研周期已推进；不能恢复旧版本。')
        votes = failure['failure']['details']['protocol_votes']
        rh = failure['failure']['details']['revision_hash']
        handoffs, source = {}, None
        for vote in votes:
            matches = [d for d in engine.store.list('decisions', run_id)
                if d.get('purpose', '').startswith(f'protocol_review:{cycle}:design:')
                and d.get('role') == vote['role']
                and d.get('result') == {k: v for k, v in vote.items() if k != 'role'}]
            if len(matches) != 1:
                raise ResearchBlocked('协议补读不能唯一绑定原评审交付。')
            hid = matches[0]['id']
            handoffs[vote['role']] = hid
            item = engine.store.get('handoff_inputs', hid)
            if not item or source and any(item[k] != source[k] for k in ('instruction', 'context', 'schema')):
                raise ResearchBlocked('协议补读的三方原始输入不一致。')
            source = item
        ctx = source['context']
        record = {'id': key(run_id, rh), 'run_id': run_id, 'cycle': cycle,
            'design_hash': ctx['design_hash'], 'revision_hash': rh, 'proposal_hash': ctx['proposal_hash'],
            'purpose': matches[0]['purpose'], 'instruction': source['instruction'], 'context': ctx,
            'votes': votes, 'handoffs': handoffs, 'status': 'blocked', 'recovery_round': 0,
            'history': [], 'legacy_failure': copy.deepcopy(failure), 'legacy_step': copy.deepcopy(step)}
        if engine.store.get(COLLECTION, record['id']):
            raise ResearchBlocked('本协议已有补读记录；不能覆盖或重复准备。')
    else:
        records = [x for x in engine.store.list(COLLECTION, run_id)
                   if x['cycle'] == cycle and x['status'] == 'blocked']
        if len(records) != 1:
            raise ResearchBlocked('没有唯一待处理的实验协议。')
        record = records[0]
    checked_votes(engine, record)
    if record.get('recovery_round', 0) >= 1:
        raise ResearchBlocked('同版本协议补读已用尽；保留未决意见，停止重复评议。')
    design = engine.outputs(run_id)['design']
    from .experiment_plan import build_plan
    plan = build_plan(design, run['config']['max_trials'])
    rh = digest({'plan': plan, 'acceptance': design['acceptance'],
        'iteration_policy': design['iteration_policy'],
        'task_primary_threshold': run['config'].get('min_relative_improvement', .01)})
    if digest(design) != record['design_hash'] or rh != record['revision_hash']:
        raise ResearchBlocked('协议或架构已变更；补读不能沿用旧版批准。')
    record.update(status='reading', recovery_round=1, activated_at=now())
    writes = [(COLLECTION, record['id'], record)]
    if legacy:
        # Keep complete old records in the recovery journal. The graph cursor
        # is redirected on dispatch, never edited in the checkpoint database.
        revised_step = {k: v for k, v in step.items() if k not in {'failure_review', 'completed_at', 'next_node'}}
        revised_step.update(status='blocked', output=None, waiting_for='experiment_protocol_review')
        writes.append(('steps', step['id'], revised_step))
        revision = engine.store.get('steps', run_id + ':revision')
        if revision:
            record['legacy_revision_step'] = copy.deepcopy(revision)
            writes.append(('steps', revision['id'], {**revision, 'status': 'skipped',
                'output': {'summary': '原节点误将协议补读路由为方案修订；已恢复同版本实现入口。'}}))
        writes.append(('research_cycles', failure['id'], {**failure,
            'status': 'protocol_review_pending', 'routing_recovery_id': record['id']}))
    engine.store.batch(writes)
    engine.update_run(run_id, stage='implementation',
        error='等待同版本实验协议补读，尚未获得执行批准。',
        state={**run.get('state', {}), 'protocol_review_return': record['id']})
    position = engine.store.get('graph_status', run_id, {})
    engine.store.put('graph_status', run_id, {**position, 'id': run_id, 'run_id': run_id,
        'node': 'implementation', 'cycle': cycle, 'status': 'waiting',
        'waiting_for': 'experiment_protocol_review', 'protocol_review_id': record['id']})
    engine.store.event(run_id, 'experiment.protocol_readback_prepared',
        '已保存一次同版本补读交接，保留原投票、架构和科研周期。',
        {'protocol_review_id': record['id'], 'legacy_route_recovered': legacy})
    return record


async def review(engine, h, run_id, purpose, instruction, context, schema):
    from .engine import Harness, ROLES, ResearchBlocked
    recovery = engine.store.get(COLLECTION, key(run_id, context['revision_hash']))
    retained, handoffs = {}, {}
    if recovery:
        if recovery.get('segmented_review'):
            if any(recovery[k] != context[k] for k in ('design_hash', 'proposal_hash', 'revision_hash')):
                raise ResearchBlocked('协议分段审计版本绑定已变化。')
            from .protocol_review_segments import advance
            return await advance(engine, h, run_id, recovery)
        if recovery['status'] != 'reading':
            raise ResearchBlocked('实验协议存在未决意见；须先准备同版本补读，不能重复旧评审。')
        if any(recovery[k] != context[k] for k in ('design_hash', 'proposal_hash', 'revision_hash')):
            raise ResearchBlocked('协议补读版本绑定已变化。')
        for vote in checked_votes(engine, recovery):
            if accepted(vote, context):
                retained[vote['role']] = vote
                handoffs[vote['role']] = recovery['handoffs'][vote['role']]
        # Reopen the exact saved index scope, including original SHA values.
        context = {**copy.deepcopy(recovery['context']), 'protocol_readback_protocol': 1,
            'prior_protocol_votes': copy.deepcopy(recovery['votes'])}
        instruction = recovery['instruction'] + (
            '\n本次仅补齐同一冻结协议的未决原文核验，不修改方案、配置或阈值。'
            '根据本角色先前 required_changes 定向读取协议及批准架构，'
            '逐项核对冻结计划中每个分支和种子的配置、'
            'final_pairs/final_test_seeds及停止规则。阅读窗口只是每次请求上限，'
            '已归档的关键正文可继续 documents.read 重开；归档本身不是方案缺陷。'
            '实际核验后才作独立判断；证据仍缺失则保持不批准，并列出精确缺口。'
            '禁止把主任意见或索引摘要当作自己的实际阅读。')
        purpose = recovery['purpose'] + ':readback:1'
    roles = [role for role in ROLES if role not in retained]
    from . import comparison_task_policy as comparison
    assignments = {role: comparison.assignment(engine, run_id, role, purpose,
        instruction, context, schema, comparison.REVIEW_GUIDANCE) for role in roles}
    if engine.store.get('runs', run_id)['mode'] == 'live' and hasattr(h.intelligence, 'preflight_batch'):
        pending = [role for role in roles if not engine.store.get('decisions',
            Harness.decision_key(run_id, role, purpose, *assignments[role]))]
        for role in pending:
            await h.complete_pending_tools(run_id, role, purpose, *assignments[role])
        requests = [h.next_request(run_id, role, purpose, *assignments[role]) for role in pending
            if not engine.store.get('decisions', Harness.decision_key(run_id, role, purpose, *assignments[role]))]
        if requests:
            if hasattr(h.intelligence, 'prepare_context_batch'):
                await h.intelligence.prepare_context_batch(run_id, requests)
            await engine._read_consensus_check(run_id, h.intelligence.preflight_batch, run_id, requests)
    tasks = [asyncio.create_task(h.ask(run_id, role, purpose, *assignments[role])) for role in roles]
    try:
        raw = await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    for role, vote in zip(roles, raw):
        retained[role] = {'role': role, **vote}
        handoffs[role] = Harness.decision_key(run_id, role, purpose, *assignments[role])
    return [retained[role] for role in ROLES], handoffs
