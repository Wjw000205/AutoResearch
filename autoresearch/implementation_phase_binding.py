"""Separate the code-attempt cursor from the approved document identity.

This only locates a submitted source. Callers still authenticate the original
author, frozen contract and independent release before admitting any work.
"""
from . import implementation_acceptance as policy


CODE_STAGES = frozenset({'implementing', 'acceptance', 'code_review', 'completed'})
DOCUMENT_STAGES = frozenset({'specification', 'spec_review'})


def code_planning_document(state, *, definition_authoring=False):
    """Return the original planning source for code-owned recovery, else None.

    A blocked author has no code attempts. Definition-repair authoring retains
    its own prior ownership check in the callers, including after code exists.
    Frozen historical attempts need no added field or rewritten instruction.
    An independently approved definition child can be the current code spec;
    the ordinary code consumer authenticates that child and its full lineage.
    Its state.spec_hash need not equal this original planning-source hash.
    """
    stage = state.get('status')
    code_owned = (definition_authoring or stage in CODE_STAGES
                  or (stage == 'blocked' and bool(state.get('attempts'))))
    if not code_owned:
        if state.get('attempts') and stage not in DOCUMENT_STAGES | {'blocked'}:
            policy.fail('Unknown implementation recovery stage with existing code attempts')
        return None
    cursor = state.get('current_revision')
    if type(cursor) is not int or cursor < 0:
        policy.fail('Code recovery lacks an exact code revision')
    binding = state.get(policy.FIELD, {}).get('binding')
    if not isinstance(binding, dict) or type(binding.get('revision')) is not int or binding['revision'] < 0:
        policy.fail('Code recovery lacks its approved document binding')
    rows = [r for r in state.get('specifications', []) if r.get('index') == binding['revision']]
    if len(rows) != 1:
        policy.fail('Code recovery requires one exactly bound implementation document')
    row = rows[0]
    if (type(row.get('index')) is not int or row.get('approved') is not True
            or binding != {'revision': row['index'], 'spec_hash': row.get('spec_hash'),
                           'document_sha256': row.get('document', {}).get('sha256')}
            or not row.get('spec_hash') or not row.get('document', {}).get('sha256')):
        policy.fail('Code recovery document differs from its approved source binding')
    return row
