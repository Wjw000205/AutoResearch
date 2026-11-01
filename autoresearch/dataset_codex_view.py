"""Bounded, read-only projection of the current dataset code-writing assignment."""
from __future__ import annotations


def execution_view(store, run):
    if run.get('stage') not in {'experiments', 'tuning'}:
        return None
    from .run_views import _one
    saved = _one(store, 'dataset_experiment_setups', ['id', 'status', 'codex_job_id',
        'previous_codex_job_id', 'updated_at'], run_id=run['id'],
        where="json_extract(data,'$.cycle')=?",
        args=(run.get('state', {}).get('research_cycle', 1),), order_by='updated_at DESC,key DESC')
    if not saved or saved['status'] == 'ready':
        return None
    job_id = saved.get('codex_job_id') or saved.get('previous_codex_job_id')
    if not job_id:
        return None
    job = _one(store, 'dataset_codex_jobs', ['id', 'status', 'workspace', 'updated_at',
        'checkpoint.thread_id', 'checkpoint.turn_id', 'checkpoint.status', 'checkpoint.last_event_at'],
        key=job_id, run_id=run['id'])
    if not job:
        return None
    preparing = not saved.get('codex_job_id') or job['status'] == 'prepared'
    request_status = None if preparing else job.get('checkpoint.status')
    phase = 'implementing' if preparing or saved['status'] == 'implementing_configuration' else 'acceptance'
    status, action = 'preparing', '正在准备实验配置修复任务，复用已保存文件并修正具体缺项。'
    if not preparing:
        action = '配置实现已返回，正在进行交付核验、独立评议与兼容性检查。'
        if request_status == 'running':
            status, action = 'running', 'Codex 正在编写实验配置与运行接口；完成后继续当前实验阶段的核验。'
        elif request_status in {'unknown', 'blocked', 'interrupted', 'failed'}:
            status, action = 'awaiting_confirmation', '正在核对配置实现的原回执与保存文件，再决定恢复原回合或修复交付。'
    if run.get('status') != 'running':
        status = 'awaiting_confirmation'
        action = '实验配置流程已暂停推进；原回执与文件已保存。' if run.get('status') == 'paused' else '实验配置流程等待处理；原回执与文件已保存。'
    return {'kind': 'local_codex', 'source': 'persisted_dataset_codex', 'workflow_id': saved['id'] if preparing else job['id'],
        'status': status, 'phase': phase, 'action': action, 'workspace': job.get('workspace'),
        'thread_id': None if preparing else job.get('checkpoint.thread_id'),
        'turn_id': None if preparing else job.get('checkpoint.turn_id'), 'request_status': request_status,
        'last_event_at': None if preparing else job.get('checkpoint.last_event_at'),
        'updated_at': saved.get('updated_at') if preparing else job.get('updated_at')}
