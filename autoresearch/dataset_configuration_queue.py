"""Incremental dataset configurations with immutable, separately bound rounds.

Only one configuration is requested at a time. Completed rounds keep their
original plan/code/measurement identities; later configurations cannot rewrite
them. Each dataset owns its attempt budget, including checks and retries.
"""
from __future__ import annotations

import copy
import json

from .experiment_workflow import sha
from .storage import now

COLLECTION = 'dataset_configuration_queues'
# Historical shared queues retain their original reserve until explicitly migrated.
LEGACY_RESERVE = 6


def _validate_policy(queue):
    policy = queue.get('budget_policy')
    if policy and (sha(policy.get('body')) != policy.get('sha256')
            or policy['body'].get('queue_id') != queue['id']
            or policy['body'].get('scope') != 'per_dataset_task'
            or policy['body'].get('config_sha256') != queue['body']['config_sha256']):
        raise ValueError('数据集预算修订的绑定发生变化。')


def task_budgeted(queue):
    return bool(queue and (queue.get('budget_policy', {}).get('body', {}).get('scope')
        or queue['body'].get('budget_scope')) == 'per_dataset_task')


def current(store, run_id):
    run = store.get('runs', run_id) or {}
    key = run.get('state', {}).get('dataset_configuration_queue')
    if not key:
        return None
    record = store.get(COLLECTION, key) or {}
    body = record.get('body', {})
    batches = record.get('batches', [])
    if not body:
        raise ValueError('逐项实验队列记录缺失。')
    _validate_policy(record)
    if (record.get('id') != key or record.get('run_id') != run_id
            or sha(body) != record.get('sha256') or body.get('config_sha256') != sha(run['config'])
            or not batches or len(batches) > len(body.get('targets', []))
            or [b['dataset'] for b in batches] != [t['dataset'] for t in body['targets'][:len(batches)]]
            or any(b['cycle'] != body['start_cycle'] + i or b['target'] != body['targets'][i]
                or b['setup_id'] != key + ':batch-' + str(i + 1) + (
                    ':source-' + b['source_generation'] if b.get('source_generation') else '')
                or b['graph_thread_id'] != b['setup_id']
                or (not task_budgeted(record) and not 0 <= b['attempt_limit'] <= body['attempt_limit'])
                for i, b in enumerate(batches))):
        raise ValueError('逐项实验队列的任务、配置或数据集绑定发生变化。')
    if task_budgeted(record):
        from .dataset_budgets import total, check_supplements
        for item in batches:
            if item.get('budget_status') == 'approved' and item.get('budget_proposal') is not None:
                if (item.get('attempt_limit') != total(item['budget_proposal']) + check_supplements(store, run_id, item)
                        or item.get('budget_binding', {}).get('budget_plan_sha256') != sha(item['budget_proposal'])):
                    raise ValueError('数据集预算与已核验方案不一致。')
    return record


def owns_transition(store, run_id, transition):
    queue = current(store, run_id)
    if not queue:
        return False
    run = store.get('runs', run_id)
    return (queue['body']['transition_id'] == transition.get('id')
            and queue['body']['transition_sha256'] == transition.get('sha256')
            and queue['batches'][-1]['cycle'] == run['state']['research_cycle'])


def batch(store, run_id):
    queue = current(store, run_id)
    if not queue:
        return None
    value = queue['batches'][-1]
    return value if value['cycle'] == store.get('runs', run_id)['state']['research_cycle'] else None


def attempts(store, run_id, queue):
    # Load accounting operands only, never metrics or held-out outputs.
    with store._read_db() as db:
        rows = db.execute("SELECT coalesce(json_extract(data,'$.research_cycle'),1), "
            "sum(coalesce(json_extract(data,'$.attempt'),1)) FROM records "
            "WHERE collection='experiments' AND key>=? AND key<? "
            "AND coalesce(json_extract(data,'$.research_cycle'),1)>=? GROUP BY 1",
            (run_id + ':', run_id + ';', queue['body']['start_cycle'])).fetchall()
    return dict(rows)


def trial_limit(store, run_id, default):
    queue = current(store, run_id)
    from .experiment_attempt_policy import unlimited
    if unlimited(store, run_id):
        return None
    if not queue:
        return default
    if task_budgeted(queue):
        active = batch(store, run_id)
        if not active or active.get('budget_status') != 'approved' or type(active.get('attempt_limit')) is not int:
            from .engine import ResearchBlocked
            raise ResearchBlocked('当前数据集预算尚未依据任务配置完成独立核验。')
        return active['attempt_limit']
    used = attempts(store, run_id, queue)
    cycle = store.get('runs', run_id)['state']['research_cycle']
    reserve = LEGACY_RESERVE * (len(queue['body']['targets']) - len(queue['batches']))
    return max(0, min(default, queue['body']['attempt_limit'] - sum(v for c, v in used.items() if c != cycle) - reserve))


def budget(store, run_id):
    queue = current(store, run_id)
    if not queue:
        return None
    active = batch(store, run_id)
    if not active:
        raise ValueError('当前实验轮次尚未绑定数据集预算。')
    limit = active.get('attempt_limit') if task_budgeted(queue) else trial_limit(store, run_id, queue['body']['attempt_limit'])
    used = attempts(store, run_id, queue).get(active['cycle'], 0)
    from .experiment_attempt_policy import current as attempt_policy
    policy = attempt_policy(store, run_id)
    if policy:
        return {'dataset': active['dataset'], 'attempt_limit': None, 'attempts_used': used,
            'remaining_attempts': None, 'status': 'unlimited', 'scope': 'all_task_datasets',
            'mode': 'until_plan_complete', 'policy_id': policy['id'],
            'completion_rule': policy['body']['completion_rule']}
    return {'dataset': active['dataset'], 'attempt_limit': limit, 'attempts_used': used,
        'configuration_retry_authorizations': active.get('configuration_retry_authorizations', []),
        'remaining_attempts': max(0, limit - used) if limit is not None else None,
        'status': active.get('budget_status', 'pending') if task_budgeted(queue) else 'approved',
        'scope': 'per_dataset_task' if task_budgeted(queue) else 'legacy_shared',
        'includes': ['interface_checks', 'validation', 'diagnostics', 'final_tests', 'failed_attempts', 'retries']}


def _batch(identity, target, cycle, index, allowance):
    key = identity + ':batch-' + str(index + 1)
    return {'dataset': target['dataset'], 'target': copy.deepcopy(target), 'cycle': cycle,
            'setup_id': key, 'graph_thread_id': key, 'status': 'configuring',
            'attempt_limit': allowance, 'created_at': now()}


def activate(engine, run_id, instruction):
    """Start a new request; retain any failed old draft without reinterpreting it."""
    from .dataset_transition import current as transition, route
    from .dataset_experiments import COLLECTION as SETUPS
    from .experiment_decisions import _archive
    existing = current(engine.store, run_id)
    if existing:
        return existing
    record = transition(engine.store, run_id)
    receipt = route(engine.store, run_id)
    run = engine.store.get('runs', run_id)
    old = engine.store.get(SETUPS, receipt['id']) if receipt else None
    if (not receipt or not instruction.strip() or old and old.get('candidate')
            or run['stage'] not in {'experiments', 'tuning'}):
        raise ValueError('逐项配置需要尚未进入执行核验的实验配置入口。')
    with engine.store._read_db() as db:
        if db.execute("SELECT 1 FROM records WHERE collection='experiments' AND key>=? AND key<? "
            "AND coalesce(json_extract(data,'$.research_cycle'),1)=? LIMIT 1",
            (run_id + ':', run_id + ';', engine.cycle(run_id))).fetchone():
            raise ValueError('本轮已有测量，不能改写为新的配置队列。')
    targets = record['request']['targets']
    body = {'transition_id': record['id'], 'transition_sha256': record['sha256'],
        'config_sha256': sha(run['config']), 'start_cycle': engine.cycle(run_id),
        'targets': copy.deepcopy(targets), 'attempt_limit': run['config']['max_trials'],
        'budget_scope': 'per_dataset_task',
        'instruction': instruction.strip(), 'entry': run['stage']}
    identity = run_id + ':dataset-queue:' + sha(body)
    prior = _archive(engine, run_id, {'setup': old}) if old else None
    allowance = None
    queue = {'id': identity, 'run_id': run_id, 'body': body, 'sha256': sha(body),
        'batches': [_batch(identity, targets[0], engine.cycle(run_id), 0, allowance)],
        'status': 'active', 'previous_setup': prior, 'created_at': now()}
    engine.store.batch([(COLLECTION, identity, queue), ('runs', run_id, {**run,
        'state': {**run['state'], 'dataset_configuration_queue': identity},
        'status': 'running' if run['status'] == 'running' else 'paused', 'error': None, 'updated_at': now()})])
    engine.store.event(run_id, 'experiment.configuration_queue_started',
        '逐个配置和执行数据集；每份配置独立核验，实验次数按数据集分别计数。',
        {'queue_id': identity, 'datasets': [t['dataset'] for t in targets], 'previous_setup': prior})
    return queue


def prepare(engine, run_id, instruction):
    """Human recovery entry, called with the task lifecycle lock held."""
    run = engine.store.get('runs', run_id) or {}
    if (run.get('status') not in {'paused', 'blocked', 'failed'}
            or run_id in engine.tasks and not engine.tasks[run_id].done()
            or engine.store.active_reasoning(run_id, {'running', 'queued', 'starting', 'turn_start_dispatched',
                'thread_start_dispatched', 'thread_resume_dispatched'})):
        raise ValueError('需要已停止且没有活动请求的实验配置任务。')
    return activate(engine, run_id, instruction)


def view(store, run_id):
    queue = current(store, run_id)
    if not queue:
        return None
    used = attempts(store, run_id, queue)
    datasets = []
    task_based = task_budgeted(queue)
    for target in queue['body']['targets']:
        item = next((b for b in queue['batches'] if b['dataset'] == target['dataset']), None)
        count = used.get(item['cycle'], 0) if item else 0
        limit = (item.get('attempt_limit') if item else None) if task_based else (item['attempt_limit'] if item else LEGACY_RESERVE)
        datasets.append({'dataset': target['dataset'], 'attempt_limit': limit, 'attempts_used': count,
            'configuration_retry_authorizations': item.get('configuration_retry_authorizations', []) if item else [],
            'remaining_attempts': max(0, limit - count) if limit is not None else None,
            'budget_status': item.get('budget_status', 'pending') if item and task_based else 'pending' if task_based else 'approved',
            'budget_proposal': item.get('budget_proposal') if item else None,
            'status': item['status'] if item else 'pending'})
    from .experiment_attempt_policy import unlimited
    unbounded = unlimited(store, run_id)
    if unbounded:
        for item in datasets:
            item.update(attempt_limit=None, remaining_attempts=None, budget_status='unlimited')
    total_limit = None if task_based or unbounded else queue['body']['attempt_limit']
    return {'id': queue['id'], 'status': queue['status'],
        'budget_scope': 'per_dataset_task' if task_based else 'legacy_shared', 'datasets': datasets,
        'attempts_unlimited': unbounded,
        'active_dataset': queue['batches'][-1]['dataset'] if queue['status'] == 'active' else None,
        'pending_datasets': [t['dataset'] for t in queue['body']['targets'][len(queue['batches']):]],
        'batches': [{k: b[k] for k in ('dataset', 'cycle', 'setup_id', 'status')} for b in queue['batches']],
        'attempt_limit': total_limit, 'attempts_used': sum(d['attempts_used'] for d in datasets),
        'remaining_attempts': None if task_based or unbounded else max(0, total_limit - sum(used.values())),
        'scope': 'Each configuration has separate frozen evidence; pending datasets have no result.'}


def context_view(store, run_id):
    """Only dispatch identity and the current allowance enter agent requests."""
    queue = current(store, run_id)
    if not queue:
        return None
    return {'id': queue['id'], 'status': queue['status'],
        'active_dataset': queue['batches'][-1]['dataset'],
        'pending_datasets': [t['dataset'] for t in queue['body']['targets'][len(queue['batches']):]],
        'budget': budget(store, run_id)}


def advance(engine, run_id):
    """Called by the worker after its graph finishes, under the lifecycle lock."""
    from . import record_archive
    from .dataset_experiments import current as setup
    from .engine import ResearchBlocked
    from .orchestration import STAGES
    queue = current(engine.store, run_id)
    if not queue or queue['status'] != 'active':
        return False
    engine.check_active(run_id)
    run = engine.store.get('runs', run_id)
    active = queue['batches'][-1]
    if active['cycle'] != engine.cycle(run_id):
        raise ResearchBlocked('本数据集发生了方法版本修订；剩余数据配置保留，需先对齐新方法版本。')
    report = engine.store.get('steps', run_id + ':report') or {}
    saved = setup(engine, run_id) or {}
    if report.get('status') != 'completed' or report.get('cycle', 1) != active['cycle'] or saved.get('status') != 'ready':
        raise ResearchBlocked('本数据集尚无完整的流程结束记录，不能切换下一份配置。')
    from .dataset_optimization import require_stable
    require_stable(engine, run_id)
    program = engine.store.get('programs', run_id)
    source_release = None
    if len(queue['batches']) < len(queue['body']['targets']) and program != saved['program']:
        if not (program or {}).get('rapid_revision_id'):
            raise ResearchBlocked('当前方法代码已修订；需先核验后续数据配置的源码版本，原实验记录继续保留。')
        from .dataset_source_release import freeze
        source_release = freeze(engine, run_id, saved, program)
    with engine.store._read_db() as db:
        if db.execute("SELECT 1 FROM records WHERE collection='experiments' AND key>=? AND key<? "
            "AND json_extract(data,'$.status') IN ('running','interrupted') LIMIT 1",
            (run_id + ':', run_id + ';')).fetchone():
            raise ResearchBlocked('仍有执行中或未确认终态的实验，不能切换配置。')
    # Preserve the full report/results on disk. The next agent receives only its
    # own dataset, budget and source references, never previous held-out results.
    keys = [(name, run_id) for name in ('programs', 'selections', 'tuning_plans', 'experiment_plans', 'experiment_restarts')]
    keys += [('steps', run_id + ':' + stage) for stage in STAGES[STAGES.index('experiments'):]]
    keys += [('stage_contexts', run_id + ':' + stage) for stage in STAGES[STAGES.index('experiments'):]]
    archives = [record_archive.save(engine.store, run_id, f) for c, k in keys
                if (f := record_archive.fingerprint(engine.store, c, k))]
    active.update(status='finished', archives=archives, completed_at=now())
    if source_release:
        active['source_release'] = source_release
    review = engine.store.get('steps', run_id + ':validation_review') or {}
    stopped = review.get('output', {}).get('decision') == 'stop'
    active['outcome'] = 'stopped' if stopped else 'reviewed'
    pending = queue['body']['targets'][len(queue['batches']):]
    if not pending:
        queue.update(status='finished', updated_at=now())
        engine.store.put(COLLECTION, queue['id'], queue)
        engine.artifact(run_id, 'dataset-queue/summary.json', json.dumps(
            view(engine.store, run_id), ensure_ascii=False, indent=2), 'experiment_configuration')
        return False
    # A frozen allowance is stored before asking for the next configuration.
    # Validation of that configuration never depends on later attempt counts.
    used = sum(attempts(engine.store, run_id, queue).values())
    from .experiment_attempt_policy import unlimited
    allowance = (None if task_budgeted(queue) or unlimited(engine.store, run_id) else
        max(0, queue['body']['attempt_limit'] - used - LEGACY_RESERVE * (len(pending) - 1)))
    if allowance is not None and allowance < LEGACY_RESERVE:
        raise ResearchBlocked('共享实验预算不足以核验下一份配置并完成对照；其余数据集保留待配置。')
    cycle = engine.cycle(run_id) + 1
    following = _batch(queue['id'], pending[0], cycle, len(queue['batches']), allowance)
    deferred = next((b for b in queue.get('deferred_batches', []) if b['dataset'] == pending[0]['dataset']), None)
    if deferred:
        old_setup = engine.store.get('dataset_experiment_setups', deferred['setup_id']) or {}
        if old_setup.get('source_program_sha256') == sha(program):
            following = copy.deepcopy(deferred)
        else:
            # Preserve the paid old configuration and its graph; a changed
            # architecture requires a new source-bound configuration identity.
            generation = sha(program)
            following.update(source_generation=generation, setup_id=following['setup_id']+':source-'+generation)
            following['graph_thread_id'] = following['setup_id']
            following['previous_setup_id'] = deferred['setup_id']
    queue['batches'].append(following)
    engine.store.batch([(COLLECTION, queue['id'], queue), ('programs', run_id, program),
        ('runs', run_id, {**run, 'stage': 'tuning', 'progress': min(run.get('progress', 0), 70),
            'state': {**run['state'], 'research_cycle': cycle}, 'updated_at': now()}),
        ('graph_status', run_id, {'id': run_id, 'run_id': run_id, 'cycle': cycle,
            'node': 'tuning', 'status': 'running', 'updated_at': now()})],
        [(c, k) for c, k in keys if c != 'programs'])
    engine.store.event(run_id, 'experiment.next_configuration', '本份实验已归档，继续配置下一个数据集。',
        {'queue_id': queue['id'], 'dataset': pending[0]['dataset'], 'cycle': cycle})
    return True
