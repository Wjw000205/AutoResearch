"""Durable experiment capsules; bounded recall instead of growing chat history."""
from __future__ import annotations

import json

from .experiment_workflow import sha
from .performance_policy import assessment_context
from .storage import now

COLLECTION = 'experiment_memories'


def recall(engine, run_id):
    """Only the latest capsule enters a request; older evidence stays on disk."""
    with engine.store._read_db() as db:
        row = db.execute("SELECT json_extract(data,'$.capsule') FROM records WHERE collection=? "
            "AND json_extract(data,'$.run_id')=? AND json_extract(data,'$.cycle')=? "
            "ORDER BY updated_at DESC,key DESC LIMIT 1", (COLLECTION, run_id, engine.cycle(run_id))).fetchone()
    capsule = json.loads(row[0]) if row else None
    from .performance_policy import current
    policy = current(engine.store, run_id)
    if policy and capsule and capsule.get('binding', {}).get('performance_policy_sha256') != policy['sha256']:
        return None  # Old-rule conclusions remain on disk, not in the new assignment.
    return capsule


def remember(engine, run_id, evidence, ref, *, decision=None, batch=None):
    """Facts and agent hypotheses have different labels and recoverable sources."""
    comparisons = evidence.get('comparisons', {})
    chosen = comparisons.get((batch or {}).get('unit_id'))
    if chosen is None:
        chosen = next((v for v in reversed(list(comparisons.values())) if v.get('comparable')), None)
    proposal = (decision or {}).get('proposal') or {}
    recent = evidence.get('rapid_revision') or {}
    core = {'binding': evidence['binding'], 'kind': 'batch' if batch else 'decision',
        'observed': {'comparison': ({k: chosen.get(k) for k in ('comparable', 'metric', 'direction',
            'n_pairs', 'baseline_score', 'proposed_score', 'relative_improvement', 'task_id')} if chosen else None),
            'coverage': evidence['ledger']['counts'], 'failure_count': len(evidence.get('failure_categories', {})),
            'batch_status': (batch or {}).get('status'), 'revision_status': recent.get('status')},
        **assessment_context(evidence),
        'agent_hypothesis': str(proposal.get('hypothesis', ''))[:320],
        'last_action': proposal.get('action') if batch else None,
        'next_action': 'reassess' if batch else proposal.get('action'),
        'verification': str(proposal.get('verification', ''))[:320],
        'stop_condition': str(proposal.get('stop_condition', ''))[:240],
        'module_targets': [c['target'][:180] for c in proposal.get('changes', [])][:3],
        'training_investigation': {k: proposal.get('training_response', {}).get(k) for k in
            ('cause', 'cause_status', 'disposition', 'followup_outcome')},
        'role_findings': [{'role': a['role'], 'observation': str(next(iter(a.get('observations', [])), ''))[:160],
            'hypothesis': str(next(iter(a.get('hypotheses', [])), ''))[:160],
            'missing_evidence': str(next(iter(a.get('missing_evidence', [])), ''))[:160]}
            for a in (decision or {}).get('analyses', [])[:3]],
        'sources': {'evidence': ref, 'decision_id': (decision or {}).get('id'),
            'batch_id': (batch or {}).get('unit_id'), 'rapid_revision_id': recent.get('id')},
        'limitations': 'Compact descriptive memory. Hypotheses are not verified causes; old code results do not approve new code.',
        'scientific_approval': False}
    identifier = run_id + ':experiment-memory:' + sha(core)
    capsule = {'id': identifier, 'sha256': sha(core), **core}
    record = {'id': identifier, 'run_id': run_id, 'cycle': evidence['binding']['cycle'],
        'capsule': capsule, 'created_at': now(), 'provenance': 'host_compressed_saved_experiment_evidence'}
    engine.store.mutate(COLLECTION, identifier, lambda old: old or record)
    return capsule


def report_packet(engine, run_id, context):
    """Reporting may read final results, but never inherits full execution history."""
    from .experiment_workflow import encoded
    digest = sha(context)
    identifier = run_id + ':report-evidence:' + digest
    record = {'id': identifier, 'run_id': run_id, 'sha256': digest, 'summary': context,
        'created_at': now(), 'provenance': 'frozen_report_sources_not_tuning_authorization'}
    engine.store.mutate('experiment_evidence', identifier, lambda old: old or record)
    def compact(value):
        if len(encoded(value)) <= 3000:
            return value
        return {'details_archived': True, 'fields': list(value)[:20] if isinstance(value, dict) else [],
                'requires_read': True}
    history = context.get('research_history') or {}
    return {'training_context_protocol': 1, 'experiment_workflow_protocol': 1,
        'research_cycle': engine.cycle(run_id),
        'objective': str(context.get('objective', ''))[:800],
        'experiment_memory': recall(engine, run_id),
        'validation_evidence': {'id': identifier, 'sha256': digest, 'read_with': 'experiments.compare'},
        'comparison': compact(context.get('comparison')),
        'evaluation': compact(context.get('evaluation')),
        'outcome': compact(context.get('outcome')),
        'report_scope': {'source': 'Saved report evidence, not authorization for further experiments',
            'cycle_count': len(history.get('cycles', [])) if isinstance(history, dict) else len(history),
            'trial_count': len(context.get('actual_trials', [])),
            'paths': ['/objective', '/proposal', '/research_state', '/comparison', '/evaluation',
                '/outcome', '/research_history', '/actual_trials'],
            'instruction': 'Read exact criteria, relevant methods/citations and cycle outcomes on demand. '
                'Do not claim complete coverage without reading archived sections. Never inject all trial parameters/logs.'}}
