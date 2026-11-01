"""Production orchestration for contract / discovery / repair / verification."""
from __future__ import annotations

import copy
import json

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


def validate_refs(engine, run_id, hid, context, refs, *, current=None, field='evidence'):
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
    required = _required_ranges(engine, run_id, refs, {did: d['sha256'] for did, d in documents.items()})
    for did, spans in required.items():
        if not read_coverage(engine, run_id, documents[did], spans, hid):
            policy.fail('证据范围缺少本交接真实读取与交付记录。')


def authenticate(engine, run_id, role, hid, result):
    from .implementation_alignment_evidence import _delivery
    from .intelligence import parse_json
    frozen = engine.store.get('handoff_inputs', hid, {})
    parse_json(json.dumps(result, ensure_ascii=False), frozen['schema'])
    return _delivery(engine, run_id, role, hid, result)


async def ask(engine, h, run_id, state, purpose, role, instruction, context, schema):
    from .codex_workflow import _frozen_spec_assignment
    frozen = _frozen_spec_assignment(engine, run_id, purpose, role)
    if frozen:
        if not policy.enabled(frozen['context']):
            policy.fail('不能改写历史冻结请求的协议。')
        instruction, context, schema = (frozen[k] for k in ('instruction', 'context', 'schema'))
    payload = {'instruction': instruction, 'context': context, 'schema': schema}
    hid = h.decision_key(run_id, role, purpose, **payload)
    # A retained decision is authenticated without charging a second model call.
    if not engine.store.get('decisions', hid):
        policy.budget_guard(engine.store, run_id, context)
    result = await h.ask(run_id, role, purpose, **payload)
    engine.check_active(run_id)
    await engine._read_consensus_check(run_id, authenticate, engine, run_id, role, hid, result)
    return {'handoff_id': hid, 'role': role, 'result': copy.deepcopy(result),
            'context': copy.deepcopy(engine.store.get('handoff_inputs', hid)['context'])}


def check_packet(engine, run_id, packet):
    if 'citation_correction' in packet:
        from .implementation_acceptance_citations import validate_packet
        return validate_packet(engine, run_id, packet)
    context = authenticate(engine, run_id, packet['role'], packet['handoff_id'], packet['result'])
    if context != packet['context']:
        policy.fail('阶段证据与冻结输入不同。')
    return context


def _source_snapshot(engine, validator, *args, **kwargs):
    """Reuse authenticated delivery operands only within this read transaction."""
    from .architecture_validation import operation
    with engine.store.read_snapshot() as store:
        reader = copy.copy(engine)
        reader.store = store
        if getattr(getattr(engine, 'sources', None), 'store', None) is engine.store:
            reader.sources = copy.copy(engine.sources)
            reader.sources.store = store
        return operation(validator)(reader, *args, **kwargs)


async def ensure_contract(engine, h, run_id, state, common, persist):
    ledger = state[policy.FIELD]
    if ledger.get('contract'):
        await engine._read_consensus_check(run_id, _source_snapshot, engine,
            validate_contract, run_id, state, common)
        engine.check_active(run_id)
        return
    source = common['architecture_document']
    if 'prior_contract' not in ledger:
        with engine.store._read_db() as db:
            prior = db.execute("SELECT key,json_extract(data,'$.acceptance_review.contract') FROM records "
                "WHERE collection='codex_workflows' AND json_extract(data,'$.run_id')=? AND key!=? "
                "AND json_type(data,'$.acceptance_review.contract')='object' "
                "ORDER BY json_extract(data,'$.acceptance_review.contract.version') DESC,updated_at DESC LIMIT 1",
                (run_id, state['id'])).fetchone()
        ledger['prior_contract'] = {'workflow_id': prior[0], 'contract': json.loads(prior[1])} if prior else None
        persist()
    prior = ledger['prior_contract']
    version = prior['contract']['version'] + 1 if prior else 1
    context = base_context(state, common['indexed_documents'])
    context.update(objective=engine.store.get('runs', run_id)['objective'],
        acceptance_criteria=policy.CRITERIA, non_waivable=policy.HARD_CONSTRAINTS,
        acceptance_budget=ledger['budget'], prior_contract=copy.deepcopy(prior))
    packet = await ask(engine, h, run_id, state,
        'implementation_acceptance_contract:' + digest(state['id']) + ':' + str(version), 'director',
        '在编写实现文档前冻结本轮验收合同。读取批准来源；给 C01-C06 分别写明确、必要且文档阶段可满足的要求与证据。'
        '不能增加实验成功要求。列目标、范围和非目标。角色、不可豁免条件和预算由宿主锁定。source_refs 引用真实已读批准架构。'
        'change_reason 说明初次冻结或此次变更的原因；存在 prior_contract 时明确说明变化与受影响范围。',
        context, policy.CONTRACT_SCHEMA)
    await engine._read_consensus_check(run_id, _source_snapshot, engine, validate_refs, run_id,
        packet['handoff_id'], packet['context'], packet['result']['source_refs'], current=source)
    engine.check_active(run_id)
    value = policy.contract(packet['result'], state, source, version,
        reason=packet['result']['change_reason'], previous_sha256=prior['contract']['sha256'] if prior else None)
    ledger['contract'] = value
    ledger['contracts'].append({'contract': copy.deepcopy(value), 'packet': packet})
    ledger['phase'] = 'AUTHOR'
    persist()


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
        previous = next((r for r in state['specifications'] if r['index'] == index - 1), None)
        if not previous or ledger['outcome'] != 'REPAIR' or previous.get('approved') is not False:
            policy.fail('局部修订必须来自完整裁决后的 REPAIR 状态。')
        value['previous_specifications'] = [{k: copy.deepcopy(previous[k]) for k in ('index', 'spec_hash', 'document')}]
        value['indexed_documents'] = [*value['indexed_documents'], previous['document']]
        from .implementation_acceptance_author import PROTOCOL
        value[PROTOCOL] = 1
        value['implementation_document_locate_protocol'] = 1
    return value


def submission_schema(base, context):
    if not policy.enabled(context) or not context['implementation_document_authoring']['revision']:
        return base
    result = copy.deepcopy(base)
    result['properties']['patch_reports'] = policy.array(policy.PATCH, 1)
    result['required'].append('patch_reports')
    from .implementation_acceptance_author import submission_schema as completion_schema
    return completion_schema(result, context)


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
    before = next(r for r in workflow['specifications'] if r['index'] == row['index'] - 1)['spec']
    allowed = set().union(*(policy.criterion_fields(context['acceptance_contract'], i['criterion_id']) for i in pending.values()))
    from .implementation_patch_scope import diagnostic
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
        policy.fail('实际修改与 patch_reports 不一致；' + '；'.join(details) + '。')
    current_context = {**context, 'indexed_documents': [*context['indexed_documents'], row['document']]}
    for report in reports:
        validate_refs(engine, run_id, hid, current_context, report['evidence'], current=row['document'])
    # Never impose a new author format on an already indexed historical version.
    published = any(r.get('index') == row['index'] and r.get('spec_hash') == row['spec_hash']
                    and r.get('authoring_handoff_id') == hid for r in workflow.get('specifications', []))
    if not completion_checked and (completion_enabled(context) or not published):
        validate_completion(engine, run_id, row, hid, result, context)
    return copy.deepcopy(reports)


def validate_report(engine, run_id, row, packet):
    from .implementation_documents import read_coverage
    context = check_packet(engine, run_id, packet)
    from .implementation_read_window_recovery import validate_source
    validate_source(engine, run_id, row, packet)
    from .implementation_acceptance_read_recovery import validate_source as validate_read_source
    validate_read_source(engine, run_id, row, packet)
    report = packet['result']
    scope = context[policy.CONTEXT]
    checks = report['checks']
    if len(checks) != len(scope['assigned_criteria']) or {c['criterion_id'] for c in checks} != set(scope['assigned_criteria']):
        policy.fail('每个分配验收项必须唯一返回 PASS、FAIL 或 UNKNOWN。')
    current = row['document']
    missing_sources, missing_reading = [], []
    known = {i['issue_id'] for i in scope['issues']} | {i['local_id'] for i in report['issues']}
    for check in checks:
        if not set(check['issue_ids']) <= known:
            policy.fail('FAIL 引用了不存在的问题。')
        if check['evidence']:
            validate_refs(engine, run_id, packet['handoff_id'], context, check['evidence'], current=current,
                          field='checks[' + check['criterion_id'] + '].evidence')
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
                      field='issues[' + issue['local_id'] + '].evidence')
    expected = {i['issue_id'] for i in scope['issues'] if i['verifier'] == packet['role'] and i['status'] == 'PATCHED'}
    if len(report['verifications']) != len(expected) or {v['issue_id'] for v in report['verifications']} != expected:
        policy.fail('定向验证必须唯一覆盖指定待核验问题。')
    for verification in report['verifications']:
        for condition in verification['conditions']:
            if condition['evidence']:
                validate_refs(engine, run_id, packet['handoff_id'], context, condition['evidence'], current=current,
                    field=f'verifications[{verification["issue_id"]}].conditions[{condition["index"]}].evidence')
    if packet['role'] == 'director':
        if len(report['consistency']) != len(policy.CONSISTENCY) or {c['id'] for c in report['consistency']} != set(policy.CONSISTENCY):
            policy.fail('发布检查必须覆盖预先定义的五项一致性。')
    elif report['consistency']:
        policy.fail('一致性发布检查由总监负责。')
    for item in report['consistency']:
        if item['evidence']:
            validate_refs(engine, run_id, packet['handoff_id'], context, item['evidence'], current=current,
                          field='consistency[' + item['id'] + '].evidence')
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


def record_decisions(engine, run_id, ledger, packet):
    for item in packet['result']['decision_records']:
        if item['chosen'] not in item['alternatives'] or any(d['decision_id'] == item['decision_id'] for d in ledger['decision_records']):
            policy.fail('决策记录必须使用唯一 ID 并选择明确列出的方案。')
        validate_refs(engine, run_id, packet['handoff_id'], packet['context'], item['evidence'])
        ledger['decision_records'].append({**copy.deepcopy(item), 'handoff_id': packet['handoff_id']})


def report_context(state, row, common, role, assigned):
    ledger = state[policy.FIELD]
    context = base_context(state, [*common['indexed_documents'], row['document']], row)
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
    return opt_in(context)


async def review(engine, h, run_id, state, row, common, persist):
    ledger = state[policy.FIELD]
    if ledger.get('outcome') in policy.TERMINAL:
        if ledger['outcome'] in policy.APPROVED:
            validate_release(engine, run_id, state, row)
            return
        stop(state, persist)
    slot = next((r for r in ledger['rounds'] if r['revision'] == row['index']), None)
    if slot is None:
        invalid = policy.bind_document(ledger, row)
        for report in row.get('acceptance_patch_reports', []):
            issue = ledger['issues'][report['issue_id']]
            if issue['status'] != 'OPEN':
                policy.fail('修复只能回应已裁决 OPEN 问题。')
            issue['patch'] = {'handoff_id': row['authoring_handoff_id'], **report}
            policy.transition(ledger, issue, 'PATCHED', actor='engineer', evidence=report['evidence'])
        slot = {'revision': row['index'], 'invalidated': invalid, 'reports': [], 'proposed_ids': [],
                'processed': False, 'complete': False}
        if row['index']:
            import difflib
            from .implementation_documents import _save
            previous = next(r for r in state['specifications'] if r['index'] == row['index'] - 1)
            before = json.dumps(previous['spec'], ensure_ascii=False, sort_keys=True, indent=2).splitlines()
            after = json.dumps(row['spec'], ensure_ascii=False, sort_keys=True, indent=2).splitlines()
            diff = '\n'.join(difflib.unified_diff(before, after, fromfile=previous['spec_hash'], tofile=row['spec_hash'], lineterm=''))
            text = '# 实现文档局部差异\n\n宿主按不可变结构化版本计算；差异不代替原文和独立验证。\n\n```diff\n' + diff + '\n```\n'
            slot['diff_document'] = _save(engine, h, run_id,
                f'cycle-{state["cycle"]}/implementation/diffs/{digest(state["id"])[:16]}-{row["index"]}.md',
                text, 'implementation_revision_diff')
        ledger['rounds'].append(slot)
        persist()
    if not slot['processed']:
        roles = ['engineer', 'director', 'scout'] if row['index'] == 0 else ['director', 'scout']
        for role in roles:
            assigned = ([c['id'] for c in policy.CRITERIA if c['id'] in {'C03', 'C05', 'C06'}]
                if role == 'engineer' else [c['id'] for c in ledger['contract']['criteria'] if c['reviewer'] == role
                    and (c['id'] in slot['invalidated'] or ledger['coverage'].get(c['id'], {}).get('status') != 'PASS')])
            has_verification = any(i['verifier'] == role and i['status'] == 'PATCHED' for i in ledger['issues'].values())
            if not assigned and not has_verification and role != 'director':
                continue
            from .implementation_read_window_recovery import recover, GUIDANCE as READING_GUIDANCE
            existing = next((p for p in slot['reports'] if p['role'] == role), None)
            if existing:
                packet = await recover(engine, h, run_id, state, row, existing)
                if packet['handoff_id'] != existing['handoff_id']:
                    slot.setdefault('reading_recovery_sources', []).append(copy.deepcopy(existing))
                if packet != existing:
                    slot['reports'][slot['reports'].index(existing)] = packet
                    persist()
                continue
            context = report_context(state, row, common, role, assigned)
            packet = await ask(engine, h, run_id, state,
                f'implementation_acceptance_review:{digest(state["id"])}:{row["index"]}', role,
                policy.GUIDANCE + READING_GUIDANCE + ('\n工程师仅做自检，不能生成独立批准或关闭证据。' if role == 'engineer' else ''),
                context, policy.REPORT_SCHEMA)
            from .implementation_delivery_correction import validate_or_correct
            packet = await validate_or_correct(engine, h, run_id, state, row, packet)
            original = packet
            packet = await recover(engine, h, run_id, state, row, packet)
            if packet['handoff_id'] != original['handoff_id']:
                slot.setdefault('reading_recovery_sources', []).append(copy.deepcopy(original))
            slot['reports'].append(packet)
            persist()
        # Commit all role findings together before authoring any next version.
        updated = copy.deepcopy(ledger)
        updated_slot = updated['rounds'][-1]
        for packet in updated_slot['reports']:
            policy.apply_report(updated, row, packet['role'], packet['result'], packet['handoff_id'],
                                self_check=packet['role'] == 'engineer')
            updated_slot['proposed_ids'].extend(policy.propose(updated, state['id'], row, packet['role'],
                                                               packet['result'], packet['handoff_id']))
        updated_slot['processed'] = True
        state[policy.FIELD] = ledger = updated
        slot = ledger['rounds'][-1]
        persist()
    from . import implementation_adjudication
    await implementation_adjudication.adjudicate(engine, h, run_id, state, row, common, persist)
    ledger = state[policy.FIELD]
    slot = ledger['rounds'][-1]
    # A rejected finding is not automatically PASS. Its owner gets one bounded
    # evidence recheck, without forcing the engineer to rewrite correct prose.
    for role in ('director', 'scout'):
        assigned = [c['id'] for c in ledger['contract']['criteria'] if c['reviewer'] == role
            and ledger['coverage'].get(c['id'], {}).get('status') == 'FAIL'
            and not any(i['criterion_id'] == c['id'] and i['severity'] in {'P0', 'P1'}
                        and i['status'] in policy.ACTIVE_ISSUES for i in ledger['issues'].values())]
        if not assigned or any(p['role'] == role for p in slot.get('rechecks', [])):
            continue
        context = report_context(state, row, common, role, assigned)
        context[policy.CONTEXT]['phase'] = 'VERIFY'
        context[policy.CONTEXT]['adjudication'] = (slot.get('adjudication') or {}).get('result')
        packet = await ask(engine, h, run_id, state,
            f'implementation_acceptance_recheck:{digest(state["id"])}:{row["index"]}', role,
            policy.GUIDANCE + '\n原问题已裁决为误报、重复或非阻断；按证据核对这些验收项一次。裁决本身不等于 PASS。',
            context, policy.REPORT_SCHEMA)
        from .implementation_delivery_correction import validate_or_correct
        packet = await validate_or_correct(engine, h, run_id, state, row, packet)
        updated = copy.deepcopy(ledger)
        policy.apply_report(updated, row, role, packet['result'], packet['handoff_id'])
        policy.propose(updated, state['id'] + ':recheck', row, role, packet['result'], packet['handoff_id'])
        updated['rounds'][-1].setdefault('rechecks', []).append(packet)
        state[policy.FIELD] = ledger = updated
        slot = ledger['rounds'][-1]
        persist()
    ledger['outcome'] = policy.outcome(ledger, row)
    ledger['phase'] = ledger['outcome']
    slot['complete'] = True
    row[policy.PROTOCOL] = 1
    row['acceptance_contract_sha256'] = ledger['contract']['sha256']
    row['approved'] = ledger['outcome'] in policy.APPROVED
    row['review_outcome'] = ledger['outcome']
    row['review_votes'] = []  # Self-check is never presented as an independent vote.
    row['review_delivery'] = {'mode': 'acceptance_contract', 'complete': True,
        'collection_complete': True, 'scientific_approval': False}
    ledger['release_sha256'] = digest({k: v for k, v in ledger.items() if k != 'release_sha256'})
    persist()
    engine.store.event(run_id, 'implementation.acceptance_decided', '实现文档阶段结论：' + ledger['outcome'],
        {'workflow_id': state['id'], 'contract_sha256': ledger['contract']['sha256'],
         'document_sha256': row['document']['sha256'], 'outcome': ledger['outcome'],
         'reviewed_versions': len(ledger['rounds']), 'issues_proposed': len(ledger['issues']),
         'duplicate_issues': sum(i['status'] == 'DUPLICATE' for i in ledger['issues'].values()),
         'verified_closed_issues': sum(i['status'] == 'CLOSED' for i in ledger['issues'].values()),
         'project_budget_usage': engine.store.get('implementation_acceptance_budgets', run_id),
         'scientific_approval': False})
    if ledger['outcome'] not in policy.APPROVED | {'REPAIR'}:
        stop(state, persist)


def stop(state, persist):
    from .implementation_failures import ImplementationSpecificationBlocked
    ledger = state[policy.FIELD]
    reason = '实现文档审核已结束：' + ledger['outcome'] + '；当前版本未获准进入代码实现。'
    state.update(status='blocked', blocked_kind='codex_spec_rejected', reason=reason)
    persist()
    raise ImplementationSpecificationBlocked(reason, kind='codex_spec_rejected',
        details={'workflow_id': state['id'], 'review_outcome': ledger['outcome'], 'scientific_approval': False})


def validate_release(engine, run_id, state, row):
    """Reauthenticate evidence at code dispatch and again at experiment dispatch."""
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


def validate_repair_previous(engine, run_id, state, revision):
    """A save continuation uses its actual protocol's authenticated review."""
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


def replay_ledger(engine, run_id, state):
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
            issue = replay['issues'][report['issue_id']]
            issue['patch'] = {'handoff_id': version['authoring_handoff_id'], **report}
            policy.transition(replay, issue, 'PATCHED', actor='engineer', evidence=report['evidence'])
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
    if any(replay[key] != ledger[key] for key in ('coverage', 'issues', 'events', 'consistency', 'binding', 'decision_records')):
        policy.fail('验收覆盖、问题关闭或事件记录无法由原始交付重建。')
    return replay
