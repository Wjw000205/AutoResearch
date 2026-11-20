"""Bounded same-author amendments over immutable, actually delivered reviews.

Only declared output defects are editable. Original reading remains attributed
to its original handoff; source bytes are reauthenticated before every replay.
The adapter produces a provenance-bound view, never rewrites a saved decision.
"""
from __future__ import annotations

import copy
from contextvars import ContextVar

from . import implementation_acceptance as policy
from .implementation_review_routing import digest
from .implementation_acceptance_flow import snapshot_check

KEY = 'implementation_report_amendment'
MARKER = 'report_amendment'
_checking = ContextVar('report_amendment_checking', default=())
INSTRUCTION = ('你是原验收报告的作者。本次只补正 targets 明确列出的输出缺陷。'
    'source_evidence 是宿主重新核验版本、正文和你原交接实际交付后恢复的原文，不是他人的阅读信用。'
    '复用这些原文；不要重做其他验收项，不重新遍历材料，不改原判断、问题、关闭条件或科学标准。'
    'dependency_fields 只能选择 proof_field_contract.fields 中的顶层字段名，不能填写文件路径或端点。'
    '无法证明完整依赖的可选 unit_proof 可返回 null，表示不申请逐单元复用，不影响原岗位判断。'
    '证据补正只选择语义相关的 source_evidence.id；原有引用由宿主保留。'
    '如果需要新原文或修改科学判断，resolution=needs_review 并说明缺口，交回正常补读复核流程。'
    '按 schema 返回本次局部修改；宿主将重建完整报告并执行全部原校验，补正本身不表示验收通过。')


class NeedsFreshReview(Exception):
    """An output-only amendment cannot supply missing scientific evidence."""


def provenance(packet):
    return {k: copy.deepcopy(packet[k]) for k in ('citation_correction', MARKER) if k in packet}


def restore_provenance(packet, marker):
    value = marker.get('source_provenance', {})
    if not isinstance(value, dict) or set(value) - {'citation_correction', MARKER}:
        policy.fail('Recovery changed its original report provenance')
    packet.update(copy.deepcopy(value))
    return packet


def _row(engine, run_id, source):
    scope = source['context'][policy.CONTEXT]
    state = engine.store.get('codex_workflows', source['context'].get('acceptance_workflow_id'), {})
    rows = [r for r in state.get('specifications', []) if r['document'] == scope['document']
            and r['spec_hash'] == scope['spec_hash']]
    if state.get('run_id') != run_id or len(rows) != 1:
        raise NeedsFreshReview('Original review implementation is unavailable or changed')
    return rows[0]


def _targets(source, diagnostic, row):
    from . import implementation_document_review_scope as scoped
    scope = source['context'][scoped.KEY]
    fields = scoped.proof_fields(source['context'], row=row)
    reason = diagnostic.get('reason')
    targets = {'proofs': [], 'checks': []}
    if reason == 'Unit proof exceeds frozen catalog':
        assigned = {a['id'] for a in scope['assignment']['units']}
        catalog = {a['id'] for a in scope['unit_catalog']}
        proofs = source['result'].get('unit_proofs', [])
        if len({p['unit_id'] for p in proofs}) != len(proofs):
            raise NeedsFreshReview('Duplicate unit identities require ordinary review')
        for proof in proofs:
            ids = [proof['unit_id'], *proof['dependency_unit_ids']]
            if proof['unit_id'] not in assigned or not set(ids) <= catalog:
                raise NeedsFreshReview('Unit ownership cannot be inferred by an amendment')
            mutable = []
            if (not set(proof['dependency_fields']) <= set(fields) or
                    scope.get('proof_field_contract') is not None and
                    len(set(proof['dependency_fields'])) != len(proof['dependency_fields'])):
                mutable.append('dependency_fields')
            if len(set(proof['dependency_unit_ids'])) != len(proof['dependency_unit_ids']):
                mutable.append('dependency_unit_ids')
            if mutable:
                targets['proofs'].append({'unit_id': proof['unit_id'], 'mutable_fields': mutable,
                                         'original': copy.deepcopy(proof)})
    elif reason == 'Scoped PASS lacks evidence at its assigned assertion or endpoint':
        approved = {d['document_id'] for d in source['context']['indexed_documents']
                    if d['kind'] == 'architecture_design'}
        if any(c['status'] == 'PASS' and not any(r['document_id'] in approved for r in c['evidence'])
               for c in source['result']['checks']):
            raise NeedsFreshReview('Missing approved-source support needs ordinary source review')
        current = source['context'][policy.CONTEXT]['document']
        from .implementation_acceptance_flow import _check_scoped_assertion_evidence
        from .engine import ResearchBlocked
        try:
            _check_scoped_assertion_evidence(current, source['context'][policy.CONTEXT], source['result']['checks'])
        except ResearchBlocked as exc:
            actual = getattr(exc, 'acceptance_delivery_error', {})
            for item in actual.get('missing_assertions', []):
                original = next(c for c in source['result']['checks'] if c['criterion_id'] == item['criterion_id'])
                targets['checks'].append({**copy.deepcopy(item), 'original': copy.deepcopy(original)})
    if not any(targets.values()):
        raise NeedsFreshReview('This diagnostic has no supported output-only amendment')
    return targets, fields


def _references(source, targets):
    refs = [r for t in targets['proofs'] for r in t['original']['evidence']]
    doc = source['context'][policy.CONTEXT]['document']
    for target in targets['checks']:
        for section in target['sections']:
            for offset, limit in section['offset_limit_pairs']:
                for start in range(offset, offset + limit, 6000):
                    refs.append({'document_id': doc['document_id'], 'document_sha256': doc['sha256'],
                        'section_id': section['section_id'], 'offset': start, 'limit': min(6000, offset + limit - start)})
    return list({digest(ref): ref for ref in refs}.values())


@snapshot_check
def correction_context(engine, run_id, source, diagnostic):
    from . import implementation_acceptance_flow as flow
    from .document_index import load
    from .implementation_reading import coverage
    context = flow.check_packet(engine, run_id, source)
    from .evidence_delivery import tracking_required
    if not tracking_required(engine.store, run_id, source['handoff_id']):
        raise NeedsFreshReview('A retrieval-only legacy ledger cannot establish original model delivery')
    row = _row(engine, run_id, source)
    targets, fields = _targets(source, diagnostic, row)
    docs = {d['document_id']: d for d in context['indexed_documents']}
    evidence, bodies = [], {}
    for ref in _references(source, targets):
        doc = docs.get(ref['document_id'])
        section = next((s for s in doc['sections'] if s['id'] == ref['section_id']), None) if doc else None
        if (not doc or doc['sha256'] != ref['document_sha256'] or not section
                or type(ref['offset']) is not int or type(ref['limit']) is not int
                or not 0 <= ref['offset'] < ref['offset'] + ref['limit'] <= section['chars']
                or not 1 <= ref['limit'] <= 6000):
            raise NeedsFreshReview('A model proof reference needs ordinary source clarification')
        start, stop = section['start'] + ref['offset'], section['start'] + ref['offset'] + ref['limit']
        if not coverage(engine, run_id, doc, [{'result': {'section_id': section['id'],
                'char_start': start, 'char_end': stop}}], [source['handoff_id']])['complete']:
            raise NeedsFreshReview('Decisive original text was not actually delivered to this author')
        from .engine import ResearchBlocked
        try:
            flow.validate_refs(engine, run_id, source['handoff_id'], context, [ref], report_evidence=True,
                               field='report_amendment.source_evidence')
        except ResearchBlocked as exc:
            if getattr(exc, 'acceptance_delivery_error', None) is not None:
                raise NeedsFreshReview('Original proof references require ordinary author correction') from exc
            raise
        if doc['document_id'] not in bodies:
            bodies[doc['document_id']] = load(engine, run_id, doc['document_id'], doc['sha256'])[1]
        evidence.append({'id': 'E' + str(len(evidence)), 'reference': copy.deepcopy(ref),
            'text': bodies[doc['document_id']][start:stop], 'untrusted': True,
            'origin_handoff_id': source['handoff_id'], 'new_reading_credit': False})
    receipts = engine.store.get('handoff_progress', source['handoff_id'], {}).get('tool_results', [])
    result = {policy.PROTOCOL: 1, 'acceptance_workflow_id': context['acceptance_workflow_id'],
        'acceptance_budget': copy.deepcopy(context['acceptance_budget']), KEY: {
            'version': 1, 'source_handoff_id': source['handoff_id'], 'role': source['role'],
            'source_result_sha256': digest(source['result']), 'source_read_ledger_sha256': digest(receipts),
            'document': {'document_id': row['document']['document_id'], 'sha256': row['document']['sha256']},
            'spec_hash': row['spec_hash'], 'contract_sha256': context[policy.CONTEXT]['contract']['sha256'],
            'review_round': row['index'] + 1,
            'diagnostic': copy.deepcopy(diagnostic), 'targets': targets,
            'proof_field_contract': {'version': 1, 'fields': fields},
            'source_evidence': evidence, 'scientific_approval': False}}
    return result


def within_input_allocation(engine, run_id, context):
    """Preflight new dispatch only; resource changes never invalidate old replies."""
    from .intelligence import token_estimate
    import json
    configured = engine.store.get('runs', run_id)['config'].get('context_tokens', 32000)
    projected = token_estimate(INSTRUCTION + json.dumps(context, ensure_ascii=False)
                               + json.dumps(schema(context), ensure_ascii=False) * 2)
    return projected + output_limit(context, 2000) <= configured // 2


def schema(context):
    payload = context[KEY]
    proofs = {}
    for target in payload['targets']['proofs']:
        props = {}
        for field in target['mutable_fields']:
            if field == 'dependency_unit_ids':
                # Removing duplicates cannot remove a real dependency or alter
                # its order. Otherwise selective carry could miss invalidation.
                props[field] = {'const': list(dict.fromkeys(target['original'][field]))}
                continue
            values = (payload['proof_field_contract']['fields'] if field == 'dependency_fields'
                      else sorted(set(target['original']['dependency_unit_ids'])))
            props[field] = {**policy.array({'enum': values}), 'uniqueItems': True} if values else {
                'type': 'array', 'maxItems': 0, 'items': False}
            retained = sorted(set(target['original'][field]) & set(values))
            if retained:
                props[field]['allOf'] = [{'contains': {'const': value}} for value in retained]
        proofs[target['unit_id']] = {'anyOf': [{'type': 'null'}, policy.obj(props)]}
    ids = [e['id'] for e in payload['source_evidence']]
    selected = ({**policy.array({'enum': ids}), 'uniqueItems': True} if ids else
                {'type': 'array', 'maxItems': 0, 'items': False})
    return policy.obj({'source_result_sha256': {'const': payload['source_result_sha256']},
        'resolution': {'enum': ['corrected', 'needs_review']}, 'reason': policy.TEXT,
        'proofs': policy.obj(proofs), 'check_evidence': policy.obj({
            t['criterion_id']: copy.deepcopy(selected) for t in payload['targets']['checks']})})


def output_limit(context, base=0):
    payload = context[KEY]
    if not isinstance(payload, dict) or type(payload.get('version')) is not int or payload['version'] != 1:
        policy.fail('Unsupported report amendment output protocol')
    # A conservative ASCII reply bound; normal provider/resource admission may
    # reject this allowance, but cannot silently truncate the editable inventory.
    import json
    example = {'source_result_sha256': payload['source_result_sha256'], 'resolution': 'corrected', 'reason': 'Correction',
        'proofs': {t['unit_id']: {field: (payload['proof_field_contract']['fields'] if field == 'dependency_fields'
                                      else list(dict.fromkeys(t['original'][field])))
                                  for field in t['mutable_fields']} for t in payload['targets']['proofs']},
        'check_evidence': {t['criterion_id']: [e['id'] for e in payload['source_evidence']]
                           for t in payload['targets']['checks']}}
    return max(base, 512 + len(json.dumps(example, ensure_ascii=True)))


def apply(source, amendment, context):
    from .tool_contracts import StrictToolValidator
    if not StrictToolValidator(schema(context)).is_valid(amendment):
        policy.fail('Report amendment exceeds its frozen editable fields')
    if amendment['resolution'] != 'corrected':
        raise NeedsFreshReview(amendment['reason'])
    result = copy.deepcopy(source['result'])
    replacements = amendment['proofs']
    if replacements:
        result['unit_proofs'] = [{**copy.deepcopy(p), **copy.deepcopy(replacements.get(p['unit_id'], {}))}
            for p in result.get('unit_proofs', []) if replacements.get(p['unit_id'], {}) is not None]
    refs = {e['id']: e['reference'] for e in context[KEY]['source_evidence']}
    for check in result['checks']:
        for identity in amendment['check_evidence'].get(check['criterion_id'], []):
            if refs[identity] not in check['evidence']:
                check['evidence'].append(copy.deepcopy(refs[identity]))
    return result


def original_schema_valid(engine, source, result):
    from .tool_contracts import StrictToolValidator
    frozen = engine.store.get('handoff_inputs', source['handoff_id'], {})
    return StrictToolValidator(frozen.get('schema', False)).is_valid(result)


def _purpose(engine, source, diagnostic):
    return (engine.store.get('handoffs', source['handoff_id'])['purpose']
            + ':report-amendment-v1:' + digest([source['result'], diagnostic])[:16])


def validate_packet(engine, run_id, packet):
    from . import implementation_acceptance_flow as flow
    marker = packet.get(MARKER)
    if (not isinstance(marker, dict) or set(marker) != {'version', 'original_report', 'previous', 'amendment'}
            or type(marker['version']) is not int or marker['version'] != 1):
        policy.fail('Invalid report amendment provenance')
    trace = (packet['handoff_id'], digest(marker))
    active = _checking.get()
    if trace in active or len(active) >= 16:
        policy.fail('Cyclic report amendment lineage')
    token = _checking.set((*active, trace))
    try:
        source = {k: copy.deepcopy(packet[k]) for k in ('handoff_id', 'role', 'context')}
        if not isinstance(marker['previous'], dict) or set(marker['previous']) - {'citation_correction', MARKER}:
            policy.fail('Report amendment changed its prior provenance')
        source.update(copy.deepcopy(marker['previous']), result=marker['original_report'])
        amendment = marker['amendment']
        supplied = flow.check_packet(engine, run_id, amendment)
        payload = supplied.get(KEY, {})
        context = correction_context(engine, run_id, source, payload.get('diagnostic', {}))
        if (amendment['role'] != source['role'] or supplied != context or
                engine.store.get('handoffs', amendment['handoff_id']).get('purpose') !=
                _purpose(engine, source, payload['diagnostic'])):
            policy.fail('Report amendment changed author, source, contract or exact editable scope')
        if (packet['result'] != apply(source, amendment['result'], context)
                or not original_schema_valid(engine, source, packet['result'])):
            policy.fail('Report amendment changed an untouched scientific judgment or field')
        return source['context']
    finally:
        _checking.reset(token)


