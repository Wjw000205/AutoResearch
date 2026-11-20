"""Separate an evidence-backed rejection from an unfinished source read."""


def missing_evidence_ids(result, context, role):
    from . import code_repair_contract as repair
    pending = {r['requirement_id'] for r in result.get('alignment_findings', []) if r.get('status') == 'pending'}
    scope = context.get(repair.FIELD)
    if scope:
        pending &= {i['requirement_id'] for i in scope.get('issues', []) if i['role'] == role}
    from .review_response_policy import evidence_required
    if 'repair_findings' in result:
        pending &= {f['requirement_id'] for f in result['repair_findings'] if evidence_required(f)}
    elif not result.get('fixes'):
        return []  # An unknown opinion without executable changes needs no invented code citation.
    # Legacy replies carry no disposition field. Their explicit executable
    # fixes still require evidence; absence of classification is not a waiver.
    return sorted(pending - {r['requirement_id'] for r in result.get('code_evidence', [])})


def complete_rejection(engine, run_id, key, result, context):
    """Called after full candidate validation; never creates an approval."""
    from jsonschema import ValidationError
    from .prose_schema import validate
    from .codex_workflow import REVIEW
    if result.get('approve') is not False or not result.get('issues') or not result.get('fixes'):
        return False
    try:
        for field in ('issues', 'fixes'):
            validate(result[field], REVIEW['properties'][field])
    except ValidationError:
        return False
    if {row['issue_index'] for row in result['fixes']} != set(range(len(result['issues']))):
        return False
    pending = [row for row in result.get('alignment_findings', []) if row.get('status') == 'pending']
    from . import code_repair_contract as repair
    scope = context.get(repair.FIELD)
    role = None
    if scope:
        role = engine.store.get('handoffs', key, {}).get('to_role')
        if (scope.get('version') != repair.VERSION or scope.get('spec_hash') != context.get('spec_hash')
                or role not in repair.ROLES):
            return False
        # A scoped correction must bind its own original issues. A preserved
        # pending judgment about another requirement is still unknown; it is
        # not an instruction to acquire new code evidence outside that scope.
    if (not pending or any(not row.get('source_refs') for row in pending)
            or missing_evidence_ids(result, context, role)):
        return False
    # The supplied citations were already validated by validate_candidate.
    # Unknown/dependency reports are useful completed opinions. They do not
    # require reading every pending requirement or confer execution authority.
    return True
