"""Keep incomplete acceptance revisions with the author, before independent review."""
from __future__ import annotations

import copy
import json

from .implementation_review_routing import digest
from .storage import now
from .engine import ResearchBlocked

PROTOCOL = 'implementation_acceptance_author_protocol'
FIELD = 'acceptance_completion'
CONTINUATION = 'implementation_acceptance_author_continuation'
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
    from .implementation_patch_scope import diagnostic
    from .implementation_acceptance import criterion_fields
    allowed = set().union(*(criterion_fields(context['acceptance_contract'], issue['criterion_id'])
                            for issue in context['acceptance_issues']))
    scope = diagnostic(previous['spec'], current['spec'], [], allowed)
    return {'baseline_document': previous['document'], 'baseline_spec_hash': previous['spec_hash'],
        'document': current['document'], 'spec_hash': current['spec_hash'],
        'actual_fields': scope['actual_fields'], 'actual_paths': scope['actual_paths'],
        'forbidden_fields': scope['forbidden_fields'], 'scientific_approval': False,
        'instruction': '这是相对上一轮送审稿的累计实际改动目录，不仅是最近一次 edit。'
            '最终 patch_reports 必须按原问题申报全部实际改动；不要为补报告重写已保存正文。'
            '本目录不代表问题已解决，也不提供阅读或自检信用。完成剩余修改后核对最新 SHA，'
            '复用已交付范围，只补读尚未交付的自检范围。'}


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
    result = copy.deepcopy(base)
    check = policy.obj({'index': {'type': 'integer', 'minimum': 0},
        'status': {'enum': ['complete', 'incomplete', 'blocked']},
        'explanation': policy.TEXT, 'evidence': policy.array(policy.REF)})
    entry = policy.obj({'issue_id': policy.TEXT, 'conditions': policy.array(check, 1)})
    result['properties'][FIELD] = policy.array(entry, 1)
    result['required'].append(FIELD)
    return result


class IncompleteRevision(ResearchBlocked):
    def __init__(self, pending):
        self.pending = pending
        super().__init__('实现文档仍有未完成修改；保留当前草稿，继续作者修订，未提交独立审核。')


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
            or payload.get('source') != _source_records(engine.store, ref['source_handoff_id'])
            or payload.get('source', {}).get(COLLECTION, {}).get('row', {}).get('spec_hash') != ref['spec_hash']):
        raise ResearchBlocked('作者续接的原草稿、提交或冻结范围已改变，未继续修改。')
    if payload.get('supplement_repair'):
        from .implementation_supplement_repair import validate_adoption
        validate_adoption(engine, run_id, state, payload['supplement_repair'])
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
    validate_schema(result, author.submission_schema(context))
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
    successor = audit.get('successor_id', '')
    inputs = engine.store.get('handoff_inputs', successor, {})
    assignment = engine.store.get('handoffs', successor, {})
    context = inputs.get('context', {})
    ref = context.get(CONTINUATION, {})
    from .implementation_authoring import COLLECTION, _checked, submission_schema
    if (audit.get('run_id') != run_id or audit.get('sha256') != digest(payload)
            or payload.get('source') != _source_records(engine.store, hid)
            or payload.get('workflow') != _workflow_scope(state)
            or (payload.get('source', {}).get('decisions') or {}).get('result') != result
            or inputs.get('run_id') != run_id or assignment.get('run_id') != run_id
            or assignment.get('to_role') != 'engineer'
            or assignment.get('input_hash') != digest({k:inputs.get(k) for k in ('instruction','context','schema')})
            or inputs.get('schema') != submission_schema(context)
            or audit.get('successor_context_sha256') != digest(context)
            or ref.get('audit_id') != audit_id or ref.get('sha256') != audit['sha256']
            or ref.get('source_handoff_id') != hid):
        raise ResearchBlocked('已有作者续接的冻结来源或交接绑定已改变，未重建或派发。')
    _checked(engine, run_id, payload['source'][COLLECTION])
    continuation_base(engine, run_id, state, context)
    return {'source_id':hid, 'payload':copy.deepcopy(payload), 'audit_id':audit_id,
        'sha256':audit['sha256'], 'context':copy.deepcopy(context),
        'instruction':inputs['instruction'], 'purpose':assignment['purpose'],
        'run_guard':{k:engine.store.get('runs',run_id).get(k) for k in ('status','config','state')}}


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
        return existing
    _report_inherited_blockers(engine, run_id, state, hid, result, context)
    try:
        author._prepare_submission(engine, run_id, state, hid, result)
        return None
    except IncompleteRevision as exc:
        pending = exc.pending
    # Attempt all still-editable conditions before stopping on an external blocker.
    if all(item['status'] == 'blocked' for item in pending):
        raise ResearchBlocked(_blocked_message(pending, context))
    source = _source_records(engine.store, hid)
    depth = context.get(CONTINUATION, {}).get('depth', 0) + 1
    payload = {'source': source, 'workflow': _workflow_scope(state), 'pending': pending}
    checksum = digest(payload)
    audit_id = hid + ':acceptance-author-continuation'
    draft = source[author.COLLECTION]['row']
    next_context = copy.deepcopy(context)
    # The new assignment has its own edit budget and source scope. The old save
    # request and its four-turn contract remain intact in the source archive.
    for key in ('implementation_document_completion_protocol', 'implementation_document_completion',
                'implementation_phase_context_protocol', 'implementation_phase_context',
                'implementation_document_repair_reads'):
        next_context.pop(key, None)
    next_context.update({PROTOCOL: 1, author.EDIT_PROTOCOL: author.EDIT_VERSION,
        'implementation_document_locate_protocol': 1,
        CONTINUATION: {'audit_id': audit_id, 'sha256': checksum, 'source_handoff_id': hid,
            'spec_hash': draft['spec_hash'], 'depth': depth, 'document': draft['document']},
        'acceptance_author_pending': pending})
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
    task = ('继续当前未完成的实现文档修订。原草稿、原修改意见及已付费答复已经保存；不重写已完成单元。'
            + AUTHOR_GUIDANCE + author.EDIT_TASK + LOCATOR_GUIDANCE + GUIDANCE)
    purpose = source['handoffs']['purpose'].split(':save:', 1)[0].split(':complete:', 1)[0] + ':complete:' + checksum[:16]
    return {'source_id': hid, 'payload': payload, 'audit_id': audit_id, 'sha256': checksum,
        'context': next_context, 'instruction': task, 'purpose': purpose,
        'run_guard': {k: engine.store.get('runs', run_id).get(k) for k in ('status', 'config', 'state')}}


def commit(engine, h, run_id, state, plan):
    """Create a successor once, with CAS on original state before dispatch."""
    from .implementation_authoring import submission_schema
    from .engine import ResearchBlocked
    from .implementation_review_clock import _get, _put
    existing = engine.store.get('stage_contexts', plan['audit_id'])
    if existing and (existing.get('payload') != plan['payload'] or existing.get('sha256') != plan['sha256']
            or existing.get('successor_context_sha256') != digest(plan['context'])):
        raise ResearchBlocked('作者续接已有另一份冻结绑定，未创建新交接。')
    child = h._handoff(run_id, 'engineer', plan['purpose'], plan['instruction'], plan['context'],
                       submission_schema(plan['context']))
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
        if (inputs.get('context') != plan['context'] or inputs.get('instruction') != plan['instruction']
                or inputs.get('schema') != submission_schema(plan['context'])):
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


async def finish(engine, h, run_id, state, hid, result):
    while True:
        from .implementation_operator_continuation import pending_plan
        from .implementation_source_supplement import review_if_pending
        plan = await engine._read_consensus_check(run_id, pending_plan, engine, run_id, state, hid)
        if plan is None:
            plan = await review_if_pending(engine, h, run_id, state, hid)
        if plan is None:
            plan = await engine._read_consensus_check(run_id, prepare, engine, run_id, state, hid, result)
        if plan is None:
            return result, hid
        engine.check_active(run_id)
        child = commit(engine, h, run_id, state, plan)
        from .implementation_authoring import submission_schema
        result = await h.ask(run_id, 'engineer', child['purpose'], plan['instruction'], plan['context'],
                             submission_schema(plan['context']))
        hid = child['id']


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
