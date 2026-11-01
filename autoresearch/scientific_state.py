"""Immutable, rebuildable research state from committed structured outputs.

No LLM, transcript summary or previous state snapshot is an input. Projection
does not approve a hypothesis or change any execution/consensus gate.
"""
from __future__ import annotations

import copy

from .context_compiler import fingerprint
from .storage import now, redact


GROUPS = {
    'active_hypotheses': {'active_hypotheses'},
    'hypothesis_records': {'hypothesis', 'hypotheses', 'new_hypotheses'},
    'rejected_hypotheses': {'rejected_hypotheses', 'rejected_candidates'},
    'unresolved_conflicts': {'unresolved_conflicts', 'unresolved_issues', 'conflicts', 'objections',
                            'required_changes', 'counterevidence', 'contradicting_evidence', 'negative_results', 'anomalies'},
    'research_gaps': {'gaps', 'research_gaps', 'open_questions', 'missing_information', 'limitations', 'uncertainties'},
    'current_priorities': {'current_priorities', 'next_steps', 'research_agenda', 'falsification', 'predictions', 'assumptions'},
}


def project(source):
    """Copy structured fields verbatim, retaining the original JSON path."""
    result = {key: [] for key in GROUPS}
    lookup = {field: group for group, fields in GROUPS.items() for field in fields}
    def walk(value, path):
        if isinstance(value, dict):
            for key, item in value.items():
                pointer = path + '/' + key.replace('~', '~0').replace('/', '~1')
                group = lookup.get(key)
                if group and item not in (None, '', [], {}):
                    if pointer == '/outputs/debate/proposal/hypothesis':
                        group = 'active_hypotheses'
                    result[group].append({'source_path': pointer, 'value': copy.deepcopy(item)})
                else:
                    walk(item, pointer)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, path + '/' + str(index))
    walk(source['outputs'], '/outputs')
    return {'version': 1, 'research_goal': source['research_goal'], 'stage': source['stage'],
        'research_cycle': source['research_cycle'], **result,
        'execution_constraints': copy.deepcopy(source['execution_constraints']),
        'provenance': source['provenance'], 'scientific_claims_verified': False,
        'policy': 'Exact structured records from committed stages, not a summary of earlier summaries. '
                  'Hypotheses and conflicts are recorded claims, not verified facts. Original gates remain authoritative.'}


def freeze(store, run, stage, outputs, constraints):
    source = redact({'run_id': run['id'], 'research_goal': run.get('objective', ''), 'stage': stage,
        'research_cycle': run.get('research_cycle', 0), 'outputs': copy.deepcopy(outputs),
        'execution_constraints': copy.deepcopy(constraints),
        'provenance': 'committed_research_stage_outputs' if run.get('mode') == 'live' else 'demo_fixture_stage_outputs'})
    source_sha = fingerprint(source)
    source_id = f"{run['id']}:scientific-state-source:{source_sha}"
    payload = project(source)
    payload['source'] = {'collection': 'research_state_sources', 'id': source_id, 'sha256': source_sha}
    sha = fingerprint(payload)
    key = f"{run['id']}:scientific-state:{sha}"
    existing = store.get('research_states', key)
    if existing:
        if existing.get('sha256') != sha or existing.get('payload') != payload:
            raise ValueError('Scientific state snapshot integrity mismatch')
        rebuild(store, run['id'], key)
        return {**copy.deepcopy(payload), 'snapshot_id': key, 'sha256': sha}
    store.batch(puts=[
        ('research_state_sources', source_id, {'id': source_id, 'run_id': run['id'], 'sha256': source_sha,
            'payload': source, 'created_at': now()}),
        ('research_states', key, {'id': key, 'run_id': run['id'], 'sha256': sha, 'payload': payload, 'created_at': now()}),
    ])
    return {**payload, 'snapshot_id': key, 'sha256': sha}


def rebuild(store, run_id, snapshot_id):
    snapshot = store.get('research_states', snapshot_id)
    if not snapshot or snapshot.get('run_id') != run_id:
        raise ValueError('Scientific state does not belong to this research')
    payload = snapshot['payload']
    if fingerprint(payload) != snapshot.get('sha256'):
        raise ValueError('Scientific state snapshot hash mismatch')
    ref = payload['source']
    source = store.get('research_state_sources', ref['id'])
    if (not source or source.get('run_id') != run_id or fingerprint(source['payload']) != ref['sha256']
            or source.get('sha256') != ref['sha256']):
        raise ValueError('Scientific state source hash mismatch')
    rebuilt = {**project(source['payload']), 'source': copy.deepcopy(ref)}
    if rebuilt != payload:
        raise ValueError('Scientific state cannot be reproduced from committed outputs')
    return rebuilt
