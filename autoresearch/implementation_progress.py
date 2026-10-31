"""Small, read-only implementation activity projections; never approval gates."""
import hashlib
import json
from datetime import datetime


ROLES = ('director', 'engineer', 'scout')


def _latest(store, collection, run_id, cycle, fields, *, generation=None):
    columns = ','.join(f"'{key}',{expression}" for key, expression in fields.items())
    generation_filter = ("AND json_extract(data,'$.implementation_generation')=? "
        if collection == 'codex_workflows' and generation is not None else '')
    candidates = ("SELECT key,data,updated_at FROM records WHERE collection=? "
        "AND json_extract(data,'$.run_id')=? AND json_extract(data,'$.cycle')=? "
        + generation_filter + ("AND json_type(data,'$.implementation_indexed_review_protocol')='integer' "
        "AND json_extract(data,'$.implementation_indexed_review_protocol')=1 " if collection == 'codex_workflows' else '')
        + "ORDER BY updated_at DESC,key DESC LIMIT 1")
    if collection == 'codex_workflows':
        # Select the document revision once. Repeating json_each for every
        # sidebar field reparses all archived specifications in this large row.
        query = f"""WITH candidate AS MATERIALIZED ({candidates}),
            current_spec AS MATERIALIZED (
                SELECT s.key,s.value FROM candidate records,
                    json_each(records.data,'$.specifications') s
                WHERE json_type(s.value,'$.implementation_indexed_review_protocol')='integer'
                    AND json_extract(s.value,'$.implementation_indexed_review_protocol')=1
                    AND (json_extract(records.data,'$.current_revision') IS NULL
                    OR json_extract(records.data,'$.status') NOT IN ('specification','spec_review','blocked')
                    OR coalesce(json_array_length(records.data,'$.attempts'),0)>0
                    OR coalesce(json_extract(s.value,'$.index'),CAST(s.key AS INTEGER))
                        =json_extract(records.data,'$.current_revision'))
                ORDER BY CAST(s.key AS INTEGER) DESC LIMIT 1)
            SELECT json_object({columns}) FROM candidate records"""
    else:
        query = f"SELECT json_object({columns}) FROM records WHERE collection=? " \
            "AND json_extract(data,'$.run_id')=? AND json_extract(data,'$.cycle')=? " \
            "ORDER BY updated_at DESC,key DESC LIMIT 1"
    with store._read_db() as db:
        args = (collection, run_id, cycle)
        if generation_filter:
            args += (generation,)
        row = db.execute(query, args).fetchone()
    return json.loads(row[0]) if row else None


def _field(path):
    return f"json_extract(data,'$.{path}')"


def _specification_progress():
    """Project only saved convergence counters, never discussion bodies or gates."""
    fields = ('version', 'mode', 'completed_rounds', 'consecutive_no_progress',
              'stagnation_window', 'stalled', 'supported_progress')
    columns = ','.join(f"'{key}',{_field('specification_progress.' + key)}" for key in fields)
    return ("CASE WHEN json_type(data,'$.specification_progress')='object' THEN json_object("
            + columns + ",'reason',substr(json_extract(data,'$.specification_progress.reason'),1,600)) END")


def _current_spec(expression):
    """Document revisions and code-attempt revisions are separate counters."""
    return f"(SELECT {expression} FROM current_spec s)"


def _strict_protocol(value, path):
    return f"CASE WHEN json_type({value},'$.{path}')='integer' THEN json_extract({value},'$.{path}') END"


def _read_activity(db, handoff_id):
    predicate = "json_extract(t.value,'$.predecessor_handoff_id') IS NULL"
    args = (handoff_id,)
    row = db.execute("""SELECT json_object('read_calls',count(*),
        'read_documents',count(DISTINCT json_array(json_extract(t.value,'$.result.document_id'),
            json_extract(t.value,'$.result.sha256'))),
        'read_sections',count(DISTINCT json_array(json_extract(t.value,'$.result.document_id'),
            json_extract(t.value,'$.result.sha256'),json_extract(t.value,'$.result.section_id'))),
        'read_ranges',count(DISTINCT json_array(json_extract(t.value,'$.result.document_id'),
            json_extract(t.value,'$.result.sha256'),json_extract(t.value,'$.result.section_id'),
            json_extract(t.value,'$.result.char_start'),json_extract(t.value,'$.result.char_end'))))
        FROM records p,json_each(p.data,'$.tool_results') t
        WHERE p.collection='handoff_progress' AND p.key=?
            AND json_extract(t.value,'$.call.name')='documents.read'
            AND json_type(t.value,'$.result.text')='text'
            AND length(json_extract(t.value,'$.result.text'))>0
            AND json_extract(t.value,'$.result.error') IS NULL AND """ + predicate, args).fetchone()
    return json.loads(row[0])


def _authoring(store, run, workflow, cycle):
    """Project saved tool activity without loading source text or spec bodies."""
    if not workflow or type(workflow.get('revision')) is not int:
        return None
    with store._read_db() as db:
        row = db.execute("""SELECT json_object(
            'handoff_id',h.key, 'status',json_extract(h.data,'$.status'),
            'updated_at',max(h.updated_at,coalesce(p.updated_at,h.updated_at),coalesce(d.updated_at,h.updated_at)),
            'tool_turns',coalesce(json_extract(p.data,'$.turn'),0),
            'mode',json_extract(p.data,'$.mode'),
            'request_status',json_extract(p.data,'$.normal_request.status'),
            'last_tool',json_extract(p.data,'$.tool_results[#-1].call.name'),
            'last_tool_error',substr(json_extract(p.data,'$.tool_results[#-1].result.error'),1,2000),
            'last_tool_validation',json_extract(p.data,'$.tool_results[#-1].result.validation'),
            'error',substr(coalesce(json_extract(p.data,'$.finalization.error'),
                json_extract(p.data,'$.normal_request.error')),1,600),
            'draft_status',json_extract(d.data,'$.status'),
            'document_id',json_extract(d.data,'$.row.document.document_id'),
            'document_sha256',json_extract(d.data,'$.row.document.sha256'))
            FROM records h JOIN records i ON i.collection='handoff_inputs' AND i.key=h.key
            LEFT JOIN records p ON p.collection='handoff_progress' AND p.key=h.key
            LEFT JOIN records d ON d.collection='implementation_document_drafts' AND d.key=h.key
                AND json_extract(d.data,'$.workflow_id')=? AND json_extract(d.data,'$.revision')=?
            WHERE h.collection='handoffs' AND json_extract(h.data,'$.run_id')=?
                AND json_extract(h.data,'$.to_role')='engineer'
                AND json_extract(h.data,'$.purpose') LIKE 'implementation_spec:%'
                AND json_extract(i.data,'$.context.research_cycle')=?
                AND json_extract(i.data,'$.context.implementation_document_authoring_protocol')=1
                AND json_extract(i.data,'$.context.implementation_document_authoring.workflow_id')=?
                AND json_extract(i.data,'$.context.implementation_document_authoring.revision')=?
            ORDER BY h.updated_at DESC,h.key DESC LIMIT 1""",
            (workflow['id'], workflow['revision'], run['id'], cycle, workflow['id'], workflow['revision'])).fetchone()
        if not row:
            return None
        result = json.loads(row[0])
        result.update(_read_activity(db, result['handoff_id']))
        writes = db.execute("""SELECT json_object('write_calls',count(*),'saved_writes',
            coalesce(sum(json_extract(t.value,'$.result.saved')=1),0),
            'current_document_writes',coalesce(sum(json_extract(t.value,'$.result.saved')=1
                AND json_extract(t.value,'$.result.submission.document_id')=?
                AND json_extract(t.value,'$.result.submission.document_sha256')=?),0))
            FROM records p,json_each(p.data,'$.tool_results') t
            WHERE p.collection='handoff_progress' AND p.key=?
                AND json_extract(t.value,'$.call.name') IN ('implementation.write_document','implementation.edit_document')""",
            (result['document_id'], result['document_sha256'], result['handoff_id'])).fetchone()
        result.update(json.loads(writes[0]))
    result['document_saved'] = bool(result.pop('current_document_writes'))
    return result


def view(store, run):
    cycle = run.get('state', {}).get('research_cycle', 1)
    workflow = _latest(store, 'codex_workflows', run['id'], cycle, {
        'id': 'key', 'status': _field('status'), 'reason': "substr(json_extract(data,'$.reason'),1,600)",
        'blocked_kind': _field('blocked_kind'),
        'definition_repair': "json_object('id',json_extract(data,'$.implementation_definition_repair.id'),"
            "'status',json_extract(data,'$.implementation_definition_repair.status'))",
        'specification_progress': _specification_progress(),
        'updated_at': 'updated_at', 'revision': _field('current_revision'),
        'document_id': _current_spec("json_extract(s.value,'$.document.document_id')"),
        'document_sha256': _current_spec("json_extract(s.value,'$.document.sha256')"),
        'document_revision': _current_spec("coalesce(json_extract(s.value,'$.index'),CAST(s.key AS INTEGER))"),
        'spec_hash': _current_spec("json_extract(s.value,'$.spec_hash')"),
        '_indexed_review': _current_spec(_indexed_review_json('s.value')),
        'acceptance': "json_object('version',json_extract(data,'$.implementation_acceptance_protocol'),"
            "'phase',json_extract(data,'$.acceptance_review.phase'),'outcome',json_extract(data,'$.acceptance_review.outcome'),"
            "'contract',json_extract(data,'$.acceptance_review.contract'),'binding',json_extract(data,'$.acceptance_review.binding'),"
            "'coverage',json_extract(data,'$.acceptance_review.coverage'),'issues',json_extract(data,'$.acceptance_review.issues'),"
            "'round_count',coalesce(json_array_length(data,'$.acceptance_review.rounds'),0))",
        'attempt_count': "coalesce(json_array_length(data,'$.attempts'),0)",
        'request_dispatched': "EXISTS(SELECT 1 FROM json_each(records.data,'$.attempts') "
            "WHERE json_extract(value,'$.checkpoint.turn_dispatched_at') IS NOT NULL "
            "OR json_extract(value,'$.checkpoint.turn_id') IS NOT NULL)",
        'request_status': _field('attempts[#-1].checkpoint.status'),
        'file_count': "coalesce(json_array_length(data,'$.attempts[#-1].snapshot.files'),json_array_length(data,'$.attempts[#-1].files'),0)",
        'last_activity_at': _field('attempts[#-1].checkpoint.last_event_at')},
        generation=run.get('state', {}).get('implementation_generation'))
    if workflow:
        workflow['review_mode'] = ('acceptance_contract' if (workflow.get('acceptance') or {}).get('version') == 1 else 'indexed_review')
        if (workflow.get('definition_repair') or {}).get('id'):
            workflow['review_mode'] = 'definition_repair'
        if workflow['review_mode'] == 'indexed_review':
            workflow.pop('acceptance', None)
    if run['stage'] != 'implementation' and not workflow:
        return None
    result = {'run_id': run['id'], 'cycle': cycle, 'run_status': run['status'],
        'projection_only': True, 'workflow': workflow,
        'code_generation_started': bool(workflow and workflow['request_dispatched']),
        'phase': 'preparing', 'label': '准备实现交接',
        'message': '正在核对批准方案与实现前提，尚未保存本轮实现文档或代码请求。',
        'next_action': '实现前提核对完成后，由工程师编写实现文档。',
        'updated_at': run.get('updated_at')}
    if workflow:
        phase = workflow['status']
        if phase == 'blocked':
            phase = ('code_generation' if workflow['attempt_count'] else
                     'document_review' if workflow['document_id'] else 'document_authoring')
        phase = {'specification': 'document_authoring', 'spec_review': 'document_review',
            'implementing': 'code_generation'}.get(phase, phase)
        descriptions = {
            'document_authoring': ('编写代码实现文档', '工程师正在编写或修订实现 Markdown。',
                '文档保存并建立索引后，进入三方整篇架构与实现对齐。'),
            'document_review': ('审计代码实现文档', '独立角色正在核对实现文档及验收要求。',
                '同版本文档审计通过后，派发本机 Codex 代码实现。'),
            'code_generation': ('生成与修复代码', '已进入本机 Codex 写码流程，公开活动及交付记录见下方实现详情。',
                '实际交付文件保存后，进行本地验收。'),
            'acceptance': ('本地交付验收', '正在核验实际文件、语法及有界开发检查。',
                '本地验收完成后，进行三方实际代码评审。'),
            'code_review': ('三方实际代码评审', '独立角色正在审查实际磁盘产物与验收记录。',
                '实际代码评审通过后，进入实验验证。'),
            'completed': ('实现交付已完成', '本轮实现流程已保存完成记录，正式实验结果另行记录。',
                '进入实验验证，按批准计划执行。')}
        label, message, next_action = descriptions.get(phase, (result['label'], result['message'], result['next_action']))
        result.update(phase=phase, label=label, message=message, next_action=next_action,
            updated_at=workflow['updated_at'])
        if phase == 'code_generation' and not workflow['request_dispatched']:
            result.update(message='正在准备本机 Codex 实现交接，尚未派发实际写码请求。',
                next_action='完成实现交接门禁后，派发本机 Codex。')
    if workflow:
        if workflow['review_mode'] == 'indexed_review':
            indexed = _indexed_review_summary(workflow.get('_indexed_review'), run['id'], workflow)
            workflow.update(indexed_review=indexed, saved_scopes=indexed['counts']['saved_reviews'],
                expected_scopes=3, saved_finals=0, expected_finals=0)
            if result['phase'] == 'document_review':
                result.update(label='三方全文索引审核',
                    message='三方通过固定索引自行读取当前文档全文；第二轮起同时审核工程师给各自的回复。',
                    next_action='收齐三方意见后统一修订并逐条回复，再审核新版本，直到三方同版本通过。')
            elif result['phase'] == 'document_authoring':
                result.update(next_action='保存并提交当前版本后，将文档索引交给三方独立全文审核。')
        workflow.pop('_indexed_review', None)
        if result['phase'] == 'document_authoring':
            authoring = _authoring(store, run, workflow, cycle)
            if authoring:
                result['authoring'] = authoring
                result['updated_at'] = authoring['updated_at']
                if authoring['document_saved']:
                    result.update(message='工程师已保存实现 Markdown，正在检查并提交同版本文档。',
                        next_action='工程师提交已保存版本后，进入三方整篇架构与实现对齐。')
                elif authoring['mode'] == 'final_only':
                    result.update(message='源文档读取阶段已结束，但尚未保存实现 Markdown；代码生成尚未派发。',
                        next_action='工程师须成功写入并提交实现文档，再进行三方整篇架构与实现对齐。')
        if workflow['status'] == 'blocked' and (workflow['blocked_kind'] in {
                'codex_spec_rejected', 'codex_spec_no_progress'} or not workflow['attempt_count']):
            result.update(label='实现文档审查已暂停',
                message='已保存的实现文档和审查意见保留待处理；不会自动进入科研方案修订。',
                next_action='处理未决审查意见与实现依赖后，再继续当前科研轮次的实现流程；文档审查通过前不派发写码。')
    if run.get('error'):
        result['block_reason'] = run['error']
    elif workflow and workflow.get('reason'):
        result['block_reason'] = workflow['reason']
    if workflow and workflow['review_mode'] == 'acceptance_contract':
        value = workflow['acceptance']
        from .implementation_acceptance import TERMINAL, APPROVED
        value['scientific_approval'] = False
        value['projection_only'] = True
        value['budget_usage'] = store.get('implementation_acceptance_budgets', run['id'])
        value['issues'] = list((value.get('issues') or {}).values())
        if result['phase'] in {'document_authoring', 'document_review', 'preparing'}:
            outcome = value.get('outcome')
            result.update(label='实现文档验收' + ('：' + outcome if outcome in TERMINAL else ''),
                message='按冻结验收合同分工检查，修复后定向验证。阶段结束与文档通过分别记录。',
                next_action=('文档验收通过后进入代码实现；代码与实验结论继续独立验收。' if outcome in APPROVED else
                    '审核已结束，处理阻断证据、执行有预算的验证任务或明确重新规划后再提交。' if outcome in TERMINAL else
                    '完成必要验收项覆盖，独立验证阻断问题的关闭条件。'))
    definition = (workflow or {}).get('definition_repair') or {}
    if definition.get('status') in {'authoring','reviewing'}:
        result.update(label='补齐资源估算定义' if definition['status']=='authoring' else '独立审核补充定义',
            message='原批准正文与已完成进度保留；仅处理代码审查指出的定义缺口。',
            next_action='作者定向补充 → 三方审核；未通过交回作者，通过后交给 Codex 修改。')
    return result


def _project(store, query, args):
    with store._read_db() as db:
        return [json.loads(row[0]) for row in db.execute(query, args)]


def _review_json(value):
    """Select current review findings without prompts or model transcripts."""
    fields = ('role', 'approve', 'rationale', 'proposal_hash', 'spec_hash', 'document_sha256',
              'code_sha256', 'delivery_hash', 'handoff_id', 'issues', 'required_changes',
              'review_routing', 'issue_confirmations', 'fix_locations', 'reading_proof')
    columns = ','.join(f"'{field}',json_extract({value},'$.{field}')" for field in fields)
    return f"""json_object({columns},'fixes',
        (SELECT json_group_array(json_object('issue_index',json_extract(f.value,'$.issue_index'),
            'location',json_extract(f.value,'$.location'),'change',json_extract(f.value,'$.change'),
            'verification',json_extract(f.value,'$.verification')))
        FROM json_each({value},'$.fixes') f))"""


def _document_json(value):
    return f"""json_object('document_id',json_extract({value},'$.document_id'),
        'sha256',json_extract({value},'$.sha256'),'title',json_extract({value},'$.title'),
        'total_chars',json_extract({value},'$.total_chars'),
        'sections',(SELECT json_group_array(json_object('id',json_extract(sec.value,'$.id'),
            'title',json_extract(sec.value,'$.title'),'start',json_extract(sec.value,'$.start'),
            'end',json_extract(sec.value,'$.end'),'chars',json_extract(sec.value,'$.chars')))
            FROM json_each({value},'$.sections') sec))"""


def _indexed_review_json(value, *, detail=False):
    vote = (_review_json('v.value') if detail else """json_object(
        'role',json_extract(v.value,'$.role'),'approve',json_extract(v.value,'$.approve'),
        'handoff_id',json_extract(v.value,'$.handoff_id'),
        'proposal_hash',json_extract(v.value,'$.proposal_hash'),
        'spec_hash',json_extract(v.value,'$.spec_hash'),
        'document_sha256',json_extract(v.value,'$.document_sha256'),
        'reading_proof',json_extract(v.value,'$.reading_proof'),
        'has_findings',coalesce(json_array_length(v.value,'$.issues'),0)>0
            OR coalesce(json_array_length(v.value,'$.fixes'),0)>0)""")
    return f"""CASE WHEN json_type({value})='object' THEN json_object(
        'contract',json_extract({value},'$.implementation_indexed_review'),
        'approved',json_extract({value},'$.approved'),
        'architecture_documents',(SELECT json_group_array(json_object(
            'document_id',json_extract(a.value,'$.document_id'),'sha256',json_extract(a.value,'$.sha256')))
            FROM json_each({value},'$.input_documents') a
            WHERE json_extract(a.value,'$.kind')='architecture_design'),
        'response_documents',json_extract({value},'$.indexed_review_response_documents'),
        'votes',(SELECT json_group_array({vote}) FROM json_each({value},'$.review_votes') v)) END"""


def _indexed_review_summary(value, run_id, scope, *, proposal_hash=None, handoffs=()):
    """Current-version saved reading metadata, never an execution authorization."""
    expected = {'workflow_id': scope.get('workflow_id') or scope.get('id'),
        'revision': scope.get('revision') if 'workflow_id' in scope else scope.get('document_revision'),
        'spec_hash': scope.get('spec_hash'), 'document_id': scope.get('document_id'),
        'document_sha256': scope.get('document_sha256')}
    if proposal_hash is not None:
        expected['proposal_hash'] = proposal_hash
    value = value or {}
    contract = value.get('contract')
    valid = bool(expected['document_id'] and expected['spec_hash'] and expected['document_sha256']
        and type(expected['revision']) is int and (contract is None or
            contract.get('version') == 1 and contract.get('scientific_approval') in (False, 0)
            and all(contract.get('binding', {}).get(key) == item for key, item in expected.items())))
    reviews = []
    for role in ROLES:
        candidates = [vote for vote in value.get('votes', []) if vote.get('role') == role]
        vote = candidates[0] if len(candidates) == 1 else {}
        pending = next((item for item in reversed(handoffs) if item.get('role') == role), {})
        proof = vote.get('reading_proof') or {}
        architecture = value.get('architecture_documents') or []
        replies = [doc for doc in value.get('response_documents') or []
            if doc.get('response_role') == role]
        required = [*architecture, {'document_id': expected['document_id'], 'sha256': expected['document_sha256']}, *replies]
        scope_complete = len(architecture) == 1 and (expected['revision'] == 0 and not replies
            or type(expected['revision']) is int and expected['revision'] > 0 and len(replies) == 1
            and replies[0].get('target_spec_hash') == expected['spec_hash']
            and replies[0].get('target_document_sha256') == expected['document_sha256'])
        reading = bool(proof.get('handoff_id') == vote.get('handoff_id') and proof.get('handoff_id')
            and proof.get('reading_provenance') == 'same_handoff_documents_read_only'
            and proof.get('scientific_approval') in (False, 0)
            and scope_complete and len(proof.get('documents', [])) == len(required)
            and all(doc.get('complete') is True for doc in proof.get('documents', []))
            and all(item.get('document_id') and item.get('sha256') and any(
                doc.get('document_id') == item['document_id'] and doc.get('sha256') == item['sha256']
                for doc in proof.get('documents', [])) for item in required))
        matches = valid and all(vote.get(key) == item for key, item in expected.items()
            if key in {'proposal_hash', 'spec_hash', 'document_sha256'})
        verdict = vote.get('approve')
        findings = vote.get('has_findings') or vote.get('issues') or vote.get('fixes')
        bound = bool(matches and reading and verdict in (True, False, 0, 1) and verdict is not None)
        status = ('version_mismatch' if vote and not bound or len(candidates) > 1 else
            'changes_requested' if bound and (verdict in (False, 0) or findings) else
            'approved' if bound else pending.get('status') or 'pending')
        reviews.append({**vote, 'role': role, 'status': status, 'binding_valid': bound,
            'reading_complete': reading if vote else None,
            'handoff_id': vote.get('handoff_id') or pending.get('handoff_id'),
            'handoff_status': pending.get('status'), 'issues': vote.get('issues') or [],
            'fixes': vote.get('fixes') or [], 'fix_locations': vote.get('fix_locations') or []})
    counts = {'expected_reviews': 3, 'saved_reviews': sum(item['binding_valid'] for item in reviews),
        'approved_reviews': sum(item['status'] == 'approved' for item in reviews),
        'changes_required_reviews': sum(item['status'] == 'changes_requested' for item in reviews)}
    status = ('version_mismatch' if contract is not None and not valid
        or any(item['status'] == 'version_mismatch' for item in reviews) else
        'changes_requested' if counts['changes_required_reviews'] else
        'approved' if value.get('approved') and counts['approved_reviews'] == 3 else 'pending')
    return {'version': 1, **expected, 'run_id': run_id, 'binding_valid': valid,
        'scientific_approval': False, 'projection_only': True, 'mode': 'indexed_review',
        'roles': reviews, 'counts': counts, 'status': status,
        'response_documents': value.get('response_documents') or []}


def _review_delivery_json(value):
    fields = ('version', 'mode', 'complete', 'collection_complete', 'scientific_approval')
    columns = ','.join(f"'{key}',json_extract({value},'$.{key}')" for key in fields)
    return f"CASE WHEN json_type({value})='object' THEN json_object({columns}) END"


def _specifications(store, workflow_id, *, revision=None):
    document = _document_json("json_extract(s.value,'$.document')")
    review = _review_json('r.value')
    delivery = _review_delivery_json("json_extract(s.value,'$.review_delivery')")
    revision_filter = " AND coalesce(json_extract(s.value,'$.index'),CAST(s.key AS INTEGER))=?" if revision is not None else ''
    return _project(store, f"""SELECT json_object(
        'index',coalesce(json_extract(s.value,'$.index'),CAST(s.key AS INTEGER)),
        'spec_hash',json_extract(s.value,'$.spec_hash'),'document',{document},
        'document_protocol',json_extract(s.value,'$.document_protocol'),
        'implementation_spec_progress_protocol',json_extract(s.value,'$.implementation_spec_progress_protocol'),
        'implementation_issue_response_protocol',json_extract(s.value,'$.implementation_issue_response_protocol'),
        'implementation_indexed_review_protocol',{_strict_protocol('s.value', 'implementation_indexed_review_protocol')},
        'definition_review_purpose',json_extract(s.value,'$.definition_review_purpose'),
        'indexed_review',{_indexed_review_json('s.value', detail=True)},
        'approved',json_extract(s.value,'$.approved'),'review_delivery',{delivery},
        'authoring_handoff_id',json_extract(s.value,'$.authoring_handoff_id'),
        'review_votes',(SELECT json_group_array({review}) FROM json_each(s.value,'$.review_votes') r),
        'acceptance_checks',(SELECT json_group_array(json_object('id',json_extract(c.value,'$.id'),
            'requirement',json_extract(c.value,'$.requirement'),'verification',json_extract(c.value,'$.verification')))
            FROM json_each(s.value,'$.spec.acceptance_checks') c),
        'implementation_map',(SELECT json_group_array(json_object('requirement_id',json_extract(m.value,'$.requirement_id'),
            'location',json_extract(m.value,'$.location'),'behavior',json_extract(m.value,'$.behavior')))
            FROM json_each(s.value,'$.spec.implementation_map') m))
        FROM records w,json_each(w.data,'$.specifications') s
        WHERE w.collection='codex_workflows' AND w.key=?
            AND json_type(s.value,'$.implementation_indexed_review_protocol')='integer'
            AND json_extract(s.value,'$.implementation_indexed_review_protocol')=1
        {revision_filter} ORDER BY CAST(s.key AS INTEGER)""", (workflow_id,revision) if revision is not None else (workflow_id,))


def _draft(store, workflow_id, revision):
    document = _document_json("json_extract(data,'$.row.document')")
    rows = _project(store, f"""SELECT json_object('handoff_id',key,'revision',json_extract(data,'$.revision'),
        'status',json_extract(data,'$.status'),'updated_at',updated_at,
        'spec_hash',json_extract(data,'$.row.spec_hash'),'document',{document},
        'implementation_issue_response_protocol',json_extract(data,'$.row.implementation_issue_response_protocol'),
        'acceptance_checks',(SELECT json_group_array(json_object('id',json_extract(c.value,'$.id'),
            'requirement',json_extract(c.value,'$.requirement'),'verification',json_extract(c.value,'$.verification')))
            FROM json_each(data,'$.row.spec.acceptance_checks') c),
        'implementation_map',(SELECT json_group_array(json_object('requirement_id',json_extract(m.value,'$.requirement_id'),
            'location',json_extract(m.value,'$.location'),'behavior',json_extract(m.value,'$.behavior')))
            FROM json_each(data,'$.row.spec.implementation_map') m))
        FROM records WHERE collection='implementation_document_drafts'
            AND json_extract(data,'$.workflow_id')=? AND json_extract(data,'$.revision')=?
        ORDER BY updated_at DESC,key DESC LIMIT 1""", (workflow_id, revision))
    return rows[0] if rows else None


def _authoring_detail(store, activity):
    if not activity:
        return None
    rows = _project(store, """SELECT json_object('tool',json_extract(t.value,'$.call.name'),
        'document_id',json_extract(t.value,'$.result.document_id'),'sha256',json_extract(t.value,'$.result.sha256'),
        'section_id',json_extract(t.value,'$.result.section_id'),'title',json_extract(t.value,'$.result.title'),
        'char_start',json_extract(t.value,'$.result.char_start'),'char_end',json_extract(t.value,'$.result.char_end'))
        FROM records p,json_each(p.data,'$.tool_results') t
        WHERE p.collection='handoff_progress' AND p.key=?
            AND json_extract(t.value,'$.call.name')='documents.read'
            AND json_type(t.value,'$.result.text')='text'
            AND length(json_extract(t.value,'$.result.text'))>0
            AND json_extract(t.value,'$.result.error') IS NULL
            AND json_extract(t.value,'$.predecessor_handoff_id') IS NULL
        ORDER BY CAST(t.key AS INTEGER) DESC LIMIT 12""", (activity['handoff_id'],))
    control = _project(store, """SELECT json_object(
        'request_status',coalesce(json_extract(data,'$.normal_request.status'),json_extract(data,'$.finalization.status')),
        'last_tool',json_extract(data,'$.tool_results[#-1].call.name'),
        'last_tool_error',substr(json_extract(data,'$.tool_results[#-1].result.error'),1,2000),
        'last_tool_validation',json_extract(data,'$.tool_results[#-1].result.validation'),
        'pending_tools',(SELECT json_group_array(json_extract(p.value,'$.name'))
            FROM json_each(data,'$.pending_tool_calls') p))
        FROM records WHERE collection='handoff_progress' AND key=?""", (activity['handoff_id'],))
    return {**activity, **(control[0] if control else {}), 'recent_reads': rows,
            'save_mode': 'complete_document_atomic_save'}


def _handoff_reviews(store, run_id, purpose):
    review = _review_json("json_extract(d.data,'$.result')")
    rows = _project(store, f"""SELECT json_object('handoff_id',h.key,
        'role',json_extract(h.data,'$.to_role'),'status',json_extract(h.data,'$.status'),
        'error',substr(json_extract(h.data,'$.error'),1,2000),'updated_at',h.updated_at,
        'context_binding',json_object('proposal_hash',json_extract(i.data,'$.context.proposal_hash'),
            'spec_hash',json_extract(i.data,'$.context.spec_hash'),
            'document_sha256',json_extract(i.data,'$.context.document_sha256')),
        'result',CASE WHEN json_type(d.data,'$.result')='object' THEN {review} ELSE NULL END)
        FROM records h JOIN records i ON i.collection='handoff_inputs' AND i.key=h.key
        LEFT JOIN records d ON d.collection='decisions' AND d.key=h.key
            AND json_extract(d.data,'$.run_id')=? AND json_extract(d.data,'$.role')=json_extract(h.data,'$.to_role')
        WHERE h.collection='handoffs' AND json_extract(h.data,'$.run_id')=?
            AND json_extract(h.data,'$.purpose')=? ORDER BY h.updated_at,h.key""", (run_id,run_id,purpose))
    for row in rows:
        if row['result']:
            row['result'].update(role=row['role'],handoff_id=row['handoff_id'])
    return rows


def _review_status(review, expected):
    if not review:
        return 'pending'
    if any(not value or review.get(key) != value for key, value in expected.items()):
        return 'binding_mismatch'
    findings = any(review.get(key) for key in ('issues', 'fixes', 'required_changes'))
    if review.get('approve') is True or review.get('approve') == 1:
        return 'changes_required' if findings else 'passed'
    return 'changes_required' if review.get('approve') is False or review.get('approve') == 0 else 'pending'


def _review_delivery(spec, workflow_id, proposal_hash):
    value = spec.get('review_delivery')
    if (spec.get('implementation_indexed_review_protocol') != 1 or not isinstance(value, dict)
            or value.get('version') != 1 or value.get('mode') != 'indexed_full_round'
            or value.get('scientific_approval') not in (False, 0)):
        return None
    reviews = spec.get('review_votes') or []
    complete = bool(value.get('complete') and value.get('collection_complete') and len(reviews) == 3
        and {review.get('role') for review in reviews} == set(ROLES)
        and all(review.get('spec_hash') == spec['spec_hash']
            and review.get('document_sha256') == spec['document']['sha256']
            and review.get('proposal_hash') == proposal_hash for review in reviews))
    return {**value, 'workflow_id': workflow_id, 'revision': spec['index'],
        'spec_hash': spec['spec_hash'], 'document_sha256': spec['document']['sha256'],
        'complete': complete, 'binding_valid': complete, 'projection_only': True}


def _reviewer(role, reviews, expected, handoffs=()):
    candidates = [row for row in reviews if row.get('role') == role]
    # State and the corresponding decision can describe the same saved vote.
    unique = {}
    for row in candidates:
        identity = {key: row.get(key) for key in ('approve', 'rationale', *expected, 'issues', 'fixes', 'required_changes')}
        unique[json.dumps(identity, sort_keys=True, ensure_ascii=False)] = row
    candidate = next(iter(unique.values()), None)
    status = 'conflicting_records' if len(unique) > 1 else _review_status(candidate, expected)
    current = next((h for h in reversed(handoffs) if h['role'] == role), None)
    if candidate is None and current and current['status'] in {'queued', 'running', 'blocked', 'paused', 'failed', 'interrupted'}:
        status = current['status']
    return {**(candidate or {}), 'role': role, 'status': status,
            'binding_valid': bool(candidate and status not in {'binding_mismatch', 'conflicting_records'}),
            'handoff_id': (candidate or {}).get('handoff_id') or (current or {}).get('handoff_id'),
            'handoff_status': (current or {}).get('status'),
            'error': (current or {}).get('error'),
            'issues': (candidate or {}).get('issues') or [], 'fixes': (candidate or {}).get('fixes') or [],
            'required_changes': (candidate or {}).get('required_changes') or []}


def _workflow_meta(store, workflow_id):
    rows = _project(store, """SELECT json_object('proposal_hash',json_extract(data,'$.proposal_hash'),
        'spec_hash',coalesce((SELECT json_extract(s.value,'$.spec_hash')
            FROM json_each(data,'$.specifications') s
            WHERE json_extract(data,'$.implementation_definition_repair.status')='approved'
                AND json_extract(s.value,'$.index')=json_extract(data,'$.implementation_definition_repair.approved_index')
                AND json_extract(s.value,'$.definition_repair_id')=json_extract(data,'$.implementation_definition_repair.id')
                AND json_extract(s.value,'$.approved')=1),json_extract(data,'$.spec_hash'))) FROM records
        WHERE collection='codex_workflows' AND key=?""", (workflow_id,))
    return rows[0] if rows else {}


def _document_audit(store, run, workflow, spec, meta, specifications=()):
    if not spec or spec.get('implementation_indexed_review_protocol') != 1:
        return None
    if spec.get('definition_review_purpose'):
        handoffs=_handoff_reviews(store,run['id'],spec['definition_review_purpose'])
        expected={'proposal_hash':meta['proposal_hash'],'spec_hash':spec['spec_hash'],
                  'document_sha256':spec['document']['sha256']}
        reviews=[_reviewer(role,spec['review_votes'],expected,handoffs) for role in ROLES]
        return {'mode':'definition_repair','run_id':run['id'],'workflow_id':workflow['id'],
            'revision':spec['index'],'document_id':spec['document']['document_id'],
            'document_sha256':spec['document']['sha256'],'spec_hash':spec['spec_hash'],
            'binding_valid':True,'approved_recorded':spec['approved'] is True or spec['approved']==1,
            'status':'approved' if spec['approved'] else 'changes_required' if spec['approved']==0 else 'pending',
            'roles':reviews,'scope':'definition_addition_only','scientific_approval':False,'projection_only':True}
    if workflow.get('review_mode') == 'acceptance_contract':
        value = workflow['acceptance']
        binding = value.get('binding') or {}
        valid = (binding.get('revision') == spec['index'] and binding.get('spec_hash') == spec['spec_hash']
            and binding.get('document_sha256') == spec['document']['sha256'])
        return {'mode': 'acceptance_contract', 'run_id': run['id'], 'workflow_id': workflow['id'],
            'revision': spec['index'], 'document_id': spec['document']['document_id'],
            'document_sha256': spec['document']['sha256'], 'spec_hash': spec['spec_hash'],
            'binding_valid': valid, 'status': value['outcome'] if valid else 'version_mismatch',
            'acceptance': value, 'scope': 'implementation_document_only'}
    token = hashlib.sha256(json.dumps(workflow['id'], ensure_ascii=False).encode()).hexdigest()
    handoffs = _handoff_reviews(store, run['id'],
        f'implementation_document_review:{token}:{spec["spec_hash"]}:round:{spec["index"]}:indexed-review-v1')
    scope = {'workflow_id': workflow['id'], 'revision': spec['index'],
        'spec_hash': spec['spec_hash'], 'document_id': spec['document']['document_id'],
        'document_sha256': spec['document']['sha256']}
    indexed = _indexed_review_summary(spec.get('indexed_review'), run['id'], scope,
        proposal_hash=meta.get('proposal_hash'), handoffs=handoffs)
    return {**scope, 'run_id': run['id'], 'mode': 'indexed_review', 'indexed_review': indexed,
        'status': indexed['status'], 'expected_roles': list(ROLES),
        'counts': indexed['counts'], 'response_documents': indexed['response_documents'],
        'approved_recorded': bool(spec['approved']), 'scope': 'implementation_document_only'}


def previous_code_attempt(store, workflow_id, index):
    previous = _project(store, """SELECT json_object(
        'index',json_extract(a.value,'$.index'),'status',json_extract(a.value,'$.status'),
        'issues',json_extract(a.value,'$.issues'),
        'smoke',(SELECT json_group_array(json_object('variant',json_extract(c.value,'$.params.variant'),
            'passed',json_extract(c.value,'$.passed'),'duration_seconds',json_extract(c.value,'$.duration_seconds'),
            'issues',json_extract(c.value,'$.issues'))) FROM json_each(a.value,'$.acceptance.smoke') c))
        FROM records w,json_each(w.data,'$.attempts') a
        WHERE w.collection='codex_workflows' AND w.key=? AND json_extract(a.value,'$.index')<?
        ORDER BY CAST(a.key AS INTEGER) DESC LIMIT 1""", (workflow_id, index))
    value = previous[0] if previous else None
    if value:
        value['issues'] = list(dict.fromkeys(item for item in value.get('issues') or [] if isinstance(item, str)))[:12]
    return value


def _attempt(store, workflow_id):
    review = _review_json('r.value')
    rows = _project(store, f"""SELECT json_object('index',json_extract(a.value,'$.index'),
        'status',json_extract(a.value,'$.status'),'code_sha256',json_extract(a.value,'$.code_sha256'),
        'delivery_hash',json_extract(a.value,'$.delivery_hash'),
        'review_purpose',json_extract(a.value,'$.review_purpose'),
        'review_scope',json_extract(a.value,'$.review_scope'),
        'review_requirement_ids',json_extract(a.value,'$.review_requirement_ids'),
        'clarification_purpose',coalesce(json_extract(a.value,'$.verification_followup.purpose'),
            json_extract(a.value,'$.repair_clarification.purpose')),
        'clarification_status',coalesce(json_extract(a.value,'$.verification_followup.status'),
            json_extract(a.value,'$.repair_clarification.status')),
        'checkpoint_status',json_extract(a.value,'$.checkpoint.status'),
        'turn_id',json_extract(a.value,'$.checkpoint.turn_id'),
        'turn_dispatched_at',json_extract(a.value,'$.checkpoint.turn_dispatched_at'),
        'last_activity_at',json_extract(a.value,'$.checkpoint.last_event_at'),
        'snapshot_file_count',json_array_length(a.value,'$.snapshot.files'),
        'delivery_selection',json_extract(a.value,'$.snapshot.selection'),
        'snapshot_bytes',(SELECT sum(json_extract(f.value,'$.bytes')) FROM json_each(a.value,'$.snapshot.files') f),
        'execution_progress',CASE WHEN json_type(a.value,'$.execution_progress')='object' THEN json_object(
            'step',json_extract(a.value,'$.execution_progress.step'),
            'label',substr(json_extract(a.value,'$.execution_progress.label'),1,400),
            'started_at',json_extract(a.value,'$.execution_progress.started_at'),
            'updated_at',json_extract(a.value,'$.execution_progress.updated_at'),
            'completed',json_extract(a.value,'$.execution_progress.completed'),
            'total',json_extract(a.value,'$.execution_progress.total'),
            'variant',json_extract(a.value,'$.execution_progress.variant'),
            'check_id',json_extract(a.value,'$.execution_progress.check_id'),
            'timeout_seconds',json_extract(a.value,'$.execution_progress.timeout_seconds'),
            'checks',(SELECT json_group_array(json_object('id',json_extract(c.value,'$.id'),
                'status',json_extract(c.value,'$.status'),'passed',json_extract(c.value,'$.passed'),
                'duration_seconds',json_extract(c.value,'$.duration_seconds'),'issues',json_extract(c.value,'$.issues')))
                FROM json_each(a.value,'$.execution_progress.checks') c)) ELSE NULL END,
        'activities',(SELECT json_group_array(json(activity)) FROM (SELECT json_object(
            'id',substr(json_extract(t.value,'$.id'),1,200),'type',json_extract(t.value,'$.type'),
            'status',CASE WHEN json_extract(t.value,'$.status') IN ('inProgress','completed','failed','declined')
                THEN json_extract(t.value,'$.status') ELSE NULL END,
            'source',CASE WHEN json_extract(t.value,'$.source') IN ('stream','readback')
                THEN json_extract(t.value,'$.source') ELSE NULL END,
            'observed_at',substr(json_extract(t.value,'$.observed_at'),1,64),
            'summary',substr(json_extract(t.value,'$.summary'),1,240),
            'paths',json_extract(t.value,'$.paths'),
            'started_at',json_extract(t.value,'$.started_at'),
            'completed_at',json_extract(t.value,'$.completed_at'),
            'duration_seconds',json_extract(t.value,'$.duration_seconds'),
            'exit_code',CASE WHEN json_type(t.value,'$.exit_code')='integer'
                THEN json_extract(t.value,'$.exit_code') ELSE NULL END) activity
            FROM json_each(a.value,'$.checkpoint.activities') t WHERE t.type='object'
                AND json_extract(t.value,'$.type') IN ('commandExecution','fileChange','contextCompaction')
            ORDER BY CAST(t.key AS INTEGER) DESC LIMIT 8)),
        'public_messages',(SELECT json_group_array(json(message)) FROM (SELECT json_object(
            'id',substr(json_extract(m.value,'$.id'),1,200),'phase','commentary',
            'text',substr(json_extract(m.value,'$.text'),1,800)) message
            FROM json_each(a.value,'$.checkpoint.public_messages') m WHERE m.type='object'
                AND json_extract(m.value,'$.phase')='commentary' AND json_type(m.value,'$.text')='text'
            ORDER BY CAST(m.key AS INTEGER) DESC LIMIT 3)),
        'event_counts',json_object(
            'turn/started',json_extract(a.value,'$.checkpoint.event_counts."turn/started"'),
            'turn/completed',json_extract(a.value,'$.checkpoint.event_counts."turn/completed"'),
            'item/started',json_extract(a.value,'$.checkpoint.event_counts."item/started"'),
            'item/completed',json_extract(a.value,'$.checkpoint.event_counts."item/completed"'),
            'error',json_extract(a.value,'$.checkpoint.event_counts.error'),
            'thread/tokenUsage/updated',json_extract(a.value,'$.checkpoint.event_counts."thread/tokenUsage/updated"')),
        'issues',json_extract(a.value,'$.issues'),
        'files',(SELECT json_group_array(json_object('path',json_extract(f.value,'$.path'),
            'sha256',json_extract(f.value,'$.sha256'),'bytes',json_extract(f.value,'$.bytes'),
            'verified',json_extract(f.value,'$.verified')))
            FROM json_each(a.value,'$.files') f),
        'acceptance',json_object('present',json_type(a.value,'$.acceptance')='object',
            'approved',json_extract(a.value,'$.acceptance.approved'),'issues',json_extract(a.value,'$.acceptance.issues'),
            'code_sha256',json_extract(a.value,'$.acceptance.code_sha256'),'delivery_hash',json_extract(a.value,'$.acceptance.delivery_hash'),
            'scope',json_extract(a.value,'$.acceptance.scope'),
            'syntax',CASE WHEN json_type(a.value,'$.acceptance.syntax')='object'
                THEN json_object('passed',json_extract(a.value,'$.acceptance.syntax.passed'),
                    'status',json_extract(a.value,'$.acceptance.syntax.status'),
                    'issues',json_extract(a.value,'$.acceptance.syntax.issues')) ELSE NULL END,
            'requirements',(SELECT json_group_array(json_object('id',json_extract(r.value,'$.id'),
                'locations',json_extract(r.value,'$.locations'),'verification',json_extract(r.value,'$.verification')))
                FROM json_each(a.value,'$.acceptance.requirements') r WHERE r.type='object'),
            'smoke',(SELECT json_group_array(json_object('id',json_extract(c.value,'$.id'),
                'status',json_extract(c.value,'$.status'),'passed',json_extract(c.value,'$.passed'),
                'exit_code',json_extract(c.value,'$.exit_code'),'duration_seconds',json_extract(c.value,'$.duration_seconds'),
                'variant',json_extract(c.value,'$.params.variant'),'issues',json_extract(c.value,'$.issues'),
                'scope',json_extract(c.value,'$.scope')))
                FROM json_each(a.value,'$.acceptance.smoke') c)),
        'reviews',(SELECT json_group_array({review}) FROM json_each(a.value,
            CASE WHEN json_type(a.value,'$.verification_followup')='object' THEN '$.verification_followup.votes'
                WHEN json_type(a.value,'$.repair_clarification')='object'
                THEN '$.repair_clarification.votes' ELSE '$.review_votes' END) r))
        FROM records w,json_each(w.data,'$.attempts') a
        WHERE w.collection='codex_workflows' AND w.key=?
          AND coalesce(json_extract(a.value,'$.status'),'') != 'superseded'
        ORDER BY CAST(a.key AS INTEGER) DESC LIMIT 1""", (workflow_id,))
    if not rows:
        return None
    result = rows[0]
    result['activities'].reverse()
    result['public_messages'].reverse()
    result['event_counts'] = {key: value for key, value in result['event_counts'].items()
        if type(value) is int and value >= 0}
    return result


def _diagnostic_json(value):
    fields = ('stage', 'rpc_method', 'category', 'next_action', 'observed_at')
    columns = ','.join(f"'{name}',json_extract({value},'$.{name}')" for name in fields)
    return f"""CASE WHEN json_type({value})='object' THEN json_object({columns},
        'retry_count',CASE WHEN json_type({value},'$.retry_count')='integer'
            THEN json_extract({value},'$.retry_count') ELSE NULL END,
        'retry_limit',CASE WHEN json_type({value},'$.retry_limit')='integer'
            THEN json_extract({value},'$.retry_limit') ELSE NULL END) ELSE NULL END"""


def _safe_initialization(value):
    """Only the native adapter's fixed categories and bounded counters are public."""
    from .local_codex import ACCOUNT_RETRY_RECEIPT_LIMITS
    if not isinstance(value, dict):
        return None
    if any(not isinstance(value.get(name), str) for name in (
            'stage', 'rpc_method', 'category', 'next_action', 'observed_at')):
        return None
    if value['stage'] == 'reconnect':
        valid = (value['rpc_method'] == 'initialize'
                 and value['category'] in {'initialize_timeout', 'initialize_internal_error_unclassified',
                     'account_read_timeout', 'internal_error_unclassified', 'initialization_transport_closed',
                     'initialization_transport_eof', 'initialization_read_timeout'}
                 and value['next_action'] in {'retry_initialization_connection', 'retry_exhausted'}
                 and type(value.get('retry_limit')) is int and value['retry_limit'] == 3)
    else:
        valid = (value['stage'] in {'initialize', 'account_read', 'authenticated'}
                 and value['rpc_method'] == ('initialize' if value['stage'] == 'initialize' else 'account/read')
                 and (value['category'] in {'checking', 'ready', 'account_read_timeout',
                     'internal_error_unclassified', 'authentication_required', 'request_rejected', 'rpc_error'}
                     or value['stage'] == 'initialize' and value['category'] in {
                         'initialize_timeout', 'initialize_internal_error_unclassified'})
                 and value['next_action'] in {'retry_account_read', 'check_login', 'retry_exhausted',
                     'inspect_initialization', 'read_original_turn', 'continue'}
                 and type(value.get('retry_limit')) is int
                 and value['retry_limit'] in ACCOUNT_RETRY_RECEIPT_LIMITS)
    if (not valid or type(value.get('retry_count')) is not int
            or not 0 <= value['retry_count'] <= value['retry_limit']):
        return None
    observed = value.get('observed_at')
    try:
        if not isinstance(observed, str) or len(observed) > 64 or datetime.fromisoformat(observed).tzinfo is None:
            return None
    except ValueError:
        return None
    return value


def _initialization(store, run, workflow, revision, phase, spec, meta, delivery):
    """Exact frozen request linkage, using SQL without decoding any request body."""
    empty = lambda role: {'role': role, 'handoff_id': None, 'purpose': None,
        'handoff_status': None, 'request_status': None, 'operation_id': None,
        'request_id': None, 'source': None, 'diagnostic_status': 'no_record', 'diagnostic': None}
    roles = {role: empty(role) for role in ROLES}
    result = {'scope': 'current_workflow_requests', 'workflow_id': workflow['id'],
        'document_revision': revision, 'roles': list(roles.values())}
    token = hashlib.sha256(json.dumps(workflow['id'], ensure_ascii=False).encode()).hexdigest()
    args = [run['id'], run['id'], run.get('state', {}).get('research_cycle', 1)]
    purpose_args = []
    if phase == 'document_authoring':
        handoff_predicate = "json_extract(h.data,'$.to_role')='engineer' AND json_extract(h.data,'$.purpose') LIKE 'implementation_spec:%'"
        predicate = """json_extract(h.data,'$.to_role')='engineer'
            AND json_extract(h.data,'$.purpose') LIKE 'implementation_spec:%'
            AND json_extract(i.data,'$.context.implementation_document_authoring_protocol')=1
            AND json_extract(i.data,'$.context.implementation_document_authoring.workflow_id')=?
            AND json_extract(i.data,'$.context.implementation_document_authoring.revision')=?"""
        args += [workflow['id'], revision]
    elif phase == 'document_review' and spec and spec.get('definition_review_purpose'):
        purpose_args=[spec['definition_review_purpose']]
        handoff_predicate="json_extract(h.data,'$.purpose')=?"
        predicate="""json_extract(h.data,'$.purpose')=?
            AND json_extract(i.data,'$.context.implementation_definition_repair_protocol')=1
            AND json_extract(i.data,'$.context.spec_hash')=?"""
        args += [*purpose_args,spec['spec_hash']]
    elif phase == 'document_review' and spec:
        purpose_args = [f'implementation_document_review:{token}:{spec["spec_hash"]}:round:{revision}:indexed-review-v1']
        handoff_predicate = "json_extract(h.data,'$.purpose')=?"
        predicate = """json_extract(h.data,'$.purpose')=?
            AND json_type(i.data,'$.context.implementation_indexed_review_protocol')='integer'
            AND json_extract(i.data,'$.context.implementation_indexed_review_protocol')=1
            AND json_extract(i.data,'$.context.proposal_hash')=?
            AND json_extract(i.data,'$.context.spec_hash')=?
            AND json_extract(i.data,'$.context.document_sha256')=?"""
        args += [*purpose_args, meta.get('proposal_hash'), spec['spec_hash'], spec['document']['sha256']]
    elif phase == 'code_review' and delivery:
        handoff_predicate = "json_extract(h.data,'$.purpose')=?"
        predicate = """json_extract(h.data,'$.purpose')=?
            AND json_extract(i.data,'$.context.proposal_hash')=?
            AND json_extract(i.data,'$.context.spec_hash')=?
            AND json_extract(i.data,'$.context.code_sha256')=?
            AND json_extract(i.data,'$.context.delivery_hash')=?"""
        purpose_args = [delivery.get('clarification_purpose') or delivery.get('review_purpose') or f'code_review:{token}:{delivery["index"]}']
        args += [*purpose_args, meta.get('proposal_hash'),
            meta.get('spec_hash'), delivery['code_sha256'], delivery['delivery_hash']]
    elif phase in {'code_generation', 'acceptance', 'completed'} and delivery:
        diagnostic = _diagnostic_json("json_extract(a.value,'$.checkpoint.initialization_diagnostic')")
        rows = _project(store, f"""SELECT json_object('operation_id',json_extract(a.value,'$.operation_id'),
            'request_status',json_extract(a.value,'$.checkpoint.status'),
            'diagnostic',CASE WHEN json_extract(a.value,'$.operation_id') IS NOT NULL
                AND json_extract(a.value,'$.checkpoint.operation_id')=json_extract(a.value,'$.operation_id')
                THEN {diagnostic} ELSE NULL END)
            FROM records w,json_each(w.data,'$.attempts') a
            WHERE w.collection='codex_workflows' AND w.key=? AND json_extract(w.data,'$.run_id')=?
            ORDER BY CAST(a.key AS INTEGER) DESC LIMIT 1""", (workflow['id'], run['id']))
        if rows:
            value = rows[0]
            diagnostic = _safe_initialization(value.pop('diagnostic'))
            roles['engineer'].update(value, source='code_delivery_checkpoint', diagnostic=diagnostic,
                diagnostic_status='recorded' if diagnostic else 'no_record')
        return result
    else:
        return result
    # final_only supersedes its normal request. Do not fall back to the old
    # authenticated request when a new finalization has no native record yet.
    control = "CASE WHEN json_extract(p.data,'$.mode')='final_only' THEN json_extract(p.data,'$.finalization') ELSE json_extract(p.data,'$.normal_request') END"
    # Handoffs are small. Order their identities before opening any frozen
    # input; old requests contain large contexts that are irrelevant once a
    # role's latest valid request has been found. This is not a LIMIT shortcut:
    # invalid bindings and superseded scopes still fall through to older rows.
    rows = _project(store, f"""SELECT json_object('handoff_id',h.key,
        'role',json_extract(h.data,'$.to_role')) FROM records h
        WHERE h.collection='handoffs' AND json_extract(h.data,'$.run_id')=?
            AND json_extract(h.data,'$.to_role') IN ('director','engineer','scout')
            AND ({handoff_predicate}) ORDER BY h.updated_at DESC,h.key DESC""", (run['id'], *purpose_args))
    diagnostic = _diagnostic_json("json_extract(n.data,'$.checkpoint.initialization_diagnostic')")
    prepared_binding = predicate.replace("json_extract(i.data,'$.context.",
        f"json_extract({control},'$.prepared.context.").replace("json_type(i.data,'$.context.",
        f"json_type({control},'$.prepared.context.")
    seen = set()
    wanted_roles = {'engineer'} if phase == 'document_authoring' else set(ROLES)
    for identity in rows:
        if identity['role'] in seen:
            continue
        # Reapply the original run/cycle/purpose/document predicates using the
        # candidate primary key. No prepared/native status is trusted yet.
        bound = _project(store, f"""SELECT json_object('handoff_id',h.key,
            'role',json_extract(h.data,'$.to_role'),'purpose',json_extract(h.data,'$.purpose'),
            'handoff_status',json_extract(h.data,'$.status'))
            FROM records h JOIN records i ON i.collection='handoff_inputs' AND i.key=h.key
                AND json_extract(i.data,'$.run_id')=?
            WHERE h.collection='handoffs' AND h.key=? AND json_extract(h.data,'$.run_id')=?
                AND json_extract(h.data,'$.to_role')=?
                AND json_extract(i.data,'$.context.research_cycle')=? AND ({predicate})""",
            (run['id'], identity['handoff_id'], run['id'], identity['role'], *args[2:]))
        if not bound:
            continue
        candidate = bound[0]
        role = candidate['role']
        if role not in roles or role in seen:
            continue
        seen.add(role)
        # Only the selected current request needs checkpoint state. Historical
        # tool_results can be megabytes and must not be parsed for every candidate.
        request = _project(store, f"""SELECT json_object('request_status',json_extract({control},'$.status'))
            FROM records p WHERE p.collection='handoff_progress' AND p.key=?
                AND json_extract(p.data,'$.run_id')=?""", (candidate['handoff_id'], run['id']))
        candidate['request_status'] = request[0]['request_status'] if request else None
        # The run index narrows archived inputs before any body comparison.
        # Compare the latest exact assignment input, then use the
        # canonical native request key for a primary-key lookup, never scan its
        # large instruction/checkpoint collection during sidebar polling.
        source = _project(store, f"""WITH current_request AS MATERIALIZED (
                SELECT key,data FROM records WHERE collection='handoff_progress' AND key=?
                    AND json_extract(data,'$.run_id')=?),
            recent_inputs AS MATERIALIZED (SELECT t.data FROM records t,current_request p
                WHERE t.collection='transcripts' AND json_extract(t.data,'$.run_id')=?
                    AND json_extract(t.data,'$.role')=? AND json_extract(t.data,'$.kind')='input'
                    AND json_extract(t.data,'$.context._purpose')=?
                    AND (json_extract(t.data,'$.context._working_set.handoff_id') IS NULL
                        OR json_extract(t.data,'$.context._working_set.handoff_id')=p.key)
                    AND json_extract(t.data,'$.task')=json_extract({control},'$.prepared.task')
                ORDER BY t.updated_at DESC,t.key DESC LIMIT 1)
            SELECT json_object('operation_id',json_extract(t.data,'$.operation_id'))
            FROM current_request p JOIN records h ON h.collection='handoffs' AND h.key=p.key
            JOIN recent_inputs t ON json(json_extract(t.data,'$.context'))=json(json_extract({control},'$.prepared.context'))
            WHERE json_type(t.data,'$.operation_id')='text'
                AND json_extract({control},'$.prepared.role')=?
                AND json_extract({control},'$.prepared.context.research_cycle')=?
                AND ({prepared_binding}) LIMIT 1""",
            (candidate['handoff_id'], run['id'], run['id'], role, candidate['purpose'], role,
             run.get('state', {}).get('research_cycle', 1), *args[3:]))
        native = []
        if source:
            operation = source[0]['operation_id']
            native_id = hashlib.sha256(json.dumps([run['id'], role, operation, 'codex-reasoning-v1']).encode()).hexdigest()
            native = _project(store, f"""SELECT json_object('request_id',n.key,
                'operation_id',json_extract(n.data,'$.operation_id'),
                'request_status',json_extract(n.data,'$.checkpoint.status'),
                'diagnostic',CASE WHEN json_extract(n.data,'$.checkpoint.operation_id')=n.key
                    THEN {diagnostic} ELSE NULL END)
                FROM records n JOIN records p ON p.collection='handoff_progress' AND p.key=?
                WHERE n.collection='codex_reasoning' AND n.key=?
                    AND json_extract(n.data,'$.run_id')=? AND json_extract(n.data,'$.role')=?
                    AND json_extract(n.data,'$.operation_id')=?
                    AND json(json_extract(n.data,'$.schema'))=json(json_extract({control},'$.prepared.schema'))""",
                (candidate['handoff_id'], native_id, run['id'], role, operation))
        value = native[0] if native else {}
        diagnostic_value = _safe_initialization(value.pop('diagnostic', None))
        roles[role].update(candidate, **value, source='codex_reasoning' if native else None,
            diagnostic=diagnostic_value, diagnostic_status='recorded' if diagnostic_value else 'no_record')
        if wanted_roles <= seen:
            break
    return result


def _code_delivery(store, run, workflow, spec, meta):
    attempt = _attempt(store, workflow['id'])
    if not attempt:
        return None
    attempt['previous_attempt'] = previous_code_attempt(store, workflow['id'], attempt['index'])
    expected = {'proposal_hash': meta.get('proposal_hash'), 'spec_hash': meta.get('spec_hash') or (spec or {}).get('spec_hash'),
                'code_sha256': attempt['code_sha256'], 'delivery_hash': attempt['delivery_hash']}
    token = hashlib.sha256(json.dumps(workflow['id'], ensure_ascii=False).encode()).hexdigest()
    handoffs = _handoff_reviews(store, run['id'],
        attempt.get('clarification_purpose') or attempt.get('review_purpose') or f"code_review:{token}:{attempt['index']}")
    votes = [*attempt.pop('reviews'), *(h['result'] for h in handoffs if h['result'])]
    reviewers = [_reviewer(role, votes, expected, handoffs) for role in ROLES]
    status = ('changes_required' if any(r['status'] == 'changes_required' for r in reviewers) else
              'binding_mismatch' if any(r['status'] in {'binding_mismatch', 'conflicting_records'} for r in reviewers) else
              'passed' if all(r['status'] == 'passed' for r in reviewers) else 'pending')
    acceptance = attempt['acceptance']
    acceptance['binding_valid'] = bool(acceptance['present'] and attempt['code_sha256'] and attempt['delivery_hash']
        and acceptance['code_sha256'] == attempt['code_sha256'] and acceptance['delivery_hash'] == attempt['delivery_hash'])
    acceptance['status'] = ('pending' if not acceptance['present'] else 'binding_mismatch' if not acceptance['binding_valid']
        else 'passed' if acceptance['approved'] and not acceptance['issues'] else 'changes_required')
    attempt.update(dispatched=bool(attempt['turn_id'] or attempt['turn_dispatched_at']),
        reviews=reviewers, review_status=status, review_scope=attempt.get('review_scope') or 'whole_delivery',
        specific_function_verification=False)
    return attempt


def _functions(spec, delivery):
    if not spec:
        return []
    acceptance = (delivery or {}).get('acceptance') or {}
    declarations = acceptance.get('requirements') or []
    files = {row['path'] for row in (delivery or {}).get('files') or []}
    rows = []
    for check in spec.get('acceptance_checks') or []:
        planned = [m for m in spec.get('implementation_map') or [] if m['requirement_id'] == check['id']]
        declared = [r for r in declarations if r['id'] == check['id']]
        declaration = declared[0] if len(declared) == 1 else {}
        locations = declaration.get('locations') if isinstance(declaration.get('locations'), list) else []
        mapping = ('not_delivered' if not acceptance.get('present') else 'missing' if not declared else
            'duplicate' if len(declared) != 1 else 'binding_mismatch' if not acceptance.get('binding_valid') else
            'invalid_locations' if not locations or any(not isinstance(loc, str) or loc.split(':', 1)[0] not in files for loc in locations) else
            'missing_verification' if not isinstance(declaration.get('verification'), str) or not declaration['verification'].strip() else 'declared')
        rows.append({**check, 'id': check['id'], 'planned_locations': [m['location'] for m in planned],
            'planned_implementation': planned, 'declared_locations': locations,
            'declared_verification': declaration.get('verification'), 'mapping_status': mapping,
            'acceptance_status': acceptance.get('status', 'pending'), 'acceptance_scope': 'whole_delivery',
            'code_review_status': ('outside_current_scope' if (delivery or {}).get('review_scope') == 'definition_followup'
                and check['id'] not in ((delivery or {}).get('review_requirement_ids') or [])
                else (delivery or {}).get('review_status', 'not_started')),
            'code_review_scope': (delivery or {}).get('review_scope') or 'whole_delivery', 'specific_verification_status': 'not_recorded',
            'function_completed': None})
    return rows


def detail(store, run):
    """On-demand recorded progress. No source/spec/code bodies or gate decisions."""
    result = view(store, run)
    if not result or not result['workflow']:
        return result
    workflow = result['workflow']
    specifications = _specifications(store, workflow['id'])
    meta = _workflow_meta(store, workflow['id'])
    if result['phase'] in {'document_authoring', 'document_review'} and type(workflow['revision']) is int:
        revision = workflow['revision']
        spec = next((s for s in specifications if s['index'] == revision), None)
    else:
        spec = next((s for s in reversed(specifications) if s['spec_hash'] == meta.get('spec_hash')), None)
        spec = spec or next(iter(reversed(specifications)), None)
        revision = spec['index'] if spec else workflow['revision'] or 0
    draft = _draft(store, workflow['id'], revision)
    activity = _authoring(store, run, {**workflow, 'revision': revision}, result['cycle'])
    authoring = _authoring_detail(store, activity)
    current = spec or draft
    document = {'revision': revision, 'spec_hash': (current or {}).get('spec_hash'),
        **((current or {}).get('document') or {}),
        'status': 'submitted' if spec else 'draft' if draft and activity and activity['document_saved'] else
            'save_unconfirmed' if draft else 'not_saved',
        'authoring': authoring,
        'save_mode': 'complete_document_atomic_save'}
    document.setdefault('sections', [])
    audit = _document_audit(store, run, workflow, spec, meta, specifications)
    delivery = _code_delivery(store, run, workflow, spec, meta)
    result['details'] = {'document': document, 'document_audit': audit,
        'review_delivery': _review_delivery(spec, workflow['id'], meta.get('proposal_hash')) if spec else None,
        'identity': {'workflow_id': workflow['id'], 'document_revision': revision,
            'document_sha256': document.get('sha256'), 'spec_hash': document['spec_hash'],
            'workflow_revision': workflow['revision'], 'attempt_index': (delivery or {}).get('index')},
        'initialization': _initialization(store, run, workflow, revision, result['phase'], spec, meta, delivery),
        'code_delivery': delivery, 'functions': _functions(current, delivery),
        'projection_only': True, 'scientific_approval': False}
    return result
