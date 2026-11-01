"""Continue an approved isolated code work item after a proven terminal turn.

Reconcile an uncertain turn using its original operation. Only a confirmed
timeout interruption (or empty transient failure) permits an automatic new
turn, in the same native thread and directory, with the original work retained.
"""
import asyncio
import copy
import hashlib
from datetime import datetime
from pathlib import Path

from .experiment_workflow import sha
from .storage import now, uid

READBACK_ERRORS = {'codex_operation_timeout', 'codex_timeout_readback_timeout',
    'codex_read_timeout', 'codex_transport_eof', 'codex_original_turn_still_in_progress'}
CONTINUE = ('原回合已确认终止。本次续接同一已批准工作项，不从头重写。'
    '先核对当前目录已有源码、交付清单和检查脚本，保留已完成的修改，只完成尚未结束的核对和交付。'
    '若文件已经齐备，核对后给出最终完成答复，不重复实现。'
    '保持原修改单、文件范围、工作目录和全部验证约束；不运行正式训练。'
    '最终仍须宿主收集磁盘产物、三方独立代码审查和 CPU 验证。')


def current(job):
    return job['continuations'][-1] if job.get('continuations') else job


def _bound(receipt, run_id, work, workspace, project_root, model, effort):
    identity = {'run_id': run_id, 'operation_id': work['operation_id'],
        'workspace': str(workspace.resolve()), 'project_root': str(project_root.resolve()),
        'instruction_sha256': hashlib.sha256(work['instruction'].encode()).hexdigest(),
        'requested_model': model, 'requested_effort': effort}
    return (receipt.get('version') == 1 and not receipt.get('reasoning_only')
        and all(receipt.get(k) == v for k, v in identity.items())
        and bool(receipt.get('thread_id')) and bool(receipt.get('turn_id'))
        and bool(receipt.get('turn_dispatched_at'))
        and not receipt.get('native_tool_violation') and not receipt.get('cleanup_error_code')
        and not receipt.get('non_retryable_server_error'))


def _automatic(receipt):
    if receipt.get('recovered_read_only') is not True or receipt.get('interrupt_requested'):
        return None
    failure = receipt.get('first_failure') or {}
    if (receipt.get('status') == 'interrupted' and receipt.get('error_code') is None
            and failure.get('error_code') == 'codex_operation_timeout'
            and failure.get('thread_id') == receipt.get('thread_id')
            and failure.get('turn_id') == receipt.get('turn_id')):
        return 'confirmed_operation_timeout'
    from .native_failure import retryable_failure
    if retryable_failure(receipt):
        return 'confirmed_transient_failure'
    return None


def _explicit_resume(engine, run_id, job, work, receipt):
    if receipt.get('status') != 'interrupted' or receipt.get('recovered_read_only') is not True:
        return None
    run = engine.store.get('runs', run_id)
    generation = engine.store.get('operator_resumes', run.get('state', {}).get('resume_generation', ''), {})
    if (generation.get('run_id') != run_id or generation.get('kind') != 'explicit_start_command'
            or not generation.get('id') or generation['id'] in job.get('consumed_resume_generations', [])):
        return None
    try:
        if datetime.fromisoformat(generation['created_at']) <= datetime.fromisoformat(work['confirmed_terminal_at']):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return generation['id']


async def _wait(engine, run_id, seconds):
    for _ in range(seconds):
        engine.check_active(run_id)
        await asyncio.sleep(1)
    engine.check_active(run_id)


async def run(engine, harness, run_id, job, save, verify, *, label):
    from .engine import ResearchBlocked
    from .local_codex import LocalCodexClient
    workspace, root = Path(job['workspace']), engine.run_dir(run_id).resolve()
    config = engine.store.get('runs', run_id)['config']
    model, effort = config.get('codex_model') or None, config.get('codex_reasoning_effort') or None
    blocked = tuple(p['api_key_env'] for p in getattr(harness.intelligence, 'settings', {}).get('providers', [])
                    if p.get('api_key_env'))
    client = getattr(engine, 'codex_client', None) or LocalCodexClient(
        timeout=config.get('codex_timeout_seconds', 1800), blocked_env_names=blocked)
    while True:
        engine.check_active(run_id)
        verify()
        previous, identifiers = job, {job['operation_id']}
        for successor in job.get('continuations', []):
            source = successor.get('source_checkpoint') or {}
            if (successor.get('source_checkpoint_sha256') != sha(source)
                    or source != previous.get('checkpoint')
                    or not _bound(source, run_id, previous, workspace, root, model, effort)
                    or source.get('recovered_read_only') is not True
                    or source.get('status') not in {'interrupted', 'failed'}
                    or successor.get('source_operation_id') != previous['operation_id']
                    or successor.get('thread_id') != source.get('thread_id')
                    or successor.get('instruction') != job['instruction'] + '\n\n' + CONTINUE
                    or not successor.get('operation_id') or successor['operation_id'] in identifiers):
                raise ResearchBlocked(f'{label}续接记录的原回合、指令或检查点绑定发生变化。')
            identifiers.add(successor['operation_id'])
            previous = successor
        work = current(job)
        checkpoint = work.get('checkpoint')
        if checkpoint and checkpoint.get('status') == 'completed':
            return copy.deepcopy(checkpoint)
        if checkpoint and 'recovery_source' not in work:
            work['recovery_source'] = copy.deepcopy(checkpoint)
            if checkpoint.get('recovered_read_only') and checkpoint.get('status') in {'failed', 'interrupted'}:
                work.setdefault('confirmed_terminal_at', checkpoint.get('updated_at') or now())
            save()
        await engine._read_consensus_check(run_id, engine._execution_gate, run_id)
        engine.check_active(run_id)
        verify()

        def progress(receipt):
            work['checkpoint'] = copy.deepcopy(receipt)
            work['updated_at'] = now()
            if receipt.get('recovered_read_only') and receipt.get('status') in {'failed', 'interrupted'}:
                work.setdefault('confirmed_terminal_at', receipt.get('updated_at') or now())
            job['active_operation_id'] = work['operation_id']
            job['active_status'] = receipt.get('status')
            save()
            from .codex_accounting import capture_usage
            usage = capture_usage(engine.store, run_id, receipt)
            if usage:
                engine.store.put('codex_usage', usage['id'], usage)

        receipt = await client.run(run_id, workspace, work['instruction'], operation_id=work['operation_id'],
            checkpoint=checkpoint, thread_id=work.get('thread_id'), project_root=root,
            model=model, effort=effort, on_progress=progress,
            check_active=lambda: engine.check_active(run_id), reasoning_only=False)
        progress(receipt)
        if receipt.get('status') == 'completed':
            return receipt
        engine.check_active(run_id)
        bound = _bound(receipt, run_id, work, workspace, root, model, effort)
        reason = _automatic(receipt) if bound else None
        generation = _explicit_resume(engine, run_id, job, work, receipt) if bound and not reason else None
        if reason or generation:
            maximum = engine.store.get('runs', run_id)['config'].get('codex_max_revisions')
            if maximum is not None and len(job.get('continuations', [])) + 1 >= maximum:
                raise ResearchBlocked(f'{label}已达到配置的代码交付次数上限（{maximum}）；原回执和文件已保留。')
            verify()
            successor = {'operation_id': uid(), 'thread_id': receipt['thread_id'],
                'instruction': job['instruction'] + '\n\n' + CONTINUE,
                'source_operation_id': work['operation_id'], 'source_checkpoint': copy.deepcopy(receipt),
                'source_checkpoint_sha256': sha(receipt), 'reason': reason or 'explicit_resume',
                'created_at': now()}
            if generation:
                successor['resume_generation'] = generation
                job.setdefault('consumed_resume_generations', []).append(generation)
            job.setdefault('continuations', []).append(successor)
            job['active_status'] = 'continuation_pending'
            job['active_operation_id'] = successor['operation_id']
            save()  # Persist the successor identity before any model dispatch.
            engine.store.event(run_id, 'codex.scoped_continuation_requested',
                f'{label}原回合已确认终止，自动在原会话续接未完成交付。',
                {'operation_id': successor['operation_id'], 'source_operation_id': work['operation_id'],
                 'thread_id': receipt['thread_id'], 'reason': successor['reason']})
            continue
        if (bound and receipt.get('status') == 'unknown' and receipt.get('error_code') in READBACK_ERRORS):
            count = work.get('readback_retries', 0) + 1
            delay = (2, 5, 15, 30)[min(count - 1, 3)]
            work['readback_retries'] = count
            job['active_status'] = 'reading_original_turn'
            save()
            await _wait(engine, run_id, delay)
            continue  # Same operation/checkpoint: LocalCodexClient only reads the original turn.
        status, error = receipt.get('status', 'unknown'), receipt.get('error_code') or 'none'
        raise ResearchBlocked(f'{label}未完成（status={status}, error={error}）；'
            '原工作项、源码和回执已保留。当前回执不满足自动续接条件，未重复执行原回合。')
