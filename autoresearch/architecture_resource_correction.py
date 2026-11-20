"""Same-author correction of an inadmissible premeasurement template revision."""
from copy import deepcopy

from .architecture_validation import operation
from .storage import now


def seed_template(engine, harness, run_id, pending):
    from .architecture_documents import units
    from .engine import ResearchBlocked
    if engine.store.get('handoff_progress', pending['handoff_id']):
        return
    if engine.store.get('decisions', pending['handoff_id']):
        raise ResearchBlocked('已交付的资源补正缺少原阅读记录。')
    context = pending['context']
    pages = operation(units)(engine, run_id, context['architecture_document'])
    harness._save_progress(run_id, pending['handoff_id'], {
        'turn': 0, 'seen': {}, 'tool_results': [page for group in pages for page in group]})


def handle_denial(engine, harness, run_id, record, review, parent, reply, assessment, hardware):
    from .engine import ResearchBlocked, digest
    from .dataset_configuration_queue import trial_limit
    from .execution_contract import current_constraints, host_budget_snapshot
    from .lab import assess_resources
    pending = record['pending']
    diagnostic = {'handoff_id': pending['handoff_id'], 'response_sha256': digest(reply),
                  'parent_design_hash': digest(parent), 'assessment': assessment}
    record['resource_denial'] = diagnostic
    message = '修订架构未通过当前资源准入。' + '；'.join(assessment['reasons'])
    context = pending['context']
    if (review.get('io_template_review') != 1 or context.get('resource_revision_correction')
            or context.get('architecture_integration_protocol') or context.get('architecture_delivery_repair_protocol')):
        engine.store.put('architecture_revisions', record['id'], record)
        raise ResearchBlocked(message)

    source = engine.store.get('decisions', pending['handoff_id'], {})
    if source.get('run_id') != run_id or source.get('role') != 'engineer' or source.get('result') != reply:
        raise ResearchBlocked(message + ' 原作者交付回执不一致。')
    proposal = engine.store.get('consensus', run_id)['proposal']
    controls = current_constraints(engine.store, run_id)
    budget = host_budget_snapshot(engine.store, run_id, proposal, digest(proposal), engine.cycle(run_id))
    context = {**context, 'resource_revision_correction': {
        **diagnostic, 'previous_change_summary': reply['change_summary'],
        'previous_evaluation': reply['design'].get('evaluation'), 'previous_acceptance': reply['design'].get('acceptance'),
        'parent_resource_plan': parent['resource_plan'], 'parent_assessment': assess_resources(parent['resource_plan'], hardware),
        'current_budget': budget}, 'execution_constraints': controls,
        'trial_budget': trial_limit(engine.store, run_id, engine.store.get('runs', run_id)['config']['max_trials']),
        'architecture_workflow_protocol': 1, 'document_read_window_protocol': 3,
        'architecture_required_documents': [context['architecture_document']['document_id']]}
    schema = deepcopy(pending['schema'])
    # Unchanged executable jobs are inherited from the exact rejected parent;
    # the original full author response and prior objections remain on record.
    fields = schema['properties']['design']['properties']
    allowed = ('architecture', 'evaluation', 'resource_plan', 'acceptance', 'limitations', 'iteration_policy')
    schema['properties']['design'] = {'type': 'object', 'properties': {k: fields[k] for k in allowed},
                                      'additionalProperties': False}
    purpose = pending['purpose'] + ':resource-correction-v1'
    instruction = ('按 review_fixes 定向修订父版完整模板。本次已提供模板全部正文和输入输出组合。'
        'design 只返回确需修改的顶层字段，宿主保留其余原配置；提供的字段整体替换。'
        '核对前次答复的选择偏差修正及未闭合项。以 resource_revision_correction.current_budget 和 '
        'execution_constraints 为本次预算依据，区分模型调用、实验次数、研究轮次与单次硬件限额。'
        'resource_revision_correction.parent_assessment 给出父版资源申请的当前实测准入。'
        '如父版计划满足需求，请继承；如需改动，应保留完整资源合同，注明真实峰值估计并留余量。'
        '禁止为过门禁低报需求、改变实验或自行宣告批准。需要改变科学范围时明确返回 requires_new_consensus。'
        '只列已实际解决的意见 ID，独立评审另行进行。返回给定 proposal_hash 和 parent_design_hash。')
    hid = harness._handoff(run_id, 'engineer', purpose, instruction, context, schema)['id']
    record['pending'] = {'handoff_id': hid, 'purpose': purpose, 'instruction': instruction,
                         'context': context, 'schema': schema}
    record.setdefault('resource_corrections', []).append({**diagnostic, 'correction_handoff_id': hid, 'created_at': now()})
    engine.store.put('architecture_revisions', record['id'], record)
    engine.store.event(run_id, 'architecture.resource_correction_prepared', message,
                       {'source_handoff_id': pending['handoff_id'], 'correction_handoff_id': hid})
