"""One journaled final-only author declaration correction after a real save.

No tool budget is extended. The received paid answer is archived, not resent;
only its diagnosed declaration fields may change. Reading and independent
review still use the ordinary immutable evidence gates.
"""
from __future__ import annotations

import copy

from . import implementation_author_response_correction as correction
from . import implementation_authoring as author
from .implementation_author_recovery_scope import SCOPE_VERSION, workflow_fingerprint
from .storage import now, uid

KEY = correction.KEY
ORIGIN_KEY = 'implementation_author_origin_correction'


def _kind(progress):
    kinds = [name for name in (KEY, ORIGIN_KEY) if progress.get(name) is not None]
    if len(kinds) > 1:
        _fail('不同类型的作者补正不能合并或互相覆盖。')
    return kinds[0] if kinds else None


def _protocol(kind):
    if kind == KEY:
        return correction
    if kind == ORIGIN_KEY:
        from . import implementation_author_origin_correction
        return implementation_author_origin_correction
    _fail('未知作者补正类型。')


def _fail(message):
    from .engine import ResearchBlocked
    raise ResearchBlocked('实现作者回复补正恢复：' + message)


def _scope(h, run_id, role, purpose, key, progress, *, correction_kind=KEY):
    from .engine import digest
    engine = h.engine
    inputs = engine.store.get('handoff_inputs', key, {})
    context = inputs.get('context', {})
    assignment = engine.store.get('handoffs', key, {})
    record = engine.store.get(author.COLLECTION, key, {})
    binding = context.get('implementation_document_authoring', {})
    state = engine.store.get('codex_workflows', binding.get('workflow_id', ''), {})
    payload = {field: inputs.get(field) for field in ('instruction', 'context', 'schema')}
    run = engine.store.get('runs', run_id, {})
    from .implementation_author_read_recovery import KEY as READ_KEY
    received_read_final = bool(progress.get(READ_KEY) and progress.get('mode') != 'final_only'
        and progress.get('normal_request', {}).get('status') == 'response_saved'
        and isinstance(progress.get('normal_request', {}).get('response', {}).get('json'), dict)
        and 'tool_calls' not in progress['normal_request']['response']['json'])
    received_origin_final = bool(correction_kind == ORIGIN_KEY
        and progress.get('mode') != 'final_only' and not progress.get('finalization')
        and progress.get('normal_request', {}).get('status') == 'response_saved'
        and isinstance(progress.get('normal_request', {}).get('response', {}).get('json'), dict)
        and 'tool_calls' not in progress['normal_request']['response']['json'])
    if (role != 'engineer' or not purpose.startswith('implementation_spec:')
            or progress.get('id') != key or progress.get('run_id') != run_id
            or (progress.get('mode') != 'final_only' and not received_read_final and not received_origin_final)
            or progress.get('pending_tool_calls')
            or inputs.get('id') != key or inputs.get('run_id') != run_id
            or assignment.get('id') != key or assignment.get('run_id') != run_id
            or assignment.get('to_role') != role or assignment.get('purpose') != purpose
            or assignment.get('input_hash') != digest(payload)
            or h.decision_key(run_id, role, purpose, **payload) != key
            or run.get('status') != 'running' or run.get('stage') != 'implementation'
            or engine.store.get('decisions', key) or not record or record.get('status') != 'draft'
            or record.get('workflow_id') != state.get('id')
            or state.get('run_id') != run_id or state.get('status') != 'specification'
            or state.get('current_revision') != binding.get('revision')
            or record.get('revision') != binding.get('revision')
            or record.get('row', {}).get('index') != binding.get('revision')
            or record.get('context') != context):
        _fail('原工程师、冻结输入、实际草稿或当前版本已改变，未派发新请求。')
    if engine.store.record_keys('tool_invocations', run_id,
            predicate="json_extract(data,'$.handoff_id')=? AND json_extract(data,'$.status') IN ('pending','running','unknown')",
            parameters=[key]):
        _fail('存在未核对工具结果，不能补正或重复执行。')
    return {'scope_version': SCOPE_VERSION, 'input_sha256': digest(inputs), 'draft_sha256': digest(record),
        'configuration_sha256': digest(run.get('config')),
        'workflow_sha256': workflow_fingerprint(state), 'normal_request_sha256': digest(progress.get('normal_request')),
        'tool_results_sha256': digest(progress.get('tool_results')), 'turn': progress.get('turn'),
        'save_resume_sha256': digest(progress.get('implementation_document_save_resume'))}


def _source(h, run_id, key, audit):
    from .engine import digest
    from .execution_archive import load
    from .implementation_save_resume import _settled_response
    try:
        payload = load(h.engine.store, run_id, 'engineer', audit['source_archive_id'], audit['source_archive_sha256'])
    except (KeyError, ValueError, OSError) as exc:
        _fail('原答复归档缺失、过期或哈希不匹配：' + str(exc))
    source_kind = audit.get('source_kind', 'finalization')
    if source_kind not in {'normal_request', 'finalization'}:
        _fail('原答复请求类型不一致。')
    final = payload.get(source_kind, {})
    if final.get('provider_id') is None and h.engine.store.get('runs', run_id, {}).get('mode') != 'demo':
        _fail('在线研究不能用测试标签作为原付费回执。')
    if (digest(final) != audit.get('source_finalization_sha256')
            or final.get('status') != 'response_saved' or not final.get('operation_id')
            or not _settled_response(h.engine.store, run_id, final)
            or digest(final.get('response', {}).get('json')) != audit.get('candidate_sha256')):
        _fail('原答复未确认完整结算或实际候选已改变，未重发原请求。')
    return final


def _checked(h, run_id, role, purpose, key, progress, *, rederive=False):
    from .engine import digest
    kind = _kind(progress)
    if kind is None:
        return None
    audit = progress[kind]
    protocol = _protocol(kind)
    if (not isinstance(audit, dict) or type(audit.get('version')) is not int or audit['version'] != 1
            or type(audit.get('scope_version')) is not int or audit['scope_version'] != SCOPE_VERSION
            or not isinstance(audit.get('scope'), dict)
            or type(audit['scope'].get('scope_version')) is not int or audit['scope']['scope_version'] != SCOPE_VERSION
            or audit.get('run_id') != run_id or audit.get('handoff_id') != key or audit.get('role') != role
            or audit.get('purpose') != purpose or type(audit.get('attempt')) is not int or audit['attempt'] != 1
            or audit.get('scientific_approval') is not False or audit.get('tools_allowed') is not False
            or audit.get('correction_kind', KEY) != kind
            or audit.get('sha256') != digest({k: v for k, v in audit.items() if k != 'sha256'})
            or _scope(h, run_id, role, purpose, key, progress, correction_kind=kind) != audit.get('scope')):
        _fail('补正范围、草稿、原证据或预算状态已改变。')
    active = progress.get('finalization', {})
    if (active.get('operation_id') != audit.get('operation_id')
            or digest(active.get('prepared')) != audit.get('prepared_sha256')
            or active.get('author_response_correction_of') != audit.get('source_operation_id')
            or active.get('withheld_tool_calls') or active.get('completed_result_hashes') != [
                digest(item) for item in progress.get('tool_results', [])]):
        _fail('补正请求被替换或添加了工具调用。')
    final = _source(h, run_id, key, audit)
    if rederive:
        context = h.engine.store.get('handoff_inputs', key)['context']
        if protocol.problems(h.engine, run_id, role, key, final['response']['json'], context) != audit['problems']:
            _fail('原异议诊断或实际原文读取账本已改变。')
    return audit, final


def validate_dispatch(h, run_id, key, progress):
    assignment = h.engine.store.get('handoffs', key, {})
    checked = _checked(h, run_id, assignment.get('to_role'), assignment.get('purpose', ''), key, progress,
                       rederive=True)
    if checked is None:
        return
    audit, original = checked
    identity = original.get('provider_id'), original.get('model')
    if hasattr(h.intelligence, 'provider_for'):
        provider = h.intelligence.provider_for('engineer')
        if identity != (provider.get('id'), provider.get('model')):
            _fail('补正必须沿用原工程师供应方和模型。')


def validate_preview(h, run_id, role, purpose, key, progress):
    """Cheap immutable scope guard before reuse/preview; dispatch rechecks reads."""
    _checked(h, run_id, role, purpose, key, progress)


def validate_candidate(h, run_id, role, purpose, key, result):
    progress = h.engine.store.get('handoff_progress', key, {})
    checked = _checked(h, run_id, role, purpose, key, progress, rederive=True)
    if checked is not None:
        audit, original = checked
        _protocol(audit.get('correction_kind', KEY)).validate_correction(
            result, original['response']['json'], audit['problems'])


def prepare_origin_schedule(h, run_id, role, purpose, key, result, problems=None):
    """Read-only eligibility proof; commits recheck its immutable scope."""
    from .engine import digest
    engine = h.engine
    progress = engine.store.get('handoff_progress', key, {})
    if _kind(progress) is not None:
        _fail('唯一一次答复补正后仍未通过，原答复、草稿和证据已保留。')
    scope = _scope(h, run_id, role, purpose, key, progress, correction_kind=ORIGIN_KEY)
    context = engine.store.get('handoff_inputs', key)['context']
    derived = _protocol(ORIGIN_KEY).problems(engine, run_id, role, key, result, context)
    if not derived or (problems is not None and derived != problems):
        _fail('来源绑定诊断与当前完整作者校验不一致，未领取补正。')
    if scope != _scope(h, run_id, role, purpose, key,
                       engine.store.get('handoff_progress', key, {}), correction_kind=ORIGIN_KEY):
        _fail('来源核验期间冻结记录改变，未领取补正。')
    return {'scope': scope, 'candidate_sha256': digest(result),
            'problems': derived, 'problems_sha256': digest(derived)}


def schedule(h, run_id, role, purpose, key, result, problems, *, correction_kind=KEY,
             _origin_validation=None):
    from .engine import digest
    from .execution_archive import save
    from .implementation_save_resume import _settled_response
    engine = h.engine
    engine.check_active(run_id)
    progress = copy.deepcopy(engine.store.get('handoff_progress', key, {}))
    protocol = _protocol(correction_kind)
    if _kind(progress) is not None:
        _fail('唯一一次答复补正后仍未通过，原答复、草稿和证据已保留。')
    scope = _scope(h, run_id, role, purpose, key, progress, correction_kind=correction_kind)
    if progress.get('mode') != 'final_only' and correction_kind == KEY:
        from .implementation_author_read_recovery import validate_scope
        validate_scope(h, run_id, role, purpose, key, progress)
    source_kind = 'finalization' if progress.get('mode') == 'final_only' else 'normal_request'
    source = progress.get(source_kind, {})
    if source.get('provider_id') is None and engine.store.get('runs', run_id, {}).get('mode') != 'demo':
        _fail('在线研究不能用测试标签领取作者答复补正。')
    if (source.get('status') != 'response_saved' or not source.get('operation_id')
            or source.get('response', {}).get('json') != result
            or source.get('prepared', {}).get('role') != role
            or source.get('prepared', {}).get('context', {}).get('_purpose') != purpose
            or not _settled_response(engine.store, run_id, source)):
        _fail('只可补正已完整接收并结算的真实 final 答复。')
    if hasattr(h.intelligence, 'provider_for'):
        provider = h.intelligence.provider_for(role)
        if (source.get('provider_id'), source.get('model')) != (provider.get('id'), provider.get('model')):
            _fail('补正供应方或模型配置已改变，原答复保留。')
    if correction_kind == ORIGIN_KEY:
        verified = _origin_validation or prepare_origin_schedule(h, run_id, role, purpose, key, result, problems)
        if (verified.get('scope') != scope or verified.get('candidate_sha256') != digest(result)
                or verified.get('problems') != problems or verified.get('problems_sha256') != digest(problems)):
            _fail('来源绑定诊断与当前完整作者校验不一致，未领取补正。')
    engine.check_active(run_id)
    # Archive once through the existing sanitized, content-addressed service.
    archive_suffix = ':author-origin-correction:v1' if correction_kind == ORIGIN_KEY else ':author-response-correction:v1'
    archive_kind = source_kind if correction_kind == ORIGIN_KEY else 'finalization'
    archive = save(engine.store, run_id, role, key + archive_suffix,
        {archive_kind: source}, handoff_id=key, request_id=source['response'].get('request_id'))
    if archive.get('redacted'):
        _fail('原答复包含已清理敏感信息，无法完整验证补正来源。')
    operation = uid()
    diagnostic = protocol.diagnostic(result, 1, 1, problems)
    prepared = copy.deepcopy(source['prepared'])
    control = prepared['context'].get('_harness_control')
    if not isinstance(control, dict):
        _fail('原请求缺少宿主控制合同。')
    control.update(mode='final_only', operation_id=operation,
                   reason=('One bounded saved-author origin binding correction; no tools'
                       if correction_kind == ORIGIN_KEY else 'One bounded saved-author declaration correction; no tools'))
    # An exact-range read turn may return a final directly. Preserve that paid
    # packet in the archive, while the new correction has only the frozen final
    # submission schema, with no tool-call branch.
    prepared['schema'] = author.submission_schema(engine.store.get('handoff_inputs', key)['context'])
    prepared['context'].setdefault('tool_results', []).append(diagnostic)
    prepared['task'] += ('\n本次只修正最新 host_diagnostic 指定的作者回复字段；已保存文档不可再改，'
        '不调用任何工具，原 submission、证据和其他回复逐字保留。返回原 Schema 完整 final。')
    from .implementation_phase_context import assert_context
    assert_context(prepared['context'])
    final = {'status': 'ready', 'operation_id': operation, 'prepared': prepared,
        'reason': control['reason'], 'created_at': now(), 'withheld_tool_calls': [],
        'completed_result_hashes': [digest(item) for item in progress.get('tool_results', [])],
        'author_response_correction_of': source['operation_id']}
    from .tool_request_recovery import prepared_successor
    final = prepared_successor(engine.store, key, final)
    audit = {'version': 1, 'scope_version': SCOPE_VERSION,
        'attempt': 1, 'run_id': run_id, 'role': role, 'purpose': purpose,
        'handoff_id': key, 'scope': scope, 'source_archive_id': archive['id'],
        'source_archive_sha256': archive['sha256'], 'source_finalization_sha256': digest(source),
        'candidate_sha256': digest(result), 'source_operation_id': source['operation_id'],
        'operation_id': operation, 'prepared_sha256': digest(prepared), 'problems': copy.deepcopy(problems),
        'created_at': now(), 'tools_allowed': False, 'scientific_approval': False}
    if correction_kind == ORIGIN_KEY:
        audit.update(correction_kind=correction_kind, source_kind=archive_kind)
    audit['sha256'] = digest(audit)
    def claim(current):
        if current != progress or _scope(h, run_id, role, purpose, key, current,
                                        correction_kind=correction_kind) != scope:
            _fail('补正已被领取或冻结记录改变，未重复派发。')
        return {**current, correction_kind: audit, 'mode': 'final_only', 'finalization': final}
    engine.check_active(run_id)
    engine.store.mutate('handoff_progress', key, claim)
    engine.store.event(run_id, ('implementation.author_origin_correction' if correction_kind == ORIGIN_KEY
                               else 'implementation.author_response_correction'),
        '已保存正文保持不变，同一工程师仅补正一次逐项答复字段；不补读、不调用写入工具、不代替三方审核。',
        {'handoff_id': key, 'source_request_id': source['response'].get('request_id'),
         'source_archive_id': archive['id'], 'operation_id': operation,
         'problem_count': len(problems), 'scientific_approval': False, 'tools_allowed': False})
    return True
