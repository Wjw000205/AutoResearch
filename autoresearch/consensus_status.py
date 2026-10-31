"""Read-only consensus progress from saved assignments, votes and gates."""
from datetime import datetime, timezone

from .storage import redact

ROLES = ('director', 'engineer', 'scout')


def _elapsed(start, checked):
    try:
        parsed = datetime.fromisoformat(start.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return None
        return max(0, int((checked - parsed).total_seconds()))
    except (TypeError, ValueError, AttributeError):
        return None


def view(engine, run_id, pending):
    """A status view never grants consensus or replaces the last full review."""
    from .discussion_actions import scope
    from .engine import digest
    store = engine.store
    run = store.get('runs', run_id)
    if run['stage'] != 'debate':
        return None
    checked = datetime.now(timezone.utc)
    committed = store.get('consensus', run_id) or {}
    if (committed.get('cycle', 1) != engine.cycle(run_id)
            or not isinstance(committed.get('proposal'), dict)
            or digest(committed['proposal']) != committed.get('proposal_hash')):
        committed = {}
    round_number = committed.get('round')
    work = store.get('stage_contexts', f'{run_id}:discussion-actions:{scope(engine, run_id)}:{round_number}') or {}
    if (not committed or work.get('run_id') != run_id or work.get('round') != round_number
            or work.get('proposal_hash') != committed.get('proposal_hash')):
        work = {}
    handoffs = {row['id']: row for row in store.list('handoffs', run_id)}
    started = None
    votes = committed.get('votes', [])
    phase = 'awaiting_revision'
    action = '核对上一轮异议及推进条件。'
    if pending and pending['phase'] == 'voting':
        phase, action, round_number = 'voting', '三方正在独立评审当前冻结方案。', pending['round']
        votes = pending['votes']
        frozen = store.get('debate_inputs', pending['source_id']) or {}
        gate_context = frozen.get('context', {})
        started = frozen.get('created_at')
    else:
        gate_context = committed
        if pending and pending['phase'] == 'preparing_proposal':
            phase, action = 'integrating', '总监正在整合补充证据与异议修正，随后对新版方案独立复审。'
            started = handoffs.get(pending.get('source_id'), {}).get('created_at')
            votes = []  # Earlier approvals cannot approve an unwritten revision.
        elif work.get('status') == 'working':
            phase, action = 'supplementing', '文献与工程角色正在交付阻塞项的具体修正。'
            started = work.get('created_at')
        elif committed.get('unanimous') is True:
            phase, action = 'ready', '当前方案已形成共识，准备进入架构设计。'
    stopped = run['status'] in {'blocked', 'paused', 'failed', 'cancelled'}
    if stopped:
        action = '任务已停止，保留当前检查点；处理停止原因后继续。'
    proposal_hash = (pending or {}).get('proposal_hash') if phase == 'voting' else committed.get('proposal_hash')
    by_role = {role: [v for v in votes if v.get('role') == role
        and v.get('proposal_hash') == proposal_hash and type(v.get('approve')) is bool] for role in ROLES}
    vote_states = [{'role': role, 'status': 'approved' if len(by_role[role]) == 1
        and by_role[role][0]['approve'] and not by_role[role][0].get('required_changes')
        else 'changes_requested' if len(by_role[role]) == 1 else 'pending'} for role in ROLES]
    gates = {name: (gate_context.get(field) or {}).get('approved') for name, field in (
        ('evidence', 'evidence_assessment'), ('resources', 'resource_assessment'), ('problem', 'problem_assessment'))}
    issues = (gate_context.get('evidence_assessment') or {}).get('idea_provenance', {}).get('issues', [])
    blockers = [issue for issue in issues if isinstance(issue, str)]
    ready = (phase == 'ready' and not stopped and all(v['status'] == 'approved' for v in vote_states)
             and all(gate is True for gate in gates.values()))
    if phase == 'ready' and not ready:
        phase, action = 'awaiting_revision', '核对已保存共识的投票和门禁状态。'
    ongoing = []
    for native in store.recent_records('codex_reasoning', run_id, limit=12):
        cp = native.get('checkpoint', {})
        if stopped or run['status'] != 'running' or cp.get('status') != 'running':
            continue
        purpose = native.get('selection', {}).get('purpose', '').split(':')[0]
        errors = cp.get('server_errors', [])
        last_error = errors[-1] if errors and isinstance(errors[-1], dict) else {}
        ongoing.append({'role': native.get('role'), 'action': {
            'context_evidence': '核对原文摘录', 'evidence': '补充文献证据',
            'revise': '整合新版方案', 'vote': '独立评审方案',
        }.get(purpose, '处理当前研究请求'), 'started_at': native.get('created_at'),
            'elapsed_seconds': _elapsed(native.get('created_at'), checked),
            'last_activity_at': cp.get('last_event_at'),
            'connection_note': '本次请求记录过连接错误；系统已安排恢复原请求，继续等待回复。'
                if last_error.get('will_retry') is True else None,
            'response_received': bool(native.get('result'))})
    return redact({'run_id': run_id, 'derived': True, 'checked_at': checked.isoformat(),
        'phase': phase, 'run_status': run['status'], 'round': round_number,
        'next_round': (pending or {}).get('next_round'), 'action': action,
        'started_at': started, 'elapsed_seconds': _elapsed(started, checked),
        'votes': vote_states, 'approved_votes': sum(v['status'] == 'approved' for v in vote_states),
        'expected_votes': 3, 'vote_scope': 'current_revision' if phase == 'integrating' else 'frozen_proposal',
        'previous_round': committed.get('round'), 'gates': gates, 'blocking_issues': blockers,
        'blocker_scope': 'previous_proposal_pending_recheck' if phase == 'integrating' else 'current_frozen_proposal',
        'work_progress': {'assigned_roles': sorted(work.get('assignments', {})),
            'delivered_roles': [d['role'] for d in work.get('deliveries', [])], 'status': work.get('status')},
        'ongoing_requests': ongoing, 'ready_for_next_stage': ready,
        'next_stage': 'design', 'next_stage_label': '架构设计',
        'advance_condition': '新版提案提交后，三方对同一版本独立同意，且证据、问题定义与资源校验通过。',
        'eta_seconds': None, 'eta_reason': '完成时间取决于模型回复、补证据及独立评审结果。',
        'stop_reason': run.get('error') if stopped else None,
        'scope': '只读状态投影；等待、历史赞成票和补充交付不授予新版本共识或实验授权。'})
