"""Authenticate exact compact-summary citations without rewriting paid answers."""
from __future__ import annotations

import copy

from .experiment_workflow import sha

KEY = 'proposal_reference_resolutions'
ALIASES = {f'/tuning_scope/{field}': f'/tuning_scope/plan/{field}'
           for field in ('strategy', 'stopping_rule')}
GUIDANCE = ('\n调参范围摘要的 strategy、stopping_rule 来自原快照 /tuning_scope/plan/strategy、'
            '/tuning_scope/plan/stopping_rule；evidence_paths 应引用这两个原始路径。')
WORKFLOW_GUIDANCE = ('\nconsensus_resolution是请求附带的协调指令，不是validation_evidence中的实测证据，'
            '不得将/consensus_resolution/...写入evidence_paths。协调要求可在rationale中说明；'
            '实际证据引用原快照路径，或绑定artifact_evidence_sha256的补充快照路径。')


def references(proposal, evidence, digest, context):
    """Only accept the two exact host projections from this frozen request."""
    from .experiment_decisions import _pointer

    paths = [path for path in proposal.get('evidence_paths', []) if path in ALIASES]
    if not paths:
        return []
    scope = evidence.get('tuning_scope') or {}
    if (sha(evidence) != digest or proposal.get('evidence_sha256') != digest
            or context.get('evidence_sha256') != digest
            or context.get('validation_evidence', {}).get('sha256') != digest
            or scope.get('status') != 'accepted' or not scope.get('plan')
            or sha(scope['plan']) != scope.get('plan_sha256')):
        raise ValueError('Decision summary reference evidence/plan binding changed')
    resolved = []
    for path in paths:
        canonical = ALIASES[path]
        try:
            value = _pointer(evidence, canonical)
            supplied = _pointer(context['experiment_decision'], path)
            if sha(supplied) != sha(value):
                raise ValueError('summary differs from its frozen source')
            # A real field can never be shadowed by a summary alias.
            if path.rsplit('/', 1)[1] in scope and sha(_pointer(evidence, path)) != sha(value):
                raise ValueError('original path conflicts with summary alias')
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ValueError(f'Decision summary reference is not exact: {path}: {exc}') from exc
        resolved.append({'cited_path': path, 'evidence_path': canonical,
                         'operation': 'identity', 'value_sha256': sha(value)})
    return resolved


def authenticate(engine, run_id, record, proposal, evidence):
    """Recheck the completed owner, input, output, and original evidence archive."""
    from .diagnostic_planning_recovery import original

    if not any(path in ALIASES for path in proposal.get('evidence_paths', [])):
        return None, None
    ref = record['evidence']
    archive = engine.store.get('experiment_evidence', ref['id']) or {}
    if (record.get('run_id') != run_id or archive.get('run_id') != run_id
            or archive.get('sha256') != ref['sha256']
            or sha(archive.get('summary')) != ref['sha256']
            or sha(evidence) != ref['sha256']):
        raise ValueError('Decision summary reference archive binding changed')
    owner = original(engine, run_id, record, proposal)
    context = engine.store.get('handoff_inputs', owner)['context']
    proof = {'version': 1, 'handoff_id': owner, 'proposal_sha256': sha(proposal),
             'evidence': copy.deepcopy(ref),
             'summary_aliases': references(proposal, evidence, ref['sha256'], context),
             'execution_authorized': False, 'scientific_approval': False}
    previous = record.get(KEY, {}).get(sha(proposal))
    if previous is not None and previous != proof:
        raise ValueError('Decision summary reference resolution changed')
    return context, proof
