"""Shared original objection identities for the indexed full-round protocol.

Only original-role confirmations may close an objection. This projection never
authorizes a document, code delivery or scientific execution.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

VERSION = 1
MATERIAL_FIELDS = ('algorithm', 'branches', 'data_contract', 'parameter_contract',
                   'metrics', 'dependencies', 'acceptance_checks', 'implementation_map', 'comparison_transport')


def _normal(value):
    if isinstance(value, str):
        return re.sub(r'\s+', ' ', value).strip()
    if isinstance(value, dict):
        return {key: _normal(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_normal(item) for item in value]
    return value


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def material(spec):
    """Only executable/acceptance contracts are eligible for progress credit."""
    return {key: _digest(_normal(spec[key])) for key in MATERIAL_FIELDS if key in spec}


def _changed(previous, current):
    old, new = material(previous.get('spec', {})), material(current.get('spec', {}))
    return sorted(key for key in set(old) | set(new) if old.get(key) != new.get(key))


def objections(row):
    """Bind each issue to its original role and exact reviewed version."""
    result = {}
    if row.get('implementation_indexed_review_protocol') != VERSION:
        return []
    for vote in row.get('review_votes', []):
        role = vote.get('role')
        for index, issue in enumerate(vote.get('issues', [])):
            identity = (role, _normal(issue))
            issue_id = _digest([row.get('index'), row.get('spec_hash'), *identity])
            item = result.setdefault(identity, {
                'id': issue_id, 'origin_id': issue_id,
                'origin_version': {'index': row.get('index'), 'spec_hash': row.get('spec_hash'),
                    'document_id': (row.get('document') or {}).get('document_id'),
                    'document_sha256': (row.get('document') or {}).get('sha256')},
                'aliases': [issue_id],
                'role': role, 'issue': issue, 'fixes': []})
            from . import implementation_issue_responses as responses
            ref = responses.origin_ref(row, vote, index)
            if responses._valid_origin(ref):
                refs = item.setdefault('origin_refs', [])
                if ref not in refs:
                    refs.append(ref)
            route = next((v for v in vote.get('review_routing', {}).get('issues', [])
                          if v.get('issue_index') == index), None)
            linked = route.get('objection_id') if route else None
            if route:
                item['disposition'] = route['kind']
                item['recovery_condition'] = route['recovery_condition']
                item['required_reads'] = copy.deepcopy(route['required_reads'])
            if linked:
                item['continuation_of'] = linked
            for fix in vote.get('fixes', []):
                if fix.get('issue_index') == index and fix not in item['fixes']:
                    item['fixes'].append(copy.deepcopy(fix))
    from .implementation_review_routing import milestones
    for item in result.values():
        item['milestone_checks'] = milestones(item)
    return list(result.values())


def _partial(row):
    delivery = row.get('review_delivery')
    return isinstance(delivery, dict) and delivery.get('complete') is not True


def _bound_vote(row, vote, *, unit):
    return (vote.get('role') in {'director', 'scout', 'engineer'}
        and isinstance(vote.get('handoff_id'), str) and bool(vote['handoff_id'])
        and vote.get('spec_hash') == row.get('spec_hash')
        and vote.get('document_sha256') == (row.get('document') or {}).get('sha256')
        and not unit and vote.get('unit_id') is None)


def _delivery_valid(row, workflow_id=None):
    """Observe a current three-role collection, without granting approval."""
    delivery = row.get('review_delivery') or {}
    contract = row.get('implementation_indexed_review') or {}
    bound = contract.get('binding') or {}
    votes = row.get('review_votes', [])
    from .implementation_review_routing import digest
    return (type(row.get('implementation_indexed_review_protocol')) is int
        and row['implementation_indexed_review_protocol'] == VERSION
        and delivery.get('collection_complete') is True and delivery.get('complete') is True
        and delivery.get('mode') == 'indexed_full_round'
        and contract.get('version') == VERSION and contract.get('scientific_approval') is False
        and contract.get('sha256') == digest({k: v for k, v in contract.items() if k != 'sha256'})
        and bound.get('revision') == row.get('index')
        and bound.get('spec_hash') == row.get('spec_hash')
        and bound.get('document_id') == (row.get('document') or {}).get('document_id')
        and bound.get('document_sha256') == (row.get('document') or {}).get('sha256')
        and (workflow_id is None or bound.get('workflow_id') == workflow_id)
        and len(votes) == 3 and {vote.get('role') for vote in votes} == {'director', 'engineer', 'scout'}
        and len({vote.get('handoff_id') for vote in votes}) == 3
        and all(_bound_vote(row, vote, unit=False) for vote in votes))


def _progress_votes(row, *, include_units=False):
    if row.get('implementation_indexed_review_protocol') != VERSION or _partial(row):
        return []
    return [vote for vote in row.get('review_votes', []) if _bound_vote(row, vote, unit=False)]


def _issue_lookup(items):
    return {identity: item for item in items for identity in [item['id'], *item.get('aliases', [])]}


def _closures(vote, old_issues, current_issues, *, row=None):
    claim = vote.get('progress') or {}
    identities = claim.get('resolved_objection_ids', [])
    if not isinstance(identities, list) or any(not isinstance(identity, str) for identity in identities):
        return None
    lookup = dict(old_issues)
    if row is not None:
        current_issues = current_issues | _current_issues(objections(row), lookup)
    closed = set()
    explicit = None
    if row is not None and row.get('implementation_issue_response_protocol') == 1:
        from .implementation_issue_responses import confirmed_ids
        explicit = confirmed_ids(vote, row)
    for identity in identities:
        issue = lookup.get(identity)
        if (not issue or issue['role'] != vote.get('role')
                or (issue['role'], _normal(issue['issue'])) in current_issues
                or explicit is not None and issue['id'] not in explicit):
            return None
        closed.add(issue['id'])
    return closed


def _current_issues(observed, old_issues):
    current = {(item['role'], _normal(item['issue'])) for item in observed}
    for item in observed:
        original = old_issues.get(item.get('continuation_of'))
        if original and original['role'] == item['role']:
            current.add((original['role'], _normal(original['issue'])))
    return current


def unresolved_objections(history, *, workflow_id=None):
    """Carry dissent across all revisions; silence and partial reads never close it."""
    pending = {}
    for row in history:
        if row.get('implementation_indexed_review_protocol') != VERSION:
            continue
        if _partial(row) or not _delivery_valid(row, workflow_id):
            continue
        observed = objections(row)
        old_issues = _issue_lookup(pending.values())
        current_issues = _current_issues(observed, old_issues)
        closed = set()
        for vote in _progress_votes(row, include_units=True):
            claim = vote.get('progress') or {}
            if (type(claim.get('made_progress')) is bool and isinstance(claim.get('rationale'), str)
                    and claim['rationale'].strip()):
                closed.update(_closures(vote, old_issues, current_issues, row=row) or [])
        # New explicit-response rows require the original reviewer's per-origin
        # confirmation. A final role-wide approve cannot erase old objections.
        pending = {identity: item for identity, item in pending.items() if item['id'] not in closed}
        for item in observed:
            identity = (item['role'], _normal(item['issue']))
            origin = old_issues.get(item.get('continuation_of'))
            if origin and origin['role'] == item['role']:
                identity = next((key for key, pending_item in pending.items()
                    if pending_item['id'] == origin['id']), (origin['role'], _normal(origin['issue'])))
            elif row.get('implementation_issue_response_protocol') == 1:
                # A new unlinked issue cannot become an alias of an older one
                # merely because the wording matches. Explicit continuations
                # retain their original canonical ID and all scope origins.
                identity = (item['role'], item['id'])
            if identity not in pending:
                pending[identity] = copy.deepcopy(item)
                continue
            original = pending[identity]
            for name in ('disposition', 'recovery_condition', 'required_reads'):
                if name in item:
                    original[name] = copy.deepcopy(item[name])
            if _normal(item['issue']) != _normal(original['issue']):
                observations = original.setdefault('continuations', [])
                observation = {'issue': item['issue'], 'origin_version': item['origin_version']}
                if observation not in observations:
                    observations.append(copy.deepcopy(observation))
            for alias in item['aliases']:
                if alias not in original['aliases']:
                    original['aliases'].append(alias)
            for fix in item['fixes']:
                if fix not in original['fixes']:
                    original['fixes'].append(copy.deepcopy(fix))
    return list(pending.values())


def _prior(state, row):
    index = row.get('index', len(state.get('specifications', [])))
    return sorted((old for old in state.get('specifications', [])
                   if old.get('index', -1) < index and old.get('approved') is not None),
                  key=lambda old: old['index'])


def context(state, row, role=None):
    """Add role-local comparison facts, without rewriting a frozen assignment."""
    history = [old for old in _prior(state, row)
               if _delivery_valid(old, state.get('id'))]
    previous = history[-1] if history else None
    return {'implementation_spec_progress_protocol': VERSION,
        'previous_implementation_review': {
            'index': previous.get('index') if previous else None,
            'spec_hash': previous.get('spec_hash') if previous else None,
            'document': copy.deepcopy(previous.get('document')) if previous else None,
            'objections': [item for item in unresolved_objections(history, workflow_id=state.get('id'))
                           if role is None or item['role'] == role],
            'changed_fields': _changed(previous, row) if previous and 'spec' in row else []}}


def schema(base):
    """New assignments opt in; callers keep persisted old schemas unchanged."""
    result = copy.deepcopy(base)
    strings = {'type': 'array', 'items': {'type': 'string', 'minLength': 1}, 'uniqueItems': True}
    fields = {'made_progress': {'type': 'boolean'},
        'resolved_objection_ids': copy.deepcopy(strings), 'changed_fields': copy.deepcopy(strings),
        'rationale': {'type': 'string', 'minLength': 1, 'maxLength': 1600, 'pattern': r'\S'}}
    result['properties']['progress'] = {'type': 'object', 'properties': fields,
        'required': list(fields), 'additionalProperties': False}
    if 'progress' not in result['required']:
        result['required'].append('progress')
    return result

