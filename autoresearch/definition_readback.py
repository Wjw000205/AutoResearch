"""Complete only missing saved-document reading; never rewrite an author result."""
from __future__ import annotations

import copy

from . import implementation_definition_repair as definitions
from .architecture_validation import operation

FIELD='definition_saved_readback'
BATCH_PAGES=12


def _fail(message):
    from .engine import ResearchBlocked
    raise ResearchBlocked(message)


def _binding(engine,run_id,state,row):
    from .saved_definition_continuation import binding
    return binding(engine,run_id,state,row)


def _packet(engine,run_id,state,row,packet):
    from .implementation_acceptance_flow import check_packet
    context=check_packet(engine,run_id,packet)
    if (packet['role']!='engineer' or context.get(FIELD,{}).get('binding')!=_binding(engine,run_id,state,row)
            or context.get('indexed_documents')!=[row['document']]):
        _fail('保存定义的补读没有绑定同一作者、文档和修订。')
    from .implementation_alignment_evidence import _required_ranges
    _required_ranges(engine,run_id,context[FIELD]['required_refs'],
        {row['document']['document_id']:row['document']['sha256']})
    return context


@operation
def coverage(engine,run_id,state,row):
    from . import document_index,implementation_documents as documents
    from .document_reference_spans import references
    doc,_=document_index.load(engine,run_id,row['document']['document_id'],row['document']['sha256'])
    doc={**row['document'],'sections':doc['sections']}
    sections={s['id']:s for s in doc['sections']}
    refs=definitions.addition_refs(engine,run_id,state,row)
    handoffs=[row['authoring_handoff_id']]
    for packet in state[definitions.FIELD].get('author_readbacks',[]):
        if packet['context'].get(FIELD,{}).get('binding',{}).get('revision')!=row['index']:
            continue  # Earlier rejected revisions retain their own readback.
        _packet(engine,run_id,state,row,packet)
        handoffs.append(packet['handoff_id'])
    spans={}
    for hid in handoffs:
        owned=documents._read_coverage_spans(engine,run_id,doc,hid)
        if owned is None:
            _fail('保存定义的原读取回执已损坏，不能以补读掩盖。')
        for section,values in owned.items(): spans.setdefault(section,[]).extend(values)
    missing=[]
    for ref in refs:
        section=sections[ref['section_id']]
        start=section['start']+ref['offset'];stop=start+ref['limit'];cursor=start
        for a,b in sorted(spans.get(ref['section_id'],[])):
            a,b=max(start,a),min(stop,b)
            if b<=a or b<=cursor: continue
            if a>cursor: missing.extend(references(doc,cursor,a))
            cursor=max(cursor,b)
        if cursor<stop: missing.extend(references(doc,cursor,stop))
    return {'complete':not missing,'missing_refs':missing,'reading_handoff_ids':handoffs}


