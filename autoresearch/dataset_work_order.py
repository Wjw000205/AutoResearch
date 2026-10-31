"""Research engineer authors a concrete, reviewed task before native dispatch."""
from __future__ import annotations

import copy

from . import dataset_planning as planning
from .dataset_budgets import SCHEMA as BUDGET
from .experiment_workflow import sha
from .storage import now

SCHEMA = planning.obj({'dataset': planning.TEXT,
    'implementation_brief': {'type': 'string', 'minLength': 1, 'maxLength': 6000},
    'protocol': planning.obj({key: planning.TEXT for key in (
        'task', 'input_output', 'split', 'preprocessing', 'evaluation', 'baseline_comparison')}),
    'configuration_requirements': planning.TEXTS, 'changes': planning.TEXTS,
    'checks': planning.TEXTS, 'evidence_paths': {**planning.TEXTS, 'minItems': 1},
    'budget_plan': BUDGET, 'stop_conditions': planning.TEXTS})

WRITE = '''为当前实验写一份可直接派发给 Codex 的具体配置与接口实现任务单。
先用 experiments.compare 读取 /task_requirements、/dataset_binding、/dataset_configuration、/source_design 相关字段，
并用 experiments.inspect_module 定向核对数据入口、模型输入输出、训练与损失接口。需要的源码分页读，不重新讨论完整架构。
基于当前数据集任务制定输入输出、划分、预处理、指标、baseline/候选共同条件、参数配置、种子及消融要求；
说明论文依据与项目选择。按 /dataset_budget 决定执行计数方式：status=unlimited 时 budget_plan=null，声明完整实验覆盖；有限额度时按实际任务量给出 budget_plan 及诊断/修复余量的依据。
实现工作由 Codex 完成：已有 CLI 就指明应写的配置；缺参数入口或真实数据适配就指定具体文件/接口/输入输出及检查方法。
不要输出完整程序，不要泛泛要求“实现方案”，implementation_brief 必须是针对本次数据与缺项的可执行指令。
修复时先读 /configuration_repair；有 /previous_delivery 时只查看相关文件。复用已交付代码，列出最小修改与回归检查。
只编排当前这一份数据配置，其余数据集后续处理。保留原模型，实验阶段不重新立项；架构缺陷须交实验决策闭环裁决。
dataset 字段只能填写 dataset_binding.targets[0].dataset 的精确名称；资产、哈希和说明放在 implementation_brief 或 evidence_paths，不能追加到 dataset。
dataset_binding 是当前队列已经指定并取得的数据资产。核对其任务协议和接口，直接编写配置与修复任务；资产绑定记录不代表配置或训练已经通过审批。
采用有限额度时，已经批准的 frozen_budget_plan 必须原样保留，不能因修复擅自扩额。每批实验后重新判断。
证据缺项使用工具补读；未执行的兼容性检查写成后续断言，不因尚无检查回执而拒绝下发实现任务。
完整理由、协议和任务单保存，当前回复只含该 schema 的必要字段。'''


WRITE += planning.SEED_GUIDANCE


def resolve(store, saved):
    ref = saved.get('agent_work_order')
    if not ref:
        return None
    plan = store.get(planning.COLLECTION, ref['id'])
    if (not plan or plan.get('run_id') != saved['run_id'] or plan.get('status') != 'approved'
            or plan.get('result_sha256') != ref['sha256'] or sha(plan.get('result')) != ref['sha256']
            or len(plan.get('votes', [])) != 3 or {v['role'] for v in plan['votes']} != {'director','engineer','scout'}
            or not all(v['approve'] for v in plan['votes']) or ref['request_sha256'] != sha(saved['request'])):
        raise ValueError('Codex task requires a current, independently reviewed agent work order')
    planning.approval(store, plan)
    return plan


def bind_current(engine, run_id, saved):
    """The experiment queue already selected this asset; verify its identity."""
    from .dataset_targets import bind
    rows = saved['dataset_packet']['datasets']
    if len(rows) != 1 or len(rows[0].get('dataset_assets', [])) != 1:
        raise ValueError('Configuration work requires one queued dataset asset')
    row = rows[0]
    target = {'note_id': row['target']['note_id'], 'dataset': row['target']['dataset'],
        **row['dataset_assets'][0]}
    proof = bind(engine, run_id, target)
    return {'kind': 'queued_dataset_asset', 'targets': [target], 'verified_sources': [proof],
        'dataset_packet_sha256': sha(saved['dataset_packet']), 'request_sha256': sha(saved['request']),
        'scientific_approval': False, 'provenance': 'task_dataset_queue_and_verified_asset'}


def repair_assignment(engine, run_id, saved):
    """Existing code plus concrete findings can go straight to implementation.

    This is an implementation scope, never a vote or training authorization.
    The delivered configuration still goes through the normal independent gate.
    """
    from .dataset_experiments import source_evidence
    from .dataset_codex import _repair_packet, previous_files
    previous_id = saved.get('previous_codex_job_id')
    if not saved.get('repair_sequence') or not previous_id:
        return None
    job = engine.store.get('dataset_codex_jobs', previous_id)
    if not job or job.get('run_id') != run_id or job.get('setup_id') != saved['id']:
        raise ValueError('Configuration repair must reference this setup\'s prior delivery')
    evidence = source_evidence(engine, run_id, saved)
    repair = evidence.get('configuration_repair') or {}
    files = previous_files(job)
    if not files or not repair.get('kind') or not repair.get('message'):
        return None
    request, packet = _repair_packet(saved, repair, job)
    return {'kind': 'existing_configuration_repair', 'request_sha256': sha(saved['request']),
        'dataset_binding': bind_current(engine, run_id, saved),
        'previous_job_id': previous_id, 'previous_files_sha256': sha(files),
        'repair_packet_sha256': sha(packet),
        'frozen_budget_plan': request['dataset_configuration'].get('repair', {}).get('frozen_budget_plan'),
        'scientific_approval': False, 'execution_authorized': False,
        'instruction': '直接修复 repair.json 中的具体缺项，复用已交付文件。配置参数由 task.json 和设计字段决定；'
            '未限定的工程参数明确提出依据。交付可执行配置、接口代码及检查，不再编写或评审另一份任务单。'
            '交付后由宿主进行独立代码核验、预算和资源准入。'}


async def ensure(engine, harness, run_id, saved):
    import asyncio
    from .dataset_experiments import source_evidence, COLLECTION
    from .dataset_codex import _repair_packet
    approved = resolve(engine.store, saved)
    if approved:
        return approved
    direct = await asyncio.to_thread(repair_assignment, engine, run_id, saved)
    engine.check_active(run_id)
    if direct:
        if saved.get('direct_repair') != direct:
            saved.update(direct_repair=direct, dataset_binding=direct['dataset_binding'],
                status='repairing_configuration', updated_at=now())
            engine.store.put(COLLECTION, saved['id'], saved)
            engine.store.event(run_id, 'experiment.configuration_repair_dispatched',
                '将已有配置、代码及具体修复项直接交给 Codex，交付后独立核验。',
                {'setup_id': saved['id'], 'assignment_sha256': sha(direct), 'execution_authorized': False})
        return None
    saved.update(status='planning_configuration', updated_at=now())
    engine.check_active(run_id)
    engine.store.put(COLLECTION, saved['id'], saved)
    evidence = source_evidence(engine, run_id, saved)
    binding = await asyncio.to_thread(bind_current, engine, run_id, saved)
    engine.check_active(run_id)
    if saved.get('dataset_binding') != binding:
        saved['dataset_binding'] = binding
        engine.store.put(COLLECTION, saved['id'], saved)
        engine.store.event(run_id, 'experiment.dataset_binding_reused',
            '复用当前队列已指定的数据资产，直接编写配置与接口修复任务。',
            {'setup_id': saved['id'], 'binding_sha256': sha(binding), 'scientific_approval': False})
    targets = binding['targets']
    method = {k: evidence['source_design'][k] for k in ('architecture','baseline','limitations') if k in evidence['source_design']}
    previous_id = saved.get('previous_codex_job_id')
    previous_job = engine.store.get('dataset_codex_jobs', previous_id) if previous_id else None
    request, repair = _repair_packet(saved, evidence.get('configuration_repair', {}), previous_job)
    evidence.update(dataset_binding=binding, dataset_configuration=request['dataset_configuration'],
        requested_targets=targets, method=method, configuration_repair=repair,
        dataset_budget=request.get('dataset_budget'))
    unbounded = (request.get('dataset_budget') or {}).get('status') == 'unlimited'
    schema = copy.deepcopy(SCHEMA)
    schema['properties']['dataset'] = {'const': targets[0]['dataset']}
    if unbounded:
        schema['properties']['budget_plan'] = {'type': 'null'}
    if previous_job and previous_job.get('partial_delivery'):
        evidence['previous_delivery'] = previous_job['partial_delivery']
    scope = saved['id'] + ':work-order:' + sha(saved['request'])
    def check(result):
        if len(targets) != 1 or result['dataset'] != targets[0]['dataset']:
            raise ValueError(f'Work order /dataset must equal {targets[0]["dataset"]!r}; put explanations in implementation_brief')
        frozen = request['dataset_configuration'].get('repair', {}).get('frozen_budget_plan')
        if frozen and not unbounded and result['budget_plan'] != frozen:
            raise ValueError('Work order must preserve the approved dataset budget')
    from .dataset_order_identity_recovery import author_request
    plan = await planning._reviewed(engine, harness, run_id, scope=scope, evidence=evidence,
        author='engineer', instruction=WRITE, schema=schema, checker=check, author_request=author_request)
    engine.check_active(run_id)
    saved.update(agent_work_order={'id': plan['id'], 'sha256': plan['result_sha256'],
        'request_sha256': sha(saved['request']), 'author_handoff_id': plan['author_handoff_id']},
        dataset_binding=binding, updated_at=now())
    engine.store.put(COLLECTION, saved['id'], saved)
    return plan
