"""Bounded, role-owned reading supplements before an optimization vote."""
from __future__ import annotations

import copy

from .experiment_workflow import sha


ACTIONS = {'revise_architecture', 'grid_search', 'search_parameters',
           'accept_architecture', 'complete_optimization'}
ROLES = {'director', 'engineer', 'scout'}
ERROR = 'All three agents must read comparison/ablation analysis and relevant module source before optimization'


def complete(reads, proposal):
    if proposal.get('action') not in ACTIONS:
        return True
    return (len(reads) == 3 and {r.get('role') for r in reads} == ROLES
            and all(r.get('module_source_read')
                    and set(r.get('analysis_kinds', [])) & {'comparison', 'ablation'} for r in reads))


def retained_proposal(record):
    """Only the exact missing-read rejection can reuse the already saved answer."""
    proposal = record.get('rejected_proposal') or {}
    if (record.get('status') == 'invalid' and record.get('rejection') == ERROR
            and proposal.get('action') in ACTIONS
            and proposal.get('evidence_sha256') == record['evidence']['sha256']):
        return copy.deepcopy(proposal)
    return None


async def ensure_reads(engine, harness, run_id, record, proposal, reads, context, checkpoint):
    from .engine import ResearchBlocked
    from .experiment_decisions import analysis_receipts, analysis_schema
    from .experiment_evidence_handoff import merge_reads

    if complete(reads, proposal):
        return reads
    if len(reads) != 3 or {r.get('role') for r in reads} != ROLES:
        raise ResearchBlocked(ERROR)
    digest = sha(proposal)
    repairs = copy.deepcopy(record.get('optimization_read_repairs', {}))
    saved = repairs.setdefault(digest, {
        'proposal_sha256': digest, 'initial_reads': copy.deepcopy(reads), 'assignments': {}})
    effective = copy.deepcopy(reads)
    for original in reads:
        role = original['role']
        if (original.get('module_source_read')
                and set(original.get('analysis_kinds', [])) & {'comparison', 'ablation'}):
            continue
        assignment = saved['assignments'].get(role)
        if not assignment:
            request = {k: copy.deepcopy(context[k]) for k in (
                'training_context_protocol', 'experiment_workflow_protocol', 'research_cycle',
                'validation_evidence', 'evidence_sha256') if k in context}
            request['experiment_decision'] = {'optimization_read_repair': {
                'proposal_sha256': digest, 'action': proposal['action'],
                'comparison_or_ablation_required': True,
                'comparison_unit_ids': list(context.get('experiment_decision', {}).get('comparisons', {})),
                'source_read_required': not original.get('module_source_read'),
                'module_paths': proposal.get('training_response', {}).get('module_paths', []),
                'execution_authorized': False, 'scientific_approval': False}}
            # Keep the action vocabulary for architecture-first datasets.
            if context.get('experiment_decision', {}).get('dataset_optimization'):
                request['experiment_decision']['dataset_optimization'] = copy.deepcopy(
                    context['experiment_decision']['dataset_optimization'])
            assignment = {
                'purpose': 'experiment_diagnose:' + context['evidence_sha256'] + ':optimization-read-1:' + digest,
                'instruction': (
                    '补齐本角色优化门禁缺少的只读证据。原训练统计、源码及产物复核保持有效，不重做。'
                    '先核对 experiments.status；对 validation_evidence 的精确 ID/SHA 调用 '
                    'experiments.analyze(kind=comparison 或 ablation)，comparison_unit_ids 提供既有对照定位；'
                    'limit=6000，有 next_offset 则继续。'
                    '若 source_read_required=true，再用 experiments.inspect_module(view=source) '
                    '读取相关模块；module_paths 是定位提示，不是已读证明。'
                    '只读当前冻结证据，不训练、不改代码、不访问留出集、不批准提案。'
                    '保存简短观察与真实 receipt_sha256，按 schema 返回分析，read_receipts 引用本次实际读取。'
                    '局限和冲突如实保留；补读后仍须独立投票，补读本身不代表架构或科学通过。'),
                'context': request, 'schema': analysis_schema(request)}
            saved['assignments'][role] = assignment
            await checkpoint(optimization_read_repairs=repairs, status='reading_optimization')
        if 'answer' not in assignment:
            answer = await harness.ask(run_id, role, assignment['purpose'], assignment['instruction'],
                                       assignment['context'], assignment['schema'])
            # Save even an invalid answer: resume must not create an endless retry.
            assignment['answer'] = copy.deepcopy(answer)
            await checkpoint(optimization_read_repairs=repairs, status='reading_optimization')
        answer = assignment['answer']
        if answer.get('evidence_sha256') != context['evidence_sha256']:
            raise ResearchBlocked('Optimization reading supplement changed the frozen evidence')
        receipt = analysis_receipts(engine, run_id, {**answer, 'role': role}, assignment['context'],
                                   purpose=assignment['purpose'], instruction=assignment['instruction'])
        if (not set(receipt.get('analysis_kinds', [])) & {'comparison', 'ablation'}
                or not (original.get('module_source_read') or receipt.get('module_source_read'))):
            raise ResearchBlocked(ERROR + f'; {role} reading supplement remains incomplete')
        merged = merge_reads(effective, receipt)
        updated = next(r for r in merged if r['role'] == role)
        if not (updated.get('module_source_read')
                and set(updated.get('analysis_kinds', [])) & {'comparison', 'ablation'}):
            raise ResearchBlocked(f'{role} optimization reading supplement still lacks comparison/source reads')
        assignment['read'] = receipt
        effective = merged
        await checkpoint(optimization_read_repairs=repairs)
    if not complete(effective, proposal):
        raise ResearchBlocked(ERROR)
    await checkpoint(analysis_reads=effective, optimization_read_repairs=repairs, status='proposing')
    return effective


def vote_context(record, context):
    """Keep newly read conflicts visible to the independent voters."""
    saved = record.get('optimization_read_repairs', {}).get(record.get('proposal_sha256'), {})
    if not saved:
        return context
    result = copy.deepcopy(context)
    result['experiment_decision']['optimization_read_supplements'] = {
        'proposal_sha256': saved['proposal_sha256'],
        'analyses': [{'role': role, **copy.deepcopy(value['answer'])}
                     for role, value in saved['assignments'].items() if value.get('read')],
        'approval_inherited': False}
    return result
