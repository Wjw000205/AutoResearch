"""Same-handoff source correction after an unsealed alignment candidate.

Native requests and original answers stay in the checkpoint, while only compact
locators enter the next ordinary, budget-admitted model turn.
"""
from __future__ import annotations

import copy

from .implementation_alignment_evidence import PROTOCOL, READ_PENDING, validate_candidate

# Alignment corrections have no attempt cap. Each successor still goes through
# ordinary budget admission, frozen-source validation and request journaling.
LIMIT = None
HISTORY = 'alignment_candidate_history'


def _review_stage(context, purpose):
    stage = purpose.split(':', 1)[0]
    return stage in {'implementation_document_review', 'code_review'} or (
        stage == 'implementation_spec' and context.get('implementation_issue_response_protocol') == 1
        and context.get('implementation_document_authoring_protocol') == 1)


def pending_continuation(h, run_id, role, purpose, key, progress):
    """Recognize the unsubmitted successor of an archived, received answer.

    This is not retry permission. Ordinary dispatch journals its request before
    contacting the provider; any current request must use normal recovery.
    """
    from .engine import digest
    if ('normal_request' in progress or 'finalization' in progress
            or progress.get('mode') == 'final_only' or progress.get('pending_tool_calls')
            or progress.get('id') != key or progress.get('run_id') != run_id):
        return False
    history, results = progress.get(HISTORY), progress.get('tool_results')
    if not isinstance(history, list) or not history or not results:
        return False
    last = history[-1]
    if not isinstance(last, dict):
        return False
    turn, previous = progress.get('turn'), last.get('source_turn')
    if (type(turn) is not int or type(previous) is not int or previous < 0
            or turn != previous + 1 or last.get('next_turn') != turn
            or last.get('attempt') != len(history)):
        return False
    frozen = h.engine.store.get('handoff_inputs', key, {})
    assignment = h.engine.store.get('handoffs', key, {})
    context = frozen.get('context', {})
    from .implementation_author_revision import continuation_limit
    history_limit = (continuation_limit(context, progress)
                     if last.get('author_revision_problems') else LIMIT)
    if history_limit is not None and len(history) > history_limit:
        return False
    payload = {field: frozen.get(field) for field in ('instruction', 'context', 'schema')}
    if (context.get(PROTOCOL) != 1 or not _review_stage(context, purpose)
            or frozen.get('id') != key or frozen.get('run_id') != run_id
            or assignment.get('id') != key or assignment.get('run_id') != run_id
            or assignment.get('to_role') != role or assignment.get('purpose') != purpose
            or assignment.get('input_hash') != digest(payload)
            or h.decision_key(run_id, role, purpose, **payload) != key
            or h.engine.store.get('decisions', key)):
        return False
    sources = last.get('source_requests', {})
    slot = 'finalization' if last.get('source_mode') == 'final_only' else 'normal_request'
    source = sources.get(slot, {})
    response = source.get('response') or {}
    candidate = last.get('candidate')
    prepared = source.get('prepared') or {}
    if (source.get('status') != 'response_saved' or not source.get('operation_id')
            or not response.get('request_id') or not isinstance(candidate, dict)
            or digest(candidate) != last.get('candidate_sha256')
            or response.get('json') != candidate or prepared.get('role') != role
            or prepared.get('context', {}).get('_purpose') != purpose
            or slot == 'normal_request' and source.get('turn') != previous):
        return False
    diagnostic = results[-1]
    if (not isinstance(diagnostic, dict) or diagnostic.get('host_diagnostic') not in {
            'implementation_alignment_read_required', 'implementation_alignment_reference_required',
            'implementation_alignment_findings_format_required', 'implementation_routing_coordinates_required',
            'implementation_confirmation_scope_required', 'implementation_review_scope_required',
            'implementation_author_revision_required', 'implementation_code_reference_required',
            'implementation_code_repair_response_required'}
            or diagnostic.get('error') != last.get('diagnostic')
            or diagnostic.get('alignment_read_correction', {}).get('attempt') != len(history)):
        return False
    for entry in history:
        if entry.get('confirmation_scope_problems'):
            _verify_confirmation_source(h.engine, run_id, role, purpose, key, context, progress, entry)
    if last.get('code_reference_problems'):
        from .code_evidence_reference_correction import verify_archive
        verify_archive(h.engine, run_id, role, purpose, key, context, last)
    if last.get('code_repair_response_problems'):
        from .code_repair_response_correction import verify_archive
        verify_archive(h.engine, run_id, role, purpose, key, context, last)
    from .implementation_author_revision import validate_rework
    validate_rework(h, run_id, role, purpose, key, progress)
    return True


def _preserve_format_candidate(result, original, context, problems):
    from .implementation_alignment import validate_findings_format_correction
    validate_findings_format_correction(result, original)


def _verify_format_source(entry, role, purpose, *, problem_field='findings_format_problems'):
    """Keep an older format anchor bound to its archived received answer."""
    from .engine import ResearchBlocked, digest
    candidate = entry.get('candidate')
    if (not isinstance(candidate, dict) or digest(candidate) != entry.get('candidate_sha256')
            or not isinstance(entry.get(problem_field), list)
            or not entry[problem_field]):
        raise ResearchBlocked('架构对齐格式修正原候选或诊断身份损坏，未重发原请求。')
    sources = entry.get('source_requests')
    if not isinstance(sources, dict):
        raise ResearchBlocked('架构对齐格式修正原请求归档损坏，未重发原请求。')
    if not sources:
        return  # Offline harnesses may have no native request journal.
    slot = 'finalization' if entry.get('source_mode') == 'final_only' else 'normal_request'
    source = sources.get(slot) or {}
    response, prepared = source.get('response') or {}, source.get('prepared') or {}
    if (source.get('status') != 'response_saved' or not source.get('operation_id')
            or not response.get('request_id') or response.get('json') != candidate
            or prepared.get('role') != role or prepared.get('context', {}).get('_purpose') != purpose
            or slot == 'normal_request' and source.get('turn') != entry.get('source_turn')):
        raise ResearchBlocked('架构对齐格式修正原付费答复与角色、目的或请求归档不一致，未重发原请求。')


def _verify_confirmation_source(engine, run_id, role, purpose, key, context, progress, entry):
    """Authenticate this narrow format anchor, including earlier read receipts."""
    from .engine import ResearchBlocked, digest
    from . import implementation_confirmation_scope_correction as confirmations
    _verify_format_source(entry, role, purpose, problem_field='confirmation_scope_problems')
    sources = entry['source_requests']
    slot = 'finalization' if entry.get('source_mode') == 'final_only' else 'normal_request'
    source = sources.get(slot) or {}
    count = entry.get('confirmation_source_tool_count')
    if (not sources or digest(source.get('response', {}).get('json')) != entry['candidate_sha256']
            or digest(sources) != entry.get('confirmation_source_requests_sha256')
            or digest(source.get('prepared')) != entry.get('confirmation_source_prepared_sha256')
            or type(count) is not int or count < 0
            or not isinstance(progress.get('tool_results'), list)
            or len(progress['tool_results']) < count
            or digest(progress['tool_results'][:count]) != entry.get('confirmation_source_tool_results_sha256')):
        raise ResearchBlocked('实现确认集合格式补正原付费请求、候选或读取检查点已改变。')
    mappings = confirmations.problems(engine, run_id, role, key, entry['candidate'], context)
    if digest(mappings) != digest(entry['confirmation_scope_problems']):
        raise ResearchBlocked('实现确认集合格式补正原冻结来源或真实读取账本已改变。')


def _format_diagnostic(result, error, attempt, problems):
    # Only this latest received answer is supplied for an exact format-only
    # correction. Earlier paid inputs/responses stay in the immutable journal.
    return {'host_diagnostic': 'implementation_alignment_findings_format_required', 'error': error,
        'alignment_read_correction': {'attempt': attempt, 'max_attempts': LIMIT},
        'alignment_findings_format_correction': {
            'duplicate_findings': copy.deepcopy(problems), 'original_candidate': copy.deepcopy(result),
            'instruction': '只修正 duplicate_findings 列出的同一 requirement_id 重复项。返回原 Schema 的完整答复；'
                '每个 ID 只保留一项，由你合并其全部来源范围与具体条件。所有非 alignment_findings 字段、'
                '原 approve、issues、fixes、路由意见必须逐字保留，不得新增批准或删除异议。'
                '保留所有原验收 ID 和全部 source_refs；implementation_evidence 只能按原 findings 顺序逐字连接'
                '该 ID 每段原依据，中间使用两个换行字符（\\n\\n），不得改写或添加任何判断。'
                '重复项采用最强原状态：有 mismatch 就是 mismatch，否则有 pending 就是 pending，否则 aligned。'
                '未重复项原样保留；禁止改哈希、偷换来源、删条件或借格式修正重新判断科研结论。'}}


def _diagnostic(result, error, attempt, context, reference_problems, indexed_missing_ranges=()):
    if indexed_missing_ranges:
        return {'host_diagnostic': 'implementation_alignment_read_required', 'error': error,
            'alignment_read_correction': {'attempt': attempt, 'max_attempts': LIMIT,
                'required_document_ranges': copy.deepcopy(list(indexed_missing_ranges)),
                'instruction': '原候选及付费请求保留，尚未封存决定。必须在同一个冻结交接内，'
                    '按所列当前文档 document_id/SHA/section_id 与章节相对 offset/limit 继续 documents.read，'
                    '依据 next_offset 完整读完缺失范围，包括当前 SPEC 和本角色回复。'
                    '目录、摘要、旧版 SPEC、作者声明或宿主内联文本不能代替实际读取。'
                    '保持当前版本和全部原异议，完成真实核验后返回原 Schema 的完整独立意见；'
                    '不能为通过读取门禁删除问题或虚构批准。'}}
    if context.get('implementation_document_authoring_protocol') == 1 and context.get('implementation_issue_response_protocol') == 1:
        return {'host_diagnostic': 'implementation_alignment_read_required', 'error': error,
            'alignment_read_correction': {'attempt': attempt, 'max_attempts': LIMIT,
                'issue_response_evidence': [{'objection_id': reply['objection_id'],
                    'origin_ref_sha256': reply['origin_ref_sha256'], 'evidence': copy.deepcopy(reply['evidence'])}
                    for reply in result.get('issue_responses', [])],
                'instruction': '原作者答复和费用已保存，但引用证据尚未实际读完，未封存提交。保持所有原异议、'
                    '当前已保存草稿和原 Schema；按证据document_id/SHA/section_id补读正文。start/end是全文绝对坐标，'
                    '请根据documents.list或实际草稿目录换算section-relative offset。读取不是关闭或批准；'
                    '补齐后返回原 Schema 的完整 issue_responses，不能遗漏、代替原提出者确认或虚构文档修改。'}}
    from .engine import digest
    sources, implementation, code, pending = {}, {}, {}, []
    for finding in result.get('alignment_findings', []):
        if finding.get('status') == 'pending':
            pending.append(finding.get('requirement_id'))
        for ref in finding.get('source_refs', []):
            sources[digest(ref)] = copy.deepcopy(ref)
    for item in result.get('code_evidence', []):
        reference = {name: item.get(name) for name in
                     ('requirement_id', 'artifact_id', 'sha256', 'offset', 'limit')}
        code[digest(reference)] = reference
        for ref in item.get('implementation_refs', []):
            implementation[digest(ref)] = copy.deepcopy(ref)
    diagnostic = {'host_diagnostic': 'implementation_alignment_read_required', 'error': error,
        'alignment_read_correction': {'attempt': attempt, 'max_attempts': LIMIT,
            'source_refs': list(sources.values()), 'implementation_refs': list(implementation.values()),
            'code_refs': list(code.values()), 'pending_requirement_ids': list(dict.fromkeys(pending)),
            'instruction': '原候选回复与付费请求已归档，尚未登记批准。保持同一任务、文档和交付版本；'
                '按下列引用补读实际缺失原文，已有真实读取可复用。返回原 Schema 的完整独立评议；'
                '真实冲突继续给出具体修改与验证方法，不能把未读改写成已对齐或删除未解决异议。'}}
    if reference_problems:
        indexed = {doc['document_id']: doc for doc in context.get('indexed_documents', [])}
        current = context['implementation_alignment_contract']['primary_sources']
        diagnostic.update(host_diagnostic='implementation_alignment_reference_required',
            alignment_reference_correction={
                'invalid_references': [{**row,
                    'referenced_document_kind': indexed.get(row['reference']['document_id'], {}).get('kind'),
                    'historical_dependency': indexed.get(row['reference']['document_id'], {}).get('historical_dependency', False)}
                    for row in reference_problems],
                'allowed_primary_sources': [{**source,
                    'title': indexed[source['document_id']].get('title')}
                    for source in current],
                'instruction': '这次候选尚未登记批准，问题是下列具体引用不属于本交接的批准来源或有效范围。'
                    '保持同一任务、架构、实现文档与原异议。只从 allowed_primary_sources 的精确版本定位承接义务的原文，'
                    '用 documents.search 定位、documents.read 补读，再返回原 Schema 的完整独立评议。'
                    '历史定义候选可以说明继承背景，但不能填入 source_refs 或替代当前批准正文。'
                    '不能自动替换哈希、仅为通过校验选无关来源、删除尚无支持的义务或把未读写成 aligned；'
                    '若批准正文缺少决定性规定，应保留 pending/mismatch 并给出具体补充与验证方法。'})
        # Invalid pointers are correction targets, never suggested read credit.
        rejected = {digest(row['reference']) for row in reference_problems}
        diagnostic['alignment_read_correction']['source_refs'] = [
            ref for ref in diagnostic['alignment_read_correction']['source_refs'] if digest(ref) not in rejected]
    return diagnostic


async def before_commit(h, run_id, role, purpose, key, result):
    """Continue reading or correct precise references without changing the vote."""
    from .engine import ResearchBlocked, digest
    from .storage import now
    engine = h.engine
    frozen = engine.store.get('handoff_inputs', key, {})
    context = frozen.get('context', {})
    stage = purpose.split(':', 1)[0]
    if context.get(PROTOCOL) != 1 or not _review_stage(context, purpose):
        return False
    if engine.store.get('handoffs', key, {}).get('purpose') != purpose:
        raise ResearchBlocked('对齐补读目的与原始冻结交接不一致。')
    engine.check_active(run_id)
    from .implementation_author_read_recovery import validate_scope as validate_author_read_scope
    await engine._read_consensus_check(run_id, validate_author_read_scope,
        h, run_id, role, purpose, key, engine.store.get('handoff_progress', key, {}))
    from .implementation_author_response_recovery import validate_candidate as validate_author_correction
    await engine._read_consensus_check(run_id, validate_author_correction,
        h, run_id, role, purpose, key, result)
    from .implementation_author_delivery_recovery import validate_candidate as validate_author_delivery
    await engine._read_consensus_check(run_id, validate_author_delivery,
        h, run_id, role, purpose, key, result)
    from .implementation_author_inherited_delivery_recovery import validate_candidate as validate_inherited_delivery
    await engine._read_consensus_check(run_id, validate_inherited_delivery,
        h, run_id, role, purpose, key, result)
    progress = engine.store.get('handoff_progress', key, {})
    history = progress.get(HISTORY, [])
    from . import code_evidence_reference_correction as code_references
    if isinstance(history, list) and history and isinstance(history[-1], dict) and history[-1].get(code_references.FIELD):
        await engine._read_consensus_check(run_id, code_references.verify_archive,
            engine, run_id, role, purpose, key, context, history[-1])
        code_references.validate_correction(result, history[-1])
    from . import code_repair_response_correction as repair_responses
    if isinstance(history, list) and history and isinstance(history[-1], dict) and history[-1].get(repair_responses.FIELD):
        repair_responses.verify_archive(engine, run_id, role, purpose, key, context, history[-1])
        repair_responses.validate_correction(result, history[-1])
    from .implementation_alignment import FINDINGS_DUPLICATE
    format_source = next((item for item in reversed(history) if isinstance(item, dict)
                          and (item.get('findings_format_problems') or item.get('diagnostic') == FINDINGS_DUPLICATE)),
                         None) if isinstance(history, list) else None
    if format_source:
        _verify_format_source(format_source, role, purpose)
        _preserve_format_candidate(result, format_source['candidate'], context,
                                   format_source['findings_format_problems'])
    from . import implementation_review_routing_correction as coordinates
    coordinate_source = next((item for item in reversed(history) if isinstance(item, dict)
                              and (item.get('routing_coordinate_problems')
                                   or item.get('diagnostic') == coordinates.BOUNDARY_ERROR)),
                             None) if isinstance(history, list) else None
    if coordinate_source:
        _verify_format_source(coordinate_source, role, purpose, problem_field='routing_coordinate_problems')
        # An unchanged invalid answer stays in the same correction history.
        # It cannot become a decision or an approval.
        if digest(result) != coordinate_source['candidate_sha256']:
            coordinates.validate_correction(result, coordinate_source['candidate'], context, role)
    from . import implementation_confirmation_scope_correction as confirmations
    confirmation_source = next((item for item in reversed(history) if isinstance(item, dict)
                                and item.get('confirmation_scope_problems')), None) if isinstance(history, list) else None
    if confirmation_source:
        _verify_confirmation_source(engine, run_id, role, purpose, key, context, progress, confirmation_source)
        if digest(result) != confirmation_source['candidate_sha256']:
            confirmations.validate_correction(engine, run_id, role, key, result,
                confirmation_source['candidate'], context, confirmation_source['confirmation_scope_problems'])
    error, reference_problems, format_problems, coordinate_problems, confirmation_problems = None, [], [], [], []
    indexed_missing_ranges = []
    convergence_violation = False
    revision_problems = []
    code_reference_problems = []
    repair_response_problems = []
    try:
        from . import implementation_indexed_review as indexed
        if indexed.CONTRACT in context:
            bound = context[indexed.CONTRACT].get('binding', {})
            state = engine.store.get('codex_workflows', bound.get('workflow_id'), {})
            matches = [row for row in state.get('specifications', [])
                       if row.get('index') == bound.get('revision')]
            if len(matches) != 1 or indexed.binding(state, matches[0]) != bound:
                raise ResearchBlocked('实现文档索引审核：候选没有绑定当前唯一规范版本。')
            await engine._read_consensus_check(run_id, indexed.validate_candidate,
                engine, run_id, state, matches[0], role, key, result, context)
        else:
            await engine._read_consensus_check(run_id, validate_candidate,
                engine, run_id, role, key, result, context, delivery=stage == 'code_review')
    except ResearchBlocked as exc:
        if getattr(exc, 'author_revision_problems', None):
            revision_problems = exc.author_revision_problems
            error = str(exc)
        from .implementation_issue_responses import ChangedFieldsBlocked
        if isinstance(exc, ChangedFieldsBlocked):
            from .implementation_author_response_recovery import schedule
            return schedule(h, run_id, role, purpose, key, result, exc.problems)
        from . import implementation_author_origin_correction as origins
        if role == 'engineer' and stage == 'implementation_spec' and str(exc) == origins.BOUNDARY_ERROR:
            from .implementation_author_response_recovery import prepare_origin_schedule, schedule
            verified = await engine._read_consensus_check(run_id, prepare_origin_schedule,
                h, run_id, role, purpose, key, result)
            engine.check_active(run_id)
            return schedule(h, run_id, role, purpose, key, result, verified['problems'],
                correction_kind=origins.KEY, _origin_validation=verified)
        if role == 'engineer' and stage == 'implementation_spec' and str(exc).startswith(READ_PENDING):
            from .implementation_author_delivery_recovery import schedule as schedule_author_delivery
            if schedule_author_delivery(h, run_id, role, purpose, key, result, exc):
                return True
            from .implementation_author_inherited_delivery_recovery import prepare_schedule, commit_schedule
            inherited_plan = await engine._read_consensus_check(run_id, prepare_schedule,
                h, run_id, role, purpose, key, result, exc)
            if commit_schedule(h, run_id, role, purpose, key, result, inherited_plan):
                return True
            from .implementation_author_read_recovery import schedule as schedule_author_read
            if schedule_author_read(h, run_id, role, purpose, key, result, exc):
                return True
            # Missing other sources cannot be repaired by enlarging the saved
            # author's tool budget or repeatedly issuing its final answer.
            raise
        if getattr(exc, 'implementation_convergence_violation', False) is True:
            convergence_violation = True
            error = str(exc)
        if getattr(exc, 'indexed_review_read_pending', False) or getattr(exc, 'indexed_review_missing_document', None):
            indexed_missing_ranges = copy.deepcopy(getattr(exc, 'indexed_review_missing_ranges', []))
            if not indexed_missing_ranges:
                raise
            error = READ_PENDING + '；' + str(exc)
        reference_problems = getattr(exc, 'alignment_reference_problems', [])
        code_reference_problems = getattr(exc, 'code_reference_problems', [])
        repair_response_problems = getattr(exc, repair_responses.FIELD, [])
        format_problems = getattr(exc, 'alignment_findings_format_problems', [])
        if str(exc) == confirmations.BOUNDARY_ERROR:
            confirmation_problems = await engine._read_consensus_check(run_id, confirmations.problems,
                engine, run_id, role, key, result, context)
            if not confirmation_problems:
                raise
        elif (error is None and not str(exc).startswith(READ_PENDING) and not reference_problems
              and not format_problems and not code_reference_problems and not repair_response_problems):
            raise
        error = error or str(exc)
    engine.check_active(run_id)
    try:
        coordinate_problems = await engine._read_consensus_check(
            run_id, coordinates.problems, result, context, role)
    except ValueError as exc:
        raise ResearchBlocked(str(exc)) from exc
    engine.check_active(run_id)
    if repair_response_problems and (coordinate_problems or reference_problems or format_problems
            or confirmation_problems or convergence_violation or revision_problems or code_reference_problems):
        raise ResearchBlocked('代码意见补正同时出现其他来源或身份错误，原答复保留。')
    if code_reference_problems and (coordinate_problems or reference_problems or format_problems
                                    or confirmation_problems or convergence_violation or revision_problems):
        raise ResearchBlocked('代码引用补正同时出现其他格式或范围错误，原回复保留，未重新提交。')
    if convergence_violation and (indexed_missing_ranges or reference_problems or format_problems
                                  or confirmation_problems or coordinate_problems):
        raise ResearchBlocked('实现评议同时含复核范围和其他不支持的格式或来源错误，未登记批准；原候选保留。')
    if coordinate_problems:
        if format_problems or reference_problems or confirmation_problems:
            raise ResearchBlocked('实现评议同时含多种不支持的格式或来源错误，未登记批准；原候选保留。')
        error = coordinates.BOUNDARY_ERROR
    if error is None and stage == 'code_review' and any(
            finding.get('status') == 'pending' for finding in result.get('alignment_findings', [])):
        from .code_review_outcome import complete_rejection
        if not await engine._read_consensus_check(run_id, complete_rejection,
                engine, run_id, key, result, context):
            error = READ_PENDING + '；本次代码核对仍有 pending 项，请在同一交付上补读复核。'
    if error is None:
        return False
    if engine.store.get('decisions', key):
        raise ResearchBlocked('已保存的对齐交付不能用补读覆盖；请核对原交接。')
    progress = copy.deepcopy(engine.store.get('handoff_progress', key, {}))
    checkpoint = copy.deepcopy(progress)
    history = progress.get(HISTORY, [])
    turn = progress.get('turn', 0)
    if (not isinstance(history, list) or type(turn) is not int or turn < 0
            or progress.get('id', key) != key or progress.get('run_id', run_id) != run_id
            or progress.get('pending_tool_calls')):
        raise ResearchBlocked('对齐补读检查点身份或待处理工具状态不一致，未覆盖原请求。')
    sources = {name: copy.deepcopy(progress[name]) for name in ('normal_request', 'finalization')
               if name in progress}
    active_name = 'finalization' if progress.get('mode') == 'final_only' else 'normal_request'
    active_request = sources.get(active_name)
    if (isinstance(active_request, dict)
            and active_request.get('status') in {'dispatching', 'blocked', 'response_saved'}
            and active_request.get('response') is None):
        raise ResearchBlocked('原本机请求尚无已确认回复，不能用对齐补读重新提交；请先恢复原请求。')
    candidate_hash = digest(result)
    if (history and not sources and history[-1].get('candidate_sha256') == candidate_hash
            and history[-1].get('next_turn') == turn):
        return True  # Restart after saving the continuation, before dispatch.
    from .implementation_author_revision import continuation_limit
    continuation_cap = continuation_limit(context, progress) if revision_problems else LIMIT
    if continuation_cap is not None and len(history) >= continuation_cap:
        label = '作者完整修订' if revision_problems else '对齐依据修复'
        raise ResearchBlocked(label + '连续 ' + str(continuation_cap) + ' 次仍未完成，已保留原稿、候选与读取记录；未新增审核轮次。最后问题：' + error)
    attempt, next_turn = len(history) + 1, turn + 1
    was_final = progress.get('mode') == 'final_only'
    prior_limit = h._tool_turn_limit(purpose, context, progress)
    archived = {'attempt': attempt, 'candidate': copy.deepcopy(result),
        'candidate_sha256': candidate_hash, 'source_requests': sources,
        'source_mode': progress.get('mode'), 'source_turn': turn,
        'next_turn': next_turn, 'diagnostic': error, 'created_at': now(),
        **({'implementation_convergence_violation': True} if convergence_violation else {}),
        **({'author_revision_problems': copy.deepcopy(revision_problems)} if revision_problems else {}),
        **({'indexed_review_missing_ranges': indexed_missing_ranges} if indexed_missing_ranges else {}),
        **({'findings_format_problems': copy.deepcopy(format_problems)} if format_problems else {}),
        **({'routing_coordinate_problems': copy.deepcopy(coordinate_problems)} if coordinate_problems else {}),
        **({code_references.FIELD: copy.deepcopy(code_reference_problems),
            'code_reference_requests_sha256': digest(sources),
            'code_reference_context_sha256': digest(context)} if code_reference_problems else {}),
        **({repair_responses.FIELD: copy.deepcopy(repair_response_problems),
            'code_repair_requests_sha256': digest(sources),
            'code_repair_context_sha256': digest(context)} if repair_response_problems else {}),
        **({'confirmation_scope_problems': copy.deepcopy(confirmation_problems),
            'confirmation_source_requests_sha256': digest(sources),
            'confirmation_source_prepared_sha256': digest((active_request or {}).get('prepared')),
            'confirmation_source_tool_count': len(progress.get('tool_results', [])),
            'confirmation_source_tool_results_sha256': digest(progress.get('tool_results', []))}
           if confirmation_problems else {})}
    if format_problems:
        _verify_format_source(archived, role, purpose)
    if code_reference_problems:
        await engine._read_consensus_check(run_id, code_references.verify_archive,
            engine, run_id, role, purpose, key, context, archived)
    if repair_response_problems:
        repair_responses.verify_archive(engine, run_id, role, purpose, key, context, archived)
    if coordinate_problems:
        _verify_format_source(archived, role, purpose, problem_field='routing_coordinate_problems')
    if confirmation_problems:
        _verify_confirmation_source(engine, run_id, role, purpose, key, context, progress, archived)
    if revision_problems:
        from .implementation_author_revision import seal_rework
        archived['author_revision_audit'] = seal_rework(h, run_id, role, purpose, key, context, progress, archived)
    history = [*history, archived]
    progress[HISTORY] = history
    progress.setdefault('seen', {})
    if repair_response_problems:
        diagnostic = repair_responses.diagnostic(result, error, attempt, repair_response_problems)
    elif code_reference_problems:
        diagnostic = code_references.diagnostic(result, attempt, code_reference_problems)
    elif revision_problems:
        from .implementation_author_revision import diagnostic as revision_diagnostic
        diagnostic = revision_diagnostic(result, context, error, attempt, continuation_cap, revision_problems)
    elif convergence_violation:
        from .implementation_review_convergence import diagnostic as scope_diagnostic
        diagnostic = scope_diagnostic(result, context, error, attempt, LIMIT)
    else:
        diagnostic = (confirmations.diagnostic(result, attempt, LIMIT, confirmation_problems) if confirmation_problems else
                      coordinates.diagnostic(result, context, role, attempt, LIMIT) if coordinate_problems else
                      _format_diagnostic(result, error, attempt, format_problems) if format_problems else
                      _diagnostic(result, error, attempt, context, reference_problems, indexed_missing_ranges))
    from .code_review_context import enabled as code_context_enabled, missing_read_plan_async
    projection_baseline = None
    if (code_context_enabled(context, purpose)
            and diagnostic.get('host_diagnostic') == 'implementation_alignment_read_required'):
        diagnostic, projection_baseline = await missing_read_plan_async(
            h, run_id, key, context, diagnostic, checkpoint)
    progress.setdefault('tool_results', []).append(diagnostic)
    progress.update(turn=next_turn, pending_tool_calls=[], tool_cursor=0, tool_counted=False)
    for name in ('normal_request', 'finalization'):
        progress.pop(name, None)
    if was_final:
        progress.pop('mode', None)
    if was_final or turn >= prior_limit:
        progress['alignment_read_extension_until'] = max(
            progress.get('alignment_read_extension_until', 0), next_turn + 4)
    engine.check_active(run_id)
    if projection_baseline is not None:
        h._check_projection_state(run_id, key, projection_baseline)
        def save(current):
            if current != projection_baseline['checkpoint']:
                raise ResearchBlocked('Read-correction checkpoint changed; computed plan was not saved')
            return {**progress, 'id': key, 'run_id': run_id}
        engine.check_active(run_id)
        progress.update(engine.store.mutate('handoff_progress', key, save))
    else:
        h._save_progress(run_id, key, progress)
    return True
