"""Explicit full-code policy, independent of the document review generation."""
from __future__ import annotations

import copy

from . import independent_full_review as full
from .implementation_review_routing import digest

KEY='independent_full_code_review_policy'
PROTOCOL='independent_full_code_review_protocol'
HISTORY='legacy_code_review_migration'
LINEAGE='legacy_code_review_lineage'


def enabled(state):
    marker=state.get(KEY)
    if marker is not None:
        if (marker.get('version')!=1 or marker.get('reviewers')!=list(full.REVIEWERS)
                or marker.get('sha256')!=full.sha({k:v for k,v in marker.items() if k!='sha256'})):
            raise ValueError('Unknown independent full-code policy')
        return True
    from .implementation_whole_review import enabled as document_whole
    return document_whole(state)  # Explicit compatibility for already frozen v1 whole-code requests.


def architecture(state):
    if KEY in state:
        enabled(state)
        marker=state[KEY]
        if (marker['workflow_id']!=state['id'] or marker['proposal_hash']!=state['proposal_hash']
                or marker['design_hash']!=digest(state['common']['design'])):
            raise ValueError('Full-code approved architecture binding changed')
        return copy.deepcopy(marker['architecture'])
    from .implementation_whole_review import KEY as DOCUMENT_REVIEW
    return copy.deepcopy(state['acceptance_review'][DOCUMENT_REVIEW]['rounds'][-1]
        ['reports']['director']['context'][DOCUMENT_REVIEW]['architecture'])


def _validate_packet(engine,run_id,state,specification,attempt,packet):
    from .implementation_alignment_evidence import validate_vote,_delivery
    from .intelligence import parse_json
    from .review_response_policy import acceptance_schema
    import json
    frozen=engine.store.get('handoff_inputs',packet['handoff_id']) or {}
    payload={k:frozen.get(k) for k in ('instruction','context','schema')}
    raw=packet['result'];context=payload['context'];purpose=packet['purpose']
    _delivery(engine,run_id,packet['role'],packet['handoff_id'],raw)
    parse_json(json.dumps(raw,ensure_ascii=False),acceptance_schema(payload['schema']))
    if (frozen!=packet['frozen'] or full.KEY in context
            or (engine.store.get('decisions',packet['handoff_id']) or {}).get('run_id')!=run_id
            or (engine.store.get('handoffs',packet['handoff_id']) or {}).get('purpose')!=purpose
            or context.get('implementation_alignment_protocol')!=1
            or any(context.get(k)!=v for k,v in {'proposal_hash':state['proposal_hash'],
                'spec_hash':specification['spec_hash'],'code_sha256':attempt['code_sha256'],
                'delivery_hash':attempt['delivery_hash'],'implementation_document':specification['document']}.items())):
        raise ValueError('Legacy code review lacks its exact completed source assignment')
    validate_vote(engine,run_id,packet['role'],packet['handoff_id'],raw,context,delivery=True)


def validate_history(engine,run_id,state,specification,attempt):
    history=attempt.get(HISTORY)
    if history is None:
        ref=attempt.get(LINEAGE)
        if ref is None:return None
        if (ref.get('version')!=1 or not ref['origin_attempt']<=ref['parent_attempt']<attempt['index']):
            raise ValueError('Legacy finding lineage has a foreign or cyclic parent')
        origin=next(a for a in state['attempts'] if a['index']==ref['origin_attempt'])
        parent=next(a for a in state['attempts'] if a['index']==ref['parent_attempt'])
        history=origin.get(HISTORY)
        if (not history or history['sha256']!=ref['archive_sha256']
                or full.sha(parent['review_votes'])!=ref['parent_votes_sha256']):
            raise ValueError('Legacy finding lineage lost its original archive or actual parent votes')
        prior=validate_history(engine,run_id,state,_specification(state,origin,specification),origin)
        if any(full.KEY in v for v in parent['review_votes']):
            full.authenticate_complete(engine.store,run_id,parent['review_votes'])
            for vote in parent['review_votes']:
                frozen=full.authenticate(engine.store,run_id,vote)
                if frozen['context'].get('legacy_code_review_history_sha256')!=history['sha256']:
                    raise ValueError('Legacy finding lineage is not present in its actual parent request')
        elif parent['index']!=origin['index'] or parent['review_votes']!=history['original_votes']:
            raise ValueError('Legacy finding parent is not its original preserved vote group')
        return prior
    if (history.get('version')!=1 or history.get('sha256')!=full.sha({k:v for k,v in history.items() if k!='sha256'})
            or history.get('source_delivery_hash')!=attempt['delivery_hash']
            or len({p['role'] for p in history['packets']})!=len(history['packets'])):
        raise ValueError('Legacy code review migration archive changed')
    original_specification=_specification(state,attempt,specification)
    for packet in history['packets']:_validate_packet(engine,run_id,state,original_specification,attempt,packet)
    from .codex_workflow import _frozen_spec_assignment
    for packet in history.get('unfinished_packets',[]):
        if _frozen_spec_assignment(engine,run_id,packet['purpose'],packet['role'])!=packet['frozen']:
            raise ValueError('Unfinished legacy code input changed after archival')
    from .code_repair_contract import issues
    votes=[{'role':p['role'],'handoff_id':p['handoff_id'],**copy.deepcopy(p['result'])} for p in history['packets']]
    if history['findings']!=issues({'delivery_hash':attempt['delivery_hash'],'review_votes':votes}):
        raise ValueError('Legacy code migration changed an original finding')
    return history


def _specification(state,attempt,fallback):
    votes=attempt.get(HISTORY,{}).get('packets',[])
    context=votes[0]['frozen']['context'] if votes else None
    if context:
        return next((s for s in state.get('specifications',[]) if s['spec_hash']==context['spec_hash']
            and s['document']==context['implementation_document']),fallback)
    if attempt.get('review_votes'):
        return next((s for s in state.get('specifications',[])
            if s['spec_hash']==attempt['review_votes'][0].get('spec_hash')),fallback)
    return fallback


def inherit_findings(engine,run_id,state,specification,attempt):
    if attempt.get(HISTORY):return
    parents=[a for a in state['attempts'] if a['index']<attempt['index'] and a.get('review_votes')]
    if not parents:return
    parent=max(parents,key=lambda a:a['index'])
    history=validate_history(engine,run_id,state,_specification(state,parent,specification),parent)
    if history is None:return
    origin=parent['index'] if parent.get(HISTORY) else parent[LINEAGE]['origin_attempt']
    ref={'version':1,'origin_attempt':origin,'parent_attempt':parent['index'],
        'archive_sha256':history['sha256'],'parent_votes_sha256':full.sha(parent['review_votes'])}
    if LINEAGE in attempt and attempt[LINEAGE]!=ref:
        raise ValueError('Frozen legacy finding lineage changed')
    attempt[LINEAGE]=ref


def previous_feedback(engine,run_id,state,specification,attempt):
    parents=[a for a in state['attempts'] if a['index']<attempt['index'] and a.get('review_votes')]
    if not parents:return None
    parent=max(parents,key=lambda a:a['index']);votes=parent['review_votes']
    if any(full.KEY in v for v in votes):
        full.authenticate_complete(engine.store,run_id,votes)
        for vote in votes:
            frozen=full.authenticate(engine.store,run_id,vote)
            if (frozen['context'].get('delivery_hash')!=parent['delivery_hash']
                    or frozen['context'].get('code_sha256')!=parent['code_sha256']
                    or (frozen['context'].get('code_revision_review') or frozen['context'].get('whole_code_review',{})).get('attempt')!=parent['index']):
                raise ValueError('Previous whole-code feedback belongs to another snapshot')
    else:
        if parent.get(HISTORY):validate_history(engine,run_id,state,_specification(state,parent,specification),parent)
        else:
            if not full.members(votes):raise ValueError('Partial legacy feedback needs its authenticated migration archive')
            for vote in votes:
                frozen=engine.store.get('handoff_inputs',vote['handoff_id'])
                _validate_packet(engine,run_id,state,_specification(state,parent,specification),parent,
                    {'role':vote['role'],'handoff_id':vote['handoff_id'],'frozen':frozen,
                        'purpose':engine.store.get('handoffs',vote['handoff_id'])['purpose'],
                        'result':{k:v for k,v in vote.items() if k not in {'role','handoff_id'}}})
    body={'version':1,'source_attempt':parent['index'],'source_code_sha256':parent['code_sha256'],
        'source_delivery_hash':parent['delivery_hash'],'votes':copy.deepcopy(votes),
        'instruction':'这是同一工作流上一版真实历史意见与最小修改要求，不继承批准；两位各自核对本版完整交付并给新意见。'}
    return {**body,'sha256':full.sha(body)}


