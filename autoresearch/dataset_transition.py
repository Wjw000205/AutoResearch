"""Explicit dataset changes create a separately recorded experiment round.

The previous cycle is immutable. A transition selects data and reuses the
existing method; it is neither a scientific vote nor an execution approval.
"""
from __future__ import annotations

import copy
import gzip
import json

from .experiment_workflow import sha
from .storage import now

COLLECTION = 'dataset_transitions'
ROUTES = 'dataset_transition_routes'
GUIDANCE = ('本轮按用户要求将既有方法迁移至指定真实数据集。复用已保存的方法、基线和实现，'
    '在实验阶段通过配置文件和命令行参数选择数据、划分、预处理及对应评估。'
    '复用模型源码与原架构，只补必要的数据接口；先冻结来源版本和公平比较协议，再做增量核验及兼容性检查。'
    '历史合成测量只说明旧试点，不证明真实数据效果，也不与新周期指标混合。'
    '使用已下载文件及其精确 dataset_assets 绑定，禁止生成合成数据替代。'
    '先做一个小规模成对验证批次，每批完成后重新判断；保留留出集用于后续正式验收。'
    '整体主指标改善即可，辅助指标作为取舍分析；执行、公平性、数据泄漏和资源检查仍须通过。'
    '旧数据专属诊断只在新数据上有明确可计算定义时保留。'
    '完整历史和代码按证据引用读取，不复制运行输出或全部历史上下文。')


def _get_field(store, collection, key, field):
    with store._read_db() as db:
        row = db.execute("SELECT json_extract(data,?) FROM records WHERE collection=? AND key=?",
                         ('$.' + field, collection, key)).fetchone()
    return json.loads(row[0]) if row and row[0] is not None else None


def current(store, run_id):
    run = store.get('runs', run_id) or {}
    key = run.get('state', {}).get('dataset_transition')
    if not key:
        return None
    record = store.get(COLLECTION, key) or {}
    body = record.get('request', {})
    from .dataset_configuration_queue import owns_transition
    queued = owns_transition(store, run_id, record)
    if body.get('next_cycle', 0) < run.get('state', {}).get('research_cycle', 1) and not queued:
        return None  # A later normal research revision owns its own graph.
    if (record.get('id') != key or record.get('graph_thread_id') != key
            or record.get('run_id') != run_id or record.get('sha256') != sha(body)
            or body.get('run_id') != run_id or record.get('cycle') != body.get('next_cycle')
            or (body.get('next_cycle') != run.get('state', {}).get('research_cycle', 1) and not queued)
            or record.get('scientific_approval') is not False
            or record.get('execution_authorized') is not False):
        raise ValueError('数据集切换的周期、配置或来源绑定发生变化。')
    return record


def project(context):
    """No prior role history, scientific-state tree or automatic memory bundle."""
    from .training_context import FIELDS
    required = FIELDS | {'dataset_transition_context_protocol', 'dataset_transition', 'dataset_adaptation', 'hardware',
        'resource_assessment', 'evidence_assessment', 'proposal_schema',
        'previous_discussion', 'discussion_work', 'previous_validation_evidence',
        'original_review', 'review_hash', 'amendments', 'previous_amendments',
        'resource_policy', 'publication_target', 'references', 'per_trial_seconds',
        'design', 'experiment_plan', 'comparison_task_policy', 'task_primary_threshold',
        'idea_literature_index', 'related_work_index'}
    return {k: copy.deepcopy(v) for k, v in context.items()
            if k in required or k.endswith(('_protocol', '_version')) or k.startswith('_')}


def _archived(record, collection):
    from .record_archive import verify
    rows = [row for row in record['archives'] if row['collection'] == collection]
    if collection == 'experiment_plans':
        rows.sort(key=lambda row: row['key'] == record['run_id'])
    if not rows:
        raise ValueError('缺少可复用的方法来源：' + collection)
    source = rows[0]
    if not any(all(source.get(key) == original.get(key) for key in
                   ('collection', 'key', 'sha256', 'bytes', 'updated_at'))
               for original in record['request']['sources']):
        raise ValueError('方法归档与原始切换快照不一致。')
    if source['bytes'] > 16 * 1024 * 1024:
        raise ValueError('方法来源超出读取上限。')
    verify(source['path'], source['sha256'], source['bytes'])
    with gzip.open(source['path'], 'rt', encoding='utf-8') as stream:
        return json.load(stream)


def route(store, run_id):
    record = current(store, run_id)
    if not record:
        return None
    key = (store.get('runs', run_id) or {}).get('state', {}).get('dataset_adaptation')
    value = store.get(ROUTES, key) if key else None
    if not value:
        return None
    body = value.get('body', {})
    if (value.get('id') != key or value.get('run_id') != run_id or value.get('graph_thread_id') != key
            or value.get('sha256') != sha(body) or body.get('transition_id') != record['id']
            or body.get('transition_sha256') != record['sha256']
            or body.get('source_cycle') != record['request']['source_cycle']
            or body.get('cycle') != record['cycle'] or body.get('entry') not in {'design', 'experiments'}
            or not body.get('instruction') or value.get('execution_authorized') is not False):
        raise ValueError('数据适配路由缺少原方法和用户指令绑定。')
    consensus = store.get('consensus', run_id)
    if sha(consensus) != body.get('source_consensus_sha256'):
        raise ValueError('复用的方法共识已改变。')
    return value


def review_scope(store, run_id):
    """Small authority packet shared by design, protocol and code reviewers."""
    receipt = route(store, run_id)
    if not receipt:
        return {}
    record = current(store, run_id)
    return {'dataset_adaptation': {
        'id': receipt['id'], 'sha256': receipt['sha256'],
        'source_cycle': receipt['body']['source_cycle'],
        'source_consensus_sha256': receipt['body']['source_consensus_sha256'],
        'datasets': [target['dataset'] for target in record['request']['targets']],
        'operator_instruction': receipt['body']['instruction'],
        'scope': 'experiment_configuration' if receipt['body']['entry'] == 'experiments' else 'existing_method_new_data_protocol',
        'guidance': GUIDANCE + ' 原方案的数据集和试点参数由本轮指定数据及其来源协议替换；'
            '原方法共识继续有效。评议本次数据适配的增量及受影响部分，复用未变更的模块定义与代码。'}}


def adapt(engine, run_id, *, transition_id, instruction):
    """Route a dataset change into configuration and checks in experiments."""
    from . import record_archive
    record = current(engine.store, run_id)
    run = engine.store.get('runs', run_id)
    if (not record or record['id'] != transition_id or run.get('status') not in {'paused', 'blocked', 'failed'}
            or not isinstance(instruction, str) or not 1 <= len(instruction.strip()) <= 4000
            or run_id in getattr(engine, 'tasks', {}) and not engine.tasks[run_id].done()):
        raise ValueError('需要已停止的数据切换与明确的适配指令。')
    prior = route(engine.store, run_id)
    if prior and prior['body']['entry'] == 'experiments':
        return prior
    if engine.store.active_reasoning(run_id, {'running', 'queued', 'starting',
            'turn_start_dispatched', 'thread_start_dispatched', 'thread_resume_dispatched'}):
        raise ValueError('仍有活动请求，不能更换路由。')
    with engine.store._read_db() as db:
        if db.execute("SELECT 1 FROM records WHERE collection='experiments' AND json_extract(data,'$.run_id')=? "
            "AND coalesce(json_extract(data,'$.research_cycle'),1)=? LIMIT 1", (run_id, record['cycle'])).fetchone():
            raise ValueError('新周期已有测量，不能回退适配入口。')
    design_step = engine.store.get('steps', run_id + ':design')
    if design_step and design_step.get('status') == 'completed' and design_step.get('output'):
        raise ValueError('已有新设计交付；须先明确复用的版本，不能自动覆盖。')
    consensus = _archived(record, 'consensus')
    if (consensus.get('cycle', 1) > record['request']['source_cycle']
            or consensus.get('unanimous') is not True):
        raise ValueError('原方法未取得共识，不能复用。')
    retained = []
    obsolete = [('consensus', run_id)] + [(collection, run_id + ':' + stage)
        for collection in ('steps', 'stage_contexts') for stage in ('debate', 'design')]
    for collection, key in obsolete:
        fingerprint = record_archive.fingerprint(engine.store, collection, key)
        if fingerprint:
            retained.append(record_archive.save(engine.store, run_id, fingerprint))
    body = {'transition_id': record['id'], 'transition_sha256': record['sha256'],
        'cycle': record['cycle'], 'entry': 'experiments', 'protocol': 2, 'instruction': instruction,
        'source_consensus_sha256': sha(consensus), 'source_cycle': record['request']['source_cycle'],
        'method_cycle': consensus.get('cycle', 1),
        'previous_route_records': retained}
    identity = f'{run_id}:dataset-adaptation:{sha(body)}'
    receipt = {'id': identity, 'run_id': run_id, 'body': body, 'sha256': sha(body),
        'graph_thread_id': identity, 'created_at': now(), 'execution_authorized': False}
    # The old vote retains its original cycle and payload. It approves the
    # existing method only; fresh data/protocol/code approvals remain mandatory.
    engine.store.batch([
        (ROUTES, identity, receipt), ('consensus', run_id, consensus),
        ('programs', run_id, _archived(record, 'programs')),
        ('runs', run_id, {**run, 'stage': 'experiments', 'status': 'paused', 'error': None,
            'state': {**run['state'], 'dataset_adaptation': identity, 'dataset_configuration_protocol': 1}, 'updated_at': now()}),
        ('graph_status', run_id, {'id': run_id, 'run_id': run_id, 'cycle': record['cycle'],
            'node': 'experiments', 'status': 'waiting', 'waiting_for': 'dataset_configuration', 'updated_at': now()}),
    ], [(collection, key) for collection, key in obsolete if collection != 'consensus'])
    engine.store.event(run_id, 'research.dataset_adaptation',
        '复用原方法与模型实现，在实验阶段配置数据集并检查数据接口；尚无新数据测量。',
        {'adaptation_id': identity, 'source_cycle': body['source_cycle'], 'cycle': record['cycle']})
    return receipt


def snapshot(engine, run_id, targets):
    from . import record_archive
    from .orchestration import STAGES
    from .dataset_targets import bind
    run = engine.store.get('runs', run_id)
    if (not run or run.get('mode') != 'live' or run.get('status') not in {'paused', 'blocked', 'failed', 'completed'}
            or run_id in getattr(engine, 'tasks', {}) and not engine.tasks[run_id].done()):
        raise ValueError('数据集切换须在真实任务停止后准备。')
    if engine.store.active_reasoning(run_id, {'running', 'queued', 'starting',
            'turn_start_dispatched', 'thread_start_dispatched', 'thread_resume_dispatched'}):
        raise ValueError('仍有活动 agent 请求，不能切换数据集。')
    cycle = engine.cycle(run_id)
    # Dataset rounds use separate measurement identities; they do not consume
    # the scientific architecture-revision limit. Each still has a trial cap.
    if not isinstance(targets, list) or not 1 <= len(targets) <= 8:
        raise ValueError('需要 1 到 8 个来源明确的数据集。')
    notes = {row['id']: row for row in engine.datasets._notes(run_id)}
    bindings, seen = [], set()
    for target in targets:
        binding = bind(engine, run_id, target, notes)
        if target['dataset'].casefold() in seen:
            raise ValueError('数据集必须来自本任务已校验的论文笔记且不能重复。')
        seen.add(target['dataset'].casefold())
        bindings.append(binding)
    proposal = _get_field(engine.store, 'consensus', run_id, 'proposal')
    if not proposal or not engine.store.get('programs', run_id):
        raise ValueError('需要可复用的已保存方法和实现。')
    with engine.store._read_db() as db:
        voted = db.execute("SELECT json_extract(data,'$.unanimous') FROM records "
                           "WHERE collection='consensus' AND key=?", (run_id,)).fetchone()
    if not voted or voted[0] != 1:
        raise ValueError('原方法尚未取得共识，不能直接进入数据适配。')
    keys = [(name, run_id) for name in ('consensus', 'programs', 'selections', 'tuning_plans',
                                       'experiment_plans', 'experiment_restarts')]
    keys += [(name, run_id + ':' + stage) for stage in STAGES[STAGES.index('debate'):]
             for name in ('steps', 'stage_contexts')]
    # A revised design can have a hash-specific active experiment plan.
    from .architecture_revisions import plan_key
    active_plan = plan_key(engine, run_id)
    if active_plan != run_id:
        keys.append(('experiment_plans', active_plan))
    sources = [value for coll, key in keys if (value := record_archive.fingerprint(engine.store, coll, key))]
    from .performance_policy import current as performance_policy
    policy = performance_policy(engine.store, run_id, check_host=False)
    config = copy.deepcopy(run['config'])
    if policy:
        config['min_relative_improvement'] = policy['policy']['criterion']['minimum_relative_improvement']
    return {'run_id': run_id, 'source_cycle': cycle, 'next_cycle': cycle + 1,
        'source_run_sha256': sha(run), 'targets': targets, 'dataset_bindings': bindings,
        'sources': sources, 'previous_proposal': proposal,
        'target_config': config,
        'performance_policy': copy.deepcopy(policy['policy']['criterion']) if policy else None}


def preview(engine, run_id, targets):
    value = snapshot(engine, run_id, targets)
    return {'snapshot_id': sha(value), 'source_cycle': value['source_cycle'],
        'next_cycle': value['next_cycle'], 'targets': targets,
        'scientific_approval': False, 'execution_authorized': False}


def prepare(engine, run_id, *, snapshot_id, targets, instruction):
    """Called under the lifecycle lock; archive before changing active pointers."""
    from . import record_archive
    if not isinstance(instruction, str) or not 1 <= len(instruction.strip()) <= 4000:
        raise ValueError('需要明确的数据集切换指令。')
    identity = f'{run_id}:dataset-transition:{snapshot_id}'
    existing = engine.store.get(COLLECTION, identity)
    if existing:
        if existing['request']['targets'] != targets or existing['request']['operator_instruction'] != instruction:
            raise ValueError('同一切换快照不能对应不同请求。')
        if not route(engine.store, run_id):
            adapt(engine, run_id, transition_id=identity, instruction=instruction)
        return current(engine.store, run_id)
    value = snapshot(engine, run_id, targets)
    if sha(value) != snapshot_id:
        raise ValueError('任务或数据来源已变化，请重新读取切换预览。')
    archives = [record_archive.save(engine.store, run_id, row) for row in value['sources']]
    if snapshot(engine, run_id, targets) != value:
        raise ValueError('归档期间任务状态发生变化，保留原入口。')
    request = {**value, 'operator_instruction': instruction}
    record = {'id': identity, 'run_id': run_id, 'cycle': value['next_cycle'],
        'request': request, 'sha256': sha(request), 'archives': archives, 'created_at': now(),
        'graph_thread_id': identity, 'scientific_approval': False, 'execution_authorized': False}
    # Only active pointers move. Measurements, paid requests, votes and code files
    # remain at their original identities, with no relabeling or supersede writes.
    with engine.store._db() as db:
        db.execute('BEGIN IMMEDIATE')
        engine.store._guard_run(db, run_id)
        row = db.execute("SELECT data FROM records WHERE collection='runs' AND key=?", (run_id,)).fetchone()
        run = json.loads(row[0])
        if sha(run) != value['source_run_sha256']:
            raise ValueError('提交前任务状态发生变化。')
        for source in value['sources']:
            row = db.execute('SELECT updated_at FROM records WHERE collection=? AND key=?',
                             (source['collection'], source['key'])).fetchone()
            if not row or row[0] != source['updated_at']:
                raise ValueError('提交前活动来源发生变化。')
        from .implementation_restart import _put
        _put(engine.store, db, COLLECTION, identity, record)
        for source in value['sources']:
            db.execute('DELETE FROM records WHERE collection=? AND key=?', (source['collection'], source['key']))
        run.update(status='paused', stage='experiments', error=None, config=value['target_config'],
            state={'research_cycle': value['next_cycle'], 'dataset_transition': identity}, updated_at=now())
        _put(engine.store, db, 'runs', run_id, run)
        _put(engine.store, db, 'graph_status', run_id, {'id': run_id, 'run_id': run_id,
            'cycle': value['next_cycle'], 'node': 'experiments', 'status': 'waiting',
            'waiting_for': 'dataset_transition', 'updated_at': now()})
    engine.store.event(run_id, 'research.dataset_transition',
        '真实数据轮已建立；复用既有方法，重新复核数据协议与代码适配，尚未批准实验。',
        {'transition_id': identity, 'datasets': [t['dataset'] for t in targets], 'cycle': value['next_cycle']})
    adapt(engine, run_id, transition_id=identity, instruction=instruction)
    return record


async def context(engine, run_id, stage, *, targets=None):
    record = current(engine.store, run_id)
    if not record:
        return {}
    request = record['request']
    from .dataset_targets import bind, acquire
    notes = {row['id']: row for row in engine.datasets._notes(run_id)}
    # Download uses the ordinary provenance/checksum service and the active-run
    # cancellation gate; this is an explicit operator-requested preparation.
    datasets = []
    if targets is not None and (not targets or any(t not in request['targets'] for t in targets)):
        raise ValueError('当前配置的数据集不属于已保存的切换请求。')
    for target, binding in zip(request['targets'], request['dataset_bindings']):
        if targets is not None and target not in targets:
            continue
        if bind(engine, run_id, target, notes) != binding:
            raise ValueError('切换请求的数据来源或目录版本发生变化。')
        asset = await acquire(engine, run_id, target)
        manifest = asset['manifest']
        datasets.append({'target': target,
            'dataset_assets': [{'asset_id': asset['id'], 'manifest_sha256': asset['manifest_sha256']}],
            'files': manifest['files'], 'origin': manifest['origin']})
    packet = {'id': record['id'], 'sha256': record['sha256'],
        'source_cycle': request['source_cycle'], 'datasets': datasets,
        'operator_instruction': request['operator_instruction'],
        'performance_criterion': request['performance_policy'], 'guidance': GUIDANCE,
        'previous_records': [{k: row[k] for k in ('collection', 'key', 'sha256', 'path')}
                             for row in record['archives'] if row['collection'] in {'programs', 'steps'}]}
    if route(engine.store, run_id):
        plan = _archived(record, 'experiment_plans')
        review = engine.store.get('architecture_reviews',
            f"{run_id}:architecture:{request['source_cycle']}:{plan.get('design_hash')}") or {}
        if review.get('document'):
            packet['approved_method_document'] = review['document']
        packet['scope'] = 'data_protocol_and_implementation_adaptation'
        packet['method_proposal_hash'] = sha(request['previous_proposal'])
        program = _archived(record, 'programs')
        packet['existing_implementation'] = {key: program[key] for key in
            ('code_sha256', 'artifact_id', 'spec_hash', 'delivery_hash', 'delivery_id',
             'codex_workspace', 'implementation_document_id', 'implementation_document_sha256') if key in program}
    return {'dataset_transition': packet, 'dataset_transition_context_protocol': 1,
            **review_scope(engine.store, run_id)}


def validate_design(engine, run_id, design, packet):
    if not packet:
        return
    expected = {row['target']['dataset']: row['dataset_assets'] for row in packet['datasets']}
    tasks = design.get('benchmark_tasks') or [design]
    if {task.get('dataset') for task in tasks} != set(expected):
        raise ValueError('数据集切换设计必须覆盖全部指定数据集。')
    for task in tasks:
        for role in ('baseline', 'proposed', 'ablation'):
            if task.get(role + '_params', {}).get('dataset_assets') != expected[task['dataset']]:
                raise ValueError('所有比较分支必须冻结同一份真实数据资产。')
