"""Same-version architecture evidence amendments with immutable paid decisions.

Only new review records select this protocol. An amendment belongs to the same
role, document and scope as its predecessor and must read the requested source.
Initial opinions and their exact reads are never replaced by the effective view.
"""
from __future__ import annotations

from .async_ownership import gather as gather_owned

import asyncio
import copy

from .storage import now

VERSION = 1

INSTRUCTION = '''
disposition 区分 approve、needs_evidence、needs_revision、scope_change。
approve 表示当前范围明确且没有实质阻塞，issues、fixes 和 evidence_requests 都为空。
缺少决定性原文时先按索引定向读取；仍需补证据时用 needs_evidence，evidence_requests
列冻结目录中的精确范围，issues/fixes 只描述证据缺口及本角色应执行的回读，不要求作者改稿。
needs_revision 仅用于已经确认的设计缺陷，给出具体修改及验收条件；scope_change 表示
必须改变批准科学范围，不能自行授权。后二者 evidence_requests 为空。
保留已批准定义与上一版已闭合条款；只有新的具体冲突和实际原文依据才可重开。
分页、已归档窗口、尚无测量结果都不是设计缺陷；不能以此重写已明确的合同。
'''


def schema(*, final=False):
    from .architecture_documents import _schema
    from .design_review_policy import schema as policy_schema
    from .document_working_notes import ANCHOR
    result = policy_schema(_schema(final=final), 'issues')
    result['properties'].update(
        disposition={'enum': ['approve', 'needs_evidence', 'needs_revision', 'scope_change']},
        evidence_requests={'type': 'array', 'maxItems': 4, 'items': copy.deepcopy(ANCHOR)})
    result['required'] += ['disposition', 'evidence_requests']
    result['allOf'] += [
        {'if': {'properties': {'approve': {'const': True}}},
         'then': {'properties': {'disposition': {'const': 'approve'}, 'evidence_requests': {'maxItems': 0}}},
         'else': {'properties': {'disposition': {'enum': ['needs_evidence', 'needs_revision', 'scope_change']}}}},
        {'if': {'properties': {'disposition': {'const': 'needs_evidence'}}},
         'then': {'properties': {'evidence_requests': {'minItems': 1}}},
         'else': {'properties': {'evidence_requests': {'maxItems': 0}}}}]
    if final:
        from .architecture_convergence import schema as progress_schema
        result = progress_schema(result, structured=True)
    return result


def demo_read_calls(context):
    """The explicit demo uses the same controller read tools and coverage gate."""
    previous = context.get('previous_architecture_evidence_review')
    targets = []
    if previous:
        targets = [*previous['evidence_requests'], *[
            anchor for source in context.get('architecture_source_index_recovery', {}).get('sources', [])
            for anchor in source['reads']]]
    elif context.get('review_unit'):
        doc = context['architecture_document']
        targets = [{'document_id': doc['document_id'], 'expected_sha256': doc['sha256'],
                    'section_id': r['section_id'], 'char_start': r['start'], 'char_end': r['end']}
                   for r in context['review_unit']['ranges']]
    pages = [r['result'] for r in context.get('tool_results', [])
             if r.get('call', {}).get('name') == 'documents.read' and isinstance(r.get('result'), dict)
             and r['result'].get('text')]
    calls = []
    for anchor in targets:
        doc = next(d for d in context['indexed_documents'] if d['document_id'] == anchor['document_id'])
        section = next(s for s in doc['sections'] if s['id'] == anchor['section_id'])
        cursor = anchor['char_start']
        for page in sorted(pages, key=lambda p: p['char_start']):
            if (page['document_id'] == doc['document_id'] and page['sha256'] == doc['sha256']
                    and page['section_id'] == section['id'] and page['char_start'] <= cursor):
                cursor = max(cursor, page['char_end'])
        if cursor < anchor['char_end']:
            calls.append({'name': 'documents.read', 'arguments': {'document_id': doc['document_id'],
                'expected_sha256': doc['sha256'], 'section_id': section['id'],
                'offset': cursor - section['start'], 'limit': min(6000, anchor['char_end'] - cursor)}})
    return calls[:3]


def _key(entry, final):
    return (entry['role'], None if final else entry['unit_id'])


def effective_source(record):
    """Derived terminal opinions; the caller still retains the immutable source."""
    result = copy.deepcopy(record)
    for final, field, amendments in ((False, 'unit_reviews', 'unit_review_amendments'),
                                      (True, 'reviews', 'final_review_amendments')):
        values = {_key(v, final): v for v in record.get(field, [])}
        for entry in record.get(amendments, []):
            values[_key(entry, final)] = entry
        result[field] = list(values.values())
    return result


def _entries(record, role, unit=None):
    final = unit is None
    field, amendments = ('reviews', 'final_review_amendments') if final else ('unit_reviews', 'unit_review_amendments')
    return [v for v in [*record.get(field, []), *record.get(amendments, [])]
            if v['role'] == role and (final or v['unit_id'] == unit['id'])]


def _units(engine, run_id, record):
    from .architecture_documents import units
    from .engine import digest
    if record.get('io_template_review') == 1:
        return []  # Each peer reviews the whole template directly in its final handoff.
    from .architecture_review_budget import KEY, checked
    if record.get(KEY):
        policy = checked(record[KEY], record['document'])
        if record.get('unit_cap') != policy['unit_chars']:
            raise ValueError('Architecture unit size differs from its frozen reading budget')
    pages = units(engine, run_id, record['document'], cap=record.get('unit_cap'))
    # These two navigation labels were added after existing paid review scopes
    # were frozen. Keep their original identity without altering actual reads;
    # text, version SHA, range, call arguments and all other fields stay bound.
    identity_pages = [[{'call': page['call'], 'result': {
        key: value for key, value in page['result'].items()
        if key not in {'document_version', 'evidence_state'}}} for page in receipts]
        for receipts in pages]
    return [{'id': digest([record['document']['sha256'], number, identity_pages[number]]),
             'number': number + 1, 'total': len(pages),
             'ranges': [{'section_id': p['result']['section_id'], 'title': p['result']['title'],
                         'start': p['result']['char_start'], 'end': p['result']['char_end']} for p in receipts]}
            for number, receipts in enumerate(pages)]


def _required(ranges):
    return [{'result': {'section_id': r['section_id'], 'char_start': r['start'], 'char_end': r['end']}}
            for r in ranges]


def _anchor_document(context, anchor):
    from .engine import ResearchBlocked
    doc = next((d for d in context['indexed_documents'] if d['document_id'] == anchor['document_id']
                and d['sha256'] == anchor['expected_sha256']), None)
    section = next((s for s in doc['sections'] if s['id'] == anchor['section_id']), None) if doc else None
    if not section or not section['start'] <= anchor['char_start'] < anchor['char_end'] <= section['end']:
        raise ResearchBlocked('架构补证据请求未绑定冻结目录中的真实原文范围。')
    return doc


def check_entry(engine, run_id, record, role, unit, entry, previous=None):
    from .engine import Harness, ResearchBlocked, digest
    from .implementation_documents import read_coverage
    from .design_review_policy import validate_summary
    final = unit is None
    omitted = {'role', 'handoff_id', 'ranges', 'unit_number', 'amends_sha256'}
    result = {k: v for k, v in entry.items() if k not in omitted}
    hid = entry['handoff_id']
    decision = engine.store.get('decisions', hid, {})
    frozen = engine.store.get('handoff_inputs', hid, {})
    assignment = engine.store.get('handoffs', hid, {})
    context = frozen.get('context', {})
    prefix = 'design_document_review:' if final else 'design_document_section:'
    if (decision.get('run_id') != run_id or decision.get('role') != role or decision.get('result') != result
            or entry.get('role') != role or assignment.get('run_id') != run_id or assignment.get('to_role') != role
            or not assignment.get('purpose', '').startswith(prefix)
            or context.get('architecture_review_recovery_protocol') != VERSION
            or context.get('architecture_document') != record['document']
            or context.get('design_hash') != record['design_hash']
            or context.get('architecture_review_read_budget') != record.get('architecture_review_read_budget')
            or result.get('document_sha256') != record['document']['sha256']
            or context.get('review_unit') != unit
            or assignment.get('input_hash') != digest({k: frozen.get(k) for k in ('instruction', 'context', 'schema')})
            or Harness.decision_key(run_id, role, assignment.get('purpose'), frozen.get('instruction'),
                                    context, frozen.get('schema')) != hid):
        raise ResearchBlocked('架构评议缺少原角色、同版本范围绑定的真实交付。')
    if (context.get('previous_architecture_evidence_review') != previous
            or entry.get('amends_sha256') != (digest(previous) if previous else None)):
        raise ResearchBlocked('架构补证据意见链与原独立意见不一致。')
    if final:
        if context.get('io_template_review') != record.get('io_template_review'):
            raise ResearchBlocked('输入输出模板评审与原交接协议不一致。')
        if record.get('io_template_review') == 1 and previous is None:
            required = [{'result': {'section_id': s['id'], 'char_start': s['start'], 'char_end': s['end']}}
                        for s in record['document']['sections']]
            if not read_coverage(engine, run_id, record['document'], required, hid):
                raise ResearchBlocked('模板与输入输出组合表尚未完整交付给本角色。')
        if result.get('design_hash') != record['design_hash']:
            raise ResearchBlocked('架构终审设计版本不一致。')
        own = [v for v in effective_source(record)['unit_reviews'] if v['role'] == role]
        if context.get('section_reviews') != own:
            raise ResearchBlocked('架构终审未绑定本角色的有效分段意见。')
        from .architecture_convergence import validate_vote
        validate_vote(result, context, record['document'])
        # Ledger closure anchors are actual reads of the unchanged candidate,
        # not merely references to a valid character offset.
        from .architecture_issue_ledger import evidence_covered
        if not evidence_covered(engine, run_id, record['document'], result, [hid], context):
            raise ResearchBlocked('架构问题闭合或重开缺少本角色实际读取的验收依据。')
    else:
        if (result.get('unit_id') != unit['id'] or entry.get('ranges') != unit['ranges']
                or entry.get('unit_number') != unit['number']):
            raise ResearchBlocked('架构分段评议范围与原交接不一致。')
        if previous is None and not read_coverage(engine, run_id, record['document'], _required(unit['ranges']), hid):
            raise ResearchBlocked('架构分段评议未实际读完指定范围。')
    validate_summary(result, context, 'issues')
    disposition = result.get('disposition')
    requests = result.get('evidence_requests', [])
    if ((disposition == 'approve') != (result['approve'] is True)
            or disposition not in {'approve', 'needs_evidence', 'needs_revision', 'scope_change'}
            or (disposition == 'needs_evidence') != bool(requests)):
        raise ResearchBlocked('架构评议须区分待补证据、具体设计缺陷与科学范围变更。')
    for anchor in requests:
        _anchor_document(context, anchor)
    if previous:
        if previous['disposition'] != 'needs_evidence':
            raise ResearchBlocked('只能追加原同版本待补证据意见，不能覆盖真实设计否决。')
        for anchor in previous['evidence_requests']:
            doc = _anchor_document(context, anchor)
            if not read_coverage(engine, run_id, doc, [{'result': anchor}], hid):
                raise ResearchBlocked('架构证据恢复未实际回读所要求的决定性范围。')
    if 'architecture_source_index_recovery' in context:
        prefix = []
        for prior in _entries(record, role, unit):
            prefix.append(prior)
            if prior == previous:
                break
        if not previous or not prefix or prefix[-1] != previous:
            raise ResearchBlocked('架构来源目录补正未绑定原独立意见链。')
        # Re-derive the catalog from the verified adoption lineage, not from a
        # model's prose or the directory supplied in a repair request.
        fresh = _context(engine, run_id, None, record, role)
        allowed = {doc['document_id']: doc for doc in fresh['indexed_documents']}
        if any(allowed.get(doc['document_id']) != doc for doc in context['indexed_documents']):
            raise ResearchBlocked('架构来源目录补正包含已改变或不属于当前核验来源的文档。')
        # An unrelated addition to a future catalog must not invalidate an
        # already frozen paid handoff. Recheck its exact subset and hash.
        expected = _source_index_recovery(engine, run_id, context, prefix)
        if (not expected or context['architecture_source_index_recovery'] != expected
                or digest(context['indexed_documents']) != expected['new_catalog_sha256']):
            raise ResearchBlocked('架构来源目录补正的原交付、失败读取或继承文档已经改变。')
        for source in expected['sources']:
            for anchor in source['reads']:
                doc = _anchor_document(context, anchor)
                if not read_coverage(engine, run_id, doc, [{'result': anchor}], hid):
                    raise ResearchBlocked('架构来源目录已补齐，但原角色尚未实际读取恢复的原文范围。')


def _checked_entries(engine, run_id, record, role, unit=None):
    entries = _entries(record, role, unit)
    previous = None
    for entry in entries:
        check_entry(engine, run_id, record, role, unit, entry, previous)
        previous = entry
    return entries


def gate(engine, run_id, record):
    from .architecture_documents import PEERS
    from .engine import ResearchBlocked
    from .architecture_review_budget import validate_parent
    validate_parent(engine, run_id, record)
    expected_units = _units(engine, run_id, record)
    for validation in [*record.get('review_delivery_repairs', []),
                       *([record['review_delivery_validation']] if record.get('review_delivery_validation') else [])]:
        unit = next((u for u in expected_units if u['id'] == validation['scope_id']), None)
        if validation['scope_id'] != 'final' and unit is None or validation['status'] != 'accepted':
            raise ResearchBlocked('架构交付补正尚未核验通过或范围已改变。')
        _check_repair(engine, run_id, record, validation['role'], unit, validation)
    if (len(record.get('unit_reviews', [])) != len(expected_units) * len(PEERS)
            or len(record.get('reviews', [])) != len(PEERS)):
        raise ResearchBlocked('架构文档仍有未评议片段或缺少独立终审。')
    checked_ids = set()
    for role in PEERS:
        for unit in [*expected_units, None]:
            entries = _checked_entries(engine, run_id, record, role, unit)
            if not entries or entries[-1]['disposition'] != 'approve':
                raise ResearchBlocked('架构评议仍有未解决问题或待补证据。')
            checked_ids.update(v['handoff_id'] for v in entries)
    all_entries = [v for field in ('unit_reviews', 'reviews', 'unit_review_amendments', 'final_review_amendments')
                   for v in record.get(field, [])]
    if len(checked_ids) != len(all_entries):
        raise ResearchBlocked('架构评议存在重复、未知范围或未绑定的补证据意见。')


def _context(engine, run_id, design, record, role):
    from .execution_contract import current_constraints
    from .architecture_convergence import context as progress_context
    documents = [record['document'], record['approved_proposal']]
    context = {}
    from .architecture_revisions import state, review_context
    if state(engine, run_id) and state(engine, run_id).get('inputs'):
        context.update(review_context(engine, run_id, design, read_window_protocol=2))
        documents += context.get('indexed_documents', [])
    context.update(indexed_document_context=True, indexed_document_review=True, document_index_protocol=1,
        document_input_protocol=2, document_read_window_protocol=2, source_tool_protocol=1,
        architecture_workflow_protocol=1, architecture_review_recovery_protocol=VERSION,
        design_review_policy_version=1, design_review_issue_field='issues',
        architecture_document=record['document'], approved_proposal=record['approved_proposal'],
        design_hash=record['design_hash'], research_cycle=record['cycle'],
        indexed_documents=list({d['document_id']: d for d in documents}.values()),
        architecture_required_documents=[record['document']['document_id']],
        execution_constraints=current_constraints(engine.store, run_id))
    if design is not None:
        context.update(progress_context(engine, run_id, 'architecture', design, role=role, structured=True))
    from .dataset_transition import review_scope
    context.update(review_scope(engine.store, run_id))
    from .dataset_contract import from_design
    request_catalog = from_design(design) if design is not None else None
    if request_catalog:
        context.update(dataset_catalog=copy.deepcopy(request_catalog), dataset_request_protocol=2)
    from .architecture_review_budget import KEY
    if record.get(KEY):
        context[KEY] = copy.deepcopy(record[KEY])
        context['prior_same_version_reviews'] = [copy.deepcopy(v) for v in record.get('inherited_unit_reviews', []) if v['role'] == role]
    return context


def _source_index_recovery(engine, run_id, context, entries):
    """A newly accessible, previously denied exact source is material progress.

    Only actual failed reads from this opinion chain can qualify. The caller
    supplies a catalog derived from validated task-owned definition ancestry.
    No historical request, decision or tool receipt is rewritten.
    """
    from .engine import digest
    if not entries or entries[-1].get('disposition') != 'needs_evidence':
        return None
    previous = entries[-1]
    frozen = engine.store.get('handoff_inputs', previous['handoff_id'], {})
    old = frozen.get('context', {}).get('indexed_documents', [])
    old_ids = {doc['document_id'] for doc in old}
    additions = {doc['document_id']: doc for doc in context['indexed_documents']
                 if doc['document_id'] not in old_ids}
    if not additions:
        return None
    sources = []
    seen = set()
    for entry in entries:
        hid = entry['handoff_id']
        progress = engine.store.get('handoff_progress', hid, {})
        source_input = engine.store.get('handoff_inputs', hid, {})
        source_docs = source_input.get('context', {}).get('indexed_documents', [])
        source_ids = {doc['document_id'] for doc in source_docs}
        reads = []
        for receipt in progress.get('tool_results', []):
            call, result = receipt.get('call', {}), receipt.get('result')
            args = call.get('arguments', {})
            if (call.get('name') != 'documents.read' or not isinstance(result, dict)
                    or result.get('error') != '文档不属于本次固定输入索引'
                    or result.get('text_returned') is not False or not isinstance(args, dict)):
                continue
            doc = additions.get(args.get('document_id'))
            if (not doc or doc['document_id'] in source_ids
                    or args.get('expected_sha256') != doc['sha256']):
                continue
            section = next((s for s in doc['sections'] if s['id'] == args.get('section_id')), None)
            offset, limit = args.get('offset', 0), args.get('limit', 4000)
            if (not section or type(offset) is not int or not 0 <= offset < section['chars']
                    or type(limit) is not int or not 1 <= limit <= 6000):
                continue
            anchor = {'document_id': doc['document_id'], 'expected_sha256': doc['sha256'],
                'section_id': section['id'], 'char_start': section['start'] + offset,
                'char_end': min(section['end'], section['start'] + offset + limit)}
            if digest(anchor) not in seen:
                reads.append(anchor)
                seen.add(digest(anchor))
        if reads:
            sources.append({'handoff_id': hid, 'entry_sha256': digest(entry),
                'input_sha256': digest(source_input), 'progress_sha256': digest(progress), 'reads': reads})
    if not sources:
        return None
    return {'version': 1, 'predecessor_handoff_id': previous['handoff_id'],
        'predecessor_sha256': digest(previous), 'prior_catalog_sha256': digest(old),
        'new_catalog_sha256': digest(context['indexed_documents']), 'sources': sources}


def _remaining_limit(record, entries, *, source_recovery=None):
    from .engine import ResearchBlocked
    from .engine import digest
    if len(entries) - 1 >= record['evidence_amendment_limit']:
        raise ResearchBlocked('同版本架构定向补证据已达本交接上限；实际意见与读取均已保留。')
    if len(entries) > 1:
        signature = lambda v: {digest(a) for a in v['evidence_requests']}
        if signature(entries[-1]) == signature(entries[-2]) and not source_recovery:
            raise ResearchBlocked('架构定向补读后仍重复相同证据缺口；已保留实际意见，须处理具体来源问题。')


def _delivery_integrity(engine, run_id, record, role, unit, entry):
    """Only an intact actual delivery is eligible for a new corrective request."""
    from .engine import Harness, ResearchBlocked, digest
    hid = entry['handoff_id']
    decision = engine.store.get('decisions', hid, {})
    assignment = engine.store.get('handoffs', hid, {})
    frozen = engine.store.get('handoff_inputs', hid, {})
    context = frozen.get('context', {})
    result = {k: v for k, v in entry.items() if k not in {'role', 'handoff_id', 'ranges', 'unit_number', 'amends_sha256'}}
    if (decision.get('run_id') != run_id or decision.get('role') != role or decision.get('result') != result
            or assignment.get('run_id') != run_id or assignment.get('to_role') != role
            or context.get('architecture_review_recovery_protocol') != VERSION
            or context.get('architecture_document') != record['document'] or context.get('review_unit') != unit
            or context.get('design_hash') != record['design_hash']
            or assignment.get('input_hash') != digest({k: frozen.get(k) for k in ('instruction', 'context', 'schema')})
            or Harness.decision_key(run_id, role, assignment.get('purpose'), frozen.get('instruction'),
                                    context, frozen.get('schema')) != hid):
        raise ResearchBlocked('原架构交付审计不完整或已改变，不能用补正请求掩盖。')
    return assignment, frozen, result


def _check_repair(engine, run_id, record, role, unit, validation):
    from .engine import ResearchBlocked, digest
    assignment, frozen, result = _delivery_integrity(engine, run_id, record, role, unit, validation['invalid_entry'])
    repair_context = engine.store.get('handoff_inputs', validation['repair_handoff_id'], {}).get('context', {})
    expected = {'source_handoff_id': assignment['id'], 'source_response_sha256': digest(result),
        'source_input_hash': assignment['input_hash'], 'source_read_sha256': validation['source_read_sha256'],
        'original_entry_sha256': digest(validation['invalid_entry']),
        'original_response': result, 'diagnostic': validation['diagnostic']}
    if (validation['source_response_sha256'] != digest(result)
            or validation['source_handoff_id'] != assignment['id']
            or validation['source_input_hash'] != assignment['input_hash']
            or validation['source_read_sha256'] != digest(engine.store.get('handoff_progress', assignment['id'], {}))
            or repair_context.get('architecture_review_delivery_repair') != expected):
        raise ResearchBlocked('架构补正的原交付或实际读取记录发生变化。')
    return frozen


def _active_handoffs(record):
    # Old reviews stored just the last active assignment. Keep its exact ID on resume.
    legacy = record.get('active_review_handoff')
    return {**({legacy['role']: legacy} if legacy else {}), **record.get('active_review_handoffs', {})}


def _set_active(record, active):
    if record.get('io_template_review') == 1:
        record['active_review_handoffs'] = {**_active_handoffs(record), active['role']: active}
    else:
        record['active_review_handoff'] = active


def _role_validation(record, role, scope_id):
    validations = [*record.get('review_delivery_repairs', []), record.get('review_delivery_validation') or {}]
    return next((v for v in reversed(validations) if v.get('role') == role and v['scope_id'] == scope_id), {})


def _prepare_delivery_repair(engine, h, run_id, record, role, unit, entry, diagnostic):
    from .engine import ResearchBlocked, digest
    assignment, frozen, result = _delivery_integrity(engine, run_id, record, role, unit, entry)
    scope_id = unit['id'] if unit else 'final'
    prior = _role_validation(record, role, scope_id)
    if prior.get('repair_handoff_id') == entry['handoff_id']:
        _check_repair(engine, run_id, record, role, unit, prior)
        prior.update(status='repair_failed', repair_invalid_entry=copy.deepcopy(entry),
                     repair_diagnostic=diagnostic, updated_at=now())
        engine.store.put('architecture_reviews', record['id'], record)
        raise ResearchBlocked('架构交付补正仍未通过同版本核验；原交付和补正均已保留，停止重复提交。')
    if record.get('review_delivery_validation'):
        # A concurrent peer may still be updating its own repair after this one is queued.
        record.setdefault('review_delivery_repairs', []).append(record['review_delivery_validation'])
    source_id = entry['handoff_id']
    repair_context = {**copy.deepcopy(frozen['context']), 'architecture_review_delivery_repair': {
        'source_handoff_id': source_id, 'source_response_sha256': digest(result),
        'source_input_hash': assignment['input_hash'],
        'source_read_sha256': digest(engine.store.get('handoff_progress', source_id, {})),
        'original_entry_sha256': digest(entry),
        'original_response': copy.deepcopy(result), 'diagnostic': diagnostic}}
    instruction = frozen['instruction'] + ('\n你已提交真实交付，但宿主尚未接收。architecture_review_delivery_repair '
        '保留原答复及核验错误。本次仅补正该交付的版本、范围、问题账本或读取缺口，保持同一设计和独立判断。'
        '先按本次范围实际补读所需原文；原答复和历史读取不新增本次阅读信用。'
        '返回完整同协议结果，不得为通过格式核验而取消真实异议或虚构批准。仅有这一次补正交接。')
    if unit:
        from .architecture_read_batch import attach_review_ranges, RANGE_GUIDANCE
        repair_context = attach_review_ranges(repair_context)
        instruction += RANGE_GUIDANCE
    purpose = assignment['purpose'] + ':delivery-repair-v1'
    hid = h._handoff(run_id, role, purpose, instruction, repair_context, frozen['schema'])['id']
    validation = {'status': 'repair_pending', 'role': role, 'scope_id': scope_id,
        'source_handoff_id': source_id, 'source_response_sha256': digest(result),
        'source_input_hash': assignment['input_hash'],
        'source_read_sha256': digest(engine.store.get('handoff_progress', source_id, {})),
        'invalid_entry': copy.deepcopy(entry), 'diagnostic': diagnostic,
        'repair_handoff_id': hid, 'created_at': now()}
    record['review_delivery_validation'] = validation
    _set_active(record, {'role': role, 'scope_id': scope_id, 'handoff_id': hid,
        'predecessor_sha256': entry.get('amends_sha256')})
    engine.store.put('architecture_reviews', record['id'], record)
    engine.store.event(run_id, 'architecture.review_delivery_repair_prepared',
        '架构独立交付已收到但尚未接收；原意见保留，已安排一次同版本定向补正。',
        {'source_handoff_id': source_id, 'repair_handoff_id': hid, 'role': role, 'scope_id': scope_id,
         'diagnostic': diagnostic})
    return record


async def _dispatch(engine, h, run_id, design, record, role, unit, entries):
    from .engine import Harness, ResearchBlocked, digest
    from .design_review_policy import REVIEW
    from .architecture_convergence import STRUCTURED_REVIEW as PROGRESS_REVIEW
    previous = entries[-1] if entries else None
    from .architecture_validation import operation
    context = await engine._read_consensus_check(
        run_id, operation(_context), engine, run_id, design, copy.deepcopy(record), role)
    source_recovery = _source_index_recovery(engine, run_id, context, entries) if previous else None
    if previous:
        _remaining_limit(record, entries, source_recovery=source_recovery)
    if source_recovery:
        context['architecture_source_index_recovery'] = source_recovery
    final = unit is None
    scope_id = 'final' if final else unit['id']
    validation = _role_validation(record, role, scope_id)
    if validation:
        _check_repair(engine, run_id, record, role, unit, validation)
        if validation['status'] == 'repair_failed':
            raise ResearchBlocked('架构交付的一次补正已失败；原交付和诊断均已保留，不能重放原缓存。')
    if final:
        context['section_reviews'] = [v for v in effective_source(record)['unit_reviews'] if v['role'] == role]
        instruction = ('根据你本人同版本有效分段意见形成独立终审，核对跨段一致性。'
            '不从头重读全包；按需回读决定性条款，分段仍有真实设计缺陷不得批准。'
            '完整处理 previous_architecture_review.issue_ledger 中本角色的问题，以稳定 ID '
            '逐项保留、闭合或有依据地重开；不能用同义改述生成新问题绕过原裁决。')
        purpose = 'design_document_review:' + record['design_hash'] + ':recovery-v1'
        if record.get('io_template_review') == 1:
            context.update(io_template_review=1, document_read_window_protocol=3)
            instruction = ('请独立审核随本次交接提供的完整架构模板、任务协议及输入输出组合表。'
                '宿主按模板核对了每行的全部字段。检查各输入输出维度的替换路径、模型适配、预算、数据隔离及验收条件，'
                '一次给出覆盖整批组合的意见。按需补读决定性证据并处理本角色已有的问题。'
                '贝叶斯脚本和选参属于后续参数微调阶段。')
            purpose += ':io-template-v1'
    else:
        context['review_unit'] = unit
        from .architecture_read_batch import attach_review_ranges, RANGE_GUIDANCE
        context = attach_review_ranges(context)
        instruction = ('独立评议 review_unit 指定的精确范围，首次必须按 documents.read 读完该范围。'
            '核对批准机制、实现、隔离、比较、验收和资源；跨章节证据按目录定向核对。'
            'previous_architecture_review 含你本人已保存的历史裁决，保留未受改动影响的条款。') + RANGE_GUIDANCE
        purpose = 'design_document_section:' + unit['id'] + ':recovery-v1'
    if previous:
        context['previous_architecture_evidence_review'] = previous
        instruction += ('本次只追加你本人原意见的同版本证据恢复。实际读取原 evidence_requests，'
            '在新证据基础上修正或保留原结论；旧范围的阅读与意见已保留，不要求从头重读。')
        purpose += ':evidence:' + str(len(entries))
    if source_recovery:
        instruction += ('宿主已从本任务核验过的定义继承链补齐原来遗漏的原文目录。'
            'architecture_source_index_recovery.sources.reads 是你本人此前实际请求但被固定索引拒绝的范围；'
            '本次必须逐段实际读取这些恢复的原文，并核对原问题，不得再次只读继承说明。'
            '这只是来源访问修复，不是问题已解决、科学批准或作者修改；仍由你独立判断。')
    instruction += REVIEW + INSTRUCTION
    from .architecture_review_budget import KEY
    if context.get(KEY):
        instruction += ('\n本阶段采用大窗口：宿主在首次模型请求前把本组全部原文分页投递到 tool_results。'
            '直接阅读已投递正文，按完整批次独立评议，不必为满足格式重复索取同一页。'
            'prior_same_version_reviews 是本角色原范围的有效历史意见，保留其判断；它们不是新批次批准。'
            '额外决定性证据仍可按索引补读。阅读分组数不限制纠错、补证或独立科学判断。')
    if final:
        instruction += PROGRESS_REVIEW
    shape = schema(final=final)
    active = _active_handoffs(record).get(role, {})
    from .architecture_review_restart import assignment as restart_assignment
    restarted = restart_assignment(engine, run_id, record, role, unit)
    if restarted:
        purpose, instruction, context, shape = (restarted[k] for k in ('purpose', 'instruction', 'context', 'schema'))
        hid = h._handoff(run_id, role, purpose, instruction, context, shape)['id']
        record.pop('prepared_review_restart')
    elif active.get('role') == role and active.get('scope_id') == scope_id and active.get('predecessor_sha256') == (digest(previous) if previous else None):
        hid = active['handoff_id']
        assignment = engine.store.get('handoffs', hid, {})
        frozen = engine.store.get('handoff_inputs', hid, {})
        if (assignment.get('run_id') != run_id or assignment.get('to_role') != role
                or assignment.get('input_hash') != digest({k: frozen.get(k) for k in ('instruction', 'context', 'schema')})
                or Harness.decision_key(run_id, role, assignment.get('purpose'), frozen.get('instruction'),
                    frozen.get('context'), frozen.get('schema')) != hid):
            raise ResearchBlocked('待恢复架构评议的冻结交接发生变化，不能重新派发掩盖。')
        purpose = assignment['purpose']
        instruction, context, shape = (frozen[k] for k in ('instruction', 'context', 'schema'))
    else:
        from . import comparison_task_policy as comparison
        instruction, context, shape = comparison.assignment(engine, run_id, role, purpose,
            instruction, context, shape, comparison.REVIEW_GUIDANCE)
        hid = h._handoff(run_id, role, purpose, instruction, context, shape)['id']
    _set_active(record, {'role': role, 'scope_id': scope_id, 'handoff_id': hid,
                         'predecessor_sha256': digest(previous) if previous else None})
    if validation.get('repair_handoff_id') == hid:
        validation.update(status='repairing', updated_at=now())
    engine.store.put('architecture_reviews', record['id'], record)
    if context.get(KEY) and unit and not previous and not engine.store.get('handoff_progress', hid):
        from .architecture_read_batch import review_calls
        from .document_index import read
        receipts = []
        for call in review_calls(context):
            engine.check_active(run_id)
            receipts.append({'call': call, 'result': read(engine, run_id, call['arguments'])})
        h._save_progress(run_id, hid, {'turn': 0, 'seen': {}, 'tool_results': receipts})
    if context.get('io_template_review') == 1 and not previous and not engine.store.get('handoff_progress', hid):
        from .architecture_documents import units
        pages = operation(units)(engine, run_id, record['document'])
        h._save_progress(run_id, hid, {'turn': 0, 'seen': {}, 'tool_results': [p for group in pages for p in group]})
    result = await h.ask(run_id, role, purpose, instruction, context, shape)
    entry = {**result, 'role': role, 'handoff_id': hid}
    if unit:
        entry.update(ranges=unit['ranges'], unit_number=unit['number'])
    if previous:
        entry['amends_sha256'] = digest(previous)
    try:
        await engine._read_consensus_check(
            run_id, operation(check_entry), engine, run_id, copy.deepcopy(record), role, unit, entry, previous)
    except (ResearchBlocked, ValueError) as exc:
        return _prepare_delivery_repair(engine, h, run_id, record, role, unit, entry, str(exc))
    if validation.get('repair_handoff_id') == hid:
        validation.update(status='accepted', accepted_handoff_id=hid, updated_at=now())
    field = ('final_review_amendments' if final else 'unit_review_amendments') if previous else ('reviews' if final else 'unit_reviews')
    record.setdefault(field, []).append(entry)
    record['updated_at'] = now()
    engine.store.put('architecture_reviews', record['id'], record)
    engine.store.event(run_id, 'architecture.evidence_reviewed' if previous else 'architecture.scope_reviewed',
        '已保存同版本独立意见；证据缺口继续由原角色定向核对，设计缺陷另行修订。',
        {'role': role, 'scope_id': scope_id, 'disposition': result['disposition'], 'handoff_id': hid})
    return record


async def advance(engine, h, run_id, design, record):
    """Dispatch independent whole-template peers together; keep segmented scopes ordered."""
    from .architecture_documents import PEERS, _json
    from .engine import ResearchBlocked
    from .architecture_validation import operation
    from .architecture_review_budget import validate_parent
    await engine._read_consensus_check(run_id, operation(validate_parent), engine, run_id, record)
    record.update(status='reviewing')
    expected_units = _units(engine, run_id, record)
    record['unit_count'] = len(expected_units)
    engine.store.put('architecture_reviews', record['id'], record)
    pending = []
    scope_change = False
    parallel = record.get('io_template_review') == 1
    scope_error = '架构评议要求改变已批准科学范围；原异议保留，不能自动改写批准。'
    for role in PEERS:
        for unit in [*expected_units, None]:
            entries = await engine._read_consensus_check(
                run_id, operation(_checked_entries), engine, run_id, record, role, unit)
            if entries and entries[-1]['disposition'] == 'scope_change':
                if not parallel:
                    raise ResearchBlocked(scope_error)
                scope_change = True
                continue
            if entries and entries[-1]['disposition'] != 'needs_evidence':
                continue
            if not parallel:
                return await _dispatch(engine, h, run_id, design, record, role, unit, entries)
            pending.append((role, entries))
    if pending:
        results = await gather_owned(*(_dispatch(engine, h, run_id, design, record, role, None, entries)
                                         for role, entries in pending), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
        return record
    if scope_change:
        raise ResearchBlocked(scope_error)
    effective = effective_source(record)
    approved = all(v['disposition'] == 'approve' for v in [*effective['unit_reviews'], *effective['reviews']])
    if approved:
        await engine._read_consensus_check(run_id, operation(gate), engine, run_id, record)
    record.update(status='approved' if approved else 'rejected', completed_at=now())
    engine.store.put('architecture_reviews', record['id'], record)
    if record.get('architecture_progress_protocol') == 1:
        from .architecture_convergence import record as record_progress
        record_progress(engine, run_id, 'architecture', record, design)
    engine.artifact(run_id, f'cycle-{record["cycle"]}/architecture/{record["design_hash"]}/review.json', _json(record), 'architecture_review')
    engine.store.event(run_id, 'architecture.reviewed', '统一架构同版本独立评议通过。' if approved else '统一架构存在真实设计修改项。',
                       {'document_sha256': record['document']['sha256'], 'design_hash': record['design_hash'], 'approved': approved})
    if not approved:
        raise ResearchBlocked('统一架构评议未通过，真实修改意见已保存。')
    return record
