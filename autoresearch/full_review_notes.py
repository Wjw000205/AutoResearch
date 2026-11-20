"""Private observations anchored to actually delivered full-material pages."""
from __future__ import annotations

import copy

from . import independent_full_review as full
from .storage import now

TOOL = 'reviews.record_notes'
KEY = 'full_review_notes'
COLLECTION = 'independent_full_review_notes'


def schema(context=None):
    from .engine import obj
    text = {'type':'string','minLength':1}
    from .code_reading_memory import analysis_schema
    note = obj({'key':{**text,'maxLength':80},'statement':{**text,'maxLength':1200},
        'receipt_sha256s':{'type':'array','minItems':1,'maxItems':12,'uniqueItems':True,
            'items':{'type':'string','pattern':'^[0-9a-f]{64}$'}}})
    if (context or {}).get('experiment_vote_notes_protocol') != 1:
        note['properties']['code_analysis'] = analysis_schema()
    return {'type':'object','properties':{'action':{'enum':['save','read']},
        'notes':{'type':'array','minItems':1,'maxItems':6,'items':note},
        'note_id': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
        'offset':{'type':'integer','minimum':0}, 'limit':{'type':'integer','minimum':1,'maximum':12}},
        'required':['action'],'additionalProperties':False,
        'oneOf':[{'properties':{'action':{'const':'save'}},'required':['notes'],
                  'not':{'anyOf':[{'required':['offset']},{'required':['limit']}]}},
                 {'properties':{'action':{'const':'read'}},'not':{'required':['notes']}}],
        'allOf': [{'if': {'required': ['note_id']}, 'then': {
            'properties': {'action': {'const': 'read'}},
            'not': {'anyOf': [{'required': ['offset']}, {'required': ['limit']}]}}}]}


def _scope(engine, run_id, role, hid):
    from .engine import Harness, digest
    from .implementation_alignment_evidence import _full_handoffs
    owner = engine.store.get('handoffs',hid,{})
    frozen = engine.store.get('handoff_inputs',hid,{})
    context = frozen.get('context',{})
    payload = {k:frozen.get(k) for k in ('instruction','context','schema')}
    if (role not in full.REVIEWERS or not full.tool_available(context)
            or owner.get('run_id')!=run_id or owner.get('to_role')!=role or frozen.get('run_id')!=run_id
            or owner.get('input_hash')!=digest(payload)
            or Harness.decision_key(run_id,role,owner.get('purpose'),**payload)!=hid):
        raise ValueError('Review notes require this role and source\'s exact frozen assignment')
    return context, _full_handoffs(engine,run_id,role,hid,context)


def records(engine, run_id, role, hid, *, for_projection=False):
    context, owners = _scope(engine,run_id,role,hid)
    index = full.material_index(context)
    rows = [r for r in engine.store.list(COLLECTION,run_id)
        if r.get('role')==role and r.get('handoff_id') in owners
        and r.get('policy_sha256')==index['policy_sha256'] and r.get('material_sha256')==index['sha256']]
    from . import review_material_packet as packets
    certificate=context.get(packets.REUSE,{}).get(role,{})
    validated_parents={}
    if certificate:
        from .review_material_reuse import parent_input,science
        if certificate.get('science_sha256')!=science(context):
            raise ValueError('Historical observation scientific source changed')
    for old in certificate.get('observations',[]):
        parent=next((r for r in certificate['parents'] if r['handoff_id']==old.get('handoff_id')),None)
        if (not parent or old.get('run_id')!=run_id or old.get('role')!=role
                or engine.store.get(COLLECTION,old['id'])!=old):
            raise ValueError('Historical private observation changed role/source/receipt')
        if parent['handoff_id'] not in validated_parents:
            prior,owner,_=parent_input(engine.store,run_id,parent['handoff_id'],reading_only=True)
            if (owner['to_role']!=role or owner['input_hash']!=parent['input_hash']
                    or science(prior['context'])!=certificate['science_sha256']):
                raise ValueError('Historical observation parent changed source or role')
            validated_parents[parent['handoff_id']] = (
                {'policy_sha256': prior['context'][full.KEY]['sha256'], 'sha256': parent['material_sha256']}
                if for_projection else full.material_index(prior['context']))
        old_index=validated_parents[parent['handoff_id']]
        if old.get('policy_sha256')!=old_index['policy_sha256'] or old.get('material_sha256')!=old_index['sha256']:
            raise ValueError('Historical observation material version changed')
        if old not in rows:rows.append(copy.deepcopy(old))
    for row in rows:
        if row.get('id')!=full.sha({k:v for k,v in row.items() if k not in {'id','created_at'}}):
            raise ValueError('Immutable review observation changed')
    return sorted(rows,key=lambda r:(r['created_at'],r['id']))


def _submitted_receipts(engine, run_id, role, hid):
    """One read operation authenticates pages and shared submission inventories."""
    if not getattr(engine.store, 'is_read_snapshot', False) and hasattr(engine.store, 'read_snapshot'):
        with engine.store.read_snapshot() as snapshot:
            owner = copy.copy(engine)
            owner.store = snapshot
            return _submitted_receipts(owner, run_id, role, hid)
    from .evidence_delivery import delivery_spans, covering_receipts
    from .full_review_receipts import acquired_material
    context, owners = _scope(engine, run_id, role, hid)
    index = full.material_index(context)
    receipts = {}
    for owner in owners:
        prior = engine.store.get('handoff_inputs', owner, {})['context']
        rows = engine.store.get('handoff_progress', owner, {}).get('tool_results', [])
        reader, pages = acquired_material(engine.store, run_id, role, owner, prior, rows)
        delivered_by_material = {}
        for call, page in pages:
            material_id = call.get('arguments', {}).get('material_id')
            if material_id not in delivered_by_material:
                page_index, page_text = reader.index(material_id), reader.text(material_id)
                delivered_by_material[material_id] = delivery_spans(engine.store, run_id, role,
                    page_index['document_id'], page_index['sha256'], identity_key='document_id',
                    handoff_id=owner, text=page_text)
            if covering_receipts(delivered_by_material[material_id], page['char_start'], page['char_end']):
                receipts[page['receipt_sha256']] = owner
    return index, receipts


def execute(engine,run_id,role,hid,args):
    from .prose_schema import validate
    frozen = engine.store.get('handoff_inputs', hid) or {}
    validate(args,schema(frozen.get('context')))
    if args.get('note_id'):
        from .code_reading_memory import state, _anchors
        _, reads, observations, _ = state(engine, run_id, role, hid, full_review=True, note_id=args['note_id'])
        wanted = next((r for r in observations if r['id'] == args['note_id']), None)
        if wanted is None or set(wanted['note']['receipt_sha256s']) - reads.keys():
            raise ValueError('Observation ID requires this role\'s authenticated original source deliveries')
        return {'notes': [engine.store.get(COLLECTION, wanted['id'])],
            'source_ranges': _anchors(wanted['note'], reads), 'new_reading_progress': False,
            'scientific_approval': False, 'reading_evidence': False}
    if args['action']=='save':
        index, receipts = _submitted_receipts(engine, run_id, role, hid)
        if any('code_analysis' in note for note in args['notes']):
            from .code_reading_memory import state, validate_notes
            validate_notes(state(engine, run_id, role, hid, full_review=True, for_save=True), args['notes'])
        if hasattr(engine, 'check_active'):
            engine.check_active(run_id)
        puts = []
        for note in args['notes']:
            if set(note['receipt_sha256s'])-set(receipts):
                raise ValueError('Observations require this reviewer\'s actually submitted full-material page receipts')
            body={'run_id':run_id,'role':role,'handoff_id':hid,'policy_sha256':index['policy_sha256'],
                'material_sha256':index['sha256'],'note':copy.deepcopy(note),'scientific_approval':False}
            identity=full.sha(body)
            if not engine.store.get(COLLECTION,identity):
                puts.append((COLLECTION,identity,{'id':identity,**body,'created_at':now()}))
        if puts:
            engine.store.batch(puts=puts, expected=[(collection, identity, None) for collection, identity, _ in puts])
    rows=records(engine,run_id,role,hid)
    offset=args.get('offset',max(0,len(rows)-12));limit=args.get('limit',12)
    return {'notes':rows[offset:offset+limit],'offset':offset,'total':len(rows),
        'next_offset':offset+limit if offset+limit<len(rows) else None,
        'scientific_approval':False,'reading_evidence':False}


def project(engine,run_id,role,hid,prepared):
    from .code_whole_review import restricted_view
    from . import implementation_definition_review_scope as definitions
    if (restricted_view(prepared['context']) or definitions.enabled(prepared['context'])
            or definitions.citation(prepared['context'])):
        return prepared
    if not full.tool_available(prepared['context']):return prepared
    result=copy.deepcopy(prepared)
    from .full_review_closeout import MARKER
    from .indexed_review_closeout import STATE
    closing = prepared['context'].get(MARKER)
    if closing is not None:
        progress = engine.store.get('handoff_progress', hid) or {}
        if (closing != progress.get(STATE) or closing.get('version') != 1
                or closing.get('run_id') != run_id or closing.get('role') != role
                or closing.get('handoff_id') != hid or closing.get('scientific_approval') is not False
                or closing.get('contract_sha256') != prepared['context'][full.KEY]['sha256']):
            raise ValueError('Full-review closeout notes require their exact host checkpoint and source policy')
    from .code_review_reading import FULL_PROJECTION, _owner
    if FULL_PROJECTION in result['context']:
        _owner(result['context'])
        result['context']['review_memory_context_policy'] = 'index-v1'
    if full.code_delivery_scope(result['context']):
        result['context']['review_memory_context_policy'] = 'index-v1'
    index_only = result['context'].get('review_memory_context_policy') == 'index-v1'
    rows = records(engine, run_id, role, hid, for_projection=index_only)
    result['context'][KEY]={'total':len(rows), 'statements_displayed': False} if index_only else {
        'total':len(rows),'latest':rows if closing is not None else rows[-12:],
        'complete_observations': closing is not None,
        'read_with':{'name':TOOL,'arguments':{'action':'read','offset':0,'limit':12}},
        'scope':'Own observations only; not source evidence or scientific approval.'}
    from .code_reading_memory import project as link_code
    result = link_code(engine, run_id, role, hid, result, full_review=True)
    if index_only:
        context, owners = _scope(engine, run_id, role, hid)
        from . import review_material_packet as packets
        memory = result['context'].get('code_reading_memory', {})
        code_ids = {r['id'] for r in memory.get('directory', [])}
        code_ids.update(identity for group in memory.get('history', {}).get('by_source_version', []) for identity in group['note_ids'])
        # Code observations already have an exact-version/function/range index.
        # Only notes about this assignment's required documents need a second
        # directory. The full immutable list/read APIs retain their old scope.
        document_ids = set()
        if packets.enabled(context):
            packet = packets.compile_packet(context)
            document_ids = {r['id'] for r in packet['catalog'] if r['required'] and r['encoding'] == 'json'}
        references = {}
        for owner in {r['handoff_id'] for r in rows if r['id'] not in code_ids}:
            for entry in engine.store.get('handoff_progress', owner, {}).get('tool_results', []):
                call, page = entry.get('call', {}), entry.get('result', {})
                if call.get('name') == full.TOOL and call.get('arguments', {}).get('material_id') in document_ids:
                    references[page.get('receipt_sha256')] = True
        selected = [r for r in rows if r['id'] not in code_ids and (r['handoff_id'] in owners
            or any(ref in references for ref in r['note']['receipt_sha256s']))]
        result['context'][KEY] = {'total': len(rows), 'statements_displayed': False,
            'directory': [{'id': row['id'], 'key': row['note']['key'],
                'read_with': {'name': TOOL, 'arguments': {'action': 'read', 'note_id': row['id']}}} for row in selected],
            'history_read_with': {'name': TOOL, 'arguments': {'action': 'read', 'offset': 0, 'limit': 12}},
            'scope': 'Current code links are in code_reading_memory. Required-document observations only; all original own notes remain readable.'}
        index = result['context']['review_material']
        result['context']['review_navigation_indexes'] = {'version': 2, 'handoff_id': hid,
            'scope': 'Current frozen implementation documents and code; history is available on demand.',
            'background': {'read_with': {'name': full.TOOL, 'arguments': {
                'policy_sha256': index['policy_sha256'], 'material_sha256': index['material_sha256'],
                'material_id': 'root', 'offset': 0, 'limit': min(full.PAGE_LIMIT, index['total_chars'])}}},
            'history_read_with': {'name': TOOL, 'arguments': {'action': 'read', 'offset': 0, 'limit': 12}}}
    return result
