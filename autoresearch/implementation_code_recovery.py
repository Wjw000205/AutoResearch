"""Audited return from an erroneous scientific cycle to an existing code delivery.

No source, paid request, review, or graph checkpoint is deleted. Only active
control pointers are restored, after their exact old values have been archived.
"""
import copy
import hashlib
import json
import os
from pathlib import Path

from .implementation_restart import _get, _put, _sha, _rows
from .storage import now

COLLECTION = 'implementation_routing_recoveries'
MARKER = 'implementation_code_recovery'


def _snapshot(engine, db, run_id, workflow_id):
    from .implementation_failures import CODE_FAILURE_KINDS
    run = _get(db, 'runs', run_id, {})
    if run.get('status') not in {'paused', 'blocked', 'failed'}:
        raise ValueError('恢复前必须暂停任务')
    if _get(db, 'runtime', 'worker', {}).get('held') or any(
            not task.done() for task in getattr(engine, 'tasks', {}).values()):
        raise ValueError('执行租约尚未释放')
    workflow = _get(db, 'codex_workflows', workflow_id, {})
    cycle = workflow.get('cycle')
    if workflow.get('run_id') != run_id or not cycle or not workflow.get('attempts'):
        raise ValueError('缺少原代码工作流')
    failure = _get(db, 'research_cycles', f'{run_id}:{cycle}', {})
    if (failure.get('failure_stage') != 'implementation'
            or failure.get('failure', {}).get('kind') not in CODE_FAILURE_KINDS
            or failure['failure'].get('details', {}).get('workflow_id') != workflow_id
            or run.get('state', {}).get('research_cycle', 1) not in {cycle, cycle + 1}):
        raise ValueError('恢复只适用于代码门禁错误跳转的一轮')
    for collection in ('programs', 'experiments'):
        if any(True for _ in _rows(db, collection, run_id)):
            raise ValueError('已有正式交付或实验；不能自动回退其控制记录')
    last = workflow['attempts'][-1]
    if last.get('checkpoint', {}).get('status') != 'completed' or not last.get('snapshot'):
        raise ValueError('原代码回合未确认完成，不能重新派发')
    from .codex_workflow import _verify_snapshot, _validate_approved_spec
    _verify_snapshot(engine, run_id, last['snapshot'])
    specs = [row for row in workflow['specifications'] if row.get('approved')]
    if len(specs) != 1:
        raise ValueError('无法唯一定位批准的实现文档')
    _validate_approved_spec(specs[0], workflow['proposal_hash'])
    common = workflow['common']
    originals = {}
    for stage in ('innovation', 'related_work', 'debate', 'design'):
        row = _get(db, 'cycle_steps', f'{run_id}:{cycle}:{stage}')
        if not row or row.get('status') != 'completed':
            raise ValueError('缺少原周期完成记录：' + stage)
        originals[stage] = row
    consensus = originals['debate']['output']
    if (not consensus.get('unanimous') or _sha(consensus['proposal']) != workflow['proposal_hash']
            or _sha(consensus['proposal']) != _sha(common['proposal'])):
        raise ValueError('原方案或架构与已审代码输入不一致')
    if _sha(originals['design']['output']) != _sha(common['design']):
        revision = _get(db, 'architecture_revisions',
            f'{run_id}:architecture-revisions:{cycle}:{workflow["proposal_hash"]}', {})
        original = {**originals['design'], 'id': f'{run_id}:design'}
        if (revision.get('status') != 'approved' or not revision.get('revisions')
                or revision.get('original_step_sha256') != _sha(original)
                or _sha(revision['revisions'][-1]['design']) != _sha(common['design'])):
            raise ValueError('原架构修订链不再对应已批准的代码输入')
    plan = _get(db, 'experiment_plans', common['experiment_plan']['id'])
    if _sha(plan) != _sha(common['experiment_plan']):
        raise ValueError('冻结计划与原工作流不一致')
    later = [_get(db, 'steps', f'{run_id}:{s}') for s in
             ('debate', 'design', 'implementation', 'experiments', 'tuning', 'validation_review', 'final_tests', 'report')]
    if any(row and row.get('cycle', 1) > cycle for row in later):
        raise ValueError('新一轮已产生审核或代码，需单独处理，不能覆盖')
    literature = None
    archives = [row for _, row in _rows(db, 'innovation_material_archives', run_id)
                if row.get('literature_step', {}).get('status') == 'completed']
    if archives:
        literature = max(archives, key=lambda row: row.get('archived_at', ''))['literature_step']
    controls = [('runs', run_id), ('consensus', run_id), ('experiment_plans', run_id),
                ('graph_status', run_id), ('research_cycles', f'{run_id}:{cycle}')]
    controls += [(c, f'{run_id}:{s}') for c in ('steps', 'stage_contexts') for s in
                 ('literature', 'innovation', 'related_work', 'debate', 'design', 'implementation', 'revision')]
    return {'run_id': run_id, 'cycle': cycle, 'workflow': workflow,
            'original_steps': originals, 'literature': literature,
            'records': [{'collection': c, 'key': k, 'value': _get(db, c, k)} for c, k in controls],
            'budget_sha256': _sha(_get(db, 'budgets', run_id)),
            'specification_sha256': _sha(workflow['specifications']),
            'approved': {key: _sha(common[key]) for key in ('proposal', 'design', 'experiment_plan')}}


def preview(engine, run_id, workflow_id):
    with engine.store._read_db() as db:
        db.execute('BEGIN')
        snapshot = _snapshot(engine, db, run_id, workflow_id)
    return {'run_id': run_id, 'workflow_id': workflow_id, 'cycle': snapshot['cycle'],
            'expect_sha': _sha(snapshot), 'approved': snapshot['approved']}


def restore(engine, run_id, *, workflow_id, expect_sha):
    identity = f'{run_id}:code-routing-recovery:{expect_sha}'
    with engine.store._db() as db:
        db.execute('BEGIN IMMEDIATE')
        engine.store._guard_run(db, run_id)
        previous = _get(db, COLLECTION, identity)
        if previous:
            return previous
        snapshot = _snapshot(engine, db, run_id, workflow_id)
        if _sha(snapshot) != expect_sha:
            raise ValueError('恢复预览后状态发生变化，请重新核对')
        root = engine.run_dir(run_id).resolve()
        path = root / 'code-routing-recovery' / ('cycle-' + str(snapshot['cycle'])) / 'archive.json'
        from .deletion import _plain_path
        _plain_path(path, Path(engine.data_dir).resolve())
        path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(snapshot, ensure_ascii=False, indent=2).encode()
        if path.exists():
            if path.read_bytes() != content:
                raise ValueError('恢复归档已存在且内容不同，不得覆盖')
        else:
            with path.open('xb') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        workflow = snapshot['workflow']
        cycle = snapshot['cycle']
        receipt = {'id': identity, 'run_id': run_id, 'version': 2, 'cycle': cycle,
                   'source': 'explicit_user_request', 'workflow_id': workflow_id,
                   'implementation_generation': workflow.get('implementation_generation'),
                   'graph_thread_id': f'{run_id}:code-recovery:{expect_sha}',
                   'archive_path': str(path), 'archive_sha256': hashlib.sha256(content).hexdigest(),
                   'approved': snapshot['approved'], 'specification_sha256': snapshot['specification_sha256'],
                   'budget_sha256': snapshot['budget_sha256'], 'created_at': now(),
                   'scientific_approval': False, 'execution_authorized': False}
        receipt['sha256'] = _sha(receipt)
        run = _get(db, 'runs', run_id)
        state = {k: v for k, v in run.get('state', {}).items() if k not in {
            'resume_generation', 'implementation_restart', 'implementation_generation',
            'implementation_graph_thread_id', 'research_brief_restart', 'protocol_review_return'}}
        state.update(research_cycle=cycle, **{MARKER: identity})
        run.update(state=state, status='paused', stage='implementation', progress=46, error=None, updated_at=now())
        for row in snapshot['records']:
            if row['collection'] in {'steps', 'stage_contexts'}:
                db.execute('DELETE FROM records WHERE collection=? AND key=?', (row['collection'], row['key']))
        for stage, original in snapshot['original_steps'].items():
            _put(engine.store, db, 'steps', f'{run_id}:{stage}', {**original, 'id': f'{run_id}:{stage}'})
        if snapshot['literature']:
            _put(engine.store, db, 'steps', f'{run_id}:literature', snapshot['literature'])
        _put(engine.store, db, 'consensus', run_id, snapshot['original_steps']['debate']['output'])
        _put(engine.store, db, 'experiment_plans', run_id, workflow['common']['experiment_plan'])
        _put(engine.store, db, 'stage_contexts', f'{run_id}:implementation', {
            'id': f'{run_id}:implementation', 'run_id': run_id, 'context': workflow['common'],
            'restored_from': identity, 'created_at': now()})
        _put(engine.store, db, 'steps', f'{run_id}:implementation', {
            'id': f'{run_id}:implementation', 'run_id': run_id, 'cycle': cycle,
            'stage': 'implementation', 'status': 'blocked', 'output': None,
            'waiting_for': 'implementation_code', 'recovery_id': identity})
        _put(engine.store, db, 'research_cycles', f'{run_id}:{cycle}', {
            'id': f'{run_id}:{cycle}', 'run_id': run_id, 'cycle': cycle, 'status': 'implementing',
            'review_type': 'implementation_code', 'proposal_hash': workflow['proposal_hash'],
            'routing_correction': identity, 'original_record_archive': str(path)})
        _put(engine.store, db, 'graph_status', run_id, {
            'id': run_id, 'run_id': run_id, 'cycle': cycle, 'node': 'implementation',
            'status': 'waiting', 'waiting_for': 'implementation_code', 'recovery_id': identity, 'updated_at': now()})
        # The completed same-code native turn is kept verbatim in the archive.
        # Resume requests a focused source repair, not another scientific proposal.
        last = workflow['attempts'][-1]
        last.update(status='rejected', issues=[
            '宿主错误路由已修复，恢复已批准轮次的代码实现；禁止改写方案、重做文献或修改已批准SPEC。',
            '比较传输内存核算已修复为分阶段且按真实输入字节预留；读取宿主bootstrap的comparison_io，按本任务传输合同核对代码中的内存预留，保持原总RAM/VRAM上限。',
            '按原审核问题修复对应模块的实际执行效率与资源探针，保留批准的数据类型、数据范围、方法、执行路径、拟合范围和时间预算。不能只改估算常数、放宽门禁或声明通过。',
            '提交结构化源码和真实测试结果；资源失败须给真实根因。完成后由宿主验收和三个角色独立审核。'])
        workflow.update(status='implementing', reason=None, blocked_kind=None, routing_recovery=identity)
        _put(engine.store, db, 'codex_workflows', workflow_id, workflow)
        _put(engine.store, db, COLLECTION, identity, receipt)
        _put(engine.store, db, 'runs', run_id, run)
        # Validate original proposal votes, the entire architecture revision
        # chain and frozen plan against this same transaction before committing.
        from .implementation_restart import _approved
        checked = _approved(engine, run_id, db)
        if checked != {'proposal_hash': snapshot['approved']['proposal'],
                       'design_hash': snapshot['approved']['design'],
                       'experiment_plan_hash': snapshot['approved']['experiment_plan']}:
            raise ValueError('恢复后的真实执行门禁与原批准输入不一致')
        db.execute('INSERT INTO events(run_id,kind,message,data,created_at) VALUES(?,?,?,?,?)',
                   (run_id, 'implementation.code_routing_restored',
                    '已恢复原科研周期代码实现；方案、实现文档和三方审核保留；误跳转记录已归档。',
                    json.dumps({'recovery_id': identity, 'cycle': cycle}), now()))
        return receipt


def active(engine, run_id, common=None):
    # Recovery authenticates many readings of the same paid handoff. Use one
    # immutable DB view so existing compact manifest/range caches apply, then
    # discard it. Ordinary stores re-decode every manifest for every clause.
    if hasattr(engine.store,'read_snapshot') and not getattr(engine.store,'is_read_snapshot',False):
        import copy
        from .architecture_validation import operation
        with engine.store.read_snapshot() as store:
            reader=copy.copy(engine)
            reader.store=store
            if getattr(getattr(engine,'sources',None),'store',None) is engine.store:
                reader.sources=copy.copy(engine.sources)
                reader.sources.store=store
            return operation(_active)(reader,run_id,common)
    return _active(engine,run_id,common)


def _active(engine, run_id, common=None):
    run = engine.store.get('runs', run_id, {})
    identity = run.get('state', {}).get(MARKER)
    if not identity:
        return None
    receipt = engine.store.get(COLLECTION, identity, {})
    if (receipt.get('run_id') != run_id or receipt.get('version') != 2
            or receipt.get('sha256') != _sha({k: v for k, v in receipt.items() if k != 'sha256'})
            or receipt.get('cycle') != run.get('state', {}).get('research_cycle', 1)):
        raise ValueError('代码恢复回执与当前周期不一致')
    path = Path(receipt['archive_path'])
    if not path.resolve().is_relative_to(engine.run_dir(run_id).resolve()):
        raise ValueError('代码恢复归档越界')
    archived = path.read_bytes()
    if hashlib.sha256(archived).hexdigest() != receipt['archive_sha256']:
        raise ValueError('代码恢复归档校验失败')
    workflow = engine.store.get('codex_workflows', receipt['workflow_id'], {})
    if workflow.get('implementation_generation') != receipt['implementation_generation']:
        raise ValueError('原代码身份或批准文档变化')
    if _sha(workflow.get('specifications')) != receipt['specification_sha256']:
        original = json.loads(archived)['workflow']['specifications']
        if (_sha(original) != receipt['specification_sha256']
                or workflow.get('specifications', [])[:len(original)] != original):
            raise ValueError('原代码身份或批准文档变化')
        from .implementation_definition_repair import validate_recovery_extension
        validate_recovery_extension(engine, run_id, workflow, len(original))
    if common and any(_sha(common.get(k)) != v for k, v in receipt['approved'].items()):
        raise ValueError('代码恢复不能更换原方案、架构或冻结计划')
    return receipt
