"""Indexed, genuinely delivered source packets for Engineer scientific authors.

This is an author protocol, not an independent-review vote or an approval.
Original model decisions and source documents remain immutable.
"""
from __future__ import annotations

import copy
import json

from . import independent_full_review as full

KEY = 'scientific_author_protocol'
MATERIAL = 'scientific_author_materials'
READING = 'scientific_author_reading'
CONTROLS = {KEY, MATERIAL, READING, 'indexed_document_context', 'document_index_protocol',
    'document_input_protocol', 'document_read_window_protocol', 'document_working_notes_protocol',
    'document_working_notes_required', 'indexed_documents'}


def render(source):
    return '# 工程师本轮完整材料\n\n原稿、原意见及冻结来源；不是审核批准或实验结果。\n\n```json\n' + json.dumps(
        source, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n```\n'


def prepare(engine, harness, run_id, context):
    """Save source bytes once, keep tool authorization host-side, expose indexes."""
    from .implementation_documents import _save
    source = {k: copy.deepcopy(v) for k, v in context.items() if k not in CONTROLS and not k.startswith('_')}
    text = render(source)
    document = _save(engine, harness, run_id, 'scientific-author-materials/v1/' + full.sha(source) + '.md',
        text, 'scientific_author_materials')
    result = copy.deepcopy(context)
    result.update({KEY: 1, MATERIAL: {'version': 1, 'document': document, 'source_sha256': full.sha(source),
        'source_fields': sorted(source)}, 'indexed_document_context': True, 'document_index_protocol': 1,
        'document_input_protocol': 2, 'document_read_window_protocol': 2,
        'document_working_notes_protocol': 1, 'document_working_notes_required': False})
    result['indexed_documents'] = [document, *[copy.deepcopy(d) for d in context.get('indexed_documents', [])
        if d['document_id'] != document['document_id']]]
    return result


def project(context):
    if context.get(KEY) != 1 or context.get(MATERIAL, {}).get('version') != 1:
        raise ValueError('Unknown scientific author material protocol')
    allowed = CONTROLS | {'tool_results', 'document_read_window', 'document_read_fragments',
        'document_working_notes', 'document_working_notes_index', 'working_notes', 'read_index'}
    result = {k: copy.deepcopy(v) for k, v in context.items() if k in allowed or k.startswith('_')}
    result['scientific_author_guidance'] = ('你是统一撰写和修订的工程师，不是本轮独立审核者。'
        '原稿、所有审核意见与冻结材料在scientific_author_materials.document中；通过documents.read逐页读完，'
        '按需核对其他索引来源。单页最多6000字符，每次最多50000字符。'
        '原页和原回复保留；补读只需读取missing_ranges。完成后按原schema返回完整交付，不声明审核批准或新实验。')
    return result


def _base(context):
    return {k: v for k, v in context.items() if k != READING}


def handoffs(engine, run_id, request, answer):
    """Authenticate the complete same-author, same-source continuation chain."""
    from .diagnostic_planning_recovery import authenticate
    from .engine import Harness
    source = _base(request['context'])
    payload = {k: request[k] for k in ('instruction', 'context', 'schema')}
    purpose = request['purpose']
    root_purpose = request['context'].get(READING, {}).get('root_purpose', purpose)
    result, visited = copy.deepcopy(answer), []
    while True:
        hid = authenticate(engine, run_id, 'engineer', purpose, payload, result)
        if (engine.store.get('decisions',hid) or {}).get('run_id') != run_id:
            raise ValueError('Scientific author decision belongs to another run')
        if hid in visited or len(visited) >= 18 or _base(payload['context']) != source:
            raise ValueError('Scientific author source chain changed ownership or material')
        if purpose != root_purpose and not purpose.startswith(root_purpose + ':source-read:'):
            raise ValueError('Scientific author source chain changed purpose')
        visited.append(hid)
        parent = payload['context'].get(READING)
        if parent is None:
            if purpose != root_purpose:
                raise ValueError('Scientific author source chain lost its root')
            return visited
        if parent.get('version') != 1 or parent.get('root_purpose') != root_purpose:
            raise ValueError('Unknown scientific author reading continuation')
        prior = engine.store.get('decisions', parent.get('parent_handoff_id', '')) or {}
        frozen = engine.store.get('handoff_inputs', parent.get('parent_handoff_id', '')) or {}
        if full.sha(prior.get('result')) != parent.get('parent_result_sha256'):
            raise ValueError('Scientific author parent answer changed')
        payload = {k: frozen.get(k) for k in ('instruction', 'context', 'schema')}
        if payload['instruction'] != request['instruction'] or payload['schema'] != request['schema']:
            raise ValueError('Scientific author continuation changed the frozen contract')
        purpose, result = prior.get('purpose'), prior.get('result')
        if Harness.decision_key(run_id, 'engineer', purpose, **payload) != parent['parent_handoff_id']:
            raise ValueError('Scientific author continuation refers to another dispatch')


def check(engine, run_id, request, answer):
    from . import document_index
    from .evidence_delivery import tracking_required
    from .implementation_reading import coverage
    context = request['context']
    if context.get(KEY) != 1:
        raise ValueError('Unknown scientific author protocol')
    marker = context.get(MATERIAL, {})
    if marker.get('version') != 1 or marker.get('source_fields') != sorted(set(marker.get('source_fields', []))):
        raise ValueError('Unknown scientific author source manifest')
    source = {k: context[k] for k in marker['source_fields']}
    document = marker['document']
    _, text = document_index.load(engine, run_id, document['document_id'], document['sha256'])
    if full.sha(source) != marker['source_sha256'] or text != render(source):
        raise ValueError('Scientific author indexed bytes differ from the frozen material')
    ids = handoffs(engine, run_id, request, answer)
    # Old untracked acquisition never substitutes for submitted source delivery.
    ids = [hid for hid in ids if tracking_required(engine.store, run_id, hid)]
    required = [{'result': {'section_id': s['id'], 'char_start': s['start'], 'char_end': s['end']}}
        for s in document['sections'] if s['end'] > s['start']]
    return coverage(engine, run_id, document, required, ids, include_memory=False)


def validate(engine, run_id, request):
    """Replay authenticates source delivery again; saved answers are not credit."""
    from .diagnostic_planning_recovery import authenticate
    authenticate(engine,run_id,'engineer',request['purpose'],
        {k:request[k] for k in ('instruction','context','schema')},request['answer'])
    if KEY in request['context'] and not check(engine,run_id,request,request['answer'])['complete']:
        raise ValueError('Scientific author source no longer has complete submitted delivery')


async def ask(engine, harness, run_id, request):
    """Resume exact requests; missing submitted pages get ordinary author work."""
    from .diagnostic_planning_recovery import authenticate
    from .engine import Harness, ResearchBlocked
    root = copy.deepcopy(request)
    if KEY not in root['context']:
        answer = root.get('answer')
        if answer is None:
            answer = await harness.ask(run_id, 'engineer', **{k: root[k] for k in ('purpose','instruction','context','schema')})
        authenticate(engine, run_id, 'engineer', root['purpose'], {k:root[k] for k in ('instruction','context','schema')}, answer)
        return {**root, 'answer': answer}
    base = _base(root['context'])
    root_purpose = root['context'].get(READING, {}).get('root_purpose', root['purpose'])
    current, previous = root, -1
    for attempt in range(17):
        engine.check_active(run_id)
        payload = {k:current[k] for k in ('instruction','context','schema')}
        hid = Harness.decision_key(run_id, 'engineer', current['purpose'], **payload)
        saved = engine.store.get('decisions', hid)
        if saved and hasattr(harness,'_moderation_gate'):
            harness._moderation_gate(run_id)
        answer = copy.deepcopy(saved['result']) if saved else await harness.ask(run_id, 'engineer', current['purpose'], **payload)
        authenticate(engine, run_id, 'engineer', current['purpose'], payload, answer)
        covered = check(engine, run_id, current, answer)
        if covered['complete']:
            return {**current, 'answer': answer}
        if attempt == 16 or (attempt and covered['covered_chars'] <= previous):
            raise ResearchBlocked('工程师原稿和修改意见补读未收敛，原输入、答复及交付回执保留。')
        previous = covered['covered_chars']
        current = {k: copy.deepcopy(root[k]) for k in ('instruction','schema')}
        current['purpose'] = root_purpose + ':source-read:' + str(attempt + 1)
        current['context'] = copy.deepcopy(base)
        current['context'][READING] = {'version':1,'root_purpose':root_purpose,'parent_handoff_id':hid,
            'parent_result_sha256':full.sha(answer),'missing_ranges':covered['missing_ranges'],
            'covered_chars':covered['covered_chars'], 'instruction':'复用本工程师同版本已提交页，只补读缺失范围后给完整交付。'}
