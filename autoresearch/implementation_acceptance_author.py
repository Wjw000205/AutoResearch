"""Keep incomplete acceptance revisions with the author, before independent review."""
from __future__ import annotations

import copy
import json

from .implementation_review_routing import digest
from .architecture_validation import operation, once
from .storage import now
from .engine import ResearchBlocked

PROTOCOL = 'implementation_acceptance_author_protocol'
FIELD = 'acceptance_completion'
CONTINUATION = 'implementation_acceptance_author_continuation'
SUBMISSION_PROTOCOL = 'implementation_acceptance_author_submission_protocol'
GUIDANCE = '''
先看完 acceptance_issues 的全部意见及 close_when，再集中规划所有受影响的 document_index 单元。
一次 implementation.edit_document 同时输出 revision_plan 和 revisions：revision_plan 按本次处理的 issue_id
及关闭条件的原始 index 列明 action、index_ids 和修改说明；revisions 一次给齐本批 revise 单元的 value 或 text_replacements。
长文本局部修订用 before/after 精确替换旧段，保留其余正文；有依据的新定义应取代旧定义及过时的缺失声明，不能只追加。
同一单元涉及多个意见时合并修改，尽量一次给齐所有修改位置；允许先保存已完成部分。
already_satisfied 必须指出当前稿对应单元和已满足的具体依据；blocked 必须说明缺少的外部条件。
宿主分别记录成功保存、写入失败和尚未处理的意见；单项失败不回滚其他可独立保存的有效修改。
必须看 revision_progress：仅重试 failed_units，再补写 missing_conditions；不要重做已保存的内容。
相互依赖、必须一起才满足文档结构的修改会一起重试，不能保存结构已损坏的文档。
保存成功后按全部原意见逐条件检查；仍有失败或遗漏就继续补修，全部补齐后才提交独立审核。
保存中间草稿不代表完成。acceptance_completion 必须覆盖每个 issue_id 的每个关闭条件，
逐条给出 complete、incomplete 或 blocked、真实依据和当前稿原文引用。
complete 只代表作者自检全部落实，不是独立审核通过；部分落实或尚待编写必须填 incomplete。
blocked 仅用于明确缺少决定性来源或宿主依赖，并写清缺什么；不得用它跳过仍可完成的文档修改。
优先完成全部可修项，再报告确实被外部条件阻塞的项。不得编造定义、运行结果或批准。
复用已有作者记忆、工作笔记和当前草稿；只补读修改涉及且上下文缺少的精确范围，不要重读全文。
继承草稿和自检只是检查点，不是本交接的保存或阅读回执。续接提交前须由本交接成功保存当前稿，
并实际读取且获交付 patch_reports 引用的同版本范围；不得只返回父交接的 submission 指针。
无需正文修改时可用 revisions=[] 与真实 already_satisfied/blocked 的 revision_plan 登记同版保存。
局部轮次用尽时保留草稿和完整待办，在项目调用及运行时间预算内继续同一修订版本；全部条件自检完成后才提交独立审核。
'''


def enabled(context):
    return type(context.get(PROTOCOL)) is int and context[PROTOCOL] == 1


def tool_limit(context):
    if not enabled(context):
        return None
    count = sum(len(issue['close_when']) for issue in context['acceptance_issues'])
    return min(96, max(24, 3 * count + 8))


def patch_manifest(context, previous, current):
    """Version-bound cumulative change metadata, not body text or author claims."""
    from .implementation_patch_scope import diagnostic, allowed_fields
    allowed = allowed_fields(context)
    scope = diagnostic(previous['spec'], current['spec'], [], allowed)
    return {'baseline_document': previous['document'], 'baseline_spec_hash': previous['spec_hash'],
        'document': current['document'], 'spec_hash': current['spec_hash'],
        'actual_fields': scope['actual_fields'], 'actual_paths': scope['actual_paths'],
        'allowed_fields': sorted(allowed),
        'forbidden_fields': scope['forbidden_fields'], 'scientific_approval': False,
        'instruction': '这是相对上一轮送审稿的累计实际改动目录，不仅是最近一次 edit。'
            '最终 patch_reports 必须按原问题申报全部实际改动；不要为补报告重写已保存正文。'
            '本目录不代表问题已解决，也不提供阅读或自检信用。完成剩余修改后核对最新 SHA，'
            '复用已交付范围，只补读尚未交付的自检范围。'
            '若 forbidden_fields 非空，须将范围外内容恢复上一送审稿；保留其余有效修改。'}


def plan_schema():
    from . import implementation_acceptance as policy
    condition = policy.obj({'index': {'type': 'integer', 'minimum': 0},
        'action': {'enum': ['revise', 'already_satisfied', 'blocked']},
        'index_ids': {'type': 'array', 'minItems': 1, 'uniqueItems': True, 'items': policy.TEXT},
        'reason': policy.TEXT})
    return policy.array(policy.obj({'issue_id': policy.TEXT, 'conditions': policy.array(condition, 1)}), 1)


def unchanged_save_retry(engine, hid, context, base):
    """Expose a known failed no-content save; never replay it or invent a receipt."""
    if CONTINUATION not in context:
        return None
    rows = engine.store.get('handoff_progress', hid, {}).get('tool_results', [])
    for receipt in reversed(rows):
        call, result = receipt.get('call', {}), receipt.get('result', {})
        if call.get('name') != 'implementation.edit_document':
            continue
        args = call.get('arguments', {})
        if (not isinstance(result, dict)
                or result.get('error') != '已保存输出内容发生变化，不能覆盖原文件。'
                or args.get('revisions') != [] or not args.get('revision_plan')
                or args.get('document_id') != base['document']['document_id']
                or args.get('expected_sha256') != base['document']['sha256']
                or args.get('base_spec_hash') != base['spec_hash']):
            return None
        return {'call': copy.deepcopy(call), 'failed_receipt_sha256': digest(receipt),
            'saved': False, 'scientific_approval': False,
            'instruction': '上次仅登记条件、正文不变的保存因续接目录变化而失败；宿主已修复原文档复用。'
                '请重试此调用以取得本交接真实保存回执，不要为避开错误改写正文。'
                '原失败回执保留，本提示不是保存成功或自检完成。'}
    return None


def validate_batch(context, base, args):
    """Check identity of a partial plan, without requiring all opinions at save time."""
    from .implementation_authoring_preservation import SaveConflict
    from .implementation_authoring import edit_arguments_schema
    from jsonschema import ValidationError
    from .prose_schema import validate as validate_schema
    try:
        validate_schema(args, edit_arguments_schema(context))
    except ValidationError as exc:
        raise SaveConflict('incomplete_revision_batch',
            '修订须提供本次 revision_plan 和 revisions；可只保存已完成部分。') from exc
    issues = {issue['issue_id']: issue for issue in context['acceptance_issues']}
    plans = args['revision_plan']
    if len({p['issue_id'] for p in plans}) != len(plans) or not {p['issue_id'] for p in plans} <= set(issues):
        raise SaveConflict('invalid_revision_plan', '修改计划的问题 ID 未知或重复。')
    planned = set()
    for plan in plans:
        conditions = plan['conditions']
        expected = set(range(len(issues[plan['issue_id']]['close_when'])))
        if len({c['index'] for c in conditions}) != len(conditions) or not {c['index'] for c in conditions} <= expected:
            raise SaveConflict('invalid_revision_plan', '计划引用了未知或重复的关闭条件。',
                               issue_id=plan['issue_id'])
        for condition in conditions:
            if condition['action'] == 'revise':
                planned.update(condition['index_ids'])
    actual = {revision['index_id'] for revision in args['revisions']}
    if actual - planned:
        raise SaveConflict('invalid_revision_plan', '本次修改需关联到原意见中的具体关闭条件。',
                           unplanned_index_ids=sorted(actual - planned))


def submission_schema(base, context):
    if not enabled(context):
        return base
    from . import implementation_acceptance as policy
    from .implementation_protocol_contracts import author_protocols
    try:
        submission_protocol = author_protocols(context).get(SUBMISSION_PROTOCOL)
    except ValueError as exc:
        policy.fail(str(exc))
    result = copy.deepcopy(base)
    check = policy.obj({'index': {'type': 'integer', 'minimum': 0},
        'status': {'enum': ['complete', 'incomplete', 'blocked']},
        'explanation': policy.TEXT, 'evidence': policy.array(policy.REF)})
    entry = policy.obj({'issue_id': policy.TEXT, 'conditions': policy.array(check, 1)})
    result['properties'][FIELD] = policy.array(entry, 1)
    if submission_protocol == 1:
        result['properties'][FIELD]['description'] = (
            '作者检查点不是提交许可；最终提交须有本交接同版本成功保存及报告引用范围的真实阅读交付。'
            '继承自检可按原证据复用，但不能替代保存或 patch_reports 阅读回执。')
    result['required'].append(FIELD)
    return result


class IncompleteRevision(ResearchBlocked):
    def __init__(self, pending):
        self.pending = pending
        super().__init__('实现文档仍有未完成修改；保留当前草稿，继续作者修订，未提交独立审核。')


class PatchScopeRevision(IncompleteRevision):
    """An authenticated draft/report needs author work, never a scope waiver."""
    def __init__(self, context, previous, row, diagnostic, message):
        from .implementation_patch_scope import repair_metadata
        pending = [{'issue_id': issue['issue_id'], 'condition_index': index,
            'requirement': condition, 'status': 'incomplete',
            'reason': '补正累计修改范围或补丁声明后，重新检查原关闭条件；保留有效修改。'}
            for issue in context['acceptance_issues'] for index, condition in enumerate(issue['close_when'])]
        if not pending:
            raise ResearchBlocked(message)
        pending[0]['submission_scope_repair'] = copy.deepcopy(repair_metadata(context, previous, row, diagnostic))
        super().__init__(pending)
        self.scope_diagnostic = copy.deepcopy(diagnostic)
        self.args = (message,)


class ReportOnlyNeedsSave(ResearchBlocked):
    """A valid old reply needs fresh author work, never parent acceptance."""


def validate(engine, run_id, row, hid, result, context):
    from . import implementation_acceptance as policy
    from .implementation_acceptance_flow import validate_refs
    expected = {issue['issue_id']: issue for issue in context['acceptance_issues']}
    answers = result.get(FIELD, [])
    if enabled(context) and (len(answers) != len(expected)
            or {answer['issue_id'] for answer in answers} != set(expected)):
        policy.fail('作者完成自检必须唯一覆盖本轮全部已裁决问题。')
    indexed = {answer['issue_id']: answer for answer in answers}
    current = {**context, 'indexed_documents': [*context['indexed_documents'], row['document']]}
    pending = []
    from .implementation_revision_batch import status
    from .implementation_authoring import COLLECTION
    saved_progress = status(engine, hid, context, engine.store.get(COLLECTION, hid)) if enabled(context) else {}
    missing = {(p['issue_id'], p['condition_index']) for p in saved_progress.get('missing_conditions', [])}
    from .implementation_self_check import reusable, reuse_candidates
    checkpoints = reuse_candidates(engine, run_id, hid, context, row)
    from .implementation_condition_roles import review_keys
    reviewer_conditions = review_keys(context)
    for iid, issue in expected.items():
        conditions = indexed.get(iid, {}).get('conditions', [])
        if enabled(context) and (len(conditions) != len(issue['close_when'])
                or {c['index'] for c in conditions} != set(range(len(issue['close_when'])))):
            policy.fail('作者完成自检必须逐条覆盖原 close_when，不能合并、遗漏或重复。')
        by_index = {condition['index']: condition for condition in conditions}
        for index, requirement in enumerate(issue['close_when']):
            answer = by_index.get(index, {})
            if (iid, index) in reviewer_conditions:
                continue  # Still required in independent VERIFY, never author approval.
            if answer.get('status') == 'complete' and (iid, index) not in missing:
                if not reusable(engine, run_id, hid, context, row, iid, answer, candidates=checkpoints):
                    try:
                        validate_refs(engine, run_id, hid, current, answer['evidence'], current=row['document'],
                                      field=f'{FIELD}[{iid}].conditions[{index}].evidence')
                    except ResearchBlocked as exc:
                        # A valid saved check survives, but this final answer is not
                        # an exact reuse and does not establish new current evidence.
                        # Return only this report item to its author; never approve it
                        # or replace the immutable paid answer with the old check.
                        if not any(e['check']['issue_id'] == iid
                                   and e['check']['index'] == index
                                   and e['check']['status'] == 'complete' for e in checkpoints):
                            raise
                        pending.append({'issue_id':iid, 'condition_index':index,
                            'requirement':requirement, 'status':'incomplete',
                            'report_repair_only':True,
                            'reason':'已保存的逐条自检仍有效，但最终报告改写了结论或引用，无法按原记录复用。'
                                '请原样复制 implementation_author_self_check.valid 中该条结论及 evidence；'
                                '若要更改结论，则须先完成当前版本的真实证据核对。'
                                '不要为修正报告重写已完成正文。原校验错误：' + str(exc)})
            else:
                pending.append({'issue_id': iid, 'condition_index': index, 'requirement': requirement,
                    'status': answer.get('status', 'incomplete') if answer.get('status') != 'complete' else 'incomplete',
                    'reason': answer.get('explanation', '旧提交没有逐条件完成自检；须核对当前稿并完成原修改要求。')})
    if saved_progress.get('failed_units') and not pending:
        issue = next(iter(expected.values()))
        pending.append({'issue_id': issue['issue_id'], 'condition_index': 0, 'requirement': issue['close_when'][0],
            'status': 'incomplete', 'reason': '仍有保存失败的索引单元，须先重试：' +
                ', '.join(unit['index_id'] for unit in saved_progress['failed_units'])})
    if pending:
        raise IncompleteRevision(pending)


def _workflow_scope(state):
    return {**{key: state.get(key) for key in ('id', 'run_id', 'current_revision', 'input_hash',
        'proposal_hash', 'design_hash', 'experiment_plan_hash', 'implementation_generation')},
        **{key + '_sha256': digest(state.get(key)) for key in ('specifications', 'acceptance_review', 'common')}}


def _source_records(store, hid):
    from .implementation_authoring import COLLECTION
    records = {col: _canonical_source(col, store.get(col, hid))
            for col in ('handoffs', 'handoff_inputs', 'handoff_progress', 'decisions', COLLECTION)}
    from .implementation_self_check import COLLECTION as CHECKS
    if store.get(CHECKS, hid):
        records[CHECKS] = store.get(CHECKS, hid)
    return records


def _canonical_source(collection, record):
    if collection == 'handoffs' and record:
        # Cache readback updates these observational fields, never the paid reply.
        return {key: value for key, value in record.items()
                if key not in {'updated_at', 'reused_count', 'legacy_decision_reused'}}
    return record


@once
def _matches_source(engine, run_id, state, hid, source):
    """Immutable audit bytes plus the ordinary submission's exact enrichment.

    On replay, submit may have marked the original draft submitted. Admit only
    that deterministic enrichment, revalidated through the real submission
    gate; never normalize paid inputs, replies, receipts or checkpoint bytes.
    """
    from . import implementation_authoring as author
    current = _source_records(engine.store, hid)
    if current == source:
        return True
    original, submitted = source.get(author.COLLECTION), current.get(author.COLLECTION)
    if not original or not submitted or submitted.get('status') != 'submitted':
        return False
    expected = copy.deepcopy(original)
    result = (source.get('decisions') or {}).get('result')
    expected.update(status='submitted', updated_at=submitted.get('updated_at'),
                    acceptance_patch_submission=result)
    if 'acceptance_parent_selection' in submitted:
        expected['acceptance_parent_selection'] = submitted['acceptance_parent_selection']
    if submitted != expected or {**current, author.COLLECTION: original} != source:
        return False
    return author._prepare_submission(engine, run_id, state, hid, result)['record'] == submitted


def continuation_base(engine, run_id, state, context):
    """Only a checksum-bound original draft can seed a continuation's first edit."""
    from .implementation_authoring import COLLECTION, _checked
    from .engine import ResearchBlocked
    ref = context[CONTINUATION]
    audit = engine.store.get('stage_contexts', ref['audit_id'], {})
    payload = audit.get('payload', {})
    if (audit.get('run_id') != run_id or audit.get('sha256') != ref['sha256']
            or digest(payload) != ref['sha256'] or payload.get('workflow') != _workflow_scope(state)
            or audit.get('successor_context_sha256') != digest(context)
            or not _matches_source(engine, run_id, state, ref['source_handoff_id'], payload.get('source', {}))
            or payload.get('source', {}).get(COLLECTION, {}).get('row', {}).get('spec_hash') != ref['spec_hash']):
        raise ResearchBlocked('作者续接的原草稿、提交或冻结范围已改变，未继续修改。')
    if payload.get('supplement_repair'):
        from .implementation_supplement_repair import validate_adoption
        validate_adoption(engine, run_id, state, payload['supplement_repair'])
    if payload.get('submission_recovery'):
        from .implementation_author_submission_recovery import validate
        validate(engine, run_id, state, payload)
    from .implementation_patch_scope import validate_repair
    validate_repair(context, state, payload['source'], payload.get('pending', []))
    return copy.deepcopy(_checked(engine, run_id, payload['source'][COLLECTION])['row'])


def _blocked_message(pending, context):
    from .implementation_condition_roles import review_pending
    review = review_pending(context)
    return ('作者修订缺少决定性依据；草稿已保留，未提交审核：' +
            '；'.join(item['reason'] for item in pending[:6]) +
            (f'。另有{len(review)}条属于后续职责审查者独立复核，不计为作者阻塞。' if review else ''))


def _report_inherited_blockers(engine, run_id, state, hid, result, context):
    """Classify an authenticated blocked report; never authorize submission."""
    from . import implementation_authoring as author
    from .prose_schema import validate as validate_schema
    if CONTINUATION not in context or engine.store.get(author.COLLECTION, hid):
        return
    assignment, frozen, bound_state, _ = author._assignment(engine, run_id, 'engineer', hid)
    inputs = engine.store.get('handoff_inputs', hid, {})
    decision = engine.store.get('decisions', hid, {})
    if (assignment.get('status') != 'completed' or frozen != context
            or _workflow_scope(bound_state) != _workflow_scope(state)
            or assignment.get('input_hash') != digest({k: inputs.get(k) for k in ('instruction', 'context', 'schema')})
            or assignment.get('output_hash') != digest(result)
            or decision.get('run_id') != run_id or decision.get('role') != 'engineer'
            or decision.get('result') != result):
        raise ResearchBlocked('继承稿阻塞报告缺少已认证的工程师最终交付。')
    validate_schema(result, inputs['schema'])
    base = continuation_base(engine, run_id, state, context)
    if {k: result.get(k) for k in author.SUBMISSION['properties']} != author.pointer({'row': base}):
        raise ResearchBlocked('继承稿阻塞报告与已保存文档版本不一致。')
    try:
        validate(engine, run_id, base, hid, result, context)
    except IncompleteRevision as exc:
        if exc.pending and all(item['status'] == 'blocked' for item in exc.pending):
            raise ResearchBlocked(_blocked_message(exc.pending, context)) from exc
    # Incomplete or all-complete reports still use the strict own-save gate.


def _existing_continuation(engine, run_id, state, hid, result):
    """Follow a frozen dispatch edge before applying newer author-routing rules."""
    audit_id = hid + ':acceptance-author-continuation'
    audit = engine.store.get('stage_contexts', audit_id)
    if not audit:
        return None
    payload = audit.get('payload', {})
    source_id = hid
    source_result = (payload.get('source', {}).get('decisions') or {}).get('result')
    if payload.get('submission_recovery'):
        from .implementation_author_submission_recovery import validate
        validate(engine, run_id, state, payload)
        recovery = payload['submission_recovery']
        if recovery['handoff_id'] != hid:
            raise ResearchBlocked('作者提交修复的失败交接身份已改变。')
        source_id = recovery['base_handoff_id']
        source_result = recovery['records']['decisions']['result']
    successor = audit.get('successor_id', '')
    inputs = engine.store.get('handoff_inputs', successor, {})
    assignment = engine.store.get('handoffs', successor, {})
    context = inputs.get('context', {})
    ref = context.get(CONTINUATION, {})
    from .implementation_authoring import COLLECTION, _checked
    if (audit.get('id') != audit_id or audit.get('run_id') != run_id or audit.get('sha256') != digest(payload)
            or not _matches_source(engine, run_id, state, source_id, payload.get('source', {}))
            or payload.get('workflow') != _workflow_scope(state)
            or source_result != result
            or inputs.get('run_id') != run_id or assignment.get('run_id') != run_id
            or assignment.get('to_role') != 'engineer'
            or assignment.get('input_hash') != digest({k:inputs.get(k) for k in ('instruction','context','schema')})
            or audit.get('successor_context_sha256') != digest(context)
            or ref.get('audit_id') != audit_id or ref.get('sha256') != audit['sha256']
            or ref.get('source_handoff_id') != source_id):
        raise ResearchBlocked('已有作者续接的冻结来源或交接绑定已改变，未重建或派发。')
    _checked(engine, run_id, payload['source'][COLLECTION])
    continuation_base(engine, run_id, state, context)
    return {'source_id':source_id, 'payload':copy.deepcopy(payload), 'audit_id':audit_id,
        'sha256':audit['sha256'], 'context':copy.deepcopy(context),
        'instruction':inputs['instruction'], 'purpose':assignment['purpose'], 'schema':copy.deepcopy(inputs['schema']),
        'run_guard':{k:engine.store.get('runs',run_id).get(k) for k in ('status','config','state')}}


def _child_has_followup(store, run_id, hid):
    """Look up controller-owned edges, never select a successor by timestamp."""
    return bool(store.get('stage_contexts', hid + ':acceptance-author-continuation')
        or store.record_keys('stage_contexts', run_id, predicate=(
            "(json_extract(data,'$.payload.source.handoffs.id')=? "
            "OR json_extract(data,'$.source_handoff_id')=?)"), parameters=[hid, hid], limit=1))


def _report_commit_records(engine, run_id, cid, progress, reply):
    """A host closeout is evidence to authenticate, never child reading credit."""
    from .working_context import validate_first_turn
    from jsonschema import ValidationError
    commit_id = progress.get('working_context_commit')
    keys = engine.store.record_keys('context_commits', run_id,
        predicate="json_extract(data,'$.handoff_id')=?", parameters=[cid])
    if set(keys) != ({commit_id} if commit_id else set()):
        raise ResearchBlocked('报告续接的上下文提交缺失或已有其他轮次。')
    if 'working_context_commit' not in progress:
        if progress.get('normal_request') is not None:
            raise ResearchBlocked('报告续接的普通请求缺少可认证的上下文提交。')
        return []  # Legacy tool_request-only delivery did not produce a commit.
    normal = progress.get('normal_request') or {}
    try:
        prepared = normal['prepared']
        response = normal['response']
        child = engine.store.get('handoffs', cid, {})
        if (not commit_id or normal.get('status') != 'response_saved'
                or normal.get('turn') != 0 or normal.get('prepared_sha256') != digest(prepared)
                or response.get('json') != reply
                or prepared['context'].get('_purpose') != child.get('purpose')
                or len(child.get('model_calls', [])) != 1
                or any(child['model_calls'][0].get(k) != response.get(k) for k in
                       ('provider', 'model', 'request_id', 'transcript_id', 'provenance', 'cache_hit'))):
            raise ValueError('Working closeout request/response identity mismatch')
        return validate_first_turn(engine.store, run_id, 'engineer', cid, prepared, reply,
                                   progress, response_id=response.get('request_id'))
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise ResearchBlocked('报告续接的上下文提交不能认证：' + str(exc)) from exc


def _report_only_child(engine, run_id, state, hid, result, plan, *, parent_prepared=False):
    """Conservative, read-only compatibility proof for a completed old edge.

    No child evidence is transferred to the parent. Unknown/new work uses the
    existing continuation path. Semantic or identity conflicts fail closed.
    """
    from . import implementation_authoring as author
    from . import implementation_acceptance as policy
    from . import implementation_self_check as checks
    from .harness_delivery import authenticate
    from .prose_schema import validate as validate_schema
    from .implementation_acceptance_flow import validate_refs
    from .implementation_patch_scope import diagnostic
    audit = engine.store.get('stage_contexts', plan['audit_id'])
    cid = audit['successor_id']
    child = engine.store.get('handoffs', cid, {})
    progress = engine.store.get('handoff_progress', cid, {})
    if (child.get('status') != 'completed' or not progress
            or progress.get('turn') != 0 or progress.get('tool_results')
            or progress.get('pending_tool_calls') or progress.get('tool_cursor', 0) != 0
            or progress.get('tool_counted') or progress.get('seen')
            or progress.get('mode') or progress.get('finalization')
            or engine.store.get(author.COLLECTION, cid) or engine.store.get(checks.COLLECTION, cid)
            or engine.store.get('stage_contexts', cid + ':acceptance-author-continuation')
            or engine.store.record_keys('tool_invocations', run_id,
                predicate="json_extract(data,'$.handoff_id')=?", parameters=[cid])):
        return False
    # Any other program-owned successor/adoption is work, even without tools.
    if _child_has_followup(engine.store, run_id, cid):
        return False
    frozen = engine.store.get('handoff_inputs', cid, {})
    reply = engine.store.get('decisions', cid, {}).get('result')
    child_context = authenticate(engine, run_id, 'engineer', cid, reply)
    parent_context = authenticate(engine, run_id, 'engineer', hid, result)
    validate_schema(reply, frozen['schema'])  # The actual author schema, never a review schema.
    validate_schema(result, engine.store.get('handoff_inputs', hid)['schema'])
    assignment, bound_context, bound_state, binding = author._assignment(engine, run_id, 'engineer', cid)
    if (bound_context != child_context or _workflow_scope(bound_state) != _workflow_scope(state)
            or binding != parent_context.get('implementation_document_authoring')):
        raise ResearchBlocked('报告续接的工作流、修订或作者任务已改变。')
    # Only fields the continuation constructor derives may differ. Every
    # scientific source, role, issue, contract and task binding remains exact.
    derived = {CONTINUATION, PROTOCOL, SUBMISSION_PROTOCOL, author.EDIT_PROTOCOL, 'implementation_document_locate_protocol',
        'acceptance_author_pending', 'acceptance_author_saved_progress', 'acceptance_author_checkpoint',
        'indexed_documents', 'implementation_document_completion_protocol', 'implementation_document_completion',
        'implementation_phase_context_protocol', 'implementation_phase_context', 'implementation_document_repair_reads'}
    from .implementation_issue_output_capacity import PROTOCOL as OUTPUT_PROTOCOL, inventory_enabled
    # Output capacity is the only new protocol metadata a successor may add;
    # it does not change any source, issue, dependency, or original evidence.
    # Unknown versions and a downgrade remain incompatible, including replay.
    parent_inventory = inventory_enabled(parent_context)
    child_inventory = inventory_enabled(child_context)
    if parent_inventory and not child_inventory:
        raise ResearchBlocked('报告续接不能移除完整原义务输出协议。')
    derived.add(OUTPUT_PROTOCOL)
    from .implementation_patch_scope import CONTEXT as SCOPE_CONTEXT, allowed_fields
    if (SCOPE_CONTEXT in parent_context and SCOPE_CONTEXT not in child_context
            or allowed_fields(parent_context) != allowed_fields(child_context)):
        raise ResearchBlocked('报告续接不能移除或改变冻结修订范围目录。')
    derived.add(SCOPE_CONTEXT)
    if (child_context.get(SUBMISSION_PROTOCOL) not in (parent_context.get(SUBMISSION_PROTOCOL), 1)
            or {k:v for k,v in child_context.items() if k not in derived}
            != {k:v for k,v in parent_context.items() if k not in derived}):
        raise ResearchBlocked('报告续接的原问题、来源或科学范围已改变。')
    draft = continuation_base(engine, run_id, state, child_context)
    documents = {d['document_id']: d for d in parent_context['indexed_documents']}
    documents[draft['document']['document_id']] = draft['document']
    if child_context['indexed_documents'] != list(documents.values()):
        raise ResearchBlocked('报告续接增加或修改了来源版本。')
    # Explicitly reject unknown recovery/progress states. Normal direct finals
    # may use the durable request ledger or the historical tool_request packet.
    known = {'id', 'run_id', 'turn', 'seen', 'tool_results', 'pending_tool_calls', 'tool_cursor',
             'tool_counted', 'normal_request', 'tool_request', 'working_context_commit', 'created_at', 'updated_at'}
    if set(progress) - known:
        return False
    if len(child.get('model_calls', [])) != 1 or child['model_calls'][0].get('json_format_repair'):
        return False  # Multiple/rewritten answers are not a proven report-only turn.
    normal = progress.get('normal_request')
    if normal is not None:
        normal_fields = {'status', 'turn', 'operation_id', 'prepared', 'prepared_sha256',
            'provider_id', 'model', 'created_at', 'dispatched_at', 'response', 'received_at'}
        if set(normal) - normal_fields:
            return False  # Retry, recovery and adverse metadata need their own normal route.
        if (normal.get('status') != 'response_saved' or normal.get('turn') != 0
                or normal.get('prepared_sha256') != digest(normal.get('prepared'))
                or normal.get('response', {}).get('json') != reply):
            raise ResearchBlocked('报告续接缺少已完成的原始请求答复。')
        response = normal['response']
        metadata = response.get('response_metadata') or {}
        if (response.get('error') or response.get('json_format_repair')
                or metadata.get('status', 'completed') != 'completed'
                or any(metadata.get(k) for k in ('error', 'error_code', 'incomplete_details',
                                                'public_output_truncated', 'native_tool_violation'))):
            return False
    elif not progress.get('tool_request'):
        return False
    request = progress.get('tool_request')
    if request and (set(request) != {'turn', 'prepared', 'prepared_sha256'}
            or request.get('turn') != 0 or request.get('prepared_sha256') != digest(request.get('prepared'))
            or normal is not None and request['prepared'] != normal['prepared']):
        raise ResearchBlocked('报告续接的冻结请求已改变。')
    prepared = (normal or request)['prepared']
    if (prepared.get('role') != 'engineer'
            or prepared.get('context', {}).get('_purpose') != child.get('purpose')
            or prepared['context'].get('implementation_document_authoring')
                != child_context.get('implementation_document_authoring')):
        raise ResearchBlocked('报告续接的请求角色或任务身份已改变。')
    if prepared['context'].get('_harness_control', {}).get('mode') not in (None, 'continue'):
        return False
    # Dispatch uses a tool/final anyOf envelope; the frozen handoff above uses
    # the author final schema. Validate both, without imposing review fields.
    validate_schema(reply, prepared['schema'])
    _report_commit_records(engine, run_id, cid, progress, reply)
    # The parent must independently pass every current save/read/scope gate.
    if not parent_prepared:
        author._prepare_submission(engine, run_id, state, hid, result, _selection_checked=True)
    if {k:v for k,v in reply.items() if k not in {'patch_reports', FIELD}} != {
            k:v for k,v in result.items() if k not in {'patch_reports', FIELD}}:
        raise ResearchBlocked('报告续接不是同一已保存文档的报告调整。')
    issues = {i['issue_id'] for i in parent_context['acceptance_issues']}
    def indexed(values, key, expected):
        if len(values) != len(expected) or {v[key] for v in values} != expected:
            raise ReportOnlyNeedsSave('报告续接遗漏、重复或新增了原问题或条件。')
        return {v[key]:v for v in values}
    reading = {**parent_context, 'indexed_documents':list(documents.values())}
    def evidence(refs, original, *, current=True):
        if original and not refs:
            raise ReportOnlyNeedsSave('报告续接遗漏原证据。')
        for ref in refs:
            if not any(all(ref[k] == prior[k] for k in ('document_id', 'document_sha256', 'section_id'))
                    and prior['offset'] <= ref['offset']
                    and ref['offset'] + ref['limit'] <= prior['offset'] + prior['limit'] for prior in original):
                raise ReportOnlyNeedsSave('报告续接引用超出原报告同版本证据。')
        if current and (refs or original):
            validate_refs(engine, run_id, hid, reading, refs, current=draft['document'])
    parents = indexed(result['patch_reports'], 'issue_id', issues)
    children = indexed(reply['patch_reports'], 'issue_id', issues)
    for iid, child_report in children.items():
        original = parents[iid]
        if {k:v for k,v in child_report.items() if k not in {'changed_fields','evidence'}} != {
                k:v for k,v in original.items() if k not in {'changed_fields','evidence'}}:
            raise ReportOnlyNeedsSave('报告续接包含新的影响或修订结论。')
        evidence(child_report['evidence'], original['evidence'])
    before = next(r for r in state['specifications'] if r['index'] == draft['index'] - 1)['spec']
    from .implementation_patch_scope import allowed_fields
    allowed = allowed_fields(parent_context)
    if not diagnostic(before, draft['spec'], reply['patch_reports'], allowed)['valid']:
        raise ReportOnlyNeedsSave('报告续接声明不匹配真实累计改动或全批次范围。')
    parents = indexed(result[FIELD], 'issue_id', issues)
    children = indexed(reply[FIELD], 'issue_id', issues)
    for issue in parent_context['acceptance_issues']:
        iid = issue['issue_id']
        expected = set(range(len(issue['close_when'])))
        old = indexed(parents[iid]['conditions'], 'index', expected)
        new = indexed(children[iid]['conditions'], 'index', expected)
        for index, check in new.items():
            if {k:v for k,v in check.items() if k != 'evidence'} != {
                    k:v for k,v in old[index].items() if k != 'evidence'}:
                raise ReportOnlyNeedsSave('报告续接包含新的完成结论或未决信息。')
            # Subranges may be shortened, but the original attestation must
            # still be reusable against its original dependency and receipts.
            # Never turn an old checkpoint into current-document read credit.
            evidence(check['evidence'], old[index]['evidence'], current=False)
            if not checks.reusable(engine, run_id, hid, parent_context, draft, iid, old[index]):
                evidence(check['evidence'], old[index]['evidence'])
    return True


def submission_selection(engine, run_id, state, hid, result, record):
    """Bind selection to immutable evidence and guard it again at submit/replay."""
    from . import implementation_acceptance as policy
    audit_id = hid + ':acceptance-author-continuation'
    audit = engine.store.get('stage_contexts', audit_id)
    previous = record.get('acceptance_parent_selection')
    if not audit:
        if previous:
            raise ResearchBlocked('原父提交选择缺少续接审计边。')
        return None, []
    cid = audit['successor_id']
    if _child_has_followup(engine.store, run_id, cid):
        raise ResearchBlocked('报告续接已有后续工作，不能复用原父提交选择。')
    child_records = _source_records(engine.store, cid)
    context_records = _report_commit_records(engine, run_id, cid,
        child_records.get('handoff_progress') or {}, (child_records.get('decisions') or {}).get('result'))
    payload = {'version':1, 'source_handoff_id':hid, 'source_result_sha256':digest(result),
        'audit_id':audit_id, 'audit_sha256':digest(audit), 'child_id':cid,
        'child_records_sha256':{col:digest(value) for col,value in child_records.items()},
        'scientific_approval':False, 'reading_credit':False}
    if context_records:
        payload['context_records_sha256'] = [
            {'collection': col, 'id': key, 'sha256': digest(value)} for col, key, value in context_records]
    selection = policy.sealed(payload)
    if previous:
        if previous != selection:
            raise ResearchBlocked('原父提交选择的续接输入、答复或回执已改变。')
    else:
        plan = _existing_continuation(engine, run_id, state, hid, result)
        if not _report_only_child(engine, run_id, state, hid, result, plan, parent_prepared=True):
            raise ResearchBlocked('续接仍有新工作或未确定状态，不能选择原父提交。')
    if engine.store.record_keys('tool_invocations', run_id,
            predicate="json_extract(data,'$.handoff_id')=?", parameters=[cid]):
        raise ResearchBlocked('报告续接出现工具工作，不能复用原父提交选择。')
    guards = [('stage_contexts', audit_id, audit),
              ('stage_contexts', cid + ':acceptance-author-continuation', None)]
    # Use the actual records for CAS, including observational fields.
    guards.extend((col, cid, engine.store.get(col, cid)) for col in child_records)
    guards.extend(context_records)
    from .implementation_self_check import COLLECTION as CHECKS
    if CHECKS not in child_records:
        guards.append((CHECKS, cid, None))
    return selection, guards


@operation
def prepare(engine, run_id, state, hid, result):
    """Read-only source validation; completed paid replies remain immutable."""
    from . import implementation_authoring as author
    from .engine import ResearchBlocked
    inputs = engine.store.get('handoff_inputs', hid, {})
    context = inputs.get('context', {})
    if context.get('implementation_acceptance_protocol') != 1 or not context.get('implementation_document_authoring', {}).get('revision'):
        return None
    existing = _existing_continuation(engine, run_id, state, hid, result)
    if existing is not None:
        try:
            if _report_only_child(engine, run_id, state, hid, result, existing):
                return None
        except IncompleteRevision:
            pass  # Still-editable parent work stays on the original bounded edge.
        except ReportOnlyNeedsSave:
            pass  # Follow the existing child, then freeze fresh own-save work.
        return existing
    _report_inherited_blockers(engine, run_id, state, hid, result, context)
    if CONTINUATION in context and not engine.store.get(author.COLLECTION, hid):
        from .implementation_author_submission_recovery import prepare as prepare_recovery
        return prepare_recovery(engine, run_id, state, hid, result, context)
    try:
        from .implementation_issue_output_capacity import insufficient
        if any(insufficient(inputs['schema'], field, len(context['acceptance_issues']))
               for field in ('patch_reports', FIELD)):
            # A new durable author assignment owns the complete output. This
            # branch grants no save/read credit and never accepts the parent.
            raise IncompleteRevision([{'issue_id': issue['issue_id'], 'condition_index': index,
                'requirement': condition, 'status': 'incomplete', 'report_repair_only': True,
                'reason': '原冻结输出容量不能表示完整原义务；续接保留全部问题、保存版本和自检，补齐完整报告。'}
                for issue in context['acceptance_issues'] for index, condition in enumerate(issue['close_when'])])
        author._prepare_submission(engine, run_id, state, hid, result)
        return None
    except IncompleteRevision as exc:
        # Includes authenticated cumulative scope/report defects. Identity,
        # frozen-source and receipt failures are not author-editable exceptions.
        pending = exc.pending
    # Attempt all still-editable conditions before stopping on an external blocker.
    if all(item['status'] == 'blocked' for item in pending):
        raise ResearchBlocked(_blocked_message(pending, context))
    source = _source_records(engine.store, hid)
    return continuation_plan(engine, run_id, state, hid, context, source, pending)


def continuation_plan(engine, run_id, state, hid, context, source, pending, *, recovery=None):
    """Construct a new author packet; all previous packets remain immutable."""
    from . import implementation_authoring as author
    from .implementation_patch_scope import validate_repair
    validate_repair(context, state, source, pending)
    depth = context.get(CONTINUATION, {}).get('depth', 0) + 1
    payload = {'source': source, 'workflow': _workflow_scope(state), 'pending': pending}
    if recovery:
        payload['submission_recovery'] = recovery
    checksum = digest(payload)
    audit_id = (recovery['handoff_id'] if recovery else hid) + ':acceptance-author-continuation'
    draft = source[author.COLLECTION]['row']
    next_context = copy.deepcopy(context)
    # The new assignment has its own edit budget and source scope. The old save
    # request and its four-turn contract remain intact in the source archive.
    for key in ('implementation_document_completion_protocol', 'implementation_document_completion',
                'implementation_phase_context_protocol', 'implementation_phase_context',
                'implementation_document_repair_reads'):
        next_context.pop(key, None)
    next_context.update({PROTOCOL: 1, SUBMISSION_PROTOCOL: 1, author.EDIT_PROTOCOL: author.EDIT_VERSION,
        'implementation_document_locate_protocol': 1,
        CONTINUATION: {'audit_id': audit_id, 'sha256': checksum, 'source_handoff_id': hid,
            'spec_hash': draft['spec_hash'], 'depth': depth, 'document': draft['document']},
        'acceptance_author_pending': pending})
    from .implementation_issue_output_capacity import PROTOCOL as OUTPUT_PROTOCOL
    next_context[OUTPUT_PROTOCOL] = 1
    from .implementation_patch_scope import attach, GUIDANCE as SCOPE_GUIDANCE
    next_context = attach(next_context)
    from .implementation_revision_batch import status
    next_context['acceptance_author_saved_progress'] = status(engine, hid, context, source[author.COLLECTION])
    docs = {doc['document_id']: doc for doc in next_context['indexed_documents']}
    docs[draft['document']['document_id']] = draft['document']
    next_context['indexed_documents'] = list(docs.values())
    # Reuse the saved draft's changed fields as author memory, with exact
    # locators for the rest. This is context, never new scientific reading credit.
    previous = next(s for s in state['specifications'] if s['index'] == draft['index'] - 1)['spec']
    fields = {k: copy.deepcopy(v) for k, v in draft['spec'].items() if digest(v) != digest(previous.get(k))}
    next_context['acceptance_author_checkpoint'] = {'document': draft['document'], 'spec_hash': draft['spec_hash'],
        'fields': fields if len(json.dumps(fields, ensure_ascii=False)) <= 24000 else {},
        'reading_credit': False, 'scientific_approval': False}
    from .implementation_acceptance import AUTHOR_GUIDANCE
    from .implementation_document_locator import GUIDANCE as LOCATOR_GUIDANCE
    from .implementation_protocol_contracts import author_task
    task = ('继续当前未完成的实现文档修订。原草稿、原修改意见及已付费答复已经保存；不重写已完成单元。'
            + AUTHOR_GUIDANCE + author_task(next_context) + LOCATOR_GUIDANCE + GUIDANCE + SCOPE_GUIDANCE)
    purpose = source['handoffs']['purpose'].split(':save:', 1)[0].split(':complete:', 1)[0] + ':complete:' + checksum[:16]
    return {'source_id': hid, 'payload': payload, 'audit_id': audit_id, 'sha256': checksum,
        'context': next_context, 'instruction': task, 'purpose': purpose,
        'run_guard': {k: engine.store.get('runs', run_id).get(k) for k in ('status', 'config', 'state')}}


def commit(engine, h, run_id, state, plan):
    """Create a successor once, with CAS on original state before dispatch."""
    from .implementation_authoring import submission_schema
    from .engine import ResearchBlocked
    from .implementation_review_clock import _get, _put
    from .implementation_patch_scope import allowed_fields, validate_repair
    allowed_fields(plan['context'])
    validate_repair(plan['context'], state, plan['payload']['source'], plan['payload']['pending'])
    existing = engine.store.get('stage_contexts', plan['audit_id'])
    if plan['payload'].get('submission_recovery'):
        from .implementation_author_submission_recovery import validate
        validate(engine, run_id, state, plan['payload'])
    if existing and (existing.get('payload') != plan['payload'] or existing.get('sha256') != plan['sha256']
            or existing.get('successor_context_sha256') != digest(plan['context'])):
        raise ResearchBlocked('作者续接已有另一份冻结绑定，未创建新交接。')
    schema = plan.get('schema', submission_schema(plan['context']))
    child = h._handoff(run_id, 'engineer', plan['purpose'], plan['instruction'], plan['context'], schema)
    audit = {'id': plan['audit_id'], 'run_id': run_id, 'payload': plan['payload'],
             'sha256': plan['sha256'], 'successor_id': child['id'],
             'successor_context_sha256': digest(plan['context']), 'scientific_approval': False}
    with engine.store._db() as db:
        db.execute('BEGIN IMMEDIATE')
        run = _get(db, 'runs', run_id, {})
        inputs = _get(db, 'handoff_inputs', child['id'], {})
        if ({k: run.get(k) for k in plan['run_guard']} != plan['run_guard']
                or run.get('status') != 'running'
                or _workflow_scope(_get(db, 'codex_workflows', state['id'], {})) != plan['payload']['workflow']
                or any(_canonical_source(col, _get(db, col, plan['source_id'])) != value
                       for col, value in plan['payload']['source'].items())):
            raise ResearchBlocked('作者续接准备期间原记录或运行状态已改变，未派发续接请求。')
        recovery = plan['payload'].get('submission_recovery')
        if recovery:
            for col, key, value in recovery_guards(recovery):
                if _canonical_source(col, _get(db, col, key)) != value:
                    raise ResearchBlocked('作者提交修复的原失败回执或继承审计已改变，未派发。')
        if (inputs.get('context') != plan['context'] or inputs.get('instruction') != plan['instruction']
                or inputs.get('schema') != schema):
            raise ResearchBlocked('作者续接的已有请求与冻结计划不一致，未派发。')
        existing = _get(db, 'stage_contexts', plan['audit_id'])
        if existing and existing != audit:
            raise ResearchBlocked('作者续接已有另一份绑定记录，未重复创建。')
        if not existing:
            _put(engine.store, db, 'stage_contexts', plan['audit_id'], audit, now())
            db.execute('INSERT INTO events(run_id,kind,message,data,created_at) VALUES(?,?,?,?,?)',
                (run_id, 'implementation.author_revision_continued', '中间稿已保留；作者继续批量修订与查漏，尚未提交独立审核。',
                 json.dumps({'handoff_id': child['id'], 'source_handoff_id': plan['source_id'],
                    'pending_conditions': len(plan['payload']['pending']), 'review_round_added': False}), now()))
    return child


def recovery_guards(recovery):
    return [(col, recovery['handoff_id'], value) for col, value in recovery['records'].items()] + [
        ('stage_contexts', recovery['base_audit']['id'], recovery['base_audit'])]


def resume_unsent_repeat(h, run_id, role, hid, context, progress):
    """Repair a pre-fix repeat-read closeout without replaying a paid request."""
    from datetime import datetime
    from .implementation_authoring import _assignment
    from .implementation_save_resume import _settled_response
    from .storage import now
    field = 'acceptance_author_repeat_recovery'
    if (getattr(h.engine.store, 'is_read_snapshot', False)
            or role != 'engineer' or not enabled(context) or progress.get('mode') != 'final_only'
            or field in progress or progress.get('pending_tool_calls')):
        return False
    final, normal = progress.get('finalization', {}), progress.get('normal_request', {})
    withheld = final.get('withheld_tool_calls', [])
    calls = normal.get('response', {}).get('json', {}).get('tool_calls', [])
    if (final.get('status') != 'ready' or any(final.get(k) is not None
            for k in ('prepared', 'dispatched_at', 'response'))
            or final.get('reason') != 'Repeated tool call without progress: documents.read'
            or not withheld or withheld[0].get('name') != 'documents.read'
            or not calls or len(withheld) > len(calls) or calls[-len(withheld):] != withheld
            or normal.get('status') != 'response_saved' or normal.get('turn') != progress.get('turn')
            or final.get('completed_result_hashes') != [digest(r) for r in progress['tool_results']]):
        return False
    engine = h.engine
    run = engine.store.get('runs', run_id, {})
    generation = engine.store.get('operator_resumes', run.get('state', {}).get('resume_generation', ''), {})
    try:
        if (run.get('status') != 'running' or generation.get('run_id') != run_id
                or datetime.fromisoformat(generation['created_at']) <= datetime.fromisoformat(final['created_at'])):
            return False
    except (KeyError, TypeError, ValueError):
        return False
    assignment, frozen, state, binding = _assignment(engine, run_id, role, hid)
    inputs = engine.store.get('handoff_inputs', hid)
    if (frozen != context or state.get('current_revision') != binding['revision']
            or state.get('status') != 'specification' or engine.store.get('decisions', hid)
            or assignment.get('status') == 'completed'
            or assignment.get('input_hash') != digest({k: inputs.get(k) for k in ('instruction', 'context', 'schema')})
            or engine.store.record_keys('tool_invocations', run_id,
                predicate="json_extract(data,'$.handoff_id')=? AND json_extract(data,'$.status') IN ('pending','running','unknown')",
                parameters=[hid]) or not _settled_response(engine.store, run_id, normal)):
        return False
    original = copy.deepcopy(progress)
    payload = {'original_progress': original, 'input_sha256': digest(inputs),
        'operator_resume_generation': generation['id'], 'workflow': _workflow_scope(state)}
    audit = {'payload': payload, 'sha256': digest(payload), 'created_at': now(),
        'paid_request_replayed': False, 'scientific_approval': False}

    def repair(current):
        engine.check_active(run_id)
        if (current != original or engine.store.get('handoff_inputs', hid) != inputs
                or engine.store.get('runs', run_id, {}).get('state', {}).get('resume_generation') != generation['id']
                or _workflow_scope(engine.store.get('codex_workflows', state['id'])) != _workflow_scope(state)):
            raise ResearchBlocked('重复读取恢复的检查点或冻结输入已改变，未继续执行。')
        result = {k: copy.deepcopy(v) for k, v in current.items() if k not in {'mode', 'finalization'}}
        result.update(pending_tool_calls=copy.deepcopy(calls), tool_cursor=len(calls) - len(withheld),
                      tool_counted=True, **{field: audit})
        return result

    updated = engine.store.mutate('handoff_progress', hid, repair)
    progress.clear()
    progress.update(updated)
    engine.store.event(run_id, 'implementation.author_repeat_recovered',
        '重复读取未送审；恢复原待处理工具批次，再使用当前记忆继续修订。',
        {'handoff_id': hid, 'audit_sha256': audit['sha256'], 'paid_request_replayed': False})
    return True
