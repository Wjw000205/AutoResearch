"""Independent full-delivery code review; no role/file/endpoint partitions."""
from __future__ import annotations

import copy

from . import independent_full_review as full
from .implementation_review_routing import digest
from .architecture_validation import operation

KEY = 'whole_code_review'
VERSION = 1
READING_SCOPE = 'code_delivery_reading_scope'
RESTRICTED_VIEW = 'code_review_source_boundary'
RESTRICTED_VIEW_VERSION = 2
READ_TOOLS = frozenset({'documents.list', 'documents.read', 'documents.search', 'documents.recall',
    'artifacts.list', 'artifacts.read', 'code.locate', 'code.read', 'code.record_notes'})
SOURCE_BOUNDARY_GUIDANCE = ('\n必读范围为当前批准实现文档，以及交付目录中冻结的源码、配置、测试和清单。'
    '可按具体问题查读一份批准架构设计的精确章节。若仍缺少决定性依据，请在独立复审结果中列出具体缺口。'
    '用 code.record_notes 保存本轮源码或文档观察，并可按 note_id 回读。')
RESPONSE_GUIDANCE = ('完整代码审核答复合同1：本规则取代旧提示中“每个issue有对应fix”的通用要求。'
    'issues中每条问题必须且只能有一个repair_findings，issue_index完整覆盖0到len(issues)-1。'
    '一个问题跨多个验收项时，由本人按真实含义拆分为独立issues及各自分类，不能给同一issue重复分类。'
    'fixes只包含disposition=repair_code的问题，且必须覆盖这些问题；needs_definition、spec_conflict、'
    'needs_execution、needs_review的下一步需求写入issue和rationale，不得塞入可执行fixes。'
    'repair_code必须引用相同requirement_id的实际code_evidence；缺证据须定向核实，不得编造或批准。')
GUIDANCE = '''
总监和研究员各自独立审核同一份完整交付：所有源码、配置、测试、清单，以及批准实现文档和架构。
只提供索引，先自行调用 reviews.read_material 读完冻结材料，再调用 code.read、documents.read 等
读完完整文本交付及两份批准文档。每页最多6000字符，每批及活跃原文最多50000字符。
不按模块归口、角色职责、接口两端拆分审核；旧review-role标记只作历史正文，不限制你的审查范围。
核对整体调用链、方法、形状、参数实际消费、数据/指标协议、资源和测试；代码证据覆盖全部验收项，
引用只给相关必要范围，不逐段投票。宿主实际执行回执只证明它实际检查过的内容。
给出具体问题、文件:行号或文件:符号、最小修改与验证办法；新文件明确写出安全相对路径及需要新增的行为。
作者负责修改，原始回执及成功工作保留；你不能修改代码、替作者关闭问题或以另一个人的批准代替自己的审核。
每个问题给出唯一 repair_findings 分类：repair_code 必须有同验收项代码依据及fix；
needs_definition/spec_conflict 明确批准文档的真实缺口；needs_execution 保留尚未执行的必要检查；
needs_review 表示仍需来源澄清，主动补读，不将资料缺失写成可执行代码修改。首次问题 origin_id=null。
缺证据主动定向补读，不能为结束流程编造通过。两位同版本均通过且实际宿主检查通过后才进入下一阶段。
'''


def enabled(state):
    from .code_whole_policy import enabled as code_policy
    return code_policy(state)


def planned_scope(state, row):
    """Compile complete planned files/requirements, without assigning reviewers."""
    from .delivery_manifest import _relative
    ids = [c['id'] for c in row['spec']['acceptance_checks']]
    locations, mapped = set(), set()
    diagnostic = None
    try:
        for item in row['spec']['implementation_map']:
            name = item['location'].replace('\\', '/').split(':', 1)[0]
            locations.add(str(_relative(name)))
            mapped.add(item['requirement_id'])
        if len(ids) != len(set(ids)) or mapped != set(ids):
            raise ValueError('Complete implementation mapping must cover every original acceptance ID')
    except (ValueError, KeyError, TypeError) as exc:
        diagnostic = str(exc)
    body = {'version': VERSION, 'mode': 'whole_delivery', 'workflow_id': state['id'],
        'spec_hash': row['spec_hash'], 'document_sha256': row['document']['sha256'],
        'reviewers': list(full.REVIEWERS), 'planned_files': sorted(locations), 'acceptance_ids': ids,
        'architecture_scope_sha256': digest(state.get('implementation_architecture_scope'))}
    plan = {'body': body, 'sha256': digest(body)} if diagnostic is None else None
    envelope = {'version': VERSION, 'workflow_id': state['id'], 'spec_hash': row['spec_hash'],
        'document_sha256': row['document']['sha256'], 'mode': 'whole_delivery', 'plan': plan,
        'diagnostic': diagnostic, 'mapping_revision': None}
    return {**envelope, 'sha256': digest(envelope)}


def attach(state, specification, attempt, context,*,engine=None,run_id=None):
    from . import implementation_alignment as alignment
    from . import code_whole_policy as policy
    result = copy.deepcopy(context)
    for key in ('code_review_assignment', 'code_review_source_plan', 'partition_redispatch',
                'partition_new_code_review', 'previous_partition_purpose', 'code_repair_scope',
                'code_repair_clarification', 'definition_code_followup',
                'implementation_alignment_acceptance_ids', 'implementation_alignment_requirement_ids'):
        result.pop(key, None)
    result = alignment.attach(result, specification['spec'])
    result[KEY] = {'version': VERSION, 'workflow_id': state['id'], 'attempt': attempt['index'],
        'document': copy.deepcopy(specification['document']), 'spec_hash': specification['spec_hash'],
        'approved_architecture': policy.architecture(state),
        'directory_sha256': digest(result['delivery_artifacts'])}
    if policy.KEY in state:result[policy.KEY]=copy.deepcopy(state[policy.KEY])
    history=policy.validate_history(engine,run_id,state,specification,attempt) if engine is not None else attempt.get(policy.HISTORY)
    if history:
        result['legacy_code_review_history_sha256']=history['sha256']
        result['legacy_code_review_findings']=copy.deepcopy(history['findings'])
    if policy.LINEAGE in attempt:result[policy.LINEAGE]=copy.deepcopy(attempt[policy.LINEAGE])
    if engine is not None:
        feedback=policy.previous_feedback(engine,run_id,state,specification,attempt)
        if feedback:result['prior_code_review_feedback']=feedback
    # The authoritative plan is indexed; large design/proposal bodies are not
    # needed to construct a new review request or authenticate its host sources.
    for key in ('design', 'proposal', 'experiment_plan', 'previous_specifications', 'previous_attempts'):
        result.pop(key, None)
    result.update(code_navigation_protocol=1, document_input_protocol=2,
        document_index_protocol=1, indexed_document_context=True, document_read_window_protocol=2,
        document_working_notes_protocol=1)
    return result


def binding(context, schema):
    marker = context[KEY]
    from .code_whole_policy import KEY as POLICY
    return full.make_binding({'workflow_id': marker['workflow_id'], 'attempt': marker['attempt'],
        'directory_sha256': marker['directory_sha256'],
        **{k: context[k] for k in ('proposal_hash', 'spec_hash', 'code_sha256', 'delivery_hash')},
        'implementation_document_sha256': marker['document']['sha256'],
        'architecture_document_sha256': marker['approved_architecture']['sha256'],
        'schema_sha256': full.sha(schema),
        **({READING_SCOPE: 1} if context.get(READING_SCOPE) == 1 else {}),
        **({'code_policy_sha256':context[POLICY]['sha256']} if POLICY in context else {})}, 'complete_code_delivery')


def restricted_view(context):
    return context.get(RESTRICTED_VIEW) in (1, RESTRICTED_VIEW_VERSION)


def reading_documents(context, *, required=False):
    documents = (context[KEY]['document'], context[KEY]['approved_architecture'])
    return documents[:1] if required and full.code_delivery_scope(context) else documents


def scope_directory(context, rows):
    allowed = tuple((doc['document_id'], doc['sha256']) for doc in reading_documents(context))
    return [row for row in rows if isinstance(row, dict)
        and (row.get('document_id'), row.get('sha256')) in allowed]


def permits_tool(context, name, args):
    if not isinstance(name, str) or name not in READ_TOOLS:
        return False
    if name.startswith('documents.'):
        if name == 'documents.recall':
            from .code_navigation import _memory_arguments
            args = _memory_arguments(context, args)
        return name == 'documents.list' and not args or any(
            doc['document_id'] == args.get('document_id') and doc['sha256'] == args.get('expected_sha256')
            for doc in reading_documents(context))
    if name in {'code.read', 'artifacts.read'}:
        return any(row['artifact_id'] == args.get('id') and row['sha256'] == args.get('expected_sha256')
            for row in context['delivery_artifacts'])
    return True  # Both directory tools and observation tools already bind this frozen owner.


def require_tool(context, name, args):
    if permits_tool(context, name, args):
        return
    from .tool_navigation import NavigationError
    raise NavigationError('代码复审仅可读取当前批准实现文档、批准架构和冻结交付文件。',
        code='outside_code_delivery_sources', next_calls=[{'name': 'documents.list', 'arguments': {}},
            {'name': 'artifacts.list', 'arguments': {'directory': '', 'offset': 0, 'limit': 20}}])


def review_schema(schema,context):
    from .code_repair_contract import review_schema as base_schema
    result=base_schema(schema)
    findings=context.get('legacy_code_review_findings',[])
    if findings:
        from .engine import obj
        ids=[f['issue_id'] for f in findings]
        result=copy.deepcopy(result)
        result['properties']['legacy_findings']={'type':'array','minItems':len(ids),'maxItems':len(ids),
            'items':obj({'issue_id':{'enum':ids},'status':{'enum':['resolved','not_applicable','still_valid','unknown']},
                'explanation':{'type':'string','minLength':1},'evidence_indices':{'type':'array','uniqueItems':True,
                    'items':{'type':'integer','minimum':0}},'issue_indices':{'type':'array','uniqueItems':True,
                    'items':{'type':'integer','minimum':0}}})}
        result['required'].append('legacy_findings')
    return result


def _covers(spans, length):
    cursor = 0
    for a, b in sorted(spans):
        if a > cursor:
            break
        cursor = max(cursor, b)
    return cursor >= length


def validate_legacy_findings(raw,history):
    findings=raw.get('legacy_findings',[])
    if (len(findings)!=len(history['findings']) or len({f['issue_id'] for f in findings})!=len(findings)
            or {f['issue_id'] for f in findings}!={f['issue_id'] for f in history['findings']}):
        raise full.ResponseCorrectionRequired('Whole code review must independently disposition every original finding',
            [{'kind': 'legacy_finding_coverage', 'required_issue_ids': [f['issue_id'] for f in history['findings']]}])
    for finding in findings:
        if (any(type(i) is not int or not 0<=i<len(raw['code_evidence']) for i in finding['evidence_indices'])
                or raw['approve'] and (finding['status'] not in {'resolved','not_applicable'} or not finding['evidence_indices'])):
            raise full.ResponseCorrectionRequired('Whole code approval lacks actual evidence resolving an original finding',
                [{'kind': 'legacy_finding_evidence', 'issue_id': finding['issue_id']}])
        indices=finding.get('issue_indices',[])
        if (any(type(i) is not int or not 0<=i<len(raw['issues']) for i in indices)
                or finding['status'] in {'still_valid','unknown'} and not indices):
            raise full.ResponseCorrectionRequired('Unresolved original findings require their actual current issue indices',
                [{'kind': 'legacy_finding_issue_indices', 'issue_id': finding['issue_id']}])


@operation
def validate_vote(engine, run_id, state, specification, attempt, vote):
    if not getattr(engine.store, 'is_read_snapshot', False) and hasattr(engine.store, 'read_snapshot'):
        with engine.store.read_snapshot() as snapshot:
            owner = copy.copy(engine)
            owner.store = snapshot
            return validate_vote(owner, run_id, state, specification, attempt, vote)
    from . import code_revision_review as revision
    if attempt.get(revision.KEY):
        return revision.validate_vote(engine,run_id,state,specification,attempt,vote)
    from .evidence_delivery import tracking_required, delivery_spans
    from .implementation_documents import read_coverage
    from .implementation_alignment_evidence import _code_text, _code_spans, validate_vote as alignment_vote
    from .implementation_whole_reading import required
    from . import code_whole_policy as policy
    frozen = full.authenticate(engine.store, run_id, vote)
    context = frozen['context']
    marker = context.get(KEY, {})
    raw = full.answer(vote)
    hid = vote[full.KEY]['handoff_id']
    expected = {'version': VERSION, 'workflow_id': state['id'], 'attempt': attempt['index'],
        'document': specification['document'], 'spec_hash': specification['spec_hash'],
        'approved_architecture': policy.architecture(state),
        'directory_sha256': digest(attempt['delivery_artifacts'])}
    exact = {'proposal_hash':state['proposal_hash'],'spec_hash':specification['spec_hash'],
        'code_sha256':attempt['code_sha256'],'delivery_hash':attempt['delivery_hash'],
        'implementation_document':specification['document'],
        'design_hash':digest(state['common']['design']),
        'experiment_plan_hash':digest(state['common'].get('experiment_plan') or {})}
    if (marker != expected or any(context.get(k)!=v for k,v in exact.items())
            or context.get(READING_SCOPE) != attempt.get(READING_SCOPE)
            or context['delivery_artifacts'] != attempt['delivery_artifacts']
            or vote[full.KEY]['policy'] != binding(context, frozen['schema'])
            or context.get(policy.KEY)!=state.get(policy.KEY)
            or not tracking_required(engine.store, run_id, hid)):
        raise ValueError('Whole code review has a different source, owner, schema or delivery identity')
    history=policy.validate_history(engine,run_id,state,specification,attempt)
    if context.get(policy.LINEAGE)!=attempt.get(policy.LINEAGE):
        raise ValueError('Whole code review changed the frozen legacy finding lineage')
    if context.get('prior_code_review_feedback')!=policy.previous_feedback(engine,run_id,state,specification,attempt):
        raise ValueError('Whole code review lost the authenticated previous-version feedback')
    if history:
        if (context.get('legacy_code_review_history_sha256')!=history['sha256']
                or context.get('legacy_code_review_findings')!=history['findings']):
            raise ValueError('Whole code review lost its authenticated original findings')
    for key in ('proposal_hash', 'spec_hash', 'code_sha256', 'delivery_hash'):
        if raw.get(key) != context[key]:
            raise ValueError('Whole code vote does not bind the exact current delivery')
    clarification = context.get('author_source_clarification')
    if clarification:
        from .implementation_alignment_evidence import _delivery
        prior = _delivery(engine,run_id,'engineer',clarification['handoff_id'],clarification['result'])
        scope = prior.get('whole_code_source_clarification',{})
        if (scope.get('version')!=1 or scope.get('workflow_id')!=state['id']
                or scope.get('source_attempt')!=attempt['index']
                or prior.get('delivery_artifacts')!=context['delivery_artifacts']
                or scope.get('source_sha256')!=clarification.get('source_sha256')):
            raise ValueError('Code source clarification lacks this exact author, source and attempt')
    full.validate_delivery(engine.store, run_id, vote, engine=engine)
    handoffs = full.reading_handoffs(engine.store, run_id, vote)
    validate_reading(engine, run_id, context, vote['role'], hid, handoffs)
    if history:
        validate_legacy_findings(raw, history)
    validate_response(raw, {c['id'] for c in specification['spec']['acceptance_checks']})
    from .harness_activity import checkpoint
    checkpoint('validation_alignment', 0, 1, '份对齐意见', vote['role'])
    alignment_vote(engine, run_id, vote['role'], hid, raw, context, delivery=True)
    checkpoint('validation_alignment', 1, 1, '份对齐意见', vote['role'])
    return context


def validate_reading(engine, run_id, context, role, hid, handoffs):
    from .implementation_alignment_evidence import _code_text, _code_spans
    from .implementation_whole_reading import required
    from .evidence_delivery import delivery_spans
    from .implementation_reading import coverage
    covered_chars = full.required_root_chars(context)
    from .harness_activity import checkpoint
    docs = reading_documents(context, required=True)
    for number, doc in enumerate(docs):
        checkpoint('validation_documents', number, len(docs), '份批准文档', role)
        checked = coverage(engine, run_id, doc, required(doc), handoffs)
        covered_chars += checked['covered_chars']
        if not checked['complete']:
            error = ValueError('Whole code review requires complete independent delivery of approved documents')
            ranges = checked['missing_ranges']
            error.missing_read_targets = [{'kind': 'document', 'document': doc, 'ranges': ranges}]
            if full.code_delivery_scope(context):
                section_starts = {s['id']: s['start'] for s in doc['sections']}
                calls = [{'name': 'documents.read', 'arguments': {'document_id': doc['document_id'],
                    'expected_sha256': doc['sha256'], 'section_id': span['section_id'],
                    'offset': offset - section_starts[span['section_id']], 'limit': min(full.PAGE_LIMIT, span['end'] - offset)}}
                    for span in ranges for offset in range(span['start'], span['end'], full.PAGE_LIMIT)]
                # Keep already-frozen full-reading children on their original identity.
                error.closeout_missing_read_targets = [{**error.missing_read_targets[0], 'read_with': calls}]
            error.covered_chars = covered_chars
            raise error
    checkpoint('validation_documents', len(docs), len(docs), '份批准文档', role)
    sources = context['delivery_artifacts']
    for number, source in enumerate(sources):
        checkpoint('validation_code', number, len(sources), '个交付文件', role)
        text = _code_text(engine, run_id, source['artifact_id'], source, allow_binary=True)
        if text is None:
            continue  # Only actual non-UTF-8 bytes use binary manifest/resource gates.
        actual = _code_spans(engine, hid, source['artifact_id'], source['sha256'], text)
        delivered = [(r['char_start'], r['char_end']) for owner in handoffs
            for r in delivery_spans(engine.store, run_id, role, source['artifact_id'], source['sha256'],
                identity_key='artifact_id', handoff_id=owner, text=text)]
        from .implementation_whole_reading import union_ranges
        covered_chars += sum(s['end']-s['start'] for s in union_ranges([
            {'section_id': source['artifact_id'], 'start': a, 'end': b} for a,b in delivered]))
        if not _covers(actual, len(text)) or not _covers(delivered, len(text)):
            error = ValueError('Whole code delivery was not completely read and submitted: ' + source['path'])
            from .review_material_reuse import union
            inspected = union([(max(a, c), min(b, d)) for a, b in actual for c, d in delivered
                               if max(a, c) < min(b, d)])
            missing, cursor = [], 0
            for a, b in inspected:
                if a > cursor:
                    missing.append([cursor, a])
                cursor = max(cursor, b)
            if cursor < len(text):
                missing.append([cursor, len(text)])
            # Preserve the legacy continuation identity. The richer controller
            # view must not change an already frozen paid full-reading child.
            error.missing_read_targets = [{'kind': 'code', 'source': source}]
            error.closeout_missing_read_targets = [{'kind': 'code', 'source': source, 'ranges': missing,
                'read_with': [{'name': 'code.read', 'arguments': {'id': source['artifact_id'],
                    'expected_sha256': source['sha256'], 'offset': offset, 'limit': min(6000, end-offset)}}
                    for start, end in missing for offset in range(start, end, 6000)]}]
            error.covered_chars = covered_chars
            raise error
    checkpoint('validation_code', len(sources), len(sources), '个交付文件', role)
    return {'covered_chars': covered_chars, 'documents': len(docs), 'code_files': len(sources)}


def validate_response(raw, requirements):
    """One contract for the complete answer, without guessing dispositions."""
    problems = []
    if raw['approve'] and (raw['issues'] or raw['fixes']):
        problems.append({'kind': 'approval_with_unresolved_changes'})
    findings = raw.get('repair_findings', [])
    if len(findings) != len(raw['issues']) or {r['issue_index'] for r in findings} != set(range(len(raw['issues']))):
        problems.append({'kind': 'issue_classification', 'required_issue_indices': list(range(len(raw['issues']))),
            'actual': [{'issue_index': f['issue_index'], 'requirement_id': f['requirement_id'],
                        'disposition': f['disposition']} for f in findings]})
    code_changes = set()
    for n, finding in enumerate(findings):
        if finding['requirement_id'] not in requirements or any(not 0 <= i < len(raw['code_evidence'])
                for i in finding['evidence_indices']):
            problems.append({'kind': 'invalid_requirement_or_evidence', 'finding_index': n,
                'requirement_ids': sorted(requirements)})
            continue
        if finding['disposition'] == 'repair_code':
            if not finding['evidence_indices'] or any(raw['code_evidence'][i]['requirement_id'] != finding['requirement_id']
                    for i in finding['evidence_indices']):
                problems.append({'kind': 'code_change_evidence', 'finding_index': n,
                    'requirement_id': finding['requirement_id']})
            code_changes.add(finding['issue_index'])
    if (not raw['approve'] and not raw['issues'] or
            {f['issue_index'] for f in raw['fixes']} != code_changes):
        problems.append({'kind': 'executable_fix_scope', 'required_issue_indices': sorted(code_changes),
            'actual_issue_indices': [f['issue_index'] for f in raw['fixes']]})
    if problems:
        raise full.ResponseCorrectionRequired('完整代码审核答复须逐问题唯一分类，且只有有据代码缺陷携带执行fixes。\n'
            + RESPONSE_GUIDANCE, problems)


def validate_release(engine, run_id, state, specification, attempt, votes):
    full.authenticate_complete(engine.store, run_id, votes)
    if full.reviewers(votes) != full.REVIEWERS:
        raise ValueError('A whole-delivery release requires the explicit full-review policy')
    for vote in votes:
        validate_vote(engine, run_id, state, specification, attempt, vote)
        raw = full.answer(vote)
        if not raw['approve'] or raw['issues'] or raw['fixes']:
            raise ValueError('Whole code release still has unresolved changes')


def repair_feedback(engine, run_id, state, specification, previous):
    votes = previous.get('review_votes', [])
    if not votes:
        return None
    for vote in votes:
        validate_vote(engine, run_id, state, specification, previous, vote)
    body = {'version': VERSION, 'workflow_id': state['id'], 'source_attempt': previous['index'],
        'source_delivery_hash': previous['delivery_hash'], 'spec_hash': specification['spec_hash'],
        'review_receipts': [copy.deepcopy(v[full.KEY]) for v in votes],
        'changes': [{'reviewer': v['role'], 'issues': copy.deepcopy(v['issues']), 'fixes': copy.deepcopy(v['fixes'])} for v in votes]}
    return {**body, 'sha256': digest(body)}


