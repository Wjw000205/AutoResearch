"""Compile authenticated reading obligations for supported full-review domains.

Completion permits a bounded answer, never approval. Unknown domain tools keep
their original lifecycle rather than guessing that reading the packet is enough.
"""
from __future__ import annotations

import copy

from . import independent_full_review as full, review_material_packet as packets
from .architecture_validation import operation

READING = 'full_review_reading_progress'
MARKER = '_full_review_closeout'


def enabled(context):
    if full.KEY not in context:
        return False
    targets = context.get(full.READING, {}).get('missing_read_targets', [])
    if any(t.get('kind') not in {'document', 'code'} for t in targets):
        return False
    whole = context.get('whole_code_review', {})
    from .implementation_definition_review_scope import enabled as scoped_definition
    return (type(whole.get('version')) is int and whole['version'] == 1 or
        packets.supported(context) or scoped_definition(context) or context.get('code_revision_review',{}).get('version')==1)


def contract(h, run_id, role, key, context):
    from .engine import Harness, ResearchBlocked, digest
    binding = full.validate_binding(context[full.KEY])
    owner = h.engine.store.get('handoffs', key) or {}
    frozen = h.engine.store.get('handoff_inputs', key) or {}
    payload = {name: frozen.get(name) for name in ('instruction', 'context', 'schema')}
    if (role not in full.REVIEWERS or owner.get('id') != key or owner.get('run_id') != run_id
            or owner.get('to_role') != role or frozen.get('id') != key or frozen.get('run_id') != run_id
            or owner.get('input_hash') != digest(payload) or payload['context'] != context
            or Harness.decision_key(run_id, role, owner.get('purpose', ''), **payload) != key):
        raise ResearchBlocked('全文审核收答缺少当前角色的精确冻结输入；原请求保留。')
    full.validate_recovery(context)
    identity = binding['source_identity']
    if (identity.get('context_sha256') and (
            identity['context_sha256'] != full.sha(full.source_context(context))
            or not full.purpose_matches(owner.get('purpose', ''), binding['domain']))
            or identity.get('schema_sha256') and identity['schema_sha256'] != full.sha(payload['schema'])
            or identity.get('instruction_sha256') and identity['instruction_sha256'] != full.sha(payload['instruction'])
            or context.get(full.MATERIALS) != full.resolve_materials(h.engine.store, run_id, context)):
        raise ResearchBlocked('全文审核收答的冻结来源、schema或生产者策略已变化；未派发请求。')
    if context.get('whole_code_review'):
        from .code_whole_review import binding as whole_binding
        if (whole_binding(context, payload['schema']) != binding
                or context['whole_code_review']['directory_sha256'] != digest(context['delivery_artifacts'])):
            raise ResearchBlocked('全文代码审核收答的完整交付身份已变化；未派发请求。')
    if 'code_revision_review' in context:
        from .code_revision_review import binding as revision_binding
        if (revision_binding(context,payload['schema'])!=binding
                or full.sha(context['revision_material'])!=context['code_revision_review']['packet_sha256']):
            raise ResearchBlocked('修改复审材料与冻结绑定不一致')
    return {'sha256': binding['sha256'], 'scope': 'full_material'}


def missing_plan(exc, run_id, role, key, contract, progress):
    fields = ('missing_offset', 'missing_documents', 'missing_materials', 'missing_read_targets', 'covered_chars')
    result = {'version': 1, 'run_id': run_id, 'role': role, 'handoff_id': key,
        'contract_sha256': contract['sha256'], 'phase': 'targeted_reading',
        'diagnostic': str(exc), 'scientific_approval': False,
        **{k: copy.deepcopy(getattr(exc, k)) for k in fields if hasattr(exc, k)}}
    if hasattr(exc, 'closeout_missing_read_targets'):
        result['missing_read_targets'] = copy.deepcopy(exc.closeout_missing_read_targets)
    return result


def prepare_missing(run_id, role, key, progress, prepared):
    from .engine import ResearchBlocked
    state = progress.get(READING)
    if not state:
        return prepared
    if (state.get('version') != 1 or state.get('phase') != 'targeted_reading'
            or (state.get('run_id'), state.get('role'), state.get('handoff_id')) != (run_id, role, key)
            or state.get('contract_sha256') != prepared['context'][full.KEY]['sha256']
            or state.get('scientific_approval') is not False):
        raise ResearchBlocked('全文审核定向阅读诊断身份损坏；未派发请求。')
    from . import implementation_definition_review_scope as definitions
    if definitions.enabled(prepared['context']):
        return definitions.prepare_scheduled(progress, prepared)
    prepared['context']['_full_review_reading_progress'] = copy.deepcopy(state)
    prepared['task'] += ('\nCONTROLLER TARGETED READING: the host authenticated the missing ranges in '
        '_full_review_reading_progress. Read those exact versions/files and ranges, then return your complete '
        'independent judgment. Reuse existing submitted pages and saved observations; duplicate pagination '
        'and repeated notes do not close these gaps. Preserve unresolved objections; reading is not approval.')
    if any(target.get('kind') == 'document' and target.get('read_with')
           for target in state.get('missing_read_targets', ())):
        prepared['task'] += ('\nFinish the required document gaps before other review work. Use any exact pages '
            'already present in this request. For the remainder, batch the listed read_with calls '
            'before reopening other sources or saving more notes.')
    return prepared


def potentially_complete(store, key, context, progress):
    """Propose a proof for completion or a small document gap, never grant credit."""
    from .implementation_definition_review_scope import enabled as scoped_definition
    if scoped_definition(context):
        return True
    if full.code_delivery_scope(context):
        return _potential_document_coverage(store, key, context, progress)
    if packets.enabled(context):
        packet = packets.compile_packet(context)
        bodies = {'root': packet['root'], **packet['nodes']}
        required = ['root', *packet['required']]
        role = (store.get('handoffs', key) or {}).get('to_role')
        claimed = context.get(packets.REUSE, {}).get(role, {}).get('ranges', {})
    else:
        bodies, required, claimed = {'root': full.material_text(context)}, ['root'], {}
    spans = {k: list(claimed.get(k, [])) for k in required}
    current, seen = key, set()
    while current and current not in seen and len(seen) < 32:
        seen.add(current)
        prior = context if current == key else (store.get('handoff_inputs', current) or {}).get('context', {})
        ledger = progress if current == key else store.get('handoff_progress', current) or {}
        for row in ledger.get('tool_results', []):
            call, page = row.get('call', {}), row.get('result', {})
            if call.get('name') != full.TOOL or not isinstance(page, dict) or page.get('error'):
                continue
            material = call.get('arguments', {}).get('material_id') or 'root'
            start, end = page.get('char_start'), page.get('char_end')
            if (material in spans and type(start) is int and type(end) is int
                    and 0 <= start < end <= len(bodies[material])
                    and page.get('text') == bodies[material][start:end]):
                spans[material].append([start, end])
        current = prior.get(full.READING, {}).get('parent_handoff_id')
    from .review_material_reuse import union
    return all(sum(b-a for a, b in union(spans[k])) == len(bodies[k]) for k in required)


def _potential_document_coverage(store, key, context, progress):
    from collections import defaultdict
    from .code_whole_review import reading_documents
    from .review_material_reuse import union
    spans = defaultdict(list)
    current, seen = key, set()
    while current and current not in seen and len(seen) < 32:
        seen.add(current)
        prior = context if current == key else (store.get('handoff_inputs', current) or {}).get('context', {})
        ledger = progress if current == key else store.get('handoff_progress', current) or {}
        for row in ledger.get('tool_results', []):
            if row.get('call', {}).get('name') != 'documents.read':
                continue
            result = row.get('result', {})
            if isinstance(result, dict) and type(result.get('char_start')) is int and type(result.get('char_end')) is int:
                spans[result.get('document_id'), result.get('sha256'), result.get('section_id')].append(
                    (result['char_start'], result['char_end']))
        current = prior.get(full.READING, {}).get('parent_handoff_id')
    # Near the end, the proof must also be allowed to discover an entirely
    # skipped section. Only its submitted-byte check may name the gaps or
    # declare completion. Cap this early check to one page of missing text.
    remaining = 0
    for doc in reading_documents(context, required=True):
        for section in doc['sections']:
            start, end = section['start'], section['end']
            pages = union(spans[doc['document_id'], doc['sha256'], section['id']])
            remaining += end - start - sum(max(0, min(end, b) - max(start, a)) for a, b in pages)
            if remaining > full.PAGE_LIMIT:
                return False
    return True


@operation
def reading_proof(h, run_id, role, key, context):
    from .full_review_notes import _scope
    from .evidence_delivery import tracking_required
    engine = h.engine
    with engine.store.read_snapshot() as snapshot:
        owner = copy.copy(engine)
        owner.store = snapshot
        frozen, owners = _scope(owner, run_id, role, key)
        if frozen != context:
            raise ValueError('Full-review reading proof changed its frozen input')
        if not tracking_required(snapshot, run_id, key):
            return None
        full.validate_reading(snapshot, run_id, role, key, context, owners, engine=owner)
        proof = {'policy_sha256': context[full.KEY]['sha256'], 'reading_handoff_ids': owners,
                 'material_sha256': full.material_index(context)['material_sha256'],
                 'scientific_approval': False}
        if context.get('whole_code_review'):
            from .code_whole_review import validate_reading
            proof['code_delivery'] = validate_reading(owner, run_id, context, role, key, owners)
        return proof
