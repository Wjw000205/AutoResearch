"""Fresh correction inputs reuse only exact original-role submitted source spans."""
import copy

from .implementation_review_routing import digest
from . import implementation_acceptance as policy
from . import implementation_document_review_scope as scoped


def bind(engine, run_id, source, context):
    from . import supplemental_reads
    from .implementation_report_amendments import _row
    from .evidence_delivery import tracking_required
    from .implementation_acceptance_reading import PLAN
    from .implementation_review_reads import KEY as READ_BATCH
    context[PLAN] = 1
    context[READ_BATCH] = 1
    row = _row(engine, run_id, source)
    context[scoped.KEY]['proof_field_contract'] = {
        'version': 1, 'fields': sorted(row['spec']), 'spec_hash': row['spec_hash']}
    if tracking_required(engine.store, run_id, source['handoff_id']):
        origin = {'role': source['role'], 'handoff_id': source['handoff_id'],
            'input_sha256': digest(engine.store.get('handoff_inputs', source['handoff_id'])),
            'progress_sha256': digest(engine.store.get('handoff_progress', source['handoff_id']))}
        context['implementation_acceptance_delivery_repair']['reuse_origin'] = origin
    else:
        context['implementation_acceptance_delivery_repair'].pop('reuse_origin', None)
    if supplemental_reads.PROTOCOL in context:
        supplemental_reads.enabled(context)
    return supplemental_reads.opt_in(context)


def pages(engine, run_id, hid, context, char_budget):
    """Read again locally, never call a model or copy another role's reading credit."""
    from .implementation_delivery_correction import REPAIR
    from .implementation_review_reads import authenticate_origin
    from .implementation_reading import coverage
    from .implementation_acceptance_flow import check_packet
    from . import document_index, supplemental_reads
    marker = context.get(REPAIR, {})
    if (marker.get('version') != 2 or not marker.get('reuse_origin')
            or not context[policy.CONTEXT]['assigned_criteria']):
        return []
    role = engine.store.get('handoffs', hid, {}).get('to_role')
    origin = marker['reuse_origin']
    if origin['role'] != role or origin['handoff_id'] == hid:
        policy.fail('Correction read reuse changed its original role')
    frozen = authenticate_origin(engine, run_id, role, origin)
    parent = {'handoff_id': origin['handoff_id'], 'role': role, 'context': frozen['context'],
              'result': marker['original_result']}
    parent.update({k: copy.deepcopy(marker[k]) for k in ('citation_correction', 'report_amendment') if k in marker})
    check_packet(engine, run_id, parent)
    if (marker['source_handoff_id'] != origin['handoff_id'] or
            marker['source_result_sha256'] != digest(parent['result'])):
        policy.fail('Correction read reuse lost its exact saved source')
    old_docs = {(d['document_id'], d['sha256']) for d in frozen['context'].get('indexed_documents', [])}
    receipts, used = [], 0
    old_receipts = engine.store.get('handoff_progress', origin['handoff_id'], {}).get('tool_results', [])
    documents = {(d['document_id'], d['sha256']): d for d in context['indexed_documents']}
    for doc in documents.values():
        if (doc['document_id'], doc['sha256']) not in old_docs:
            continue
        required = [{'result': {'section_id': s['id'], 'char_start': s['start'], 'char_end': s['end']}}
                    for s in doc['sections']]
        actual = coverage(engine, run_id, doc, required, [origin['handoff_id']])
        bases = {s['id']: s['start'] for s in doc['sections']}
        for span in actual['covered_ranges']:
            for start in range(span['start'], span['end'], 6000):
                count = min(6000, span['end'] - start, char_budget - used)
                if count <= 0:
                    return receipts
                args = {'document_id': doc['document_id'], 'expected_sha256': doc['sha256'],
                        'section_id': span['section_id'], 'offset': start-bases[span['section_id']], 'limit': count}
                if scoped.permitted_read(context, args):
                    call = {'name': 'documents.read', 'arguments': args}
                    result = document_index.read(engine, run_id, args)
                else:
                    current = context[policy.CONTEXT]['document']
                    if (doc.get('kind') not in supplemental_reads.SOURCE_KINDS and
                            (doc['document_id'], doc['sha256']) != (current['document_id'], current['sha256'])):
                        continue
                    criteria = set(context[policy.CONTEXT]['assigned_criteria'])
                    candidates = [r['call']['arguments'].get('requirement_id') for r in old_receipts
                        if r.get('call', {}).get('name') == supplemental_reads.TOOL and all(
                            r['call']['arguments'].get(k) == args[k] for k in ('document_id', 'expected_sha256', 'section_id'))
                        and r['call']['arguments']['offset'] < args['offset'] + count
                        and args['offset'] < r['call']['arguments']['offset'] + r['call']['arguments']['limit']]
                    candidates += [c['criterion_id'] for c in parent['result']['checks'] if any(
                        ref['document_id'] == doc['document_id'] and ref['document_sha256'] == doc['sha256']
                        and ref['section_id'] == span['section_id'] and ref['offset'] < args['offset'] + count
                        and args['offset'] < ref['offset'] + ref['limit'] for ref in c['evidence'])]
                    cid = next((cid for cid in candidates if cid in criteria), None)
                    if cid is None:
                        continue  # Do not invent scientific relevance for an unbound old page.
                    request = {**args, 'kind': 'document', 'requirement_id': cid,
                               'reason': '恢复本角色原交接已实际交付的同版本原文；补正不转移职责或预授批准。'}
                    call = {'name': supplemental_reads.TOOL, 'arguments': request}
                    result = supplemental_reads.execute(engine, run_id, role, hid, request)
                receipts.append({'call': call, 'result': result, 'correction_read_provenance': copy.deepcopy(origin)})
                used += count
    return receipts
