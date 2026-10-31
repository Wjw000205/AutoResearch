"""Separate an evidence-backed rejection from an unfinished source read."""


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
    evidenced = {row['requirement_id'] for row in result.get('code_evidence', [])}
    if not pending or any(not row.get('source_refs') or row['requirement_id'] not in evidenced for row in pending):
        return False
    from .implementation_alignment_recovery import _diagnostic
    from .code_review_context import missing_read_plan
    plan = missing_read_plan(engine, run_id, key, context,
        _diagnostic(result, '', 0, context, []))['alignment_read_correction']
    # Pending describes an unresolved judgment, not necessarily missing text.
    # The existing repair-map and independent SPEC audits still gate code dispatch.
    return all(not plan.get(field) for field in ('source_refs', 'implementation_refs', 'code_refs'))
