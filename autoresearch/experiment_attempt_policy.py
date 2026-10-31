"""Explicit task-level attempt policy, separate from immutable experiment plans."""
from .experiment_workflow import sha
from .storage import now

COLLECTION = 'experiment_attempt_policies'
KEY = 'experiment_attempt_policy'


def current(store, run_id):
    run = store.get('runs', run_id) or {}
    ref = run.get('state', {}).get(KEY)
    if not ref:
        return None
    row = store.get(COLLECTION, ref.get('id', '')) or {}
    body = row.get('body', {})
    if (row.get('run_id') != run_id or sha(body) != ref.get('sha256')
            or row.get('sha256') != ref['sha256'] or body.get('mode') != 'until_plan_complete'
            or body.get('run_id') != run_id):
        raise ValueError('实验次数策略的授权记录发生变化。')
    return row


def unlimited(store, run_id):
    return current(store, run_id) is not None


def prepare(engine, run_id, *, instruction):
    run = engine.store.get('runs', run_id) or {}
    if (run.get('status') not in {'paused', 'blocked', 'failed'}
            or not isinstance(instruction, str) or not 1 <= len(instruction.strip()) <= 4000):
        raise ValueError('需要已停止的任务和明确的取消实验次数上限指令。')
    if current(engine.store, run_id):
        return current(engine.store, run_id)
    body = {'run_id': run_id, 'mode': 'until_plan_complete', 'attempt_limit': None,
        'instruction': instruction.strip(), 'scope': 'all_task_datasets',
        'completion_rule': 'Complete the declared validation and ablation coverage, reusing completed runs. '
            'Re-evaluate after each batch. No stop solely because of experiment counts. '
            'Execution failures require repair; independent acceptance and held-out authorization remain required.',
        'scientific_approval': False}
    digest = sha(body)
    row = {'id': run_id + ':attempt-policy:' + digest, 'run_id': run_id,
        'body': body, 'sha256': digest, 'created_at': now()}
    engine.store.batch([(COLLECTION, row['id'], row), ('runs', run_id, {**run,
        'state': {**run.get('state', {}), KEY: {'id': row['id'], 'sha256': digest}}, 'updated_at': now()})])
    engine.store.event(run_id, 'experiment.attempt_limit_removed',
        '按用户要求取消本任务所有数据集的实验次数上限，保留已完成结果与逐批决策。',
        {'policy_id': row['id'], 'attempt_limit': None})
    return row
