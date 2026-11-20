"""One context selection policy for dispatch, previews, caches and history reads.

Current task inputs and tool receipts are never removed here. Phase contracts
select their own material; unrelated role transcripts are not implicit inputs.
Unknown assignments use their explicit packet and may retrieve via normal tools.
"""
from __future__ import annotations

import copy
import hashlib
import json

VERSION = 1
KEY = '_context_assembly'
RESEARCH = {
    'literature': 'reading', 'literature_reading': 'reading',
    'proposal': 'research_judgment', 'debate': 'research_judgment',
    'vote': 'research_judgment', 'revise': 'research_judgment',
    'amendment': 'research_judgment', 'innovation_candidate': 'research_judgment',
    'innovation_compare': 'research_judgment', 'innovation_review': 'research_judgment',
    'design': 'architecture', 'protocol_review': 'architecture',
    'cycle_review': 'evaluation', 'report': 'report',
}
READING = frozenset({'literature', 'literature_reading', 'evidence'})
JUDGMENT = frozenset({'proposal','debate','vote','revise','amendment',
    'innovation_candidate','innovation_compare','innovation_review','cycle_review'})
ARCHITECTURE = frozenset({'design','design_document_section','design_document_review',
    'design_revision','protocol_review','protocol_amendment'})


def _policy(kind, phase, rule, *, history=False, retrieval=False):
    return {'version':VERSION,'purpose_kind':kind,'phase':phase,'rule':rule,
        'history':history,'retrieval':retrieval}


def select(context, purpose=None):
    kind=str(purpose or context.get('_purpose','')).split(':',1)[0]
    # This selects material; the owning validators still authenticate evidence.
    # Re-hashing large repair packets on every cache/profile lookup is wasteful.
    if kind=='implementation_evidence_review' and isinstance(context.get('implementation_evidence_task'),dict):
        return _policy(kind,'implementation','evidence_work_group')
    if kind in {'code_repair_map','code_repair_audit','code_repair_amend'} and context.get('code_repair_protocol')==1:
        return _policy(kind,'implementation','frozen_repair_packet')
    if kind in {'context_evidence','deep_paper','paper_visual'}:
        return _policy(kind,{'context_evidence':'evidence_extraction','deep_paper':'deep_single_paper',
            'paper_visual':'paper_visual'}[kind],'single_source')
    # Classify the contract, including future child-purpose names, before the
    # broad research-purpose table. Tool-capability flags alone are not a scope.
    for field,phase in (
        ('training_context_protocol','experiment_execution'),
        ('dataset_transition_context_protocol','dataset_transition'),
        ('experiment_workflow_protocol','experiment_execution'),
        ('implementation_indexed_review_protocol','implementation'),
        ('implementation_acceptance_protocol','implementation'),
        ('implementation_phase_context_protocol','implementation'),
        ('architecture_workflow_protocol','architecture'),
        ('innovation_review_protocol','research_judgment'),
    ):
        if type(context.get(field)) is int and context[field]==1:
            return _policy(kind,phase,'contract:'+field)
    if (context.get('document_index_protocol')==1 and
            (context.get('indexed_document_context') is True or context.get('indexed_document_review') is True)):
        return _policy(kind,'documents','indexed_documents')
    if isinstance(context.get('bounded_evidence_review'),dict):
        return _policy(kind,'experiment_execution','bounded_evidence_packet')
    if context.get('literature_protocol')==2:
        return _policy(kind,RESEARCH.get(kind,'reading'),'indexed_research')
    if kind in RESEARCH:
        return _policy(kind,RESEARCH[kind],'selected_research_history',history=True,retrieval=True)
    if not kind:
        # Compatibility for direct, non-Harness readers with no assignment.
        return _policy(kind,'research','legacy_unspecified',history=True,retrieval=True)
    return _policy(kind,'task','explicit_task_packet')


def resolve(context):
    saved=context.get(KEY)
    if saved is None:
        return select(context)
    kind=str(context.get('_purpose','')).split(':',1)[0]
    if (isinstance(saved,dict) and saved.get('version')==VERSION
            and saved.get('purpose_kind')==kind
            and all(type(saved.get(k)) is bool for k in ('history','retrieval'))
            and all(isinstance(saved.get(k),str) for k in ('rule','phase'))):
        return copy.deepcopy(saved)
    # An obsolete selector never authorizes a broad history fallback or blocks
    # a reasonable current response. Its original bytes remain in the archive.
    return _policy(kind,'task','unrecognized_saved_policy')


def relevant_history(context, source, scope, value):
    """Filter by task subject before deciding which adverse facts are mandatory."""
    policy=resolve(context)
    if not policy['history']:
        return False
    if policy['rule']=='legacy_unspecified':
        return True
    kind=policy['purpose_kind'];other=str(scope.get('purpose') or '').split(':',1)[0]
    factual=any(k in value for k in ('metrics','evaluation','validation_evidence','measured_results'))
    previous=context.get('previous_cycle') or {}
    previous_hash=previous.get('proposal_hash')
    if not previous_hash and isinstance(previous.get('proposal'),dict):
        previous_hash=hashlib.sha256(json.dumps(previous['proposal'],sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()
    prior_subject=(previous_hash is not None and previous_hash==scope.get('proposal_hash')
        and previous.get('cycle')==scope.get('research_cycle'))
    receipt_ids={r.get('id') for r in context.get('previous_validation_evidence',[])
        if isinstance(r,dict) and r.get('id')}
    explicit_fact=factual and (value.get('trial_id',value.get('id')) in receipt_ids or prior_subject)
    explicit_source=other in READING and prior_subject
    for field in ('research_cycle','revision_hash','spec_hash','delivery_hash','code_sha256'):
        left=context.get(field);right=source.get(field,scope.get(field))
        if left is not None and right is not None and left!=right:
            if field=='research_cycle' and (explicit_fact or explicit_source):
                continue
            return False
    # Measurements can inform a current decision, but another task's review
    # instructions cannot become requirements merely because they are adverse.
    if factual:
        return (explicit_fact or (kind=='report' and context.get('proposal_hash') is None) or context.get('proposal_hash') is not None
            and scope.get('proposal_hash')==context['proposal_hash'])
    allowed=(READING if kind in READING else READING|JUDGMENT if kind in JUDGMENT
        else READING|JUDGMENT|ARCHITECTURE if kind in {'design','protocol_review'} else None)
    return allowed is None or other in allowed


def retrieval_query(context, task):
    # Search the current scientific question, never tool descriptions/schema or
    # the entire project objective when a narrower candidate/question exists.
    for key in ('selected_idea','proposal','research_question','question'):
        value=context.get(key)
        if isinstance(value,str) and value.strip():
            return value
        if isinstance(value,dict):
            parts=[value[k] for k in ('hypothesis','method','research_question','title')
                if isinstance(value.get(k),str) and value[k].strip()]
            if parts:
                return '\n'.join(parts)
    return context.get('objective') or task


def history_candidates(store,run_id,role,operation_id,context,*,include_code_reviews=False):
    """Read candidate bodies only after SQL filters role and task family.

    Resolve scope from the latest authoritative input for that operation. Only
    its binding fields are selected; huge old input packets are never decoded.
    Subject/version checks in relevant_history still apply to these candidates.
    """
    policy=resolve(context);kind=policy['purpose_kind']
    allowed=(READING if kind in READING else READING|JUDGMENT if kind in JUDGMENT
        else READING|JUDGMENT|ARCHITECTURE if kind in {'design','protocol_review'} else None)
    fields=('_purpose','research_cycle','proposal_hash','proposal','previous_proposal','validation_review',
        'revision_hash','spec_hash','delivery_hash','code_sha256')
    # Input ownership wins even when its field is null; an output's echoed or
    # saved scope must never repair a contradictory original assignment.
    def scope(field):
        return ("CASE WHEN source.key IS NOT NULL THEN json_extract(source.data,'$.context."+field+
            "') ELSE json_extract(output.data,'$.request_scope."+('purpose' if field=='_purpose' else field)+"') END")
    projection='json_object('+','.join("'"+k+"',"+scope(k) for k in fields)+')'
    sql=("SELECT output.data,"+projection+",source.key FROM records output LEFT JOIN records source "
        "ON source.collection='transcripts' AND source.key=(SELECT prior.key FROM records prior "
        "WHERE prior.collection='transcripts' AND json_extract(prior.data,'$.kind')='input' "
        "AND json_extract(prior.data,'$.run_id')=? AND json_extract(prior.data,'$.role')=? "
        "AND json_extract(prior.data,'$.operation_id')=json_extract(output.data,'$.operation_id') "
        "ORDER BY prior.updated_at DESC,prior.key DESC LIMIT 1) "
        "WHERE output.collection='transcripts' AND json_extract(output.data,'$.run_id')=? "
        "AND json_extract(output.data,'$.role')=? AND json_extract(output.data,'$.kind')='output' "
        "AND coalesce(json_extract(output.data,'$.operation_id'),'')!=?")
    args=[run_id,role,run_id,role,operation_id]
    if allowed is not None:
        purposes=set(allowed)|{'experiment_evidence','experiment_train','experiment_diagnose','experiment_decide'}
        if include_code_reviews:purposes.add('code_review')
        expression=scope('_purpose')
        sql+=" AND substr("+expression+",1,instr(coalesce("+expression+",'')||':',':')-1) IN (SELECT value FROM json_each(?))"
        args.append(json.dumps(sorted(purposes)))
    sql+=' ORDER BY output.updated_at DESC,output.key DESC'
    with store._read_db() as db:
        for row in db.execute(sql,args):
            source=json.loads(row[1])
            yield json.loads(row[0]),({'context':source} if row[2] is not None else None)
