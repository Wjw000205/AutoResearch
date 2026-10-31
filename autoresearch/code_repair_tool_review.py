"""Same-reviewer, on-demand evidence completion without rewriting an objection.

Legacy paid inputs stay replayable. A new review only carries issue locations,
the approved source catalog and its own earlier checks, never the source bodies.
"""
from __future__ import annotations

import copy

from . import code_repair_contract as repair

TASK = ('严格按当前批准实现文档核对本清单修改意见。只核对 targets 中的原问题，'
    '不得新增未定义算法、参数、测试门槛或研究任务。原文缺失先用工具定位、补读，不能直接推测。'
    '逐项确认：实现文档的具体位置要求什么、当前代码哪里偏离、fixes 是否仅修复该偏差。'
    '发现文档未定义、冲突或超出批准范围，supported=false 并说明具体条款；不能给 Codex 自创指令。'
    '同一角色 prior_audit 的已支持且未变更问题沿用原结论；他人结论不能代替独立判断。'
    'checks 覆盖全部当前问题，原已支持项逐字保留；只改变 targets 的结论。'
    '每个 supported=true 的 target 在 evidence 中引用本交接的代码读取回执及文档依据回执哈希；'
    '文档优先 documents.recall 复用本角色同版本记忆，命中的已读条款无需再 documents.read。'
    '定位结果、作者自报和旧目录不算读取。code.record_notes 保存简短发现后继续按需读取。'
    '不重读整份文档或整目录，不改代码，不批准实验。未决事项继续保留，不能自动判通过。'
    'approve=true 仅表示修改意见均符合文档，绝不表示代码验收通过。')

SCHEMA = repair.obj({**copy.deepcopy(repair.AUDIT_SCHEMA['properties']),
    'evidence': {'type': 'array', 'maxItems': 90, 'items': repair.obj({
        'issue_id': repair.TEXT, 'document_receipts': {'type': 'array', 'minItems': 1,
            'maxItems': 20, 'uniqueItems': True, 'items': repair.TEXT},
        'code_receipts': {'type': 'array', 'minItems': 1, 'maxItems': 20,
            'uniqueItems': True, 'items': repair.TEXT}})}})


def request(packet, prior=None, memory=None):
    positive = {c['issue_id'] for c in prior['result']['checks'] if c['supported']} if prior else set()
    selected = [i for i in packet['items'] if i['issue_id'] not in positive]
    targets = [{k: copy.deepcopy(i[k]) for k in ('issue_id', 'role', 'issue', 'requirement_id', 'fixes', 'allowed_paths')}
        | {'implementation_locations': list({repair.sha(p): p for e in i['host_evidence']
            for p in e['code_reference']['implementation_refs']}.values())} for i in selected]
    docs = [{k: copy.deepcopy(d[k]) for k in ('document_id', 'sha256', 'total_chars', 'sections')}
            for d in packet['audit_sources']['documents']]
    # Only navigation enters the request. The full packet stays in SQLite.
    view = {'source_packet_sha256': repair.sha(packet), 'spec_hash': packet['spec_hash'],
        'delivery_hash': packet['delivery_hash'], 'targets': targets,
        'all_issue_ids': [i['issue_id'] for i in packet['items']],
        'original_standards': [copy.deepcopy(i) for i in (packet.get('review_scope') or {}).get('issues', [])
            if i['issue_id'] in {t['issue_id'] for t in targets}],
        'deferred_issue_ids': [i['issue_id'] for i in packet.get('deferred_items', [])]}
    if prior:
        view['prior_audit'] = copy.deepcopy(prior)
    if prior and prior.get('tool_review'):
        view['reading_memory'] = copy.deepcopy(memory or {'reads': [], 'notes': []})
        view['continuation'] = ('上一轮已有结论和读取保持有效。剩余 targets 缺少决定性依据，'
            '本轮继续用原工具补齐具体位置，不退回作者修改意见。reading_memory 提供本人已读回执、'
            '位置和笔记，可直接在 evidence 中复用，勿重读已完成范围。需要回查时才再次读取。'
            '补读后确证不符合文档的 target 也须在 evidence 引用对应代码和文档回执，以区别于尚未读全。'
            '可用来源尚未读完时先请求工具；只有实际条款冲突、来源不可用或补读后仍无法裁决时才拒绝。')
    return {'code_repair_protocol': 1, 'code_repair_tool_review': 1, 'indexed_document_context': True,
        'document_index_protocol': 1, 'indexed_documents': docs, 'code_navigation_protocol': 1,
        'packet_sha256': repair.sha(packet), 'audit_view_version': 3,
        'repair_packet_view': view, 'audit_view_sha256': repair.sha(view)}


def semantic(value):
    return {k: copy.deepcopy(value[k]) for k in repair.AUDIT_SCHEMA['properties']}


def validate(engine, run_id, role, hid, packet, context, value, *, ancestry=None):
    from .intelligence import parse_json
    from .code_navigation import read_receipt_ids, read, verify_recall
    from . import document_index
    import json
    parse_json(json.dumps(value, ensure_ascii=False), SCHEMA)
    repair.validate_audit(semantic(value), packet, require_approval=False)
    target_ids = {t['issue_id'] for t in context['repair_packet_view']['targets']}
    prior = context['repair_packet_view'].get('prior_audit')
    if prior:
        authenticate(engine, run_id, packet, prior, seen=ancestry)
        if prior['role'] != role:
            repair.fail('独立核对不能借用其他角色的支持结论。')
        old = {c['issue_id']: c for c in prior['result']['checks'] if c['supported']}
        if any(row != old[row['issue_id']] for row in value['checks'] if row['issue_id'] not in target_ids):
            repair.fail('补读不能改写本角色已支持且未变更的原问题。')
    evidence = {e['issue_id']: e for e in value['evidence']}
    if len(evidence) != len(value['evidence']) or set(evidence) - target_ids:
        repair.fail('补读依据超出原分歧或包含重复问题。')
    from .code_navigation import audit_predecessors
    receipts = {ref: (owner, r) for owner in [hid, *audit_predecessors(engine, run_id, role, context)]
        for r in engine.store.get('handoff_progress', owner, {}).get('tool_results', [])
        if r.get('call', {}).get('name') in {'code.read', 'documents.read', 'documents.recall'}
        and isinstance(r.get('result'), dict) and (r['result'].get('text') or r['result'].get('entries')) and not r['result'].get('error')
        for ref in read_receipt_ids(r)}
    targets = {i['issue_id']: i for i in packet['items']}
    checked = set()
    for check in value['checks']:
        identity = check['issue_id']
        if identity not in target_ids:
            continue
        item = evidence.get(identity)
        if check['supported'] and not item:
            repair.fail('支持修改必须引用本角色实际补读的文档和代码。')
        if not item:
            continue
        for field, tool in (('document_receipts', 'documents.read'), ('code_receipts', 'code.read')):
            for ref in item[field]:
                owner, row = receipts.get(ref, (None, None))
                if not row or row['call']['name'] not in ({tool, 'documents.recall'} if tool == 'documents.read' else {tool}):
                    repair.fail('补读引用没有同一交接的真实读取回执。')
                if tool == 'code.read' and row['result']['path'] not in targets[identity]['allowed_paths']:
                    repair.fail('补读代码不属于原问题允许范围。')
                if ref not in checked:
                    if row['call']['name'] == 'documents.recall':
                        verify_recall(engine, run_id, role, owner, row['call']['arguments'], row['result'])
                        checked.add(ref)
                        continue
                    exact = (read(engine, run_id, role, owner, row['call']['arguments']) if tool == 'code.read' else
                        document_index.read(engine, run_id, row['call']['arguments']))
                    if exact != row['result']:
                        repair.fail('补读回执与冻结原文不一致。')
                    checked.add(ref)


def reading_memory(engine, run_id, role, packet, prior):
    from .code_navigation import audit_predecessors, read_receipt_id
    context = {'packet_sha256': repair.sha(packet), 'repair_packet_view': {'prior_audit': prior}}
    targets = {c['issue_id'] for c in prior['result']['checks'] if not c['supported']}
    paths = {p for i in packet['items'] if i['issue_id'] in targets for p in i['allowed_paths']}
    reads, notes = {}, {}
    for owner in audit_predecessors(engine, run_id, role, context):
        for row in engine.store.get('handoff_progress', owner, {}).get('tool_results', []):
            name, page = row.get('call', {}).get('name'), row.get('result', {})
            if not isinstance(page, dict) or page.get('error'):
                continue
            if name == 'code.record_notes':
                for note in page.get('notes', []):
                    notes.setdefault(note['key'], copy.deepcopy(note))
            elif name in {'code.read', 'documents.read', 'documents.recall'} and (page.get('text') or page.get('entries')):
                if name == 'code.read' and page.get('path') not in paths:
                    continue
                identity = read_receipt_id(row)
                meta = {k: page[k] for k in ('path', 'artifact_id', 'document_id', 'sha256', 'section_id',
                    'offset', 'limit', 'char_start', 'char_end', 'start_line', 'end_line') if k in page}
                if name == 'documents.recall':
                    meta['ranges'] = [{k: e[k] for k in ('document_id', 'sha256', 'section_id', 'char_start', 'char_end')}
                        for e in page['entries']]
                reads.setdefault(identity, {'receipt_sha256': identity, 'handoff_id': owner, 'tool': name,
                    'reopen': copy.deepcopy(row['call']), **meta})
    return {'reads': list(reads.values()), 'notes': list(notes.values()),
        'scope': 'Only this auditor\'s sealed same-version reads. Original bytes stay in their journal; no new read claim.'}


def authenticate(engine, run_id, packet, audit, *, seen=None):
    from .code_repair_amendment import seal
    seen = set(seen or ())
    if audit['handoff_id'] in seen:
        repair.fail('定向补读的审查回执出现循环。')
    seen.add(audit['handoff_id'])
    if not audit.get('tool_review'):
        value = seal(engine, run_id, audit['role'], 'code_repair_audit:' + repair.sha(packet),
            repair.audit_task(packet), repair.audit_context(packet), repair.AUDIT_SCHEMA, audit['handoff_id'])
    else:
        inputs = engine.store.get('handoff_inputs', audit['handoff_id'], {})
        prior = inputs.get('context', {}).get('repair_packet_view', {}).get('prior_audit')
        memory = reading_memory(engine, run_id, audit['role'], packet, prior) if prior and prior.get('tool_review') else None
        context = request(packet, prior, memory)
        value = seal(engine, run_id, audit['role'], 'code_repair_audit:' + repair.sha(packet),
            TASK, context, SCHEMA, audit['handoff_id'])
        validate(engine, run_id, audit['role'], audit['handoff_id'], packet, context, value, ancestry=seen)
        value = semantic(value)
    if value != audit['result']:
        repair.fail('审查结果与已保存原始结论不一致。')
    return value


async def review(engine, h, run_id, role, packet):
    """Use unchanged legacy decisions; supplement only a compact audit's gaps."""
    purpose = 'code_repair_audit:' + repair.sha(packet)
    hid = h.decision_key(run_id, role, purpose, repair.audit_task(packet), repair.audit_context(packet), repair.AUDIT_SCHEMA)
    decision = engine.store.get('decisions', hid, {})
    prior = None
    if decision:
        prior = {'role': role, 'handoff_id': hid, 'result': decision['result']}
        authenticate(engine, run_id, packet, prior)
        repair.validate_audit(prior['result'], packet, require_approval=False)
        if prior['result']['approve'] or not packet.get('audit_revision'):
            return prior
    visited = set()
    while True:
        memory = reading_memory(engine, run_id, role, packet, prior) if prior and prior.get('tool_review') else None
        context = request(packet, prior, memory)
        value = await h.ask(run_id, role, purpose, TASK, context, SCHEMA)
        hid = h.decision_key(run_id, role, purpose, TASK, context, SCHEMA)
        validate(engine, run_id, role, hid, packet, context, value)
        current = {'role': role, 'handoff_id': hid, 'result': semantic(value), 'tool_review': 1}
        unsupported = {r['issue_id'] for r in value['checks'] if not r['supported']}
        unread = unsupported - {e['issue_id'] for e in value['evidence']}
        if not unread:
            return current
        # Missing source reading belongs to this auditor, not to the author of
        # an otherwise unchanged edit. No synthetic support or issue deletion.
        inspected = reading_memory(engine, run_id, role, packet, current)
        mark = repair.sha({'unread': sorted(unread), 'reads': sorted(r['receipt_sha256'] for r in inspected['reads'])})
        if mark in visited:
            repair.fail('原审查者补读未取得新依据；保留原分歧，不能转成修改授权。')
        visited.add(mark)
        engine.check_active(run_id)
        engine.store.event(run_id, 'implementation.repair_reading_continued',
            '原审查者继续补齐缺失证据，保留本人已支持结论和已读记忆。',
            {'role': role, 'packet_sha256': repair.sha(packet), 'handoff_id': hid, 'issue_ids': sorted(unread)})
        prior = current
