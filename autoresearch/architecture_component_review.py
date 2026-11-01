"""One scoped review dispatch per graph node, with evidence-only amendments."""
from __future__ import annotations

import copy

from .storage import now


def units(record):
    from .engine import digest
    rows = []
    for group in record['work_plan']['groups']:
        current, chars = [], 0
        for iid in group['issue_ids']:
            draft = next(u for u in record['issue_drafts'] if u['issue_id'] == iid)
            if current and chars + draft['document']['total_chars'] > 11000:
                rows.append(current)
                current, chars = [], 0
            current.append(draft)
            chars += draft['document']['total_chars']
        if current:
            rows.append(current)
    scoped = []
    for group in rows:
        ranges = [{'document_id': d['document']['document_id'], 'section_id': s['id'],
                   'start': s['start'], 'end': s['end']}
                  for d in group for s in d['document']['sections']]
        if sum(r['end'] - r['start'] for r in ranges) <= 11000:
            scoped.append((group, ranges))
            continue
        # A single unusually long issue also needs retained per-range judgments.
        part, chars = [], 0
        for scope in ranges:
            start = scope['start']
            while start < scope['end']:
                end = min(scope['end'], start + 11000 - chars)
                part.append({**scope, 'start': start, 'end': end})
                chars += end - start
                start = end
                if chars == 11000:
                    scoped.append((group, part))
                    part, chars = [], 0
        if part:
            scoped.append((group, part))
    return [{'id': digest([record['proposal_hash'], record['parent_design_hash'],
                          [(d['issue_id'], d['document']['sha256']) for d in group], ranges]),
             'number': n + 1, 'total': len(scoped), 'issue_ids': [d['issue_id'] for d in group],
             'documents': [copy.deepcopy(d['document']) for d in group], 'ranges': ranges}
            for n, (group, ranges) in enumerate(scoped)]


def sources(record):
    return [*record['input_documents'], *[u['document'] for u in record['issue_drafts']], record['document']]


def schema():
    from .architecture_definitions import _schema
    from .document_working_notes import ANCHOR
    from .engine import STR, STRS
    from .design_review_policy import schema as review_schema
    shape = _schema(vote=True)
    shape['properties'].pop('reviewed_issue_ids')
    shape['required'].remove('reviewed_issue_ids')
    shape = review_schema(shape, 'required_changes')
    shape['properties'].update(unit_id=STR, disposition={'enum': [
        'approve', 'needs_evidence', 'needs_revision', 'scope_change']},
        affected_issue_ids={**copy.deepcopy(STRS), 'description':
            '仅列需要作者修改的原意见 ID；批准和补读时为空。审查范围已在 review_unit.issue_ids 中。'},
        evidence_requests={'type': 'array', 'maxItems': 4, 'items': copy.deepcopy(ANCHOR)})
    shape['required'] += ['unit_id', 'disposition', 'affected_issue_ids', 'evidence_requests']
    shape['allOf'] += [
        {'if': {'properties': {'approve': {'const': True}}},
         'then': {'properties': {'disposition': {'const': 'approve'},
             'affected_issue_ids': {'maxItems': 0}, 'evidence_requests': {'maxItems': 0}}},
         'else': {'properties': {'disposition': {'enum': [
             'needs_evidence', 'needs_revision', 'scope_change']}}}},
        {'if': {'properties': {'disposition': {'const': 'needs_evidence'}}},
         'then': {'properties': {'affected_issue_ids': {'maxItems': 0},
             'evidence_requests': {'minItems': 1}}}},
        {'if': {'properties': {'disposition': {'enum': ['needs_revision', 'scope_change']}}},
         'then': {'properties': {'affected_issue_ids': {'minItems': 1}}}}]
    return shape


def _entries(record, role, unit):
    return [v for v in [*record.get('unit_reviews', []), *record.get('review_amendments', [])]
            if v['role'] == role and v['unit_id'] == unit['id']]


def check_entry(engine, run_id, record, role, unit, entry):
    from .architecture_definitions import _binding
    from .implementation_documents import read_coverage
    from .engine import Harness, ResearchBlocked, digest
    from .design_review_policy import validate
    excluded = {'role', 'handoff_id', 'unit_number', 'unit_documents', 'amends_sha256'}
    result = {k: v for k, v in entry.items() if k not in excluded}
    _binding(record, result)
    decision = engine.store.get('decisions', entry['handoff_id'], {})
    frozen = engine.store.get('handoff_inputs', entry['handoff_id'], {})
    context = frozen.get('context', {})
    assignment = engine.store.get('handoffs', entry['handoff_id'], {})
    if (decision.get('run_id') != run_id or decision.get('role') != role
            or decision.get('result') != result or entry['role'] != role
            or entry.get('document_sha256') != record['document']['sha256']
            or context.get('architecture_component_review_protocol') not in (1, 2)
            or context.get('review_unit') != unit or entry.get('unit_documents') != unit['documents']
            or entry.get('unit_number') != unit['number']
            or not set(entry['affected_issue_ids']) <= {i['id'] for i in record['issues']}
            or assignment.get('run_id') != run_id or assignment.get('to_role') != role
            or not assignment.get('purpose', '').startswith('architecture_component_review:')
            or assignment.get('input_hash') != digest({k: frozen.get(k) for k in ('instruction', 'context', 'schema')})
            or Harness.decision_key(run_id, role, assignment.get('purpose'), frozen.get('instruction'),
                                    context, frozen.get('schema')) != entry['handoff_id']):
        raise ResearchBlocked('定义分组评议缺少绑定原范围的真实交付。')
    validate(result, 'required_changes')
    for doc in unit['documents']:
        receipts = [{'result': {'section_id': s['section_id'], 'char_start': s['start'], 'char_end': s['end']}}
                    for s in unit['ranges'] if s['document_id'] == doc['document_id']]
        if not read_coverage(engine, run_id, doc, receipts, entry['handoff_id']):
            raise ResearchBlocked('定义分组评议未实际读完本组合同。')
    for a in entry['evidence_requests']:
        d = next((d for d in sources(record) if d['document_id'] == a['document_id']
                  and d['sha256'] == a['expected_sha256']), None)
        section = next((s for s in d['sections'] if s['id'] == a['section_id']), None) if d else None
        if not section or not section['start'] <= a['char_start'] < a['char_end'] <= section['end']:
            raise ResearchBlocked('定向补读只能引用冻结目录中的真实原文范围。')
    disposition = entry['disposition']
    # Protocol 1 did not constrain this field in its schema or explain its
    # approval meaning. Preserve unequivocal paid approvals that annotate only
    # their own read scope; those IDs never become author revision requests.
    legacy_scope = (context.get('architecture_component_review_protocol') == 1
        and set(entry['affected_issue_ids']) <= set(unit['issue_ids']))
    if ((disposition == 'approve') != (entry['approve'] is True)
            or disposition == 'approve' and (entry['evidence_requests']
                or entry['affected_issue_ids'] and not legacy_scope)
            or disposition == 'needs_evidence' and (not entry['evidence_requests'] or entry['affected_issue_ids'])
            or disposition in {'needs_revision', 'scope_change'} and not entry['affected_issue_ids']):
        raise ResearchBlocked('定义评议须区分缺审查证据、具体修改与科学范围变更。')
    if disposition == 'approve' and any(d['result']['definitions'][0]['status'] == 'unresolved'
            for d in record['issue_drafts'] if d['issue_id'] in unit['issue_ids']):
        raise ResearchBlocked('作者仍声明未决的定义不能通过分组批准。')
    previous = context.get('previous_component_review')
    if previous is not None:
        from .engine import digest
        if entry.get('amends_sha256') != digest(previous):
            raise ResearchBlocked('证据恢复评议未绑定原独立意见。')
        for a in previous['evidence_requests']:
            d = next(d for d in sources(record) if d['document_id'] == a['document_id'])
            if not read_coverage(engine, run_id, d, [{'result': {'section_id': a['section_id'],
                    'char_start': a['char_start'], 'char_end': a['char_end']}}], entry['handoff_id']):
                raise ResearchBlocked('证据恢复评议未实际回读要求的决定性范围。')


def checked_role(engine, run_id, record, role):
    from .engine import ResearchBlocked
    out = []
    for unit in units(record):
        entries = _entries(record, role, unit)
        if not entries:
            raise ResearchBlocked('定义分组独立意见仍未完成。')
        for index, entry in enumerate(entries):
            check_entry(engine, run_id, record, role, unit, entry)
            if index:
                from .engine import digest
                if entry.get('amends_sha256') != digest(entries[index - 1]):
                    raise ResearchBlocked('证据恢复意见链发生变化。')
        if entries[-1]['disposition'] == 'needs_evidence':
            raise ResearchBlocked('仍须完成本角色的定向补证据，不能转交作者重写。')
        out.append(entries[-1])
    return out


def affected_issues(engine, run_id, record):
    from .architecture_definitions import ROLES
    affected = set()
    for role in ROLES:
        role_affected = set()
        for entry in checked_role(engine, run_id, record, role):
            if entry['disposition'] in {'needs_revision', 'scope_change'}:
                role_affected.update(entry['affected_issue_ids'])
        final = next((v for v in record['votes'] if v['role'] == role), None)
        if final:
            role_affected.update(final['affected_issue_ids'])
        affected.update(role_affected)
    for group in record['work_plan']['groups']:
        if affected.intersection(group['issue_ids']):
            affected.update(group['issue_ids'])
    return affected


def _retained_assignment(engine, run_id, record, role, unit, previous):
    """Resume the exact frozen assignment even after prompts have improved."""
    from .engine import Harness, ResearchBlocked, digest
    active = record.get('active_review_handoff') or {}
    if active.get('role') != role:
        return None
    hid = active.get('handoff_id')
    frozen = engine.store.get('handoff_inputs', hid, {})
    ctx = frozen.get('context', {})
    if ctx.get('review_unit') != unit or ctx.get('previous_component_review') != previous:
        return None
    assignment = engine.store.get('handoffs', hid, {})
    if (assignment.get('run_id') != run_id or assignment.get('to_role') != role
            or not assignment.get('purpose', '').startswith('architecture_component_review:')
            or assignment.get('input_hash') != digest({k: frozen.get(k) for k in ('instruction', 'context', 'schema')})
            or Harness.decision_key(run_id, role, assignment.get('purpose'), frozen.get('instruction'),
                                    ctx, frozen.get('schema')) != hid):
        raise ResearchBlocked('待恢复分组评议的冻结交接已改变；原交付保留，不能重新派发掩盖。')
    return assignment, frozen


async def advance(engine, h, run_id, record, context):
    """Dispatch one missing group or one evidence amendment, then yield."""
    from .architecture_definitions import ROLES
    from .engine import digest, ResearchBlocked
    all_units = units(record)
    for role in ROLES:
        for unit in all_units:
            entries = _entries(record, role, unit)
            if entries:
                for entry in entries:
                    check_entry(engine, run_id, record, role, unit, entry)
                if entries[-1]['disposition'] != 'needs_evidence':
                    continue
            previous = entries[-1] if entries else None
            if previous and len(entries) > 1 and previous['evidence_requests'] == entries[-2]['evidence_requests']:
                raise ResearchBlocked('定向补读后仍重复相同证据缺口，已保留两次真实意见；需处理具体来源问题。')
            ctx = {**context, 'architecture_workflow_protocol': 1,
                'architecture_read_batch_protocol': 1,
                'architecture_component_review_protocol': 2, 'review_unit': unit,
                'definition_issues': [i for i in record['issues'] if i['id'] in unit['issue_ids']],
                'definition_issue_groups': copy.deepcopy(record['work_plan']['groups']),
                'related_definition_documents': [{'issue_id': u['issue_id'], 'document': copy.deepcopy(u['document'])}
                                                  for u in record['issue_drafts']],
                'architecture_required_documents': [d['document_id'] for d in unit['documents']],
                'indexed_documents': list({d['document_id']: d for d in
                    [*unit['documents'], *context['indexed_documents']]}.values())}
            if previous:
                ctx['previous_component_review'] = copy.deepcopy(previous)
            instruction = ('独立评议 review_unit 中的相关具体定义，先读完 ranges 指定的合同范围，'
                '本组精确分页可在同一 tool_calls 数组批量请求，每次最多24页；优先一次读完本组，'
                '不把每个短标题拆成单次模型往返。批量分支仅允许本组已列出的精确只读分页；'
                '按需回读冻结目录中的原合同或论文，不从头阅读整包。分组内以唯一规范定义为准，'
                '核对引用和补充是否一致。只提供一次完整且可执行的意见。'
                '审查范围已由 review_unit.issue_ids 给出，不填入 affected_issue_ids；'
                'affected_issue_ids 仅表示需要作者修改的范围，approve 时必须为空。'
                'disposition=approve 表示本组明确且无实质阻塞；needs_evidence 表示需要你继续补读，'
                'evidence_requests 精确定位缺失原文范围，affected_issue_ids 此时必须为空，不能让作者重写。'
                'needs_revision 才要求作者修改，affected_issue_ids 列出所有受影响原意见；'
                '只有必须改变已批准科学机制、问题、比较或门槛才 scope_change。'
                '合理新增实现细节可批准；候选或笔记不认证原文或科学成功。'
                '返回原绑定哈希、document_sha256=' + record['document']['sha256'] + '、unit_id=' + unit['id'])
            from .design_review_policy import REVIEW
            instruction += REVIEW
            if previous:
                instruction += ('\n本次是你原意见的同版本证据恢复；实际读完 evidence_requests 后复核，'
                    '只补充或修正原评议，不改变候选，也不把旧读取当作本次原文。')
            purpose = 'architecture_component_review:' + digest([record['id'], unit['id'], len(entries)])
            shape = schema()
            retained = _retained_assignment(engine, run_id, record, role, unit, previous)
            if retained:
                assignment, frozen = retained
                hid, purpose = assignment['id'], assignment['purpose']
                instruction, ctx, shape = (frozen[k] for k in ('instruction', 'context', 'schema'))
            else:
                hid = h._handoff(run_id, role, purpose, instruction, ctx, shape)['id']
            record['active_review_handoff'] = {'role': role, 'handoff_id': hid,
                'unit_number': unit['number'], 'unit_count': len(all_units)}
            record['review_unit_count'] = len(all_units)
            engine.store.put('architecture_definitions', record['id'], record)
            paid = engine.store.get('decisions', hid)
            result = copy.deepcopy(paid['result']) if paid else await h.ask(
                run_id, role, purpose, instruction, ctx, shape)
            entry = {**result, 'role': role, 'handoff_id': hid, 'unit_number': unit['number'],
                'unit_documents': copy.deepcopy(unit['documents'])}
            if previous:
                entry['amends_sha256'] = digest(previous)
            check_entry(engine, run_id, record, role, unit, entry)
            record.setdefault('review_amendments' if previous else 'unit_reviews', []).append(entry)
            record['updated_at'] = now()
            engine.store.put('architecture_definitions', record['id'], record)
            engine.store.event(run_id, 'architecture.component_reviewed',
                '已保存定义分组的独立意见；缺证据继续定向补读，设计缺陷另行修订。',
                {'role': role, 'issue_ids': unit['issue_ids'], 'disposition': entry['disposition'],
                 'handoff_id': hid, 'scientific_approval': False})
            if entry['disposition'] == 'scope_change':
                raise ResearchBlocked('具体定义要求改变原科学范围，必须另行取得科研共识；不能自动改写原批准。')
            return False
    return True


def check_final(engine, run_id, record, vote):
    from .engine import ResearchBlocked
    own = checked_role(engine, run_id, record, vote['role'])
    ctx = engine.store.get('handoff_inputs', vote['handoff_id'], {}).get('context', {})
    if ctx.get('definition_unit_reviews') != own or ctx.get('definition_document') != record['document']:
        raise ResearchBlocked('终审须保留本角色同版本真实分组意见。')
    if vote['approve'] and any(v['disposition'] != 'approve' for v in own):
        raise ResearchBlocked('具体分组仍有真实阻塞，不能通过终审放行。')
    affected = set(vote.get('affected_issue_ids', []))
    required = {iid for v in own if v['disposition'] in {'needs_revision', 'scope_change'}
                for iid in v['affected_issue_ids']}
    if (not affected <= {i['id'] for i in record['issues']} or not required <= affected
            or vote['approve'] and affected or not vote['approve'] and not affected):
        raise ResearchBlocked('终审批改必须准确覆盖受影响的原意见，批准时不得保留未决项。')
    return True
