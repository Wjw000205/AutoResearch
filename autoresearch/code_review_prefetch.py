"""Retrieve an already validated missing-read plan without another model turn.

These are same-role, version-bound retrieval receipts. Only actual subsequent
model delivery and independent judgment can close the original evidence gap.
"""
from .architecture_validation import operation
from .code_review_context import _frozen


@operation
def retrieve(engine, run_id, role, key, context, diagnostic, progress):
    from . import document_index, code_navigation
    from .code_repair_contract import sha
    frozen = _frozen(engine, run_id, role, key)['context']
    docs = {d['document_id']: d for d in frozen.get('indexed_documents', [])}
    files = {d['artifact_id']: d for d in frozen.get('delivery_artifacts', [])}
    missing = diagnostic.get('alignment_read_correction', {})
    seen = {sha(r['call']) for r in progress.get('tool_results', []) if isinstance(r.get('call'), dict)}
    budget = min(72000, max(0, engine.store.get('runs', run_id).get('config', {}).get('context_tokens', 32000) // 4))
    refs = [(kind, ref) for kind in ('source_refs', 'implementation_refs', 'code_refs')
            for ref in missing.get(kind, [])]
    rows, chars = [], 0
    for kind, ref in refs:
        offset, count = ref.get('offset'), ref.get('limit')
        if type(offset) is not int or type(count) is not int or offset < 0 or count <= 0:
            continue  # The original diagnostic remains for coordinate correction.
        if kind == 'code_refs':
            source = files.get(ref.get('artifact_id'))
            if not source or source['sha256'] != ref.get('sha256'):
                continue
            if offset + count > len(code_navigation.body(engine, run_id, frozen, source)):
                continue
            args = {'id': source['artifact_id'], 'expected_sha256': source['sha256']}
            name = 'code.read'
        else:
            source = docs.get(ref.get('document_id'))
            if not source or source['sha256'] != ref.get('document_sha256'):
                continue
            section = next((s for s in source.get('sections', []) if s['id'] == ref.get('section_id')), None)
            if not section or offset + count > section['end'] - section['start']:
                continue
            args = {'document_id': source['document_id'], 'expected_sha256': source['sha256'],
                    'section_id': ref['section_id']}
            name = 'documents.read'
        cursor = offset
        while cursor < offset + count and len(rows) < 12 and chars < budget:
            size = min(6000, offset + count - cursor, budget - chars)
            call = {'name': name, 'arguments': {**args, 'offset': cursor, 'limit': size}}
            cursor += size
            if sha(call) in seen:
                continue
            result = (document_index.read(engine, run_id, call['arguments']) if name == 'documents.read'
                      else code_navigation.read(engine, run_id, role, key, call['arguments']))
            rows.append({'call': call, 'result': result})
            seen.add(sha(call))
            chars += len(result.get('text', ''))
        if len(rows) >= 12 or chars >= budget:
            break
    return rows
