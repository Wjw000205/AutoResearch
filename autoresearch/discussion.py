"""Auditable discussion progress; no fixed round limit in until-stalled mode."""
import hashlib
import json
import re


MATERIAL_FIELDS = ('hypothesis', 'novelty', 'method', 'baseline', 'metrics', 'risks',
                   'resource_plan', 'data_plan', 'references', 'publication_gap')


def _normal(value):
    if isinstance(value, str):
        return re.sub(r'\s+', ' ', value.casefold()).strip()
    if isinstance(value, dict):
        return {k: _normal(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_normal(v) for v in value]
    return value


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def evidence_ids(evidence):
    evidence = evidence or {}
    identifiers = {x.get('source_id') or x.get('id') for key in ('sources', 'verified_papers')
                   for x in evidence.get(key, []) if isinstance(x, dict) and (x.get('source_id') or x.get('id'))}
    papers = {x.get('id') for x in evidence.get('verified_papers', []) if isinstance(x, dict)}
    # Controller snapshots contain only revalidated notes. A new analysis of
    # an existing source can be progress even when no new PDF was downloaded.
    identifiers.update(n['id'] for n in evidence.get('literature_notes', [])
        if isinstance(n, dict) and n.get('id') and n.get('paper_id') in papers
        and n.get('source_id') and n.get('text_sha256') and n.get('reading_scope') == 'inspected_passages'
        and n.get('scientific_claims_verified', False) is False)
    return identifiers


def platform_evidence_work(context):
    """Stable platform objection IDs shared by delivery and progress checking."""
    assessment = context.get('evidence_assessment') or {}
    if assessment.get('approved') is not False:
        return []
    issues = assessment.get('idea_provenance', {}).get('issues', [])
    issues = list(dict.fromkeys(issue for issue in issues if isinstance(issue, str) and issue.strip()))
    if not issues:
        return []
    assessment_hash = _hash(assessment)
    return [{'id': 'platform:evidence:' + _hash([context['proposal_hash'], assessment_hash, issue])[:16],
             'role': 'platform', 'assigned_role': 'scout', 'origin': 'platform_evidence_assessment',
             'proposal_hash': context['proposal_hash'], 'assessment_sha256': assessment_hash, 'text': issue}
            for issue in issues]


def review_hash(record):
    return _hash({k: v for k, v in record.items() if k not in {
        'id', 'review_id', 'run_id', 'created_at', 'amendments', 'amendments_added_at', 'progress_reassessment'}})


def effective_progress(record):
    audit = record.get('progress_reassessment') or {}
    if (audit.get('review_hash') == review_hash(record) and audit.get('derived') is True
            and audit.get('reassessed_progress', {}).get('version') == 2):
        return audit['reassessed_progress']
    return record.get('discussion_progress', {})


def previous_review(record):
    if not record:
        return {'round': None, 'objections': [], 'proposal': None}
    objections = [{'id': vote['role'] + ':' + _hash(_normal(text))[:16],
                   'role': vote['role'], 'text': text}
                  for vote in record['votes'] for text in vote.get('required_changes', [])]
    if record.get('proposal_hash') == _hash(record['proposal']):
        objections.extend(platform_evidence_work(record))
    return {'round': record['round'], 'objections': objections, 'proposal': record['proposal'],
            'amendments': record.get('amendments', []),
            'evidence_ids': sorted(evidence_ids(record.get('discussion_evidence')))}


def assess_progress(history, current, *, window=3):
    """Check progress claims against changed fields and cited prior objections.

    Semantic judgments remain attributed to the independent reviewers. A title
    rewrite, a smaller objection count, or the proposing role's assertion alone
    does not reset stagnation. Exact rejected states also detect A/B/A loops.
    """
    proposal = {key: _normal(current['proposal'].get(key)) for key in MATERIAL_FIELDS}
    available = evidence_ids(current.get('discussion_evidence'))
    def valid(vote, record):
        return not record.get('proposal_hash') or vote.get('proposal_hash') == record['proposal_hash']
    approved = {vote['role'] for vote in current['votes'] if valid(vote, current)
                and vote.get('approve') is True and not vote.get('required_changes')}
    state = _hash([proposal, sorted(available), sorted(approved)])
    def same_state(old):
        old_approved = {v['role'] for v in old['votes'] if valid(v, old)
                        and v.get('approve') is True and not v.get('required_changes')}
        return _hash([{key: _normal(old['proposal'].get(key)) for key in MATERIAL_FIELDS},
            sorted(evidence_ids(old.get('discussion_evidence'))), sorted(old_approved)]) == state
    repeated = any(same_state(old) for old in history)
    result = {'version': 2, 'state_hash': state, 'stagnation_window': window,
              'consecutive_no_progress': 0, 'stalled': False, 'repeated_state': repeated,
              'progress_roles': [], 'new_evidence_ids': [], 'changed_fields': [],
              'note': 'Progress judgments are attributed reviewer assessments, not proof of scientific novelty.'}
    if not history:
        return {**result, 'reason': 'Initial proposal establishes the comparison point.'}
    prior = history[-1]
    old_proposal = prior['proposal']
    changed = [key for key in MATERIAL_FIELDS if _normal(old_proposal.get(key)) != proposal[key]]
    new_ids = available - evidence_ids(prior.get('discussion_evidence'))
    objection_ids = {x['id'] for x in previous_review(prior)['objections']}
    previous_approved = {vote['role'] for vote in prior['votes'] if valid(vote, prior)
                         and vote.get('approve') is True and not vote.get('required_changes')}
    progress_roles = set()
    for vote in current['votes']:
        role = vote['role']
        if not valid(vote, current):
            continue
        if role in approved and role not in previous_approved:
            progress_roles.add(role)
        claim = vote.get('progress') or {}
        if claim.get('made_progress') is not True:
            continue
        resolved = set(claim.get('resolved_objection_ids', [])) & objection_ids
        changed_claim = set(claim.get('changed_fields', [])) & set(changed)
        cited_new = set(claim.get('new_evidence_ids', [])) & new_ids
        if (resolved and changed_claim) or cited_new:
            progress_roles.add(role)
    # A fresh retrieved source alone may be irrelevant. Require independent
    # reviewer support; the director cannot self-certify a paraphrase as progress.
    supported = len(progress_roles) >= 2 or len(approved) > len(previous_approved)
    assessed = all(isinstance(vote.get('progress'), dict) for vote in current['votes'])
    previous_stall = effective_progress(prior).get('consecutive_no_progress', 0)
    if supported:
        stalled_rounds = 0
    elif assessed or not changed and not new_ids:
        stalled_rounds = previous_stall + 1
    else:
        # Legacy decisions lack this protocol. Preserve them without inventing
        # retrospective reviewer opinions, and start counting on new rounds.
        stalled_rounds = 0
    stalled = repeated or stalled_rounds >= window
    reason = ('Returned to an already rejected proposal with the same evidence.' if repeated else
              f'{stalled_rounds} consecutive rounds without supported material progress.' if not supported else
              'Independent reviewers identified grounded progress or resolved an approval blocker.')
    if not assessed and not stalled and not supported:
        reason = 'Legacy review has no progress assessment; new rounds will use the progress protocol.'
    return {**result, 'consecutive_no_progress': stalled_rounds, 'stalled': stalled,
            'progress_roles': sorted(progress_roles), 'new_evidence_ids': sorted(new_ids),
            'changed_fields': changed, 'reason': reason}
