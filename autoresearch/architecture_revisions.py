"""Progress-driven immutable architecture revisions through graph handoffs."""
from __future__ import annotations

import copy
import math

from .storage import now
from .architecture_validation import operation

VERSION = 1


def enabled(engine, run_id):
    return engine.store.get('runs', run_id)['config'].get('architecture_revision_protocol') == VERSION


def key(engine, run_id):
    from .engine import digest
    proposal = engine.store.get('consensus', run_id, {}).get('proposal')
    return f'{run_id}:architecture-revisions:{engine.cycle(run_id)}:{digest(proposal)}'


def state(engine, run_id):
    return engine.store.get('architecture_revisions', key(engine, run_id)) if enabled(engine, run_id) else None


def _available(engine, run_id):
    from .engine import ResearchBlocked
    cycle = engine.cycle(run_id)
    with engine.store._read_db() as db:
        existing = db.execute("SELECT 1 FROM records WHERE json_extract(data,'$.run_id')=? AND ("
            "(collection='experiments' AND coalesce(json_extract(data,'$.research_cycle'),1)=?) OR "
            "(collection='codex_workflows' AND coalesce(json_extract(data,'$.cycle'),1)=?)) LIMIT 1",
            (run_id, cycle, cycle)).fetchone()
    if (engine.committed_outputs(run_id).get('implementation') or existing
            or engine.store.get('programs', run_id)):
        raise ResearchBlocked('已有实际代码交付或实验，不能通过测量前架构修订改变冻结方法；须沿用原流程。')


def prepare(engine, run_id, *, operator_authorization=None):
    """Explicitly prepare the existing stopped run; no model calls or resumption."""
    from .engine import ResearchBlocked, digest
    run = engine.store.get('runs', run_id)
    existing = engine.store.get('architecture_revisions', key(engine, run_id))
    if existing:
        return existing
    if run['status'] == 'running' or run['mode'] != 'live':
        raise ResearchBlocked('只可为已停止且尚无实际实现或实验的真实任务准备架构修订。')
    if engine.store.active_reasoning(run_id, {
            'running', 'queued', 'starting', 'turn_start_dispatched', 'thread_start_dispatched', 'thread_resume_dispatched'}):
        raise ResearchBlocked('原任务仍有活动请求，不能准备后续架构修订。')
    _available(engine, run_id)
    original = engine.store.get('steps', run_id + ':design')
    if not original or original['status'] != 'completed':
        raise ResearchBlocked('尚无已保存架构设计，不需要旧版本修订交接。')
    record = {'id': key(engine, run_id), 'run_id': run_id, 'cycle': engine.cycle(run_id),
        'version': VERSION, 'status': 'prepared', 'original_design_hash': digest(original['output']),
        'original_step_sha256': digest(original), 'revisions': [], 'created_at': now(),
        'document_read_window_protocol': 1}
    if operator_authorization:
        # Only the human-facing preparation caller supplies this, never a model.
        if operator_authorization.get('max_calls') != run['config'].get('max_calls'):
            raise ResearchBlocked('操作授权与当前调用次数配置不一致。')
        record['operator_authorization'] = copy.deepcopy(operator_authorization)
    engine.store.put('architecture_revisions', record['id'], record)
    engine.update_run(run_id, config={**run['config'], 'architecture_revision_protocol': VERSION})
    engine.store.event(run_id, 'architecture.revision_prepared',
        '已安排根据独立架构意见生成修订版本；旧文档、评议、计划和付费请求保留。', {'id': record['id']})
    return record


@operation
def _checked(engine, run_id, original):
    from .engine import ResearchBlocked, digest
    saved = state(engine, run_id)
    if not saved:
        return None
    if saved.get('original_design_hash') != digest(original):
        raise ResearchBlocked('原架构设计已变化，不能复用旧修订链。')
    original_step = engine.store.get('steps', run_id + ':design')
    if saved.get('original_step_sha256') is not None and digest(original_step) != saved['original_step_sha256']:
        raise ResearchBlocked('原架构检查点发生变化，不能复用旧修订链。')
    adopted = None
    if saved.get('definition_adoption_id'):
        from .architecture_definitions import validate_adoption
        adopted = validate_adoption(engine, run_id, saved['definition_adoption_id'])
    if saved.get('integration_handoff'):
        from .architecture_integration import check
        check(engine, run_id, saved['integration_handoff']['audit_id'], adoption=adopted)
    for recovery in saved.get('reading_recoveries', []):
        audit = engine.store.get('stage_contexts', recovery['audit_id'], {})
        prior = engine.store.get('decisions', audit.get('previous_handoff_id', ''), {})
        if (audit.get('run_id') != run_id or audit.get('sha256') != digest(audit.get('payload'))
                or digest(audit.get('payload', {}).get('pending')) != recovery['pending_sha256']
                or prior.get('result') != audit.get('payload', {}).get('previous_result')
                or engine.store.get('handoff_progress', audit.get('previous_handoff_id', '')) != audit.get('payload', {}).get('original_progress')
                or prior.get('run_id') != run_id or prior.get('role') != 'engineer'):
            raise ResearchBlocked('文档阅读窗口恢复缺少原交付和不可变审计。')
    parent = digest(original)
    design = original
    for row in saved['revisions']:
        decision = engine.store.get('decisions', row['handoff_id'], {})
        if row.get('integration_audit_id'):
            from .architecture_integration import materialize
            frozen = engine.store.get('handoff_inputs', row['handoff_id'], {})
            if frozen.get('context', {}).get('integration_audit_id') != row['integration_audit_id']:
                raise ResearchBlocked('架构整合候选缺少原作者参数基点绑定。')
            expected_design = materialize(engine, run_id, frozen, row['response'], adoption=adopted)
        else:
            expected_design = {**design, **row['response']['design']}
        if row.get('local_revalidation_audit_id'):
            from .architecture_integration_recovery import checked_local_revalidation
            from .experiment_plan import build_for_engine
            audit, replayed = checked_local_revalidation(engine, run_id, row['local_revalidation_audit_id'])
            payload = audit['payload']
            if (replayed != row['response'] or payload['candidate_hash'] != digest(expected_design)
                    or payload['source_audit_id'] != row.get('delivery_validation_audit_id')
                    or payload['plan_hash'] != digest(build_for_engine(engine, run_id, expected_design, payload['trial_budget']))):
                raise ResearchBlocked('本地复验候选或冻结实验矩阵与原审计不一致。')
        if (row['parent_design_hash'] != parent or row['design_hash'] != digest(row['design'])
                or decision.get('run_id') != run_id or decision.get('role') != 'engineer'
                or decision.get('result') != row['response']
                or row['response']['proposal_hash'] != saved['proposal_hash']
                or row['response']['parent_design_hash'] != parent
                or row['design'] != expected_design):
            raise ResearchBlocked('架构修订缺少同版本真实作者交付，保留原记录并停止。')
        parent = row['design_hash']
        design = row['design']
    return saved


def effective(engine, run_id, original):
    saved = _checked(engine, run_id, original) if original is not None else None
    return saved['revisions'][-1]['design'] if saved and saved['revisions'] else original


def revised(engine, run_id, design):
    from .engine import digest
    saved = state(engine, run_id)
    return bool(saved and saved['revisions'] and saved['revisions'][-1]['design_hash'] == digest(design))


def plan_key(engine, run_id):
    from .engine import digest
    original = engine.store.get('steps', run_id + ':design', {}).get('output')
    design = effective(engine, run_id, original)
    return f'{run_id}:design:{digest(design)}' if revised(engine, run_id, design) else run_id


def balanced_cap(document):
    """Reserve room for cross-section reads and avoid a paid whitespace tail."""
    return max(1, math.ceil(document['total_chars'] / max(2, math.ceil(document['total_chars'] / 4000))))


def _save(engine, h, run_id, name, text, kind):
    from .implementation_documents import _save as save
    return save(engine, h, run_id, name, text, kind)


def _check_author_delivery(engine, h, run_id, record, design, result, context):
    """A bound author rejection is an outcome, not a broken version binding."""
    from .engine import ResearchBlocked, digest
    expected = {row['id'] for row in context['review_fixes']}
    resolved = set(result['resolved_issue_ids'])
    if (result['proposal_hash'] != record['proposal_hash'] or result['parent_design_hash'] != digest(design)
            or not resolved <= expected):
        raise ResearchBlocked('新架构交付未绑定原版本或引用了未知评议意见；原请求保留。')
    unresolved = [copy.deepcopy(row) for row in context['review_fixes'] if row['id'] not in resolved]
    if not result['requires_new_consensus'] and not unresolved:
        return
    if not result['requires_new_consensus'] and context.get('architecture_progress_protocol') == 1:
        # A partial author delivery is still a candidate for independent review,
        # never an approval or a claim that its remaining issues are closed.
        record['last_author_open_issues'] = unresolved
        return
    status = 'needs_consensus' if result['requires_new_consensus'] else 'blocked'
    title = '架构作者要求科学方案重审' if result['requires_new_consensus'] else '架构作者交付仍有未闭合意见'
    reason = f'{title}，作者申报已处理 {len(resolved)}/{len(expected)} 条意见，仍有 {len(unresolved)} 条必改意见未闭合。'
    reason += ('须补齐具体定义并重新取得独立共识；原交付和评议保留。'
               if result['requires_new_consensus'] else '须闭合具体意见并完成独立评议；原交付保留。')
    lines = ['# ' + title, '', '该交付尚未取得实施或实验批准。', '',
        '## 交付说明', '', result['change_summary'], '', '## 未闭合评议意见', '']
    for issue in unresolved:
        lines += [f'- {issue["role"]} · `{issue["id"]}`：{issue["issue"]}']
    lines += ['', '## 原作者交付', '', '```json', architecture_json(result), '```', '']
    doc = _save(engine, h, run_id,
        f'cycle-{engine.cycle(run_id)}/architecture/author-response/{digest(result)}.md',
        '\n'.join(lines), 'architecture_author_response')
    record.update(status=status, reason=reason, response=copy.deepcopy(result),
        author_response_document=doc, unresolved_issues=unresolved,
        resolved_issue_count=len(resolved), review_issue_count=len(expected))
    engine.store.put('architecture_revisions', record['id'], record)
    engine.store.event(run_id, 'architecture.author_review_needed', reason,
        {'handoff_id': record['pending']['handoff_id'], 'status': status,
         'resolved_issue_count': len(resolved), 'review_issue_count': len(expected),
         'document_id': doc['document_id'], 'scientific_approval': False})
    raise ResearchBlocked(reason)


def _benchmark_sources(engine, run_id, notes, proposal, original, existing):
    """Bounded navigation to selected notes' exact acquired source versions.

    This neither imports new papers nor upgrades historical notes to current
    reading. Only the first construction of the architecture inputs calls it.
    """
    from . import paper_bindings, paper_document_navigation as navigation
    from .architecture_documents import _json
    scope = _json({'proposal': proposal, 'design': original}).casefold()

    def rank(item):
        paper_id, note = item
        named = paper_id.casefold() in scope
        data_match = any(str(dataset).strip().casefold() in scope
                         for dataset in note.get('datasets', []) if str(dataset).strip())
        return (not named, not data_match, paper_id)

    known = {doc['document_id']: doc['sha256'] for doc in existing}
    additions = []
    for paper_id, note in sorted(notes.items(), key=rank):
        if len(additions) >= 8:
            break
        source_id = note['source_id']
        if source_id.startswith('rag-source:'):
            source_id = paper_bindings.shared_alias(engine.store, run_id, source_id)
        bound = paper_bindings.valid_binding(engine.store, run_id, source_id) if source_id else None
        # Legacy notes may have no canonical acquisition binding. Leave that
        # gap visible in their original note, never synthesize an original.
        if not bound or bound['text_sha256'] != note['text_sha256']:
            continue
        source_key = paper_bindings.canonical_source(engine.store, bound['rag_source_id'])
        for doc in navigation.for_papers(engine, run_id, [paper_id]):
            if (doc['canonical_source_id'] != 'rag-source:' + source_key
                    or doc['sha256'] != note['text_sha256']):
                continue
            prior = known.get(doc['document_id'])
            if prior is not None:
                if prior != doc['sha256']:
                    raise ValueError('Benchmark source directory version conflict')
                continue
            known[doc['document_id']] = doc['sha256']
            additions.append(doc)
    return additions


async def inputs(engine, h, run_id, record, original):
    """Freeze directories, current host receipts and already validated RAG notes."""
    from .engine import digest
    from . import architecture_documents as architecture
    from .execution_contract import constraints, host_budget_snapshot
    from .lab import assess_resources
    from .literature_notes import notes_view
    if record.get('inputs'):
        return record['inputs']
    saved = architecture.save(engine, h, run_id, original)
    run = engine.store.get('runs', run_id)
    proposal = engine.store.get('consensus', run_id)['proposal']
    hardware = await engine.execution_hardware(run_id)
    authority = {'version': 1, 'checked_at': now(), 'run_id': run_id,
        'proposal_hash': digest(proposal), 'execution_constraints': constraints(run['config']),
        'current_call_budget': host_budget_snapshot(engine.store, run_id, proposal, digest(proposal), engine.cycle(run_id)),
        'operator_authorization': record.get('operator_authorization'),
        'resource_admission': {'source': 'host current hardware/preferences and original resource_plan',
            'resource_plan': original['resource_plan'], 'assessment': assess_resources(original['resource_plan'], hardware)},
        'current_hardware': hardware,
        'scope': 'Current configured controls supersede historical narrative values for new assignments. '
            'An explicit supplied human limit change supersedes the old objective. No scientific measurement, '
            'extra trial, implemented program or experimental authorization is created by this receipt.'}
    text = '# 当前操作约束与宿主准入\n\n'
    for field, value in authority.items():
        text += '## ' + field + '\n\n```json\n' + architecture._json(value) + '\n```\n\n'
    authority_doc = _save(engine, h, run_id,
        f'cycle-{engine.cycle(run_id)}/architecture/authority/{digest(authority)}.md', text, 'architecture_host_receipt')
    docs = [saved['document'], saved['approved_proposal'], authority_doc]
    old_plan = engine.store.get('experiment_plans', run_id)
    if old_plan:
        text = '# 原冻结计划\n\n保留原版本；修订架构通过后须另建并评议新计划。\n\n'
        for field, value in old_plan.items():
            text += '## ' + field + '\n\n```json\n' + architecture._json(value) + '\n```\n\n'
        docs.append(_save(engine, h, run_id, f'cycle-{engine.cycle(run_id)}/architecture/plans/{digest(old_plan)}.md',
                          text, 'original_experiment_plan'))
    core = set(engine.store.get('steps', run_id + ':related_work', {}).get('output', {}).get('reading_gate', {}).get('noted_core_ids', []))
    latest = {}
    for note in sorted(notes_view(engine.store, run_id, document_loader=engine.sources.document), key=lambda row: row.get('created_at', '')):
        if note['paper_id'] in core:
            latest[note['paper_id']] = note
    for paper_id, note in sorted(latest.items()):
        text = '# 已校验历史论文选读笔记\n\n'
        text += '来源为此前实际读取并保存的选段；文档索引不新增本次论文阅读信用、全文资格或科学认证。\n\n'
        for field, value in note.items():
            text += '## ' + field + '\n\n```json\n' + architecture._json(value) + '\n```\n\n'
        docs.append(_save(engine, h, run_id, f'cycle-{engine.cycle(run_id)}/architecture/evidence/{digest(note)}.md',
                          text, 'validated_literature_note'))
    docs.extend(_benchmark_sources(engine, run_id, latest, proposal, original, docs))
    record.update(inputs=docs, authority_document=authority_doc,
                  proposal_hash=saved['proposal_hash'], execution_constraints=constraints(run['config']))
    engine.store.put('architecture_revisions', record['id'], record)
    return docs


def indexed_inputs(engine, run_id):
    """Derive new handoff directories without rewriting frozen author inputs."""
    from .engine import ResearchBlocked
    saved = state(engine, run_id) or {}
    documents = copy.deepcopy(saved.get('inputs', []))
    if saved.get('integration_authority_document'):
        documents.append(copy.deepcopy(saved['integration_authority_document']))
    context = {'indexed_documents': documents}
    adoption_id = saved.get('definition_adoption_id')
    if not adoption_id:
        return context
    from .architecture_definitions import validate_adoption
    adopted = validate_adoption(engine, run_id, adoption_id)
    # Keep the old response and its working notes in their original handoff.
    # Later stages need the approved supplement and the same physical sources.
    additions = [adopted['document'], *[
        doc for doc in adopted['input_documents'] if doc.get('kind') == 'paper_rag_source']]
    by_id = {doc['document_id']: doc for doc in documents}
    for doc in additions:
        prior = by_id.get(doc['document_id'])
        if prior is not None:
            if prior['sha256'] != doc['sha256']:
                raise ResearchBlocked('科学定义交接中的同一文档索引存在版本冲突。')
            continue
        detached = copy.deepcopy(doc)
        documents.append(detached)
        by_id[doc['document_id']] = detached
    context.update(definition_adoption_id=adoption_id,
                   approved_definition_document=copy.deepcopy(adopted['document']))
    from .architecture_dependency_catalog import inherited_documents
    documents.extend(inherited_documents(engine, run_id, saved, documents, adopted=adopted))
    return context


def review_context(engine, run_id, design, *, read_window_protocol=None):
    saved = state(engine, run_id)
    context = {'indexed_document_context': True, 'document_index_protocol': 1, 'document_input_protocol': 2,
        **indexed_inputs(engine, run_id), 'operator_authorization': saved.get('operator_authorization'),
        'execution_constraints': saved['execution_constraints'],
        'authority_document': saved.get('integration_authority_document') or saved['authority_document']}
    window = saved.get('document_read_window_protocol', 0) if read_window_protocol is None else read_window_protocol
    if window == 1:
        context['document_read_window_protocol'] = 1
    from .dataset_transition import review_scope
    context.update(review_scope(engine.store, run_id))
    return context


def _save_protocol_context(engine, h, run_id, design, plan, acceptance, policy,
                           proposal_hash, revision_hash, checked_context):
    """Save on the caller after its definition ancestry has been checked."""
    from .engine import digest
    from . import architecture_documents as architecture
    payload = {'experiment_plan': plan, 'acceptance': acceptance, 'iteration_policy': policy,
               'task_primary_threshold': engine.store.get('runs', run_id)['config'].get('min_relative_improvement', .01)}
    text = '# 新架构的预定执行计划\n\n本版本无测量结果；不得改写原冻结计划。\n\n'
    for field, value in payload.items():
        text += '## ' + field + '\n\n```json\n' + architecture._json(value) + '\n```\n\n'
    doc = _save(engine, h, run_id,
        f'cycle-{engine.cycle(run_id)}/architecture/{digest(design)}/protocol/{revision_hash}.md', text, 'revised_experiment_protocol')
    saved = architecture.save(engine, h, run_id, design)
    ctx = copy.deepcopy(checked_context)
    ctx.update(proposal_hash=proposal_hash, revision_hash=revision_hash, design_hash=digest(design),
               protocol_document=doc, task_primary_threshold=payload['task_primary_threshold'],
               indexed_documents=[doc, saved['document'], *[
                   d for d in ctx['indexed_documents'] if d['document_id'] != saved['document']['document_id']]])
    return ctx


def protocol_context(engine, h, run_id, design, plan, acceptance, policy, proposal_hash, revision_hash):
    """A new plan is reviewed through its index, leaving the old plan immutable."""
    context = operation(review_context)(engine, run_id, design)
    return _save_protocol_context(engine, h, run_id, design, plan, acceptance, policy,
                                  proposal_hash, revision_hash, context)


async def protocol_context_async(engine, h, run_id, design, plan, acceptance, policy,
                                 proposal_hash, revision_hash):
    context = await engine._read_consensus_check(
        run_id, operation(review_context), engine, run_id, design)
    engine.check_active(run_id)
    return _save_protocol_context(engine, h, run_id, design, plan, acceptance, policy,
                                  proposal_hash, revision_hash, context)


def validate_plan(engine, run_id, record, design):
    """A revised plan cannot drift from its real, same-version approvals."""
    from .engine import digest, ResearchBlocked
    from .experiment_plan import build_plan
    run = engine.store.get('runs', run_id)
    from .experiment_plan import build_for_engine
    expected = build_for_engine(engine, run_id, design, run['config']['max_trials'])
    acceptance, policy = design['acceptance'], design['iteration_policy']
    from .performance_policy import MODE, classify
    if record.get('acceptance', {}).get('policy') == MODE:
        acceptance = classify(acceptance, design['primary_metric'])
    ph = digest(engine.store.get('consensus', run_id)['proposal'])
    rh = digest({'plan': expected, 'acceptance': acceptance, 'iteration_policy': policy,
                 'task_primary_threshold': run['config'].get('min_relative_improvement', .01)})
    if (record['id'] != plan_key(engine, run_id) or record.get('acceptance') != acceptance
            or record.get('iteration_policy') != policy or record.get('protocol_revision_hash') != rh
            or any(record.get(k) != v for k, v in expected.items())):
        raise ResearchBlocked('修订架构冻结计划与具体设计或已评议版本不一致。')
    votes = record.get('protocol_votes', [])
    if len(votes) != 3 or {v.get('role') for v in votes} != {'director', 'engineer', 'scout'}:
        raise ResearchBlocked('修订架构计划缺少完整三方评议。')
    for vote in votes:
        decision = engine.store.get('decisions', record.get('protocol_handoffs', {}).get(vote['role'], ''), {})
        if (vote.get('approve') is not True or vote.get('required_changes')
                or vote.get('proposal_hash') != ph or vote.get('revision_hash') != rh
                or decision.get('role') != vote['role'] or decision.get('run_id') != run_id
                or decision.get('result') != {k: v for k, v in vote.items() if k != 'role'}):
            raise ResearchBlocked('修订架构计划缺少同一版本真实评议交付。')
    from .protocol_review_recovery import COLLECTION, key as protocol_key
    segmented = engine.store.get(COLLECTION, protocol_key(run_id, rh))
    if segmented and segmented.get('segmented_review'):
        if segmented.get('status') != 'approved' or segmented['segmented_review'].get('status') != 'approved':
            raise ResearchBlocked('分段实验协议尚未批准；不能授权冻结计划。')
        from .protocol_review_segments import checked
        checked(engine, segmented, design, complete=True)


def _objections(review):
    from .engine import digest
    if review.get('review_recovery_protocol') == 1:
        from .architecture_review_recovery import effective_source
        review = effective_source(review)
    # Final summaries may omit a section blocker. Preserve all actual dissent;
    # duplicate text from the same role shares the final issue's historical ID.
    objections = {}
    for final, vote in [(True, v) for v in review.get('reviews', [])] + [(False, v) for v in review.get('unit_reviews', [])]:
        updates = vote.get('progress', {}).get('issue_updates') if final else None
        stable_ids = {index: u['issue_id'] for u in updates or []
                      if u.get('disposition') in {'carried_forward', 'reopened'}
                      for index in u.get('current_issue_indices', [])}
        for index, text in enumerate(vote.get('issues', [])):
            identity = (vote['role'], text.strip())
            row = objections.setdefault(identity, {
                'id': stable_ids.get(index, digest([review['design_hash'], vote['role'], index, text])),
                'role': vote['role'], 'issue': text})
            if final and updates is not None:
                row['final_issue_index'] = index
            remedies = [copy.deepcopy(f) for f in vote.get('fixes', []) if f.get('issue_index') == index]
            if remedies:
                row.setdefault('fixes', [])
                row['fixes'].extend(f for f in remedies if f not in row['fixes'])
    return list(objections.values())


def prepare_read_window_handoff(engine, run_id):
    """Only recover a completed author stopped by the old cumulative quota.

    Never replay an unknown paid response or replace an independent review.
    The old author decision and every read receipt remain untouched.
    """
    from .engine import ResearchBlocked, Harness, digest
    run = engine.store.get('runs', run_id)
    record = state(engine, run_id)
    if record and record.get('reading_window_handoff'):
        return record['reading_window_handoff']
    if not record or run['status'] not in {'blocked', 'failed', 'paused'}:
        raise ResearchBlocked('文档窗口恢复需要已停止且存在原架构修订交接的任务。')
    _available(engine, run_id)
    original = engine.store.get('steps', run_id + ':design', {}).get('output')
    _checked(engine, run_id, original)
    if engine.store.active_reasoning(run_id, {
            'running', 'queued', 'starting', 'turn_start_dispatched', 'thread_start_dispatched', 'thread_resume_dispatched'}):
        raise ResearchBlocked('仍有活动付费请求，不能准备文档窗口恢复。')
    pending = record.get('pending')
    if not pending or pending['context'].get('document_read_window_protocol'):
        raise ResearchBlocked('没有受旧累计读取配额影响的待处理架构作者交接。')
    expected = Harness.decision_key(run_id, 'engineer', pending['purpose'],
        pending['instruction'], pending['context'], pending['schema'])
    decision = engine.store.get('decisions', expected, {})
    if (expected != pending['handoff_id'] or decision.get('run_id') != run_id
            or decision.get('role') != 'engineer' or not decision.get('result')):
        raise ResearchBlocked('原架构作者尚无确定的保存交付，不能重发未知结果请求。')
    progress = engine.store.get('handoff_progress', expected, {})
    pages = [v['result'] for v in progress.get('tool_results', [])
        if v.get('call', {}).get('name') == 'documents.read'
        and isinstance(v.get('result'), dict) and isinstance(v['result'].get('text'), str)]
    quota_error = '本次输入正文达到范围；按目录定位，保留尚待核实的问题，不累积全文。'
    denied = [v for v in progress.get('tool_results', [])
        if v.get('call', {}).get('name') == 'documents.read'
        and v.get('result', {}).get('error') == quota_error
        and v.get('result', {}).get('text_returned') is False]
    used = sum(len(p['text']) for p in pages)
    per_document = {}
    for page in pages:
        did = page['document_id']
        per_document[did] = per_document.get(did, 0) + len(page['text'])
    if not denied or not (used >= 12000 or max(per_document.values(), default=0) >= 6000):
        raise ResearchBlocked('没有累计文档配额耗尽的原始读取证据，不能覆盖原科学判断。')
    from . import document_index
    directories = {v['document_id']: v for v in pending['context']['indexed_documents']}
    for denied_row in denied:
        args = denied_row['call']['arguments']
        doc = directories.get(args.get('document_id'))
        if not doc or doc['sha256'] != args.get('expected_sha256'):
            raise ResearchBlocked('受影响读取不属于原固定输入。')
        document_index.read(engine, run_id, args)  # exact local bytes; no AI call
    audit_id = run_id + ':document-read-window-handoff:1'
    payload = {'pending': copy.deepcopy(pending), 'previous_result': copy.deepcopy(decision['result']),
        'original_progress': copy.deepcopy(progress), 'progress_sha256': digest(progress),
        'returned_document_chars': used, 'per_document_chars': per_document,
        'denied_reads': len(denied), 'reason': 'controller_cumulative_document_quota',
        'scope': 'One new writing candidate; no scientific approval, code or experiment authorization.'}
    audit = {'id': audit_id, 'run_id': run_id, 'source': 'controller_bug_recovery',
        'previous_handoff_id': expected, 'status': 'prepared', 'version': 1,
        'payload': payload, 'sha256': digest(payload), 'created_at': now()}
    if engine.store.get('stage_contexts', audit_id):
        raise ResearchBlocked('已有文档窗口恢复审计，禁止覆盖或再次建立恢复。')
    engine.store.put('stage_contexts', audit_id, audit)
    record['reading_window_handoff'] = {k: audit[k] for k in
        ('id', 'status', 'previous_handoff_id', 'sha256', 'created_at')}
    engine.store.put('architecture_revisions', record['id'], record)
    engine.store.event(run_id, 'architecture.read_window_prepared',
        '已保留旧作者和全部读取回执，安排按请求窗口重新核对受拒绝的章节。', {'audit_id': audit_id})
    return record['reading_window_handoff']


def _activate_read_window(engine, h, run_id, record):
    from .engine import ResearchBlocked, digest
    queued = record.get('reading_window_handoff')
    if not queued or queued['status'] != 'prepared':
        return record
    audit = engine.store.get('stage_contexts', queued['id'], {})
    if audit.get('sha256') != queued['sha256'] or digest(audit.get('payload')) != queued['sha256']:
        raise ResearchBlocked('文档窗口恢复审计发生变化。')
    payload = audit['payload']
    if record.get('pending') != payload['pending']:
        raise ResearchBlocked('文档窗口恢复目标已变化。')
    prior = payload['pending']
    text = '# 原架构作者交付与读取故障\n\n此交付未成为批准的新版本；原决定与读取回执保留。\n\n'
    text += '## previous_result\n\n```json\n' + architecture_json(payload['previous_result']) + '\n```\n\n'
    text += '## controller_read_failure\n\n```json\n' + architecture_json({k: payload[k] for k in
        ('returned_document_chars', 'per_document_chars', 'denied_reads', 'reason', 'scope')}) + '\n```\n'
    doc = _save(engine, h, run_id, f'cycle-{engine.cycle(run_id)}/architecture/reading-recovery/{audit["sha256"]}.md',
                text, 'architecture_reading_recovery')
    context = {**prior['context'], 'document_read_window_protocol': 1,
        'indexed_documents': [*prior['context']['indexed_documents'], doc],
        'previous_author_response_document': doc,
        'document_reading_recovery': {'audit_id': audit['id'], 'audit_sha256': audit['sha256'],
                                    'previous_handoff_id': audit['previous_handoff_id']}}
    instruction = prior['instruction'] + ('\n原作者交付和读取故障按 previous_author_response_document 索引保存。'
        '旧读取因控制器累计配额被拒绝；现在按每次请求的有界阅读窗口重新核对必要章节。'
        '独立判断，保留科学约束；读取失败不是科学否证。仍需改变已批准科学方向时继续标明 requires_new_consensus=true。'
        '本次只是新写作候选，不授予任何架构、代码或实验批准。')
    purpose = prior['purpose'] + ':read-window-v1'
    hid = h._handoff(run_id, 'engineer', purpose, instruction, context, prior['schema'])['id']
    record.setdefault('reading_recoveries', []).append({'audit_id': audit['id'],
        'pending_sha256': digest(prior), 'next_handoff_id': hid, 'created_at': now()})
    record.update(status='drafting', document_read_window_protocol=1,
        pending={'purpose': purpose, 'instruction': instruction, 'context': context,
                 'schema': prior['schema'], 'handoff_id': hid},
        reading_window_handoff={**queued, 'status': 'applied', 'next_handoff_id': hid})
    record.pop('reason', None)
    record.pop('response', None)  # complete original retained in immutable audit and decision
    engine.store.put('architecture_revisions', record['id'], record)
    return record


def architecture_json(value):
    from .architecture_documents import _json
    return _json(value)


async def run(engine, h, run_id, original):
    """Review one current version and hand off at most one successor version."""
    from .engine import DESIGN, STR, STRS, ResearchBlocked, digest, obj
    from . import architecture_documents as architecture
    from .experiment_plan import build_plan
    from .lab import assess_resources
    if not enabled(engine, run_id):
        await architecture.review(engine, h, run_id, original)
        return original
    record = await engine._read_consensus_check(run_id, _checked, engine, run_id, original)
    if record is None:
        step = engine.store.get('steps', run_id + ':design')
        record = {'id': key(engine, run_id), 'run_id': run_id, 'version': VERSION,
            'cycle': engine.cycle(run_id), 'original_design_hash': digest(original),
            'original_step_sha256': digest(step) if step and step.get('status') == 'completed' else None,
            'revisions': [], 'created_at': now(), 'status': 'prepared',
            'document_read_window_protocol': 1}
        engine.store.put('architecture_revisions', record['id'], record)
    record = _activate_read_window(engine, h, run_id, record)
    from .architecture_note_recovery import activate as activate_note_recovery
    record = activate_note_recovery(engine, h, run_id, record)
    from .architecture_integration import activate as activate_integration
    record = await activate_integration(engine, h, run_id, record)
    from . import architecture_integration_recovery as delivery_recovery
    record = delivery_recovery.activate(engine, h, run_id, record)
    if record.get('definition_handoff_id'):
        from .architecture_definitions import run as review_definitions
        previous_definition = record['definition_handoff_id']
        record = await review_definitions(engine, h, run_id, record)
        if record.get('definition_handoff_id') != previous_definition:
            return await engine._read_consensus_check(run_id, effective, engine, run_id, original)
        definition = engine.store.get('architecture_definitions', record['definition_handoff_id'], {})
        if (record['status'] == 'needs_consensus'
                and definition.get('architecture_workflow_protocol') == 1
                and definition['status'] in {'prepared', 'drafting', 'reviewing'}):
            return await engine._read_consensus_check(run_id, effective, engine, run_id, original)
    if record['status'] in {'needs_consensus', 'blocked'}:
        raise ResearchBlocked(record['reason'])
    design = await engine._read_consensus_check(run_id, effective, engine, run_id, original)
    try:
        review = await architecture.review(engine, h, run_id, design)
    except ResearchBlocked:
        review = engine.store.get('architecture_reviews', f'{run_id}:architecture:{engine.cycle(run_id)}:{digest(design)}')
        if not review or review['status'] != 'rejected':
            raise
    else:
        if review and review['status'] == 'reviewing':
            engine.store.put('architecture_revisions', record['id'], {**record, 'status': 'reviewing'})
            return design
        # Revalidation still reads the full evidence chain above. Replaying an
        # approved version must preserve its original completion receipt: the
        # implementation restart binds this exact scientific record hash.
        if record['status'] != 'approved':
            engine.store.put('architecture_revisions', record['id'],
                {**record, 'status': 'approved', 'completed_at': now()})
        return design
    _available(engine, run_id)
    from . import architecture_convergence as convergence
    if convergence.enabled(engine, run_id):
        convergence.record(engine, run_id, 'architecture', review, design)
        convergence.check(engine, run_id, 'architecture')
    docs = await inputs(engine, h, run_id, record, original)
    if not record.get('pending'):
        objections = _objections(review)
        context = review_context(engine, run_id, design)
        docs = context['indexed_documents']
        context = {**context, 'source_tool_protocol': 1,
            'research_cycle': engine.cycle(run_id), 'proposal_hash': record['proposal_hash'],
            'parent_design_hash': digest(design), 'architecture_document': review['document'],
            'indexed_documents': [review['document'], *[d for d in docs if d['document_id'] != review['document']['document_id']]],
            'review_fixes': objections, 'revision_number': len(record['revisions']) + 1,
            'trial_budget': engine.store.get('runs', run_id)['config']['max_trials'],
            'per_trial_seconds': engine.store.get('runs', run_id)['config']['experiment_timeout']}
        if convergence.enabled(engine, run_id):
            context.update(convergence.context(engine, run_id, 'architecture', design))
        schema = obj({'design': copy.deepcopy(DESIGN), 'proposal_hash': STR, 'parent_design_hash': STR,
            'resolved_issue_ids': STRS, 'change_summary': STR, 'requires_new_consensus': {'type': 'boolean'}})
        instruction = ('根据已保存的两位独立架构评议，输出新的完整结构化 DESIGN，控制器将保存新 Markdown 后重审。'
            '先按输入索引定位原架构、批准方案、原冻结计划、已校验论文选读笔记和宿主当前约束。'
            '不复制全文或重新下载已有论文。逐项处理 review_fixes，绑定 proposal_hash 和 parent_design_hash 原值。'
            '保持批准机制、强基线、所有变体、种子、阈值、数据隔离和停止规则；只闭合明确实现与核验缺口。'
            '所有原参数、分支、配置引用须确定展开，新增字段须明确作用；不得隐藏调参或删必要比较。'
            '当前明确操作授权与宿主配置优先于历史叙述的调用上限；模型调用上限与实验次数独立。'
            '数值参数与资源须有限且可执行。定量验收使用val_指标，严格不等式设greater_than。'
            '逐种子原始正向条件与区间排零分别执行；零分母不能通过删除记录绕过。'
            '缺少的实现定义须补齐，不能仅把必改问题转写为局限。已有选读不足以认证发表版等价或新颖性。'
            '必须改变批准科学方向、生成器或放宽门槛时 requires_new_consensus=true，由宿主退回重审。'
            '否则 resolved_issue_ids须逐一覆盖本次意见，change_summary描述实质修改。不要写代码或运行实验。')
        from .design_review_policy import AUTHOR
        instruction += AUTHOR
        if context.get('architecture_progress_protocol') == convergence.VERSION:
            instruction += convergence.AUTHOR
        purpose = f'design_revision:{engine.cycle(run_id)}:{digest(design)}:{len(record["revisions"])+1}'
        from . import comparison_task_policy as task_policy
        instruction, context, schema = task_policy.assignment(
            engine, run_id, 'engineer', purpose, instruction, context, schema,
            task_policy.DESIGN_GUIDANCE)
        hid = h._handoff(run_id, 'engineer', purpose, instruction, context, schema)['id']
        record.update(status='drafting', pending={'purpose': purpose, 'context': context,
            'instruction': instruction, 'schema': schema, 'handoff_id': hid})
        engine.store.put('architecture_revisions', record['id'], record)
        engine.store.event(run_id, 'architecture.revision_started', '正在按独立修改意见编写新架构版本。',
            {'parent_design_hash': digest(design), 'revision': len(record['revisions']) + 1})
    pending = record['pending']
    from .intelligence import ProviderError
    try:
        local_revalidation = (record.get('delivery_validation') or {}).get('local_revalidation')
        if local_revalidation and pending['context'].get('architecture_delivery_repair_protocol') == 1:
            result = delivery_recovery.cached_revalidated_result(engine, run_id, record)
        else:
            result = await h.ask(run_id, 'engineer', pending['purpose'], pending['instruction'], pending['context'], pending['schema'])
    except (ProviderError, ResearchBlocked) as exc:
        if (pending['context'].get('architecture_delivery_repair_protocol') == 1
                and any(fragment in str(exc) for fragment in ('schema validation', 'no valid JSON', 'Final-only response rejected'))):
            delivery_recovery.mark_failed(engine, run_id, record, exc)
        raise
    try:
        _check_author_delivery(engine, h, run_id, record, design, result, pending['context'])
    except ResearchBlocked:
        if (record.get('status') == 'needs_consensus'
                and pending['context'].get('architecture_progress_protocol') == convergence.VERSION):
            from .architecture_definitions import prepare as prepare_definitions
            prepare_definitions(engine, run_id, continuation=True)
            return design
        raise
    if pending['context'].get('architecture_integration_protocol') == 1:
        from .architecture_integration import materialize
        try:
            new_design = materialize(engine, run_id, pending, result)
        except (ProviderError, ResearchBlocked) as exc:
            if pending['context'].get('architecture_delivery_repair_protocol') == 1:
                delivery_recovery.mark_failed(engine, run_id, record, exc)
                raise ResearchBlocked(record['reason'] + ' ' + str(exc)) from exc
            if not isinstance(exc, ProviderError):
                raise
            delivery_recovery.record_failure(engine, run_id, record, pending, result, exc)
            delivery_recovery.activate(engine, h, run_id, record)
            return design
    else:
        new_design = {**design, **result['design']}
    dh = digest(new_design)
    prior_designs = [original, *[row['design'] for row in record['revisions']]]
    if (dh in [digest(d) for d in prior_designs]
            or convergence.enabled(engine, run_id) and any(
                convergence.material(new_design, 'architecture') == convergence.material(d, 'architecture')
                for d in prior_designs)):
        record.update(status='blocked', reason='架构修订没有实质新版本，停止重复评议。', response=result)
        engine.store.put('architecture_revisions', record['id'], record)
        raise ResearchBlocked(record['reason'])
    if not new_design['primary_metric'].startswith('val_'):
        if pending['context'].get('architecture_delivery_repair_protocol') == 1:
            delivery_recovery.mark_failed(engine, run_id, record, '修订架构主指标必须仅使用验证集。')
        raise ResearchBlocked('修订架构主指标必须仅使用验证集。')
    try:
        from .experiment_plan import build_for_engine
        checked_plan = build_for_engine(engine, run_id, new_design, pending['context']['trial_budget'])
    except (ValueError, TypeError) as exc:
        if pending['context'].get('architecture_delivery_repair_protocol') == 1:
            delivery_recovery.mark_failed(engine, run_id, record, '修订架构不能形成完整冻结实验矩阵：' + str(exc))
        raise ResearchBlocked('修订架构不能形成完整冻结实验矩阵：' + str(exc)) from exc
    if local_revalidation and pending['context'].get('architecture_delivery_repair_protocol') == 1:
        audit, _ = delivery_recovery.checked_local_revalidation(engine, run_id, local_revalidation['audit_id'], pending=pending)
        if audit['payload']['candidate_hash'] != dh or audit['payload']['plan_hash'] != digest(checked_plan):
            raise ResearchBlocked('本地复验后的候选或冻结实验矩阵发生变化；未登记新版本。')
    assessment = assess_resources(new_design['resource_plan'], await engine.execution_hardware(run_id))
    if not assessment['approved']:
        raise ResearchBlocked('修订架构未通过当前资源准入。')
    row = {'number': len(record['revisions']) + 1, 'parent_design_hash': digest(design),
           'design': new_design, 'design_hash': dh, 'response': result,
           'handoff_id': pending['handoff_id'], 'created_at': now()}
    if pending['context'].get('architecture_integration_protocol') == 1:
        row['integration_audit_id'] = pending['context']['integration_audit_id']
    if pending['context'].get('architecture_delivery_repair_protocol') == 1:
        row['delivery_validation_audit_id'] = pending['context']['delivery_validation_audit_id']
        if local_revalidation:
            row['local_revalidation_audit_id'] = local_revalidation['audit_id']
        record['delivery_validation'] = {**record['delivery_validation'], 'status': 'repaired',
            'reason': '补正已形成合法候选；保留原约束，等待独立架构评议。', 'error': None,
            'candidate_hash': dh, 'scientific_approval': False}
    record['revisions'].append(row)
    record.pop('pending')
    record.update(status='reviewing', updated_at=now())
    engine.store.put('architecture_revisions', record['id'], record)
    engine.store.event(run_id, 'architecture.revision_saved', '新架构版本已保存，开始独立分段评议；原否决保留。',
        {'design_hash': dh, 'revision': row['number'], 'parent_design_hash': row['parent_design_hash']})
    # The next persisted graph dispatch reviews this version. This call never
    # runs another author/reviewer round or treats an unreviewed version as ready.
    return new_design


def view(engine, run_id):
    from .architecture_definitions import view as definition_view
    from .architecture_convergence import view as progress_view
    from .architecture_integration_recovery import view as delivery_view
    saved = state(engine, run_id)
    if not saved:
        return None
    pending = saved.get('pending') or {}
    progress = engine.store.get('handoff_progress', pending.get('handoff_id', ''), {}) if pending else {}
    pages = [row['result'] for row in progress.get('tool_results', [])
        if row.get('call', {}).get('name') == 'documents.read'
        and isinstance(row.get('result', {}).get('text'), str)]
    ranges = {}
    for page in pages:
        ranges.setdefault(page['document_id'], []).append((page['char_start'], page['char_end']))
    inspected = 0
    for spans in ranges.values():
        merged = []
        for start, end in sorted(spans):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        inspected += sum(end - start for start, end in merged)
    notes = progress.get('document_working_notes') or {}
    handoff = engine.store.get('handoffs', pending.get('handoff_id', ''), {}) if pending else {}
    reading = {'handoff_status': handoff.get('status'), 'continuation_turn': progress.get('turn', 0),
        'finalizing': progress.get('mode') == 'final_only', 'read_pages': len(pages),
        'read_documents': len(ranges), 'unique_inspected_chars': inspected,
        'working_notes_count': len(notes.get('payload', {}).get('entries', [])),
        'working_notes_document': notes.get('document'),
        'review_issue_count': len(pending.get('context', {}).get('review_fixes', [])),
        'scientific_approval': False} if pending else None
    return {k: saved.get(k) for k in ('id', 'version', 'status', 'cycle', 'reason', 'original_design_hash', 'authority_document')} | {
        'revision_count': len(saved['revisions']), 'current_design_hash': saved['revisions'][-1]['design_hash'] if saved['revisions'] else saved['original_design_hash'],
        'pending_parent_design_hash': pending.get('context', {}).get('parent_design_hash'), 'reading': reading,
        'definition_review': definition_view(engine, run_id),
        'convergence': progress_view(engine, run_id, 'architecture'),
        'author_response': {'change_summary': saved['response']['change_summary'],
            'requires_new_consensus': saved['response']['requires_new_consensus'],
            'resolved_issue_count': saved.get('resolved_issue_count', 0),
            'review_issue_count': saved.get('review_issue_count', 0),
            'unresolved_issues': copy.deepcopy(saved.get('unresolved_issues', [])),
            'document': saved.get('author_response_document'), 'scientific_approval': False}
            if saved.get('author_response_document') and saved.get('response') else None,
        'integration': copy.deepcopy(saved.get('integration_handoff')),
        'delivery_validation': delivery_view(engine, run_id, saved)}
