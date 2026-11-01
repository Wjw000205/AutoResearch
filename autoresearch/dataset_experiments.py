"""Dataset configuration, scoped loader adaptation and checks inside experiments.

Original method/code receipts remain in their original cycle. New approvals
cover a configuration and its data adapter, never a replacement architecture.
"""
from __future__ import annotations

import ast
import asyncio
import copy
import hashlib
import json
from pathlib import Path

from jsonschema import ValidationError

from .prose_schema import validate

from .experiment_workflow import sha
from .storage import now
from . import dataset_budgets
from .experiment_requirements import SEED_GUIDANCE

COLLECTION = 'dataset_experiment_setups'
TEXT = {'type': 'string', 'minLength': 1}
DRAFT = {'type': 'object', 'additionalProperties': False, 'required': ['configuration_json', 'adapter_files', 'checks', 'rationale'],
    'properties': {'configuration_json': {**TEXT, 'maxLength': 80000}, 'rationale': {**TEXT, 'maxLength': 1500},
        'adapter_files': {'type': 'array', 'maxItems': 8, 'items': {'type': 'object', 'additionalProperties': False,
            'required': ['path', 'source'], 'properties': {'path': TEXT, 'source': {**TEXT, 'maxLength': 24000}}}},
        'checks': {**TEXT, 'maxLength': 12000}, 'budget_plan': dataset_budgets.SCHEMA}}
DRAFT = {'type': 'object', 'oneOf': [DRAFT,
    {'type': 'object', 'additionalProperties': False, 'required': ['status', 'blockers', 'rationale'],
     'properties': {'status': {'const': 'blocked'}, 'rationale': {**TEXT, 'maxLength': 1500},
         'blockers': {'type': 'array', 'minItems': 1, 'maxItems': 8, 'items': {**TEXT, 'maxLength': 1200}}}}]}
WRITE = '''在实验阶段完成指定数据集的配置与运行适配，研究方法和模型源码继续复用。
先按需用 experiments.compare 读取 /source_design/resource_plan、/source_design/baseline_params、/source_design/proposed_params 等具体字段，用 experiments.inspect_module 读取入口、模型和数据接口；未读完整的字段或源码根据 next_offset 继续，不必从整份历史文档开头反复扫描。
本次任务是提出待复核的新配置。可依据任务、数据来源与既有模型接口选择目标字段、划分、预处理和参数，并说明依据；旧数据参数作为参考。配置、独立评议、CPU检查、训练依次进行，尚未执行后续检查不妨碍先交付待检查的配置及检查代码。
resource_plan 严格按 configuration_schema 填写所需资源，不把授权状态或预算说明塞入资源字段；实际准入由宿主随后核验。
返回 configuration_json：JSON 对象包含 evaluation、primary_metric、direction、acceptance、iteration_policy、resource_plan、limitations、datasets。
datasets 仅填写 dataset_configuration.datasets 中当前这一份配置；其余数据集由宿主队列随后调度，无需提前编写。每项包含 baseline_params、proposed_params、ablation_params、search_space、ablation_search_space、validation_seeds、final_test_seeds。
参数完整、可直接执行；使用一个小规模成对验证配置，先固定 search_space=[{}]，每批后由实验决策闭环重新判断。dataset_budget.scope=per_dataset_task 时同时返回 budget_plan：按当前数据集的任务要求、对照、全部种子与消融配置核算 validation、ablations、final_tests 次数，按实现和实验风险说明 configuration_checks、optimization、diagnostics、retries 余量；写明 rationale 和 requirement_refs。先读取 task_requirements_path，不能照搬其他数据集的次数。宿主核算总数并交三方评审后冻结额度，待配置时 trial_budget=null 表示尚未制定；每批完成仍重新决策，额度不是必须执行完的清单。接口检查、失败及重试均扣减本数据集预算。
dataset、dataset_assets 及 benchmark 身份由宿主绑定。comparison_task={version:1,dimensions:[{name,value,paths}]} 指明所有数据、划分、预处理与外部输入输出维度的精确 JSON 路径；各方法必须完全一致。
原数据专用指标不可直接沿用。根据任务选择共同主指标，辅助结果用于取舍分析。预处理只拟合训练数据；留出集只在 phase=test 且宿主授权后加载，不把测试集反馈用于配置选择。
已有配置入口可用时 adapter_files=[]。旧入口不支持真实数据时，仅新增 dataset_runtime/adapter.py（公开 run(config) 返回宿主 metrics/evaluation JSON）和必要的 dataset_runtime/*.py 文件；复用已有 src 模型实现，不复制重写模型、不修改模型源码、全局变量或 monkeypatch。
固定入口接受 --config、--dataset、--phase、--variant、--seed；宿主也兼容旧 params.json 位置参数。数据文件路径与资产 SHA 见 dataset_configuration.datasets。不得联网下载或生成合成数据替代真实验证。
checks 是实际执行的 CPU Python 断言：验证每份配置的数据读取、训练拟合预处理、划分隔离、baseline/候选形状兼容、CLI 与输出契约；禁止完整训练、GPU或读留出集。检查时 sys.path 已包含项目及 src，可读取配置文件 dataset_runtime/configurations.json 与本次真实数据文件。小型内存测试样例只能用于单元断言，不可当成实验测量。
只补数据读取、配置、训练入口及任务评估接口，保持模型实现；不重写架构文档、不返回想法共识。若模型结构确实不兼容，保存具体接口错误与最小复现证据，后续再由实验决策闭环判断。
如确有无法解决的缺项，返回 {status:"blocked",blockers:[具体缺项及证据],rationale:说明}；不得在 configuration_json 中伪造拒绝记录、空参数或授权字段。'''
WRITE += '''
缺项处理：现有工具能补齐的字段或源码应继续定向读取；参数未被任务指定时由你提出可检验设置并说明依据。
读取 experiments.inspect_module 时 evidence_id/expected_sha256 使用当前 validation_evidence 的 ID/SHA，source_code 是其中的源码快照，不能混用两类引用。
dataset_configuration.repair 存在时，先处理列出的缺项、评议或接口错误；上轮观察仅作定位，精确参数或源码需要时可重新读取归档页。
工具轮次结束时，明确保存尚缺字段、源码路径与下一步；宿主会派发补读和修复任务，不能把尚未阅读等同于资料不存在。'''
REVIEW = '''独立核验本次实验配置与数据接口增量。用 experiments.compare 读取 /configuration 和 /rationale；用 experiments.inspect_module 读取全部 changed_ranges。
按 /host_runtime_contract 核对实际运行接口；不得要求或批准依赖不存在宿主字段的实现。CPU 检查在 trial 根目录的 experiment.py 中执行，输入是 code_check 参数；训练参数哈希和检查参数哈希不能混同。
检查本份配置的真实数据绑定、划分与训练预处理、公平 baseline 对比、独立测试隔离、预算和 CPU 断言。其余数据集尚未配置，不作为本份配置的缺项，也不能据此声称全部实验已完成。原架构/模型源码不可重新实现或 monkeypatch，适配入口必须实际调用既有模型。
核对 budget_plan 是否对应本数据集的任务要求、种子/重复、消融与诊断需求；余量需有依据，不能任意复制固定额度。原始任务要求见 /task_requirements。预算审批与本次配置哈希绑定，超额后不能自行重置或借用别的数据集额度。
配置驱动数据选择及维度，CLI 使用 --config/--dataset/--phase/--variant/--seed；更换配置不应依赖修改模型代码。拒绝掩盖不兼容、虚构指标或合成替代。
这是数据配置及接口兼容性的审批，不重新审议想法、整份架构或原 SPEC。返回所给三个哈希与真实源码 read_receipts。批准只允许本次 CPU 检查和检查通过后的冻结小批次验证。'''


WRITE += SEED_GUIDANCE
REVIEW += SEED_GUIDANCE


def route(engine, run_id):
    from .dataset_transition import route as current
    value = current(engine.store, run_id)
    return value if value and value['body'].get('entry') == 'experiments' else None


def source(engine, run_id):
    from .dataset_transition import current, _archived
    from .dataset_configuration_queue import current as queue_current
    queue = queue_current(engine.store, run_id)
    if queue and len(queue['batches']) > 1:
        prior = queue['batches'][-2]
        if prior.get('source_release'):
            from .dataset_source_release import read
            value = read(engine, run_id, prior)
            return {**{k: value[k] for k in ('program', 'plan', 'design', 'architecture_design', 'architecture_cycle')},
                'cycle': value['source_cycle'], 'source_release': prior['source_release']}
        saved = engine.store.get(COLLECTION, prior['setup_id']) or {}
        if (saved.get('status') != 'ready' or saved.get('cycle') != prior['cycle']
                or saved.get('program', {}).get('dataset_runtime_id') != prior['setup_id']):
            raise ValueError('前一份配置缺少可复用的数据接口核验记录。')
        return {'program': saved['program'], 'plan': saved['plan'], 'design': saved['design'], 'cycle': saved['cycle'],
            'architecture_design': saved['architecture_design'], 'architecture_cycle': saved['architecture_cycle']}
    transition = current(engine.store, run_id)
    program = _archived(transition, 'programs')
    plan = _archived(transition, 'experiment_plans')
    if program.get('dataset_runtime_id'):
        previous = engine.store.get(COLLECTION, program['dataset_runtime_id'])
        if not previous or previous.get('status') != 'ready' or previous.get('program') != program or previous.get('plan') != plan:
            raise ValueError('原数据配置交付缺少匹配的核验记录。')
        return {'program': program, 'plan': plan, 'design': previous['design'], 'cycle': previous['cycle'],
                'architecture_design': previous['architecture_design'], 'architecture_cycle': previous['architecture_cycle']}
    workflow = engine.store.get('codex_workflows', program.get('delivery_id', ''), {})
    design = workflow.get('common', {}).get('design')
    if design is None:
        rows = [r for r in transition['archives'] if r['collection'] == 'steps' and r['key'] == run_id + ':design']
        import gzip
        from .record_archive import verify
        if not rows:
            raise ValueError('缺少原实验配置来源。')
        row = rows[0]
        verify(row['path'], row['sha256'], row['bytes'])
        with gzip.open(row['path'], 'rt', encoding='utf-8') as stream:
            design = json.load(stream)['output']
    if sha(design) != plan.get('design_hash'):
        raise ValueError('原模型实现与实验配置版本不一致。')
    return {'program': program, 'plan': plan, 'design': design, 'cycle': transition['request']['source_cycle'],
            'architecture_design': design, 'architecture_cycle': workflow.get('cycle', transition['request']['source_cycle'])}


def current(engine, run_id):
    from .dataset_configuration_queue import batch
    active = batch(engine.store, run_id)
    receipt = route(engine, run_id)
    return engine.store.get(COLLECTION, active['setup_id'] if active else receipt['id']) if receipt else None


def source_evidence(engine, run_id, saved):
    ref = saved['request']['validation_evidence']
    record = engine.store.get('experiment_evidence', ref['id']) or {}
    evidence = record.get('summary') or {}
    if (record.get('run_id') != run_id or sha(evidence) != ref['sha256']
            or not {'binding', 'code_sources', 'source_design', 'source_plan'} <= evidence.keys()):
        raise ValueError('实验配置的原计划与源码引用缺失或发生变化。')
    return copy.deepcopy(evidence)


def recover_configuration(engine, run_id, *, setup_id, instruction):
    """Reopen only an unimplemented configuration after a host workflow repair."""
    from .experiment_decisions import _archive
    run = engine.store.get('runs', run_id)
    saved = current(engine, run_id)
    if (not saved or saved['id'] != setup_id or saved.get('candidate') or saved.get('check')
            or run.get('mode') != 'live' or run['status'] not in {'paused', 'blocked', 'failed'}
            or not isinstance(instruction, str) or not 1 <= len(instruction.strip()) <= 4000):
        raise ValueError('需要已停止且尚未进入独立核验或执行的配置任务。')
    if (run_id in getattr(engine, 'tasks', {}) and not engine.tasks[run_id].done()
            or engine.store.active_reasoning(run_id, {'running', 'queued', 'starting', 'turn_start_dispatched',
                'thread_start_dispatched', 'thread_resume_dispatched'})):
        raise ValueError('仍有活动请求，不能更换配置任务。')
    if saved.get('configuration_recovery_protocol') == 2 and not saved.get('draft'):
        return saved
    # Do not patch a paid request, synthesize resources, or promote a refusal.
    evidence = source_evidence(engine, run_id, saved)
    if sha(source(engine, run_id)['program']) != saved['source_program_sha256']:
        raise ValueError('配置恢复的原模型版本发生变化。')
    previous = _archive(engine, run_id, {'setup': saved, 'instruction': instruction.strip()})
    request = {**saved['request'], 'dataset_configuration': {**saved['request']['dataset_configuration'],
        'recovery': {'previous_setup': previous, 'protocol': 2, 'instruction': instruction.strip()},
        'source_paths': ['/source_design/' + k for k in ('resource_plan', 'evaluation', 'primary_metric',
            'direction', 'acceptance', 'baseline_params', 'proposed_params', 'ablation_params') if k in evidence['source_design']]}}
    fresh = {k: v for k, v in saved.items() if k not in {'draft', 'configuration_repairs', 'error', 'blockers'}}
    fresh.update(request=request, status='configuring', configuration_recovery_protocol=2,
        previous_setup=previous, updated_at=now())
    engine.store.batch([(COLLECTION, setup_id, fresh), ('runs', run_id, {**run,
        'status': 'paused', 'error': None, 'updated_at': now()})])
    engine.store.event(run_id, 'experiment.configuration_recovered',
        '配置读取流程已修复，继续编写当前数据集配置；先前拒绝记录与付费请求保留。',
        {'setup_id': setup_id, 'previous_setup': previous, 'protocol': 2})
    return fresh


def require_delivery(engine, run_id, saved):
    draft = saved.get('draft') or {}
    if draft.get('status') == 'blocked':
        from .dataset_configuration_repair import NeedsRepair
        saved.update(status='repairing_configuration', blockers=draft['blockers'], error=draft['rationale'])
        engine.store.put(COLLECTION, saved['id'], saved)
        raise NeedsRepair('实验配置证据待补齐：' + '；'.join(draft['blockers'])[:1800], kind='missing_evidence')


def tuning_entry(engine, run_id):
    run = engine.store.get('runs', run_id)
    key = run.get('state', {}).get('experiment_tuning_resume')
    if not key:
        return None
    record = engine.store.get('experiment_tuning_resumes', key) or {}
    body = record.get('body', {})
    if body.get('cycle', 0) < engine.cycle(run_id):
        return None
    receipt = route(engine, run_id)
    if (not receipt or record.get('id') != key or record.get('run_id') != run_id
            or sha(body) != record.get('sha256') or body.get('entry') != 'tuning'
            or body.get('cycle') != engine.cycle(run_id) or body.get('route_id') != receipt['id']
            or body.get('route_sha256') != receipt['sha256'] or not body.get('instruction')
            or body.get('config_sha256') != sha(run['config'])):
        raise ValueError('参数微调恢复入口的任务、配置或来源已变化。')
    return key, 'tuning'


def prepare_tuning(engine, run_id, *, transition_id, instruction):
    """Explicit operator recovery; preserve old deliveries and all measurements."""
    from .dataset_transition import current as transition
    from .experiment_decisions import _archive
    run = engine.store.get('runs', run_id)
    receipt, saved = route(engine, run_id), current(engine, run_id)
    if (not receipt or transition(engine.store, run_id)['id'] != transition_id or not saved
            or run['status'] not in {'paused', 'blocked', 'failed'} or run.get('mode') != 'live'
            or not isinstance(instruction, str) or not 1 <= len(instruction.strip()) <= 4000):
        raise ValueError('需要已停止的实验配置任务及明确的参数微调恢复指令。')
    if (run_id in getattr(engine, 'tasks', {}) and not engine.tasks[run_id].done()
            or engine.store.active_reasoning(run_id, {'running', 'queued', 'starting', 'turn_start_dispatched',
                'thread_start_dispatched', 'thread_resume_dispatched'})):
        raise ValueError('仍有活动请求，暂不能切换参数微调入口。')
    existing = tuning_entry(engine, run_id)
    if existing:
        return engine.store.get('experiment_tuning_resumes', existing[0])
    if saved.get('candidate') and saved.get('status') != 'ready':
        raise ValueError('数据接口已进入复核或执行；需先处理具体检查记录，不能清空候选。')
    evidence = source_evidence(engine, run_id, saved)
    previous = _archive(engine, run_id, {'setup': saved,
        'steps': {stage: engine.store.get('steps', run_id + ':' + stage) for stage in ('experiments', 'tuning')}})
    body = {'entry': 'tuning', 'cycle': engine.cycle(run_id), 'route_id': receipt['id'],
        'route_sha256': receipt['sha256'], 'config_sha256': sha(run['config']),
        'instruction': instruction.strip(), 'previous_setup': previous, 'scientific_approval': False}
    key = run_id + ':experiment-tuning:' + sha(body)
    record = {'id': key, 'run_id': run_id, 'body': body, 'sha256': sha(body), 'created_at': now()}
    puts = [('experiment_tuning_resumes', key, record)]
    previous_step = engine.store.get('steps', run_id + ':experiments')
    if previous_step and previous_step.get('status') == 'running':
        puts.append(('steps', run_id + ':experiments', {**previous_step, 'status': 'interrupted',
            'error': '用户恢复至参数微调；未完成的配置和对照由微调入口继续。'}))
    if saved.get('status') != 'ready':
        # A new frozen request after repairing the source access contract. The
        # failed paid requests, notes and outputs remain unchanged in the journal.
        ref = _archive(engine, run_id, evidence)
        request = {**saved['request'], 'stage': 'tuning', 'validation_evidence': ref,
            'evidence_sha256': ref['sha256'], 'dataset_configuration': {
                **saved['request']['dataset_configuration'], 'recovery_id': key,
                'instruction': instruction.strip(), 'source_paths': {
                    'configuration_fields': ['/source_design/' + field for field in
                        ('resource_plan', 'evaluation', 'primary_metric', 'acceptance', 'baseline_params', 'proposed_params', 'ablation_params')
                        if field in evidence['source_design']],
                    'old_plan_fields': ['/source_plan/' + field for field in evidence['source_plan']]},
                'coverage_scope': '本轮真实数据实验计划尚未冻结。原数据的种子和消融清单是历史配置参考；'
                    '请依据当前派发的数据集，在现有预算内提出小批次配置，留出一次 CPU 检查；其余数据集随后配置。'
                    '新配置须独立复核；不得减少用户明确要求的覆盖，也不得改写旧实验。'}}
        fresh = {k: v for k, v in saved.items() if k not in {'draft', 'configuration_repairs', 'error'}}
        fresh.update(request=request, status='configuring', recovery_id=key, previous_setup=previous, updated_at=now())
        puts.append((COLLECTION, saved['id'], fresh))
    puts += [('runs', run_id, {**run, 'status': 'paused', 'stage': 'tuning', 'error': None,
        'state': {**run['state'], 'experiment_tuning_resume': key}, 'updated_at': now()}),
        ('graph_status', run_id, {'id': run_id, 'run_id': run_id, 'cycle': engine.cycle(run_id),
            'node': 'tuning', 'status': 'waiting', 'waiting_for': 'resume', 'updated_at': now()})]
    engine.store.batch(puts)
    engine.store.event(run_id, 'experiment.tuning_restored', '恢复参数微调入口；数据配置、接口检查及初始对照仍需真实完成。',
        {'recovery_id': key, 'previous_setup': previous})
    return record


def outputs(engine, run_id):
    saved = current(engine, run_id)
    original = source(engine, run_id)
    return {'design': (saved or {}).get('design', original['design']),
            'implementation': (saved or {}).get('program', original['program'])}


def configuration_schema():
    from .engine import DESIGN
    from .benchmark_matrix import PLAN_FIELDS
    fields = ('evaluation', 'primary_metric', 'direction', 'acceptance', 'iteration_policy', 'resource_plan', 'limitations')
    return {'type': 'object', 'additionalProperties': False,
        'required': [*fields, 'datasets'], 'properties': {
            **{key: DESIGN['properties'][key] for key in fields},
            'datasets': {'type': 'object', 'minProperties': 1, 'additionalProperties': {'type': 'object',
                'properties': {key: DESIGN['properties'][key] for key in PLAN_FIELDS},
                'required': list(PLAN_FIELDS), 'additionalProperties': False}}}}


def compile_configuration(engine, run_id, configuration, packet, budget_plan=None):
    from . import benchmark_matrix as matrix
    from .experiment_plan import build_plan
    from .fair_comparison import task_signature
    from .performance_policy import classify
    original = source(engine, run_id)
    allowed = set(configuration_schema()['required'])
    if not isinstance(configuration, dict):
        raise ValueError('实验配置必须为 JSON 对象。')
    if set(configuration) != allowed:
        raise ValueError(f'实验配置字段不匹配：missing={sorted(allowed - set(configuration))}; '
            f'extra={sorted(set(configuration) - allowed)}。预算单独交付 budget_plan.json。')
    from .engine import DESIGN
    try:
        validate(configuration, configuration_schema())
    except ValidationError as exc:
        raise ValueError('配置字段 ' + '/'.join(map(str, exc.absolute_path)) + ': ' + exc.message) from exc
    expected = {d['target']['dataset']: d for d in packet['datasets']}
    if set(configuration['datasets']) != set(expected):
        raise ValueError('本份配置需与当前派发的数据集一致；其他数据集保留待配置。')
    design = {**copy.deepcopy(original['design']), **{k: copy.deepcopy(v) for k, v in configuration.items() if k != 'datasets'}}
    if not isinstance(design['primary_metric'], str) or not design['primary_metric'].startswith('val_') or design['direction'] not in {'minimize', 'maximize'}:
        raise ValueError('配置必须明确验证主指标及优化方向。')
    tasks, protocols = [], {}
    for index, (name, settings) in enumerate(configuration['datasets'].items()):
        if not isinstance(settings, dict) or set(settings) != set(matrix.PLAN_FIELDS) or settings['search_space'] != [{}]:
            raise ValueError('数据切换先配置一个小规模成对批次及明确种子，不自动启动整张网格。')
        task = copy.deepcopy(settings)
        for role in ('baseline', 'proposed', 'ablation'):
            params = task[role + '_params']
            if not isinstance(params, dict):
                raise ValueError('每个比较分支必须提供参数对象。')
            params.update(dataset=name, dataset_assets=expected[name]['dataset_assets'])
            contract = params.setdefault('comparison_task', {'version': 1, 'dimensions': []})
            dimensions = contract.get('dimensions', [])
            if not any(d.get('name') == 'dataset' for d in dimensions):
                dimensions.append({'name': 'dataset', 'value': name, 'paths': ['/dataset']})
        signature = task_signature(task['baseline_params'])
        body = {'kind': 'experiment_configuration', 'transition_sha256': packet['sha256'],
            'dataset': name, 'dataset_assets': expected[name]['dataset_assets'],
            'parameters': [{'name': k, 'status': 'configured', 'values': [v]} for k, v in signature.items() if k != 'dataset_assets']}
        protocol = {**body, 'id': f'{run_id}:dataset-config:{sha(body)}', 'protocol_sha256': sha(body)}
        task.update(task_id=f'dataset-{index + 1}', dataset=name, protocol_id=protocol['id'], protocol_sha256=protocol['protocol_sha256'])
        tasks.append(task)
        protocols[protocol['id']] = protocol
    design['benchmark_tasks'] = tasks
    design['dataset'] = ', '.join(expected)
    from .engine import DESIGN
    try:
        validate({k: v for k, v in design.items() if k != 'benchmark_tasks'}, DESIGN)
    except ValidationError as exc:
        raise ValueError('配置字段 ' + '/'.join(map(str, exc.absolute_path)) + ': ' + exc.message) from exc
    from .dataset_configuration_queue import batch, current as current_queue, task_budgeted
    active = batch(engine.store, run_id)
    allowance = active['attempt_limit'] if active else engine.store.get('runs', run_id)['config']['max_trials']
    task_budget = task_budgeted(current_queue(engine.store, run_id))
    from .experiment_attempt_policy import unlimited
    unbounded = unlimited(engine.store, run_id)
    # Existing plans remain byte-identical; their old counts are historical
    # coverage operands. New uncapped configurations need no budget proposal.
    task_budget = task_budget and (not unbounded or budget_plan is not None)
    if unbounded and budget_plan is None:
        allowance = None
    if task_budget:
        allowance = dataset_budgets.total(budget_plan)
        plan_allowance = allowance - sum(budget_plan[k] for k in ('configuration_checks', 'optimization', 'diagnostics', 'retries'))
    else:
        plan_allowance = allowance - 1 if allowance is not None else None
    plan = build_plan(design, plan_allowance, protocols=protocols)
    if task_budget:
        dataset_budgets.check(budget_plan, plan)
    acceptance = classify(design['acceptance'], design['primary_metric'])
    if not any(c['metric'] == design['primary_metric'] and c['direction'] == design['direction'] for c in acceptance['criteria']):
        raise ValueError('配置未包含主指标验收定义。')
    threshold = engine.store.get('runs', run_id)['config'].get('min_relative_improvement', 0)
    if any(c['metric'] == design['primary_metric'] and c['minimum_relative_improvement'] < threshold for c in acceptance['criteria']):
        raise ValueError('数据配置不能降低任务约定的主指标标准。')
    plan.update(id=run_id, run_id=run_id, design_hash=sha(design), acceptance=acceptance, iteration_policy=design['iteration_policy'],
                dataset_configuration_protocol=1, cycle=engine.cycle(run_id))
    return design, plan


def _base_bundle(engine, run_id, original):
    if original.get('source_release'):
        from .dataset_source_release import bundle
        return bundle(engine, run_id, original)
    if original['program'].get('dataset_runtime_id'):
        saved = engine.store.get(COLLECTION, original['program']['dataset_runtime_id'])
        _reviews(engine, run_id, saved)
        from .code_bundle import validate
        checked = engine.store.get('experiments', run_id + ':' + saved['check']['id'])
        if sha(checked) != saved['check']['sha256']:
            raise ValueError('原数据接口检查回执已改变。')
        validate(saved['candidate']['bundle'], original['program']['code'])
        return saved['candidate']['bundle']
    from .codex_workflow import validate_program
    if original['program'].get('implementation_backend') != 'local_codex':
        raise ValueError('实验配置复用需要已有可核验的实现交付。')
    return validate_program(engine, run_id, original['program'], source_cycle=original['cycle'], source_only=True)


def candidate(engine, run_id, draft, original):
    from . import experiment_config, dataset_file_identity
    from .dataset_configuration_queue import batch
    from .rapid_experiment_revision import _write_files
    from .experiment_analysis import freeze_sources
    bundle = _base_bundle(engine, run_id, original)
    files = {r['path']: (Path(bundle['root']) / r['path']).read_bytes() for r in bundle['files']}
    if not draft['adapter_files']:
        if original['program'].get('execution_interface') != experiment_config.INTERFACE:
            raise ValueError('原入口只支持旧配置；请在实验阶段补齐 dataset_runtime 数据接口。')
    else:
        seen = set()
        for item in draft['adapter_files']:
            name = item['path']
            from .code_bundle import relative_file
            relative_file(name)
            if (not name.startswith('dataset_runtime/') or not name.endswith('.py')
                    or name in seen or name in {'dataset_runtime/config.py', 'dataset_runtime/configurations.json', 'dataset_runtime/host_identity.py'}):
                raise ValueError('数据适配仅允许 dataset_runtime/ 下的 Python 文件，模型源码不可改动。')
            try:
                compile(item['source'], name, 'exec')
            except SyntaxError as exc:
                raise ValueError(f'数据接口 {name}:{exc.lineno} 语法错误：{exc.msg}') from exc
            seen.add(name)
            files[name] = item['source'].encode('utf-8')
        if 'dataset_runtime/adapter.py' not in seen:
            raise ValueError('缺少统一数据适配入口 run(config)。')
        files['experiment.py'] = experiment_config.ENTRY.encode()
        files['dataset_runtime/__init__.py'] = b''
        files['dataset_runtime/config.py'] = Path(experiment_config.__file__).read_bytes()
        files['dataset_runtime/host_identity.py'] = Path(dataset_file_identity.__file__).read_bytes()
    files['dataset_runtime/configurations.json'] = draft['configuration_json'].encode()
    identity = sha(draft)
    root = engine.run_dir(run_id) / 'dataset-configurations' / identity
    frozen = _write_files(root / 'runtime', files)
    code = files['experiment.py'].decode()
    program = {**original['program'], 'code': code, 'code_sha256': hashlib.sha256(code.encode()).hexdigest(),
        'revision': original['program'].get('revision', 0) + 1,
        'dataset_runtime_id': (batch(engine.store, run_id) or {}).get('setup_id', route(engine, run_id)['id']), 'execution_interface': experiment_config.INTERFACE,
        'reviews': [], 'acceptance_scope': 'Dataset configuration and loader compatibility; original model retained'}
    # This candidate is approved by its own dataset setup. Prior rapid approval
    # authenticates reusable source only and cannot authorize another dataset.
    for key in ('rapid_revision_id', 'base_approval_reference'):
        program.pop(key, None)
    ranges = [{'path': n, 'sha256': hashlib.sha256(raw).hexdigest(), 'start': 0, 'end': len(raw.decode())}
        for n, raw in files.items() if n == 'experiment.py' or n.startswith('dataset_runtime/') if raw]
    return {'program': program, 'bundle': frozen, 'code_sources': freeze_sources(engine, run_id, program, frozen),
        'changed_ranges': ranges, 'amendment_sha256': identity,
        'checks_sha256': hashlib.sha256(draft['checks'].encode()).hexdigest()}


def _source_gate(engine, run_id):
    from .architecture_documents import gate
    original = source(engine, run_id)
    engine._consensus_gate(run_id)
    gate(engine, run_id, original['architecture_design'], source_cycle=original['architecture_cycle'])
    return original


def _reviews(engine, run_id, saved):
    from .engine import Harness
    from .rapid_experiment_revision import VOTE, review_receipts
    candidate_ = saved['candidate']
    instruction, schema = saved.get('review_instruction', REVIEW), saved.get('review_schema', VOTE)
    votes = saved.get('votes', [])
    if len(votes) != 3 or {v['role'] for v in votes} != {'director', 'engineer', 'scout'}:
        raise ValueError('实验配置与接口适配尚未取得三方独立核验。')
    for vote in votes:
        context = saved.get('review_contexts',{}).get(vote['role'],saved['review_context'])
        assignment = saved.get('review_clarifications',{}).get(vote['role'])
        expected = {'amendment_sha256': candidate_['amendment_sha256'],
            'candidate_sha256': candidate_['bundle']['sha256'], 'checks_sha256': candidate_['checks_sha256']}
        if not vote.get('approve') or vote.get('issues') or any(vote.get(k) != v for k, v in expected.items()):
            raise ValueError('实验配置仍有未解决的独立评议意见。')
        purpose = 'dataset_config_review:' + candidate_['amendment_sha256']
        current_instruction,current_schema = instruction,schema
        if assignment:
            purpose,context,current_instruction,current_schema = (assignment[k] for k in ('purpose','context','instruction','schema'))
        key = Harness.decision_key(run_id, vote['role'], purpose, current_instruction, context, current_schema)
        actual = engine.store.get('decisions', key, {})
        if actual.get('result') != {k: v for k, v in vote.items() if k != 'role'}:
            raise ValueError('实验配置审批缺少同版本真实交付。')
        review_receipts(engine, run_id, saved, vote, context, purpose=purpose, instruction=current_instruction, schema=current_schema)


async def _check(engine, run_id, saved):
    from .rapid_experiment_revision import _write_files
    from .lab import assess_resources
    _source_gate(engine, run_id)
    _reviews(engine, run_id, saved)
    run = engine.store.get('runs', run_id)
    if run['config'].get('allow_local_execution') is not True and run['config'].get('execution_mode') != 'docker':
        raise ValueError('实验数据检查需要已授权的代码执行环境。')
    raw = saved['draft']['checks']
    from .dataset_configuration_repair import NeedsRepair
    try:
        tree = ast.parse(raw)
    except SyntaxError as exc:
        raise NeedsRepair('检查代码语法错误：' + str(exc), kind='check_code') from exc
    if not any(isinstance(n, ast.Assert) for n in ast.walk(tree)):
        raise NeedsRepair('数据配置检查必须有实际行为断言。', kind='check_code')
    if any(isinstance(n, ast.Name) and n.id.startswith('__dataset_') for n in ast.walk(tree)):
        raise NeedsRepair('检查代码不能覆盖宿主断言计数器。', kind='check_code')
    from .check_instrumentation import instrument
    checked, assertion_count = instrument(raw)
    code = ('import sys,json\nfrom pathlib import Path\nsys.path[:0]=[str(Path.cwd()),str(Path.cwd()/"src")]\n'
        '__dataset_passed=set()\n' + checked + f'\nassert len(__dataset_passed)=={assertion_count}\n'
        'print(json.dumps({"metrics":{"dataset_config_check":1,"assertions":len(__dataset_passed)}}))\n')
    bundle = saved['candidate']['bundle']
    files = {r['path']: (Path(bundle['root']) / r['path']).read_bytes() for r in bundle['files']}
    files['dataset_runtime/candidate_entry.py'] = files['experiment.py']
    from .dataset_runtime_contract import dispatch_samples
    files['dataset_runtime/dispatch_samples.json'] = json.dumps(dispatch_samples(saved['plan']), ensure_ascii=False).encode()
    files['experiment.py'] = code.encode()
    check_identity = sha({name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()})
    check_bundle = _write_files(engine.run_dir(run_id) / 'dataset-checks' / check_identity, files)
    assessed = assess_resources(saved['design']['resource_plan'], await engine.execution_hardware(run_id))
    if not assessed['approved']:
        raise ValueError('数据配置检查未通过资源准入。')
    limits = {**assessed['limits'], 'execution_mode': run['config'].get('execution_mode', 'local'),
        'code_bundle': check_bundle, 'required_packages': saved['program']['dependencies'], 'gpu_allowed': False,
        'experiment_binding': {'research_cycle': saved['cycle'], 'code_revision': saved['program']['revision'],
            'code_sha256': hashlib.sha256(code.encode()).hexdigest(), 'experiment_plan_sha256': sha(saved['plan'])}}
    for key, cap in {'timeout_seconds': min(120, run['config']['experiment_timeout']), 'cpu_seconds': 120,
                      'cpu_cores': 2, 'memory_mb': 2048, 'disk_mb': 1024}.items():
        limits[key] = min(limits.get(key, cap), cap)
    assets = [binding for dataset in saved['dataset_packet']['datasets'] for binding in dataset['dataset_assets']]
    # A host instrumentation repair changes executable bytes even when the
    # reviewed candidate is unchanged. Do not replay a different failed binary.
    name = 'dataset-check-' + check_bundle['sha256'][:24]
    result = engine.store.get('experiments', run_id + ':' + name)
    if result is None:
        from .rapid_experiment_revision import _budget
        _budget(engine, run_id, 1 + saved['plan']['planned_trials'])
        engine.check_active(run_id)
        result = await engine.runner.run(run_id, code, {'phase': 'code_check', 'dataset_assets': assets},
            name, limits, engine.cancellations[run_id])
    if (result.get('status') != 'completed' or result.get('metrics', {}).get('dataset_config_check') != 1
            or result.get('metrics', {}).get('assertions') != assertion_count or result.get('limits', {}).get('code_bundle') != check_bundle):
        if result.get('status') in {'running', 'interrupted'}:
            from .engine import ResearchBlocked
            raise ResearchBlocked('数据接口检查尚未确认终态，保留执行现场后再继续。')
        raise NeedsRepair('数据接口兼容性检查未通过，需要修复适配代码或断言。', kind='interface_check',
            details={'trial_id': name, 'status': result.get('status'),
                'diagnostic_tool': 'experiments.read', 'result_sha256': sha(result)})
    return {'id': result['id'], 'sha256': sha(result), 'bundle_sha256': bundle['sha256']}


async def ensure(engine, harness, run_id):
    from .dataset_configuration_repair import NeedsRepair, prepare
    while True:
        engine.check_active(run_id)
        try:
            return await _ensure_once(engine, harness, run_id)
        except NeedsRepair as failure:
            prepare(engine, run_id, current(engine, run_id), failure)


async def review_configuration(engine, harness, run_id, saved):
    """Independent roles review the same frozen candidate concurrently."""
    from .rapid_experiment_revision import VOTE
    roles = ('director', 'engineer', 'scout')
    votes = {v['role']: v for v in saved.get('votes', [])}
    async def ask(role):
        from .dataset_review_protocol import role_context
        contexts = saved.setdefault('review_contexts',{})
        if role not in contexts:
            contexts[role] = role_context(engine,run_id,saved,role)
            engine.store.put(COLLECTION,saved['id'],saved)
        result = {k:v for k,v in votes[role].items() if k!='role'} if role in votes else await harness.ask(
            run_id, role, 'dataset_config_review:' + saved['candidate']['amendment_sha256'],
            saved.get('review_instruction', REVIEW), contexts[role], saved.get('review_schema', VOTE))
        if result.get('approve') is True and result.get('issues'):
            from .dataset_review_protocol import clarification
            assignments = saved.setdefault('review_clarifications',{})
            if role not in assignments:
                assignments[role] = clarification(engine,run_id,saved,role,result)
                engine.store.put(COLLECTION,saved['id'],saved)
            task = assignments[role]
            result = await harness.ask(run_id,role,task['purpose'],task['instruction'],task['context'],task['schema'])
        engine.check_active(run_id)
        votes[role] = {'role': role, **result}
        saved.update(votes=[votes[r] for r in roles if r in votes])
        engine.store.put(COLLECTION, saved['id'], saved)
    tasks = [asyncio.create_task(ask(role)) for role in roles if role not in votes or votes[role].get('approve') is True and votes[role].get('issues')]
    try:
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    failures = [r for r in outcomes if isinstance(r, BaseException)]
    if failures:
        raise failures[0]
    engine.check_active(run_id)
    saved.update(status='checking_data')
    engine.store.put(COLLECTION, saved['id'], saved)


async def _ensure_once(engine, harness, run_id):
    """One persisted setup; resume exact paid inputs, never mutate old requests."""
    from .dataset_transition import context
    from .dataset_configuration_repair import NeedsRepair
    from .experiment_analysis import freeze_sources
    from .experiment_decisions import _archive
    from .rapid_experiment_revision import VOTE
    from . import dataset_configuration_queue as queue
    from .dataset_transition import current as transition
    receipt = route(engine, run_id)
    saved = current(engine, run_id)
    from .dataset_configuration_repair import consume_requested
    consume_requested(engine, run_id, saved)
    if not queue.current(engine.store, run_id) and not saved:
        queue.activate(engine, run_id, '逐个配置和执行数据集，每批后依据证据重新判断。')
        saved = current(engine, run_id)
    active = queue.batch(engine.store, run_id)
    if saved and saved.get('status') == 'ready':
        validate_program(engine, run_id, saved['program'])
        return saved
    if not saved:
        original = await asyncio.to_thread(_source_gate, engine, run_id)
        packet = (await context(engine, run_id, 'experiments',
            targets=[active['target']] if active else None))['dataset_transition']
        bundle = await asyncio.to_thread(_base_bundle, engine, run_id, original)
        sources = freeze_sources(engine, run_id, original['program'], bundle)
        evidence = {'binding': {'transition_id': receipt['id'], 'source_program_sha256': sha(original['program'])},
            'code_sources': sources, 'source_design': original['design'], 'source_plan': original['plan'],
            'task_requirements': engine.store.get('runs', run_id).get('objective', '')}
        ref = _archive(engine, run_id, evidence)
        from .engine import DESIGN
        from .benchmark_matrix import PLAN_FIELDS
        from .split_identity import capability
        fields = ('evaluation', 'primary_metric', 'direction', 'acceptance', 'iteration_policy', 'resource_plan', 'limitations')
        request = {'training_context_protocol': 1, 'experiment_workflow_protocol': 1,
            'research_cycle': engine.cycle(run_id), 'stage': engine.store.get('runs', run_id)['stage'], 'validation_evidence': ref,
            'evidence_sha256': ref['sha256'], 'dataset_configuration': {'id': receipt['id'],
                'datasets': packet['datasets'], 'source_code': sources,
                'instruction': '完成当前派发的数据配置，随后独立核验并运行；其他数据集由队列继续派发。' if active else receipt['body']['instruction'],
                'pending_datasets': (queue.view(engine.store, run_id) or {}).get('pending_datasets', []),
                'budget_schema': dataset_budgets.SCHEMA, 'task_requirements_path': '/task_requirements',
                'coverage_scope': '当前真实数据配置尚未冻结。原数据的种子和消融清单是历史参考；'
                    '按当前任务要求与本份预算配置必要对照，保留一次 CPU 检查；不能更改已冻结实验或降低明确验收要求。',
                'configuration_schema': {**{key: DESIGN['properties'][key] for key in fields},
                    'datasets': {'each_value': {key: DESIGN['properties'][key] for key in PLAN_FIELDS}}},
                'result_contract': {'metrics': 'finite numeric values, configured validation metric; no test_* metrics in validation',
                    'evaluation': {'dataset_id': 'content/version identity', 'seed': 'exact config seed', 'phase': 'exact config phase',
                        'actual_params': 'the complete unmodified resolved config', 'split_identity': capability()},
                    'host_evaluation_contract_version': engine.store.get('runs', run_id)['config'].get('evaluation_contract_version', 0)},
                'logging': 'Persist training progress in artifacts/training_log.json; never include raw logs in model requests.'},
            'trial_budget': queue.trial_limit(engine.store, run_id, engine.store.get('runs', run_id)['config']['max_trials'])
                if (queue.budget(engine.store, run_id) or {}).get('status') == 'unlimited'
                else active['attempt_limit'] if active else engine.store.get('runs', run_id)['config']['max_trials'],
            'dataset_budget': queue.budget(engine.store, run_id),
            '_output_token_limit': 16000}
        saved = {'id': active['setup_id'] if active else receipt['id'], 'run_id': run_id, 'cycle': engine.cycle(run_id), 'status': 'configuring',
            'request': request, 'dataset_packet': packet, 'source_program_sha256': sha(original['program']),
            'architecture_design': original['architecture_design'], 'architecture_cycle': original['architecture_cycle'], 'created_at': now()}
        engine.store.put(COLLECTION, saved['id'], saved)
    if not saved.get('draft'):
        from . import dataset_codex
        if dataset_codex.enabled(engine, run_id):
            # Resume an already dispatched native task with its exact paid input.
            # Existing deliveries with concrete defects go directly to Codex;
            # independent review remains after implementation, before execution.
            if not saved.get('codex_job_id'):
                from .dataset_work_order import ensure as ensure_work_order
                await ensure_work_order(engine, harness, run_id, saved)
            draft = await dataset_codex.deliver(engine, harness, run_id, saved)
        else:
            from .dataset_configuration_repair import assignment
            from .engine import Harness
            task = assignment(saved)
            draft = await harness.ask(run_id, 'engineer', task['purpose'], WRITE, task['context'], DRAFT)
            saved['configuration_owner'] = Harness.decision_key(run_id, 'engineer', task['purpose'], WRITE, task['context'], DRAFT)
        validate(draft, DRAFT)
        # Retain invalid configuration deliveries too; no paid request is silently reinterpreted.
        saved.update(draft=draft, status='checking_configuration')
        engine.store.put(COLLECTION, saved['id'], saved)
    from .dataset_work_order import resolve as resolve_work_order
    work_order = resolve_work_order(engine.store, saved)
    if (work_order and saved['draft'].get('status') != 'blocked'
            and saved['draft'].get('budget_plan') != work_order['result']['budget_plan']):
        raise NeedsRepair('Codex 交付的预算与研究 agent 任务单不一致。', kind='work_order_delivery')
    if not saved.get('candidate'):
        original = source(engine, run_id)
        require_delivery(engine, run_id, saved)
        try:
            design, plan = compile_configuration(engine, run_id, json.loads(saved['draft']['configuration_json']), saved['dataset_packet'], saved['draft'].get('budget_plan'))
            built = await asyncio.to_thread(candidate, engine, run_id, saved['draft'], original)
        except ValueError as exc:
            from .dataset_configuration_repair import NeedsRepair
            raise NeedsRepair(str(exc)) from exc
        from .dataset_runtime_contract import describe
        evidence = {'binding': {'transition_id': receipt['id'], 'plan_sha256': sha(plan)},
            'host_runtime_contract': describe(),
            'code_sources': built['code_sources'], 'configuration': json.loads(saved['draft']['configuration_json']),
            'rationale': saved['draft']['rationale'], 'budget_plan': saved['draft'].get('budget_plan'),
            'task_requirements': engine.store.get('runs', run_id).get('objective', '')}
        ref = _archive(engine, run_id, evidence)
        review_context = {'training_context_protocol': 1, 'experiment_workflow_protocol': 1,
            'research_cycle': engine.cycle(run_id), 'stage': saved['request']['stage'],
            'validation_evidence': ref, 'evidence_sha256': ref['sha256'],
            'dataset_configuration': {'amendment_sha256': built['amendment_sha256'],
                'candidate_sha256': built['bundle']['sha256'], 'checks_sha256': built['checks_sha256'],
                'changed_ranges': built['changed_ranges'], 'checks': saved['draft']['checks'],
                'trial_budget': None if (queue.budget(engine.store, run_id) or {}).get('status') == 'unlimited'
                    else dataset_budgets.total(saved['draft']['budget_plan']) if saved['draft'].get('budget_plan') else saved['request'].get('trial_budget'),
                'attempt_policy': queue.budget(engine.store, run_id),
                'budget_plan': saved['draft'].get('budget_plan'),
                'planned_trials': plan['planned_trials'], 'datasets': list(json.loads(saved['draft']['configuration_json'])['datasets'])}}
        saved.update(candidate=built, program=built['program'], design=design, plan=plan, review_context=review_context, status='reviewing')
        engine.store.put(COLLECTION, saved['id'], saved)
    try:
        dataset_budgets.propose(engine, run_id, saved)
    except ValueError as exc:
        from .dataset_configuration_repair import NeedsRepair
        raise NeedsRepair(str(exc), kind='budget_configuration') from exc
    from .dataset_review_protocol import prepare as prepare_review
    prepare_review(engine, run_id, saved)
    await review_configuration(engine, harness, run_id, saved)
    try:
        _reviews(engine, run_id, saved)
    except ValueError as exc:
        from .dataset_configuration_repair import NeedsRepair
        raise NeedsRepair(str(exc), kind='review', details={'votes': saved.get('votes', [])}) from exc
    dataset_budgets.approve(engine, run_id, saved)
    checked = await _check(engine, run_id, saved)
    saved.update(check=checked, status='ready', updated_at=now())
    engine.store.put(COLLECTION, saved['id'], saved)
    engine.store.put('programs', run_id, saved['program'])
    engine.store.put('experiment_plans', run_id, saved['plan'])
    engine.artifact(run_id, 'dataset-configurations/' + saved['candidate']['amendment_sha256'] + '/configuration.json',
        saved['draft']['configuration_json'], 'experiment_configuration')
    engine.store.event(run_id, 'experiment.dataset_configured', '数据配置与接口检查完成，进入小批次成对验证。',
        {'setup_id': saved['id'], 'plan_sha256': sha(saved['plan']), 'bundle_sha256': saved['candidate']['bundle']['sha256']})
    return saved


def validate_program(engine, run_id, program):
    from .code_bundle import validate as validate_bundle
    saved = current(engine, run_id)
    if not saved or saved.get('status') != 'ready' or saved['program'] != program:
        raise ValueError('新数据配置与兼容性尚未完成核验。')
    original = _source_gate(engine, run_id)
    if sha(original['program']) != saved['source_program_sha256'] or sha(saved['draft']) != saved['candidate']['amendment_sha256']:
        raise ValueError('数据适配的原实现或配置版本发生变化。')
    from .dataset_transition import current as transition
    if saved['dataset_packet']['sha256'] != transition(engine.store, run_id)['sha256']:
        raise ValueError('数据配置未绑定当前切换请求。')
    design, plan = compile_configuration(engine, run_id, json.loads(saved['draft']['configuration_json']), saved['dataset_packet'], saved['draft'].get('budget_plan'))
    if design != saved['design'] or plan != saved['plan'] or program['dependencies'] != original['program']['dependencies']:
        raise ValueError('实验配置或依赖与同版本独立核验内容不一致。')
    _reviews(engine, run_id, saved)
    checked = engine.store.get('experiments', run_id + ':' + saved['check']['id'])
    if sha(checked) != saved['check']['sha256'] or saved['check']['bundle_sha256'] != saved['candidate']['bundle']['sha256']:
        raise ValueError('数据适配缺少同版本实际检查回执。')
    validate_bundle(saved['candidate']['bundle'], program['code'])
    return saved['candidate']['bundle']


def gate(engine, run_id):
    from .engine import ResearchBlocked
    saved = current(engine, run_id)
    if not saved or saved.get('status') != 'ready':
        raise ResearchBlocked('实验数据配置尚未就绪；需完成配置、独立复核和接口检查。')
    validate_program(engine, run_id, saved['program'])


def view(engine, run_id):
    from .dataset_transition import current as transition
    if not route(engine, run_id):
        return None
    saved = current(engine, run_id) or {}
    record = transition(engine.store, run_id)
    result = {'status': saved.get('status', 'pending'), 'datasets': [t['dataset'] for t in record['request']['targets']],
        'source_cycle': record['request']['source_cycle'], 'stage': 'experiments',
        'configuration_sha256': saved.get('candidate', {}).get('amendment_sha256'),
        'reviews': [{'role': v['role'], 'approve': v['approve'], 'issues': v['issues']} for v in saved.get('votes', [])],
        'check': saved.get('check'), 'error': saved.get('error'),
        'repair_sequence': saved.get('repair_sequence', 0),
        'repair': (saved.get('request', {}).get('dataset_configuration', {}).get('repair') or {}).get('issues', []),
        'command': 'python experiment.py --config params.json --dataset <dataset> --phase validation --variant baseline --seed <seed>'}
    from .dataset_configuration_queue import view as queue_view
    from .dataset_planning import view as planning_view
    result['planning'] = planning_view(engine.store, run_id, saved['id']) if saved.get('id') else []
    result['queue'] = queue_view(engine.store, run_id)
    from .dataset_codex import view as codex_view
    result['codex'] = codex_view(engine.store, saved)
    if saved.get('candidate'):
        result['configuration_path'] = str(Path(saved['candidate']['bundle']['root']) / 'dataset_runtime/configurations.json')
    return result
