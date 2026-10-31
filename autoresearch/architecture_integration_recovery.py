"""One immutable, same-author repair of an accepted but unusable design patch."""
from __future__ import annotations

import copy
import json

from .storage import now
from .architecture_validation import operation

VERSION = 1


def _provider_identity(progress):
    request = progress.get('finalization') or progress.get('normal_request') or {}
    response = request.get('response') or {}
    return {'provider_id': request.get('provider_id') or response.get('provider'),
            'model': request.get('model') or response.get('model')}


def _extra_fields(value, shape, pointer=''):
    rows = []
    if isinstance(value, dict):
        properties = shape.get('properties', {})
        for field, child in value.items():
            path = pointer + '/' + field.replace('~', '~0').replace('/', '~1')
            if field not in properties and shape.get('additionalProperties') is False:
                rows.append({'source_pointer': path, 'original_value': copy.deepcopy(child)})
            elif field in properties:
                rows.extend(_extra_fields(child, properties[field], path))
    elif isinstance(value, list) and isinstance(shape.get('items'), dict):
        for index, child in enumerate(value):
            rows.extend(_extra_fields(child, shape['items'], pointer + '/' + str(index)))
    return rows


def _without_extra_fields(value, shape):
    """Only remove schema-unknown keys for comparison; never coerce a value."""
    if isinstance(value, dict):
        properties = shape.get('properties', {})
        return {field: _without_extra_fields(child, properties.get(field, {}))
                for field, child in value.items()
                if field in properties or shape.get('additionalProperties') is not False}
    if isinstance(value, list) and isinstance(shape.get('items'), dict):
        return [_without_extra_fields(child, shape['items']) for child in value]
    return copy.deepcopy(value)


def _preserved_design(base, response):
    from .engine import DESIGN, ResearchBlocked
    from .intelligence import ProviderError
    from .architecture_integration import apply
    cleaned = copy.deepcopy(response)
    cleaned['design'] = _without_extra_fields(cleaned.get('design', {}), DESIGN)
    try:
        return apply(base, cleaned, expected_hash=response['integration_base_hash'])
    except (ProviderError, ResearchBlocked, TypeError, ValueError) as exc:
        raise ResearchBlocked('原交付除额外字段外仍有无法无歧义映射的错误；不能以结构补正猜测科学参数。') from exc


def _checked(engine, run_id, audit_id, *, audit_record=None):
    from .engine import DESIGN, Harness, ResearchBlocked, digest
    from .architecture_integration import check
    audit = audit_record if audit_record is not None else engine.store.get('stage_contexts', audit_id, {})
    payload = audit.get('payload', {})
    pending = payload.get('pending', {})
    hid = pending.get('handoff_id', '')
    decision = engine.store.get('decisions', hid, {})
    assignment = engine.store.get('handoffs', hid, {})
    frozen = engine.store.get('handoff_inputs', hid, {})
    if (audit.get('run_id') != run_id or audit.get('kind') != 'architecture_delivery_validation'
            or audit.get('sha256') != digest(payload)
            or hid != Harness.decision_key(run_id, 'engineer', pending.get('purpose'),
                pending.get('instruction'), pending.get('context'), pending.get('schema'))
            or any(row.get('id') != hid or row.get('run_id') != run_id
                   for row in (decision, assignment, frozen))
            or any(frozen.get(k) != pending.get(k) for k in ('instruction', 'context', 'schema'))
            or decision.get('run_id') != run_id or decision.get('role') != 'engineer'
            or decision.get('result') != payload.get('response')
            or digest(payload.get('response')) != payload.get('response_sha256')
            or assignment.get('run_id') != run_id or assignment.get('to_role') != 'engineer'
            or assignment.get('status') != 'completed' or assignment.get('output_hash') != payload.get('response_sha256')
            or digest(engine.store.get('handoff_progress', hid, {})) != payload.get('progress_sha256')
            or payload.get('provider_identity') != _provider_identity(engine.store.get('handoff_progress', hid, {}))
            or payload.get('preserved_constraints') != _extra_fields(payload.get('response', {}).get('design', {}), DESIGN)):
        raise ResearchBlocked('架构交付补正的原答复、冻结输入或读取回执发生变化。')
    source = check(engine, run_id, pending['context']['integration_audit_id'])
    if (payload.get('base_design') != source['payload']['base_design']
            or payload.get('base_design_hash') != source['payload']['base_design_hash']
            or digest(payload.get('base_design')) != payload.get('base_design_hash')):
        raise ResearchBlocked('架构交付补正的原参数基点发生变化。')
    try:
        preserved = _preserved_design(payload['base_design'], payload['response'])
    except ResearchBlocked as exc:
        if payload.get('repair_unavailable_reason') != str(exc) or payload.get('preserved_design') is not None:
            raise ResearchBlocked('不可补正交付缺少原始校验诊断。') from exc
    else:
        if payload.get('repair_unavailable_reason') or payload.get('preserved_design') != preserved:
            raise ResearchBlocked('架构补正的原有科学参数与冻结交付不一致。')
    return audit


def record_failure(engine, run_id, revision, pending, result, error):
    """Journal after receiving a real response, before preparing another assignment."""
    from .engine import DESIGN, ResearchBlocked, digest
    from .architecture_integration import check
    if pending['context'].get('architecture_delivery_repair_protocol') == VERSION:
        mark_failed(engine, run_id, revision, error)
        raise ResearchBlocked(revision['reason'])
    previous = revision.get('delivery_validation')
    if previous:
        if previous.get('status') != 'repaired' or previous.get('original_handoff_id') == pending['handoff_id']:
            raise ResearchBlocked('该架构交付已有一次补正记录，不能重新创建补正。')
        revision.setdefault('delivery_validation_history', []).append(copy.deepcopy(previous))
    source = check(engine, run_id, pending['context']['integration_audit_id'])
    progress = engine.store.get('handoff_progress', pending['handoff_id'], {})
    payload = {'pending': copy.deepcopy(pending), 'response': copy.deepcopy(result),
        'response_sha256': digest(result), 'base_design': copy.deepcopy(source['payload']['base_design']),
        'base_design_hash': source['payload']['base_design_hash'], 'progress_sha256': digest(progress),
        'diagnostic': str(error), 'preserved_constraints': _extra_fields(result.get('design', {}), DESIGN),
        'provider_identity': _provider_identity(progress)}
    try:
        payload['preserved_design'] = _preserved_design(payload['base_design'], result)
    except ResearchBlocked as exc:
        payload.update(preserved_design=None, repair_unavailable_reason=str(exc))
    audit_id = revision['id'] + ':delivery-validation-v1:' + digest(pending['handoff_id'])
    audit = {'id': audit_id, 'run_id': run_id, 'kind': 'architecture_delivery_validation',
             'payload': payload, 'sha256': digest(payload), 'created_at': now()}
    existing = engine.store.get('stage_contexts', audit_id)
    if existing and existing.get('payload') != payload:
        raise ResearchBlocked('交付失败审计已存在，不能覆盖原始答复。')
    _checked(engine, run_id, audit_id, audit_record=existing or audit)
    status = {'status': 'delivery_validation_failed', 'audit_id': audit_id,
        'original_handoff_id': pending['handoff_id'], 'original_response_sha256': digest(result),
        'base_design_hash': payload['base_design_hash'], 'error': str(error),
        'reason': '作者答复已保存，但尚不能形成合法架构；将复用原交付进行一次定向补正。',
        'scientific_approval': False, 'attempt_limit': 1}
    if payload.get('repair_unavailable_reason'):
        status.update(status='repair_failed', reason=payload['repair_unavailable_reason'],
                      repair_unavailable=True)
    revision.update(status='blocked' if status['status'] == 'repair_failed' else 'delivery_validation_failed',
                    reason=status['reason'], delivery_validation=status)
    engine.store.batch([('stage_contexts', audit_id, existing or audit),
                        ('architecture_revisions', revision['id'], revision)])
    engine.store.event(run_id, 'architecture.delivery_validation_failed', status['reason'], status)
    return revision


def activate(engine, h, run_id, revision):
    from .engine import ResearchBlocked, digest
    from .architecture_integration import schema, SCHEMA_VERSION
    state = revision.get('delivery_validation', {})
    if state.get('status') == 'repair_failed':
        raise ResearchBlocked(state['reason'])
    if state.get('status') not in {'delivery_validation_failed', 'repair_pending'}:
        return revision
    audit = _checked(engine, run_id, state['audit_id'])
    payload = audit['payload']
    if state['status'] == 'repair_pending':
        if revision.get('pending', {}).get('handoff_id') != state.get('repair_handoff_id'):
            raise ResearchBlocked('架构补正交接与检查点不一致。')
        return revision
    if revision.get('pending') != payload['pending']:
        raise ResearchBlocked('架构补正目标已变化。')
    context = copy.deepcopy(payload['pending']['context'])
    context.update(architecture_delivery_repair_protocol=VERSION, integration_schema_version=SCHEMA_VERSION,
        delivery_validation_audit_id=audit['id'], delivery_validation_sha256=audit['sha256'],
        original_delivery=copy.deepcopy(payload['response']), original_delivery_sha256=payload['response_sha256'],
        delivery_validation_error=payload['diagnostic'],
        preserved_review_constraints=copy.deepcopy(payload['preserved_constraints']),
        delivery_repair_provider_identity=payload['provider_identity'])
    # The reply already completed its evidence work. A repair never earns new
    # reading credit or requires another pass through the same documents.
    context.update(document_working_notes_required=False, architecture_required_documents=[],
                   definition_integration_agenda=[], host_admission_reads=[])
    instruction = ('原作者交付已经完整保存，但合成候选未通过结构校验。只补正 original_delivery 中的结构错误，'
        '返回相对同一 integration_base_hash 的完整修正版增量补丁。复用已经完成的证据工作，不调用工具、'
        '不重新读文献或运行实验。这是本交付唯一一次定向补正。保留原科学判断、全部比较、参数、种子、阈值、'
        '隔离和失败停止条件，不得为了通过校验改变结果或自行批准。proposal_hash、parent_design_hash、'
        'resolved_issue_ids、requires_new_consensus、unset_paths 保持原交付原值。'
        '原非标准字段的完整内容由宿主原样附入 evaluation 的“原交付约束保全附件”，明确待独立审议；'
        '不得把移除非法JSON键解释成取消该约束。所有原有合法字段，包括 architecture、dataset、evaluation、'
        'limitations 及 acceptance.limitations 的叙述都必须原样保留；宿主单独附加约束保全章节。'
        '保留原 acceptance.complete 布尔值；仅在 change_summary 说明其定量标准覆盖语义，不能因没有测量自动改变它。'
        'acceptance 只允许 criteria、complete、limitations；额外核验条件由宿主附加，模型不改写 evaluation。')
    # STR/STRS are shared constants in the existing schema factory. Bind this
    # request on a deep copy, never mutate another role's output contract.
    shape = copy.deepcopy(schema())
    for field in ('integration_base_hash', 'proposal_hash', 'parent_design_hash', 'resolved_issue_ids', 'requires_new_consensus', 'unset_paths'):
        shape['properties'][field] = {**shape['properties'][field],
                                      'const': copy.deepcopy(payload['response'][field])}
    acceptance = payload['response'].get('design', {}).get('acceptance', {})
    complete = acceptance.get('complete', payload['base_design'].get('acceptance', {}).get('complete'))
    if type(complete) is bool:
        shape['properties']['design']['properties']['acceptance']['properties']['complete']['const'] = complete
    purpose = payload['pending']['purpose'] + ':delivery-repair-v1'
    hid = h._handoff(run_id, 'engineer', purpose, instruction, context, shape)['id']
    revision.update(status='repair_pending', pending={'purpose': purpose, 'instruction': instruction,
        'context': context, 'schema': shape, 'handoff_id': hid},
        delivery_validation={**state, 'status': 'repair_pending', 'repair_handoff_id': hid,
            'reason': '一次定向补正已准备；原答复、原参数与全部约束保留，新架构仍须独立评议。'})
    revision['reason'] = revision['delivery_validation']['reason']
    engine.store.put('architecture_revisions', revision['id'], revision)
    engine.store.event(run_id, 'architecture.delivery_repair_prepared', revision['reason'],
                       {'audit_id': audit['id'], 'repair_handoff_id': hid, 'scientific_approval': False})
    return revision


def preserve_constraints(engine, run_id, pending, result, candidate):
    from .engine import ResearchBlocked
    audit = _checked(engine, run_id, pending['context']['delivery_validation_audit_id'])
    if pending['context'].get('delivery_validation_sha256') != audit['sha256']:
        raise ResearchBlocked('架构补正来源哈希不一致。')
    payload = audit['payload']
    for field in ('integration_base_hash', 'proposal_hash', 'parent_design_hash', 'resolved_issue_ids', 'requires_new_consensus', 'unset_paths'):
        if result.get(field) != payload['response'].get(field):
            raise ResearchBlocked('结构补正不能改变原版本绑定、问题闭合申报或科学判断。')
    original_acceptance = payload['response'].get('design', {}).get('acceptance', {})
    complete = original_acceptance.get('complete', payload['base_design'].get('acceptance', {}).get('complete'))
    if candidate.get('acceptance', {}).get('complete') != complete:
        raise ResearchBlocked('结构补正不能隐式改变定量验收完整性判断。')
    expected = payload['preserved_design']
    for field in set(expected) | set(candidate):
        actual, original = candidate.get(field), expected.get(field)
        if actual != original:
            raise ResearchBlocked('结构补正改变了原交付的合法叙述、科学参数、实验矩阵或资源约束：' + field)
    if payload['preserved_constraints']:
        candidate = copy.deepcopy(candidate)
        candidate['evaluation'] += ('\n\n## 原交付约束保全附件\n\n'
            '下列约束来自原作者实际交付，因字段位置不符合格式而原样保全；待独立架构评议，'
            '不表示已核验、已通过或已获得执行批准。原交付 SHA256：' + payload['response_sha256']
            + '\n\n```json\n' + json.dumps(payload['preserved_constraints'], ensure_ascii=False, indent=2)
            + '\n```\n')
    return candidate


def mark_failed(engine, run_id, revision, error):
    state = revision['delivery_validation']
    reason = '本次交付的一次定向补正仍未通过；保留两份原答复，停止自动重发。'
    revision.update(status='blocked', reason=reason,
        delivery_validation={**state, 'status': 'repair_failed', 'error': str(error), 'reason': reason})
    engine.store.put('architecture_revisions', revision['id'], revision)
    engine.store.event(run_id, 'architecture.delivery_repair_failed', reason,
                       {'audit_id': state['audit_id'], 'error': str(error), 'scientific_approval': False})


def _completed_repair(engine, run_id, pending):
    """Inspect a completed repair without Harness.ask's cache-miss dispatch."""
    from .engine import Harness, ResearchBlocked, digest
    from .intelligence import parse_json
    hid = pending.get('handoff_id')
    decision = engine.store.get('decisions', hid, {})
    assignment = engine.store.get('handoffs', hid, {})
    frozen = engine.store.get('handoff_inputs', hid, {})
    progress = engine.store.get('handoff_progress', hid, {})
    context = pending.get('context', {})
    if (not hid or not isinstance(decision.get('result'), dict)
            or context.get('architecture_delivery_repair_protocol') != VERSION
            or any(row.get('id') != hid or row.get('run_id') != run_id for row in (decision, assignment, frozen))
            or decision.get('role') != 'engineer' or assignment.get('to_role') != 'engineer'
            or assignment.get('status') != 'completed' or assignment.get('purpose') != pending.get('purpose')
            or assignment.get('output_hash') != digest(decision['result'])
            or assignment.get('input_hash') != digest({k: pending.get(k) for k in ('instruction', 'context', 'schema')})
            or any(frozen.get(k) != pending.get(k) for k in ('instruction', 'context', 'schema'))
            or Harness.decision_key(run_id, 'engineer', pending.get('purpose'), pending.get('instruction'),
                                    context, pending.get('schema')) != hid):
        raise ResearchBlocked('本地复验要求同版本已完成的补正原答复、冻结输入和原交接；未发送模型请求。')
    parse_json(json.dumps(decision['result'], ensure_ascii=False, allow_nan=False), pending['schema'])
    return decision['result'], {name: digest(value) for name, value in
        (('decision_sha256', decision), ('handoff_sha256', assignment), ('inputs_sha256', frozen),
         ('progress_sha256', progress), ('pending_sha256', pending))}


def checked_local_revalidation(engine, run_id, audit_id, *, pending=None):
    from .engine import ResearchBlocked, digest
    audit = engine.store.get('stage_contexts', audit_id, {})
    payload = audit.get('payload', {})
    hid = payload.get('repair_handoff_id')
    if pending is None:
        frozen = engine.store.get('handoff_inputs', hid, {})
        assignment = engine.store.get('handoffs', hid, {})
        pending = {**{k: frozen.get(k) for k in ('instruction', 'context', 'schema')},
                   'handoff_id': hid, 'purpose': assignment.get('purpose')}
    result, anchors = _completed_repair(engine, run_id, pending)
    original = engine.store.get('stage_contexts', payload.get('source_audit_id'), {})
    if (audit.get('kind') != 'architecture_delivery_local_revalidation' or audit.get('run_id') != run_id
            or audit.get('sha256') != digest(payload) or pending['handoff_id'] != hid
            or payload.get('anchors') != anchors or payload.get('response_sha256') != digest(result)
            or payload.get('source_audit_sha256') != original.get('sha256')
            or original.get('sha256') != digest(original.get('payload'))
            or pending['context'].get('delivery_validation_audit_id') != payload.get('source_audit_id')):
        raise ResearchBlocked('本地复验审计或已保存补正交付发生变化；未发送模型请求。')
    return audit, copy.deepcopy(result)


def cached_revalidated_result(engine, run_id, revision):
    """Return only the hash-bound paid reply; missing cache is a hard stop."""
    from .engine import ResearchBlocked
    from .experiment_plan import PLANNER_VALIDATION_VERSION
    state = revision.get('delivery_validation', {})
    marker = state.get('local_revalidation') or {}
    audit, result = checked_local_revalidation(engine, run_id, marker.get('audit_id'), pending=revision.get('pending'))
    if (marker.get('sha256') != audit['sha256']
            or audit['payload']['validator_version'] != PLANNER_VALIDATION_VERSION
            or state.get('repair_handoff_id') != audit['payload']['repair_handoff_id']
            or state.get('audit_id') != audit['payload']['source_audit_id']):
        raise ResearchBlocked('本地复验版本或补正来源已变化；须重新核验，未发送模型请求。')
    return result


def _prepare_local_revalidation(engine, run_id, revision):
    from .engine import ResearchBlocked, digest
    from .experiment_plan import build_plan, PLANNER_VALIDATION_VERSION
    from .architecture_integration import materialize
    from . import architecture_convergence as convergence
    state, pending = revision['delivery_validation'], revision.get('pending', {})
    if (state.get('repair_unavailable') or not state.get('repair_handoff_id')
            or pending.get('handoff_id') != state['repair_handoff_id']
            or pending.get('context', {}).get('delivery_validation_audit_id') != state['audit_id']):
        raise ResearchBlocked('没有完整可复验的已保存补正交付；不能再次发送模型补正。')
    result, anchors = _completed_repair(engine, run_id, pending)
    original = engine.store.get('steps', run_id + ':design')['output']
    parents = [original, *[row['design'] for row in revision['revisions']]]
    context = pending['context']
    expected = {issue['id'] for issue in context['review_fixes']}
    resolved = set(result['resolved_issue_ids'])
    if (result['parent_design_hash'] != digest(parents[-1]) or result['proposal_hash'] != revision['proposal_hash']
            or not resolved <= expected or result['requires_new_consensus']
            or resolved != expected and context.get('architecture_progress_protocol') != 1):
        raise ResearchBlocked('已保存补正不满足原版本和作者范围约束，不能以本地复验取得批准。')
    # These are the original strong source/constraint validators, not a new
    # model response or an exemption from the immutable scientific contract.
    candidate = materialize(engine, run_id, pending, result)
    if (not candidate['primary_metric'].startswith('val_')
            or any(digest(candidate) == digest(parent) or convergence.enabled(engine, run_id)
                   and convergence.material(candidate, 'architecture') == convergence.material(parent, 'architecture')
                   for parent in parents)):
        raise ResearchBlocked('已保存补正缺少实质新版本或验证集主指标，不能准备恢复。')
    from .experiment_plan import build_for_engine
    plan = build_for_engine(engine, run_id, candidate, context['trial_budget'])
    source = engine.store.get('stage_contexts', state['audit_id'])
    payload = {'validator_version': PLANNER_VALIDATION_VERSION, 'source_audit_id': source['id'],
        'source_audit_sha256': source['sha256'], 'repair_handoff_id': pending['handoff_id'],
        'anchors': anchors, 'response_sha256': digest(result), 'candidate_hash': digest(candidate),
        'plan_hash': digest(plan), 'trial_budget': context['trial_budget'],
        'previous_failure': copy.deepcopy(state), 'model_calls': 0, 'scientific_approval': False}
    audit_id = revision['id'] + ':local-revalidation:' + digest([pending['handoff_id'], PLANNER_VALIDATION_VERSION])
    audit = {'id': audit_id, 'run_id': run_id, 'kind': 'architecture_delivery_local_revalidation',
             'payload': payload, 'sha256': digest(payload), 'created_at': now()}
    existing = engine.store.get('stage_contexts', audit_id)
    if existing and existing.get('payload') != payload:
        raise ResearchBlocked('本校验版本已有不同的本地复验审计，不能覆盖原失败或重复恢复。')
    marker = {'status': 'passed', 'audit_id': audit_id, 'sha256': audit['sha256'], 'validator_version': PLANNER_VALIDATION_VERSION,
              'model_calls': 0, 'scientific_approval': False}
    reason = '已保存补正通过本地兼容校验，等待恢复独立评议；不会再次发送补正模型请求。'
    revision.update(status='repair_pending', reason=reason, delivery_validation={**state, 'status': 'repair_pending',
        'local_revalidation': marker, 'reason': reason, 'error': None})
    engine.store.batch([('stage_contexts', audit_id, existing or audit), ('architecture_revisions', revision['id'], revision)])
    engine.store.event(run_id, 'architecture.delivery_locally_revalidated', reason, marker)
    return revision


def view(engine, run_id, revision):
    state = revision.get('delivery_validation')
    if state:
        return {**copy.deepcopy(state), 'can_prepare': state['status'] == 'delivery_validation_failed'}
    pending = revision.get('pending', {})
    run = engine.store.get('runs', run_id, {})
    # Lightweight legacy projection; GET must not replay candidate validation.
    if (run.get('status') in {'blocked', 'paused', 'failed'}
            and pending.get('context', {}).get('architecture_integration_protocol') == 1
            and 'schema validation' in (run.get('error') or '')
            and engine.store.get('decisions', pending.get('handoff_id', ''))):
        return {'status': 'delivery_validation_failed', 'can_prepare': True,
            'original_handoff_id': pending['handoff_id'], 'error': run['error'],
            'reason': '原交付已保存，合成架构未通过校验；可准备一次保留原约束的定向补正。',
            'scientific_approval': False, 'attempt_limit': 1}
    return None


@operation
def prepare_delivery_repair(engine, run_id):
    """Explicit stopped-run preparation: no model calls, resumption or approval."""
    from .engine import Harness, ResearchBlocked
    from .intelligence import ProviderError
    from . import architecture_revisions as revisions, architecture_definitions as definitions
    from .architecture_integration import materialize
    run = engine.store.get('runs', run_id, {})
    revision = revisions.state(engine, run_id)
    if (run.get('mode') != 'live' or run.get('stage') != 'design'
            or run.get('status') not in {'blocked', 'paused', 'failed'} or not revision):
        raise ResearchBlocked('交付补正只能为已停止的真实架构任务准备。')
    engine._consensus_gate(run_id)
    revisions._available(engine, run_id)
    revisions._checked(engine, run_id, engine.store.get('steps', run_id + ':design')['output'])
    if any((row.get('checkpoint') or {}).get('status') in definitions.TRANSIENT
           for row in engine.store.list('codex_reasoning', run_id)):
        raise ResearchBlocked('仍有活动请求，不能准备交付补正。')
    h = Harness(engine, None)
    if revision.get('delivery_validation'):
        if revision['delivery_validation']['status'] == 'repair_failed':
            return _project_preparation(engine, run_id, _prepare_local_revalidation(engine, run_id, revision))
        if revision['delivery_validation'].get('local_revalidation') and revision.get('pending'):
            cached_revalidated_result(engine, run_id, revision)
        revision = activate(engine, h, run_id, revision)
        return _project_preparation(engine, run_id, revision)
    pending = revision.get('pending', {})
    result = engine.store.get('decisions', pending.get('handoff_id', ''), {}).get('result')
    if not result or pending.get('context', {}).get('architecture_integration_protocol') != 1:
        raise ResearchBlocked('没有已完成且待验证的架构整合交付。')
    try:
        materialize(engine, run_id, pending, result)
    except ProviderError as exc:
        record_failure(engine, run_id, revision, pending, result, exc)
    else:
        raise ResearchBlocked('原交付可形成合法候选，无需结构补正。')
    if revision['delivery_validation']['status'] == 'repair_failed':
        return _project_preparation(engine, run_id, revision)
    return _project_preparation(engine, run_id, activate(engine, h, run_id, revision))


def _project_preparation(engine, run_id, revision):
    """Show the prepared recovery without resuming or changing the original step."""
    state = revision.get('delivery_validation', {})
    if state.get('status') == 'repair_pending':
        reason = (state['reason'] if state.get('local_revalidation') else
                  '架构交付补正已准备，等待恢复执行；原答复及核验约束保留，原错误已归档。')
        engine.update_run(run_id, error=reason)
        graph = engine.store.get('graph_status', run_id, {})
        graph.update(id=run_id, node='design', cycle=engine.cycle(run_id), status='waiting',
                     activity='architecture_delivery_repair', waiting_for='architecture_delivery_repair',
                     updated_at=now())
        graph.pop('error', None)
        engine.store.put('graph_status', run_id, graph)
    elif state.get('status') == 'repair_failed':
        engine.update_run(run_id, error=state['reason'])
    return view(engine, run_id, revision)
