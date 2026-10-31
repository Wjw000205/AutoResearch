"""A frozen checklist for finishing the author's existing revision work.

This checks completeness, saved fields and evidence, not scientific correctness.
Only the original reviewers can accept the author's fixes or rebuttals.
"""
from __future__ import annotations

import copy

from .implementation_review_routing import digest

KEY = 'implementation_author_revision_contract'
FIELD = 'revision_completion'
VERSION = 1
RESUME_KEY = 'implementation_author_revision_resume'
_READONLY_RECOVERY = ('implementation_author_read_recovery', 'implementation_author_delivery_recovery',
    'implementation_author_inherited_delivery_recovery', 'implementation_author_response_correction',
    'implementation_author_origin_correction')
GUIDANCE = '''
本轮作者提交前必须完成 implementation_author_revision_contract.items 的全部原修改要求。
每个 fix_id 对应原评审的一条 change/verification，不能拆掉其中的条件或提高原标准。
revision_completion 必须逐 fix、逐目标字段给出完成自检：revised 表示全部要求已落实；
rebutted 表示用当前稿和批准来源证明原要求已经满足或不适用；incomplete 表示尚未完成。
field_checks.changed 必须是本轮实际成功保存的变化；already_satisfied 必须说明现有条款如何满足原标准。
仅局部修复、仍缺定义、保存失败、尚待编写不是反驳，必须标 incomplete 并继续原作者修订。
每个目标字段都须在对应 issue_responses.evidence 中引用当前稿该章节的实际原文。
verification 说明逐条自检结果及文档中的验收计划，不要求此时已有代码、训练、CUDA或科研结果。
保存失败会保留整批待修改字段。修好报错字段后还须重交其余修改，不能漏掉原批次的其他字段。
先完成全部可修文档项再最终提交；外部来源或宿主依赖仍按原分类说明，不得编造恢复证据。
未完成的提交会退回同一作者交接继续处理，不新增文档审核轮次。
'''


def _fail(message):
    from .engine import ResearchBlocked
    raise ResearchBlocked('实现作者完整修订：' + message)


def _incomplete(problems):
    from .engine import ResearchBlocked
    error = ResearchBlocked('实现作者完整修订：还有 ' + str(len(problems)) + ' 项未完成，继续本轮作者修订，未提交三方审核。')
    error.author_revision_problems = problems
    raise error


def root(field):
    if not isinstance(field, str) or not field:
        return None
    if field.startswith('/'):
        return field[1:].split('/', 1)[0].replace('~1', '/').replace('~0', '~')
    # Existing reviewer locators also use schema-style dotted/array paths.
    return field.split('.', 1)[0].split('[', 1)[0]


def attach(state, revision, context):
    if revision == 0 or KEY in context:
        return context
    from .implementation_issue_responses import CONTRACT
    old = next(row for row in state['specifications'] if row['index'] == revision - 1)
    actions = context[CONTRACT]['actions']
    items = []
    for action in actions:
        if action['disposition'] != 'document_revision':
            continue
        origin = action['latest_action_sources'][0]
        vote = next(v for v in old['review_votes'] if v['handoff_id'] == origin['handoff_id'])
        for number, fix in enumerate(vote['fixes']):
            if fix['issue_index'] != origin['issue_index']:
                continue
            fields = sorted({root(loc['field']) for loc in vote.get('fix_locations', [])
                if loc['fix_index'] == number})
            if not fields or any(field not in old['spec'] for field in fields):
                _fail('原修改要求缺少可核验的 SPEC 字段位置。')
            item = {'objection_id': action['objection_id'], 'origin_ref_sha256': action['origin_ref_sha256'],
                'role': action['role'], 'source_handoff_id': vote['handoff_id'], 'fix_index': number,
                'fields': fields, 'change': fix['change'], 'verification': fix['verification']}
            items.append({'fix_id': digest(item), **item})
    value = {'version': VERSION, 'workflow_id': state['id'], 'revision': revision,
        'previous_spec_hash': old['spec_hash'], 'items': items, 'scientific_approval': False,
        'author_tool_turn_budget': min(96, max(32, 4 * len(items) + 8)),
        'save_tool_turn_budget': min(64, max(12, 2 * len(items) + 8))}
    return {**context, KEY: {**value, 'sha256': digest(value)}}


def contract(context):
    value = context.get(KEY)
    if value is None:
        return None
    if (not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != VERSION
            or value.get('scientific_approval') is not False
            or value.get('sha256') != digest({k: v for k, v in value.items() if k != 'sha256'})
            or any(item.get('fix_id') != digest({k: v for k, v in item.items() if k != 'fix_id'})
                   for item in value.get('items', []))):
        _fail('冻结修改清单或原核验要求已改变。')
    return value


def author_schema(base, context):
    value = contract(context)
    if value is None:
        return base
    result = copy.deepcopy(base)
    text = {'type': 'string', 'minLength': 1, 'pattern': r'\S'}
    check = {'field': text, 'outcome': {'enum': ['changed', 'already_satisfied', 'incomplete']},
             'rationale': {**text, 'maxLength': 1600}}
    entry = {'fix_id': {'enum': [item['fix_id'] for item in value['items']] or ['no_document_fixes']},
        'outcome': {'enum': ['revised', 'rebutted', 'incomplete']},
        'field_checks': {'type': 'array', 'items': {'type': 'object', 'properties': check,
            'required': list(check), 'additionalProperties': False}},
        'verification': {**text, 'maxLength': 2400}}
    result['properties'][FIELD] = {'type': 'array', 'minItems': len(value['items']),
        'maxItems': len(value['items']), 'items': {'type': 'object', 'properties': entry,
            'required': list(entry), 'additionalProperties': False}}
    result['required'].append(FIELD)
    return result


def _field_evidence(document, refs, field):
    from .implementation_document_readability import HEADINGS
    titles = {HEADINGS.get(field, field), field,
        {'architecture_traceability': '架构要求与实现验收对应',
         'comparison_transport': '比较数据传递合同'}.get(field, field)}
    sections = {s['id'] for s in document['sections'] if s['title'].strip('# ') in titles}
    # Older serialization has a single structured SPEC section. Its exact
    # current-version reads still pass the ordinary read/anchor validator.
    if not sections:
        sections = {s['id'] for s in document['sections'] if s['title'] in {'SPEC', '实现规范'}}
    return any(ref['document_id'] == document['document_id'] and ref['sha256'] == document['sha256']
               and ref['section_id'] in sections for ref in refs)


def validate(engine, run_id, state, row, hid, result, context):
    value = contract(context)
    if value is None:
        return
    old = next((r for r in state['specifications'] if r['index'] == row['index'] - 1), None)
    if (not old or value['workflow_id'] != state['id'] or value['revision'] != row['index']
            or value['previous_spec_hash'] != old['spec_hash']):
        _fail('修改清单不属于当前唯一前版。')
    answers = result.get(FIELD, [])
    expected = {item['fix_id']: item for item in value['items']}
    if (not isinstance(answers, list) or len(answers) != len(expected)
            or {item.get('fix_id') for item in answers} != set(expected)):
        _incomplete([{'fix_id': item['fix_id'], 'fields': item['fields'],
            'reasons': ['最终答复必须逐项覆盖全部原修改要求，不能遗漏或重复。'],
            'change': item['change'], 'verification': item['verification']} for item in value['items']])
    replies = {reply['origin_ref_sha256']: reply for reply in result.get('issue_responses', [])}
    problems = []
    for answer in answers:
        item = expected[answer['fix_id']]
        reply = replies.get(item['origin_ref_sha256'], {})
        checks = answer.get('field_checks', [])
        fields = item['fields']
        reasons = []
        if reply.get('objection_id') != item['objection_id']:
            _fail('自检没有绑定该原问题的实际逐项回应。')
        if len(checks) != len(fields) or {check.get('field') for check in checks} != set(fields):
            reasons.append('必须逐字段覆盖原要求：' + ', '.join(fields))
        if answer.get('outcome') == 'incomplete' or any(check.get('outcome') == 'incomplete' for check in checks):
            reasons.append('作者确认本条尚未完整落实')
        changed = []
        for check in checks:
            field = check.get('field')
            if field not in fields:
                continue
            if check.get('outcome') == 'changed':
                if ((field in old['spec']) == (field in row['spec'])
                        and digest(old['spec'].get(field)) == digest(row['spec'].get(field))):
                    reasons.append(field + ' 未实际保存变化')
                if field not in {root(path) for path in reply.get('changed_fields', [])}:
                    reasons.append(field + ' 未在对应逐项回应中声明实际修改')
                changed.append(field)
            if not _field_evidence(row['document'], reply.get('evidence', []), field):
                reasons.append(field + ' 缺少当前稿该章节原文证据')
        if answer.get('outcome') == 'revised' and not changed:
            reasons.append('声明已修订却没有实际修改字段')
        if answer.get('outcome') == 'rebutted' and changed:
            reasons.append('反驳不能同时声称本条已修改')
        if reasons:
            problems.append({'fix_id': item['fix_id'], 'objection_id': item['objection_id'],
                'fields': fields, 'reasons': reasons, 'change': item['change'], 'verification': item['verification']})
    from .implementation_author_patch_intents import derive
    progress = engine.store.get('handoff_progress', hid, {})
    pending = derive(context, progress.get('tool_results', []), old['spec'], row['spec'])
    required_fields = {field for item in value['items'] for field in item['fields']}
    for field in pending['pending_fields']:
        if field not in required_fields:
            continue  # Optional edits cannot become new reviewer requirements.
        # Explicit already-satisfied evidence is the only way to withdraw an
        # unnecessary failed edit; silently dropping its field is insufficient.
        withdrawals = [(answer, expected[answer['fix_id']]) for answer in answers
            if any(c.get('field') == field and c.get('outcome') == 'already_satisfied'
                   for c in answer.get('field_checks', []))]
        if not withdrawals or any(not _field_evidence(row['document'],
                replies[item['origin_ref_sha256']].get('evidence', []), field) for _, item in withdrawals):
            problems.append({'fix_id': None, 'fields': [field], 'reasons': ['失败批次的该字段尚未重新保存或有证据撤回']})
    if problems:
        _incomplete(problems)


def save_changes(context, base, spec):
    if contract(context) is None:
        return None
    before = (base or {}).get('spec', {})
    fields = [{'field': field, 'before_sha256': digest(before.get(field)), 'after_sha256': digest(spec.get(field)),
        'before_present': field in before, 'after_present': field in spec}
        for field in sorted(set(before) | set(spec))
        if (field in before) != (field in spec) or digest(before.get(field)) != digest(spec.get(field))]
    value = {'version': 1, 'base_spec_hash': (base or {}).get('spec_hash'),
             'saved_spec_hash': digest(spec), 'fields': fields}
    return {**value, 'sha256': digest(value)}


def tool_limit(context, progress):
    value = contract(context)
    if value is None:
        return None
    limit = value['save_tool_turn_budget'] if context.get('implementation_document_completion_protocol') else value['author_tool_turn_budget']
    for entry in progress.get('alignment_candidate_history', []):
        if entry.get('author_revision_problems'):
            limit = max(limit, entry['next_turn'] + value['author_tool_turn_budget'])
    return limit


def diagnostic(result, context, error, attempt, limit, problems):
    return {'host_diagnostic': 'implementation_author_revision_required', 'error': error,
        'alignment_read_correction': {'attempt': attempt, 'max_attempts': limit,
            'candidate_sha256': digest(result), 'pending_fixes': copy.deepcopy(problems),
            'instruction': '当前稿保留，但本轮修订未完成，尚未提交评审。继续同一作者交接，用当前草稿三项身份定向编辑；'
                '逐项完成下列原修改要求，保存失败的其他字段仍须重交。每项只定向核对相关来源和目标字段，'
                '随即保存该项实质修改，再检查新稿对应章节；不要重新遍历全部固定输入或仅反复整理未完成答复。'
                '完成全部可修项后返回完整 submission、issue_responses、revision_completion。'
                '有证据的反驳须说明当前条款如何已满足原要求，承认缺项不能标完成；不新增科学要求或伪造执行证据。'}}


def _scope(h, run_id, role, purpose, hid, context, *, require_running=True):
    from . import implementation_authoring as author
    from .implementation_author_recovery_scope import workflow_fingerprint
    assignment, frozen, state, binding = author._assignment(h.engine, run_id, role, hid)
    run = h.engine.store.get('runs', run_id, {})
    inputs = h.engine.store.get('handoff_inputs', hid, {})
    payload = {k: inputs.get(k) for k in ('instruction', 'context', 'schema')}
    if (role != 'engineer' or assignment.get('purpose') != purpose or frozen != context
            or contract(context) is None or inputs.get('schema') != author.submission_schema(context)
            or assignment.get('input_hash') != digest(payload)
            or h.decision_key(run_id, role, purpose, **payload) != hid
            or (run.get('status') != 'running' if require_running else run.get('status') not in {'running', 'blocked', 'paused'})
            or run.get('stage') != 'implementation'
            or state.get('status') != 'specification' or state.get('current_revision') != binding['revision']
            or h.engine.store.get('decisions', hid)):
        _fail('原作者输入、工作流或运行范围已改变，未继续返工。')
    return {'input_sha256': digest(inputs), 'configuration_sha256': digest(run.get('config')),
            'workflow_sha256': workflow_fingerprint(state), 'mode': run.get('mode')}


def seal_rework(h, run_id, role, purpose, hid, context, progress, entry):
    from .implementation_alignment_recovery import _verify_format_source
    _verify_format_source(entry, role, purpose, problem_field='author_revision_problems')
    # The old narrow final/read corrections freeze the draft. Never erase
    # those guards to obtain edit permission.
    if any(progress.get(key) is not None for key in _READONLY_RECOVERY):
        _fail('当前交接已冻结为原稿补读或无工具补正，不能借完整性返工解除原限制。')
    source = entry['source_requests'].get('finalization' if entry['source_mode'] == 'final_only' else 'normal_request', {})
    if h.engine.store.get('runs', run_id, {}).get('mode') != 'demo':
        from .implementation_save_resume import _settled_response
        if not source or not _settled_response(h.engine.store, run_id, source):
            _fail('原请求尚未完整接收结算，不能派发返工或重发原请求。')
    value = {'version': 1, 'scope': _scope(h, run_id, role, purpose, hid, context),
        'source_requests_sha256': digest(entry['source_requests']), 'candidate_sha256': entry['candidate_sha256'],
        'problems_sha256': digest(entry['author_revision_problems']), 'next_turn': entry['next_turn'],
        'source_tool_count': len(progress['tool_results']), 'source_tool_results_sha256': digest(progress['tool_results']),
        'provider_id': source.get('provider_id'), 'model': source.get('model'), 'scientific_approval': False}
    return {**value, 'sha256': digest(value)}


def _validate_rework(h, run_id, role, purpose, hid, progress, *, require_running=True):
    context = h.engine.store.get('handoff_inputs', hid, {}).get('context', {})
    resumed = _resume_checked(context, progress)
    history = progress.get('alignment_candidate_history', [])
    if not isinstance(history, list) or any(not isinstance(entry, dict) for entry in history):
        _fail('作者返工原候选历史不是有效封存对象。')
    if resumed is None and any(entry.get(RESUME_KEY) is not None for entry in history):
        _fail('显式作者续接的原授权标记缺失，不能重新领取预算。')
    entries = [entry for entry in history if entry.get('author_revision_problems')]
    if not entries:
        return
    from .implementation_alignment_recovery import _verify_format_source
    scope = _scope(h, run_id, role, purpose, hid, context, require_running=require_running)
    for entry in entries:
        _verify_format_source(entry, role, purpose, problem_field='author_revision_problems')
        audit = entry.get('author_revision_audit', {})
        if not isinstance(audit, dict):
            _fail('作者返工原核验封存不是有效对象。')
        count = audit.get('source_tool_count')
        if (audit.get('version') != 1 or audit.get('scientific_approval') is not False
                or audit.get('sha256') != digest({k: v for k, v in audit.items() if k != 'sha256'})
                or audit.get('scope') != scope or audit.get('source_requests_sha256') != digest(entry['source_requests'])
                or audit.get('candidate_sha256') != entry['candidate_sha256']
                or audit.get('problems_sha256') != digest(entry['author_revision_problems'])
                or audit.get('next_turn') != entry['next_turn'] or type(count) is not int or count < 0
                or len(progress.get('tool_results', [])) < count
                or audit.get('source_tool_results_sha256') != digest(progress['tool_results'][:count])):
            _fail('返工的原候选、原核验标准、前缀账本或冻结范围已改变。')
        if hasattr(h.intelligence, 'provider_for'):
            provider = h.intelligence.provider_for(role)
            if (audit['provider_id'], audit['model']) != (provider.get('id'), provider.get('model')):
                _fail('作者返工必须沿用原供应方和模型。')
    if resumed:
        generation = h.engine.store.get('operator_resumes', resumed['operator_resume_generation'], {})
        if (generation.get('id') != resumed['operator_resume_generation'] or generation.get('run_id') != run_id
                or generation.get('kind') != 'explicit_start_command'
                or generation.get('author_revision_targets', {}).get(hid) != resumed['target']
                or resumed['target']['scope'] != scope):
            _fail('显式作者续接的授权或原冻结范围已改变。')


def validate_rework(h, run_id, role, purpose, hid, progress):
    _validate_rework(h, run_id, role, purpose, hid, progress)


def validate_dispatch(h, run_id, role, purpose, hid, progress):
    if not any(entry.get('author_revision_problems') for entry in progress.get('alignment_candidate_history', [])):
        return
    if h.engine.store.get('handoff_progress', hid) != progress:
        _fail('返工检查点在派发准备期间发生变化，未发送新请求。')
    validate_rework(h, run_id, role, purpose, hid, progress)


def _resume_checked(context, progress):
    """Authenticate the stored continuation and its immutable source prefixes."""
    audit = progress.get(RESUME_KEY)
    if audit is None:
        return None
    value, history, rows = contract(context), progress.get('alignment_candidate_history', []), progress.get('tool_results', [])
    target = audit.get('target', {}) if isinstance(audit, dict) else {}
    if not isinstance(target, dict):
        _fail('显式作者续接授权不是有效封存对象。')
    count, tools = target.get('source_history_count'), target.get('source_tool_count')
    if (not isinstance(audit, dict) or audit.get('version') != VERSION or type(audit.get('version')) is not int
            or audit.get('scientific_approval') is not False or audit.get('old_request_replayed') is not False
            or audit.get('sha256') != digest({k: v for k, v in audit.items() if k != 'sha256'})
            or target.get('version') != VERSION or type(target.get('version')) is not int
            or target.get('sha256') != digest({k: v for k, v in target.items() if k != 'sha256'})
            or target.get('run_id') != progress.get('run_id') or target.get('handoff_id') != progress.get('id')
            or target.get('role') != 'engineer' or target.get('scientific_approval') is not False
            or target.get('old_request_replayed') is not False or value is None
            or target.get('contract_sha256') != digest(value)
            or target.get('workflow_id') != value.get('workflow_id') or target.get('revision') != value.get('revision')
            or type(target.get('source_turn')) is not int or type(target.get('next_turn')) is not int
            or target['source_turn'] < 0 or target['next_turn'] != target['source_turn'] + 1
            or type(count) is not int or count < 3 or not isinstance(history, list) or not count < len(history) <= count + 3
            or type(tools) is not int or tools < 0 or not isinstance(rows, list) or len(rows) < tools + 2
            or target.get('source_history_sha256') != digest(history[:count])
            or target.get('source_tool_results_sha256') != digest(rows[:tools])
            or not isinstance(audit.get('operator_resume_generation'), str) or not audit['operator_resume_generation']):
        _fail('显式作者续接的授权、原清单或前缀账本已改变。')
    entry = history[count]
    if not isinstance(entry, dict):
        _fail('显式作者续接的原请求归档已改变。')
    sources = entry.get('source_requests', {})
    candidate = entry.get('candidate', {})
    if (audit.get('resumed_entry_sha256') != digest(entry) or entry.get('attempt') != count + 1
            or entry.get('source_mode') != 'final_only'
            or entry.get(RESUME_KEY) != {'version': VERSION,
                'operator_resume_generation': audit['operator_resume_generation'], 'target_sha256': target['sha256']}
            or entry.get('source_turn') != target.get('source_turn')
            or entry.get('next_turn') != target.get('next_turn')
            or entry.get('candidate_sha256') != target.get('candidate_sha256')
            or digest(sources) != target.get('source_requests_sha256')
            or digest(entry.get('author_revision_problems')) != target.get('problems_sha256')
            or entry.get('author_revision_audit', {}).get('source_tool_count') != tools
            or entry.get('author_revision_audit', {}).get('provider_id') != target.get('provider_id')
            or entry.get('author_revision_audit', {}).get('model') != target.get('model')
            or {key: candidate.get(key) for key in ('document_id', 'document_sha256', 'spec_hash')}
                != target.get('draft_pointer')
            or rows[tools] != _resume_edit(audit['operator_resume_generation'])
            or rows[tools + 1] != diagnostic(candidate, context, entry['diagnostic'], count + 1, count + 3,
                entry['author_revision_problems'])):
        _fail('显式作者续接的原答复、当前稿绑定或宿主诊断已改变。')
    return audit


def continuation_limit(context, progress):
    """A single official resume adds a bounded window; flags grant no authority."""
    audit = _resume_checked(context, progress)
    return audit['target']['source_history_count'] + 3 if audit else 3


def _resume_edit(generation):
    return {'host_diagnostic': 'implementation_author_edit_required',
        'reason': 'explicit_author_revision_resume', 'operator_resume_generation': generation,
        'instruction': '先落实原未完成修改：按原 fix_id 和目标字段编辑当前草稿，再核对实际保存的新稿；'
            '不得循环重读已取回的固定输入，不新增科学要求，不伪造完成、批准或执行证据。'}


def _resume_problems(engine, run_id, hid, candidate, context):
    from .engine import ResearchBlocked
    from .implementation_alignment_evidence import validate_candidate
    try:
        validate_candidate(engine, run_id, 'engineer', hid, candidate, context)
    except ResearchBlocked as exc:
        problems = getattr(exc, 'author_revision_problems', None)
        if problems:
            return str(exc), copy.deepcopy(problems)
        raise
    _fail('当前答复不再属于尚未完成的原作者修订。')


def _resume_target(engine, run_id, hid, progress):
    from .engine import Harness
    from . import implementation_authoring as author
    from .implementation_save_resume import _settled_response
    assignment, context, state, binding = author._assignment(engine, run_id, 'engineer', hid)
    value = contract(context)
    history, rows = progress.get('alignment_candidate_history'), progress.get('tool_results')
    if (progress.get(RESUME_KEY) is not None or value is None or binding['revision'] <= 0
            or assignment.get('status') not in {'blocked', 'interrupted', 'running'}
            or progress.get('id') != hid or progress.get('run_id') != run_id
            or progress.get('mode') != 'final_only' or type(progress.get('turn')) is not int
            or progress['turn'] < 0 or progress.get('pending_tool_calls')
            or not isinstance(history, list) or len(history) < 3 or not isinstance(rows, list)
            or any(not isinstance(entry, dict) or not entry.get('author_revision_problems')
                or entry.get(RESUME_KEY) is not None
                or any(entry.get(key) for key in ('findings_format_problems', 'routing_coordinate_problems',
                    'confirmation_scope_problems', 'indexed_review_missing_ranges', 'implementation_convergence_violation'))
                for entry in history)
            or any(container.get(key) is not None for container in (progress, context) for key in _READONLY_RECOVERY)
            or engine.store.get('handoff_progress', hid) != progress):
        return None
    run = engine.store.get('runs', run_id, {})
    h = Harness(engine, None)
    if run.get('mode') != 'demo':
        from .intelligence import Intelligence
        from .config import get_settings
        h.intelligence = Intelligence(engine.store, get_settings(engine.store))
    scope = _scope(h, run_id, 'engineer', assignment['purpose'], hid, context, require_running=False)
    _validate_rework(h, run_id, 'engineer', assignment['purpose'], hid, progress, require_running=False)
    if engine.store.record_keys('tool_invocations', run_id,
            predicate="json_extract(data,'$.handoff_id')=? AND json_extract(data,'$.status') IN ('pending','running','unknown')",
            parameters=[hid]):
        return None
    record = engine.store.get(author.COLLECTION, hid, {})
    if (record.get('status') != 'draft' or record.get('workflow_id') != state['id']
            or record.get('revision') != binding['revision'] or record.get('row', {}).get('index') != binding['revision']
            or record.get('context') != context):
        return None
    pointer = author.pointer(author._checked(engine, run_id, record))
    sources = {name: copy.deepcopy(progress[name]) for name in ('normal_request', 'finalization') if name in progress}
    final = sources.get('finalization', {})
    candidate = final.get('response', {}).get('json')
    if (final.get('status') != 'response_saved' or not isinstance(candidate, dict)
            or {key: candidate.get(key) for key in pointer} != pointer
            or any(source.get(key) is not None for source in sources.values() for key in (
                'author_delivery_of', 'author_inherited_delivery_of', 'author_response_correction_of'))
            or any(source.get('prepared', {}).get('context', {}).get(key) is not None
                for source in sources.values() for key in _READONLY_RECOVERY)
            or any(source.get('status') != 'response_saved' or not _settled_response(engine.store, run_id, source)
                for source in sources.values())):
        return None
    if hasattr(h.intelligence, 'provider_for'):
        provider = h.intelligence.provider_for('engineer')
        if (final.get('provider_id'), final.get('model')) != (provider.get('id'), provider.get('model')):
            return None
    for entry in history:
        slot = 'finalization' if entry['source_mode'] == 'final_only' else 'normal_request'
        if not _settled_response(engine.store, run_id, entry['source_requests'].get(slot, {})):
            return None
    _, problems = _resume_problems(engine, run_id, hid, candidate, context)
    target = {'version': VERSION, 'run_id': run_id, 'handoff_id': hid, 'role': 'engineer',
        'scope': scope, 'workflow_id': state['id'], 'revision': binding['revision'],
        'checkpoint_sha256': digest(progress), 'contract_sha256': digest(value),
        'draft_sha256': digest(record), 'draft_pointer': pointer,
        'source_history_count': len(history), 'source_history_sha256': digest(history),
        'source_tool_count': len(rows), 'source_tool_results_sha256': digest(rows),
        'source_requests_sha256': digest(sources), 'candidate_sha256': digest(candidate),
        'problems_sha256': digest(problems), 'source_turn': progress['turn'], 'next_turn': progress['turn'] + 1,
        'provider_id': final.get('provider_id'), 'model': final.get('model'),
        'scientific_approval': False, 'old_request_replayed': False}
    return {**target, 'sha256': digest(target)}


def resume_target(engine, run_id, hid, progress):
    """Read-only fingerprint of settled, exhausted editable author work."""
    from .engine import ResearchBlocked
    from .intelligence import ProviderError
    from jsonschema import ValidationError
    try:
        return _resume_target(engine, run_id, hid, progress)
    except (ResearchBlocked, ProviderError, ValidationError, ValueError, TypeError, KeyError, OSError):
        return None


def activate_resume(h, run_id, role, hid, context, progress):
    """Consume one official resume using CAS; never replay a paid request."""
    from .storage import now
    if role != 'engineer' or KEY not in context or progress.get(RESUME_KEY) is not None:
        return False
    engine = h.engine
    run = engine.store.get('runs', run_id, {})
    generation = engine.store.get('operator_resumes', run.get('state', {}).get('resume_generation', ''), {})
    expected = generation.get('author_revision_targets', {}).get(hid)
    if not expected:
        return False
    if (run.get('status') != 'running' or generation.get('run_id') != run_id
            or generation.get('id') != run.get('state', {}).get('resume_generation')
            or generation.get('kind') != 'explicit_start_command'
            or expected != resume_target(engine, run_id, hid, progress)
            or engine.store.get('handoff_inputs', hid, {}).get('context') != context):
        _fail('显式作者续接的原请求、草稿、输入或授权已改变；未发送新请求。')
    if hasattr(h.intelligence, 'provider_for'):
        provider = h.intelligence.provider_for(role)
        if (expected['provider_id'], expected['model']) != (provider.get('id'), provider.get('model')):
            _fail('显式作者续接必须沿用原供应方和模型。')
    elif run.get('mode') != 'demo':
        _fail('在线作者续接缺少原供应方绑定。')
    before = copy.deepcopy(progress)
    sources = {name: copy.deepcopy(progress[name]) for name in ('normal_request', 'finalization') if name in progress}
    candidate = sources['finalization']['response']['json']
    error, problems = _resume_problems(engine, run_id, hid, candidate, context)
    count = expected['source_history_count']
    entry = {'attempt': count + 1, 'candidate': copy.deepcopy(candidate),
        'candidate_sha256': digest(candidate), 'source_requests': sources,
        'source_mode': 'final_only', 'source_turn': progress['turn'], 'next_turn': expected['next_turn'],
        'diagnostic': error, 'created_at': now(), 'author_revision_problems': problems,
        RESUME_KEY: {'version': VERSION, 'operator_resume_generation': generation['id'], 'target_sha256': expected['sha256']}}
    entry['author_revision_audit'] = seal_rework(h, run_id, role,
        engine.store.get('handoffs', hid)['purpose'], hid, context, progress, entry)
    audit = {'version': VERSION, 'operator_resume_generation': generation['id'],
        'target': copy.deepcopy(expected), 'resumed_entry_sha256': digest(entry),
        'scientific_approval': False, 'old_request_replayed': False, 'created_at': now()}
    audit['sha256'] = digest(audit)
    def consume(current):
        from . import implementation_authoring as author
        from .implementation_author_recovery_scope import workflow_fingerprint
        if (current != before or engine.store.get('runs', run_id) != run
                or engine.store.get('operator_resumes', generation['id']) != generation
                or digest(engine.store.get('handoff_inputs', hid)) != expected['scope']['input_sha256']
                or digest(engine.store.get(author.COLLECTION, hid)) != expected['draft_sha256']
                or workflow_fingerprint(engine.store.get('codex_workflows', expected['workflow_id'], {}))
                    != expected['scope']['workflow_sha256']
                or engine.store.get('decisions', hid)
                or engine.store.record_keys('tool_invocations', run_id,
                    predicate="json_extract(data,'$.handoff_id')=? AND json_extract(data,'$.status') IN ('pending','running','unknown')",
                    parameters=[hid])):
            _fail('显式作者续接已被领取或检查点已改变；未重复派发。')
        result = {k: copy.deepcopy(v) for k, v in current.items() if k not in {'mode', 'normal_request', 'finalization'}}
        result.update({RESUME_KEY: audit, 'alignment_candidate_history': [*result['alignment_candidate_history'], entry],
            'turn': expected['next_turn'], 'pending_tool_calls': [], 'tool_cursor': 0, 'tool_counted': False})
        result['tool_results'].extend([_resume_edit(generation['id']),
            diagnostic(candidate, context, error, count + 1, count + 3, problems)])
        _resume_checked(context, result)
        return result
    saved = engine.store.mutate('handoff_progress', hid, consume)
    progress.clear()
    progress.update(saved)
    engine.store.event(run_id, 'implementation.author_revision_resumed',
        '显式恢复继续原作者修订；原付费答复和修改要求已封存，先编辑当前稿，未新增审核或执行授权。',
        {'handoff_id': hid, 'operator_resume_generation': generation['id'], 'source_turn': expected['source_turn'],
         'next_turn': expected['next_turn'], 'pending_items': len(problems),
         'scientific_approval': False, 'old_request_replayed': False})
    return True
