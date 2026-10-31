"""Integrate approved definitions by a bound patch, preserving the paid draft.

This is a writing handoff. Its predecessor's claims remain claims until the
resulting complete architecture receives its own independent review.
"""
from __future__ import annotations

import copy
import json
import re

from . import document_index
from .storage import now

VERSION = 1
SCHEMA_VERSION = 2

ACCEPTANCE_INSTRUCTION = '''
acceptance 只允许 criteria、complete、limitations。complete 表示定量验收标准是否完整覆盖，
不是实验是否已经执行或结果是否通过；没有测量本身不表示 complete=false，也不授权把已有 false 自动改成 true。
实现核验、参数计数、泄漏检查、预算记账等非数值验收条款写入 evaluation 的“实现核验清单”章节，
保留问题 ID、完整通过条件、失败停止条件及计费/预算归属；不要向 acceptance 添加自创字段。
本次是增量合并：对象只省略未改字段，已有字段类型、合法键及数组元素的完整结构仍须严格满足 schema。
'''

INSTRUCTION = '''
把已经批准的科学定义纳入同一个架构候选。integration_base_parameters 是原作者实际保存的参数草稿，
宿主会原样保留未改字段；本次 design 只提交需要修改的字段，参数对象递归合并，数组整体替换。
unset_paths 只用于删除明确冲突的嵌套参数字段，使用 JSON pointer；不要重写不受影响的参数或重新生成整份 DESIGN。
原 architecture/dataset/evaluation 说明按 previous_author_response_document 定向查看，需要变更时替换对应字段。
本轮范围是 review_fixes，definition_integration_agenda 把每项意见绑定到已批准的同版本定义及尚未读过的范围。
先补读 remaining_reads，必要的跨条款依赖按原目录核对，并保存带实际原文锚点的 working_notes；
不要为了重建旧参数反复读取同一页。历史读取和笔记保留供恢复，不新增本轮阅读或科学认证信用。
已批准定义可以用精确文档版本和章节引用纳入唯一规范，不必再次抄写全文或索取同版本共识。
它们属于获批的项目实现合同；不能冒称发表版完全等价或实验已完成。
逐项关闭本轮意见并说明修改及验收条件；不再以“旧页已归档”或“仍缺代码测量”为理由重开已批准定义。
涉及资源的意见先核对 host_admission_reads。资源准入是宿主的计划评估，显存及耗时估计不冒称实测；
吞吐预检、超限停止和测量留待已有计划内的执行门禁，不能让设计等待尚未获准运行的实验。
保留原问题、机制、完整强基线、全部变体、种子、阈值、数据隔离、实验预算及失败停止政策。
只有批准补充之外的科学范围确实需要改变时 requires_new_consensus=true 并给出具体缺口。
resolved_issue_ids 只登记本轮 review_fixes 中实际处理的意见；不要把未处理问题标成已闭合。
宿主将保存由真实前稿和本次修改组成的完整候选，之后另行独立评议。本次不得写代码、运行实验或自行批准。
''' + ACCEPTANCE_INSTRUCTION


def _patch_object(shape):
    """Objects merge recursively; array replacements keep complete item schemas."""
    result = copy.deepcopy(shape)
    if result.get('type') == 'object':
        if 'required' in result:
            result['required'] = []
        for field, child in result.get('properties', {}).items():
            if child.get('type') == 'object':
                result['properties'][field] = _patch_object(child)
    return result


def schema():
    from .engine import DESIGN, STR, STRS, obj
    patch = _patch_object(DESIGN)
    patch['minProperties'] = 1
    return obj({'design': patch, 'unset_paths': STRS, 'integration_base_hash': STR,
        'proposal_hash': STR, 'parent_design_hash': STR, 'resolved_issue_ids': STRS,
        'change_summary': {'type': 'string', 'minLength': 1},
        'requires_new_consensus': {'type': 'boolean'}})


def _merge(base, patch):
    result = copy.deepcopy(base)
    for field, value in patch.items():
        result[field] = (_merge(result[field], value)
                         if isinstance(result.get(field), dict) and isinstance(value, dict)
                         else copy.deepcopy(value))
    return result


def apply(base, result, *, expected_hash):
    from .engine import DESIGN, ResearchBlocked, digest
    from .intelligence import parse_json
    parse_json(json.dumps(result, ensure_ascii=False, allow_nan=False), schema())
    if result.get('integration_base_hash') != expected_hash or digest(base) != expected_hash:
        raise ResearchBlocked('架构整合修改未绑定实际保存的原作者草稿。')
    candidate = _merge(base, result['design'])
    deleted = set()
    for pointer in result.get('unset_paths', []):
        if (not isinstance(pointer, str) or not pointer.startswith('/')
                or re.search(r'~(?![01])', pointer) or pointer in deleted):
            raise ResearchBlocked('架构整合删除路径无效或重复。')
        parts = [p.replace('~1', '/').replace('~0', '~') for p in pointer[1:].split('/')]
        if len(parts) < 2 or parts[0] not in DESIGN['properties']:
            raise ResearchBlocked('架构整合不能删除完整必需字段或修改索引以外的字段。')
        target = candidate
        for part in parts[:-1]:
            if not isinstance(target, dict) or part not in target:
                raise ResearchBlocked('架构整合删除路径不属于实际草稿。')
            target = target[part]
        if not isinstance(target, dict) or parts[-1] not in target:
            raise ResearchBlocked('架构整合删除路径不属于实际草稿。')
        del target[parts[-1]]
        deleted.add(pointer)
    parse_json(json.dumps({k: v for k, v in candidate.items() if k in DESIGN['properties']},
                          ensure_ascii=False, allow_nan=False), DESIGN)
    from .architecture_convergence import material
    if material(candidate, 'architecture') == material(base, 'architecture'):
        raise ResearchBlocked('架构整合没有实质修改，停止重复提交同一草稿。')
    return candidate


def check(engine, run_id, integration_id, *, audit_record=None, adoption=None):
    from .engine import Harness, ResearchBlocked, digest
    from .architecture_definitions import validate_adoption
    audit = audit_record if audit_record is not None else engine.store.get('stage_contexts', integration_id, {})
    payload = audit.get('payload', {})
    prior = payload.get('pending', {})
    hid = prior.get('handoff_id', '')
    decision = engine.store.get('decisions', hid, {})
    frozen = engine.store.get('handoff_inputs', hid, {})
    from_adoption = payload.get('source_mode') == 'approved_definition_adoption'
    if (audit.get('run_id') != run_id or audit.get('kind') != 'architecture_integration_predecessor'
            or audit.get('sha256') != digest(payload)
            or hid != Harness.decision_key(run_id, 'engineer', prior.get('purpose'),
                prior.get('instruction'), prior.get('context'), prior.get('schema'))
            or decision.get('run_id') != run_id or decision.get('role') != 'engineer'
            or decision.get('result') != payload.get('response')
            or frozen.get('context') != prior.get('context')
            or frozen.get('instruction') != prior.get('instruction')
            or frozen.get('schema') != prior.get('schema')
            or engine.store.get('handoff_progress', hid, {}) != payload.get('progress')
            or payload['response'].get('requires_new_consensus') is not (True if from_adoption else False)):
        raise ResearchBlocked('架构整合缺少同版本实际作者交付及不可变读取记录。')
    adopted = (adoption if adoption and adoption['id'] == payload['definition_adoption_id']
               else validate_adoption(engine, run_id, payload['definition_adoption_id']))
    if ((not from_adoption and prior['context'].get('definition_adoption_id') != adopted['id'])
            or (from_adoption and (adopted.get('source_author_handoff_id') != hid
                or adopted.get('original_author_payload', {}).get('response') != payload['response']))
            or payload['response']['proposal_hash'] != adopted['proposal_hash']
            or payload['response']['parent_design_hash'] != adopted['parent_design_hash']):
        raise ResearchBlocked('架构整合的作者草稿与获批定义不属于同一版本。')
    revision = engine.store.get('architecture_revisions', payload['revision_id'], {})
    original = engine.store.get('steps', run_id + ':design', {}).get('output')
    parents = [original, *[r['design'] for r in revision.get('revisions', [])]]
    parent = next((p for p in parents if p is not None and digest(p) == adopted['parent_design_hash']), None)
    expected = prior['context']['review_fixes']
    resolved = set(payload['response']['resolved_issue_ids'])
    if (parent is None or not resolved <= {i['id'] for i in expected}
            or payload['unresolved_issues'] != [i for i in expected if i['id'] not in resolved]
            or payload['base_design'] != {**parent, **payload['response']['design']}
            or digest(payload['base_design']) != payload['base_design_hash']):
        raise ResearchBlocked('架构整合草稿或剩余意见不等于真实作者交付。')
    return audit


def prepare_from_adoption(engine, run_id, revision, adopted):
    """Use the actual pre-adoption draft immediately, without another full rewrite."""
    from .engine import ResearchBlocked, digest
    from . import architecture_revisions as revisions
    revisions._available(engine, run_id)
    original = engine.store.get('steps', run_id + ':design')['output']
    payload = copy.deepcopy(adopted['original_author_payload'])
    payload.update(source_mode='approved_definition_adoption', definition_adoption_id=adopted['id'],
                   revision_id=revision['id'])
    parent = revision['revisions'][-1]['design'] if revision['revisions'] else original
    payload['base_design'] = {**copy.deepcopy(parent), **copy.deepcopy(payload['response']['design'])}
    payload['base_design_hash'] = digest(payload['base_design'])
    key = revision['id'] + ':integration-adoption-v2:' + digest(adopted['id'])
    audit = {'id': key, 'run_id': run_id, 'kind': 'architecture_integration_predecessor',
             'payload': payload, 'sha256': digest(payload), 'created_at': now()}
    existing = engine.store.get('stage_contexts', key)
    if existing:
        if existing.get('payload') != payload:
            raise ResearchBlocked('已批准定义的增量整合基点发生变化。')
        audit = existing
    check(engine, run_id, key, audit_record=audit, adoption=adopted)
    handoff = {'audit_id': key, 'sha256': audit['sha256'], 'status': 'prepared',
        'remaining_issue_count': len(payload['unresolved_issues']), 'scientific_approval': False}
    updated = {**revision, 'status': 'drafting', 'definition_adoption_id': adopted['id'],
               'pending': copy.deepcopy(payload['pending']), 'response': copy.deepcopy(payload['response']),
               'integration_handoff': handoff}
    engine.store.batch([('stage_contexts', key, audit), ('architecture_revisions', revision['id'], updated)])
    engine.store.event(run_id, 'architecture.integration_prepared',
        '定义已批准，直接基于原作者交付安排增量整合；新架构仍须独立评议。', handoff)
    return updated


def prepare(engine, run_id):
    """Prepare one concrete stopped-run handoff without making a model call."""
    from .engine import ResearchBlocked, digest
    from . import architecture_revisions as revisions, architecture_definitions as definitions
    run, revision = engine.store.get('runs', run_id), revisions.state(engine, run_id)
    if revision and revision.get('integration_handoff'):
        check(engine, run_id, revision['integration_handoff']['audit_id'])
        return revision['integration_handoff']
    if (run['mode'] != 'live' or run['stage'] != 'design'
            or run['status'] not in {'blocked', 'paused', 'failed'}
            or not revision or revision['status'] != 'blocked'
            or not revision.get('definition_adoption_id') or not revision.get('unresolved_issues')):
        raise ResearchBlocked('架构整合恢复需要已停止、定义已批准但作者仍有具体未处理意见的任务。')
    if any((v.get('checkpoint') or {}).get('status') in definitions.TRANSIENT
           for v in engine.store.list('codex_reasoning', run_id)):
        raise ResearchBlocked('仍有活动请求，不能重新交接架构整合。')
    revisions._available(engine, run_id)
    original = engine.store.get('steps', run_id + ':design')['output']
    revisions._checked(engine, run_id, original)
    if (revision['pending']['context'].get('architecture_integration_protocol')
            or revision['response'].get('requires_new_consensus') is not False):
        raise ResearchBlocked('该交付需要原科学重审或已采用整合协议，不能反复重新提交相同作者任务。')
    payload = {field: copy.deepcopy(revision[field]) for field in
        ('pending', 'response', 'author_response_document', 'unresolved_issues', 'definition_adoption_id')}
    payload['progress'] = copy.deepcopy(engine.store.get('handoff_progress', payload['pending']['handoff_id'], {}))
    parent = revision['revisions'][-1]['design'] if revision['revisions'] else original
    payload['base_design'] = {**copy.deepcopy(parent), **copy.deepcopy(payload['response']['design'])}
    payload['base_design_hash'] = digest(payload['base_design'])
    payload['revision_id'] = revision['id']
    key = revision['id'] + ':integration-predecessor-v1'
    audit = {'id': key, 'run_id': run_id, 'kind': 'architecture_integration_predecessor',
        'payload': payload, 'sha256': digest(payload), 'created_at': now()}
    if engine.store.get('stage_contexts', key):
        raise ResearchBlocked('已有整合恢复审计，不能覆盖或再次创建。')
    check(engine, run_id, key, audit_record=audit)
    handoff = {'audit_id': key, 'sha256': audit['sha256'], 'status': 'prepared',
               'remaining_issue_count': len(payload['unresolved_issues']), 'scientific_approval': False}
    engine.store.batch([('stage_contexts', key, audit),
        ('architecture_revisions', revision['id'], {**revision, 'integration_handoff': handoff})])
    engine.store.event(run_id, 'architecture.integration_prepared',
        '已保留真实作者草稿与批准定义，安排对剩余意见提交定向修改；尚未批准新架构。', handoff)
    return handoff


def _missing_reads(document, start, end, receipts):
    spans = [(start, end)]
    for row in receipts:
        page = row.get('result', {})
        if (row.get('call', {}).get('name') != 'documents.read'
                or page.get('document_id') != document['document_id']
                or page.get('sha256') != document['sha256'] or not isinstance(page.get('text'), str)):
            continue
        a, b = page['char_start'], page['char_end']
        spans = [piece for x, y in spans for piece in ((x, min(y, a)), (max(x, b), y)) if piece[0] < piece[1]]
    calls = []
    for section in document['sections']:
        for a, b in spans:
            cursor, stop = max(a, section['start']), min(b, section['end'])
            for position in range(cursor, stop, 6000):
                calls.append({'name': 'documents.read', 'arguments': {
                    'document_id': document['document_id'], 'expected_sha256': document['sha256'],
                    'section_id': section['id'], 'offset': position - section['start'],
                    'limit': min(6000, stop - position)}})
    return calls


async def activate(engine, h, run_id, revision):
    from .engine import DESIGN, ResearchBlocked, digest
    from .architecture_definitions import validate_adoption
    queued = revision.get('integration_handoff')
    if not queued or queued['status'] != 'prepared':
        return revision
    audit = check(engine, run_id, queued['audit_id'])
    payload = audit['payload']
    if (audit['sha256'] != queued['sha256'] or revision['pending'] != payload['pending']
            or revision['response'] != payload['response']):
        raise ResearchBlocked('架构整合恢复目标发生变化。')
    adopted = validate_adoption(engine, run_id, payload['definition_adoption_id'])
    from .execution_contract import constraints, host_budget_snapshot
    from .lab import assess_resources
    from .implementation_documents import _save
    run = engine.store.get('runs', run_id)
    host_key = audit['id'] + ':host-planning-v1'
    host_snapshot = engine.store.get('stage_contexts', host_key)
    if host_snapshot:
        admission = host_snapshot['payload']
        if (host_snapshot.get('run_id') != run_id or host_snapshot.get('sha256') != digest(admission)
                or admission.get('proposal_hash') != adopted['proposal_hash']
                or admission.get('execution_constraints') != constraints(run['config'])):
            raise ResearchBlocked('整合宿主快照或当前操作约束发生变化，不能重发原交接。')
    else:
        hardware = await engine.execution_hardware(run_id)
        admission = {'checked_at': now(), 'proposal_hash': adopted['proposal_hash'],
            'execution_constraints': constraints(run['config']),
            'current_call_budget': host_budget_snapshot(engine.store, run_id,
                engine.store.get('consensus', run_id)['proposal'], adopted['proposal_hash'], engine.cycle(run_id)),
            'operator_authorization': revision.get('operator_authorization'),
            'resource_admission': {'resource_plan': payload['base_design']['resource_plan'],
                'assessment': assess_resources(payload['base_design']['resource_plan'], hardware)},
            'current_hardware': hardware,
            'scope': 'Fresh host planning assessment, not measured training time or peak memory. '
                'Original experimental matrix and independent execution gates remain mandatory.'}
        engine.store.put('stage_contexts', host_key, {'id': host_key, 'run_id': run_id,
            'kind': 'architecture_integration_host_planning', 'payload': admission,
            'sha256': digest(admission), 'created_at': now()})
    admission_text = '# 当前架构整合的宿主准入\n\n' + '\n\n'.join(
        '## ' + k + '\n\n```json\n' + json.dumps(v, ensure_ascii=False, indent=2, allow_nan=False)
        + '\n```' for k, v in admission.items()) + '\n'
    authority = _save(engine, h, run_id,
        f'cycle-{engine.cycle(run_id)}/architecture/architecture-integration-host-admission-v1.md',
        admission_text, 'architecture_host_receipt')
    doc = adopted['document']
    _, text = document_index.load(engine, run_id, doc['document_id'], doc['sha256'])
    receipts = payload['progress'].get('tool_results', [])
    agenda = []
    for issue in payload['unresolved_issues']:
        definition = next((d for d in adopted['candidate']['definitions'] if d['issue_id'] == issue['id']), None)
        entry = {'issue_id': issue['id'], 'issue': issue['issue'], 'approved_definition': None, 'remaining_reads': []}
        if definition:
            start = text.find(definition['definition_md'])
            if start < 0:
                raise ResearchBlocked('批准定义的具体条款不能定位到不可变正文。')
            end = start + len(definition['definition_md'])
            entry.update(approved_definition={'document_id': doc['document_id'], 'sha256': doc['sha256'],
                'title': doc['title'], 'char_start': start, 'char_end': end},
                remaining_reads=_missing_reads(doc, start, end, receipts))
        agenda.append(entry)
    context = copy.deepcopy(payload['pending']['context'])
    notes = payload['progress'].get('document_working_notes', {}).get('document')
    additions = [doc, authority, payload['author_response_document'], *([notes] if notes else [])]
    directories = {d['document_id']: d for d in [*context['indexed_documents'], *additions]}
    from .architecture_convergence import context as progress_context, AUTHOR
    context.update(progress_context(engine, run_id, 'architecture', payload['base_design']))
    context.update(architecture_integration_protocol=VERSION,
        integration_schema_version=SCHEMA_VERSION, definition_adoption_id=adopted['id'],
        approved_definition_document=doc,
        architecture_workflow_protocol=1, document_read_window_protocol=2,
        integration_audit_id=audit['id'], integration_base_hash=payload['base_design_hash'],
        integration_base_parameters={k: copy.deepcopy(v) for k, v in payload['base_design'].items()
            if k in DESIGN['properties'] and k not in {'architecture', 'dataset', 'evaluation', 'limitations'}},
        previous_author_response_document=payload['author_response_document'],
        previous_engineer_notes_document=notes,
        previous_engineer_read_handoff_id=payload['pending']['handoff_id'],
        review_fixes=copy.deepcopy(payload['unresolved_issues']),
        revision_agenda=copy.deepcopy(payload['unresolved_issues']),
        definition_integration_agenda=agenda, indexed_documents=list(directories.values()),
        authority_document=authority, execution_constraints=admission['execution_constraints'])
    # Exact host sections answer planning admission; no measurements are created.
    context['host_admission_reads'] = [call for section in authority['sections']
        if section['title'] in {'execution_constraints', 'current_call_budget', 'operator_authorization', 'resource_admission'}
        for call in _missing_reads(authority, section['start'], section['end'], [])]
    purpose = payload['pending']['purpose'] + ':integration-v1'
    shape = schema()
    instruction = INSTRUCTION + AUTHOR
    hid = h._handoff(run_id, 'engineer', purpose, instruction, context, shape)['id']
    revision.update(status='drafting', pending={'purpose': purpose, 'instruction': instruction,
        'context': context, 'schema': shape, 'handoff_id': hid},
        integration_authority_document=authority,
        integration_handoff={**queued, 'status': 'applied', 'next_handoff_id': hid})
    revision.pop('reason', None)
    revision.pop('response', None)
    engine.store.put('architecture_revisions', revision['id'], revision)
    engine.store.event(run_id, 'architecture.integration_started',
        '开始纳入已批准定义；保留未改参数，仅补读剩余条款并提交修改。',
        {'handoff_id': hid, 'remaining_issue_count': len(agenda), 'scientific_approval': False})
    return revision


def materialize(engine, run_id, pending, result, *, adoption=None):
    from .engine import ResearchBlocked
    audit = check(engine, run_id, pending['context']['integration_audit_id'], adoption=adoption)
    payload = audit['payload']
    if pending['context']['integration_base_hash'] != payload['base_design_hash']:
        raise ResearchBlocked('整合交接的参数基点发生变化。')
    candidate = apply(payload['base_design'], result, expected_hash=payload['base_design_hash'])
    if pending['context'].get('architecture_delivery_repair_protocol') == 1:
        from .architecture_integration_recovery import preserve_constraints
        candidate = preserve_constraints(engine, run_id, pending, result, candidate)
    return candidate


def next_reads(context, receipts):
    """One exact incomplete issue per prompt; ordinary extra reads remain bounded."""
    documents = {d['document_id']: d for d in context['indexed_documents']}
    groups = [e['remaining_reads'] for e in context['definition_integration_agenda']]
    groups.append(context['host_admission_reads'])
    for group in groups:
        calls = []
        used = 0
        for call in group:
            args = call['arguments']
            doc = documents[args['document_id']]
            section = next(s for s in doc['sections'] if s['id'] == args['section_id'])
            start = section['start'] + args['offset']
            for missing in _missing_reads(doc, start, start + args['limit'], receipts):
                length = missing['arguments']['limit']
                if len(calls) >= 24 or used + length > 11000:
                    return calls
                calls.append(missing)
                used += length
        if calls:
            return calls
    return []
