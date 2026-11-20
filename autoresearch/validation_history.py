"""Bind historical code-review navigation to the exact validated revision.

This is a context projection, never an acceptance or execution gate. Unknown
bindings stay fully visible. Original transcripts and current objections remain
unchanged; a different artifact's review can only be used after an exact read.
"""
from __future__ import annotations

import hashlib
import json
import re


FIELDS = ('spec_hash', 'code_sha256', 'delivery_hash')
ROLES = {'director', 'engineer', 'scout'}


def _digest(value):
    # The persisted review-cycle and handoff identities use this serialization.
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode('utf-8')).hexdigest()


def _binding(value):
    if not isinstance(value, dict) or any(not isinstance(value.get(k), str)
            or not re.fullmatch('[0-9a-f]{64}', value[k]) for k in FIELDS):
        return None
    return {k: value[k] for k in FIELDS}


def current_binding(store, run_id, context):
    from .history_directory import enabled
    if not enabled(context):
        return None
    purpose = str(context.get('_purpose', ''))
    if purpose.startswith('revise:cycle:'):
        context = _revision_subject(store, run_id, context)
        if context is None:
            return None
    elif purpose.split(':', 1)[0] != 'cycle_review':
        return None
    program = store.get('programs', run_id) or {}
    binding = _binding(program)
    if binding is None or program.get('run_id') != run_id:
        return None
    try:
        if (program['artifact_id'] != context['research_state']['implementation']['artifact_id']
                or hashlib.sha256(program['code'].encode('utf-8')).hexdigest() != binding['code_sha256']):
            return None
        summary = {k: v for k, v in context['validation_summary'].items() if k != 'revision_hash'}
        revision = _digest({'cycle': context['research_cycle'], 'design': context['design'],
            'code_sha': _digest(program['code']), 'selected_params': context['selection']['params'],
            'baseline_params': context['selection'].get('baseline_params'),
            'experiment_plan': context['experiment_plan'], 'validation_summary': summary})
        if (revision != context['revision_hash']
                or revision != context['validation_summary']['revision_hash']
                or summary['proposal_hash'] != context['proposal_hash']):
            return None
        votes = program['reviews']
        from . import independent_full_review as full
        compact = program.get('implementation_review_strategy') == 'document_code'
        if compact:
            if len(votes)!=2 or {v['role'] for v in votes}!=set(full.REVIEWERS):
                return None
        elif not full.members(votes):
            return None
        receipts = []
        for vote in votes:
            modern = full.KEY in vote
            if modern:
                full.authenticate(store, run_id, vote)
            hid = vote[full.KEY]['handoff_id'] if modern else vote['handoff_id']
            handoff = store.get('handoffs', hid) or {}
            decision = store.get('decisions', hid) or {}
            frozen = store.get('handoff_inputs', hid) or {}
            result = decision.get('result')
            assignment = frozen.get('context', {})
            if compact:
                work = assignment.get('implementation_work', {})
                target = work.get('materials',{}).get('identity',{})
                if (work.get('action') != 'implementation_code_review' or work.get('workflow_id') != program['delivery_id']
                        or work.get('cycle') != context['research_cycle'] or _binding(target) != binding
                        or target.get('proposal_hash') != context['proposal_hash'] or handoff.get('status') != 'completed'
                        or handoff.get('run_id')!=run_id or frozen.get('run_id')!=run_id or decision.get('run_id')!=run_id
                        or handoff.get('to_role')!=vote['role'] or decision.get('role')!=vote['role']
                        or result != vote['result'] or handoff.get('output_hash')!=_digest(result)
                        or result.get('approve') is not True or result.get('findings')!=[]):
                    return None
                receipts.append(hid)
                continue
            if (handoff.get('run_id') != run_id or handoff.get('status') != 'completed'
                    or handoff.get('to_role') != vote['role']
                    or decision.get('run_id') != run_id or decision.get('role') != vote['role']
                    or frozen.get('run_id') != run_id
                    or not str(handoff.get('purpose', '')).startswith('code_review:')
                    or result != (full.answer(vote) if modern else {k: v for k, v in vote.items() if k not in ('role', 'handoff_id')})
                    or handoff.get('output_hash') != _digest(result)
                    or vote.get('approve') is not True or vote.get('issues') != [] or vote.get('fixes') != []
                    or _binding(vote) != binding or _binding(assignment) != binding
                    or assignment.get('proposal_hash') != context['proposal_hash']
                    or vote.get('proposal_hash') != context['proposal_hash']
                    or assignment.get('research_cycle') != context['research_cycle']):
                return None
            receipts.append(hid)
        return {**binding, 'artifact_id': program['artifact_id'], 'revision_hash': revision,
                'proposal_hash': context['proposal_hash'], 'research_cycle': context['research_cycle'],
                'code_review_handoff_ids': receipts}
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def _revision_subject(store, run_id, context):
    """Recover the evaluated subject, without changing the revision assignment.

    Revision requests deliberately omit top-level proposal/revision hashes and
    selected parameters. Their saved completed review and selection provide the
    binding; a similar-looking or model-echoed review cannot substitute for it.
    """
    try:
        review = context['validation_review']
        cycle = context['research_cycle']
        if type(cycle) is not int or cycle < 1 or context['_purpose'] != f'revise:cycle:{cycle}':
            return None
        saved = store.get('research_cycles', f'{run_id}:{cycle}')
        step = store.get('steps', f'{run_id}:validation_review') or {}
        selection = store.get('selections', run_id) or {}
        if (not isinstance(review, dict) or review != saved or review.get('run_id') != run_id
                or review.get('cycle') != cycle or review.get('decision') != 'iterate'
                or step.get('run_id') != run_id or step.get('cycle') != cycle
                or step.get('status') != 'completed' or step.get('output') != review
                or selection.get('run_id') != run_id
                or _digest(selection['params']) != review.get('selection_hash')
                or _digest(context['previous_proposal']) != review.get('proposal_hash')
                or context['design'] != review.get('design')
                or context['experiment_plan'] != review.get('experiment_plan')):
            return None
        return {**context, 'proposal_hash': review['proposal_hash'], 'revision_hash': review['revision_hash'],
                'validation_summary': review['validation_summary'], 'selection': selection}
    except (KeyError, TypeError, ValueError):
        return None


def historical_binding(source_context, value, current):
    """Only trusted original assignments can identify a different code version."""
    if not current or not isinstance(source_context, dict) or not isinstance(value, dict):
        return None
    previous = _binding(source_context)
    if (str(source_context.get('_purpose', '')).split(':', 1)[0] != 'code_review'
            or previous is None or previous == _binding(current) or _binding(value) != previous
            or type(value.get('approve')) is not bool
            or source_context.get('proposal_hash') != current['proposal_hash']
            or value.get('proposal_hash') != current['proposal_hash']
            or source_context.get('research_cycle') != current['research_cycle']):
        return None
    return {'historical': previous, 'current': current}
