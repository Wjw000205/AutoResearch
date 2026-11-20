"""Recover copied review-document IDs from the frozen version and the role's own reads."""
import copy
import re

from . import implementation_alignment as alignment
from .host_section_recovery import one_digit_copy

UNBOUND = '对齐依据引用了未绑定的文档或旧版本。'
OUTSIDE = '对齐依据引用不是当前原文中的有效字符范围。'
ERRORS = {UNBOUND, OUTSIDE}


def references(value):
    for group, field in (('code_evidence','implementation_refs'), ('alignment_findings','source_refs')):
        for i, item in enumerate(value.get(group, [])):
            if isinstance(item, dict) and isinstance(item.get(field), list):
                for j, ref in enumerate(item[field]):
                    if isinstance(ref, dict):
                        yield group, i, field, j, ref


def copied_id(ref):
    value = ref.get('document_id')
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{63}|[0-9a-f]{65}', value) is not None


def derive(engine, run_id, role, key, context, result):
    """Never infer the version or the passage. Both must already have been delivered."""
    from .engine import ResearchBlocked, digest
    from .implementation_alignment_evidence import _required_ranges, _workflow_design
    from .implementation_reading import coverage
    current = context.get('implementation_document') or {}
    indexed = {doc['document_id']: doc for doc in context.get('indexed_documents', [])}
    corrected = copy.deepcopy(result)
    edits = []
    sources = None
    for group, i, field, j, ref in references(corrected):
        if not copied_id(ref):
            continue
        old = ref['document_id']
        if old in indexed:
            return None
        if field == 'implementation_refs':
            allowed = [current]
        else:
            if sources is None:
                sources = alignment._sources(context, _workflow_design(engine, run_id, key, context))
            allowed = sources.values()
        matches = [doc for doc in allowed if doc.get('sha256') == ref.get('document_sha256')
                   and re.fullmatch('[0-9a-f]{64}', doc.get('document_id',''))
                   and one_digit_copy(old, doc['document_id'])]
        if len(matches) != 1:
            return None
        doc, = matches
        did = doc['document_id']
        if indexed.get(did, {}).get('sha256') != doc['sha256']:
            return None
        ref['document_id'] = did
        try:
            if field == 'source_refs':
                alignment._validate_refs([ref], {did:doc})
            ranges = _required_ranges(engine, run_id, [ref], {did:doc['sha256']})
        except ResearchBlocked as exc:
            if str(exc) == OUTSIDE or getattr(exc,'alignment_reference_problems',None):
                return None
            raise
        if not coverage(engine, run_id, doc, ranges[did], [key])['complete']:
            return None
        edits.append({'path':[group,i,field,j,'document_id'], 'original':old, 'verified':did,
                      'document_sha256':doc['sha256'], 'section_id':ref['section_id'],
                      'offset':ref['offset'], 'limit':ref['limit']})
    if not edits:
        return None
    return corrected, {'role':role, 'handoff_id':key, 'original_result_sha256':digest(result),
                       'corrected_result_sha256':digest(corrected), 'edits':edits}


def saved_candidate(store, intelligence, run_id, frozen, prepared, response):
    """Bind the actual paid native response to this exact role, input and output."""
    from .codex_reasoning import key_for
    from .native_retained_response import recover, captured_delivery
    key, operation = key_for(intelligence, run_id, **prepared)
    native = store.get('codex_reasoning', key, {})
    if (native.get('id') != key or native.get('operation_id') != operation or native.get('run_id') != run_id
            or native.get('role') != prepared['role'] or native.get('schema') != prepared['schema']):
        return False
    restored = recover(intelligence, native, strict_source=True)
    return bool(restored and all(restored.get(k) == response.get(k) for k in
                ('json','text','request_id','transcript_id','model','thread_id','turn_id'))
                and captured_delivery(store, native, frozen))


def prepare(h, run_id, role, purpose, key, result):
    from .engine import digest
    from .architecture_validation import operation
    from .intelligence import Intelligence
    if not isinstance(h.intelligence, Intelligence):
        return None
    with h.engine.store.read_snapshot() as store:
        reader = copy.copy(h.engine)
        reader.store = store
        ai = copy.copy(h.intelligence)
        ai.store = store
        progress = store.get('handoff_progress', key, {})
        slot = 'finalization' if progress.get('mode') == 'final_only' else 'normal_request'
        request = progress.get(slot, {})
        active = request.get('response') or {}
        response = request.get('original_document_reference_response', active)
        frozen = store.get('handoff_inputs', key, {})
        assignment = store.get('handoffs', key, {})
        prepared = request.get('prepared') or {}
        if (request.get('provider_id') != 'local_codex' or request.get('status') != 'response_saved'
                or slot == 'normal_request' and request.get('turn') != progress.get('turn')
                or progress.get('pending_tool_calls') or active.get('json') != result
                or progress.get('id') != key or progress.get('run_id') != run_id
                or assignment.get('to_role') != role or assignment.get('purpose') != purpose
                or prepared.get('role') != role or prepared.get('context', {}).get('_purpose') != purpose
                or request.get('model') != response.get('model') or store.get('decisions',key)
                or frozen.get('run_id') != run_id or frozen.get('context',{}).get(alignment.FIELD) != 1):
            return None
        payload = {k:frozen[k] for k in ('instruction','context','schema')}
        if (h.decision_key(run_id, role, purpose, **payload) != key or assignment.get('input_hash') != digest(payload)
                or not saved_candidate(store, ai, run_id, frozen, prepared, response)):
            return None
        derived = operation(derive)(reader,run_id,role,key,frozen['context'],response['json'])
        if not derived:
            return None
        value, audit = derived
        audit.update(request_id=response['request_id'], transcript_id=response['transcript_id'])
        replacement = {**response, 'json':value, 'document_reference_repair':audit}
        if active is not response:
            if active != replacement:
                return None
            return progress, progress, value, audit
        next_progress = {**progress, slot:{**request, 'response':replacement,
                                           'original_document_reference_response':response}}
        return progress, next_progress, value, audit


async def resolve(h, run_id, role, purpose, key, response):
    from .engine import ResearchBlocked
    result = response['json']
    if (purpose.split(':',1)[0] not in {'code_review','implementation_document_review'}
            or not (response.get('document_reference_repair') or any(copied_id(ref) for *_,ref in references(result)))):
        return result
    plan = await h.engine._read_consensus_check(run_id, prepare, h, run_id, role, purpose, key, result)
    if plan is None:
        if response.get('document_reference_repair'):
            raise ResearchBlocked('已补正的文档引用与原请求或同角色读取回执不一致。')
        return result
    previous, updated, value, audit = plan
    if previous is not updated:
        h.engine.check_active(run_id)
        h.engine.store.batch([('handoff_progress',key,updated)],
                             expected=[('handoff_progress',key,previous),('decisions',key,None)])
        h.engine.store.event(run_id,'handoff.document_reference_repaired',
            '已依据相同正文哈希和本角色已读范围校正引用的文档 ID，继续核验原评审。', audit)
    return value
