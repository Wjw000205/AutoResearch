"""A same-author citation-only amendment over an authenticated saved review.

The original review handoff owns all reading credit. The amendment can select
only approved-source ranges actually read AND delivered in that same review.
No copied receipt becomes a new read, and no vote or scientific text is edited.
Both immutable model deliveries reconstruct the effective packet on replay.
"""
from __future__ import annotations

import copy

from . import implementation_acceptance as policy
from .implementation_review_routing import digest

KEY = 'implementation_acceptance_citation_correction'
INSTRUCTION = ('你是原独立审查报告的作者。本次仅补齐 target_checks 中各 PASS 缺失的批准架构来源引用。'
    'source_evidence 是宿主从你原交接真实读取、实际投递且版本匹配的原文恢复的精确片段；不是其他角色的阅读信用。'
    '根据各原结论及冻结验收要求，选择语义相关的 architecture_design 片段，填写精确引用和关联理由。'
    '只使用这里已有证据，不重新通读全文，不修改原 PASS/FAIL/UNKNOWN、问题、关闭条件、说明或其他引用。'
    '不能为了通过校验选无关来源。若现有片段不足以支持某项，返回 insufficient_evidence 和原因；不得补造证据。'
    '本次是带原始读取出处的引用补充，不是新的独立审查票、代码批准或实验授权。按给定 schema 输出。')


def _targets(packet):
    sources = {d['document_id'] for d in packet['context']['indexed_documents'] if d['kind'] == 'architecture_design'}
    return [c['criterion_id'] for c in packet['result']['checks'] if c['status'] == 'PASS'
            and not any(r['document_id'] in sources for r in c['evidence'])]


def _purpose(engine, source):
    handoff = engine.store.get('handoffs', source['handoff_id'], {})
    return handoff.get('purpose', '') + ':source-citation-correction:' + digest(source['handoff_id'])[:16]


def correction_context(engine, run_id, source):
    from . import implementation_acceptance_flow as flow
    from .document_index import load
    if 'citation_correction' in source or KEY in source['context']:
        policy.fail('同一原报告的批准来源引用只能定向补充一次。')
    context = flow.check_packet(engine, run_id, source)
    scope = context[policy.CONTEXT]
    targets = _targets(source)
    if not targets:
        policy.fail('原报告没有待补充的批准来源引用。')
    documents = {d['document_id']:d for d in context['indexed_documents']}
    approved = {did for did,d in documents.items() if d['kind'] == 'architecture_design'}
    progress = engine.store.get('handoff_progress', source['handoff_id'], {})
    receipts = progress.get('tool_results', [])
    refs = [r for c in source['result']['checks'] if c['criterion_id'] in targets for r in c['evidence']]
    for receipt in receipts:
        page = receipt.get('result', {})
        if (receipt.get('call', {}).get('name') != 'documents.read' or page.get('error')
                or page.get('document_id') not in approved or not isinstance(page.get('text'), str)):
            continue
        doc = documents[page['document_id']]
        section = next((s for s in doc['sections'] if s['id'] == page.get('section_id')), None)
        if not section or page.get('sha256') != doc['sha256']:
            policy.fail('批准来源读取记录与冻结版本不一致。')
        refs.append({'document_id': doc['document_id'], 'document_sha256': doc['sha256'],
            'section_id': section['id'], 'offset': page['char_start']-section['start'],
            'limit': page['char_end']-page['char_start']})
    refs = list({digest(ref):ref for ref in refs}.values())
    # Authenticate actual original-role delivery as well as local source bytes.
    # No source text is supplied merely because it exists in the document index.
    flow.validate_refs(engine, run_id, source['handoff_id'], context, refs, field='citation_correction.source_evidence')
    if not any(r['document_id'] in approved for r in refs):
        policy.fail('原交接没有实际读取并投递的批准来源，不能只补引用。')
    evidence, cache = [], {}
    for ref in refs:
        did = ref['document_id']; doc = documents[did]
        if did not in cache:
            cache[did] = load(engine, run_id, did, doc['sha256'])[1]
        section = next(s for s in doc['sections'] if s['id'] == ref['section_id'])
        start = section['start'] + ref['offset']; end = start + ref['limit']
        evidence.append({'reference': copy.deepcopy(ref), 'kind':doc['kind'], 'title':doc['title'],
            'text':cache[did][start:end], 'origin_handoff_id':source['handoff_id'],
            'provenance':'authenticated_original_review_read', 'new_reading_credit':False, 'untrusted':True})
    payload = {'version':1, 'source_handoff_id':source['handoff_id'], 'role':source['role'],
        'source_result_sha256':digest(source['result']), 'source_read_ledger_sha256':digest(receipts),
        'document':scope['document'], 'spec_hash':scope['spec_hash'], 'contract_sha256':scope['contract']['sha256'],
        'target_checks':[copy.deepcopy(c) for c in source['result']['checks'] if c['criterion_id'] in targets],
        'criteria':[copy.deepcopy(c) for c in scope['contract']['criteria'] if c['id'] in targets],
        'source_evidence':evidence, 'attempt':1, 'attempt_limit':1, 'scientific_approval':False}
    return {policy.PROTOCOL:1, 'acceptance_workflow_id':context['acceptance_workflow_id'],
        'acceptance_budget':copy.deepcopy(context['acceptance_budget']), 'document_input_protocol':2,
        'document_index_protocol':1, 'indexed_document_context':True, 'document_read_window_protocol':3,
        'indexed_documents':[copy.deepcopy(d) for d in documents.values()
                             if d['document_id'] in approved or d['document_id']==scope['document']['document_id']],
        KEY:payload}


def schema(context):
    entry = policy.obj({'status':{'enum':['supported','insufficient_evidence']},
        'evidence':policy.array(policy.REF, 0, 12), 'reason':policy.TEXT})
    return policy.obj({'source_result_sha256':{'const':context[KEY]['source_result_sha256']},
        'citations':policy.obj({c['criterion_id']:copy.deepcopy(entry) for c in context[KEY]['target_checks']})})


def apply(source, amendment, context):
    """Deterministically append selected citations, preserving every judgment."""
    payload = context[KEY]
    if (amendment.get('source_result_sha256') != digest(source['result'])
            or set(amendment.get('citations', {})) != set(_targets(source))):
        policy.fail('引用补充没有唯一绑定原报告及全部缺失项。')
    allowed = [e['reference'] for e in payload['source_evidence'] if e['kind']=='architecture_design']
    result = copy.deepcopy(source['result'])
    for check in result['checks']:
        extra = amendment['citations'].get(check['criterion_id'])
        if extra is None:
            continue
        if (set(extra) != {'status','evidence','reason'} or extra['status'] != 'supported'
                or not extra['evidence'] or not isinstance(extra['reason'],str) or not extra['reason'].strip()):
            policy.fail('原审查者未能补齐 ' + check['criterion_id'] + ' 的批准来源：' + str(extra.get('reason','')))
        for ref in extra['evidence']:
            if not any(all(ref.get(k)==r[k] for k in ('document_id','document_sha256','section_id'))
                       and type(ref.get('offset')) is int and type(ref.get('limit')) is int
                       and 0 < ref['limit'] <= 6000 and r['offset'] <= ref['offset']
                       and ref['offset']+ref['limit'] <= r['offset']+r['limit'] for r in allowed):
                policy.fail('补充引用超出原审查者实际收到的批准来源范围。')
            if ref not in check['evidence']:
                check['evidence'].append(copy.deepcopy(ref))
    return result


def validate_packet(engine, run_id, packet):
    """Rebuild from two immutable model deliveries; trust no derived payload."""
    from . import implementation_acceptance_flow as flow
    marker = packet.get('citation_correction')
    if not isinstance(marker,dict) or marker.get('version') != 1:
        policy.fail('引用补充出处无效。')
    source = {k:copy.deepcopy(packet[k]) for k in ('handoff_id','role','context')}
    source['result'] = marker.get('original_report')
    context = correction_context(engine, run_id, source)
    amendment = marker.get('amendment', {})
    if amendment.get('role') != source['role'] or 'citation_correction' in amendment:
        policy.fail('引用补充必须由原审查角色完成一次。')
    supplied = flow.check_packet(engine, run_id, amendment)
    handoff = engine.store.get('handoffs', amendment['handoff_id'], {})
    if supplied != context or handoff.get('purpose') != _purpose(engine, source):
        policy.fail('引用补充改变了原任务、冻结范围或读取出处。')
    if packet['result'] != apply(source, amendment['result'], context):
        policy.fail('引用补充改变了原报告的结论或非引用字段。')
    return source['context']


async def correct(engine, h, run_id, state, row, source, diagnostic):
    from . import implementation_acceptance_flow as flow
    if diagnostic != {'version':1,'kind':'missing_approved_source_references','criteria':_targets(source)}:
        policy.fail('批准来源引用修正诊断与原报告不一致。')
    context = await engine._read_consensus_check(run_id, correction_context, engine, run_id, source)
    amendment = await flow.ask(engine, h, run_id, state, _purpose(engine, source), source['role'],
                               INSTRUCTION, context, schema(context))
    result = apply(source, amendment['result'], context)
    packet = {**copy.deepcopy(source), 'result':result, 'citation_correction':{
        'version':1, 'original_report':copy.deepcopy(source['result']), 'amendment':amendment}}
    await engine._read_consensus_check(run_id, flow.validate_report, engine, run_id, row, packet)
    return packet
