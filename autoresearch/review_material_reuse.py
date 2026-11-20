"""Authenticate same-role submitted bytes across material-format generations.

No decisions, inputs, reads or billable replies are rewritten. A reused range
is a derived view of its own original acquisition AND submitted inventory.
"""
from __future__ import annotations

import copy
import json
import functools
from bisect import bisect_right

from . import independent_full_review as full,review_material_packet as packets

SCIENCE_FIELDS=('run_id','cycle','plan_sha256','metric_contract_sha256','source_work_order',
    'source_sha256','author_handoff_id','approved_source_design','approved_base_plan','approved_acceptance','metric_contract')


def science(context):
    if any(key not in context for key in SCIENCE_FIELDS):raise ValueError('Material reuse lacks its complete scientific source identity')
    return full.sha({key:context[key] for key in SCIENCE_FIELDS})


def _frozen_input(store,run_id,hid):
    from .engine import Harness,digest
    frozen=store.get('handoff_inputs',hid) or {};owner=store.get('handoffs',hid) or {}
    payload={k:frozen.get(k) for k in ('instruction','context','schema')}
    context=payload['context'] or {};binding=full.validate_binding(context.get(full.KEY))
    role=owner.get('to_role');purpose=owner.get('purpose')
    if (role not in full.REVIEWERS or frozen.get('run_id')!=run_id or owner.get('run_id')!=run_id
            or owner.get('status') not in {'queued','running','completed','interrupted','failed','blocked'}
            or owner.get('input_hash')!=digest(payload)
            or Harness.decision_key(run_id,role,purpose,**payload)!=hid
            or binding['source_identity'].get('context_sha256')!=full.sha(full.source_context(context))
            or binding['source_identity'].get('instruction_sha256')!=full.sha(payload['instruction'])
            or binding['source_identity'].get('schema_sha256')!=full.sha(payload['schema'])
            or not full.purpose_matches(purpose, binding['domain'])):
        raise ValueError('Material reuse parent lacks its exact original role/source/request')
    progress=store.get('handoff_progress',hid) or {}
    return frozen,owner,progress


def parent_input(store,run_id,hid,*,reading_only=False):
    frozen,owner,progress=_frozen_input(store,run_id,hid)
    if owner.get('status') not in {'completed','interrupted','failed','blocked'}:
        raise ValueError('Material reuse parent has no terminal handoff state')
    for key in ('normal_request','finalization'):
        request=progress.get(key,{})
        if (request.get('status') in {'dispatching','blocked'} and not request.get('response')
                and (not reading_only or owner.get('status')=='completed' or store.get('decisions',hid))):
            raise ValueError('Material reuse parent model outcome remains unknown')
    decision=store.get('decisions',hid)
    if decision:full.authenticate(store,run_id,full.stamp_vote(owner['to_role'],decision['result'],hid,frozen['context'][full.KEY]))
    return frozen,owner,progress


def unresolved_requests(progress):
    """Disclosure only: a past submitted page does not resolve a later call."""
    return [{'kind':key,'status':request['status'],'operation_id':request.get('operation_id'),
        'prepared_sha256':request.get('prepared_sha256'),'response_confirmed':False,
        'charge_status':'unknown','retry_authorized':False}
        for key in ('normal_request','finalization') if (request:=progress.get(key,{})).get('status')
            in {'dispatching','blocked'} and not request.get('response')]


def union(spans):
    result=[]
    for start,end in sorted(spans,key=lambda value:(value[0],value[1])):
        if start>=end:continue
        if result and start<=result[-1][1]:result[-1][1]=max(result[-1][1],end)
        else:result.append([start,end])
    return result


@functools.lru_cache(maxsize=8)
def json_value_spans(old):
    decoder=json.JSONDecoder();values=[]
    def whitespace(pos):
        while pos<len(old) and old[pos].isspace():pos+=1
        return pos
    def visit(pos):
        value,end=decoder.raw_decode(old,pos)
        values.append((pos,end,value if isinstance(value,str) else None))
        cursor=whitespace(pos+1)
        if isinstance(value,dict):
            for index in range(len(value)):
                _,cursor=decoder.raw_decode(old,cursor) # object keys are not source values
                cursor=whitespace(cursor)
                if old[cursor]!=':':raise ValueError('Invalid canonical JSON object')
                cursor=whitespace(visit(whitespace(cursor+1)))
                if index<len(value)-1:cursor=whitespace(cursor+1)
        elif isinstance(value,list):
            for index in range(len(value)):
                cursor=whitespace(visit(cursor))
                if index<len(value)-1:cursor=whitespace(cursor+1)
        return end
    if whitespace(visit(whitespace(0)))!=len(old):raise ValueError('Invalid JSON source boundary')
    return tuple(values)


def decoded_ranges(old,delivered,text):
    """Map only fully delivered JSON character tokens to exact source chars.

    Escaped newlines/quotes/backslashes must be completely submitted; partial
    escape tokens never earn a character. This is reading reuse, not approval.
    """
    if not text:return []
    result=[]
    ends=[b for _,b in delivered]
    width=len(packets.encoded(text))-2
    for quote,end,value in json_value_spans(old):
        if value is None:continue
        start=value.find(text)
        cursor,offset=0,quote+1
        while start>=0:
            # Non-overlapping matches advance monotonically. Re-encoding the
            # whole prefix for each common fragment was quadratic in old JSON
            # strings; encode only the gap since the previous occurrence.
            offset+=len(packets.encoded(value[cursor:start]))-2
            stop=offset+width
            span_index=bisect_right(ends,offset)
            if span_index<len(delivered) and delivered[span_index][0]<=offset and stop<=ends[span_index]:
                return [[0,len(text)]]
            if span_index<len(delivered) and delivered[span_index][0]<stop:
                run=None
                for index,char in enumerate(text):
                    token=2 if char in '\"\\\b\f\n\r\t' else 6 if ord(char)<32 else 1
                    while span_index<len(delivered) and ends[span_index]<=offset:span_index+=1
                    covered=(span_index<len(delivered) and delivered[span_index][0]<=offset
                        and offset+token<=ends[span_index])
                    if covered and run is None:run=index
                    if not covered and run is not None:
                        result.append((run,index));run=None
                    offset+=token
                if run is not None:result.append((run,len(text)))
            # Skip disjoint matches without scanning their decoded characters.
            offset=stop;cursor=start+len(text)
            start=value.find(text,start+len(text))
    return union(result)


def submitted(store,run_id,hid,role,context,progress,*,report=None):
    from .evidence_delivery import tracking_required,delivery_spans
    if not tracking_required(store,run_id,hid):return {}
    groups={}
    from .full_review_receipts import acquired_material
    reader,pages=acquired_material(store,run_id,role,hid,context,progress.get('tool_results',[]),progress=report)
    for call,page in pages:
        key=call.get('arguments',{}).get('material_id','root')
        groups.setdefault(key,[]).append((page['char_start'],page['char_end']))
    result={}
    for key,actual in groups.items():
        text,index=reader.text(key),reader.index(key)
        delivered=delivery_spans(store,run_id,role,index['document_id'],index['sha256'],
            identity_key='document_id',handoff_id=hid,text=text)
        spans=union([(max(a,p['char_start']),min(b,p['char_end'])) for a,b in actual for p in delivered])
        if spans:result[key]={'text':text,'spans':spans,'encoding':reader.encoding(key)}
    return result


def map_ranges(target,actual,spans):
    """Alias exact bytes, including a code scalar inside original JSON."""
    bodies={'root':target['root'],**target['nodes']}
    for source in actual.values():
        old=source['text']
        boundaries={(a,b) for a,b,_ in json_value_spans(old)} if source['encoding']=='json' else set()
        for key in spans:
            text=bodies[key]
            if not text:continue
            if source['encoding']=='json' and target['formats'].get(key)=='text':
                spans[key].extend(decoded_ranges(old,source['spans'],text))
                continue
            if source['encoding']=='text' and target['formats'].get(key,'json')=='json':continue
            start=old.find(text)
            while start>=0:
                if source['encoding']!='json' or (start,start+len(text)) in boundaries:
                    spans[key].extend((max(a,start)-start,min(b,start+len(text))-start)
                        for a,b in source['spans'] if max(a,start)<min(b,start+len(text)))
                start=old.find(text,start+len(text))


def reused(store,run_id,context,parent_ids,role,*,report=None):
    target=packets.compile_packet(context);bodies={'root':target['root'],**target['nodes']}
    spans={key:[] for key in ['root',*target['required']]};parents=[];observations=[]
    expected=science(context)
    for position,hid in enumerate(parent_ids):
        if report:report('historical_handoffs',position,len(parent_ids),'份历史交接',role)
        frozen,owner,progress=parent_input(store,run_id,hid,reading_only=True)
        if owner['to_role']!=role or science(frozen['context'])!=expected:
            raise ValueError('Material read reuse changed role/run/cycle/plan or scientific source')
        report_page=(lambda done,total: report('read_receipts',done,total,'条历史读取记录',role)) if report else None
        actual=submitted(store,run_id,hid,role,frozen['context'],progress,report=report_page)
        from .full_review_notes import COLLECTION
        from .evidence_delivery import covering_receipts
        old_index=full.material_index(frozen['context'])
        readable={}
        from .implementation_alignment_evidence import _full_handoffs
        from types import SimpleNamespace
        for ancestor in _full_handoffs(SimpleNamespace(store=store),run_id,role,hid,frozen['context']):
            if ancestor==hid:prior,prior_progress,prior_actual=frozen,progress,actual
            else:
                prior,_,prior_progress=parent_input(store,run_id,ancestor,reading_only=True)
                prior_actual=submitted(store,run_id,ancestor,role,prior['context'],prior_progress,report=report_page)
            for row in prior_progress.get('tool_results',[]):
                page=row.get('result',{});call=row.get('call',{})
                if call.get('name')!=full.TOOL:continue
                # submitted() has already authenticated failures; they are
                # diagnostics, never source pages or saved-note anchors.
                if isinstance(page,dict) and 'error' in page:continue
                group=prior_actual.get(call.get('arguments',{}).get('material_id','root'),{})
                if covering_receipts([{'char_start':a,'char_end':b} for a,b in group.get('spans',[])],page['char_start'],page['char_end']):
                    readable[page['receipt_sha256']]=True
        for note in store.list(COLLECTION,run_id):
            if note.get('handoff_id')!=hid or note.get('role')!=role:continue
            if (note.get('id')!=full.sha({k:v for k,v in note.items() if k not in {'id','created_at'}})
                    or note.get('policy_sha256')!=old_index['policy_sha256'] or note.get('material_sha256')!=old_index['sha256']
                    or set(note['note']['receipt_sha256s'])-readable.keys()):
                raise ValueError('Reused observations lack their original same-role submitted sources')
            observations.append(copy.deepcopy(note))
        map_ranges(target,actual,spans)
        parents.append({'handoff_id':hid,'input_hash':owner['input_hash'],
            'material_sha256':full.material_index(frozen['context'])['sha256'],
            **({'unresolved_model_outcomes':unresolved_requests(progress)} if unresolved_requests(progress) else {})})
        if report:report('historical_handoffs',position+1,len(parent_ids),'份历史交接',role)
    spans={key:union(value) for key,value in spans.items() if value}
    return {'version':1,'science_sha256':expected,'parents':parents,'ranges':spans,
        'covered_chars':sum(b-a for value in spans.values() for a,b in value),
        'observations':observations,'approval_inherited':False}


def validate_certificate(store,run_id,context,role,claimed):
    try:return _validate_certificate(store,run_id,context,role,claimed)
    except (KeyError,TypeError,IndexError,AttributeError) as exc:
        raise ValueError('Frozen reading certificate has invalid typed fields') from exc


def _validate_certificate(store,run_id,context,role,claimed):
    """Keep the frozen credit; later native readback grants no extra authority.

    Unknown-outcome disclosure describes the original freeze. Resolving that
    call does not erase its earlier submitted pages or alter the new request.
    Every credited range and saved observation is still checked against the
    actual original sources on every validation.
    """
    if (not isinstance(claimed,dict) or set(claimed)!={'version','science_sha256','parents','ranges',
            'covered_chars','observations','approval_inherited'} or type(claimed.get('version')) is not int
            or claimed['version']!=1 or claimed['science_sha256']!=science(context)
            or claimed['approval_inherited'] is not False):
        raise ValueError('Frozen reading certificate changed its scientific source or version')
    ids=[p['handoff_id'] for p in claimed['parents']]
    if len(ids)!=len(set(ids)):raise ValueError('Duplicate frozen reading parents')
    from .harness_activity import checkpoint
    def validation_progress(phase,completed,total,unit,owner):
        checkpoint('validation_handoffs' if phase=='historical_handoffs' else phase,
            completed,total,unit,owner)
    actual=reused(store,run_id,context,ids,role,report=validation_progress)
    core=('handoff_id','input_hash','material_sha256')
    if ([{k:p.get(k) for k in core} for p in claimed['parents']]
            !=[{k:p.get(k) for k in core} for p in actual['parents']]):
        raise ValueError('Frozen reading parents changed their original request or material')
    target=packets.compile_packet(context);bodies={'root':target['root'],**target['nodes']}
    required={'root',*target['required']};covered=0
    for key,ranges in claimed['ranges'].items():
        if key not in required:raise ValueError('Frozen reading credit names a foreign material')
        length=len(bodies[key])
        if any(not isinstance(pair,list) or len(pair)!=2 or any(type(v) is not int for v in pair)
                or not 0<=pair[0]<pair[1]<=length for pair in ranges) or union(ranges)!=ranges:
            raise ValueError('Frozen reading ranges are invalid or noncanonical')
        available=actual['ranges'].get(key,[])
        if any(not any(a<=start and end<=b for a,b in available) for start,end in ranges):
            raise ValueError('Frozen reading credit exceeds actual submitted sources')
        covered+=sum(b-a for a,b in ranges)
    if type(claimed['covered_chars']) is not int or claimed['covered_chars']!=covered:
        raise ValueError('Frozen reading progress differs from its unique credited ranges')
    if (len({note['id'] for note in claimed['observations']})!=len(claimed['observations'])
            or any(note not in actual['observations'] for note in claimed['observations'])):
        raise ValueError('Frozen observations no longer match immutable submitted-source notes')
    return True


def prepare(engine,run_id,context,*,progress=None):
    """Program-owned migration selects all exact-source parents, never newest."""
    if not packets.enabled(context):return context
    if not getattr(engine.store,'is_read_snapshot',False):
        # One coherent read transaction enables the existing request-local
        # receipt/manifest memoization. No authority cache survives this call.
        from types import SimpleNamespace
        with engine.store.read_snapshot() as snapshot:
            return prepare(SimpleNamespace(store=snapshot),run_id,context,progress=progress)
    # New requests for this capability version must not acquire themselves as
    # parents on replay. Changed capability identities can reuse equal bytes.
    predicate="json_extract(data,'$.context.source_work_order')=?"
    if hasattr(engine.store,'record_keys'):
        ids=engine.store.record_keys('handoff_inputs',run_id,predicate=predicate,
            parameters=[context['source_work_order']])
    else:ids=[r['id'] for r in engine.store.list('handoff_inputs',run_id)
        if r.get('context',{}).get('source_work_order')==context['source_work_order']]
    candidates={role:[] for role in full.REVIEWERS};expected=science(context);snapshots=[];historical=[]
    from .metric_runtime_scope import KEY as metric_scope
    for position,hid in enumerate(sorted(ids)):
        if progress:progress('source_inventory',position,len(ids),'份冻结输入',None)
        frozen=engine.store.get('handoff_inputs',hid) or {};old=frozen.get('context',{})
        if full.KEY not in old or any(k not in old for k in SCIENCE_FIELDS) or science(old)!=expected:continue
        if metric_scope in context and metric_scope not in old:continue
        if packets.enabled(old) and packets.source(old)==packets.source(context):
            _,owner,_=_frozen_input(engine.store,run_id,hid)
            decision=engine.store.get('decisions',hid)
            if decision:full.authenticate(engine.store,run_id,full.stamp_vote(owner['to_role'],decision['result'],hid,old[full.KEY]))
            snapshots.append(copy.deepcopy(old.get(packets.REUSE)))
            continue
        historical.append(hid)
    if progress:progress('source_inventory',len(ids),len(ids),'份冻结输入',None)
    result=copy.deepcopy(context)
    if snapshots:
        snapshot=snapshots[0]
        if any(value!=snapshot for value in snapshots):
            raise ValueError('Same-source frozen reading certificates disagree')
        if snapshot is None:result.pop(packets.REUSE,None)
        else:
            if not isinstance(snapshot,dict) or set(snapshot)!=set(full.REVIEWERS):
                raise ValueError('Frozen reading certificate reviewer set changed')
            for role,certificate in snapshot.items():validate_certificate(engine.store,run_id,context,role,certificate)
            result[packets.REUSE]=snapshot
        return result
    for hid in historical:
        old=engine.store.get('handoff_inputs',hid)['context'];packets.enabled(old)
        owner=engine.store.get('handoffs',hid) or {}
        if owner.get('status') not in {'completed','interrupted','failed','blocked'}:continue
        parent_input(engine.store,run_id,hid,reading_only=True)
        candidates[owner['to_role']].append(hid)
    result[packets.REUSE]={role:reused(engine.store,run_id,context,ids,role,report=progress) for role,ids in candidates.items()}
    return result


def validate_delivery(store,run_id,vote):
    if not getattr(store,'is_read_snapshot',False) and hasattr(store,'read_snapshot'):
        with store.read_snapshot() as snapshot:
            return validate_delivery(snapshot,run_id,vote)
    from .evidence_delivery import tracking_required
    frozen=full.authenticate(store,run_id,vote);context=frozen['context'];role=vote['role']
    hid=vote[full.KEY]['handoff_id']
    return validate_reading(store, run_id, role, hid, context, full.reading_handoffs(store, run_id, vote))


def validate_reading(store, run_id, role, hid, context, owners):
    from .evidence_delivery import tracking_required
    if not tracking_required(store,run_id,hid):raise ValueError('Packet review lacks actual submitted inventory')
    claimed=context.get(packets.REUSE,{}).get(role)
    target=packets.compile_packet(context)
    bodies={'root':target['root'],**target['nodes']}
    required=['root',*target['required']]
    spans={key:[] for key in required}
    if claimed:
        validate_certificate(store,run_id,context,role,claimed)
        for key,value in claimed['ranges'].items():spans[key].extend(value)
    for current in owners:
        parent=store.get('handoff_inputs',current)['context'];progress=store.get('handoff_progress',current) or {}
        from .harness_activity import checkpoint
        map_ranges(target,submitted(store,run_id,current,role,parent,progress,
            report=lambda done,total:checkpoint('read_receipts',done,total,'条历史读取记录',role)),spans)
    missing=[];covered=0
    for key in required:
        length=len(bodies[key]);cursor=0
        unique=union(spans.get(key,[]));covered+=sum(b-a for a,b in unique)
        gaps=[]
        for start,end in unique:
            if start>cursor:gaps.append([cursor,start])
            cursor=max(cursor,end)
        if cursor<length:gaps.append([cursor,length])
        if gaps:missing.append({'material_id':key,'missing_ranges':gaps,'total_chars':length})
    if missing:
        error=ValueError('Required unique review sources have missing actual submitted pages')
        error.missing_offset=covered;error.covered_chars=covered;error.missing_materials=missing
        raise error
    return True
