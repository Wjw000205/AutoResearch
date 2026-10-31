"""Immutable, scoped repair of a completed supplementary-definition review.

The operator supplies exact unit replacements. Only the original objectors
recheck them; prior votes stay bound to their original document. Successful
rechecks enable author revision, never scientific approval or code execution.
"""
from __future__ import annotations

import copy
import hashlib
import re
from types import SimpleNamespace

from .engine import ResearchBlocked, digest
from .storage import now
from . import implementation_source_supplement as review
from . import implementation_acceptance_author as author
from . import implementation_acceptance_flow as flow
from . import document_index
from .architecture_validation import operation


def unit_spans(text):
    """Identify disjoint definition sections and individual dependency rows."""
    spans = {}
    for section in document_index.sections(text):
        match = re.search(r'\b([HD]0[1-4])\b', section['title'])
        if match:
            unit = match[1]
            if unit in spans:
                raise ValueError('Duplicate supplementary unit')
            spans[unit] = (section['start'], section['end'])
    for match in re.finditer(r'^\| (G0[1-5]) \|[^\n]*(?:\n|$)', text, re.M):
        if match[1] in spans:
            raise ValueError('Duplicate supplementary dependency')
        spans[match[1]] = match.span()
    if set(spans) != set(review.UNITS):
        raise ValueError('Supplement must retain all eleven indexed units')
    return spans


def patched_text(text, replacements, expected_units):
    if (not isinstance(replacements, list) or not replacements
            or any(not isinstance(p, dict) or set(p) != {'unit', 'before_sha256', 'text'} for p in replacements)
            or len({p['unit'] for p in replacements}) != len(replacements)
            or {p['unit'] for p in replacements} != set(expected_units)):
        raise ValueError('Replace exactly the unresolved units, once each')
    spans = unit_spans(text)
    changes = []
    for patch in replacements:
        start, end = spans[patch['unit']]
        before, after = text[start:end], patch['text']
        if (hashlib.sha256(before.encode()).hexdigest() != patch['before_sha256']
                or not isinstance(after, str) or not after.strip() or after == before):
            raise ValueError('Replacement must bind and change the exact previous unit')
        if patch['unit'].startswith('G'):
            if not after.startswith('| ' + patch['unit'] + ' |') or after.count('\n') != 1 or not after.endswith('\n'):
                raise ValueError('Dependency replacement must remain one table row')
        elif after.splitlines()[0] != before.splitlines()[0] or not after.endswith('\n\n'):
            raise ValueError('Replacement must preserve its heading and section boundary')
        changes.append((start, end, after))
    result = text
    for start, end, after in sorted(changes, reverse=True):
        result = result[:start] + after + result[end:]
    new_spans = unit_spans(result)
    for unit in review.UNITS:
        if unit not in expected_units:
            a, b = spans[unit]; x, y = new_spans[unit]
            if text[a:b] != result[x:y]:
                raise ValueError('Previously accepted unit changed outside repair scope')
    return result


@operation
def original_issues(engine, run_id, parent, state):
    payload = review.checked(engine, run_id, parent, state)
    roles = [p['role'] for p in parent['votes']]
    if len(roles) != len(review.ROLES) or set(roles) != set(review.ROLES):
        raise ResearchBlocked('定向修订需要完整的原三方审核。')
    for packet in parent['votes']:
        review.validate_vote(engine, run_id, packet, payload['context']['source_supplement']['document'], payload['context'])
    return [{'role':p['role'], **copy.deepcopy(item)} for p in parent['votes']
            for item in p['result']['checks'] if item['status'] != 'accept']


def prepare(engine, run_id, *, record_id, expected_sha256, replacements):
    """Explicit operator action; save one new candidate and four scoped queues."""
    from .config import get_settings
    from .intelligence import Intelligence
    from .implementation_documents import _save
    run = engine.store.get('runs', run_id, {})
    parent = engine.store.get(review.COLLECTION, record_id, {})
    if (run.get('status') not in {'blocked', 'paused'} or run.get('mode') != 'live'
            or parent.get('run_id') != run_id or parent.get('sha256') != expected_sha256
            or parent.get('status') != 'changes_required' or parent.get('payload', {}).get('repair')):
        raise ResearchBlocked('仅能定向修订已完成审核且仍有意见的原补充候选。')
    state = engine.store.get('codex_workflows', parent['payload']['workflow_id'])
    issues = original_issues(engine, run_id, parent, state)
    if not issues:
        raise ResearchBlocked('没有待修订的补充意见。')
    old_doc = parent['payload']['context']['source_supplement']['document']
    _, old_text = document_index.load(engine, run_id, old_doc['document_id'], old_doc['sha256'])
    new_text = patched_text(old_text, replacements, {i['id'] for i in issues})
    binding = {'version': 1, 'parent_id': parent['id'], 'parent_sha256': parent['sha256'],
        'parent_votes_sha256': digest(parent['votes']), 'replacements': copy.deepcopy(replacements),
        'issues': issues, 'author_provenance': 'operator_requested_targeted_revision', 'recheck_limit': 1}
    candidate = _save(engine, SimpleNamespace(intelligence=Intelligence(engine.store, get_settings(engine.store))),
        run_id, 'cycle-' + str(engine.cycle(run_id)) + '/supplements/repairs/' + digest(binding) + '.md',
        new_text, 'scientific_supplement_candidate')
    payload = copy.deepcopy(parent['payload'])
    payload['repair'] = binding
    context = payload['context']
    context['indexed_documents'] = [*context['indexed_documents'], candidate]
    context['source_supplement']['document'] = candidate
    key = run_id + ':source-supplement:' + digest(payload)
    existing = engine.store.get(review.COLLECTION, key)
    pointer = engine.store.get('stage_contexts', run_id + review.POINTER, {})
    if pointer.get('record_id') not in {parent['id'], key}:
        raise ResearchBlocked('补充审核当前指针已改变，未替换其他任务。')
    row = existing or {'id': key, 'run_id': run_id, 'payload': payload, 'sha256': digest(payload),
        'status': 'prepared', 'votes': [], 'rechecks': {}, 'issues': issues, 'created_at': now(),
        'adopted': False, 'scientific_approval': False, 'execution_authorized': False}
    if row['payload'] != payload or row['sha256'] != digest(payload):
        raise ResearchBlocked('定向修订已有不同的冻结记录。')
    engine.store.batch([(review.COLLECTION, key, row), ('stage_contexts', run_id + review.POINTER,
        {'id': run_id + review.POINTER, 'run_id': run_id, 'record_id': key})])
    engine.store.event(run_id, 'implementation.supplement_repair_prepared',
        '补充候选已按原意见保存新版本；仅由原提出者复核修改项，已接受内容和原回执保留。',
        {'record_id': key, 'parent_id': parent['id'], 'document': candidate,
         'targets': [{'role':i['role'], 'unit':i['id']} for i in issues]})
    return row


@operation
def checked(engine, run_id, row, state):
    payload = review.checked(engine, run_id, row, state)
    binding = payload.get('repair', {})
    parent = engine.store.get(review.COLLECTION, binding.get('parent_id', ''), {})
    if (binding.get('version') != 1 or binding.get('recheck_limit') != 1
            or parent.get('sha256') != binding.get('parent_sha256')
            or digest(parent.get('votes')) != binding.get('parent_votes_sha256')
            or original_issues(engine, run_id, parent, state) != binding.get('issues')):
        raise ResearchBlocked('原审核或定向修订范围已改变。')
    old = parent['payload']['context']['source_supplement']['document']
    new = payload['context']['source_supplement']['document']
    _, before = document_index.load(engine, run_id, old['document_id'], old['sha256'])
    _, after = document_index.load(engine, run_id, new['document_id'], new['sha256'])
    if patched_text(before, binding['replacements'], {i['id'] for i in binding['issues']}) != after:
        raise ResearchBlocked('修订文档与冻结的最小修改不一致。')
    return payload


def recheck_context(row, issue):
    context = copy.deepcopy(row['payload']['context'])
    context['source_supplement']['units'] = [issue['id']]
    context['source_supplement_recheck'] = {'version': 1, 'previous_issue': copy.deepcopy(issue),
        'parent_review_id': row['payload']['repair']['parent_id'], 'one_scoped_pass': True,
        'scope': '只核对原 required_change；不新增标准、重审其他项或要求现在运行实验。'}
    return context


def validate_rechecks(engine, run_id, row, *, complete):
    targets = {i['role'] + ':' + i['id']: i for i in row['payload']['repair']['issues']}
    packets = row.get('rechecks', {})
    if set(packets) - set(targets) or complete and set(packets) != set(targets):
        raise ResearchBlocked('定向复核回执缺项或越界。')
    for key, packet in packets.items():
        issue = targets[key]
        context = recheck_context(row, issue)
        result = packet['result']
        if (flow.check_packet(engine, run_id, packet) != context or packet['role'] != issue['role']
                or result['supplement_sha256'] != context['source_supplement']['document']['sha256']
                or len(result['checks']) != 1 or result['checks'][0]['id'] != issue['id']):
            raise ResearchBlocked('定向复核没有绑定原提出者、当前版本和原问题。')
        review.validate_item(engine, run_id, packet, context['source_supplement']['document'],
            context, result['checks'][0])


def adoption_binding(row):
    return {'record_id': row['id'], 'payload_sha256': row['sha256'], 'rechecks_sha256': digest(row['rechecks'])}


@operation
def validate_adoption(engine, run_id, state, binding):
    row = engine.store.get(review.COLLECTION, binding.get('record_id', ''), {})
    checked(engine, run_id, row, state)
    validate_rechecks(engine, run_id, row, complete=True)
    if (adoption_binding(row) != binding
            or any(p['result']['checks'][0]['status'] != 'accept' for p in row['rechecks'].values())):
        raise ResearchBlocked('补充来源仍未通过定向复核，不能交接作者。')
    return row


@operation
def author_plan(engine, run_id, state, row):
    """Preserve the original saved draft; extend only its source/revision scope."""
    from . import implementation_authoring as drafting
    binding = adoption_binding(row)
    validate_adoption(engine, run_id, state, binding)
    source_id = row['payload']['source_handoff_id']
    old_context = engine.store.get('handoff_inputs', source_id)['context']
    base = author.continuation_base(engine, run_id, state, old_context)
    base_id = old_context[author.CONTINUATION]['source_handoff_id']
    source = author._source_records(engine.store, base_id)
    if (source.get(drafting.COLLECTION) or {}).get('row', {}).get('spec_hash') != base['spec_hash']:
        raise ResearchBlocked('补充来源作者交接没有匹配的已保存基稿。')
    audit_id = row['id'] + ':author-adoption'
    payload = {'source': source, 'workflow': author._workflow_scope(state),
        'pending': copy.deepcopy(row['payload']['repair']['issues']), 'supplement_repair': binding}
    context = copy.deepcopy(old_context)
    for key in ('implementation_document_completion_protocol', 'implementation_document_completion',
                'implementation_phase_context_protocol', 'implementation_phase_context', 'implementation_document_repair_reads'):
        context.pop(key, None)
    context[author.CONTINUATION] = {'audit_id': audit_id, 'sha256': digest(payload),
        'source_handoff_id': base_id, 'spec_hash': base['spec_hash'], 'document': base['document'],
        'depth': old_context[author.CONTINUATION].get('depth', 0) + 1}
    directories = {d['document_id']: d for d in context['indexed_documents']}
    directories.update({d['document_id']: d for d in row['payload']['context']['indexed_documents']})
    context['indexed_documents'] = list(directories.values())
    context['source_supplement_adoption'] = {**binding,
        'document': row['payload']['context']['source_supplement']['document'],
        'historical_review_id': row['payload']['repair']['parent_id'],
        'scope': '原三方候选评议加原提出者对修改项的定向复核；不是全新版本三方全文审核。',
        'scientific_approval': False, 'execution_authorized': False}
    context['acceptance_author_pending'] = copy.deepcopy(old_context.get('acceptance_author_pending', []))
    context['supplement_resolved_source_gaps'] = [i['id'] for i in row['payload']['repair']['issues']]
    instruction = ('原来源缺口已有补充候选和定向复核。先读取 source_supplement_adoption.document 的相关条款，'
        '按它与原验收意见定向修订当前实现文档，不再重复报告已经补齐的来源缺失。'
        '这仅授权作者采用补充候选修改草稿，不能关闭原问题、批准实现或执行实验。'
        '不重写已完成且未受影响的单元；实际变更后补做对应自检，既有调用预算与原问题列表不变。'
        '将已审核补充中各问题的候选定义与同步职责落实到对应正文。'
        '保留论文与项目新增定义的差异；软件协议和资源核验仍须相应阶段验证，不能写成已执行。'
        + drafting.EDIT_TASK + author.GUIDANCE)
    purpose = 'implementation_spec:source-repair:' + row['sha256']
    existing = engine.store.get('stage_contexts', audit_id)
    if existing:
        # Guidance may change while the author is working. Follow the saved
        # dispatch edge after authenticating it; never create a second author
        # or abandon the existing draft because newer prose hashes differently.
        from .engine import Harness
        successor = existing.get('successor_id', '')
        frozen = engine.store.get('handoff_inputs', successor, {})
        assignment = engine.store.get('handoffs', successor, {})
        packet = {k: frozen.get(k) for k in ('instruction', 'context', 'schema')}
        if (existing.get('run_id') != run_id or existing.get('payload') != payload
                or existing.get('sha256') != digest(payload)
                or existing.get('successor_context_sha256') != digest(context)
                or frozen.get('run_id') != run_id or frozen.get('context') != context
                or frozen.get('schema') != drafting.submission_schema(context)
                or not isinstance(frozen.get('instruction'), str)
                or assignment.get('run_id') != run_id or assignment.get('to_role') != 'engineer'
                or assignment.get('purpose') != purpose or assignment.get('input_hash') != digest(packet)
                or Harness.decision_key(run_id, 'engineer', purpose, **packet) != successor):
            raise ResearchBlocked('已有补充修订交接的冻结来源或绑定已改变，未重建或派发。')
        instruction = frozen['instruction']
    return {'source_id': base_id, 'payload': payload, 'audit_id': audit_id, 'sha256': digest(payload),
        'context': context, 'instruction': instruction,
        'purpose': purpose,
        'run_guard': {k: engine.store.get('runs', run_id).get(k) for k in ('status', 'config', 'state')}}


async def continue_repair(engine, h, run_id, state, row):
    checked(engine, run_id, row, state)
    validate_rechecks(engine, run_id, row, complete=False)
    for issue in row['payload']['repair']['issues']:
        key = issue['role'] + ':' + issue['id']
        if key in row['rechecks']:
            continue
        engine.check_active(run_id)
        row.update(status='reviewing', active_role=issue['role'])
        engine.store.put(review.COLLECTION, row['id'], row)
        schema = copy.deepcopy(review.SCHEMA)
        schema['properties']['checks']['minItems'] = schema['properties']['checks']['maxItems'] = 1
        schema['properties']['checks']['items']['properties']['id'] = {'enum': [issue['id']]}
        packet = await flow.ask(engine, h, run_id, state,
            'implementation_source_supplement_recheck:' + row['sha256'] + ':' + key, issue['role'],
            '复核你上一轮提出的这一条修改意见。读取新候选对应条款及必要原依据，'
            '仅按 previous_issue.required_change 验收，不重开发现阶段，不增加新标准或重审其他项。'
            '若原要求已落实，应接受该候选供作者采用；不要求现在运行效果实验或资源探针。'
            '只返回本条，当前候选和原依据必须有本交接真实读取证据。',
            recheck_context(row, issue), schema)
        row['rechecks'][key] = packet
        validate_rechecks(engine, run_id, row, complete=False)
        engine.store.put(review.COLLECTION, row['id'], row)
    validate_rechecks(engine, run_id, row, complete=True)
    issues = [{'role':p['role'], **p['result']['checks'][0]} for p in row['rechecks'].values()
              if p['result']['checks'][0]['status'] != 'accept']
    row.update(status='changes_required' if issues else 'reviewed', issues=issues,
        active_role=None, completed_at=row.get('completed_at') or now())
    engine.store.put(review.COLLECTION, row['id'], row)
    if issues:
        raise ResearchBlocked('补充候选定向复核仍需修订：' + '；'.join(
            i['role'] + ' ' + i['id'] + '：' + i['required_change'] for i in issues))
    return await engine._read_consensus_check(run_id, author_plan, engine, run_id, state, row)
