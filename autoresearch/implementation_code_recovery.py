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
