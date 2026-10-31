"""Task-specific performance criteria, independent of research domain.

An explicit operator amendment changes future interpretation. Frozen plans,
measurements, old comparisons and independent votes retain their identities.
"""
from __future__ import annotations

import asyncio
import copy
import json
import re

from .experiment_workflow import sha, encoded, validation_records
from .storage import now

COLLECTION = 'performance_policies'
ARCHIVES = 'performance_policy_archives'
MODE = 'aggregate_primary'
GUIDANCE = ('按本任务 performance_policy 指定的整体主指标、方向及聚合口径判断性能收益。'
    '各子集、场景和辅助指标允许有得有失，须说明收益与代价，不把诊断项变差当作自动否决。'
    '不要自行增加“所有指标、所有种子都提升”的要求。主指标无有效配对证据时仍须补证据；'
    '执行失败、数据泄漏、不公平比较和资源限制不属于可用性能收益抵消的问题。'
    '旧规则的 passed/scientific_passed 仅代表旧规则，不能作为当前规则的结论。'
    '整体指标满足后可以停止优化并报告已知取舍，不必为消除每个局部退化反复修改架构。')


def required(check):
    return check.get('required', True) is not False


def outcome(checks, complete):
    gates = [c for c in checks if required(c)]
    return ('negative' if any(c.get('passed') is False for c in gates) else
            'positive' if complete and gates and all(c.get('passed') is True for c in gates) else 'inconclusive')


def classify(acceptance, primary_metric):
    """New plans default to one primary performance gate; explicit constraints remain."""
    value = copy.deepcopy(acceptance)
    value['policy'] = MODE
    for criterion in value['criteria']:
        criterion.setdefault('required', criterion['metric'] == primary_metric)
        if criterion['metric'] == primary_metric:
            criterion['required'] = True
    return value


def reference(store, run_id):
    with store._read_db() as db:
        row = db.execute("SELECT json_extract(data,'$.state.performance_policy') FROM records "
                         "WHERE collection='runs' AND key=?", (run_id,)).fetchone()
    return row[0] if row else None


def current(store, run_id, *, check_host=True):
    key = reference(store, run_id)
    if not key:
        return dataset_policy(store, run_id)
    record = store.get(COLLECTION, key) or {}
    body = record.get('policy', {})
    run = store.get('runs', run_id) or {}
    from .rapid_continuation import validated, host_identity
    parent = validated(store, run_id, check_host=False) or {}
    if (record.get('id') != key or record.get('run_id') != run_id or record.get('sha256') != sha(body)
            or body.get('run_id') != run_id or body.get('mode') != MODE
            or body.get('cycle') != run.get('state', {}).get('research_cycle', 1)
            or body.get('config_sha256') != sha(run.get('config'))
            or body.get('parent_id') != parent.get('id') or body.get('parent_sha256') != parent.get('sha256')
            or body.get('plan_sha256') != sha(store.get('experiment_plans', body.get('plan_id', '')))
            or check_host and body.get('host_files') != host_identity()
            or body.get('scientific_approval') is not False or body.get('final_tests_allowed') is not False
            or body.get('criterion', {}).get('required') is not True
            or body.get('criterion', {}).get('minimum_relative_improvement') != 0
            or body.get('criterion', {}).get('comparison') != 'greater_than'
            or body.get('direction') not in {'minimize', 'maximize'}
            or body.get('criterion', {}).get('metric') != body.get('primary_metric')
            or body.get('criterion', {}).get('direction') != body.get('direction')
            or not body.get('operator_instruction')):
        raise ValueError('任务性能规则的来源、计划、配置或宿主版本已变化，请重新核对。')
    if body.get('previous_policy_id'):
        previous = store.get(COLLECTION, body['previous_policy_id']) or {}
        old = previous.get('policy', {})
        if (previous.get('run_id') != run_id or previous.get('sha256') != body.get('previous_policy_sha256')
                or sha(old) != previous.get('sha256')
                or any(old.get(k) != body.get(k) for k in
                    ('cycle', 'mode', 'primary_metric', 'direction', 'criterion', 'aggregation',
                     'diagnostic_policy', 'parent_id', 'parent_sha256', 'plan_id', 'plan_sha256',
                     'config_sha256', 'scientific_approval', 'final_tests_allowed'))):
            raise ValueError('宿主更新必须保留原任务规则及其不可变来源。')
    return record


def dataset_policy(store, run_id):
    """Carry the exact operator criterion explicitly authorized by a transition.

    Frozen plans and old comparisons remain unchanged. A model-authored stricter
    threshold cannot silently replace the criterion carried in that authority.
    """
    # The normal summary path must not load historical records when there is
    # no transition authority to resolve.
    with store._read_db() as db:
        active=db.execute("SELECT json_extract(data,'$.state.dataset_transition') FROM records "
            "WHERE collection='runs' AND key=?",(run_id,)).fetchone()
    if not active or not active[0]:return None
    from .dataset_transition import current as transition_current
    transition = transition_current(store, run_id)
    if not transition or not transition['request'].get('performance_policy'):
        return None
    run = store.get('runs', run_id)
    plan = store.get('experiment_plans', run_id) or {}
    cycle = run.get('state', {}).get('research_cycle', 1)
    if plan.get('cycle') != cycle or not plan.get('dataset_configuration_protocol'):
        return None
    request = transition['request']
    criterion = copy.deepcopy(request['performance_policy'])
    import math
    if (not request.get('operator_instruction') or criterion.get('required') is not True
            or criterion.get('direction') not in {'minimize', 'maximize'}
            or criterion.get('comparison') not in {'at_least','greater_than'}
            or not isinstance(criterion.get('metric'),str) or not criterion['metric'].startswith('val_')
            or type(criterion.get('minimum_relative_improvement')) not in (int,float)
            or not math.isfinite(criterion['minimum_relative_improvement'])):
        raise ValueError('Dataset transition performance authority is incomplete')
    criteria = (plan.get('acceptance') or {}).get('criteria', [])
    if not any(c.get('metric') == criterion['metric'] and c.get('direction') == criterion['direction'] for c in criteria):
        raise ValueError('Dataset transition criterion does not match the current plan metric')
    targets = {t['dataset'] for t in request['targets']}
    tasks = plan.get('task_plans') or []
    if tasks and not {t['dataset'] for t in tasks}.issubset(targets):
        raise ValueError('Dataset performance authority is outside its authorized targets')
    body = {'run_id':run_id,'cycle':cycle,'mode':MODE,
        'primary_metric':criterion['metric'],'direction':criterion['direction'],'criterion':criterion,
        'aggregation':('per_task_paired_seed_mean_then_relative_improvement' if tasks else
                       'paired_seed_mean_then_relative_improvement'),
        'diagnostic_policy':'辅助指标用于说明取舍；执行完整性、资源、架构、参数搜索和稳定性门禁保持。',
        'plan_id':run_id,'plan_sha256':sha(plan),'config_sha256':sha(run['config']),
        'authority':{'collection':'dataset_transitions','id':transition['id'],
            'sha256':transition['sha256'],'criterion_path':'/request/performance_policy',
            'operator_instruction':request['operator_instruction']},
        'historical_acceptance':copy.deepcopy(plan['acceptance']),
        'operator_instruction':request['operator_instruction'],'scientific_approval':False,'final_tests_allowed':False}
    return {'id':run_id+':dataset-performance:'+sha(body),'run_id':run_id,'sha256':sha(body),'policy':body,
        'provenance':'effective_criterion_from_authenticated_dataset_transition'}


def compact(record):
    if not record:
        return None
    body = record['policy']
    return {'id': record['id'], 'sha256': record['sha256'], **{k: body[k] for k in
        ('mode', 'primary_metric', 'direction', 'aggregation', 'criterion', 'diagnostic_policy')},
        **({'authority':body['authority']} if body.get('authority') else {}),
        'changed_after_observation': True, 'scientific_approval': False,
        'guidance': (GUIDANCE.replace('整体指标满足后可以停止优化并报告已知取舍，不必为消除每个局部退化反复修改架构。',
            '性能判据满足不代表架构评估、实际参数搜索或稳定性门禁已完成，不能提前结束当前阶段。')
            if body.get('authority') else GUIDANCE)}


def effective(store, run_id, design):
    record = current(store, run_id)
    return ((record['policy']['primary_metric'], record['policy']['direction']) if record else
            (design['primary_metric'], design['direction']))


def context_fields(store, run_id, *, binding=None):
    record=current(store, run_id)
    if (record and binding is not None and record['policy'].get('authority')
            and binding.get('performance_policy_sha256')!=record['sha256']):
        # Reconstructing an old code-review request must retain its original
        # policy namespace, rather than changing its paid handoff identity.
        return {}
    value = compact(record)
    return {'performance_policy': value} if value else {}


def assessment_context(evidence):
    if not evidence.get('performance_assessment'):
        return {}
    result = evidence['performance_assessment']
    return {'performance_policy': evidence['performance_policy'], 'performance_assessment': {
        **{k: result[k] for k in ('policy_id', 'policy_sha256', 'primary_metric', 'direction',
            'research_outcome', 'aggregation', 'changed_after_observation', 'scientific_approval')},
        'checks': [{k: check.get(k) for k in ('metric', 'passed', 'baseline_mean', 'proposed_mean',
            'relative_improvement', 'minimum_relative_improvement', 'comparison', 'n_pairs', 'complete_seed_coverage')}
            for check in result['checks']],
        'integrity_issue_count': len(result['integrity_issues']),
        'details_path': '/performance_assessment'}}


def observation_hash(row):
    # Scalar projection identity; legacy code hashes may be hydrated from the
    # saved environment by the ledger, without modifying the execution record.
    return sha({k: row.get(k) for k in ('id', 'status', 'params', 'metrics',
        'evaluation', 'code_revision', 'source_updated_at')})


def assess(rows, plan, record, *, phase='validation', _root_plan_sha256=None):
    """Compute the declared aggregate from bounded, validated scalar observations.

This is a new explicit comparison, never a fallback that re-labels the old
program's normalized comparison. Transport diagnostics retain their old scope.
"""
    from .experiment_plan import check_outcome, validation_seeds, final_test_seeds
    from .research_cycle import validation_summary
    body = record['policy']
    if plan.get('version') == 3:
        from .benchmark_matrix import task_trials, metadata
        tasks = [{**metadata(task), 'assessment':assess(task_trials(rows,task),task['plan'],record,
            phase=phase,_root_plan_sha256=sha(plan))} for task in plan['task_plans']]
        checks=[{**check,**{k:v for k,v in task.items() if k!='assessment'}}
                for task in tasks for check in task['assessment']['checks']]
        integrity=[task['task_id']+': '+issue for task in tasks for issue in task['assessment']['integrity_issues']]
        return {'policy_id':record['id'],'policy_sha256':record['sha256'],
            'primary_metric':body['primary_metric'],'direction':body['direction'],
            'research_outcome':outcome(checks,all(t['assessment']['research_outcome']=='positive' for t in tasks)),
            'aggregation':body['aggregation'],'diagnostic_policy':body['diagnostic_policy'],
            'checks':checks,'benchmark_tasks':tasks,'integrity_issues':integrity,
            'trial_ids':[tid for t in tasks for tid in t['assessment']['trial_ids']],
            'changed_after_observation':True,'scientific_approval':False,
            'source':'host_scalar_aggregate_under_operator_amendment',
            'scope':'Per-task paired observations under the authorized criterion; no pooled cross-dataset winner or scientific approval.'}
    cycle = body['cycle']
    selected = [r for r in rows if (r.get('research_cycle') or 1) == cycle and not r.get('superseded')
                and (r.get('params') or {}).get('phase') == phase
                and (r.get('params') or {}).get('variant') in {'baseline', 'proposed'}]
    seeds = validation_seeds(plan) if phase == 'validation' else final_test_seeds(plan)
    issues = []
    if any(r.get('status') != 'completed' or r.get('params_complete') is False
           or r.get('metrics_complete') is False or r.get('evaluation_errors')
           or r.get('evaluation_errors_complete') is False for r in selected):
        issues.append('Incomplete or invalid execution evidence; performance cannot waive integrity checks')
    identities = set()
    for row in selected:
        legacy = body.get('legacy_observations', {}).get(row['id'], {})
        identity = (row.get('code_revision'), row.get('code_sha256'), row.get('experiment_plan_sha256'))
        if legacy and legacy.get('projection_sha256') == observation_hash(row):
            saved = body['legacy_identity']
            identity = (row.get('code_revision'), row.get('code_sha256') or saved['code_sha256'],
                        row.get('experiment_plan_sha256') or saved['experiment_plan_sha256'])
        identities.add(identity)
        if identity[0] is None or not identity[1] or identity[2] != (_root_plan_sha256 or sha(plan)):
            issues.append('Missing or stale execution identity: ' + row['id'])
        evidence = row.get('evaluation') or {}
        splits = evidence.get('split_fingerprints') or {}
        if not evidence.get('dataset_id') or not splits.get('train') or not splits.get('validation'):
            issues.append('Missing dataset or validation split identity: ' + row['id'])
    if len(identities) != 1:
        issues.append('Missing or mixed execution identities')
    # Selection is still based on all frozen seeds, never on a favorable seed.
    summary = validation_summary(selected, body['primary_metric'], body['direction']) if phase == 'validation' else None
    if summary:
        issues.extend(i['code'] for i in summary.get('issues', [])
                      if i['code'] not in {'small_sample', 'unverified_tuning_budget'})
    if summary and summary.get('comparable'):
        chosen = set(summary['best_baseline']['trial_ids'] + summary['best_proposed']['trial_ids'])
        selected = [r for r in selected if r['id'] in chosen]
    result = check_outcome(selected, [body['criterion']], phase=phase,
                           complete=True, expected_seeds=seeds, use_reported=False)
    issues.extend(i['code'] for check in result['checks'] for i in check.get('issues', [])
                  if i.get('severity') == 'error')
    if issues:
        for check in result['checks']:
            check.update(passed=None, integrity_issues=issues)
        result['research_outcome'] = 'inconclusive'
    result.update(policy_id=record['id'], policy_sha256=record['sha256'],
        primary_metric=body['primary_metric'], direction=body['direction'],
        aggregation=body['aggregation'], diagnostic_policy=body['diagnostic_policy'],
        changed_after_observation=True, scientific_approval=False,
        source='host_scalar_aggregate_under_operator_amendment',
        trial_ids=[r['id'] for r in selected], integrity_issues=issues,
        scope='当前任务规则下的整体性能重评；旧规则结果保留，尚非独立科学审批或留出验证。')
    return result


def preview(engine, run_id, metric, direction):
    from .rapid_continuation import validated, host_identity
    from .record_archive import fingerprint
    if not isinstance(metric, str) or not re.fullmatch(r'val_[A-Za-z0-9_]+', metric) or direction not in {'minimize', 'maximize'}:
        raise ValueError('选择本任务的验证主指标和优化方向。')
    run = engine.store.get('runs', run_id)
    if not run or run.get('status') not in {'paused', 'blocked'} or engine.store.get('run_deletions', run_id):
        raise ValueError('先暂停当前任务，再调整性能判据。')
    # Metadata/source authority is checked here. The explicit amendment binds
    # the reviewed current host version; old host receipts are not overwritten.
    parent = validated(engine.store, run_id, check_host=False)
    if not parent:
        raise ValueError('本轮规则调整需要已授权的快速实验流程。')
    previous = current(engine.store, run_id, check_host=False)
    if previous and (previous['policy']['host_files'] == host_identity()
            or previous['policy']['primary_metric'] != metric or previous['policy']['direction'] != direction):
        raise ValueError('已有规则仅允许显式绑定新的宿主版本，不能覆盖或顺带修改任务判据。')
    plan = engine.store.get('experiment_plans', parent['policy_amendment']['plan_id'])
    if plan.get('version') == 3:
        raise ValueError('多任务基准需要先明确跨任务聚合权重；不能默认混合不同任务的指标。')
    rows = validation_records(engine.store, run_id)
    if any(r.get('status') in {'running', 'interrupted'} for r in rows):
        raise ValueError('存在未确认终态的实验，先保留执行现场。')
    if not any(metric in r.get('metrics', {}) for r in rows):
        raise ValueError('所选整体指标尚无已保存验证观测。')
    # Older executions predate inline code/plan hashes. Bind only observations
    # whose complete record still matches the archived quick-lane source.
    legacy = {}
    retained = {ref['key']: ref for ref in parent['sources']['trials']}
    for row in rows:
        if row.get('code_sha256') and row.get('experiment_plan_sha256'):
            continue
        key = run_id + ':' + row['id']
        ref = retained.get(key)
        if ref and fingerprint(engine.store, 'experiments', key) == ref:
            legacy[row['id']] = {'projection_sha256': observation_hash(row),
                                 'record_sha256': ref['sha256']}
    checkpoints = [('steps', f'{run_id}:{stage}') for stage in ('tuning', 'revision', 'validation_review', 'final_tests', 'report')]
    checkpoints += [('stage_contexts', key) for _, key in list(checkpoints)]
    checkpoints += [('experiment_decision_sessions', f'{run_id}:{engine.cycle(run_id)}'),
                    ('research_cycles', f'{run_id}:{engine.cycle(run_id)}')]
    sources = [ref for collection, key in checkpoints if (ref := fingerprint(engine.store, collection, key))]
    body = {'run_id': run_id, 'cycle': engine.cycle(run_id), 'mode': MODE,
        'primary_metric': metric, 'direction': direction,
        'criterion': {'metric': metric, 'direction': direction, 'minimum_relative_improvement': 0,
            'comparison': 'greater_than', 'required': True, 'rationale': '整体主指标相对同条件基线改善即可；局部收益与代价另行报告。'},
        'aggregation': 'paired_seed_mean_then_relative_improvement',
        'diagnostic_policy': '辅助指标和分场景结果用于诊断与说明取舍，不要求逐项改善，不自动否决。',
        'parent_id': parent['id'], 'parent_sha256': parent['sha256'],
        'plan_id': parent['policy_amendment']['plan_id'], 'plan_sha256': sha(plan),
        'config_sha256': sha(run['config']), 'host_files': host_identity(), 'source_refs': sources,
        'observations_sha256': sha([observation_hash(row) for row in rows]),
        'legacy_observations': legacy,
        'legacy_identity': {'code_sha256': parent['sources']['program'].get('code_sha256'),
            'experiment_plan_sha256': sha(plan)},
        'operator_instruction': '用户授权以任务整体指标为准，允许局部性能取舍。',
        'scientific_approval': False, 'final_tests_allowed': False}
    if previous:
        body.update(previous_policy_id=previous['id'], previous_policy_sha256=previous['sha256'])
        for key in ('criterion', 'aggregation', 'diagnostic_policy'):
            body[key] = copy.deepcopy(previous['policy'][key])
    provisional = {'id': run_id + ':performance:' + sha(body), 'sha256': sha(body), 'policy': body}
    return {'snapshot_id': sha(body), 'policy': body, 'assessment': assess(rows, plan, provisional)}


async def adopt(engine, run_id, snapshot_id, metric, direction, instruction):
    from .record_archive import save
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError('规则修订需要用户的明确指令。')
    async with engine._lifecycle_lock:
        if any(not task.done() for task in engine.tasks.values()):
            raise ValueError('等待活动 worker 停止后再修订规则。')
        run = engine.store.get('runs', run_id)
        def prepare():
            frozen = preview(engine, run_id, metric, direction)
            if frozen['snapshot_id'] != snapshot_id:
                raise ValueError('规则或证据已变化，请重新预览。')
            return frozen, [save(engine.store, run_id, ref) for ref in frozen['policy']['source_refs']]
        task = asyncio.create_task(asyncio.to_thread(prepare))
        try:
            frozen, archived = await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
        if engine.store.get('runs', run_id) != run:
            raise ValueError('任务状态已变化，未提交规则。')
        body = {**frozen['policy'], 'operator_instruction': instruction.strip()}
        key = run_id + ':performance:' + sha(body)
        record = {'id': key, 'run_id': run_id, 'sha256': sha(body), 'policy': body, 'created_at': now()}
        assessment = {**frozen['assessment'], 'policy_id': key, 'policy_sha256': record['sha256']}
        puts = [(COLLECTION, key, record), (ARCHIVES, key + ':assessment',
            {'id': key + ':assessment', 'run_id': run_id, 'assessment': assessment, 'created_at': now()}),
            ('runs', run_id, {**run, 'state': {**run.get('state', {}), 'performance_policy': key}, 'updated_at': now()})]
        deletes = []
        for source in archived:
            archive_id = key + ':' + source['collection'] + ':' + source['key']
            puts.append((ARCHIVES, archive_id, {'id': archive_id, 'run_id': run_id, 'source': source}))
            if source['collection'] != 'research_cycles':
                deletes.append((source['collection'], source['key']))
        engine.store.batch(puts, deletes)
        engine.artifact(run_id, f'performance-policy/{record["sha256"]}/rule.json', encoded(record), 'review')
        engine.store.event(run_id, 'experiment.performance_policy_changed',
            ('已按用户代码修正指令绑定新宿主版本；任务判据与历史记录保留。' if body.get('previous_policy_id') else
             '用户已将本任务验收调整为整体主指标；局部指标保留为诊断，不逐项否决。'),
            {'policy_id': key, 'sha256': record['sha256'], 'primary_metric': metric, 'direction': direction})
        return {'policy': compact(record), 'assessment': assessment}
