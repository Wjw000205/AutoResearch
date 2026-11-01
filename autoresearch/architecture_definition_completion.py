"""Issue-sized author deliveries, assembled without inventing an author vote."""
from __future__ import annotations

import copy

from . import architecture_definitions as definitions, architecture_revisions as revisions, document_index
from .storage import now
from .architecture_validation import once

PROTOCOL = 2
_READ_RECOVERY_POLICY_V1 = {'version': 1, 'scope': 'indexed_prior_issue_and_actual_original_read_ranges'}
_READ_RECOVERY_POLICY_V2 = {**_READ_RECOVERY_POLICY_V1, 'version': 2,
                            'reuse_compatible_unreviewed_issues': True}
_READ_RECOVERY_POLICY_V3 = {**_READ_RECOVERY_POLICY_V2, 'version': 3,
                            'document_read_window_protocol': 2}
READ_RECOVERY_POLICY = _READ_RECOVERY_POLICY_V3
WORKFLOW_POLICY = {'version': 4, 'architecture_workflow_protocol': 1}


def read_window_protocol(record):
    """Only newly captured policy v3/v4 changes the disposable prompt window."""
    return 2 if record.get('completion_policy') in (_READ_RECOVERY_POLICY_V3, WORKFLOW_POLICY) else 1


def completion_status(engine, run_id, record, revision):
    """Read-only routing hint; prepare still validates all original evidence."""
    run = engine.store.get('runs', run_id)
    used = len(revision.get('definition_handoffs', []))
    from .architecture_convergence import view
    progress = view(engine, run_id, 'definitions')
    from .architecture_final_delivery import diagnostic
    delivery = diagnostic(engine, run_id, record)
    eligible = (run.get('mode') == 'live' and run.get('stage') == 'design'
        and run.get('status') in {'blocked', 'failed', 'paused'}
        and revision.get('status') == 'needs_consensus'
        and record.get('status') in {'blocked', 'rejected'}
        and bool(record.get('document')) and bool(record.get('candidate')))
    return {'can_prepare': eligible and not progress['stalled'] and not delivery, 'limit_reached': False,
            'versions_used': used, 'version_limit': None, 'next_version': used + 1,
            'convergence': progress,
            'delivery_recovery': delivery,
            'preparation_starts_research': False}


def _directories(engine, run_id, documents):
    from .engine import ResearchBlocked
    unique = {}
    for doc in documents:
        prior = unique.get(doc['document_id'])
        if prior and prior['sha256'] != doc['sha256']:
            raise ResearchBlocked('逐项定义目录包含同一文档的冲突版本。')
        document_index.load(engine, run_id, doc['document_id'], doc['sha256'])
        unique[doc['document_id']] = copy.deepcopy(doc)
    return list(unique.values())


def prepare(engine, run_id, *, continuation=False):
    from .engine import ResearchBlocked, digest
    run, revision = engine.store.get('runs', run_id), revisions.state(engine, run_id)
    allowed = {'blocked', 'failed', 'paused'} | ({'running'} if continuation and run['stage'] == 'design' else set())
    if (not revision or revision['status'] != 'needs_consensus' or run['mode'] != 'live'
            or run['status'] not in allowed):
        raise ResearchBlocked('逐项定义交接只能来自停止的真实任务及原作者科学重审请求。')
    engine._consensus_gate(run_id)
    revisions._available(engine, run_id)
    revisions._checked(engine, run_id, engine.store.get('steps', run_id + ':design')['output'])
    if any((row.get('checkpoint') or {}).get('status') in definitions.TRANSIENT
           for row in engine.store.list('codex_reasoning', run_id)):
        raise ResearchBlocked('仍有活动请求，不能准备逐项定义交接。')
    old = definitions._checked(engine, run_id, revision.get('definition_handoff_id', ''))
    from .architecture_final_delivery import pending
    if pending(old):
        raise ResearchBlocked('本版有待处理的实际终审交付；先恢复交付，不能把交付错误当成科学否决重写候选。')
    if old.get('authoring_protocol') == PROTOCOL and old['status'] in {'prepared', 'drafting', 'reviewing'}:
        return definitions.view(engine, run_id)
    if old['status'] not in {'blocked', 'rejected'} or not old.get('document') or not old.get('candidate'):
        raise ResearchBlocked('缺少已保存的真实未完成候选，不能重新编写逐项定义。')
    check_candidate(engine, run_id, old)
    from . import architecture_convergence as convergence
    if convergence.enabled(engine, run_id):
        convergence.record(engine, run_id, 'definitions', old, old['candidate'])
        convergence.check(engine, run_id, 'definitions')
    progress = {}
    ids = [old.get('author_handoff_id'), (old.get('active_review_handoff') or {}).get('handoff_id')]
    ids += [v['handoff_id'] for v in old['votes']]
    ids += [u['handoff_id'] for u in old.get('issue_drafts', [])]
    for hid in filter(None, ids):
        progress[hid] = copy.deepcopy(engine.store.get('handoff_progress', hid, {}))
    policy = WORKFLOW_POLICY if run['config'].get('architecture_workflow_protocol') == 1 else READ_RECOVERY_POLICY
    payload = {'record': copy.deepcopy(old), 'progress': progress,
               'completion_policy': copy.deepcopy(policy)}
    key = f'{run_id}:architecture-definition-completion:{engine.cycle(run_id)}:{digest(payload)}'
    audit_id = key + ':predecessor'
    docs = [*old['input_documents'], old['document']]
    docs += [u['document'] for u in old.get('issue_drafts', [])]
    docs += [p['document_working_notes']['document'] for p in progress.values()
             if p.get('document_working_notes', {}).get('document')]
    record = {field: copy.deepcopy(old[field]) for field in ('run_id', 'version', 'cycle', 'proposal_hash',
        'parent_design_hash', 'source_author_handoff_id', 'source_author_response_sha256',
        'original_author_payload', 'original_author_payload_sha256', 'issues')}
    record.update(id=key, status='prepared', authoring_protocol=PROTOCOL,
        predecessor_id=old['id'], predecessor_audit_id=audit_id, predecessor_sha256=digest(payload),
        completion_policy=copy.deepcopy(policy),
        input_documents=_directories(engine, run_id, docs), issue_drafts=[], votes=[],
        review_protocol=2, unit_reviews=[], created_at=now(), design_review_policy_version=1,
        definition_feedback_protocol=2)
    if convergence.enabled(engine, run_id):
        record['architecture_progress_protocol'] = convergence.VERSION
        record['issue_ledger_protocol'] = 1
    if policy == WORKFLOW_POLICY:
        record.update(architecture_workflow_protocol=1, review_protocol=3, review_amendments=[])
    audit = {'id': audit_id, 'run_id': run_id, 'kind': 'architecture_definition_predecessor',
             'sha256': digest(payload), 'payload': payload, 'created_at': now()}
    updated = {**revision, 'definition_handoff_id': key,
               'definition_handoffs': [*revision.get('definition_handoffs', []), key]}
    engine.store.batch([('stage_contexts', audit_id, audit), ('architecture_definitions', key, record),
                        ('architecture_revisions', revision['id'], updated)])
    engine.store.event(run_id, 'architecture.definition_completion_prepared',
        '已安排逐项编写并保存具体定义，再合成同版本候选收集三方独立意见。',
        {'id': key, 'issue_count': len(record['issues']), 'scientific_approval': False})
    return definitions.view(engine, run_id)


def _predecessor_summary(engine, run_id, audit_id, expected_sha):
    """Authenticate one archive once per operation; retain only used operands.

    Large transcript/tool bodies are discarded after their original digest and
    live-record equality checks. Callers still check their own policy, identity,
    source pages and independent votes. Nothing survives the operation.
    """
    from .architecture_validation import source_read
    from .engine import ResearchBlocked, digest

    def read():
        audit = engine.store.get('stage_contexts', audit_id, {})
        payload = audit.get('payload', {})
        actual_sha = digest(payload)
        if (audit.get('run_id') != run_id or audit.get('sha256') != actual_sha
                or actual_sha != expected_sha):
            raise ResearchBlocked('归档证据恢复的原交付审计发生变化。')
        old = payload.get('record', {})
        progress = {}
        for hid, value in payload.get('progress', {}).items():
            reads = []
            for row in value.get('tool_results', []):
                page = row.get('result', {})
                if (row.get('call', {}).get('name') == 'documents.read'
                        and isinstance(page.get('text'), str) and page['text']):
                    reads.append({k: page[k] for k in ('document_id','sha256','section_id','char_start','char_end') if k in page})
            progress[hid] = {'sha256': digest(value), 'reads': reads,
                'matches_current': engine.store.get('handoff_progress', hid, {}) == value,
                'notes': value.get('document_working_notes', {}).get('document')}
        return {'record': old, 'completion_policy': payload.get('completion_policy'), 'progress': progress,
            'record_matches_current': engine.store.get('architecture_definitions', old.get('id'), {}) == old}

    return source_read(engine.store, ('definition_predecessor_summary', run_id, audit_id, expected_sha),
                       read, borrowed=True)


@once
def check_predecessor(engine, run_id, record):
    from .engine import ResearchBlocked, digest
    payload = _predecessor_summary(engine, run_id, record['predecessor_audit_id'], record['predecessor_sha256'])
    old = definitions._checked(engine, run_id, record['predecessor_id'])
    if (payload.get('record') != old or not payload['record_matches_current']
            or any(not progress['matches_current'] for progress in payload['progress'].values())
            or record['original_author_payload'] != old['original_author_payload']
            or record['issues'] != old['issues']):
        raise ResearchBlocked('逐项定义交接的前一候选或原读取记录发生变化。')
    policy = payload.get('completion_policy')
    if (record.get('completion_policy') != policy
            or policy not in (None, _READ_RECOVERY_POLICY_V1, _READ_RECOVERY_POLICY_V2, _READ_RECOVERY_POLICY_V3, WORKFLOW_POLICY)):
        raise ResearchBlocked('逐项定义的归档证据恢复策略发生变化。')
    if old.get('candidate'):
        check_candidate(engine, run_id, old)
    elif policy == WORKFLOW_POLICY:
        for unit in old.get('issue_drafts', []):
            check_unit(engine, run_id, old, unit)
    else:
        raise ResearchBlocked('定义交接缺少完整原候选。')
    expected_docs = [*old['input_documents'], *([old['document']] if old.get('document') else [])]
    expected_docs += [u['document'] for u in old.get('issue_drafts', [])]
    expected_docs += [p['notes'] for p in payload['progress'].values() if p['notes']]
    if record['input_documents'] != _directories(engine, run_id, expected_docs):
        raise ResearchBlocked('逐项定义冻结输入目录发生变化。')
    for number, unit in enumerate(record.get('issue_drafts', [])):
        if number >= len(record['issues']) or unit['issue_id'] != record['issues'][number]['id']:
            raise ResearchBlocked('逐项定义交付遗漏、重复或顺序错误。')
        check_unit(engine, run_id, record, unit)


def schema():
    from .document_working_notes import ANCHOR
    result = definitions._schema()
    result['properties']['definitions']['minItems'] = 1
    result['properties']['definitions']['maxItems'] = 1
    result['properties']['definitions']['items']['properties']['definition_md']['maxLength'] = 12000
    result['properties']['source_anchors'] = {'type': 'array', 'minItems': 1, 'maxItems': 20, 'items': ANCHOR}
    result['required'].append('source_anchors')
    return result


def _render_unit(record, result):
    from .engine import digest
    return (definitions._render({**record, 'authoring_protocol': 1}, result) + '\n## 实际读取定位\n\n'
        + '\n'.join(f'- `{a["document_id"]}` / `{a["expected_sha256"]}` / {a["section_id"]} '
                    f'/ [{a["char_start"]}, {a["char_end"]})' for a in result['source_anchors'])
        + f'\n\n逐项实际作者交付指纹：`{digest(result)}`。\n')


@once
def _prior_issue_navigation(engine, run_id, record, issue_id):
    """Expose source navigation, never archived bodies or a fabricated closure."""
    from .engine import ResearchBlocked, digest
    if record.get('completion_policy') not in (_READ_RECOVERY_POLICY_V1, _READ_RECOVERY_POLICY_V2, _READ_RECOVERY_POLICY_V3, WORKFLOW_POLICY):
        return {}  # Existing dispatched assignments retain their exact inputs.
    payload = _predecessor_summary(engine, run_id, record['predecessor_audit_id'], record['predecessor_sha256'])
    prior = next((unit for unit in payload['record'].get('issue_drafts', [])
                  if unit['issue_id'] == issue_id), None)
    if prior is None:
        active = payload['record'].get('active_issue_handoff') or {}
        if record.get('completion_policy') != WORKFLOW_POLICY or active.get('issue_id') != issue_id:
            return {}
        pending_progress = payload['progress'].get(active['handoff_id'], {})
        notes = pending_progress.get('notes')
        return {'interrupted_definition_issue': {'handoff_id': active['handoff_id'],
                    'reading_credit_granted': False, 'scientific_approval': False},
                **({'prior_definition_issue_notes_document': copy.deepcopy(notes)} if notes else {})}
    progress = payload['progress'][prior['handoff_id']]
    original = {doc['document_id']: doc for doc in record['input_documents'] if doc['kind'] not in {
        'document_working_notes', 'architecture_scientific_definitions',
        'architecture_definition_issue', 'architecture_author_response'}}
    ranges = {}
    for page in progress['reads']:
        doc = original.get(page.get('document_id'))
        if not doc or page.get('sha256') != doc['sha256']:
            continue
        ranges.setdefault((doc['document_id'], doc['sha256'], page['section_id']), []).append(
            (page['char_start'], page['char_end']))
    read_index = []
    for (did, sha, section), spans in sorted(ranges.items()):
        merged = []
        for start, end in sorted(spans):
            if merged and start <= merged[-1]['char_end']:
                merged[-1]['char_end'] = max(merged[-1]['char_end'], end)
            else:
                merged.append({'char_start': start, 'char_end': end})
        read_index.append({'document_id': did, 'expected_sha256': sha, 'section_id': section,
                           'actually_read_ranges': merged})
    result = {'definition_read_recovery_protocol': 1,
        'prior_definition_issue_document': copy.deepcopy(prior['document']),
        'prior_definition_issue_author': {'handoff_id': prior['handoff_id'],
            'result_sha256': digest(prior['result']), 'progress_sha256': progress['sha256'],
            'status': prior['result']['definitions'][0]['status'], 'scientific_approval': False},
        'prior_definition_issue_read_index': read_index}
    notes = progress.get('notes')
    if notes:
        result['prior_definition_issue_notes_document'] = copy.deepcopy(notes)
    if record.get('completion_policy') in (_READ_RECOVERY_POLICY_V2, _READ_RECOVERY_POLICY_V3):
        compatible = []
        for issue in record['issues']:
            if issue['id'] == issue_id:
                continue
            reusable = _reusable_issue(engine, run_id, record, issue['id'])
            if not reusable:
                continue
            source_record, unit = reusable
            compatible.append({'issue': copy.deepcopy(issue), 'document': copy.deepcopy(unit['document']),
                'source_definition_id': source_record['id'], 'handoff_id': unit['handoff_id'],
                'candidate_status': unit['result']['definitions'][0]['status'],
                'result_sha256': digest(unit['result']), 'scientific_approval': False,
                'reading_credit_granted': False})
        result['prior_compatible_definition_documents'] = compatible
    return result


@once
def _reusable_issue(engine, run_id, record, issue_id):
    """Reuse an immutable paid candidate, never an independent approval or read."""
    from .engine import ResearchBlocked, digest
    if record.get('completion_policy') not in (_READ_RECOVERY_POLICY_V2, _READ_RECOVERY_POLICY_V3, WORKFLOW_POLICY):
        return None
    payload = _predecessor_summary(engine, run_id, record['predecessor_audit_id'], record['predecessor_sha256'])
    old = payload.get('record', {})
    if (payload.get('completion_policy') != record['completion_policy']
            or old.get('id') != record['predecessor_id']
            or not payload['record_matches_current']
            or old.get('issues') != record['issues']):
        raise ResearchBlocked('复用逐项候选的原交付审计或版本发生变化。')
    # Independent objections may concern interactions between any of the units.
    workflow = record.get('completion_policy') == WORKFLOW_POLICY
    if old.get('authoring_protocol') != PROTOCOL:
        return None
    if not workflow and (old['status'] != 'blocked' or old['votes']):
        return None
    if not workflow and record.get('definition_feedback_protocol') == 2 and old.get('unit_reviews'):
        return None
    if workflow and (old['votes'] or old.get('unit_reviews')):
        # Structured verdicts name affected original IDs. Unclassified legacy
        # dissent remains conservative; no string heuristic creates approval.
        if old.get('review_protocol') != 3:
            return None
        from .architecture_component_review import affected_issues
        affected = affected_issues(engine, run_id, old)
        if issue_id in affected:
            return None
    prior = next((u for u in old.get('issue_drafts', []) if u['issue_id'] == issue_id), None)
    if (not prior or not prior.get('document')
            or prior['result']['compatible_with_approved_scope'] is not True
            or prior['result']['scope_issues']
            or prior['result']['definitions'][0]['status'] not in {'existing_contract', 'proposed_definition'}):
        return None
    if not payload['progress'][prior['handoff_id']]['matches_current']:
        raise ResearchBlocked('复用逐项候选的原实际读取记录发生变化。')
    check_unit(engine, run_id, old, prior)
    return old, {**copy.deepcopy(prior), 'reused_from_definition_id': old['id'],
                 'reused_unit_sha256': digest(prior)}


@once
def check_unit(engine, run_id, record, unit):
    from .engine import ResearchBlocked, Harness
    from .implementation_documents import read_coverage
    from .intelligence import parse_json
    import json
    result, hid = unit['result'], unit['handoff_id']
    parse_json(json.dumps(result), schema())
    definitions._binding(record, result)
    if 'reused_from_definition_id' in unit or 'reused_unit_sha256' in unit:
        reusable = _reusable_issue(engine, run_id, record, unit['issue_id'])
        if not reusable or unit != reusable[1]:
            raise ResearchBlocked('复用逐项候选缺少不可变的原实际交付。')
        return
    decision = engine.store.get('decisions', hid, {})
    frozen = engine.store.get('handoff_inputs', hid, {})
    context = frozen.get('context', {})
    issue = next((i for i in record['issues'] if i['id'] == unit['issue_id']), None)
    if (not issue or [d['issue_id'] for d in result['definitions']] != [unit['issue_id']]
            or decision.get('run_id') != run_id or decision.get('role') != 'director'
            or decision.get('result') != result or context.get('definition_issues') != [issue]
            or context.get('definition_completion_id') != record['id']
            or context.get('predecessor_sha256') != record['predecessor_sha256']
            or Harness.decision_key(run_id, 'director', unit['purpose'], frozen.get('instruction'),
                                    context, frozen.get('schema')) != hid):
        raise ResearchBlocked('逐项定义缺少绑定原范围的实际作者交付。')
    navigation = _prior_issue_navigation(engine, run_id, record, unit['issue_id'])
    if any(context.get(key) != value for key, value in navigation.items()):
        raise ResearchBlocked('逐项定义的定向恢复目录与原实际交付不一致。')
    for anchor in result['source_anchors']:
        doc = next((d for d in record['input_documents'] if d['document_id'] == anchor['document_id']
                    and d['sha256'] == anchor['expected_sha256']), None)
        if doc is None or doc['kind'] in {'document_working_notes', 'architecture_scientific_definitions',
                                         'architecture_definition_issue', 'architecture_author_response'}:
            raise ResearchBlocked('逐项定义必须回读原合同或论文，候选与笔记不能替代原始定位。')
        section = next((s for s in doc['sections'] if s['id'] == anchor['section_id']), None)
        if (section is None or not section['start'] <= anchor['char_start'] < anchor['char_end'] <= section['end']
                or not read_coverage(engine, run_id, doc, [{'result': {
                    'section_id': anchor['section_id'], 'char_start': anchor['char_start'],
                    'char_end': anchor['char_end']}}], hid)):
            raise ResearchBlocked('逐项定义定位缺少实际同版本读取。')
    if unit.get('document'):
        _, text = document_index.load(engine, run_id, unit['document']['document_id'], unit['document']['sha256'])
        if text != _render_unit(record, result):
            raise ResearchBlocked('逐项定义 Markdown 与实际作者交付不一致。')


def assemble(record):
    """Deterministic assembly of paid deliveries; never a fabricated decision."""
    binding = {f: record[f] for f in ('proposal_hash', 'parent_design_hash', 'source_author_response_sha256')}
    results = [u['result'] for u in record['issue_drafts']]
    return {**binding, 'change_summary': '逐项实际作者交付的确定性合成；不是独立投票或执行批准。',
        'compatible_with_approved_scope': all(r['compatible_with_approved_scope'] for r in results),
        'scope_issues': list(dict.fromkeys(i for r in results for i in r['scope_issues'])),
        'definitions': [copy.deepcopy(d) for r in results for d in r['definitions']]}


@once
def check_candidate(engine, run_id, record):
    from .engine import ResearchBlocked
    candidate = record['candidate']
    definitions._binding(record, candidate)
    if record.get('authoring_protocol') == PROTOCOL:
        if (len(record['issue_drafts']) != len(record['issues'])
                or record.get('assembly_provenance') != 'controller_assembly_from_actual_issue_deliveries'
                or assemble(record) != candidate):
            raise ResearchBlocked('科学定义合成缺少完整的逐项实际交付。')
        for unit in record['issue_drafts']:
            check_unit(engine, run_id, record, unit)
    else:
        decision = engine.store.get('decisions', record.get('author_handoff_id', ''), {})
        if decision.get('role') != 'director' or decision.get('run_id') != run_id or decision.get('result') != candidate:
            raise ResearchBlocked('科学定义候选缺少实际作者交付。')
    ids = [d['issue_id'] for d in candidate['definitions']]
    _, text = document_index.load(engine, run_id, record['document']['document_id'], record['document']['sha256'])
    if len(ids) != len(set(ids)) or set(ids) != {i['id'] for i in record['issues']} or text != definitions._render(record, candidate):
        raise ResearchBlocked('科学定义候选与完整实际交付不一致。')


async def write(engine, h, run_id, record):
    from .engine import ResearchBlocked, digest
    prior = engine.store.get('architecture_definitions', record['predecessor_id'])
    workflow = record.get('architecture_workflow_protocol') == 1
    if workflow:
        from .architecture_workflow import plan
        await plan(engine, h, run_id, record)
    feedback_lines = ['# 上版具体定义必改意见', '', '原意见保留，当前候选仍须独立评议。', '']
    reviews = prior['votes']
    if record.get('definition_feedback_protocol') == 2:
        reviews = [*reviews, *prior.get('unit_reviews', [])]
    rejected = [vote for vote in reviews if not vote['approve'] or vote['required_changes']]
    for vote in rejected:
        location = f' / 第 {vote["unit_number"]} 组' if vote.get('unit_number') else ''
        feedback_lines += ['## ' + vote['role'] + location, '', vote['rationale'], '',
                           *['- ' + fix for fix in vote['required_changes']], '']
        if record.get('definition_feedback_protocol') == 2 and vote.get('fixes'):
            feedback_lines += ['### 完整修改方案', '', '```json',
                               revisions.architecture_json(vote['fixes']), '```', '']
    feedback = None
    if rejected:
        feedback = revisions._save(engine, h, run_id,
            f'cycle-{record["cycle"]}/architecture/definition-feedback/{record["predecessor_sha256"]}.md',
            '\n'.join(feedback_lines), 'architecture_definition_feedback')
    for number, issue in enumerate(record['issues']):
        if number < len(record['issue_drafts']):
            check_unit(engine, run_id, record, record['issue_drafts'][number])
            continue
        reusable = _reusable_issue(engine, run_id, record, issue['id'])
        if reusable:
            engine.check_active(run_id)
            prior_record, unit = reusable
            check_unit(engine, run_id, record, unit)
            record['issue_drafts'].append(unit)
            record.update(status='drafting', active_issue_handoff=None,
                reason=f'已保存 {len(record["issue_drafts"])}/{len(record["issues"])} 项，复用兼容的原实际候选；尚未独立评议。')
            engine.store.put('architecture_definitions', record['id'], record)
            engine.store.event(run_id, 'architecture.definition_issue_reused', '已复用兼容的原逐项候选，等待同版本独立评议。',
                {'issue_id': issue['id'], 'predecessor_id': prior_record['id'],
                 'handoff_id': unit['handoff_id'], 'document': unit['document'],
                 'saved_items': len(record['issue_drafts']), 'total_items': len(record['issues']),
                 'new_author_call': False, 'new_source_read': False, 'scientific_approval': False})
            continue
        context = definitions._context(engine, run_id, record)
        context.update(definition_issues=[copy.deepcopy(issue)], definition_completion_id=record['id'],
            predecessor_sha256=record['predecessor_sha256'], definition_issue_number=number + 1,
            definition_issue_count=len(record['issues']), completed_definition_documents=[
                u['document'] for u in record['issue_drafts']])
        navigation = _prior_issue_navigation(engine, run_id, record, issue['id'])
        context.update(navigation)
        if record.get('architecture_progress_protocol') == 1:
            from . import architecture_convergence as convergence
            context.update(convergence.context(engine, run_id, 'definitions', prior.get('candidate') or assemble(prior)))
        if workflow:
            from .architecture_workflow import group_context
            context.update(architecture_workflow_protocol=1, **group_context(record, issue['id']))
        context['indexed_documents'] = _directories(engine, run_id,
            [*context['indexed_documents'], *context['completed_definition_documents'], *([feedback] if feedback else [])])
        if feedback:
            context['previous_definition_feedback_document'] = feedback
        instruction = ('只完成当前 definition_issues 中这一项具体定义，控制器逐项保存 Markdown 后合成同版本候选，另收集三方独立意见。'
            '先用 documents.search 定位批准方案、原架构和已有工作笔记，再 documents.read 回读所需原合同与论文公式。'
            '以前的读取及笔记仍可按索引恢复，不能因为正文不在当前窗口就声称定义缺失。不要为这一项逐页重读整个方案。'
            '如有 previous_definition_feedback_document，先读上版具体必改意见并落实涉及当前项的要求。'
            'existing_contract 必须给出原版本精确定位；确实未冻结的实现细节允许提出唯一、完整、可复现的 proposed_definition，待三方批准。'
            '写清这一项需要的公式、轴、抽样、种子、优先级、归约和停止动作，不能只复述缺失清单。'
            'source_anchors 必须来自本次实际 documents.read 的原合同或论文精确 Unicode 区间；候选、作者答复和笔记只作导航，不认证原文。'
            '保留原问题、机制、强基线、全部变体、模型种子、试次数、阈值、数据隔离、资源门禁及当前无调用次数上限的授权。'
            '不要对未测量结果作成功判断，不写代码或运行实验。compatible_with_approved_scope 判断是否改变上述批准范围；'
            '提出未冻结细节本身不等于改变范围。仍无法完成的定义标 unresolved，改变批准范围须 compatible_with_approved_scope=false 并具体说明。'
            '只返回这一项的版本绑定定义与实际读取定位。')
        if navigation.get('prior_definition_issue_document'):
            instruction += ('\n本次是上一项真实候选的定向补齐，先按 prior_definition_issue_document '
                '索引读取未决结论；自己的原笔记见 prior_definition_issue_notes_document（如有）。'
                'prior_definition_issue_read_index 只列上一作者实际读过的原文范围，不是原文正文或新增阅读信用。'
                '按这些同版本范围定位并 documents.read 回读决定性掩码、读出、字段对应等缺口；'
                '复用其余符合原合同的候选内容，不为本次重读整篇论文或重新生成所有已明确的定义。'
                '原文离开当前窗口只表示需要按索引回读，不能据此宣称原文不存在或不允许提出具体定义。'
                '尚未冻结的初始化、索引布局或归约可提出唯一且兼容的具体实现，待三方独立审议；'
                '不必认证一份尚未存在的旧代码实现。scope_issues 只列实际改变批准科学范围的事项，'
                '候选待评议、尚无测量或正文已归档本身不属于范围改变。确实不能解决的事项仍标 unresolved。'
                '保留全部旧交付和读取记录；本次完整一项交付仍须给出自己实际回读的原始 source_anchors。')
            if navigation.get('prior_compatible_definition_documents'):
                instruction += ('\nprior_compatible_definition_documents 把其他具体意见与兼容的原实际候选索引对应起来。'
                    '当前总览或交叉要求涉及这些意见时，按对应索引复用并核对相关候选细节，避免重新生成全部已明确内容；'
                    '候选间存在矛盾时须明确处理。这里都是尚待独立评议的候选，不是原文、科学批准或新增阅读信用。'
                    '最终仍须实际回读决定性原合同或论文，并由三方对合成版本独立评议。')
        elif navigation.get('interrupted_definition_issue'):
            instruction += ('\n这一项此前中断，尚无完整候选。若有 prior_definition_issue_notes_document，'
                '先按索引查看原工作笔记，用其精确定位定向回读决定性原文后完成当前项。'
                '历史笔记和读取只能导航，不能作为本次 source_anchors 或已获批准的证明。')
        if record.get('design_review_policy_version') == 1:
            from .design_review_policy import AUTHOR
            instruction += AUTHOR
        if record.get('architecture_progress_protocol') == 1:
            instruction += convergence.AUTHOR
        if workflow:
            instruction += ('\n本项与 definition_issue_group 内其他意见共同检查。'
                '若 canonical_definition_document 已提供，先读该真实候选，引用其版本作为唯一规范，'
                '仅补充当前意见还未覆盖的条款，不再复制整套公式或另定义相同模块。'
                'prior_definition_issue_document 中已明确的条款保持，只修未决小节。'
                'interrupted_definition_issue 的旧笔记用于定向恢复，不是原文或新增读取信用。'
                '本次工具回合无固定次数上限；新增决定性原文范围才能继续，避免重复已核验阅读。')
        purpose = 'architecture_definition_issue_author:' + digest([record['id'], issue['id']])
        output_schema = schema()
        active = record.get('active_issue_handoff') or {}
        if active.get('issue_id') == issue['id']:
            frozen = engine.store.get('handoff_inputs', active['handoff_id'])
            if not frozen or frozen.get('run_id') != run_id:
                raise ResearchBlocked('逐项定义在途交接缺少原冻结输入。')
            instruction, context, output_schema = (copy.deepcopy(frozen[field])
                for field in ('instruction', 'context', 'schema'))
            if h.decision_key(run_id, 'director', purpose, instruction, context, output_schema) != active['handoff_id']:
                raise ResearchBlocked('逐项定义在途交接的原冻结身份发生变化。')
        hid = h._handoff(run_id, 'director', purpose, instruction, context, output_schema)['id']
        record.update(status='drafting', active_issue_handoff={'handoff_id': hid, 'issue_id': issue['id'],
            'number': number + 1}, reason=f'正在逐项编写：已保存 {number}/{len(record["issues"])} 项，尚未进入独立评议。')
        engine.store.put('architecture_definitions', record['id'], record)
        result = await h.ask(run_id, 'director', purpose, instruction, context, output_schema)
        unit = {'issue_id': issue['id'], 'handoff_id': hid, 'purpose': purpose, 'result': copy.deepcopy(result)}
        try:
            check_unit(engine, run_id, record, unit)
        except ResearchBlocked as exc:
            record.update(status='blocked', reason=str(exc), invalid_issue_delivery=copy.deepcopy(unit))
            engine.store.put('architecture_definitions', record['id'], record)
            raise
        unit['document'] = revisions._save(engine, h, run_id,
            f'cycle-{record["cycle"]}/architecture/definition-issues/{digest([record["id"], issue["id"], result])}.md',
            _render_unit(record, result), 'architecture_definition_issue')
        record['issue_drafts'].append(unit)
        engine.store.put('architecture_definitions', record['id'], record)
        engine.store.event(run_id, 'architecture.definition_issue_saved', '已保存一项真实科学定义候选。',
            {'issue_id': issue['id'], 'saved_items': len(record['issue_drafts']), 'total_items': len(record['issues']),
             'status': result['definitions'][0]['status'], 'document': unit['document'], 'scientific_approval': False})
    candidate = assemble(record)
    doc = revisions._save(engine, h, run_id,
        f'cycle-{record["cycle"]}/architecture/definitions/{digest(candidate)}.md',
        definitions._render(record, candidate), 'architecture_scientific_definitions')
    record.update(candidate=candidate, document=doc, status='reviewing',
        assembly_provenance='controller_assembly_from_actual_issue_deliveries',
        reason='全部逐项候选已保存并合成，等待三方同版本独立评议。')
    if (not candidate['compatible_with_approved_scope'] or candidate['scope_issues']
            or not workflow and any(d['status'] == 'unresolved' for d in candidate['definitions'])):
        record.update(status='blocked', reason='逐项科学定义仍有未决项或改变批准范围；完整候选和实际交付均已保留。')
    engine.store.put('architecture_definitions', record['id'], record)
    if record['status'] == 'blocked':
        raise ResearchBlocked(record['reason'])
    return record
