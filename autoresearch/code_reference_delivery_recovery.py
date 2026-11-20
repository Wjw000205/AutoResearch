"""Return an invalid correction payload without relaxing its original scope.

The bad received answer and native journal are retained separately. They never
replace the original opinion or become an evidence/approval anchor.
"""
from __future__ import annotations

import copy

from .engine import ResearchBlocked, Harness, digest
from . import code_evidence_reference_correction as references

FIELD = 'code_reference_delivery_problems'
HISTORY = 'alignment_candidate_history'


class DeliveryRejected(ResearchBlocked):
    def __init__(self, message, result, anchor):
        super().__init__(message)
        original = anchor['candidate']
        old, new = original.get('code_evidence'), result.get('code_evidence')
        self.problems = [{'kind': 'correction_payload_drift',
            'expected_evidence_count': len(old) if isinstance(old, list) else None,
            'received_evidence_count': len(new) if isinstance(new, list) else None,
            'changed_locked_fields': sorted(k for k in original.keys() | result.keys()
                if k != 'code_evidence' and original.get(k) != result.get(k))}]


def _fail(message):
    raise ResearchBlocked('代码引用答复补正：' + message)


def _run_binding(engine, run_id):
    run = engine.store.get('runs', run_id, {})
    return {'status': run.get('status'), 'owner_instance': run.get('owner_instance'),
            'resume_generation': run.get('state', {}).get('resume_generation')}


def _assignment(engine, run_id, role, purpose, key, context):
    frozen = engine.store.get('handoff_inputs', key, {})
    assignment = engine.store.get('handoffs', key, {})
    if not isinstance(frozen, dict) or not isinstance(assignment, dict) or not isinstance(context, dict):
        _fail('原交接或冻结上下文格式损坏。')
    payload = {name: frozen.get(name) for name in ('instruction', 'context', 'schema')}
    if (purpose.split(':', 1)[0] != 'code_review' or role not in {'director', 'engineer', 'scout'}
            or frozen.get('id') != key or frozen.get('run_id') != run_id
            or frozen.get('context') != context or assignment.get('id') != key
            or assignment.get('run_id') != run_id or assignment.get('to_role') != role
            or assignment.get('purpose') != purpose or assignment.get('input_hash') != digest(payload)
            or Harness.decision_key(run_id, role, purpose, **payload) != key
            or engine.store.get('decisions', key)):
        _fail('原交接、角色或冻结输入已改变，或决定已封存。')


def anchor_for(history, entry=None):
    if not isinstance(history, list):
        _fail('原补正历史格式损坏。')
    if entry is None:
        return next((row for row in reversed(history) if isinstance(row, dict) and row.get(references.FIELD)), None)
    candidates = [row for row in history if isinstance(row, dict)
                  and row.get('attempt') == entry.get('code_reference_anchor_attempt') and row.get(references.FIELD)]
    if (len(candidates) != 1 or digest(candidates[0]) != entry.get('code_reference_anchor_sha256')
            or type(entry.get('attempt')) is not int or candidates[0]['attempt'] >= entry['attempt']):
        _fail('被拒答复的原意见锚点已改变。')
    return candidates[0]


def verify_archive(engine, run_id, role, purpose, key, context, progress, entry):
    from .implementation_alignment_recovery import _verify_format_source
    _assignment(engine, run_id, role, purpose, key, context)
    _verify_format_source(entry, role, purpose, problem_field=FIELD)
    if (not entry.get('source_requests')
            or digest(entry['source_requests']) != entry.get('delivery_source_requests_sha256')
            or digest(context) != entry.get('delivery_context_sha256')):
        _fail('被拒答复的原请求或上下文已改变。')
    anchor = anchor_for(progress.get(HISTORY), entry)
    references.verify_archive(engine, run_id, role, purpose, key, context, anchor)
    try:
        references._legacy_validate_correction(entry['candidate'], anchor)
    except DeliveryRejected as exc:
        if exc.problems != entry[FIELD] or str(exc) != entry.get('diagnostic'):
            _fail('补正差异与已归档诊断不一致。')
    else:
        _fail('被拒答复的原错误已不能复现。')


def plan(engine, run_id, role, purpose, key, result, anchor):
    """Authenticate one received reply; no writes or retry of an uncertain call."""
    from .implementation_alignment_recovery import _verify_format_source
    frozen = engine.store.get('handoff_inputs', key, {})
    if not isinstance(frozen, dict) or not isinstance(frozen.get('context'), dict):
        _fail('原冻结上下文格式损坏。')
    context = frozen['context']
    _assignment(engine, run_id, role, purpose, key, context)
    progress = engine.store.get('handoff_progress', key, {})
    if (not isinstance(progress, dict) or progress.get('id') != key or progress.get('run_id') != run_id
            or type(progress.get('turn')) is not int or progress['turn'] < 0
            or progress.get('pending_tool_calls')):
        _fail('检查点身份或待处理工具状态不一致。')
    history = progress.get(HISTORY)
    current_anchor = anchor_for(history)
    if not current_anchor or current_anchor != anchor:
        _fail('当前原意见锚点已改变。')
    references.verify_archive(engine, run_id, role, purpose, key, context, anchor)
    for prior in history:
        if prior.get(FIELD):
            verify_archive(engine, run_id, role, purpose, key, context, progress, prior)
    try:
        references._legacy_validate_correction(result, anchor)
    except DeliveryRejected as exc:
        message, problems = str(exc), exc.problems
    else:
        _fail('仅可退回已诊断的不合规补正答复。')
    sources = {name: copy.deepcopy(progress[name]) for name in ('normal_request', 'finalization') if name in progress}
    if not sources and history and history[-1].get(FIELD):
        previous = history[-1]
        if (previous['candidate_sha256'] == digest(result) and previous['next_turn'] == progress['turn']
                and previous['source_turn'] + 1 == progress['turn']):
            return {'reused': True, 'baseline': progress, 'run_binding': _run_binding(engine, run_id)}
    attempt, turn = len(history) + 1, progress['turn']
    entry = {'attempt': attempt, 'candidate': copy.deepcopy(result), 'candidate_sha256': digest(result),
        'source_requests': sources, 'source_mode': progress.get('mode'), 'source_turn': turn,
        'next_turn': turn + 1, 'diagnostic': message, FIELD: problems,
        'code_reference_anchor_attempt': anchor['attempt'], 'code_reference_anchor_sha256': digest(anchor),
        'delivery_source_requests_sha256': digest(sources), 'delivery_context_sha256': digest(context)}
    _verify_format_source(entry, role, purpose, problem_field=FIELD)
    if not sources:
        _fail('未确认收到原请求答复，不能补正重发。')
    verify_archive(engine, run_id, role, purpose, key, context, {**progress, HISTORY: [*history, entry]}, entry)
    diagnostic = references.diagnostic(anchor['candidate'], attempt, anchor[references.FIELD], error=message)
    correction = diagnostic['code_reference_correction']
    correction['delivery_rejection'] = {'received_candidate_sha256': digest(result), 'problems': copy.deepcopy(problems)}
    correction['instruction'] = ('历史补正答复和原意见均已留档。当前接收规则允许重排、增删证据及修订未封存意见，'
        '不再要求恢复原文和固定条数。请修复实际引用并回应原问题，返回完整评审。' + correction['instruction'])
    return {'reused': False, 'baseline': progress, 'entry': entry, 'diagnostic': diagnostic,
            'run_binding': _run_binding(engine, run_id)}


async def schedule(h, run_id, role, purpose, key, result, anchor):
    engine = h.engine
    owner_task = engine.tasks.get(run_id)
    prepared = await engine._read_consensus_check(run_id, plan, engine, run_id, role, purpose, key, result, anchor)
    def check_owner():
        engine.check_active(run_id)
        if (_run_binding(engine, run_id) != prepared['run_binding']
                or prepared['run_binding']['owner_instance'] not in (None, engine.instance_id)
                or engine.tasks.get(run_id) is not owner_task):
            _fail('任务所有者或恢复代次已改变，未领取补正。')
    check_owner()
    if prepared['reused']:
        return True
    from .storage import now
    baseline = prepared['baseline']
    progress = copy.deepcopy(baseline)
    entry = {**prepared['entry'], 'created_at': now()}
    progress[HISTORY] = [*progress[HISTORY], entry]
    progress.setdefault('tool_results', []).append(prepared['diagnostic'])
    progress.update(turn=entry['next_turn'], pending_tool_calls=[], tool_cursor=0, tool_counted=False)
    if baseline.get('mode') == 'final_only' or baseline['turn'] >= h._tool_turn_limit(purpose,
            engine.store.get('handoff_inputs', key)['context'], baseline):
        progress['alignment_read_extension_until'] = max(progress.get('alignment_read_extension_until', 0), entry['next_turn'] + 4)
    for name in ('normal_request', 'finalization', 'mode'):
        progress.pop(name, None)
    def claim(current):
        check_owner()
        if current != baseline:
            _fail('检查点已改变，未覆盖或重复领取补正。')
        return progress
    engine.store.mutate('handoff_progress', key, claim)
    engine.store.event(run_id, 'handoff.returned_for_correction',
        '补正答复超出原范围，已退回原角色恢复完整意见和证据；原答复、读取与其他角色决定保留。',
        {'handoff_id': key, 'role': role, 'category': 'code_reference_delivery',
         'attempt': entry['attempt'], 'approval_granted': False})
    return True


def recoverable_blocked_run(engine, run):
    if (run.get('status') != 'blocked' or run.get('stage') != 'implementation'
            or run.get('error') not in references.PAYLOAD_ERRORS):
        return False
    run_id = run['id']
    keys = engine.store.record_keys('handoffs', run_id,
        predicate="json_extract(data,'$.purpose') LIKE 'code_review:%' "
                  "AND json_extract(data,'$.status') IN ('blocked','interrupted','running')")
    for key in keys:
        try:
            assignment = engine.store.get('handoffs', key, {})
            progress = engine.store.get('handoff_progress', key, {})
            if not isinstance(assignment, dict) or not isinstance(progress, dict):
                continue
            anchor = anchor_for(progress.get(HISTORY))
            if not anchor:
                continue
            source = progress.get('finalization' if progress.get('mode') == 'final_only' else 'normal_request')
            if not isinstance(source, dict) or not isinstance(source.get('response'), dict):
                continue
            candidate = source['response'].get('json')
            if not isinstance(candidate, dict):
                continue
            pending = plan(engine, run_id, assignment['to_role'], assignment['purpose'], key, candidate, anchor)
            if not pending['reused'] and pending['entry']['diagnostic'] == run['error']:
                return True
        except (ResearchBlocked, KeyError, ValueError, TypeError, OSError):
            continue
    return False
