"""Derive one truncated reference hash from the same reply's verified identity.

Used only inside the settled native-response recovery transaction. It cannot
change judgments, ranges, a valid but wrong SHA, or any original paid record.
"""
from .prose_schema import validator as response_validator
import copy
import hashlib
import json
import re

from jsonschema import Draft202012Validator, SchemaError, ValidationError

from .json_format_recovery import _unique_object, completed_output
from .working_note_recovery import _directories, _read_anchor


def _normalize(h, run_id, role, key, progress, prepared, text, metadata):
    from .engine import Harness, ResearchBlocked, digest
    from .implementation_source_supplement import validate_item
    from .intelligence import parse_json
    store=h.engine.store
    from .evidence_delivery import tracking_required
    if not tracking_required(store,run_id,key):
        return None
    inputs=store.get('handoff_inputs',key,{})
    assignment=store.get('handoffs',key,{})
    context=inputs.get('context',{})
    unit=context.get('source_supplement_unit_review',{})
    normal=progress.get('normal_request',{})
    if (not completed_output(metadata) or not isinstance(text,str) or not 0<len(text)<=24000
            or context.get('implementation_source_supplement_protocol')!=1 or unit.get('version')!=1
            or context.get('source_supplement',{}).get('units')!=[unit.get('unit')]
            or role not in ('scout','engineer','director') or prepared.get('role')!=role
            or any(row.get('id')!=key or row.get('run_id')!=run_id for row in (inputs,assignment,progress))
            or assignment.get('to_role')!=role or assignment.get('status')=='completed'
            or store.get('decisions',key) or store.get('run_deletions',run_id)
            or progress.get('mode')=='final_only' or progress.get('pending_tool_calls')
            or store.get('handoff_progress',key)!=progress
            or normal.get('status') not in ('blocked','dispatching')
            or normal.get('turn')!=progress.get('turn') or normal.get('prepared')!=prepared
            or prepared.get('context',{}).get('source_supplement_unit_review')!=unit
            or prepared.get('context',{}).get('_purpose')!=assignment.get('purpose')
            or key!=Harness.decision_key(run_id,role,assignment.get('purpose'),inputs.get('instruction'),context,inputs.get('schema'))
            or assignment.get('input_hash')!=digest({k:inputs.get(k) for k in ('instruction','context','schema')})):
        return None
    value=parse_json(text)
    strict=json.loads(text,object_pairs_hook=_unique_object,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))
    if value!=strict or not isinstance(value,dict) or 'tool_calls' in value:
        return None
    errors=list(response_validator(inputs['schema']).iter_errors(value))
    if len(errors)!=1:
        return None
    error=errors[0];path=list(error.absolute_path)
    if (error.validator!='pattern' or len(path)!=5 or path[:3]!=['checks',0,'evidence']
            or type(path[3]) is not int or path[4]!='document_sha256'
            or len(value.get('checks',[]))!=1 or value['checks'][0]['id']!=unit['unit']):
        return None
    item=value['checks'][0];bad=item['evidence'][path[3]];old=bad['document_sha256']
    docs=_directories(context);requested=_directories(prepared['context']);doc=docs.get(bad['document_id'])
    if not doc or requested.get(bad['document_id'])!=doc:
        return None
    expected=doc['sha256']
    # A single contiguous omission, corroborated by another correct reference
    # in this exact paid answer. Never guess a source or substitute versions.
    if (not isinstance(old,str) or not re.fullmatch('[0-9a-f]{48,63}',old)
            or not re.fullmatch('[0-9a-f]{64}',expected)
            or not any(expected[:i]+expected[i+64-len(old):]==old for i in range(len(old)+1))
            or not any(i!=path[3] and ref['document_id']==bad['document_id']
                and ref['document_sha256']==expected for i,ref in enumerate(item['evidence']))):
        return None
    section=next(s for s in doc['sections'] if s['id']==bad['section_id'])
    start=section['start']+bad['offset'];end=min(section['end'],start+bad['limit'])
    proof=_read_anchor(h.engine,run_id,{'document_id':bad['document_id'],'section_id':bad['section_id'],
        'char_start':start,'char_end':end},doc,progress['tool_results'],{})
    result=copy.deepcopy(value);result['checks'][0]['evidence'][path[3]]['document_sha256']=expected
    response_validator(inputs['schema']).validate(result)
    response_validator(prepared['schema']).validate(result)
    # All references, including the other correct one, still require this
    # handoff's real read and acknowledged delivery; a directory is not credit.
    validate_item(h.engine,run_id,{'handoff_id':key},context['source_supplement']['document'],context,result['checks'][0])
    encoded=json.dumps(result,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
    return result,{'kind':'supplement_reference_hash_from_verified_read','version':1,
        'source_handoff_id':key,'role':role,'original_sha256':hashlib.sha256(text.encode()).hexdigest(),
        'repaired_sha256':hashlib.sha256(encoded.encode()).hexdigest(),'schema_sha256':digest(prepared['schema']),
        'edits':[{'path':'/checks/0/evidence/'+str(path[3])+'/document_sha256','old':old,'new':expected,**proof}],
        'charges_added':0,'scientific_claims_verified':False,'scientific_approval':False}


def normalize(*args):
    from .engine import ResearchBlocked
    from .intelligence import ProviderError
    try:
        return _normalize(*args)
    except (ResearchBlocked,ProviderError,ValueError,TypeError,KeyError,AttributeError,OSError,
            StopIteration,RecursionError,SchemaError,ValidationError):
        return None
