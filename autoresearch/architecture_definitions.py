"""Indexed, bounded scientific definition review before architecture resumes.

This is a premeasurement amendment to the existing proposal, not a fabricated
validation failure or a replacement research task. Original decisions survive.
"""
from __future__ import annotations

import copy

from . import architecture_revisions as revisions, document_index
from .storage import now
from .architecture_validation import operation, once

VERSION = 1
ROLES = ('director', 'engineer', 'scout')
TRANSIENT = {'running', 'queued', 'starting', 'turn_start_dispatched',
             'thread_start_dispatched', 'thread_resume_dispatched'}


def prepare(engine, run_id, *, continuation=False):
    from .engine import Harness, ResearchBlocked, digest
    run, revision = engine.store.get('runs', run_id), revisions.state(engine, run_id)
    if not revision or revision['status'] != 'needs_consensus':
        raise ResearchBlocked('科学定义补充须来自已保存、明确要求重新共识的作者交付。')
    allowed = {'blocked', 'failed', 'paused'} | ({'running'} if continuation and run['stage'] == 'design' else set())
    if run['mode'] != 'live' or run['status'] not in allowed:
        raise ResearchBlocked('只能为已停止的真实任务准备科学定义重审。')
    engine._consensus_gate(run_id)
    revisions._available(engine, run_id)
    revisions._checked(engine, run_id, engine.store.get('steps', run_id + ':design')['output'])
    if engine.store.active_reasoning(run_id, TRANSIENT):
        raise ResearchBlocked('原任务仍有活动请求，不能准备科学定义重审。')
    pending, result = revision['pending'], revision['response']
    hid = Harness.decision_key(run_id, 'engineer', pending['purpose'], pending['instruction'],
                              pending['context'], pending['schema'])
    decision = engine.store.get('decisions', hid, {})
    if (hid != pending['handoff_id'] or decision.get('result') != result
            or decision.get('run_id') != run_id or decision.get('role') != 'engineer'
            or result.get('requires_new_consensus') is not True
            or result.get('proposal_hash') != revision['proposal_hash']
            or result.get('parent_design_hash') != pending['context']['parent_design_hash']):
        raise ResearchBlocked('科学重审缺少原版本绑定的实际作者交付。')
    key = f'{run_id}:architecture-definitions:{engine.cycle(run_id)}:{digest(result)}'
    existing = engine.store.get('architecture_definitions', key)
    if existing:
        return view(engine, run_id)
    from . import architecture_convergence as convergence
    if convergence.enabled(engine, run_id):
        convergence.check(engine, run_id, 'definitions')
    issues = copy.deepcopy(revision['unresolved_issues'])
    if not issues:
        raise ResearchBlocked('没有具体未闭合意见，不能重启泛化讨论。')
    payload = {field: copy.deepcopy(revision[field]) for field in
               ('pending', 'response', 'author_response_document', 'unresolved_issues')}
    payload['progress'] = copy.deepcopy(engine.store.get('handoff_progress', hid, {}))
    directories = copy.deepcopy(pending['context']['indexed_documents'])
    directories += [revision['author_response_document']]
    notes = payload['progress'].get('document_working_notes', {}).get('document')
    if notes:
        directories.append(notes)
    from .paper_document_navigation import for_papers
    core = engine.store.get('steps', run_id + ':related_work', {}).get('output', {}).get('reading_gate', {}).get('noted_core_ids', [])
    try:
        directories += for_papers(engine, run_id, core)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise ResearchBlocked('科学定义重审缺少可核验的既有论文原文索引：' + str(exc)) from exc
    directories = list({d['document_id']: d for d in directories}.values())
    for doc in directories:
        document_index.load(engine, run_id, doc['document_id'], doc['sha256'])
    record = {'id': key, 'run_id': run_id, 'version': VERSION, 'cycle': engine.cycle(run_id),
        'status': 'prepared', 'proposal_hash': revision['proposal_hash'],
        'parent_design_hash': pending['context']['parent_design_hash'],
        'source_author_handoff_id': hid, 'source_author_response_sha256': digest(result),
        'original_author_payload': payload, 'original_author_payload_sha256': digest(payload),
        'input_documents': directories, 'issues': issues, 'votes': [], 'review_protocol': 2,
        'unit_reviews': [], 'created_at': now(), 'design_review_policy_version': 1}
    if convergence.enabled(engine, run_id):
        record['architecture_progress_protocol'] = convergence.VERSION
        record['issue_ledger_protocol'] = 1
    updated = {**revision, 'definition_handoff_id': key,
               'definition_handoffs': [*revision.get('definition_handoffs', []), key]}
    engine.store.batch([('architecture_definitions', key, record),
                        ('architecture_revisions', revision['id'], updated)])
    engine.store.event(run_id, 'architecture.definitions_prepared',
        '已安排具体科学定义补充；候选 Markdown 须取得三方独立意见，尚未批准代码或实验。',
        {'id': key, 'issue_count': len(issues), 'scientific_approval': False})
    return view(engine, run_id)


@once
def _checked(engine, run_id, key):
    from .engine import ResearchBlocked, digest
    record = engine.store.get('architecture_definitions', key)
    if (not record or record.get('run_id') != run_id or record.get('version') != VERSION
            or record.get('review_protocol', 1) not in (1, 2, 3)):
        raise ResearchBlocked('科学定义补充记录缺失或归属错误。')
    payload = record['original_author_payload']
    hid = record['source_author_handoff_id']
    decision = engine.store.get('decisions', hid, {})
    resolved = set(payload['response']['resolved_issue_ids'])
    expected = payload['pending']['context']['review_fixes']
    if (digest(payload) != record['original_author_payload_sha256']
            or decision.get('result') != payload['response'] or decision.get('role') != 'engineer'
            or decision.get('run_id') != run_id
            or digest(payload['response']) != record['source_author_response_sha256']
            or payload['pending']['handoff_id'] != hid
            or payload['pending']['context']['parent_design_hash'] != record['parent_design_hash']
            or engine.store.get('handoff_progress', hid, {}) != payload['progress']
            or record['proposal_hash'] != digest(engine.store.get('consensus', run_id)['proposal'])
            or payload['response']['proposal_hash'] != record['proposal_hash']
            or payload['response']['parent_design_hash'] != record['parent_design_hash']
            or not resolved <= {i['id'] for i in expected}
            or record['issues'] != payload['unresolved_issues']
            or record['issues'] != [i for i in expected if i['id'] not in resolved]):
        raise ResearchBlocked('科学定义重审的原作者、原共识或不可变读取记录发生变化。')
    for doc in record['input_documents']:
        document_index.load(engine, run_id, doc['document_id'], doc['sha256'])
    if record.get('authoring_protocol') == 2:
        from .architecture_definition_completion import check_predecessor
        check_predecessor(engine, run_id, record)
    if record.get('work_plan'):
        from .architecture_workflow import check_plan
        check_plan(engine, run_id, record)
    return record


def _context(engine, run_id, record):
    context = copy.deepcopy(record['original_author_payload']['pending']['context'])
    for field in ('revision_number', 'review_fixes', 'previous_author_response_document',
                  'revision_agenda', 'previous_architecture_review', 'architecture_progress_protocol'):
        context.pop(field, None)
    context.update(indexed_document_context=True, document_index_protocol=1,
        document_input_protocol=2, document_read_window_protocol=1,
        document_working_notes_protocol=1, document_working_notes_required=True,
        indexed_documents=record['input_documents'], definition_issues=record['issues'],
        parent_design_hash=record['parent_design_hash'], proposal_hash=record['proposal_hash'],
        source_author_response_sha256=record['source_author_response_sha256'],
        definition_review_protocol=VERSION)
    if record.get('authoring_protocol') == 2:
        from .architecture_definition_completion import read_window_protocol
        context['document_read_window_protocol'] = read_window_protocol(record)
        context['definition_issue_documents'] = [{'issue_id': u['issue_id'], 'document': copy.deepcopy(u['document'])}
                                                  for u in record.get('issue_drafts', [])]
        context['indexed_documents'] = [*context['indexed_documents'], *[u['document'] for u in record.get('issue_drafts', [])]]
        if any(u.get('reused_from_definition_id') for u in record.get('issue_drafts', [])):
            from .architecture_definition_completion import _directories
            context['indexed_documents'] = _directories(engine, run_id, context['indexed_documents'])
    if record.get('review_protocol') == 2 and record.get('authoring_protocol') != 2:
        context['document_read_window_protocol'] = 2
    if record.get('architecture_workflow_protocol') == 1:
        context.update(architecture_workflow_protocol=1, document_read_window_protocol=2)
    return context


def _schema(vote=False):
    from .engine import STR, STRS, obj
    fields = {'proposal_hash': STR, 'parent_design_hash': STR, 'source_author_response_sha256': STR}
    if vote:
        fields.update(document_sha256=STR, approve={'type': 'boolean'},
            rationale={'type': 'string', 'maxLength': 5000}, required_changes=STRS,
            reviewed_issue_ids=STRS)
    else:
        fields.update(change_summary={'type': 'string', 'minLength': 1, 'maxLength': 2000},
            compatible_with_approved_scope={'type': 'boolean'}, scope_issues=STRS,
            definitions={'type': 'array', 'minItems': 1, 'maxItems': 20, 'items': obj({
                'issue_id': STR, 'status': {'enum': ['existing_contract', 'proposed_definition', 'unresolved']},
                'definition_md': {'type': 'string', 'minLength': 1, 'maxLength': 4000}})})
    return obj(fields)


def _binding(record, result):
    from .engine import ResearchBlocked
    if any(result[field] != record[field] for field in
           ('proposal_hash', 'parent_design_hash', 'source_author_response_sha256')):
        raise ResearchBlocked('科学定义交付或评议未绑定原方案、架构和作者版本。')


def _render(record, result):
    from .engine import digest
    lines = ['# 科学定义补充候选', '', '该候选不是已批准共识，不产生代码或实验授权。', '',
        '## 版本与范围', '', result['change_summary'], '',
        f'原方案：`{record["proposal_hash"]}`；原架构：`{record["parent_design_hash"]}`。',
        f'原作者交付：`{record["source_author_response_sha256"]}`；本候选完整交付：`{digest(result)}`。', '']
    if result['scope_issues']:
        lines += ['范围未决事项：', *['- ' + issue for issue in result['scope_issues']], '']
    for row in result['definitions']:
        lines += ['## 意见 ' + row['issue_id'], '', '定义状态：' + row['status'], '', row['definition_md'], '']
        if record.get('authoring_protocol') == 2:
            unit = next((u for u in record.get('issue_drafts', []) if u['issue_id'] == row['issue_id']), None)
            if unit:
                if unit.get('reused_from_definition_id'):
                    lines += [f'本项复用原候选 `{unit["reused_from_definition_id"]}` 的实际作者交付 '
                              f'`{unit["handoff_id"]}`；未产生新作者调用或原文阅读信用，仍须本版本独立评议。', '']
                lines += ['本项实际读取定位（由原交付确定性保留）：', *[
                    f'- `{a["document_id"]}` / `{a["expected_sha256"]}` / {a["section_id"]} / '
                    f'[{a["char_start"]}, {a["char_end"]})' for a in unit['result']['source_anchors']], '']
    return '\n'.join(lines)


def _check_vote(engine, run_id, record, vote):
    from .engine import ResearchBlocked
    from .architecture_documents import units
    from .implementation_documents import read_coverage
    doc = record['document']
    decision = engine.store.get('decisions', vote['handoff_id'], {})
    expected = {k: v for k, v in vote.items() if k not in {'role', 'handoff_id'}}
    _binding(record, expected)
    if record.get('review_protocol') == 3:
        from .architecture_component_review import check_final
        covered = check_final(engine, run_id, record, vote)
    elif record.get('review_protocol') == 2:
        from .definition_review_segments import check_vote_reads
        covered = check_vote_reads(engine, run_id, record, vote)
    else:
        covered = all(read_coverage(engine, run_id, doc, group, vote['handoff_id'])
                      for group in units(engine, run_id, doc))
    if (vote['role'] not in ROLES or vote['document_sha256'] != doc['sha256']
            or decision.get('role') != vote['role'] or decision.get('run_id') != run_id
            or decision.get('result') != expected
            or not covered):
        raise ResearchBlocked('科学定义评议缺少真实同版本交付或未实际读完全部意见。')
    from .architecture_final_delivery import check_scope
    check_scope(engine, run_id, record, vote)
    if record.get('issue_ledger_protocol') == 1:
        from . import architecture_issue_ledger as ledger
        frozen = engine.store.get('handoff_inputs', vote['handoff_id'], {})
        context = frozen.get('context', {})
        if context.get('issue_ledger_protocol') != 1:
            raise ResearchBlocked('科学定义终审缺少冻结的问题账本协议。')
        try:
            ledger.validate_vote(vote, context, doc)
        except ValueError as exc:
            raise ResearchBlocked(str(exc)) from exc
        hids = [vote['handoff_id'], *[u['handoff_id'] for u in record.get('unit_reviews', [])
                                     if u['role'] == vote['role']]]
        if not ledger.evidence_covered(engine, run_id, doc, vote, hids, context):
            raise ResearchBlocked('科学定义问题闭合或重开缺少本角色同版本真实读取。')


@once
def validate_adoption(engine, run_id, key):
    from .engine import ResearchBlocked
    record = _checked(engine, run_id, key)
    if record['status'] != 'approved' or len(record['votes']) != len(ROLES):
        raise ResearchBlocked('科学定义补充尚未取得三方同版本独立批准。')
    doc = record['document']
    _, text = document_index.load(engine, run_id, doc['document_id'], doc['sha256'])
    from .architecture_definition_completion import check_candidate
    check_candidate(engine, run_id, record)
    candidate = record['candidate']
    _binding(record, candidate)
    ids = [row['issue_id'] for row in candidate['definitions']]
    if (not candidate['compatible_with_approved_scope'] or candidate['scope_issues']
            or any(row['status'] == 'unresolved' for row in candidate['definitions'])
            or len(ids) != len(set(ids)) or set(ids) != {i['id'] for i in record['issues']}
            or text != _render(record, candidate)):
        raise ResearchBlocked('科学定义候选缺少实际作者交付。')
    for role in ROLES:
        matches = [v for v in record['votes'] if v['role'] == role]
        if len(matches) != 1:
            raise ResearchBlocked('科学定义独立角色票数无效。')
        vote = matches[0]
        _check_vote(engine, run_id, record, vote)
        if not vote['approve'] or vote['required_changes']:
            raise ResearchBlocked('科学定义批准缺少真实同版本交付、完整读取或仍有必改意见。')
    return record


async def run(engine, h, run_id, revision):
    from .engine import ResearchBlocked, digest
    from . import architecture_convergence as convergence
    key = revision.get('definition_handoff_id')
    if not key:
        return revision
    await engine._read_consensus_check(run_id, engine._consensus_gate, run_id)
    record = await engine._read_consensus_check(run_id, _checked, engine, run_id, key)
    if record['status'] in {'rejected', 'blocked'}:
        if record['status'] == 'rejected' and record.get('architecture_progress_protocol') == convergence.VERSION:
            from .architecture_definition_completion import check_candidate, prepare as prepare_next
            await engine._read_consensus_check(run_id, check_candidate, engine, run_id, record)
            if len(record['votes']) != len(ROLES) or {v['role'] for v in record['votes']} != set(ROLES):
                raise ResearchBlocked('定义评议未完成，不能自动交接下一版。')
            for vote in record['votes']:
                await engine._read_consensus_check(run_id, operation(_check_vote), engine, run_id, record, vote)
            convergence.record(engine, run_id, 'definitions', record, record['candidate'])
            convergence.check(engine, run_id, 'definitions')
            prepare_next(engine, run_id, continuation=True)
            return revisions.state(engine, run_id)
        raise ResearchBlocked(record['reason'])
    if revision.get('definition_adoption_id') == key:
        await engine._read_consensus_check(run_id, validate_adoption, engine, run_id, key)
        return revision
    if (record.get('status') == 'approved' and (record.get('architecture_workflow_protocol') == 1
            or record.get('architecture_progress_protocol') == convergence.VERSION)):
        # Approval may have committed just before the integration handoff was
        # interrupted. Do not change its completion timestamp or progress hash.
        await engine._read_consensus_check(run_id, validate_adoption, engine, run_id, key)
        from .architecture_integration import prepare_from_adoption, activate
        return await activate(engine, h, run_id, prepare_from_adoption(engine, run_id, revision, record))
    context = await engine._read_consensus_check(run_id, operation(_context), engine, run_id, record)
    if not record.get('document') and record.get('authoring_protocol') == 2:
        from .architecture_definition_completion import write
        try:
            record = await write(engine, h, run_id, record)
        except ResearchBlocked:
            record = await engine._read_consensus_check(run_id, _checked, engine, run_id, key)
            candidate = record.get('candidate') or {}
            if (record.get('architecture_progress_protocol') == convergence.VERSION
                    and record['status'] == 'blocked' and record.get('document')
                    and candidate.get('compatible_with_approved_scope') is True and not candidate.get('scope_issues')):
                convergence.record(engine, run_id, 'definitions', record, candidate)
                convergence.check(engine, run_id, 'definitions')
                from .architecture_definition_completion import prepare as prepare_next
                prepare_next(engine, run_id, continuation=True)
                return revisions.state(engine, run_id)
            raise
    if not record.get('document'):
        instruction = ('为已保存的具体未闭合意见提出完整、可复现的科学定义补充，控制器另存 Markdown 并收集三方独立意见。'
            '先按索引查批准方案、原架构、作者交付及其工作笔记；现有定义给出原版本章节和精确定位，不能把读取遗漏当成缺失。'
            'paper_rag_source 目录动态绑定本任务已有论文的统一物理索引；用 documents.search 定位、documents.read 分段核对原文公式和页码。'
            '原文定义须给出该目录版本哈希、页码及精确字符位置；检索片段或分析笔记不能代替实际读取，也不能认证发表版。'
            '确实未冻结的细节可提出具体待批准定义，并显式标为 proposed_definition；不得称为已批准事实或发表版原文。'
            '对每个 definition_issues.id 恰好返回一项，写出公式、张量轴、抽样分布、种子映射、归约和停止动作等实际所需定义。'
            '保留原科学问题、机制、强基线、所有比较变体、模型种子、试次数、阈值与数据隔离。'
            '不能满足时标 unresolved；改变上述批准范围时 compatible_with_approved_scope=false，说明 scope_issues。'
            '当前宿主明确授权优先于历史调用上限。没有实验结果，不写代码或运行实验，不宣称获得共识。'
            '不要重写完整提案或复述泛化讨论；只形成这些具体意见的候选补充，其他批准合同保持。')
        from .design_review_policy import AUTHOR
        if record.get('design_review_policy_version') == 1:
            instruction += AUTHOR
        purpose = 'architecture_definitions_author:' + digest(key)
        schema = _schema()
        hid = h._handoff(run_id, 'director', purpose, instruction, context, schema)['id']
        record.update(status='drafting', author_handoff_id=hid)
        engine.store.put('architecture_definitions', key, record)
        result = await h.ask(run_id, 'director', purpose, instruction, context, schema)
        ids = [row['issue_id'] for row in result['definitions']]
        try:
            _binding(record, result)
            if len(ids) != len(set(ids)) or set(ids) != {i['id'] for i in record['issues']}:
                raise ResearchBlocked('科学定义候选必须逐项绑定全部具体意见，不能遗漏、重复或添加未知意见。')
        except ResearchBlocked as exc:
            record.update(status='blocked', reason=str(exc), invalid_author_result=copy.deepcopy(result))
            engine.store.put('architecture_definitions', key, record)
            raise
        doc = revisions._save(engine, h, run_id,
            f'cycle-{record["cycle"]}/architecture/definitions/{digest(result)}.md',
            _render(record, result), 'architecture_scientific_definitions')
        record.update(candidate=result, document=doc, status='reviewing')
        if (not result['compatible_with_approved_scope'] or result['scope_issues']
                or any(row['status'] == 'unresolved' for row in result['definitions'])):
            record.update(status='blocked', reason='科学定义候选仍有未决项或改变批准范围；原目标和共识保留，不能推进实现。')
        engine.store.put('architecture_definitions', key, record)
        if record['status'] == 'blocked':
            raise ResearchBlocked(record['reason'])
    doc = record['document']
    from .architecture_definition_completion import check_candidate
    await engine._read_consensus_check(run_id, check_candidate, engine, run_id, record)
    for vote in record['votes']:
        await engine._read_consensus_check(run_id, operation(_check_vote), engine, run_id, record, vote)
    context = await engine._read_consensus_check(run_id, operation(_context), engine, run_id, record)
    vote_context = {**context, 'definition_document': doc,
                    'indexed_documents': [doc, *context['indexed_documents']]}
    if record.get('review_protocol') == 3:
        vote_context['indexed_documents'] = [doc, *[u['document'] for u in record['issue_drafts']],
                                              *context['indexed_documents']]
    if record.get('review_protocol') in (2, 3):
        vote_context['document_read_window_protocol'] = 2
    from . import design_review_policy as policy
    if record.get('design_review_policy_version') == policy.VERSION:
        vote_context.update(design_review_policy_version=policy.VERSION,
                            design_review_issue_field='required_changes')
    from . import architecture_convergence as convergence
    if record.get('review_protocol') == 3:
        from .architecture_component_review import advance
        if not await advance(engine, h, run_id, record, vote_context):
            return revisions.state(engine, run_id)
    for role in ROLES:
        if any(v['role'] == role for v in record['votes']):
            continue
        role_context = vote_context
        instruction = ('独立审查同版本科学定义补充候选，先按索引读完该候选所有章节，并回查相关原合同。'
            '当前输入不含其他角色投票；分别判断既有定义恢复和新提出的具体定义是否合理、可复现并符合批准机制。'
            '逐项核对全部 definition_issues；缺失公式、种子、抽样、配置优先级、归约或停止条件时给出具体必改意见。'
            '保持原强基线、比较变体、种子、阈值、试次和隔离；不得把未测量结果当作批准前置条件。'
            '禁止以新定义为由缩小原科学问题或放宽标准。现有索引和工作笔记不能认证论文原文或科学成功。'
            '返回原方案、原架构、原作者哈希与 definition_document.sha256；reviewed_issue_ids覆盖每项意见。'
            '批准仅表示同意这些具体定义进入架构修订，仍需新架构、实现文档、代码及实际实验门禁。')
        if record.get('review_protocol') == 3:
            from .architecture_component_review import checked_role
            checked_units = await engine._read_consensus_check(run_id, operation(checked_role), engine, run_id, record, role)
            role_context = {**vote_context, 'definition_unit_reviews': checked_units,
                            'definition_final_scope_protocol': 1}
            instruction = ('根据你本人已保存且绑定原文范围的同版本分组意见，核对全部原意见'
                '与组间一致性，必要时定向回查决定性条款；不再从头读整包。'
                '不得因正文在窗口中归档而重提已经完成的本角色具体裁决。'
                '真实未闭合必改项仍不得批准；返回原绑定哈希、当前 document_sha256 和全部 reviewed_issue_ids。'
                '批准仅允许进入统一架构修订，不授权代码或实验。')
        elif record.get('review_protocol') == 2:
            from .definition_review_segments import review_context
            role_context = await review_context(engine, h, run_id, record, role, vote_context)
            instruction = ('对同版本候选形成最终独立意见。definition_unit_reviews 是你自己已保存、'
                '绑定精确读取范围的分段评议，不能当作论文原文或未读材料的证明。'
                '核对全部 definition_issues 与跨段一致性，必要时按文档索引回读决定性条款；'
                '本次不用再从头读取全文，不注入其他角色意见或整份正文。'
                '任何分段必改问题未解决时不得批准；保持全部原比较、种子、阈值、试次和资源隔离。'
                '返回原方案、原架构、原作者哈希与 definition_document.sha256，reviewed_issue_ids 覆盖全部意见。'
                '批准只允许具体定义进入架构修订，仍需新架构、实现文档、代码与实际实验门禁。')
        purpose, schema = 'architecture_definitions_review:' + doc['sha256'], _schema(vote=True)
        if record.get('review_protocol') == 3:
            from .engine import STRS
            schema['properties']['affected_issue_ids'] = copy.deepcopy(STRS)
            schema['required'].append('affected_issue_ids')
            instruction += ('\n否决时 affected_issue_ids 覆盖本角色全部分组必改涉及的原 ID，'
                '并加入你新发现的跨组冲突影响项；批准时为空。不得笼统要求全部作者重写。'
                '旧协议批准意见可能用该字段标注已审范围；approve=true 且必改项为空的'
                '此类标注不属于未决项，不能转为作者修订请求。')
            schema.setdefault('allOf', []).append({
                'if': {'properties': {'approve': {'const': True}}},
                'then': {'properties': {'affected_issue_ids': {'maxItems': 0}}},
                'else': {'properties': {'affected_issue_ids': {'minItems': 1}}}})
        if role_context.get('design_review_policy_version') == policy.VERSION:
            instruction += policy.REVIEW + '\n终审合并本角色全部分段阻塞，逐项给出完整修正，不得遗漏。'
            schema = policy.schema(schema, 'required_changes')
        if record.get('architecture_progress_protocol') == convergence.VERSION:
            structured = record.get('issue_ledger_protocol') == 1
            role_context = {**role_context, **convergence.context(engine, run_id, 'definitions', record['candidate'],
                                                                 role=role, structured=structured)}
            instruction += convergence.STRUCTURED_REVIEW if structured else convergence.REVIEW
            schema = convergence.schema(schema, structured=structured)
        if record.get('review_protocol') == 3:
            from .architecture_final_delivery import constrain
            constrain(schema, role_context)
            instruction += ('\nID 范围：reviewed_issue_ids 只列出 definition_issues.id，每项恰好一次；'
                'previous_architecture_review.objections.id 是历史异议，闭合时只放入 '
                'progress.resolved_objection_ids，不能混入本版已审 ID。')
        hid = h._handoff(run_id, role, purpose, instruction, role_context, schema)['id']
        record['active_review_handoff'] = {'role': role, 'handoff_id': hid}
        engine.store.put('architecture_definitions', key, record)
        result = await h.ask(run_id, role, purpose, instruction, role_context, schema)
        entry = {'role': role, 'handoff_id': hid, **result}
        try:
            await engine._read_consensus_check(run_id, operation(_check_vote), engine, run_id, record, entry)
        except ResearchBlocked as exc:
            record.update(status='blocked', reason=str(exc), invalid_review=entry)
            engine.store.put('architecture_definitions', key, record)
            raise
        record['votes'].append(entry)
        engine.store.put('architecture_definitions', key, record)
        engine.store.event(run_id, 'architecture.definition_vote', '已收到具体科学定义的独立意见。',
            {'role': role, 'received_votes': len(record['votes']), 'expected_votes': 3,
             'approve': result['approve'] and not result['required_changes'], 'document_sha256': doc['sha256']})
        if record.get('review_protocol') == 3 and len(record['votes']) < len(ROLES):
            return revisions.state(engine, run_id)
    approved = all(v['approve'] and not v['required_changes'] for v in record['votes'])
    record.update(status='approved' if approved else 'rejected', completed_at=now(),
        reason='科学定义补充取得三方同版本批准，继续编写新架构。' if approved
               else '科学定义补充未取得三方独立一致意见；具体修改已保存，原方案与否决保留。')
    engine.store.put('architecture_definitions', key, record)
    if record.get('architecture_progress_protocol') == convergence.VERSION:
        convergence.record(engine, run_id, 'definitions', record, record['candidate'])
    if not approved:
        if record.get('architecture_progress_protocol') == convergence.VERSION:
            convergence.check(engine, run_id, 'definitions')
            from .architecture_definition_completion import prepare as prepare_next
            prepare_next(engine, run_id, continuation=True)
            return revisions.state(engine, run_id)
        raise ResearchBlocked(record['reason'])
    await engine._read_consensus_check(run_id, validate_adoption, engine, run_id, key)
    if (record.get('architecture_workflow_protocol') == 1
            or record.get('architecture_progress_protocol') == convergence.VERSION):
        from .architecture_integration import prepare_from_adoption, activate
        revision = prepare_from_adoption(engine, run_id, revision, record)
        return await activate(engine, h, run_id, revision)
    prior = record['original_author_payload']['pending']
    next_context = {**copy.deepcopy(prior['context']), 'approved_definition_document': doc,
        'definition_adoption_id': key, 'indexed_documents': [doc, *record['input_documents']],
        'document_working_notes_protocol': 1, 'document_working_notes_required': True}
    # Progress is a property of the new handoff, not inherited from an old paid
    # assignment which may predate the protocol.
    if record.get('architecture_progress_protocol') == convergence.VERSION:
        original = engine.store.get('steps', run_id + ':design')['output']
        design = await engine._read_consensus_check(run_id, revisions.effective, engine, run_id, original)
        next_context.update(convergence.context(engine, run_id, 'architecture', design))
    if record.get('architecture_workflow_protocol') == 1:
        next_context.update(architecture_workflow_protocol=1, document_read_window_protocol=2,
                            architecture_required_documents=[doc['document_id']])
    instruction = prior['instruction'] + ('\n科学定义补充现已取得三方对同一版本的独立批准，按 approved_definition_document '
        '索引读取并纳入新架构，保留原批准问题、比较、种子及门槛。此补充批准不能替代架构、代码或实验门禁。'
        '不要再次索取这同一补充的共识；若还需要补充之外的科学改动，必须明确提出并继续停止。')
    from .design_review_policy import AUTHOR
    instruction += AUTHOR
    if next_context.get('architecture_progress_protocol') == convergence.VERSION:
        instruction += convergence.AUTHOR
    purpose = prior['purpose'] + ':definitions-v1:' + doc['sha256']
    hid = h._handoff(run_id, 'engineer', purpose, instruction, next_context, prior['schema'])['id']
    revision = {**revision, 'status': 'drafting', 'definition_adoption_id': key,
        'pending': {'purpose': purpose, 'instruction': instruction, 'context': next_context,
                    'schema': copy.deepcopy(prior['schema']), 'handoff_id': hid},
        'reason': record['reason']}
    engine.store.put('architecture_revisions', revision['id'], revision)
    return revision


def _activity(engine, run_id, record):
    if record['status'] == 'drafting':
        role, hid = 'director', ((record.get('active_issue_handoff') or {}).get('handoff_id')
                                if record.get('authoring_protocol') == 2 else record.get('author_handoff_id'))
    elif record['status'] == 'reviewing':
        active = record.get('active_review_handoff') or {}
        role, hid = active.get('role'), active.get('handoff_id')
    else:
        return None
    if not hid:
        return None
    handoff = engine.store.get('handoffs', hid, {})
    if handoff.get('run_id') != run_id or handoff.get('to_role') != role:
        return None
    progress = engine.store.get('handoff_progress', hid, {})
    pages = [row['result'] for row in progress.get('tool_results', [])
        if row.get('call', {}).get('name') == 'documents.read'
        and isinstance(row.get('result', {}).get('text'), str)]
    notes = progress.get('document_working_notes') or {}
    return {'role': role, 'handoff_status': handoff.get('status'),
        'continuation_turn': progress.get('turn', 0), 'finalizing': progress.get('mode') == 'final_only',
        'read_pages': len(pages), 'read_documents': len({page['document_id'] for page in pages}),
        'rag_source_read_pages': sum(page['document_id'].startswith('paper-rag-source:') for page in pages),
        'searches': sum(row.get('call', {}).get('name') == 'documents.search' for row in progress.get('tool_results', [])),
        'working_notes_count': len(notes.get('payload', {}).get('entries', [])),
        'working_notes_document': copy.deepcopy(notes.get('document')), 'scientific_approval': False}


def view(engine, run_id):
    revision = revisions.state(engine, run_id) or {}
    record = engine.store.get('architecture_definitions', revision.get('definition_handoff_id', ''))
    if not record:
        return None
    candidate = record.get('candidate')
    definition_items = candidate['definitions'] if candidate is not None else [
        item for unit in record.get('issue_drafts', []) for item in unit['result']['definitions']]
    scope_issues = candidate['scope_issues'] if candidate is not None else list(dict.fromkeys(
        issue for unit in record.get('issue_drafts', []) for issue in unit['result']['scope_issues']))
    from .architecture_definition_completion import completion_status
    return {field: copy.deepcopy(record.get(field)) for field in ('id', 'status', 'reason', 'document')} | {
        'completion': completion_status(engine, run_id, record, revision),
        'issue_count': len(record['issues']), 'received_votes': len(record['votes']), 'expected_votes': len(ROLES),
        'activity': _activity(engine, run_id, record),
        'authoring_protocol': record.get('authoring_protocol', 1),
        'review_protocol': record.get('review_protocol', 1),
        'review_unit_count': record.get('review_unit_count', 0),
        'saved_review_units': len(record.get('unit_reviews', [])),
        'expected_review_units': record.get('review_unit_count', 0) * len(ROLES),
        'active_review_unit_number': (record.get('active_review_handoff') or {}).get('unit_number'),
        'saved_issue_count': len(record.get('issue_drafts', [])),
        'reused_issue_count': sum(bool(u.get('reused_from_definition_id')) for u in record.get('issue_drafts', [])),
        'active_issue_number': (record.get('active_issue_handoff') or {}).get('number'),
        'issue_documents': [{'issue_id': u['issue_id'], 'status': u['result']['definitions'][0]['status'],
                             'delivery_provenance': 'reused_actual_delivery' if u.get('reused_from_definition_id') else 'new_actual_delivery',
                             'reused_from_definition_id': u.get('reused_from_definition_id'),
                             'document': copy.deepcopy(u['document'])} for u in record.get('issue_drafts', [])],
        'unresolved_issue_count': sum(d['status'] == 'unresolved' for d in definition_items),
        'scope_issues': copy.deepcopy(scope_issues),
        'votes': [{k: copy.deepcopy(v[k]) for k in ('role', 'approve', 'rationale', 'required_changes', 'fixes', 'suggestions') if k in v}
                  for v in record['votes']], 'scientific_approval': False,
        'scope': 'Definition adoption only; architecture, implementation and experiment gates remain.'}
