"""Read-only research map derived from committed assignments and evidence.

Public model answers and tool records are observable. Private provider reasoning
is neither requested nor presented as an observable stream.
"""
import base64
import binascii
import hashlib
import json

from .storage import redact
from .orchestration import STAGES, NEXT

LABELS = {'intake': '目标与资源', 'literature': '文献检索与核验', 'innovation': '创新点讨论', 'related_work': 'idea相关论文分析', 'debate': '三方立项共识',
          'design': '实验架构', 'implementation': '代码与独立审查', 'experiments': '基线与提案验证',
          'tuning': '验证集调参', 'architecture_tuning': '架构微调', 'validation_review': '三方结果评议', 'revision': '修订研究方案',
          'final_tests': '冻结后最终测试', 'report': '结果与复现报告', 'end': '本轮流程完成'}
PURPOSES = {'innovation_candidate': '独立提出创新点', 'innovation_compare': '比较并收束创新点',
            'dataset_configure': '配置实验数据与运行接口', 'dataset_config_review': '复核实验配置与数据接口',
            'dataset_select': '选择任务数据集', 'dataset_work_order': '编写配置与代码任务单',
            'dataset_plan_review': '复核数据选型与实现任务单',
            'proposal': '提出研究方案', 'vote': '独立立项投票', 'revise': '根据异议修订方案',
            'evidence': '文献补充核验', 'amendment': '提交修改方案',
            'discussion_action': '交付讨论阻塞项的具体修正', 'literature_reading': '补读并保存文献证据',
            'implementation_spec': '制定明确实现规范', 'spec_review': '独立评审实现规范', 'code_review': '独立评审实际代码',
            'design_document_section': '分段评议架构文档', 'design_document_review': '汇总架构文档评议',
            'design_revision': '根据具体意见修订架构文档',
            'architecture_definitions_author': '补充未闭合的具体科学定义',
            'architecture_definitions_review': '独立审议科学定义补充',
            'architecture_diagram_author': '绘制神经网络图、流程图及段落解释',
            'architecture_definition_issue_author': '逐项编写科学定义候选',
            'architecture_definition_work_plan': '整理定义依赖与评议范围',
            'architecture_component_review': '审议相关定义与具体修正',
            'implementation_document_section': '历史分段实现文档评议', 'implementation_document_review': '三方整篇架构与实现对齐',
            'implementation_document_sections_v1': '历史批次实现文档评议',
            'review': '独立审查代码', 'repair': '修复实验代码', 'tune': '提出参数候选',
            'cycle_review': '评议本轮验证结果', 'protocol_review': '审查预定执行与停止规则', **LABELS}


def public_text(value):
    if isinstance(value, str):
        return redact(value)
    return json.dumps(redact(value), ensure_ascii=False, indent=2)


def _review_consensus(result):
    """Public full-history review fields; the small card has a bounded preview."""
    if not isinstance(result, dict):
        return {}
    value = {key: result[key] for key in ('approve', 'proposal_hash', 'revision_hash',
        'rationale', 'issues', 'fixes', 'required_changes', 'review_routing') if key in result}
    for name in ('issues', 'fixes', 'required_changes'):
        if isinstance(value.get(name), list):
            value[name + '_count'] = len(value[name])
    routing = value.get('review_routing')
    if isinstance(routing, dict) and isinstance(routing.get('issues'), list):
        value['review_routing_issues_count'] = len(routing['issues'])
        value['review_routing_kinds'] = list(dict.fromkeys(item['kind'] for item in routing['issues']
            if isinstance(item, dict) and item.get('kind') in
                ('document_revision', 'external_dependency', 'review_incomplete')))
    if any(key in value for key in ('issues', 'fixes', 'required_changes', 'review_routing')):
        value['feedback_truncated'] = False
    return value


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def current_debate(engine, run_id):
    """Project unfinished work without changing a committed consensus or casting votes."""
    store = engine.store
    run, cycle = store.get('runs', run_id), engine.cycle(run_id)
    if run['stage'] not in {'debate', 'revision'}:
        return None
    consensus = store.get('consensus', run_id) or {}
    if run['stage'] == 'revision':
        return {'derived': True, 'phase': 'preparing_proposal', 'status': run['status'], 'cycle': cycle,
                'round': None, 'proposal': None, 'proposal_hash': None, 'votes': [], 'role_states': [],
                'unanimous': None, 'reason': '正在准备后续研究提案；上一轮已完成评议保留为历史，不是新提案的投票。'}
    frozen = []
    innovation_step = store.get('steps', f'{run_id}:innovation') or {}
    innovation_id = (innovation_step.get('output') or {}).get('id') if innovation_step.get('cycle') == cycle else None
    for record in store.list('debate_inputs', run_id):
        identity = str(record.get('id', '')).split(':')
        context = record.get('context')
        if innovation_id and (not isinstance(context, dict)
                or context.get('innovation_selection', {}).get('session_id') != innovation_id):
            continue
        if (len(identity) < 4 or identity[0] != run_id or not identity[1].isdigit()
                or not identity[2].isdigit() or int(identity[1]) != cycle or int(identity[2]) < 1
                or not isinstance(context, dict) or context.get('research_cycle') != cycle
                or not isinstance(context.get('proposal'), dict)
                or context.get('proposal_hash') != identity[3] or _digest(context['proposal']) != identity[3]):
            continue
        frozen.append((int(identity[2]), record.get('created_at', ''), record['id'], record))
    if not frozen:
        return None
    round_number, _, _, record = max(frozen, key=lambda item: item[:3])
    context, proposal_hash = record['context'], record['context']['proposal_hash']
    committed_cycle = consensus.get('cycle', 1)
    committed_round = consensus.get('round', 0)
    committed_here = (committed_cycle == cycle and type(committed_round) is int and committed_round >= round_number
                      and (not innovation_id or consensus.get('innovation_session_id') == innovation_id))
    handoffs = store.list('handoffs', run_id)
    inputs = {}

    def trusted_input(handoff):
        identity = handoff['id']
        if identity not in inputs:
            inputs[identity] = store.get('handoff_inputs', identity)
        assignment = inputs[identity]
        if (not assignment or assignment.get('run_id') != run_id or assignment.get('id') != identity
                or not isinstance(assignment.get('context'), dict)):
            return None
        fingerprint = _digest({key: assignment.get(key) for key in ('instruction', 'context', 'schema')})
        expected_id = f"{run_id}:{handoff['purpose']}:{handoff['to_role']}:{fingerprint}"
        return assignment if handoff['id'] == expected_id and handoff.get('input_hash') == fingerprint else None

    if committed_here:
        # A completed round stays canonical. A subsequent revision has no new proposal to vote on yet.
        if committed_round != round_number or consensus.get('proposal_hash') != proposal_hash:
            return None
        revisions = []
        for handoff in handoffs:
            if handoff.get('purpose') != f'revise:{round_number}' or handoff.get('to_role') != 'director':
                continue
            assignment = trusted_input(handoff)
            # A new work delivery may refresh the resource policy. Its frozen
            # work record must prove ownership before these fields can differ.
            refreshed = False
            work = (assignment or {}).get('context', {}).get('discussion_work')
            if isinstance(work, dict):
                delivery = store.get('stage_contexts', work.get('work_id', ''), {})
                refreshed = (delivery.get('run_id') == run_id and delivery.get('round') == round_number
                             and delivery.get('proposal_hash') == proposal_hash and delivery.get('status') == 'completed'
                             and delivery.get('delivery') == work
                             and assignment['context'].get('execution_constraints') == delivery.get('effective_constraints', delivery.get('current_constraints'))
                             and assignment['context'].get('hardware') == delivery.get('hardware'))
            # Qualified bindings refresh with evidence for an unfrozen revision.
            # The full assignment fingerprint remains mandatory; vote matching
            # below still requires the exact frozen context without exclusions.
            ignore = {'source_tool_protocol', 'evidence', 'source_bindings'} | ({'discussion_work', 'execution_constraints', 'hardware'} if refreshed else set())
            if assignment and {k: v for k, v in assignment['context'].items()
                               if k not in ignore | {'votes', 'amendments'}} == {
                                   k: v for k, v in context.items() if k not in ignore}:
                revisions.append(handoff)
        if not revisions:
            return None
        revision = max(revisions, key=lambda item: item['updated_at'])
        status = revision['status']
        if status == 'running' and run['status'] != 'running':
            status = run['status']
        return {'derived': True, 'phase': 'preparing_proposal', 'status': status, 'cycle': cycle,
                'round': None, 'previous_round': round_number, 'next_round': round_number + 1,
                'proposal': None, 'proposal_hash': None, 'votes': [], 'role_states': [], 'unanimous': None,
                'source_id': revision['id'], 'reason': '上一轮评议已完成；正在准备下一版本提案，尚未开始新版本三方投票。'}
    decisions = {item['id']: item for item in store.list('decisions', run_id)}
    votes, role_states = [], []
    for role in ('director', 'engineer', 'scout'):
        matches = []
        for handoff in handoffs:
            if handoff.get('purpose') != f'vote:{round_number}' or handoff.get('to_role') != role:
                continue
            assignment = trusted_input(handoff)
            if assignment and assignment['context'] == context:
                matches.append(handoff)
        handoff = max(matches, key=lambda item: item['updated_at']) if matches else None
        decision = decisions.get(handoff['id'], {}) if handoff else {}
        result = decision.get('result') or {}
        valid = (isinstance(result, dict) and decision.get('role') == role
                 and decision.get('purpose') == f'vote:{round_number}' and result.get('proposal_hash') == proposal_hash
                 and type(result.get('approve')) is bool and isinstance(result.get('required_changes'), list)
                 and isinstance(result.get('rationale'), str))
        if valid:
            votes.append({'role': role, **{key: result[key] for key in ('approve', 'proposal_hash', 'rationale', 'required_changes')},
                          **({'fixes': result['fixes']} if 'fixes' in result else {}),
                          **({'problem_review': result['problem_review']} if isinstance(result.get('problem_review'), dict) else {})})
        status = 'completed' if valid else handoff['status'] if handoff else 'pending'
        if not valid and status == 'completed':
            status = 'pending'
        if status == 'running' and run['status'] != 'running':
            status = run['status']
        role_states.append({'role': role, 'status': status, 'has_vote': valid,
                            'handoff_id': handoff['id'] if handoff else None,
                            'error': handoff.get('error') if handoff else None})
    status = run['status'] if run['status'] in {'blocked', 'paused', 'failed', 'cancelled'} else 'awaiting_commit' if len(votes) == 3 else 'collecting'
    return redact({'derived': True, 'phase': 'voting', 'status': status, 'cycle': cycle, 'round': round_number,
                   'proposal': context['proposal'], 'proposal_hash': proposal_hash, 'votes': votes,
                   'role_states': role_states, 'received_votes': len(votes), 'expected_votes': 3,
                   'unanimous': None, 'source_id': record['id'], 'reason': '当前冻结提案的评议尚未提交完整共识；缺少投票不等于反对或同意。'})


def handoff_views(store, run_id):
    """Project a committed decision's citation audit without altering its handoff."""
    from .implementation_activity import records as activity_records
    rows = []
    # Keep the activity directory and its exact-key decisions in one read-only
    # transaction instead of opening two connections for every retained handoff.
    with store.read_snapshot() as snapshot:
        for handoff in sorted(activity_records(snapshot, 'handoffs', run_id), key=lambda row: row.get('created_at', '')):
            decision = snapshot.get('decisions', handoff['id'], {})
            audit = decision.get('citation_discard_audit')
            rows.append({**handoff, **({'citation_discard_audit': audit} if isinstance(audit, dict)
                and decision.get('role') == handoff.get('to_role') else {})})
    return redact(rows)


def _agent_history_records(store, run_id, role):
    """Build the sortable directory without decoding assignment or answer bodies."""
    from .implementation_activity import receipt_metadata, predicate
    activity = receipt_metadata(store, run_id)
    def metadata(collection, fields, *, where='', args=()):
        columns = ','.join(f"'{field}',json_extract(data,'$.{field}')" for field in fields)
        visible, visible_args = predicate(activity, collection)
        with store._read_db() as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT json_object('_record_key',key," + columns + ") FROM records "
                "WHERE collection=? AND json_extract(data,'$.run_id')=?" + where +
                ' AND ' + visible + ' ORDER BY updated_at,key', (collection, run_id, *args, *visible_args))]

    history = []
    handoffs = sorted(metadata('handoffs', ('id', 'purpose', 'status', 'created_at', 'updated_at', 'summary', 'error'),
        where=" AND json_extract(data,'$.to_role')=?", args=(role,)), key=lambda x: x['created_at'])
    decisions = {x['id']: x for x in metadata('decisions', ('id', 'created_at'),
        where=" AND json_extract(data,'$.role')=?", args=(role,))}
    inputs = {x['id']: x['_record_key'] for x in metadata('handoff_inputs', ('id',))}
    for handoff in handoffs:
        purpose = handoff['purpose']
        history.append({'id': handoff['id'] + ':assignment', 'at': handoff['created_at'], 'purpose': purpose,
                        'status': handoff['status'], 'kind': 'assignment',
                        '_input_key': inputs.get(handoff['id']), '_fallback_text': handoff.get('summary') or ''})
        decision = decisions.get(handoff['id'])
        if decision:
            history.append({'id': decision['id'] + ':output', 'at': decision['created_at'], 'purpose': purpose,
                            'status': 'completed', 'kind': 'output', '_decision_key': decision['_record_key']})
        elif handoff.get('error'):
            history.append({'id': handoff['id'] + ':error', 'at': handoff['updated_at'], 'purpose': purpose,
                            'status': handoff['status'], 'kind': 'tool', 'text': handoff['error']})
    for event in store.role_tool_events(run_id, role):
        history.append({'id': 'event:' + str(event['id']), 'at': event['created_at'], 'purpose': event['message'],
                        'status': event['kind'].split('.', 1)[1], 'kind': 'tool',
                        'tool_name': event['message'], 'caller_role': role, '_event_id': event['id']})
    history.sort(key=lambda x: (x['at'], x['id']))
    return history


def _render_agent_history(store, run_id, role, history, *, full):
    # Old tool payloads can be large; fetch bodies only for the requested page.
    events = {event['id']: event for event in store.role_tool_events(run_id, role,
              event_ids=[item['_event_id'] for item in history if '_event_id' in item])}
    for item in history:
        input_key = item.pop('_input_key', None)
        fallback = item.pop('_fallback_text', '')
        if item['kind'] == 'assignment':
            # Only the page's instruction is needed, never its frozen context.
            with store._read_db() as db:
                row = db.execute("SELECT json_type(data,'$.instruction'),json_extract(data,'$.instruction') "
                    "FROM records WHERE collection='handoff_inputs' AND key=?", (input_key,)).fetchone() if input_key is not None else None
            item['text'] = row[1] if row and row[0] is not None else fallback
        decision_key = item.pop('_decision_key', None)
        if decision_key is not None:
            with store._read_db() as db:
                row = db.execute("SELECT json_object('has_result',json_type(data,'$.result') IS NOT NULL,"
                    "'result_type',json_type(data,'$.result'),"
                    "'result',json_extract(data,'$.result'),"
                    "'citation_discard_audit',json_extract(data,'$.citation_discard_audit')) "
                    "FROM records WHERE collection='decisions' AND key=?", (decision_key,)).fetchone()
            decision = json.loads(row[0]) if row else {}
            result = decision.get('result') if decision.get('has_result') else {}
            if decision.get('result_type') in ('true', 'false'):
                result = decision['result_type'] == 'true'
            item.update(text=public_text(result), consensus=_review_consensus(result))
            if isinstance(decision.get('citation_discard_audit'), dict):
                item['citation_discard_audit'] = decision['citation_discard_audit']
        event_id = item.pop('_event_id', None)
        if event_id is not None:
            event = events[event_id]
            for field in ('tool_call_id', 'handoff_id'):
                if event['data'].get(field):
                    item[field] = event['data'][field]
            item['text'] = public_text({'tool': event['message'], 'event': event['kind'], **event['data']})
        item['text'] = redact(item['text'])
        item['truncated'] = not full and len(item['text']) > 3000
        if not full:
            item['text'] = item['text'][:3000]
    return redact(history)


def agent_history(store, run_id, role, *, full=False):
    history = _agent_history_records(store, run_id, role)
    total = len(history)
    history = _render_agent_history(store, run_id, role, history[-1000:] if full else history[-30:], full=full)
    return {'role': role, 'history': history, 'total': total, 'returned': len(history), 'has_older': total > len(history)}


def _history_cursor(run_id, role, before):
    payload = {'v': 1, 'run_id': run_id, 'role': role, 'before': list(before)}
    return base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=True, separators=(',', ':')).encode()).decode().rstrip('=')


def _history_boundary(cursor, run_id, role):
    try:
        if not isinstance(cursor, str) or not cursor or len(cursor) > 4096:
            raise ValueError
        payload = json.loads(base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True))
        if (not isinstance(payload, dict) or set(payload) != {'v', 'run_id', 'role', 'before'}
                or type(payload['v']) is not int or payload['v'] != 1
                or payload['run_id'] != run_id or payload['role'] != role
                or not isinstance(payload['before'], list) or len(payload['before']) != 2
                or any(not isinstance(item, str) or not item for item in payload['before'])):
            raise ValueError
        return tuple(payload['before'])
    except (ValueError, TypeError, UnicodeError, binascii.Error, RecursionError) as exc:
        raise ValueError('Invalid role history cursor for this task and role') from exc


def agent_history_page(store, run_id, role, *, limit=100, cursor=None):
    """Newest eligible records, ascending within each exclusive keyset page.

    The cursor binds the task, role and version to (at,id), so newer inserts do
    not shift older pages. `total` is the current complete role-history count.
    """
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('History limit must be an integer between 1 and 200')
    boundary = _history_boundary(cursor, run_id, role) if cursor is not None else None
    history = _agent_history_records(store, run_id, role)
    total = len(history)
    eligible = [item for item in history if boundary is None or (item['at'], item['id']) < boundary]
    page = eligible[-limit:]
    has_older = len(eligible) > len(page)
    next_cursor = _history_cursor(run_id, role, (page[0]['at'], page[0]['id'])) if has_older else None
    page = _render_agent_history(store, run_id, role, page, full=True)
    return {'role': role, 'history': page, 'total': total, 'returned': len(page),
            'has_older': has_older, 'next_cursor': next_cursor}


def execution_activity(run, trials):
    """Describe persisted experiment execution separately from model assignments."""
    running = [trial for trial in trials if trial.get('status') == 'running' and not trial.get('superseded')]
    if running:
        active = run['status'] == 'running'
        return {'kind': 'experiment', 'status': 'running' if active else 'awaiting_confirmation',
                'action': ('实验执行器正在运行：' if active else '任务已停止推进；以下试验的结束状态待执行器确认：')
                          + '、'.join(str(trial['id']) for trial in running),
                'trials': [{'id': trial['id'], 'phase': trial.get('params', {}).get('phase'),
                            'variant': trial.get('params', {}).get('variant'), 'seed': trial.get('params', {}).get('seed'),
                            'execution_mode': trial.get('execution_mode'), 'started_at': trial.get('started_at'),
                            'recorded_status': trial['status'], 'source_id': trial.get('storage_key', trial['id'])}
                           for trial in running], 'source': 'persisted_experiment_records'}
    if run['status'] == 'running' and run['stage'] in {'experiments', 'tuning', 'final_tests'}:
        return {'kind': 'experiment', 'status': 'preparing', 'action': '实验阶段尚未完成；当前没有记录为运行中的试验。',
                'trials': [], 'source': 'persisted_experiment_records'}
    return None


def codex_execution_activity(run, workflows, cycle):
    """Project the native executor without assigning its work to model roles."""
    if run['stage'] not in {'implementation', 'experiments', 'tuning', 'final_tests'}:
        return None
    candidates = [row for row in workflows if row.get('run_id') == run['id']
                  and row.get('cycle') == cycle and not row.get('superseded')
                  and (not run.get('state', {}).get('implementation_generation')
                       or row.get('implementation_generation') == run['state']['implementation_generation'])
                  and (row.get('kind') == 'runtime_repair' or run['stage'] == 'implementation')]
    state = max(candidates, key=lambda row: (row.get('updated_at', ''), row.get('id', '')), default=None)
    if not state or state.get('status') not in {'implementing', 'acceptance', 'code_review', 'blocked'}:
        return None
    attempt = next((row for row in reversed(state.get('attempts', []))
                    if row.get('index') == state.get('current_revision')), {})
    checkpoint = attempt.get('checkpoint') or {}
    phase = state['status']
    active = run['status'] == 'running'
    status = 'preparing'
    if not active or phase == 'blocked' or checkpoint.get('status') in {'unknown', 'blocked', 'failed', 'interrupted'}:
        status = 'awaiting_confirmation'
        action = '本机 Codex 的实现流程已停止推进；请在代码实现与验收页核对原请求、恢复记录与缺项。'
    elif phase == 'acceptance':
        status, action = 'running', '本机代码验收正在运行；检查实际文件与本地执行结果，尚未通过三方代码评审。'
    elif phase == 'code_review':
        action = '本机 Codex 已返回产物，正在等待三方对同一代码版本完成独立评审。'
    elif checkpoint.get('status') == 'running':
        status, action = 'running', '本机 Codex 正在独立目录实现代码；尚未完成产物核验与三方代码评审。'
    elif checkpoint.get('status') == 'completed':
        action = '本机 Codex 请求已完成，正在等待核验磁盘产物；完成回复不代表验收通过。'
    else:
        action = '正在准备或派发本机 Codex 实现请求；尚无正在执行的确定回执。'
    activities = checkpoint.get('activities') or []
    last_activity = activities[-1] if activities and isinstance(activities[-1], dict) else {}
    return {'kind': 'local_codex', 'status': status, 'action': action,
            'source': 'persisted_codex_workflow', 'workflow_id': state['id'], 'phase': phase,
            'workspace': checkpoint.get('workspace') or state.get('workspace'),
            'thread_id': checkpoint.get('thread_id'), 'turn_id': checkpoint.get('turn_id'),
            'request_status': checkpoint.get('status'), 'last_event_at': checkpoint.get('last_event_at'),
            'latest_activity_type': last_activity.get('type'), 'updated_at': state.get('updated_at')}


def research_map(engine, run_id, *, orchestration=None):
    from .consensus_status import view as consensus_status_view
    from .run_views import agent_assignment_summary
    store = engine.store
    run = store.get('runs', run_id)
    cycle = engine.cycle(run_id)
    steps = {s['stage']: s for s in store.list('steps', run_id)}
    agents = [dict(agent) for agent in (orchestration or engine.orchestration(run_id))['agents']]
    pending_debate = current_debate(engine, run_id)
    from .implementation_activity import records as activity_records
    handoffs = activity_records(store, 'handoffs', run_id)
    events = store.recent_events(run_id, limit=5000)
    trials = store.list('experiments', run_id)
    from .dataset_codex_view import execution_view
    execution = (execution_view(store, run)
                 or codex_execution_activity(run, store.list('codex_workflows', run_id), cycle)
                 or execution_activity(run, trials))
    for agent in agents:
        role = agent['role']
        # Share exact assignment/output identity with the light first-screen
        # projection. Role-wide last output/tool events can belong to another
        # parallel assignment and must not be presented as this task's work.
        agent.update(agent_assignment_summary(store, run, role))
        view = agent_history(store, run_id, role)
        agent.update(history=view['history'], history_total=view['total'], history_url=f'/api/runs/{run_id}/agents/{role}/history')
    mainline = []
    validation_review = (steps.get('validation_review', {}).get('output') or {})
    for stage in STAGES:
        if stage == 'revision':
            continue
        if stage == 'related_work' and run.get('config', {}).get('literature_protocol') != 2:
            continue
        step = steps.get(stage)
        status = 'passed' if step and step['status'] == 'completed' else 'pending'
        is_current = stage == run['stage']
        if is_current and run['status'] == 'running':
            status = 'active'
        reason = run.get('error') if is_current and run['status'] in {'blocked', 'failed'} else None
        if stage == 'final_tests' and validation_review.get('decision') == 'stop' and not step:
            status = 'skipped'
            reason = '验证未达标，按预先声明的停止规则结束本方法族；未执行独立测试。'
        if is_current and run['status'] == 'paused':
            reason = '研究已暂停，恢复后从当前检查点继续。'
        mainline.append({'id': stage, 'label': LABELS[stage], 'status': status, 'cycle': cycle,
                         'is_current': is_current, 'activity_status': run['status'] if is_current else None,
                         'reason': reason})
    consensus = store.get('consensus', run_id)
    rounds = list(store.list('proposal_reviews', run_id))
    for step in store.list('cycle_steps', run_id):
        if step['stage'] == 'debate' and step.get('output'):
            rounds.extend({**r, 'cycle': step.get('cycle', 1)} for r in step['output'].get('history', []))
    if consensus:
        rounds.extend(consensus.get('history') or [consensus])
    branches = []
    seen = set()
    for record in rounds:
        number = record.get('cycle', 1)
        verdict = json.dumps({'votes': record.get('votes'), 'unanimous': record.get('unanimous')}, sort_keys=True)
        key = f'proposal:{number}:{record.get("round", 1)}:{record.get("proposal_hash", "")}:{verdict}'
        if key in seen:
            continue
        seen.add(key)
        passed = record.get('unanimous') is True
        reasons = [v.get('rationale', '') for v in record.get('votes', []) if not v.get('approve') or v.get('required_changes')]
        reasons.extend(record.get('resource_assessment', {}).get('reasons', []))
        missing = record.get('evidence_assessment', {}).get('unverified_reference_ids', [])
        if missing:
            reasons.append('未核验引用：' + ', '.join(missing))
        branches.append({'id': record.get('review_id') or 'proposal:' + str(len(branches)), 'parent_id': 'debate', 'cycle': number, 'kind': 'proposal',
                         'label': record.get('proposal', {}).get('title', '研究提案'),
                         'status': 'passed' if passed else 'rejected', 'reason': '三位 Agent 同意本版本立项；效果仍待实验验证' if passed else '；'.join(map(str, reasons)) or '尚未满足同版本三票及资源门禁',
                         'source_id': record.get('proposal_hash')})
    for record in store.list('research_cycles', run_id):
        if record.get('review_type') == 'implementation_specification':
            branches.append({'id': record['id'], 'parent_id': 'implementation', 'cycle': record['cycle'],
                             'kind': 'implementation', 'label': '实现规范审查待处理', 'status': 'pending',
                             'reason': record['rationale'], 'source_id': record['id']})
            continue
        branches.append({'id': record['id'], 'parent_id': 'validation_review', 'cycle': record['cycle'], 'kind': 'validation',
                         'label': f'第 {record["cycle"]} 轮验证', 'status': 'passed' if record['decision'] == 'finalize' else 'rejected',
                         'reason': record['rationale'], 'source_id': record['revision_hash']})
    if run['stage'] == 'revision':
        branches.append({'id': f'active-revision:{cycle}', 'parent_id': 'validation_review', 'cycle': cycle,
                         'kind': 'revision', 'label': '正在修订方案，下一步重新三方立项', 'status': 'pending',
                         'reason': '根据本轮真实验证结果和异议调整，修订后还需重新投票', 'source_id': f'{run_id}:{cycle}'})
    active_trials = [x for x in trials if not x.get('superseded')]
    selection = store.get('selections', run_id, {})
    for trial in active_trials:
        params = trial.get('params', {})
        if params.get('phase') != 'validation':
            continue
        selected = trial['id'] == selection.get('selected_trial_id')
        status = 'passed' if selected else 'rejected' if trial['status'] == 'failed' else 'pending'
        branches.append({'id': 'trial:' + trial['id'], 'parent_id': 'tuning', 'cycle': trial.get('research_cycle', 1),
                         'kind': 'experiment', 'label': f'{params.get("variant", "实验")} · {trial["id"]}', 'status': status,
                         'reason': ('验证集选中的参数；验证未达标，按预定规则停止，未执行独立测试' if validation_review.get('decision') == 'stop' else '验证集选中的参数，等待独立最终测试') if selected else '执行失败：' + str(trial.get('error', '')) if status == 'rejected' else '已测量，保留为基线/消融/候选证据；不等于方案被否定' if trial['status'] == 'completed' else '实验待完成',
                         'source_id': trial['id']})
    if pending_debate:
        voting = pending_debate['phase'] == 'voting'
        branches.append({'id': 'current-debate:' + str(pending_debate.get('source_id', cycle)),
                         'parent_id': 'debate' if run['stage'] == 'debate' else 'validation_review',
                         'cycle': cycle, 'kind': 'partial_review' if voting else 'preparing_proposal',
                         'label': f"第 {pending_debate['round']} 轮 · " + pending_debate['proposal'].get('title', '当前提案') if voting else '下一版本提案准备中',
                         'status': 'pending', 'reason': pending_debate['reason'] +
                         (f" 已记录 {pending_debate['received_votes']} / 3 票；状态：{pending_debate['status']}。" if voting else ''),
                         'source_id': pending_debate.get('source_id')})
    if not branches:
        branches.append({'id': 'awaiting-proposal', 'parent_id': 'debate', 'cycle': cycle, 'kind': 'proposal',
                         'label': '研究方案待形成和三方评议', 'status': 'pending', 'reason': '暂无立项或实验结论', 'source_id': None})
    review = store.get('research_cycles', f'{run_id}:{cycle}', validation_review)
    next_stage = ({'iterate': 'revision', 'stop': 'report'}.get(review.get('decision'), NEXT.get(run['stage'], 'intake'))
                  if run['stage'] == 'validation_review' else NEXT.get(run['stage'], 'intake'))
    if run['status'] == 'blocked':
        next_action = '处理阻塞后从当前检查点继续：' + (run.get('error') or '查看最近一条事件')
    elif run['status'] == 'failed':
        next_action = '处理失败原因后恢复当前检查点：' + (run.get('error') or '查看失败事件与阶段报告')
    elif run['status'] == 'cancelled':
        next_action = '研究已取消；查看保留记录与复现包，如需继续研究请新建任务'
    elif run['status'] == 'paused':
        next_action = '恢复后继续：' + LABELS.get(run['stage'], run['stage'])
    elif run['status'] == 'completed':
        next_action = '查看报告、负结果和复现包；流程完成不代表发表达标'
    elif run['status'] == 'draft':
        next_action = '启动研究，先解析用户目标与资源'
    elif execution and execution['kind'] == 'local_codex':
        next_action = ('等待三方对同一代码版本完成评审，再推进实验。' if execution['phase'] == 'code_review' else
                       '先核对本机 Codex 原请求与实现缺项；未验收前不推进实验。' if execution['status'] == 'awaiting_confirmation' else
                       '等待本机 Codex 实现与本地验收，再由三方评审实际代码。')
    elif execution and execution['status'] == 'running':
        next_action = '等待当前试验完成并核验输出，再继续：' + LABELS.get(next_stage, next_stage)
    elif execution and execution['status'] == 'preparing':
        next_action = '完成本阶段实验与结果核验后继续：' + LABELS.get(next_stage, next_stage)
    else:
        next_action = LABELS.get(next_stage, next_stage)
    from .review_context_status import CONTEXT_ERROR, context_reference_status
    reference_check = context_reference_status(store, run_id)
    if reference_check and reference_check['status'] == 'ready':
        next_action = '已保存回复的上下文引用已通过复核；恢复后继续检查其他执行条件。'
        if run['config'].get('innovation_protocol') == 1 and not steps.get('innovation'):
            next_action = '上下文引用已通过复核；恢复后先由三位角色独立提出创新点，比较收束后重新进入共识。'
        for agent in agents:
            if agent.get('current_action') == CONTEXT_ERROR:
                agent['current_action'] = '已保存回复的上下文引用已通过复核，等待恢复。'
        for node in mainline:
            if node.get('reason') == CONTEXT_ERROR:
                node['reason'] = '原停止记录已通过当前引用复核，等待恢复；其他门禁仍需检查。'
    links = [{'id': h['id'], 'from_role': h['from_role'], 'to_role': h['to_role'], 'purpose': h['purpose'],
              'status': h['status'], 'active': h['status'] == 'running' and run['status'] == 'running',
              'at': h['updated_at']} for h in sorted(handoffs, key=lambda x: x['updated_at'])[-30:]]
    final_output = steps.get('final_tests', {}).get('output') or {}
    outcome = final_output.get('outcome') if isinstance(final_output, dict) else None
    if not outcome and review.get('decision') == 'stop':
        outcome = review.get('outcome')
    return redact({'current_stage': run['stage'], 'current_stage_label': LABELS.get(run['stage'], run['stage']),
                   'current_cycle': cycle, 'progress': run['progress'], 'progress_scope': '当前流程阶段进度，非研究达标概率',
                   'run_status': run['status'], 'run_error': run.get('error'), 'current_execution': execution,
                   'next_action': next_action, 'mainline': mainline, 'branches': branches, 'agents': agents, 'links': links,
                   'current_debate': pending_debate, 'consensus_activity': consensus_status_view(engine, run_id, pending_debate), 'context_reference_check': reference_check,
                   **({'outcome': outcome} if isinstance(outcome, dict) else {})})
