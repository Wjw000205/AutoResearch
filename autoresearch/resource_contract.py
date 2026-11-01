"""User capacity is distinct from the model's requested experiment allocation."""
from __future__ import annotations
from .prose_schema import validator as response_validator

import copy


def plan_schema():
    from .engine import DESIGN
    schema = copy.deepcopy(DESIGN['properties']['resource_plan'])
    schema['properties']['estimated_vram_gb'] = {'type': 'number', 'minimum': 0}
    schema['allOf'] = [{'if': {'properties': {'gpu_required': {'const': True}}, 'required': ['gpu_required']},
                       'then': {'required': ['estimated_vram_gb'], 'properties': {'estimated_vram_gb': {'exclusiveMinimum': 0}}}}]
    return schema


def issues(plan):
    from jsonschema import Draft202012Validator
    return [error.message for error in response_validator(plan_schema()).iter_errors(plan)]


async def repair_proposal(engine, harness, run_id, common, proposal):
    """Have the engineer complete missing numbers; never guess allocations."""
    from .engine import ResearchBlocked, digest
    if not issues(proposal.get('resource_plan')):
        return proposal
    hardware = await engine.execution_hardware(run_id)
    context = {**common, 'proposal': proposal, 'hardware': hardware,
        'resource_plan_protocol': 1, 'resource_plan_issues': issues(proposal.get('resource_plan')),
        'resource_policy': '用户配置是可使用上限；方案资源计划是本实验的需求，缺失不代表CPU-only或小内存。'}
    preferences = hardware.get('execution_preferences', {})
    purpose = f'resource_plan:{engine.cycle(run_id)}:{digest(proposal)}:{digest(preferences)}'
    plan = await harness.ask(run_id, 'engineer', purpose,
        '原提案的resource_plan只有文字或缺少数值。请负责补齐严格资源计划，返回计划对象。'
        '保持科学机制、数据和评估目标不变。hardware.execution_preferences是用户允许的资源上限，'
        '不是要求占满；不能把缺少gpu_required误读成禁止GPU，也不能采用2核/2GB/0.1GB作为用户限制。'
        '当提案使用CUDA/PyTorch神经模型且用户允许GPU时，按实际需要声明gpu_required=true和estimated_vram_gb；'
        '确实使用CPU的算法可以显式声明false。必须提供ram_gb、disk_gb、cpu_cores、timeout_seconds、'
        'cpu_seconds、required_packages等字段；数值结合当前硬件与任务时限。不要安装依赖、编造测量或擅自缩减科学方案。',
        context, plan_schema())
    if issues(plan):
        raise ResearchBlocked('实验工程师的资源计划仍缺少可执行数值，已保存修正记录')
    result = {**proposal, 'resource_plan': plan}
    engine.store.event(run_id, 'proposal.resources_completed', '实验工程师已补齐数值资源计划，重新提交评审',
                       {'original_proposal_hash': digest(proposal), 'proposal_hash': digest(result),
                        'resource_plan': plan, 'user_preferences': preferences})
    return result
