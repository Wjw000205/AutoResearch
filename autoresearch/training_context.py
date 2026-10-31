"""Small, explicit execution packets; archived research is never auto-injected."""
from __future__ import annotations

import copy

KEY = 'training_context_protocol'
PURPOSES = {'experiment_train', 'tune', 'cycle_review', 'loss_diagnose', 'loss_propose', 'loss_vote',
            'experiment_diagnose', 'experiment_decide', 'experiment_vote', 'revise',
            'rapid_implement', 'rapid_review', 'dataset_configure', 'dataset_config_review', 'report',
            'dataset_select', 'dataset_work_order', 'dataset_plan_review'}
FIELDS = {
    'research_anchor',
    'benchmark_task', 'benchmark_tasks',
    KEY, 'stage', 'research_cycle', 'dataset', 'evaluation', 'primary_metric', 'direction',
    'resource_plan', 'execution_constraints', 'trial_budget', 'program', 'trial',
    'plan_sha256', 'acceptance', 'iteration_policy', 'limitations', 'selection',
    'validation_summary', 'proposal_hash', 'revision_hash', 'min_relative_improvement',
    'objective', 'proposal', 'tool_results', 'current_date', '_purpose',
    '_output_token_limit', '_tool_contract_version', '_harness_control', 'output_schema_repair',
    'evidence_sha256', 'loss_evidence', 'training_scope', 'loss_analyses', 'loss_proposal',
    'proposal_sha256', 'loss_diagnostics', 'loss_review_context',
    'experiment_workflow_protocol', 'validation_evidence', 'experiment_read_history', 'experiment_notes',
    'experiment_decision', 'decision_analyses', 'decision_proposal', 'experiment_batch',
    'previous_proposal', 'validation_review', 'idea_provenance_protocol',
    'rapid_revision', 'experiment_memory', 'dataset_configuration', 'dataset_budget',
    'comparison', 'outcome', 'report_scope', 'performance_policy', 'performance_assessment',
    'dataset_planning', 'dataset_tools_protocol', 'source_tool_protocol', 'literature_notes_protocol',
    'experiment_protocol_tools',
    'experiment_note_repair',
    'experiment_review_evidence',
    'artifact_review_request', 'artifact_review_results',
    'yaml_candidate_review',
    'dataset_identity_correction',
    'tuning_scope_operator_review', 'operator_review_material',
}


def enabled(context, purpose=None):
    if context.get('dataset_transition_context_protocol') == 1:
        return str(purpose or context.get('_purpose', '')).split(':', 1)[0] in {
            'proposal', 'vote', 'amendment', 'design', 'protocol_review'}
    return (context.get(KEY) == 1
            and str(purpose or context.get('_purpose', '')).split(':', 1)[0] in PURPOSES)


def project(context):
    if context.get('dataset_transition_context_protocol') == 1:
        from .dataset_transition import project as transition_project
        return transition_project(context)
    result = {key: copy.deepcopy(value) for key, value in context.items() if key in FIELDS}
    if context.get('experiment_workflow_protocol') == 1:
        from .experiment_tools import project_receipts
        result = project_receipts(result)
    return result


def review(common):
    """Current decision evidence stays exact; source documents stay in storage."""
    design = common['design']
    result = {KEY: 1, **{k: copy.deepcopy(common[k]) for k in (
        'objective', 'research_cycle', 'execution_constraints') if k in common}}
    result.update({key: copy.deepcopy(design[key]) for key in (
        'dataset', 'evaluation', 'primary_metric', 'direction', 'resource_plan',
        'acceptance', 'iteration_policy', 'limitations') if key in design})
    result['proposal'] = {k: copy.deepcopy(v) for k, v in (common.get('proposal') or {}).items()
        if k in {'title', 'hypothesis', 'method', 'baseline', 'data_plan', 'novelty',
                 'metrics', 'risks', 'publication_gap', 'references'}}
    return result
