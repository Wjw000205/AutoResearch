"""Current code-review scope over immutable original scientific materials.

Only the new request's reading obligations change. Original evidence, identities,
scientific acceptance and execution gates stay with their existing owners.
"""
from __future__ import annotations

import copy


def scoped_root(original, navigation, archive, add):
    identity_fields = ('run_id', 'cycle', 'source_work_order', 'source_sha256',
        'author_handoff_id', 'plan_sha256', 'metric_contract_sha256',
        'previous_capability_sha256', 'current_capability_sha256')
    checks = original.get('capability_check_evidence') or {}
    reports = checks.get('reports', [])
    matching = [r for r in reports if r.get('applicable') is True]
    matching_receipts = [{**{k: r.get(k) for k in ('path', 'exit_code',
            'source_unchanged_during_checks', 'actual_junit_authenticated',
            'actual_log_authenticated', 'actual_junit_summary')},
        'documents': {kind: document['text'] for kind, document in r.get('documents', {}).items()}}
        for r in (navigation.get('capability_check_evidence') or {}).get('reports', [])
        if r.get('applicable') is True]
    # Preserve the actual negative statements and their original source/role.
    # They are review requirements, never inferred resolutions or approvals.
    objections = []
    for group in checks.get('prior_capability_feedback', []):
        for vote in group.get('votes', []):
            result = vote.get('result', {})
            if result.get('approve') is False or result.get('required_changes'):
                objections.append({'review_id': group.get('review_id', group.get('id')),
                    **copy.deepcopy(vote)})
    root = {'review_material_protocol': 3, 'review_scope': original['review_scope'],
        'source_sha256_of_exact_original': navigation['source_sha256_of_exact_original'],
        'identity': {k: original[k] for k in identity_fields if k in original},
        'reading_policy': {
            'required': 'Read this root, every current code/implementation-document material marked required in the catalog, and explicit prior objections.',
            'on_demand': 'Read scientific design, experiment plan/combinations, check reports and historical versions only for a specific missing value or conflict. Do not traverse their directories.',
            'unchanged_scientific_criteria': True,
            'approval': 'Reading and check summaries grant neither scientific acceptance nor execution permission.'},
        'current_sources': {'required_materials': len({r['source'][1] for r in navigation['runtime_capability']}),
            'implementation_documents': sorted({r['path'] for r in navigation['runtime_capability']
                if r.get('path', '').endswith(('.md', '.rst', '.txt'))})},
        'check_summary': {'status': checks.get('status'), 'matching_reports': len(matching),
            'historical_reports': len(reports) - len(matching),
            'matching_receipts': add(matching_receipts, 'Current source-matching check receipts (on demand)')},
        'on_demand': {
            'scientific_design': navigation['approved_source_design'],
            'scientific_plan': add(original['approved_base_plan'], 'Original approved scientific plan and complete combinations (on demand)'),
            'scientific_contract': add({k: original[k] for k in ('metric_contract', 'approved_acceptance')},
                'Original metric and acceptance contract (on demand)'),
            'check_evidence': add(navigation.get('capability_check_evidence'), 'Complete frozen check evidence navigation (on demand)'),
            'original_request': archive}}
    if objections:
        root['prior_objections'] = add(objections, 'Original inherited unresolved review requirements', mandatory=True)
    return root
