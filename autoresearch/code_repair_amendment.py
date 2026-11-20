"""Return semantic audit dissent to the original issue author, then re-audit.

This revises proposed edit instructions, never sealed code-review votes. Every
transition is reconstructible from immutable paid inputs and decisions; only
the disputed issues may change and two independent audits still gate dispatch.
"""
from __future__ import annotations

import copy
import json

from . import code_repair_contract as repair
from .code_repair_audit_view import view, complete_view, rebase_sources, excerpt
from .repair_evidence_scope import code_sources, provided_code_paths
from .async_ownership import to_thread as owned_to_thread

FOLLOWUPS = 'code_repair_read_followups'
HOST_READ_WINDOW = 4000


def host_reading_metadata(reading):
    fields = ('host_evidence_followup', 'host_evidence_requests', 'followup_id', 'followup_sha256')
    if (any(key not in reading for key in fields) or not isinstance(reading['host_evidence_requests'], list)
            or not isinstance(reading['followup_id'], str) or not isinstance(reading['followup_sha256'], str)):
        repair.fail('宿主正文补读的冻结元数据不完整。')
    return {key: copy.deepcopy(reading[key]) for key in fields}


def citation_reading(engine, run_id, role, packet, context, hid, value, paths):
    """Freeze host-selected windows once, before any new author request."""
    binding = {'packet_sha256': repair.sha(packet), 'source_sha256': context['amendment_source_sha256'],
        'handoff_id': hid, 'role': role, 'output_sha256': repair.sha(value)}
    identifier = run_id + ':amendment-read:' + repair.sha(binding)
    def freeze(old):
        if old is not None:
            if (not isinstance(old, dict) or not isinstance(old.get('body'), dict)
                    or old.get('run_id') != run_id or old.get('id') != identifier
                    or old.get('body', {}).get('binding') != binding or old.get('sha256') != repair.sha(old['body'])):
                repair.fail('宿主补读的冻结来源发生变化。')
            return old
        source = context['amendment_source']
        queries = [{'kind': 'code', 'identity': path, 'sha256': source['code_files'][path]['sha256'],
            'offset': 0, 'limit': min(HOST_READ_WINDOW, source['code_files'][path]['total_chars']),
            'reason': '宿主发现原回复引用了仅有目录的文件；向原作者补充冻结正文后重新确认，不追认之前已读。'}
            for path in sorted(paths)[:20]]
        body = {'version': 1, 'binding': binding, 'queries': queries}
        return {'id': identifier, 'run_id': run_id, 'body': body, 'sha256': repair.sha(body)}
    record = engine.store.mutate(FOLLOWUPS, identifier, freeze)
    return {'handoff_id': hid, 'result': copy.deepcopy(value), 'host_evidence_followup': True,
        'host_evidence_requests': copy.deepcopy(record['body']['queries']),
        'followup_id': identifier, 'followup_sha256': record['sha256']}


def verify_citation_reading(engine, run_id, role, packet, context, reading):
    host_reading_metadata(reading)
    record = engine.store.get(FOLLOWUPS, reading.get('followup_id')) or {}
    binding = {'packet_sha256': repair.sha(packet), 'source_sha256': context['amendment_source_sha256'],
        'handoff_id': reading['handoff_id'], 'role': role, 'output_sha256': repair.sha(reading['result'])}
    if not isinstance(record, dict) or not isinstance(record.get('body'), dict):
        repair.fail('宿主正文补读缺少有效的冻结记录。')
    body = record['body']
    if (record.get('id') != run_id + ':amendment-read:' + repair.sha(binding)
            or record.get('run_id') != run_id or body.get('version') != 1 or body.get('binding') != binding
            or record.get('sha256') != repair.sha(body) or reading.get('followup_sha256') != record.get('sha256')
            or reading.get('host_evidence_requests') != body.get('queries')):
        repair.fail('宿主正文补读缺少同角色同版本的冻结记录。')

TASK = '''独立核对发现当前修改意见中的局部分歧。仅处理 targets 中你本人提出的原 issue_id。
这是纠正拟修改意见，不是重新验收代码；其他问题、已完成阶段和原始评审结论不重写。
依据随附的当前冻结原文，纠正与批准条款不符、混入未证实状态或扩大标准的子要求；
保留同一问题中已有依据的修复内容。不得新增问题、拓宽文件范围或改变批准规范。
尤其不能把缺少修复回执的宿主问题宣称为已修复，也不能把不同版本的用例简单叠加。
corrected 必须实际纠正 change/verification，并给出同版本 document_refs 和实际 code_paths。
fixes 的条数、issue_index 和 location 原样保留；只纠正已被质疑的要求，不降低批准文档的标准。
输入只含必要定位和片段。缺少决定性依据时，responses=[]，用 evidence_requests 指定具体原文字符范围，
每次只补读缺失位置；不得申请整份历史。收到补读后继续同一问题。确实不能裁决用 deferred，保留原 fixes。
每个 targets 问题返回一项，不得修改其他问题。本次没有代码批准或实验执行授权。
返回 amendment_source_sha256 必须逐字使用输入值；提交修改意见时 evidence_requests=[]。'''

READ_SCHEMA = {'type': 'array', 'maxItems': 20, 'items': repair.obj({
    'kind': {'enum': ['document', 'code']}, 'identity': repair.TEXT, 'sha256': repair.TEXT,
    'offset': {'type': 'integer', 'minimum': 0}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 4000},
    'reason': repair.TEXT})}

SCHEMA = repair.obj({'amendment_source_sha256': repair.TEXT, 'responses': {
    'type': 'array', 'maxItems': 90, 'items': repair.obj({
        'issue_id': repair.TEXT, 'resolution': {'enum': ['corrected', 'deferred']},
        'reason': repair.TEXT, 'fixes': {'type': 'array', 'minItems': 1, 'maxItems': 90,
            'items': repair.obj({'issue_index': {'type': 'integer', 'minimum': 0},
                'location': repair.TEXT, 'change': repair.TEXT, 'verification': repair.TEXT})},
        'document_refs': {'type': 'array', 'minItems': 1, 'uniqueItems': True,
            'items': repair.obj({'document_id': repair.TEXT, 'sha256': repair.TEXT, 'section_id': repair.TEXT})},
        'code_paths': {'type': 'array', 'minItems': 1, 'uniqueItems': True, 'items': repair.TEXT}})},
    'evidence_requests': READ_SCHEMA})


def request(packet, audit, role, readings=()):
    repair.validate_audit(audit['result'], packet, require_approval=False)
    if audit['role'] not in {'director', 'scout'} or audit['result']['approve'] is not False:
        repair.fail('意见纠正只能由真实独立复审分歧触发。')
    dissent = {row['issue_id']: row['reason'] for row in audit['result']['checks'] if not row['supported']}
    targets = [i for i in packet['items'] if i['role'] == role and i['issue_id'] in dissent]
    if not targets:
        repair.fail('意见纠正未绑定本角色被质疑的原问题。')
    target_ids = {t['issue_id'] for t in targets}
    source = view(packet, target_ids)
    source['targets'] = [{**copy.deepcopy(i), 'audit_reason': dissent[i['issue_id']]}
        for i in source['items'] if i['issue_id'] in {t['issue_id'] for t in targets}]
    source.pop('items')
    source.pop('deferred_items', None)
    source['audit'] = {'role': audit['role'], 'handoff_id': audit['handoff_id'],
        'checks': [r for r in audit['result']['checks'] if r['issue_id'] in target_ids]}
    source['requested_evidence'] = []
    source['reading_receipts'] = []
    for reading in readings:
        context = {'amendment_source': source}
        context['amendment_source_sha256'] = repair.sha(source)
        if reading.get('host_evidence_followup') is True:
            from .implementation_failures import ImplementationAmendmentReadRequired
            try:
                validate(reading['result'], context)
            except ImplementationAmendmentReadRequired as missing:
                from .intelligence import parse_json
                queries = reading.get('host_evidence_requests')
                parse_json(json.dumps(queries, ensure_ascii=False), READ_SCHEMA)
                if ([q['identity'] for q in queries] != missing.paths[:20]
                        or any(q['kind'] != 'code' or q['offset'] != 0 for q in queries)):
                    repair.fail('宿主补读不能扩展原回复的缺失正文范围。')
            else:
                repair.fail('宿主补读必须绑定原回复中真实缺失的代码正文。')
            added = read_ranges(packet, source, queries)
            source['requested_evidence'].extend(added)
            source.setdefault('host_evidence_followups', []).append({
                'handoff_id': reading['handoff_id'], 'output_sha256': repair.sha(reading['result']),
                'paths': [row['identity'] for row in added], 'prior_reading_credit': False,
                'prior_responses': copy.deepcopy(reading['result']['responses']),
                'requires_original_author_confirmation': True})
            continue
        validate(reading['result'], context)
        if not reading['result']['evidence_requests']:
            repair.fail('补读链混入最终意见。')
        added = read_ranges(packet, source, reading['result']['evidence_requests'])
        source['requested_evidence'].extend(added)
        source['reading_receipts'].append({'handoff_id': reading['handoff_id'], 'output_sha256': repair.sha(reading['result'])})
    identity = repair.sha(source)
    return 'code_repair_amend:' + identity, {
        'code_repair_protocol': 1, 'indexed_document_context': True,
        'amendment_source_sha256': identity, 'amendment_source': source}


def read_ranges(packet, source, queries):
    full = complete_view(packet)
    documents = {d['document_id']: d for d in full['documents']}
    result = []
    existing = source.get('requested_evidence', [])
    for query in queries:
        document = query['kind'] == 'document'
        item = documents.get(query['identity']) if document else full['code_files'].get(query['identity'])
        if (not item or item['sha256'] != query['sha256'] or (not document and query['identity'] not in code_sources(packet))
                or not 0 <= query['offset'] < query['offset'] + query['limit'] <= len(item['text'])):
            repair.fail('补读必须指向同一冻结证据集、版本及有效范围。')
        prior_parts = [r for r in [*existing, *result] if all(query[k] == r[k] for k in ('kind', 'identity', 'sha256'))]
        if document:
            prior_parts += [e for d in source['documents'] if d['document_id'] == query['identity'] for e in d['excerpts']]
        else:
            prior_parts += source['code_files'].get(query['identity'], {}).get('excerpts', [])
        cursor = query['offset']
        for part in sorted(prior_parts, key=lambda r: r['offset']):
            if part['offset'] <= cursor:
                cursor = max(cursor, part['offset'] + part['limit'])
        if cursor >= query['offset'] + query['limit']:
            repair.fail('补读未增加新的证据位置，停止重复请求。')
        row = {**query, **excerpt(item['text'], query['offset'], query['limit'])}
        if document:
            row['section_id'] = next(s['id'] for s in item['sections'] if s['start'] <= query['offset'] < s['end'])
        result.append(row)
    return result


def seal(engine, run_id, role, purpose, task, context, schema, hid):
    from .engine import Harness
    expected = Harness.decision_key(run_id, role, purpose, task, context, schema)
    saved = engine.store.get('decisions', hid, {})
    frozen = engine.store.get('handoff_inputs', hid, {})
    hand = engine.store.get('handoffs', hid, {})
    payload = {'instruction': task, 'context': context, 'schema': schema}
    if (hid != expected or saved.get('run_id') != run_id or saved.get('role') != role
            or frozen.get('run_id') != run_id or {k: frozen.get(k) for k in payload} != payload
            or hand.get('status') != 'completed' or hand.get('input_hash') != repair.sha(payload)
            or hand.get('output_hash') != repair.sha(saved.get('result'))):
        repair.fail('意见纠正缺少原角色完整冻结输入或真实完成回执。')
    return saved['result']


def validate(value, context, *, replaying_sealed_revision=False):
    from .intelligence import parse_json
    parse_json(json.dumps(value, ensure_ascii=False), SCHEMA)
    source = context['amendment_source']
    known = {i['issue_id']: i for i in source['targets']}
    rows = value['responses']
    if value['amendment_source_sha256'] != repair.sha(source):
        repair.fail('补读或纠正未绑定原输入。')
    if value['evidence_requests']:
        if rows:
            repair.fail('补读请求不能同时提交未经补查的修改结论。')
        return
    if (value['amendment_source_sha256'] != repair.sha(source)
            or len(rows) != len(known) or {r['issue_id'] for r in rows} != set(known)):
        repair.fail('意见纠正遗漏、新增或未绑定原分歧。')
    documents = {(d['document_id'], d['sha256']): {e['section_id'] for e in d['excerpts']}
                 for d in source.get('documents', [])}
    for entry in source.get('requested_evidence', []):
        if entry['kind'] == 'document':
            documents.setdefault((entry['identity'], entry['sha256']), set()).add(entry['section_id'])
    missing_code = set()
    provided = provided_code_paths(source)
    for row in rows:
        old = known[row['issue_id']]
        if (len(row['fixes']) != len(old['fixes']) or any(
                new[k] != previous[k] for new, previous in zip(row['fixes'], old['fixes'])
                for k in ('issue_index', 'location'))):
            repair.fail('意见纠正不能新增子问题或扩大修改位置。')
        if row['resolution'] == 'deferred':
            if row['fixes'] != old['fixes']:
                repair.fail('待核实问题必须保留原要求，不能暗中改写。')
        elif row['fixes'] == old['fixes']:
            repair.fail('意见纠正未修订被质疑的要求，不能重复同一审查。')
        if any(ref['section_id'] not in documents.get((ref['document_id'], ref['sha256']), set())
               for ref in row['document_refs']):
            repair.fail('意见纠正必须引用当前冻结来源的真实章节。')
        for path in row['code_paths']:
            if path in provided:
                continue
            if (replaying_sealed_revision and path in old['allowed_paths']
                    and path in source.get('code_files', {})):
                # Historical code_paths were location references. Reconstruct
                # an authenticated old revision under that frozen contract;
                # this grants no reading credit and never admits a new reply.
                continue
            if path not in source.get('code_files', {}) or not source['code_files'][path].get('total_chars'):
                repair.fail('意见纠正必须引用同一冻结证据集中实际提供正文的代码。')
            missing_code.add(path)
    if missing_code:
        from .implementation_failures import ImplementationAmendmentReadRequired
        raise ImplementationAmendmentReadRequired(missing_code)


def apply(packet, audit, authors, *, replaying_sealed_revision=False):
    result = copy.deepcopy(packet)
    dissent = {r['issue_id'] for r in audit['result']['checks'] if not r['supported']}
    expected_roles = {i['role'] for i in packet['items'] if i['issue_id'] in dissent}
    if len(authors) != len(expected_roles) or {a['role'] for a in authors} != expected_roles:
        repair.fail('未收齐所有被质疑问题的原作者纠正。')
    responses = {}
    for author in authors:
        _, context = request(packet, audit, author['role'], author.get('readings', []))
        validate(author['result'], context, replaying_sealed_revision=replaying_sealed_revision)
        if author['result']['evidence_requests']:
            repair.fail('补读请求不是修改结论。')
        responses.update({r['issue_id']: {**r, 'read_evidence': context['amendment_source']['requested_evidence']}
                          for r in author['result']['responses']})
    if set(responses) != dissent:
        repair.fail('意见纠正范围与独立审查分歧不一致。')
    retained, deferred = [], list(result.get('deferred_items', []))
    for item in result['items']:
        amendment = responses.get(item['issue_id'])
        if amendment:
            item['fixes'] = copy.deepcopy(amendment['fixes'])
            item['amendment'] = {k: copy.deepcopy(v) for k, v in amendment.items() if k != 'fixes'}
            if amendment['resolution'] == 'deferred':
                item['disposition'] = 'needs_review'
                deferred.append(item)
                continue
        retained.append(item)
    result['items'] = retained
    if deferred:
        result['deferred_items'] = deferred
    rebase_sources(result, packet)
    result['audit_revision'] = {'version': 1, 'base_packet_sha256': repair.sha(packet),
        'audit': {k: copy.deepcopy(audit[k]) for k in ('role', 'handoff_id', 'tool_review') if k in audit},
        'checks': copy.deepcopy(audit['result']['checks']),
        'audit_output_sha256': repair.sha(audit['result']),
        'authors': [{k: copy.deepcopy(author[k]) for k in ('role', 'handoff_id')} |
                    {'output_sha256': repair.sha(author['result']), 'readings': [
                        {'handoff_id': r['handoff_id'], 'output_sha256': repair.sha(r['result']),
                         **(host_reading_metadata(r) if r.get('host_evidence_followup') is True else {})}
                        for r in author.get('readings', [])]} for author in authors]}
    return result


def verify(engine, run_id, packet, seen=None):
    """Reconstruct every revision from sealed records, including on restart."""
    record = packet.get('audit_revision')
    if not record:
        return
    seen = set() if seen is None else seen
    identity = repair.sha(packet)
    if identity in seen or record.get('version') != 1:
        repair.fail('意见纠正的冻结链无效。')
    seen.add(identity)
    audit = record['audit']
    frozen = engine.store.get('handoff_inputs', audit['handoff_id'], {})
    base_context = frozen.get('context', {})
    # Legacy audits contain the original packet; v2 views have an immutable
    # packet record written by prepare before any asynchronous audit.
    base = base_context.get('repair_packet')
    if base is None:
        base = engine.store.get('code_repair_packets', record['base_packet_sha256'], {}).get('packet')
    if not base or repair.sha(base) != record['base_packet_sha256']:
        repair.fail('意见纠正的原修改清单发生变化。')
    verify(engine, run_id, base, seen)
    if audit.get('tool_review'):
        from .code_repair_tool_review import authenticate, semantic
        stored = engine.store.get('decisions', audit['handoff_id'], {}).get('result', {})
        vote = authenticate(engine, run_id, base, {**audit, 'result': semantic(stored)})
    else:
        vote = seal(engine, run_id, audit['role'], 'code_repair_audit:' + repair.sha(base),
            repair.audit_task(base), repair.audit_context(base), repair.AUDIT_SCHEMA, audit['handoff_id'])
    repair.validate_audit(vote, base, require_approval=False)
    if repair.sha(vote) != record['audit_output_sha256'] or all(r['supported'] for r in vote['checks']):
        repair.fail('意见纠正未绑定真实的独立复审分歧。')
    full_audit = {**audit, 'result': vote}
    authors = []
    for author in record['authors']:
        readings = []
        for reading in author.get('readings', []):
            purpose, context = request(base, full_audit, author['role'], readings)
            value = seal(engine, run_id, author['role'], purpose, TASK, context, SCHEMA, reading['handoff_id'])
            if repair.sha(value) != reading['output_sha256']:
                repair.fail('定向补读的原请求发生变化。')
            restored = {'handoff_id': reading['handoff_id'], 'result': value,
                **(host_reading_metadata(reading) if reading.get('host_evidence_followup') is True else {})}
            if restored.get('host_evidence_followup') is True:
                verify_citation_reading(engine, run_id, author['role'], base, context, restored)
            readings.append(restored)
        purpose, context = request(base, full_audit, author['role'], readings)
        response = seal(engine, run_id, author['role'], purpose, TASK, context, SCHEMA, author['handoff_id'])
        if repair.sha(response) != author['output_sha256']:
            repair.fail('原作者纠正结果发生变化。')
        authors.append({**author, 'readings': readings, 'result': response})
    if apply(base, full_audit, authors, replaying_sealed_revision=True) != packet:
        repair.fail('修改清单超出已保存的原作者定向纠正。')


def fingerprint(packet):
    return repair.sha([{k: item[k] for k in ('issue_id', 'fixes', 'disposition')}
        for group in ('items', 'deferred_items') for item in packet.get(group, [])])


def restore(engine, run_id, packet, audit):
    """Replay only completed author work, without requests or followup writes."""
    from .engine import Harness
    from .implementation_failures import ImplementationAmendmentReadRequired
    dissent = {r['issue_id'] for r in audit['result']['checks'] if not r['supported']}
    authors = []
    for role in repair.ROLES:
        if not any(i['role'] == role and i['issue_id'] in dissent for i in packet['items']):
            continue
        readings, seen = [], set()
        while True:
            purpose, context = request(packet, audit, role, readings)
            if purpose in seen:
                repair.fail('已保存的定向补读形成循环。')
            seen.add(purpose)
            hid = Harness.decision_key(run_id, role, purpose, TASK, context, SCHEMA)
            value = seal(engine, run_id, role, purpose, TASK, context, SCHEMA, hid)
            try:
                validate(value, context)
            except ImplementationAmendmentReadRequired:
                binding = {'packet_sha256':repair.sha(packet), 'source_sha256':context['amendment_source_sha256'],
                    'handoff_id':hid,'role':role,'output_sha256':repair.sha(value)}
                identifier = run_id+':amendment-read:'+repair.sha(binding)
                record = engine.store.get(FOLLOWUPS,identifier) or {}
                reading = {'handoff_id':hid,'result':value,'host_evidence_followup':True,
                    'host_evidence_requests':record.get('body',{}).get('queries'),
                    'followup_id':identifier,'followup_sha256':record.get('sha256')}
                verify_citation_reading(engine,run_id,role,packet,context,reading)
                readings.append(reading)
                continue
            if value['evidence_requests']:
                readings.append({'handoff_id':hid,'result':value})
                continue
            authors.append({'role':role,'handoff_id':hid,'result':value,'readings':readings})
            break
    restored = apply(packet,audit,authors)
    verify(engine,run_id,restored)
    return restored


