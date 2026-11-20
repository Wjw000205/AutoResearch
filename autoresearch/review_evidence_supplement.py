"""A reviewer supplies only missing source evidence. Its frozen ballot stays host-side."""
import copy

from .scoped_file_reference import select

FIELD = 'review_evidence_supplement'
TITLE = 'Missing code evidence for this reviewer'


def direct_output(context, schema):
    return bool(context.get(FIELD) and isinstance(schema,dict) and schema.get('title') == TITLE
                and schema.get('type') == 'object' and schema.get('additionalProperties') is False)


def legacy_error(error):
    return isinstance(error,str) and error.removeprefix('Final-only completion did not finish: ').startswith(
        'Provider JSON failed schema validation at ') and any(
        'unexpected fields: ' + field in error for field in ('document_id','artifact_id','alignment_binding'))


def rejected_receipt(store, intelligence, run_id, key, progress):
    """Identify a paid, settled full-ballot rewrite on an old supplemental request."""
    import json
    from . import code_review_catalog as catalog
    from . import code_repair_response_correction as correction
    from .codex_reasoning import key_for
    from .engine import digest
    from .native_retained_response import recover, captured_delivery
    from .prose_schema import validator
    slot = progress.get('finalization') or {}
    if (progress.get('mode') != 'final_only' or slot.get('status') != 'blocked'
            or not legacy_error(slot.get('error')) or slot.get('provider_id') != 'local_codex'
            or slot.get('response') or progress.get('pending_tool_calls') or store.get('decisions',key)):
        return None
    history = progress.get('alignment_candidate_history') or []
    if not history or {p['kind'] for p in history[-1].get(correction.FIELD,[])} != {'missing_evidence_indices'}:
        return None
    prepared = slot['prepared']
    if FIELD in prepared['context'] or prepared['context'].get(catalog.CATALOG,{}).get('version') != 2:
        return None
    frozen = store.get('handoff_inputs',key)
    handoff = store.get('handoffs',key)
    if (not frozen or not handoff or frozen['run_id'] != run_id or prepared['role'] != handoff['to_role']
            or prepared['context'].get('_purpose') != handoff['purpose']
            or slot.get('prepared_sha256') not in (None,digest(prepared))):
        return None
    correction.verify_archive(None,run_id,handoff['to_role'],handoff['purpose'],key,frozen['context'],history[-1])
    native_key, op = key_for(intelligence,run_id,**prepared)
    native = store.get('codex_reasoning',native_key) or {}
    if (native.get('operation_id') != op or native.get('schema') != prepared['schema']
            or native.get('role') != prepared['role'] or not captured_delivery(store,native,frozen)):
        return None
    def to_wire(text, metadata):
        value = json.loads(text)
        from .review_response_policy import acceptance_schema
        if not validator(acceptance_schema(frozen['schema'])).is_valid(value):
            return None
        missing = {p['requirement_id'] for p in history[-1][correction.FIELD]}
        if missing.intersection(e['requirement_id'] for e in value['code_evidence']):
            return None  # Do not discard a delivered supplement containing new requested evidence.
        result = copy.deepcopy(value)
        for field in catalog.HEADERS:
            result.pop(field,None)
        for finding in result['alignment_findings']:
            for ref in finding['source_refs']:
                ref['document'] = ref.pop('document_id')
        for evidence in result['code_evidence']:
            evidence['file'] = evidence.pop('artifact_id')
            for ref in evidence['implementation_refs']:
                ref['document'] = ref.pop('document_id')
        return result, {'kind':'legacy_full_review_on_supplement'}
    receipt = recover(intelligence,native,strict_source=True,schema_repair=to_wire)
    if not receipt or not receipt.get('json_format_repair'):
        return None
    # The old reply is recorded as rejected. No judgment from it is merged.
    return {k:receipt[k] for k in ('request_id','transcript_id','thread_id','turn_id','retained_response_recovery')}


async def resume_legacy(h, run_id, key, progress):
    if not legacy_error((progress.get('finalization') or {}).get('error')):
        return
    receipt = await h.engine._read_consensus_check(run_id,rejected_receipt,h.engine.store,
                                                   h.intelligence,run_id,key,progress)
    if not receipt:
        return
    _resume_reads(h,run_id,key,progress,{'receipt':receipt})


def _resume_reads(h, run_id, key, progress, archive):
    updated = copy.deepcopy(progress)
    archive['requests'] = {s:updated.pop(s) for s in ('normal_request','finalization') if s in updated}
    if 'schema_correction' in updated:
        archive['schema_correction'] = updated.pop('schema_correction')
    updated.setdefault('rejected_evidence_supplements',[]).append(archive)
    updated.pop('mode',None)
    updated['turn'] += 1
    updated['alignment_read_extension_until'] = max(updated.get('alignment_read_extension_until',0),updated['turn']+4)
    h.engine.check_active(run_id)
    h.engine.store.batch([('handoff_progress',key,updated)],
                         expected=[('handoff_progress',key,progress),('decisions',key,None)])
    progress.clear()
    progress.update(updated)


def retry_full_rewrite(h, run_id, key, result, progress):
    """Retain an inadmissible full rewrite and send the original role the evidence slots."""
    from .code_repair_response_correction import FIELD as PROBLEMS
    from .code_review_catalog import CATALOG
    from .engine import digest
    anchor = progress['alignment_candidate_history'][-1]
    if {p['kind'] for p in anchor.get(PROBLEMS,[])} != {'missing_evidence_indices'}:
        return False
    slot = 'finalization' if progress.get('mode') == 'final_only' else 'normal_request'
    request = progress.get(slot) or {}
    prepared = request.get('prepared') or {}
    response = request.get('response') or {}
    if (FIELD in prepared.get('context',{}) or prepared.get('context',{}).get(CATALOG,{}).get('version') != 2
            or request.get('status') != 'response_saved'
            or not response.get('request_id') or response.get('json') != result or progress.get('pending_tool_calls')
            or h.engine.store.get('decisions',key)):
        return False
    _resume_reads(h,run_id,key,progress,{'source_request_id':response['request_id'],'rejected_sha256':digest(result)})
    return True


def project(prepared, progress):
    from .review_response_policy import FIELD as POLICY, VERSION
    if prepared['context'].get(POLICY) == VERSION:
        return prepared  # A current reviewer may submit a complete revised opinion.
    from .code_repair_response_correction import FIELD as PROBLEMS
    from .code_review_catalog import CATALOG
    from .engine import digest
    history = progress.get('alignment_candidate_history') or []
    if FIELD in prepared['context'] or not history or prepared['context'].get(CATALOG, {}).get('version') != 2:
        return prepared
    anchor = history[-1]
    problems = anchor.get(PROBLEMS) or []
    if not problems or any(p['kind'] != 'missing_evidence_indices' for p in problems):
        return prepared
    slots = {f'E{i}': {k:p[k] for k in ('field','index','issue_id','requirement_id')}
             for i,p in enumerate(problems,1)}
    schema = copy.deepcopy(prepared['schema'])
    for branch in schema.get('anyOf', [schema]):
        if 'code_evidence' not in branch.get('properties', {}):
            continue
        base = branch['properties']['code_evidence']['items']['properties']
        source = base['implementation_refs']['items']['properties']
        paragraph = closed({k:source[k] for k in ('section_id','offset','limit')})
        citation = closed({'file':{'type':'string','minLength':1}, 'offset':base['offset'], 'limit':base['limit'],
                           'implementation_refs':{'type':'array','items':paragraph,'minItems':1},
                           'verification':base['verification']})
        branch.clear()
        branch.update(closed({name:{'type':'array','items':citation,'minItems':1,'maxItems':256}
                              for name in slots}),title=TITLE)
    marker = {'candidate_sha256':digest(anchor['candidate']), 'slots':slots}
    task = ('按本请求 Schema 仅补交下列验收项的源码证据。每个 E 编号对应原意见的一处依据。'
            'file 可填本次交付的相对路径、文件编号或 SHA。implementation_refs 填已读实现文档段落。'
            '需要新证据时先读取对应源码或规范。宿主将把你的每组证据追加到原意见，保留其他内容。')
    requirements = {r['id']:r.get('locations',[]) for r in
                    prepared['context'].get('implementation_alignment_manifest_requirements',[])}
    for name, slot in slots.items():
        row = anchor['candidate'][slot['field']][slot['index']]
        issue = row.get('reason')
        if not issue and isinstance(row.get('issue_index'),int):
            issue = anchor['candidate']['issues'][row['issue_index']]
        task += f"\n{name} · {slot['requirement_id']} · {issue or slot['issue_id']}"
    for requirement in dict.fromkeys(s['requirement_id'] for s in slots.values()):
        if requirements.get(requirement):
            task += f"\n{requirement} 的实现清单列出了以下位置。请查阅与原意见相关的实际代码并给出读取区间：" + '、'.join(requirements[requirement])
    return {**prepared,'schema':schema,'task':task,
            'context':{**prepared['context'], FIELD:marker}}


def closed(fields):
    return {'type':'object', 'properties':fields, 'required':list(fields), 'additionalProperties':False}


def merge(raw, marker, anchor, files, directory, document, choices):
    from .engine import digest, ResearchBlocked
    from .code_repair_response_correction import FIELD as PROBLEMS, validate_correction
    slots = {f'E{i}': {k:p[k] for k in ('field','index','issue_id','requirement_id')}
             for i,p in enumerate(anchor[PROBLEMS],1) if p['kind']=='missing_evidence_indices'}
    if (marker['candidate_sha256'] != digest(anchor['candidate']) or marker['slots'] != slots
            or set(raw) != set(slots)):
        raise ResearchBlocked('本次补证与原角色的缺项清单不一致。')
    result = copy.deepcopy(anchor['candidate'])
    evidence = result['code_evidence']
    known = {digest(row):i for i,row in enumerate(evidence)}
    entries = {a:files[a] for a in directory['code_files']}
    for name, slot in slots.items():
        indices = []
        for n, ref in enumerate(raw[name]):
            location = f'supplement/{name}/{n}'
            file = select(ref,entries,document=False,location=location,selection=choices.get(location))
            row = {k:copy.deepcopy(ref[k]) for k in ('offset','limit','verification','implementation_refs')}
            row.update(requirement_id=slot['requirement_id'], artifact_id=file['artifact_id'], sha256=file['sha256'])
            for paragraph in row['implementation_refs']:
                paragraph.update(document_id=document['document_id'],document_sha256=document['sha256'])
            identity = digest(row)
            if identity not in known:
                known[identity] = len(evidence)
                evidence.append(row)
            if known[identity] not in indices:
                indices.append(known[identity])
        result[slot['field']][slot['index']]['evidence_indices'] = indices
    validate_correction(result,anchor)
    return result
