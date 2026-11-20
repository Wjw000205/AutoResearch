"""Production orchestration for contract / discovery / repair / verification."""
from __future__ import annotations

from .async_ownership import gather as gather_owned, cancel as cancel_owned

import copy
import json
import asyncio
from functools import wraps


from . import implementation_acceptance as policy
from .implementation_review_routing import digest


def base_context(state, documents, row=None):
    value = {policy.PROTOCOL: 1, 'acceptance_workflow_id': state['id'], 'acceptance_budget': state[policy.FIELD]['budget'],
        'document_input_protocol': 2, 'document_index_protocol': 1,
        'document_read_window_protocol': 1, 'indexed_document_context': True,
        'document_read_budget': {'prompt_chars': 12000, 'page_chars': 6000},
        'indexed_documents': copy.deepcopy(documents)}
    if row:
        value['implementation_document_delivery'] = {'version': 1, 'current_document': {
            'document_id': row['document']['document_id'], 'document_sha256': row['document']['sha256'],
            'review_round': row['index'] + 1, 'spec_hash': row['spec_hash']}}
    return value


def validate_refs(engine, run_id, hid, context, refs, *, current=None, field='evidence', report_evidence=False):
    from .implementation_alignment_evidence import _required_ranges
    from .implementation_documents import read_coverage
    documents = {d['document_id']: d for d in context['indexed_documents']}
    if current and not any(r['document_id'] == current['document_id'] and r['document_sha256'] == current['sha256'] for r in refs):
        from .engine import ResearchBlocked
        from .implementation_document_delivery import location
        target = location(engine, run_id, current)
        version = 'v' + str(target['file_version']) if target['file_version'] else '以 SHA256 标识的版本'
        error = ResearchBlocked('实现文档验收：' + field + ' 必须引用当前文档，不能只引用历史意见或批准来源。'
            f'目标文件：{target["absolute_path"]}；文件版本：{version}；'
            f'document_id={target["document_id"]}；document_sha256={target["document_sha256"]}。'
            '请通过 documents.read 读取相应章节后补充真实范围。')
        error.document_delivery_error = {'field': field, 'expected_document': target,
            'received_documents': [{'document_id': r['document_id'], 'document_sha256': r['document_sha256']} for r in refs]}
        raise error
    from .implementation_document_review_scope import wire_enabled, permitted_read, permitted_report_evidence
    if wire_enabled(context) and context['implementation_document_review_scope']['stage'] == 'global':
        # Predecessors have been independently authenticated before dispatch;
        # exact references to their conclusions do not claim a new global read.
        reports = context['implementation_document_review_scope']['authenticated_reports']
        admitted = [ref for report in reports for check in report['result']['checks']
                    for ref in check['evidence']]
        refs = [ref for ref in refs if ref not in admitted]
        if not refs:
            return
    if wire_enabled(context):
        permitted = permitted_report_evidence if report_evidence else permitted_read
        for ref in refs:
            if not permitted(context, {'document_id': ref['document_id'],
                    'expected_sha256': ref['document_sha256'], 'section_id': ref['section_id'],
                    'offset': ref['offset'], 'limit': ref['limit']}):
                from .supplemental_reads import permits_evidence
                if not permits_evidence(engine, run_id, hid, context, ref, report_evidence=report_evidence):
                    policy.delivery_error('Evidence exceeds frozen scoped read authority: ' + field)
    required = _required_ranges(engine, run_id, refs, {did: d['sha256'] for did, d in documents.items()})
    from . import independent_full_review as full
    if full.KEY in context:
        from .implementation_alignment_evidence import _full_handoffs
        role = engine.store.get('handoffs', hid, {}).get('to_role')
        owners = _full_handoffs(engine, run_id, role, hid, context)
        from .implementation_reading import coverage
        for did, spans in required.items():
            if not coverage(engine, run_id, documents[did], spans, owners)['complete']:
                policy.delivery_error('Full-review evidence lacks actual same-role submitted source ranges: ' + field)
        return
    for did, spans in required.items():
        if not read_coverage(engine, run_id, documents[did], spans, hid):
            policy.delivery_error('证据范围缺少本交接真实读取与交付记录：' + field)


def authenticate(engine, run_id, role, hid, result):
    from .implementation_alignment_evidence import _delivery
    from .intelligence import parse_json
    frozen = engine.store.get('handoff_inputs', hid, {})
    from .implementation_document_review_scope import authenticate_read_authority
    authenticate_read_authority(frozen['context'])
    parse_json(json.dumps(result, ensure_ascii=False), frozen['schema'])
    return _delivery(engine, run_id, role, hid, result)


def check_packet(engine, run_id, packet):
    if 'report_amendment' in packet:
        from .implementation_report_amendments import validate_packet
        return validate_packet(engine, run_id, packet)
    if 'citation_correction' in packet:
        from .implementation_acceptance_citations import validate_packet
        return validate_packet(engine, run_id, packet)
    context = authenticate(engine, run_id, packet['role'], packet['handoff_id'], packet['result'])
    if context != packet['context']:
        policy.fail('阶段证据与冻结输入不同。')
    from .implementation_delivery_correction import REPAIR, validate_repair
    if REPAIR in context:
        validate_repair(engine, run_id, packet)
    return context


def snapshot_check(validator):
    """Synchronous boundary only: never retain proofs across a write or await."""
    @wraps(validator)
    def checked(engine, *args, **kwargs):
        return _source_snapshot(engine, validator, *args, **kwargs)
    return checked


def _source_snapshot(engine, validator, *args, **kwargs):
    """Reuse authenticated delivery operands only within this read transaction."""
    from .architecture_validation import operation
    if getattr(engine.store, 'is_read_snapshot', False):
        return operation(validator)(engine, *args, **kwargs)
    with engine.store.read_snapshot() as store:
        reader = copy.copy(engine)
        reader.store = store
        if getattr(getattr(engine, 'sources', None), 'store', None) is engine.store:
            reader.sources = copy.copy(engine.sources)
            reader.sources.store = store
        return operation(validator)(reader, *args, **kwargs)


def validate_contract(engine, run_id, state, common=None):
    ledger = state[policy.FIELD]
    value = ledger['contract']
    policy.check_seal(value)
    entry = ledger['contracts'][-1]
    context = check_packet(engine, run_id, entry['packet'])
    source = next((d for d in context['indexed_documents'] if d['kind'] == 'architecture_design'), None)
    prior = ledger.get('prior_contract')
    if prior:
        stored = engine.store.get('codex_workflows', prior['workflow_id'], {}).get(policy.FIELD, {}).get('contract')
        if (stored != prior['contract'] or value['previous_sha256'] != stored['sha256']
                or value['version'] != stored['version'] + 1):
            policy.fail('合同变更没有绑定历史版本与变更来源。')
    if not source or common and source != common['architecture_document']:
        policy.fail('批准来源已变化，需要新合同版本与重新规划。')
    expected = policy.contract(entry['packet']['result'], state, source, value['version'],
        reason=value['change_reason'], previous_sha256=value['previous_sha256'])
    if value != expected or entry['contract'] != value or value['change_reason'] != entry['packet']['result']['change_reason']:
        policy.fail('验收合同与原总监交付或冻结科学输入不一致。')
    validate_refs(engine, run_id, entry['packet']['handoff_id'], context, value['source_refs'], current=source)


def author_context(state, index, context):
    ledger = state[policy.FIELD]
    value = {**context, policy.PROTOCOL: 1, 'implementation_indexed_review_protocol': 1,
        'acceptance_contract': copy.deepcopy(ledger['contract']), 'acceptance_budget': ledger['budget'],
        'acceptance_issues': [copy.deepcopy(i) for i in ledger['issues'].values()
                              if i['status'] == 'OPEN' and i['severity'] in {'P0', 'P1'}],
        'previous_specifications': []}
    if index:
        value['implementation_revision_read_batch'] = 1
        previous = next((r for r in state['specifications'] if r['index'] == index - 1), None)
        if not previous or ledger['outcome'] != 'REPAIR' or previous.get('approved') is not False:
            policy.fail('局部修订必须来自完整裁决后的 REPAIR 状态。')
        value['previous_specifications'] = [{k: copy.deepcopy(previous[k]) for k in ('index', 'spec_hash', 'document')}]
        value['indexed_documents'] = [*value['indexed_documents'], previous['document']]
        from .implementation_acceptance_author import PROTOCOL, SUBMISSION_PROTOCOL
        value[PROTOCOL] = 1
        value[SUBMISSION_PROTOCOL] = 1
        from .implementation_issue_output_capacity import PROTOCOL as OUTPUT_PROTOCOL
        value[OUTPUT_PROTOCOL] = 1
        value['implementation_document_locate_protocol'] = 1
        from .implementation_endpoint_gap import WORK, author_items
        work = next((s.get('scoped_supplement', {}).get(WORK) for s in ledger['rounds']
                     if s['revision'] == index - 1), None)
        from .implementation_whole_review import enabled as whole_enabled
        if work and not whole_enabled(state):
            value['implementation_document_authoring'] = {
                **value['implementation_document_authoring'], WORK: copy.deepcopy(work)}
            value['acceptance_issues'].extend(author_items(work))
    from .implementation_whole_review import enabled as whole_enabled, PROTOCOL as WHOLE, POLICY
    if whole_enabled(state):
        value[WHOLE] = 1
        value['implementation_whole_review_author_policy'] = copy.deepcopy(POLICY)
    if index:
        from .implementation_patch_scope import attach
        value = attach(value)
    return value


def submission_schema(base, context):
    if not policy.enabled(context) or not context['implementation_document_authoring']['revision']:
        return base
    result = copy.deepcopy(base)
    result['properties']['patch_reports'] = policy.array(policy.PATCH, 1)
    result['required'].append('patch_reports')
    from .implementation_endpoint_gap import WORK, DIRECTED_EDIT
    if context['implementation_document_authoring'].get(WORK, {}).get('version') == 4:
        result['properties']['patch_reports']['description'] = DIRECTED_EDIT
    from .implementation_acceptance_author import submission_schema as completion_schema
    result = completion_schema(result, context)
    if context['implementation_document_authoring'].get(WORK, {}).get('version') == 4:
        completion = result['properties']['acceptance_completion']
        completion['description'] = completion.get('description', '') + DIRECTED_EDIT
    from .implementation_issue_output_capacity import author_schema
    return author_schema(result, context)


def validate_patch(engine, run_id, row, hid, result, context):
    """An engineer's evidence marks PATCHED only; it can never close an issue."""
    if not policy.enabled(context) or not context['implementation_document_authoring']['revision']:
        return None
    from .implementation_authoring import SUBMISSION
    from .intelligence import parse_json
    parse_json(json.dumps(result, ensure_ascii=False), submission_schema(SUBMISSION, context))
    pending = {i['issue_id']: i for i in context['acceptance_issues']}
    reports = result['patch_reports']
    if len(reports) != len(pending) or {r['issue_id'] for r in reports} != set(pending):
        policy.fail('工程师须逐项回应本轮已裁决问题，不得自增或遗漏。')
    workflow = engine.store.get('codex_workflows', context['implementation_document_authoring']['workflow_id'])
    from .implementation_endpoint_gap import WORK, author_items
    if WORK in context['implementation_document_authoring']:
        # The ordinary submission/recovery path must reauthenticate the old
        # terminal chain, not trust an author's copy of the work item.
        from . import implementation_document_review_scope as scoped
        from .implementation_endpoint_gap import repeated_work
        previous = next(r for r in workflow['specifications'] if r['index'] == row['index'] - 1)
        saved = next(s[scoped.SLOT] for s in workflow[policy.FIELD]['rounds']
                     if s['revision'] == previous['index'])
        scoped.validate_endpoint_followups(engine, run_id, workflow, previous, saved)
        work = repeated_work(saved, engine, run_id)
        if (context['implementation_document_authoring'][WORK] != work or [i for i in context['acceptance_issues']
                if i.get('kind') == 'pending_evidence'] != author_items(work)):
            policy.fail('Author evidence work differs from authenticated predecessor')
    previous = next(r for r in workflow['specifications'] if r['index'] == row['index'] - 1)
    before = previous['spec']
    from .implementation_patch_scope import diagnostic, allowed_fields
    allowed = allowed_fields(context)
    scope = diagnostic(before, row['spec'], reports, allowed)
    from .implementation_acceptance_author import validate as validate_completion, enabled as completion_enabled
    # An authenticated intermediate draft is not a submission. Let the existing
    # author continuation handle unfinished work even when its cumulative report
    # is incomplete. Invalid/out-of-scope claims still fail closed; a completed
    # self-check must pass the full report and reading gates below.
    completion_checked = completion_enabled(context) and not (scope['invalid_paths'] or scope['forbidden_fields'])
    if completion_checked:
        validate_completion(engine, run_id, row, hid, result, context)
    if not scope['valid']:
        details = []
        for key, label in [('invalid_paths', '无效路径'), ('unchanged_claims', '声明但未修改'),
                           ('unreported_paths', '未申报的修改'), ('forbidden_fields', '超出问题依赖范围')]:
            if scope[key]:
                details.append(label + ': ' + ', '.join(scope[key][:12]))
        from .implementation_acceptance_author import PatchScopeRevision
        raise PatchScopeRevision(context, previous, row, scope,
            '实现文档验收：实际修改与 patch_reports 不一致；' + '；'.join(details) + '。')
    if completion_checked and WORK in context['implementation_document_authoring']:
        from .implementation_endpoint_gap import validate_directed_edit
        validate_directed_edit(work, before, row['spec'], reports)
    current_context = {**context, 'indexed_documents': [*context['indexed_documents'], row['document']]}
    for report in reports:
        validate_refs(engine, run_id, hid, current_context, report['evidence'], current=row['document'])
    # Never impose a new author format on an already indexed historical version.
    published = any(r.get('index') == row['index'] and r.get('spec_hash') == row['spec_hash']
                    and r.get('authoring_handoff_id') == hid for r in workflow.get('specifications', []))
    if not completion_checked and (completion_enabled(context) or not published):
        validate_completion(engine, run_id, row, hid, result, context)
    return copy.deepcopy(reports)


def _check_scoped_assertion_evidence(current, scope, checks, *, criterion_id=None):
    """Return every missing assigned assertion to its author, without changing it.

    Group exact frozen offsets by criterion and section so a correction does not
    have to guess which endpoint caused a generic first-failure rejection.
    This is a diagnostic, never a read receipt or a substitute review response.
    """
    sections = {s['id']: s for s in current['sections']}
    missing = []
    for check in checks:
        if check['status'] != 'PASS':
            continue
        groups = {}
        for span in scope['required_sections'][check['criterion_id']]:
            if not any(ref['document_id'] == current['document_id']
                       and ref['document_sha256'] == current['sha256']
                       and ref['section_id'] == span['id']
                       and sections[span['id']]['start'] + ref['offset'] < span['end']
                       and sections[span['id']]['start'] + ref['offset'] + ref['limit'] > span['start']
                       for ref in check['evidence']):
                groups.setdefault(span['id'], []).append([
                    span['start'] - sections[span['id']]['start'], span['end'] - span['start']])
        if groups:
            missing.append({'criterion_id': check['criterion_id'], 'sections': [
                {'section_id': sid, 'offset_limit_pairs': [list(pair) for pair in sorted(set(map(tuple, spans)))]}
                for sid, spans in sorted(groups.items())]})
    if missing and (criterion_id is None or any(item['criterion_id'] == criterion_id for item in missing)):
        policy.delivery_error('Scoped PASS lacks evidence at its assigned assertion or endpoint',
            document_id=current['document_id'], document_sha256=current['sha256'],
            missing_assertions=missing,
            instruction='逐项核对缺失断言，保留已有有效引用；按本交接真实读取与交付的原文补齐证据。'
                '范围按章节内 offset/limit 给出；不得以诊断代替原文或把缺证据计为 PASS。')


@snapshot_check
def validate_report(engine, run_id, row, packet):
    # Store-owned memoization expires with this exact SQLite snapshot, even
    # when the caller has an outer architecture validation operation. Full
    # argument identity prevents edited contexts/proofs from borrowing credit.
    return engine.store.memoized_read(
        ('implementation_acceptance_report_v1', run_id, digest([row, packet])),
        lambda: _validate_report(engine, run_id, row, packet))


def _validate_report(engine, run_id, row, packet):
    from .implementation_documents import read_coverage
    context = check_packet(engine, run_id, packet)
    from .implementation_read_window_recovery import validate_source
    validate_source(engine, run_id, row, packet)
    from .implementation_acceptance_read_recovery import validate_source as validate_read_source
    validate_read_source(engine, run_id, row, packet)
    from .implementation_source_read_recovery import validate_source as validate_source_read
    validate_source_read(engine, run_id, row, packet)
    report = packet['result']
    scope = context[policy.CONTEXT]
    checks = report['checks']
    if len(checks) != len(scope['assigned_criteria']) or {c['criterion_id'] for c in checks} != set(scope['assigned_criteria']):
        policy.delivery_error('每个分配验收项必须唯一返回 PASS、FAIL 或 UNKNOWN。')
    current = row['document']
    missing_sources, missing_reading = [], []
    known = {i['issue_id'] for i in scope['issues']} | {i['local_id'] for i in report['issues']}
    for check in checks:
        if not set(check['issue_ids']) <= known:
            policy.delivery_error('FAIL 引用了不存在的问题。')
        if check['evidence']:
            validate_refs(engine, run_id, packet['handoff_id'], context, check['evidence'], current=current,
                          field='checks[' + check['criterion_id'] + '].evidence', report_evidence=True)
        if context.get('implementation_document_review_scope') and check['status'] == 'PASS':
            # Keep the historical first-failure order: this criterion's source
            # identity/read/delivery checks precede its assertion coverage check.
            _check_scoped_assertion_evidence(current, scope, checks, criterion_id=check['criterion_id'])
        if check['status'] == 'PASS':
            sources = {d['document_id'] for d in context['indexed_documents'] if d['kind'] == 'architecture_design'}
            if not any(r['document_id'] in sources for r in check['evidence']):
                missing_sources.append(check['criterion_id'])
            required = [{'result': {'section_id': s['id'], 'char_start': s['start'], 'char_end': s['end']}}
                        for s in scope['required_sections'][check['criterion_id']]]
            if not read_coverage(engine, run_id, current, required, packet['handoff_id']):
                from .implementation_acceptance_reading import gaps
                missing_reading.append(gaps(engine, run_id, row, packet, check['criterion_id']))
    for issue in report['issues']:
        validate_refs(engine, run_id, packet['handoff_id'], context, issue['evidence'], current=current,
                      field='issues[' + issue['local_id'] + '].evidence', report_evidence=True)
    expected = {i['issue_id'] for i in scope['issues'] if i['verifier'] == packet['role'] and i['status'] == 'PATCHED'}
    if len(report['verifications']) != len(expected) or {v['issue_id'] for v in report['verifications']} != expected:
        policy.delivery_error('定向验证必须唯一覆盖指定待核验问题。')
    for verification in report['verifications']:
        for condition in verification['conditions']:
            if condition['evidence']:
                validate_refs(engine, run_id, packet['handoff_id'], context, condition['evidence'], current=current,
                    field=f'verifications[{verification["issue_id"]}].conditions[{condition["index"]}].evidence', report_evidence=True)
    global_check = context.get('implementation_document_review_scope', {}).get('stage', 'global') == 'global'
    if packet['role'] == 'director' and global_check:
        if len(report['consistency']) != len(policy.CONSISTENCY) or {c['id'] for c in report['consistency']} != set(policy.CONSISTENCY):
            policy.delivery_error('发布检查必须覆盖预先定义的五项一致性。')
    elif report['consistency']:
        policy.delivery_error('一致性发布检查由总监负责。')
    for item in report['consistency']:
        if item['evidence']:
            validate_refs(engine, run_id, packet['handoff_id'], context, item['evidence'], current=current,
                          field='consistency[' + item['id'] + '].evidence', report_evidence=True)
    if missing_reading:
        from .implementation_acceptance_reading import reject
        reject(packet, missing_reading)
    if missing_sources:
        # Finish the other evidence gates first. A missing citation must never
        # mask unread modules, invalid ranges, wrong identities or scope errors.
        from .engine import ResearchBlocked
        error = ResearchBlocked('实现文档验收：' + '、'.join(missing_sources)
                                + ' 的 PASS 缺少本项批准来源引用；原报告和已读证据保留。')
        error.acceptance_source_reference_error = {'version': 1,
            'kind': 'missing_approved_source_references', 'criteria': missing_sources}
        raise error
    if context.get('implementation_document_review_scope'):
        from .implementation_document_review_scope import validate_unit_proofs
        validate_unit_proofs(engine, run_id, row, packet)


def record_decisions(engine, run_id, ledger, packet):
    recorded = {d['decision_id'] for d in ledger['decision_records']}
    for item in packet['result']['decision_records']:
        if item['chosen'] not in item['alternatives'] or item['decision_id'] in recorded:
            policy.delivery_error('决策记录 ' + item['decision_id'] + ' 必须使用唯一 ID，并令 chosen 精确等于 alternatives 中的一项。')
        recorded.add(item['decision_id'])
        validate_refs(engine, run_id, packet['handoff_id'], packet['context'], item['evidence'])
    for item in packet['result']['decision_records']:
        ledger['decision_records'].append({**copy.deepcopy(item), 'handoff_id': packet['handoff_id']})


def report_context(state, row, common, role, assigned):
    ledger = state[policy.FIELD]
    from .experiment_materials import carry
    context = carry(common, base_context(state, [*common['indexed_documents'], row['document']], row))
    from .implementation_acceptance_reading import PLAN
    context[PLAN] = 1
    context[policy.CONTEXT] = {'phase': ledger['phase'], 'contract': ledger['contract'],
        'assigned_criteria': assigned,
        'required_sections': {cid: policy.required_sections(row, ledger['contract'], [cid]) for cid in assigned},
        'issues': [copy.deepcopy(i) for i in ledger['issues'].values()
                   if i['status'] in policy.ACTIVE_ISSUES and (i['verifier'] == role or role == 'engineer')],
        'document': row['document'], 'document_revision': row['index'], 'spec_hash': row['spec_hash'],
        'self_check': role == 'engineer',
        'patch_reports': row.get('acceptance_patch_reports', []),
        'closed_history': [{k: copy.deepcopy(i[k]) for k in ('issue_id', 'criterion_id', 'claim', 'close_when',
            'status', 'evidence', 'document_version')} for i in ledger['issues'].values()
            if i['status'] not in policy.ACTIVE_ISSUES and (i['criterion_id'] in assigned or i['verifier'] == role)],
        'decision_records': [copy.deepcopy(d) for d in ledger['decision_records'] if d['criterion_id'] in assigned]}
    from .experiment_materials import acceptance_sources
    original = acceptance_sources(context, assigned)
    if original is not None:
        context[policy.CONTEXT]['original_source_locations'] = original
    if row['index']:
        previous = next(r for r in state['specifications'] if r['index'] == row['index'] - 1)
        context[policy.CONTEXT]['changes'] = [{'field': field, 'before_sha256': digest(previous['spec'].get(field)),
            'after_sha256': digest(row['spec'].get(field)), 'before': previous['spec'].get(field), 'after': row['spec'].get(field)}
            for field in set(previous['spec']) | set(row['spec']) if previous['spec'].get(field) != row['spec'].get(field)]
        # Exact old/new ranges remain available on demand. Do not inline long diffs.
        context[policy.CONTEXT]['changes'] = [{k: v for k, v in diff.items() if k not in {'before', 'after'}}
                                             for diff in context[policy.CONTEXT]['changes']]
        context['indexed_documents'].append(previous['document'])
        context[policy.CONTEXT]['previous_document'] = previous['document']
        slot = next(r for r in ledger['rounds'] if r['revision'] == row['index'])
        if slot.get('diff_document'):
            context['indexed_documents'].append(slot['diff_document'])
            context[policy.CONTEXT]['diff_document'] = slot['diff_document']
    from .implementation_read_window_recovery import opt_in
    from .implementation_review_reads import KEY as READ_BATCH
    context[READ_BATCH] = 1
    return opt_in(context)


def stop(state, persist):
    from .implementation_failures import ImplementationSpecificationBlocked
    ledger = state[policy.FIELD]
    reason = '实现文档审核已结束：' + ledger['outcome'] + '；当前版本未获准进入代码实现。'
    state.update(status='blocked', blocked_kind='codex_spec_rejected', reason=reason)
    persist()
    raise ImplementationSpecificationBlocked(reason, kind='codex_spec_rejected',
        details={'workflow_id': state['id'], 'review_outcome': ledger['outcome'], 'scientific_approval': False})


@snapshot_check
def validate_release(engine, run_id, state, row):
    """Reauthenticate evidence at code dispatch and again at experiment dispatch."""
    from . import implementation_whole_review as whole
    if whole.enabled(state) and row.get(whole.PROTOCOL) == 1:
        return whole.validate_release(engine, run_id, state, row)
    from .implementation_authoring import validate_submission
    ledger = state[policy.FIELD]
    validate_contract(engine, run_id, state)
    if (ledger.get('release_sha256') != digest({k: v for k, v in ledger.items() if k != 'release_sha256'})
            or ledger.get('outcome') not in policy.APPROVED or not row.get('approved')
            or row.get('acceptance_contract_sha256') != ledger['contract']['sha256']
            or ledger.get('binding') != {'spec_hash': row['spec_hash'], 'document_sha256': row['document']['sha256'], 'revision': row['index']}
            or policy.outcome(ledger, row) != ledger['outcome']):
        policy.fail('缺少当前有效合同、全部验收证据或确定性发布结论。')
    replay_ledger(engine, run_id, state)
    return {'outcome': ledger['outcome'], 'document_sha256': row['document']['sha256'],
            'contract_sha256': ledger['contract']['sha256'], 'scientific_approval': False}


def validate_repair_previous(engine, run_id, state, revision, *, author_context=None):
    """A save continuation uses its actual protocol's authenticated review."""
    from . import implementation_whole_review as whole
    if whole.enabled(state):
        if author_context is not None and not whole.request_enabled(author_context):
            return whole.historical_previous(engine, run_id, state, revision)
        return whole.validate_previous(engine, run_id, state, revision)
    ledger = state[policy.FIELD]
    rows = [row for row in state.get('specifications', []) if row.get('index') == revision - 1]
    if (len(rows) != 1 or rows[0].get('approved') is not False or ledger.get('outcome') != 'REPAIR'
            or ledger.get('binding') != {'spec_hash': rows[0]['spec_hash'],
                'document_sha256': rows[0]['document']['sha256'], 'revision': rows[0]['index']}
            or policy.outcome(ledger, rows[0]) != 'REPAIR'):
        policy.fail('保存续接缺少紧邻前版的完整修订裁决。')
    validate_contract(engine, run_id, state)
    replay_ledger(engine, run_id, state)
    return rows[0]


@snapshot_check
def replay_ledger(engine, run_id, state, *, compare=True):
    from .implementation_authoring import validate_submission
    ledger = state[policy.FIELD]
    replay = {'contract': ledger['contract'], 'budget': ledger['budget'], 'coverage': {},
              'issues': {}, 'events': [], 'consistency': [], 'decision_records': []}
    for slot in ledger['rounds']:
        version = next(r for r in state['specifications'] if r['index'] == slot['revision'])
        validate_submission(engine, run_id, state, version)
        if not slot['complete']:
            policy.fail('历史审核轮尚未完整交付。')
        invalidated = policy.bind_document(replay, version)
        if invalidated != slot['invalidated']:
            policy.fail('模块失效范围与真实版本依赖不同。')
        for report in version.get('acceptance_patch_reports', []):
            from .implementation_endpoint_gap import WORK, author_items
            prior_work = next((s.get('scoped_supplement', {}).get(WORK) for s in ledger['rounds']
                               if s['revision'] == version['index'] - 1), None)
            if prior_work and report['issue_id'] in {i['issue_id'] for i in author_items(prior_work)}:
                continue
            issue = replay['issues'][report['issue_id']]
            issue['patch'] = {'handoff_id': version['authoring_handoff_id'], **report}
            policy.transition(replay, issue, 'PATCHED', actor='engineer', evidence=report['evidence'])
        if slot.get('scoped_supplement'):
            from .implementation_document_review_scope import replay_round
            replay_round(engine, run_id, state, version, slot, replay)
            continue
        proposed = []
        for packet in slot['reports']:
            validate_report(engine, run_id, version, packet)
            policy.apply_report(replay, version, packet['role'], packet['result'], packet['handoff_id'],
                                self_check=packet['role'] == 'engineer')
            proposed.extend(policy.propose(replay, state['id'], version, packet['role'], packet['result'], packet['handoff_id']))
        if proposed != slot['proposed_ids']:
            policy.fail('问题台账与原始报告身份不同。')
        if proposed:
            from . import implementation_adjudication
            implementation_adjudication.replay(engine, run_id, state, version, slot, replay)
        for packet in slot.get('rechecks', []):
            validate_report(engine, run_id, version, packet)
            policy.apply_report(replay, version, packet['role'], packet['result'], packet['handoff_id'])
            policy.propose(replay, state['id'] + ':recheck', version, packet['role'], packet['result'], packet['handoff_id'])
    if compare and any(replay[key] != ledger[key] for key in ('coverage', 'issues', 'events', 'consistency', 'binding', 'decision_records')):
        policy.fail('验收覆盖、问题关闭或事件记录无法由原始交付重建。')
    return replay
