"""Explicit, non-executing fact review of an already completed artifact assessment.

The source answer can fail an execution gate. It stays rejected and immutable;
only its factual observations are reviewed before a new, fully gated proposal.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from .prose_schema import validate

from .experiment_workflow import sha
from .storage import now

KEY = 'fact_review'


def reference(saved):
    return (saved.get(KEY) or {}).get('evidence') or saved['evidence']


def authenticate(engine, run_id, saved, proposal, assignment):
    from . import experiment_evidence_handoff as handoff
    key = handoff.assignment_key(engine, run_id, 'engineer', assignment['purpose'],
        assignment['instruction'], assignment['context'], assignment['schema'])
    owner = engine.store.get('handoffs', key) or {}
    answer = engine.store.get('decisions', key) or {}
    if (owner.get('status') != 'completed' or answer.get('result') != proposal
            or owner.get('output_hash') != sha(proposal)):
        raise ValueError('Fact review requires the completed original engineer answer and output hash')
    validate(proposal, assignment['schema'])
    if proposal.get('artifact_evidence_sha256') != saved['evidence']['sha256']:
        raise ValueError('Fact assessment is not bound to its original artifact snapshot')
    read = handoff.require_reads(engine, run_id, 'engineer', assignment['purpose'],
        assignment['instruction'], assignment['context'], assignment['schema'])
    spec = saved.get(KEY)
    if spec and (spec['source_handoff_id'] != key or spec['assessment_sha256'] != sha(proposal)
                 or spec['original_artifact_evidence'] != saved['evidence']):
        raise ValueError('Fact review source binding changed')
    return key, read


def register(engine, run_id, decision_id, audit_path, audit_sha256, instruction, *, dry_run=False):
    """Register a host-observed receipt once, only after paid work has settled."""
    from . import experiment_evidence_handoff as handoff
    from .experiment_decisions import _archive
    run = engine.store.get('runs', run_id) or {}
    record = engine.store.get('experiment_decisions', decision_id) or {}
    saved = record.get(handoff.KEY) or {}
    if not instruction.strip() or run.get('status') != 'blocked' or record.get('run_id') != run_id:
        raise ValueError('Fact review registration requires explicit direction on a blocked run')
    if saved.get(KEY) or saved.get('independent_review') or record.get('proposal') or record.get('votes'):
        raise ValueError('Do not replace an existing fact review, proposal or vote')
    with engine.store._read_db() as db:
        active = db.execute("SELECT key FROM records WHERE "
            "(collection IN ('experiments','handoffs') AND json_extract(data,'$.status')='running') "
            "OR (collection='experiments' AND json_extract(data,'$.status')='queued')").fetchall()
        native = db.execute("SELECT key FROM records WHERE collection='codex_reasoning' "
            "AND json_extract(data,'$.selection.purpose') LIKE ? "
            "AND coalesce(json_extract(data,'$.checkpoint.status'),'') NOT IN "
            "('completed','failed','cancelled','interrupted')", ('%'+record['evidence']['sha256']+'%',)).fetchall()
    if active or native:
        raise ValueError('Do not interrupt running experiments or unsettled review requests')
    proposal = record.get('rejected_proposal') or {}
    matches = []
    for owner in engine.store.list('handoffs', run_id):
        if owner.get('to_role') != 'engineer' or ':artifact-evidence-1:' not in owner.get('purpose', ''):
            continue
        frozen = engine.store.get('handoff_inputs', owner['id']) or {}
        answer = engine.store.get('decisions', owner['id']) or {}
        if answer.get('result') == proposal and frozen.get('context', {}).get('validation_evidence') == record['evidence']:
            matches.append({'purpose':owner['purpose'], **{k:frozen[k] for k in ('instruction','context','schema')}})
    if len(matches) != 1:
        raise ValueError('Fact review needs exactly one authenticated original artifact assessment')
    assignment = matches[0]
    source_key, read = authenticate(engine, run_id, saved, proposal, assignment)
    archive = engine.store.get('experiment_evidence', saved['evidence']['id']) or {}
    if archive.get('run_id') != run_id or sha(archive.get('summary')) != saved['evidence']['sha256']:
        raise ValueError('Original artifact evidence changed')
    path = Path(audit_path).resolve()
    raw = path.read_bytes()
    if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != audit_sha256:
        raise ValueError('Host audit receipt changed or exceeds its bounded size')
    audit = json.loads(raw)
    script = path.with_name('audit-new-baseline-checkpoint.py')
    if not script.is_file() or hashlib.sha256(script.read_bytes()).hexdigest() != audit['script_sha256']:
        raise ValueError('Original host audit script changed')
    tid = audit['trial_id']
    trial = engine.store.get('experiments', run_id+':'+tid) or {}
    if (trial.get('status') != 'completed' or trial.get('exit_code') != 0
            or audit.get('kind') != 'read_only_saved_artifact_identity_audit'
            or audit.get('historical_root_cause') != 'unknown' or audit.get('scientific_approval') is not False
            or any(audit.get(k) != 0 for k in ('fit_calls','optimizer_steps','evaluation_calls','model_restore_calls'))):
        raise ValueError('Host audit must be a completed-trial, non-executing identity observation')
    with engine.store._read_db() as db:
        source = db.execute("SELECT data FROM records WHERE collection='experiments' AND key=?",
                            (run_id+':'+tid,)).fetchone()[0]
    if hashlib.sha256(source.encode()).hexdigest() != audit['record_sha256']:
        raise ValueError('Original trial receipt changed after the audit')
    from .experiment_views import _safe_file
    for filename, digest in audit['files'].items():
        if filename not in {'best.pt','result.json','training_log.json'}:
            raise ValueError('Unexpected artifact in host checkpoint audit')
        file = _safe_file(engine, run_id, trial, 'artifacts/'+filename)
        if file is None or file.stat().st_size > 16*1024*1024 or hashlib.sha256(file.read_bytes()).hexdigest() != digest:
            raise ValueError('Audited checkpoint artifact changed')
    if set(audit['files']) != {'best.pt','result.json','training_log.json'}:
        raise ValueError('Incomplete host audit file binding')
    selected = [t for t in archive['summary']['trials'] if t['summary']['trial_id'] == tid]
    if len(selected) != 1:
        raise ValueError('Audited trial is outside the requested original artifact evidence')
    summary = selected[0]['summary']
    training = selected[0]['evaluation_details'].get('data', {}).get('training', {})
    if (summary['checkpoint'].get('sha256') != audit['files']['best.pt']
            or training.get('model_sha256') != audit['reported_model_sha256']
            or training.get('best_checkpoint_sha256') != audit['files']['best.pt']):
        raise ValueError('Audit does not match the original supplied checkpoint identities')
    # Exact existing file receipt, not another extraction or model execution.
    material = copy.deepcopy(archive['summary'])
    material['host_checkpoint_audit'] = {'receipt':audit, 'receipt_sha256':audit_sha256,
        'source':'host_read_only_saved_tensor_identity_check', 'execution_authorized':False}
    material['checkpoint_identity'] = {k:copy.deepcopy(summary[k]) for k in (
        'trial_id','seed','variant','algorithm','params_sha256','code_sha256','plan_sha256',
        'record_projection_sha256','metrics','checkpoint','training')}
    material['fact_review_scope'] = {
        'trial_id':tid, 'original_request_sha256':saved['request_sha256'],
        'source_assessment_sha256':sha(proposal), 'source_assessment_rejection':record['rejection'],
        'instruction':instruction.strip(), 'source_artifact_evidence':saved['evidence'],
        'questions':['Do the saved checkpoint tensors match the historical reported post-restore model fingerprint?',
            'Which remaining uncertainty actually blocks the next action under the original training and stability gates?'],
        'limits':'File consistency is not an independent historical execution witness, a causal explanation, or parameter stability. Do not repeat unrelated initialization/capacity audits.',
        'execution_authorized':False,'scientific_approval':False}
    if dry_run:
        return {'source_handoff_id':source_key, 'source_assessment_sha256':sha(proposal),
                'source_reads':read, 'audit_receipt_sha256':audit_sha256,
                'new_material_sha256':sha(material), 'trial_id':tid,
                'required_paths':['/checkpoint_identity','/host_checkpoint_audit','/fact_review_scope'],
                'execution_authorized':False,'scientific_approval':False}
    ref = _archive(engine, run_id, material)
    spec = {'registered_at':now(),'source_handoff_id':source_key,'assessment_sha256':sha(proposal),
        'original_artifact_evidence':copy.deepcopy(saved['evidence']), 'source_assignment':assignment,
        'source_read':read,'evidence':ref,'audit_receipt_sha256':audit_sha256,
        'required_paths':['/checkpoint_identity','/host_checkpoint_audit','/fact_review_scope'],
        'execution_authorized':False,'scientific_approval':False}
    engine.store.put('experiment_decisions', decision_id, {**record,
        handoff.KEY:{**saved,KEY:spec},'updated_at':now()})
    return spec


def review_packet(saved, packet):
    spec = saved.get(KEY)
    if spec:
        packet = {**packet, 'evidence':spec['evidence'], 'required_paths':spec['required_paths'],
                  'kind':spec.get('kind','checkpoint_identity_fact_review')}
    return packet


def synthesis_schema(schema, evidence):
    """Constrain new synthesis only; never rewrite a paid request or answer."""
    from .proposal_request_contract import frozen_criteria
    return frozen_criteria(schema, evidence)
