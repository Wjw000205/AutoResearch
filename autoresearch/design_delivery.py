"""Resume an explicitly reset design author at its replacement frozen request."""
from __future__ import annotations

import copy

from .storage import now


GUIDANCE = '''
论文基准设计沿用 literature.record_protocol 已登记的同一论文、版本及实验设置。
只登记对公平对比有实际影响、需要方法间统一的关键参数，例如数据版本、输入长度、预测长度，
以及本任务必须统一的目标、划分、预处理或指标口径。按已登记值实现，不能换成自有比较协议。
模型内部结构、学习率、优化器等按已批准方案配置，不要求一律作为论文协议参数登记；
不要把模型特有超参数放进共同 comparison_task。关键参数的 not_reported/conflicting 保留为缺项，
定向补读；不得按常见惯例补成论文报告值或伪造 protocol_id/哈希。
benchmark_tasks 逐项提供 protocol_id、protocol_sha256、完整三分支参数、搜索表和种子列表；
仅输入输出变化时可提交一份完整模板及 benchmark_io_matrix 的确切组合。
所有 comparison_task 维度必须指向实际参数路径并与同一登记协议匹配。真实数据沿用已选资产和清单哈希。
任务表和 comparison_task 使用统一维度名 input_length、output_length；实际参数路径可以是 /seq_len、
/prediction_length 等真实键名。宿主按已知别名核对原始论文登记，不改写登记内容或哈希；同义字段冲突仍拒绝。
批准提案与论文协议冲突时逐项列明冲突及原文依据，交独立评议修订，不能自行改写实验协议。
条件级消融清单不一致时提交显式 benchmark_tasks；paper_reported 矩阵中不安排消融的条件可写
ablation_search_space=[]，整批仍须保留明确的消融条件。不能把模板消融广播到原计划未安排的条件，或新增训练凑格式。
数据集身份字段遵循当前请求 Schema；协议是否闭合、未确认条件和执行限制写入说明字段，
不得拼接到数据集引用、task_id、protocol_id、哈希或资产身份中。
每个实际任务的三分支参数须展开完整固定配置，包括已批准的模型/训练配置和真实参数路径；根级候选不能替代任务内配置。
来源缺项仍保留 unknown/not_reported；模型内部超参数按已批准方案给出，不要求伪装成论文报告值。
'''

from .experiment_configuration import DESIGN_GUIDANCE as YAML_DESIGN_GUIDANCE
GUIDANCE += YAML_DESIGN_GUIDANCE


def assignment(engine, run_id, instruction, context, schema):
    from .engine import digest
    consensus = engine.store.get('consensus', run_id, {})
    engine._consensus_gate(run_id)
    from .benchmark_matrix import design_schema
    result = design_schema(schema)
    result.setdefault('allOf', []).append({'oneOf': [
        {'required': ['benchmark_tasks']}, {'required': ['benchmark_io_matrix']}]})
    names = sorted({row['target']['dataset'] for row in context.get('dataset_selection', {}).get('datasets', [])})
    if names:
        tasks = result['properties']['benchmark_tasks']
        tasks['items']['properties']['dataset'] = {
            'type': 'string', 'enum': names,
            'description': 'Exact approved dataset identity only; put protocol status and caveats in limitations.'}
        for name in names:
            coverage = {'contains': {'type': 'object', 'properties': {'dataset': {'const': name}},
                                     'required': ['dataset']}, 'minContains': 1}
            if coverage not in tasks.setdefault('allOf', []):
                tasks['allOf'].append(coverage)
    from .yaml_migration import requested
    yaml_request = requested(engine, run_id)
    packet = {
        **context, 'paper_protocol_design': {'version': 1, 'proposal_hash': consensus['proposal_hash'],
            'consensus_sha256': digest(consensus)},
        **({'experiment_configuration_requirement': copy.deepcopy(yaml_request)} if yaml_request else {}),
        'proposal': consensus['proposal']}
    from .dataset_contract import bind_request
    return bind_request(instruction + '\n' + GUIDANCE, packet, result)


async def prepare_protocols(engine, harness, run_id, context):
    """The Scout extracts source protocols before the engineer authors a design."""
    from .engine import ResearchBlocked, digest
    from .experiment_protocols import checked
    selection = context.get('dataset_selection', {})
    policy = context.get('paper_protocol_design')
    if not selection or not policy:
        return
    scope = {'cycle': engine.cycle(run_id), 'selection': selection['id'], 'sha256': selection['sha256'],
             'policy': policy}
    targets = [row['target'] for row in selection['datasets']]
    packet = {k: copy.deepcopy(context[k]) for k in (
        'objective', 'publication_target', 'proposal', 'research_cycle', 'evidence', 'literature_generation',
        'pdf_tools_protocol', 'literature_notes_protocol', 'paper_rag_protocol') if k in context}
    packet.update(source_tool_protocol=1, experiment_protocol_tools=1, literature_notes_protocol=1,
                  dataset_tools_protocol=1, paper_protocol_scope=scope, selected_protocol_targets=targets)
    text = {'type': 'string', 'minLength': 1}
    schema = {'type': 'object', 'properties': {
        'summary': text, 'protocols': {'type': 'array', 'maxItems': 32, 'items': {
            'type': 'object', 'properties': {'protocol_id': text,
                'protocol_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}},
            'required': ['protocol_id', 'protocol_sha256'], 'additionalProperties': False}},
        'unresolved': {'type': 'array', 'items': text}},
        'required': ['summary', 'protocols', 'unresolved'], 'additionalProperties': False}
    reuse = context.get('registered_paper_protocols')
    if reuse:
        from .protocol_receipt_reuse import result as reuse_protocol_result
        result = reuse_protocol_result(engine, run_id, reuse)
    else:
        result = await harness.ask(run_id, 'scout', 'literature_reading:design_protocol:' + digest(scope),
        '为架构工程师补齐已选数据的论文关键比较参数。只登记各方法必须统一、影响公平对比的条件，'
        '例如实际数据版本、输入长度和预测长度，以及本任务必要的目标、划分、预处理或指标口径。'
        '不要求登记全部模型内部超参数、学习率、优化器、硬件或论文的所有实现细节。'
        '复用 selected_protocol_targets 的原文锚定笔记，'
        '先用 literature.protocols 读取已有登记，缺项再按现有 source/pdf 工具定向读取原论文或官方配置，'
        '通过 literature.record_protocol 保存同一论文实验设置的关键比较参数和决定性来源定位，'
        '未报告或冲突保留原状态，不能登记自选默认值、合成设置或项目自有划分。'
        '登记后返回实际 protocol_id/protocol_sha256。数据来自某论文笔记并不证明版本或协议等价。'
        '已有完整协议直接复用，不重复下载或生成笔记。不改变已批准方法，不运行实验。',
        packet, schema)
    covered = set()
    for ref in result['protocols']:
        row = await engine._read_consensus_check(
            run_id, checked, engine, run_id, ref['protocol_id'], ref['protocol_sha256'])
        covered.add((row['dataset'], row['paper_id'], row['source_id'], row['text_sha256']))
    missing = []
    for target in targets:
        note = engine.store.get('literature_notes', target['note_id'], {})
        identity = (target['dataset'], note.get('paper_id'), note.get('source_id'), note.get('text_sha256'))
        if (note.get('run_id') != run_id or not all(identity) or identity not in covered):
            missing.append(target['dataset'])
    missing.sort()
    if missing:
        raise ResearchBlocked('已选数据仍缺真实论文登记协议：' + ', '.join(missing) + '；' + '；'.join(result['unresolved']))


def request(engine, run_id, purpose, instruction, context, schema, *, selected_plan=None):
    from .engine import Harness, ResearchBlocked, digest
    key = engine.store.get('runs', run_id, {}).get('state', {}).get('design_request_restart')
    if not key:
        return purpose, instruction, context, schema
    owner = engine.store.get('stage_contexts', key, {})
    if owner.get('cycle') != engine.cycle(run_id):
        return purpose, instruction, context, schema
    proposal = engine.store.get('consensus', run_id, {}).get('proposal_hash')
    if owner.get('run_id') != run_id or owner.get('proposal_hash') != proposal or owner.get('evidence_issue'):
        raise ValueError('Design request restart does not match this approved proposal')
    assignment = owner['assignment']
    frozen_selection = assignment['context'].get('dataset_selection') or {}
    current_selection = ({'id': selected_plan['id'], 'sha256': selected_plan['result_sha256']}
                         if selected_plan is not None else context.get('dataset_selection') or {})
    if any(frozen_selection.get(k) != current_selection.get(k) for k in ('id', 'sha256')):
        raise ResearchBlocked('重发设计绑定的数据选型版本已过期；须以当前已批准选型冻结新请求，原交接保持不变。')
    restart = owner.get('task_restarts', [])[-1]
    receipt = engine.store.get('task_restarts', restart['id'], {})
    hid = Harness.decision_key(run_id, 'engineer', **assignment)
    if (receipt.get('run_id') != run_id or receipt.get('new_handoff_id') != hid
            or receipt.get('assignment_sha256') != digest(assignment) or assignment['purpose'] != purpose):
        raise ValueError('Design restart assignment changed')
    return tuple(assignment[k] for k in ('purpose', 'instruction', 'context', 'schema'))


def prepare_restart(engine, run_id, handoff_id):
    """Prepare an operator-authorized failed delivery reset without model calls."""
    from .engine import Harness, digest
    store = engine.store
    run = store.get('runs', run_id, {})
    worker = store.get('runtime', 'worker', {})
    handoff = store.get('handoffs', handoff_id, {})
    frozen = store.get('handoff_inputs', handoff_id, {})
    decision = store.get('decisions', handoff_id, {})
    if (run.get('status') not in {'paused', 'blocked'} or run.get('stage') != 'design' or worker.get('held')
            or handoff.get('run_id') != run_id or handoff.get('stage') != 'design'
            or handoff.get('to_role') != 'engineer' or handoff.get('purpose') != 'design'
            or handoff.get('status') != 'completed' or decision.get('run_id') != run_id
            or handoff.get('output_hash') != digest(decision.get('result'))
            or store.get('steps', f'{run_id}:design', {}).get('status') == 'completed'):
        raise ValueError('A stopped failed design delivery with its exact original response is required')
    original = {'purpose': 'design', **{k: copy.deepcopy(frozen[k]) for k in ('instruction', 'context', 'schema')}}
    if Harness.decision_key(run_id, 'engineer', **original) != handoff_id:
        raise ValueError('Design input identity changed')
    from .dataset_planning import check_design
    selection = store.get('dataset_agent_plans', original['context'].get('dataset_selection', {}).get('id', ''), {})
    if selection.get('status') != 'approved':
        raise ValueError('Design reset requires its exact completed dataset selection')
    try:
        check_design(decision['result'], selection)
    except ValueError as exc:
        error = str(exc)
    else:
        raise ValueError('Design passed the recorded delivery check')
    instruction, context, schema = assignment(engine, run_id, original['instruction'],
                                                       original['context'], original['schema'])
    instruction += '\n本次重新交付源绑定的完整设计，严格依据论文登记协议，保留真实共识、来源与资产回执。'
    replacement = {'purpose': 'design', 'instruction': instruction, 'context': context, 'schema': schema}
    owner_key = f'{run_id}:design-author:{engine.cycle(run_id)}:{digest(original)}'
    owner = {'id': owner_key, 'run_id': run_id, 'cycle': engine.cycle(run_id),
             'proposal_hash': store.get('consensus', run_id)['proposal_hash'],
             'status': 'pending', 'assignment': original, 'evidence_issue': {
                 'handoff_id': handoff_id, 'error': error}, 'deliveries': [], 'created_at': now()}
    updated = {**run, 'status': 'blocked', 'error': error, 'updated_at': now(),
               'state': {**run.get('state', {}), 'design_request_restart': owner_key}}
    store.batch(puts=[('stage_contexts', owner_key, owner), ('runs', run_id, updated)],
        expected=[('runs', run_id, run), ('stage_contexts', owner_key, None)])
    return owner_key, replacement
