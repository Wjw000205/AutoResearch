"""Auditable project review time: running intervals, with explicit legacy adoption."""
from __future__ import annotations

import copy
import json
from datetime import datetime

from .storage import now
from .implementation_review_routing import digest

ACTIVE = 'policy_active_time'
WALL = 'policy_wall_time'
COLLECTION = 'implementation_acceptance_budgets'
_STATES = {'run.started': 'running', 'run.resume_authorized': 'running',
    'run.paused': 'paused', 'run.recovered': 'paused', 'stage.interrupted': 'paused',
    'run.blocked': 'blocked', 'run.failed': 'failed', 'run.completed': 'completed',
    'run.cancelled': 'cancelled'}


def _get(db, collection, key, default=None):
    row = db.execute('SELECT data FROM records WHERE collection=? AND key=?', (collection, key)).fetchone()
    return json.loads(row['data']) if row else default


def _put(store, db, collection, key, value, at):
    store._guard_write(db, collection, key, value)
    db.execute('INSERT INTO records VALUES (?,?,?,?) ON CONFLICT(collection,key) '
        'DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at',
        (collection, key, json.dumps(value, ensure_ascii=False, allow_nan=False), at))


def _greater_limit(value, ceiling):
    return ceiling is not None and (value is None or value > ceiling)


def _remaining(limit, used):
    return None if limit is None else max(0, limit - used)


def _adjustment(db, run_id, record):
    key = record.get('clock_adjustment_id')
    if not key:
        return None
    audit = _get(db, 'implementation_review_clock_adjustments', key, {})
    if (audit.get('id') != key or audit.get('run_id') != run_id
            or audit.get('sha256') != digest({k: v for k, v in audit.items() if k != 'sha256'})
            or _greater_limit(record['max_seconds'], audit['after_budget']['max_seconds'])
            or _greater_limit(record['max_calls'], audit['after_budget']['max_calls'])):
        raise ValueError('审核计时调整记录身份或摘要不一致。')
    return audit


def effective_restart_config(db, run, target):
    """Project only operator-authorized budget changes onto an old restart receipt."""
    record = _get(db, COLLECTION, run['id'], {})
    audit = _adjustment(db, run['id'], record)
    if audit is None:
        return target
    latest, seen = audit, set()
    while audit is not None:
        if audit['id'] in seen:
            raise ValueError('审核预算调整记录链出现循环。')
        seen.add(audit['id'])
        before_run = audit['before']['run']
        if any(before_run.get('state', {}).get(key) != run.get('state', {}).get(key)
               for key in ('implementation_restart', 'implementation_generation')):
            return target
        original = before_run['config']
        audit = _adjustment(db, run['id'], audit['before']['budget'])
    if any(type(original.get(k)) is not type(v) or original.get(k) != v for k, v in target.items()):
        return target
    return {**target, 'implementation_review_time_basis': ACTIVE,
        'implementation_review_max_seconds': latest['after_budget']['max_seconds'],
        'implementation_review_max_calls': latest['after_budget']['max_calls']}


def usage(db, run_id, record, at):
    """Only confirmed paused/blocked intervals are excluded; unknown time is charged."""
    start, end = datetime.fromisoformat(record['started_at']), datetime.fromisoformat(at)
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError('审核预算时间戳必须带时区。')
    end = max(start, end)
    kinds = [*_STATES, 'run.status_changed']
    rows = db.execute('SELECT id,kind,data,created_at FROM events WHERE run_id=? '
        'AND created_at>=? AND kind IN (' + ','.join('?' for _ in kinds) + ') ORDER BY id',
        (run_id, record['started_at'], *kinds)).fetchall()
    events = []
    for row in rows:
        stamp = datetime.fromisoformat(row['created_at'])
        status = json.loads(row['data']).get('status') if row['kind'] == 'run.status_changed' else _STATES[row['kind']]
        if start <= stamp <= end and isinstance(status, str):
            events.append({'id': row['id'], 'at': row['created_at'], 'status': status})
    events.sort(key=lambda event: (datetime.fromisoformat(event['at']), event['id']))
    current = record.get('initial_status', 'running')
    cursor, active, excluded = start, 0.0, []
    for event in [*events, {'at': end.isoformat(), 'status': current}]:
        stamp = datetime.fromisoformat(event['at'])
        if current == 'running':
            active += (stamp - cursor).total_seconds()
        elif stamp > cursor:
            excluded.append({'start': cursor.isoformat(), 'end': stamp.isoformat(),
                             'status': current, 'seconds': (stamp - cursor).total_seconds()})
        cursor, current = stamp, event['status']
    wall = (end - start).total_seconds()
    return {'active_seconds': active, 'wall_seconds': wall, 'excluded_seconds': wall - active,
            'excluded_intervals': excluded, 'event_sha256': digest(events), 'event_count': len(events),
            'as_of': at}


def charge(store, run_id, policy, *, consume=False, at=None):
    at = at or now()
    with store._db() as db:
        db.execute('BEGIN IMMEDIATE')
        store._guard_run(db, run_id)
        record = _get(db, COLLECTION, run_id)
        if record is None:
            run = _get(db, 'runs', run_id, {})
            basis = run.get('config', {}).get('implementation_review_time_basis', ACTIVE)
            record = {'id': run_id, 'run_id': run_id, 'started_at': at, 'calls': 0,
                'max_calls': policy['max_calls'], 'max_seconds': policy['max_seconds'],
                'time_basis': ACTIVE if basis == ACTIVE else WALL,
                'initial_status': run.get('status', 'running')}
        adjustment = _adjustment(db, run_id, record)
        for field, replaced in [('max_seconds', 'replaced_time_limits'), ('max_calls', 'replaced_call_limits')]:
            requested_limit = policy[field]
            if adjustment and requested_limit in adjustment[replaced]:
                # Explicit operator extensions apply to original frozen packets;
                # their historical budget fields and scientific contracts stay intact.
                requested_limit = adjustment['after_budget'][field]
            limits = [v for v in (record[field], requested_limit) if v is not None]
            record[field] = min(limits) if limits else None
        if record.get('time_basis', WALL) == ACTIVE:
            measured = usage(db, run_id, record, at)
            elapsed = max(record.get('elapsed_seconds', 0), measured['active_seconds'])
            record.update(elapsed_seconds=elapsed, excluded_seconds=measured['excluded_seconds'],
                          observed_at=at, status_events_sha256=measured['event_sha256'])
        else:
            elapsed = max(0, (datetime.fromisoformat(at) - datetime.fromisoformat(record['started_at'])).total_seconds())
        exhausted = []
        if record['max_seconds'] is not None and elapsed >= record['max_seconds']:
            exhausted.append('项目级审核时间预算耗尽')
        if record['max_calls'] is not None and record['calls'] >= record['max_calls']:
            exhausted.append('项目级审核调用预算耗尽')
        if consume and not exhausted:
            record['calls'] += 1
        _put(store, db, COLLECTION, run_id, record, at)
    return exhausted


def _recovery(workflow):
    """Restore only the exact pre-timeout REPAIR ledger, proven by its saved digest."""
    ledger = workflow.get('acceptance_review', {})
    reasons = ledger.get('budget_exhausted')
    budget_stop = workflow.get('blocked_kind') == 'codex_spec_review_budget_exhausted'
    replay_stop = (workflow.get('blocked_kind') == 'codex_spec_rejected'
        and workflow.get('reason') == '实现文档审核已结束：BLOCKED；当前版本未获准进入代码实现。')
    if (workflow.get('status') != 'blocked' or not (budget_stop or replay_stop)
            or not isinstance(reasons, list) or not reasons
            or any(r not in ('项目级审核时间预算耗尽', '项目级审核调用预算耗尽') for r in reasons)):
        return None
    rows = workflow.get('specifications', [])
    if not rows or rows[-1].get('review_outcome') != 'REPAIR' or rows[-1].get('approved') is not False:
        return None
    original = copy.deepcopy(ledger)
    original.pop('budget_exhausted', None)
    original.update(phase='REPAIR', outcome='REPAIR')
    if original.get('release_sha256') != digest({k: v for k, v in original.items() if k != 'release_sha256'}):
        return None
    return {**copy.deepcopy(workflow), 'acceptance_review': original,
            'status': 'specification', 'blocked_kind': None, 'reason': None}


def _preview(db, run_id, max_seconds=None, max_calls=None, *, unlimited=False):
    run = _get(db, 'runs', run_id)
    if not run:
        raise ValueError('研究任务不存在。')
    if run.get('status') not in {'paused', 'blocked'}:
        raise ValueError('调整审核计时前须停止运行。')
    record = _get(db, COLLECTION, run_id)
    if not record:
        raise ValueError('任务尚无项目审核预算记录。')
    _adjustment(db, run_id, record)
    if type(unlimited) is not bool or unlimited and (max_seconds is not None or max_calls is not None):
        raise ValueError('无上限设置不能同时指定有限额度。')
    limit = None if unlimited else (record['max_seconds'] if max_seconds is None else max_seconds)
    if limit is not None and (type(limit) is not int or not 1 <= limit <= 604800 or _greater_limit(record['max_seconds'], limit)):
        raise ValueError('本入口仅允许保持或延长有限时间预算，最长 7 天。')
    call_limit = None if unlimited else (record['max_calls'] if max_calls is None else max_calls)
    if call_limit is not None and (type(call_limit) is not int or not 1 <= call_limit <= 10000 or _greater_limit(record['max_calls'], call_limit)):
        raise ValueError('本入口仅允许保持或提高有限调用上限，最多 10000 次。')
    # Stopped time cannot change the actual usage or the preview identity.
    measured = usage(db, run_id, record, run['updated_at'])
    measured['active_seconds'] = max(record.get('elapsed_seconds', 0), measured['active_seconds'])
    workflows = [json.loads(row['data']) for row in db.execute(
        "SELECT data FROM records WHERE collection='codex_workflows' AND json_extract(data,'$.run_id')=?", (run_id,))]
    generation = run.get('state', {}).get('implementation_generation')
    recoverable = [w['id'] for w in workflows if w.get('implementation_generation') == generation
                   and _recovery(w) is not None]
    if len(recoverable) > 1:
        raise ValueError('当前代际存在多个可恢复的审核工作流，需要先核对工作流身份。')
    value = {'run_id': run_id, 'from_time_basis': record.get('time_basis', WALL), 'to_time_basis': ACTIVE,
        'budget': record, 'max_seconds': limit, 'max_calls': call_limit, 'usage': measured,
        'remaining_seconds': _remaining(limit, measured['active_seconds']),
        'recoverable_workflows': recoverable, 'run_sha256': digest(run),
        'workflow_sha256': {w['id']: digest(w) for w in workflows},
        'scientific_approval': False, 'calls_reset': False}
    value['preview_sha256'] = digest(value)
    return value, run, workflows


def preview(store, run_id, max_seconds=None, max_calls=None, *, unlimited=False):
    with store._read_db() as db:
        db.execute('BEGIN')
        return _preview(db, run_id, max_seconds, max_calls, unlimited=unlimited)[0]


def adopt(store, run_id, expected_sha256, max_seconds=None, max_calls=None, *, unlimited=False):
    """Explicit operator adjustment, atomic with archived before/after records."""
    if type(unlimited) is not bool or unlimited and (max_seconds is not None or max_calls is not None):
        raise ValueError('无上限设置不能同时指定有限额度。')
    audit_id = run_id + ':active-review-clock:' + expected_sha256
    with store._db() as db:
        db.execute('BEGIN IMMEDIATE')
        store._guard_run(db, run_id)
        previous = _get(db, 'implementation_review_clock_adjustments', audit_id)
        if previous:
            if unlimited and any(previous['after_budget'][k] is not None for k in ('max_seconds', 'max_calls')):
                raise ValueError('调整上限与原预览不一致，请重新获取预览。')
            if any(value is not None and value != previous['after_budget'][field]
                   for field, value in [('max_seconds', max_seconds), ('max_calls', max_calls)]):
                raise ValueError('调整上限与原预览不一致，请重新获取预览。')
            return previous['result']
        value, run, workflows = _preview(db, run_id, max_seconds, max_calls, unlimited=unlimited)
        if value['preview_sha256'] != expected_sha256:
            raise ValueError('审核状态已改变，请重新获取计时调整预览。')
        if (value['from_time_basis'] == ACTIVE and not value['recoverable_workflows']
                and all(value[k] == value['budget'][k] for k in ('max_seconds', 'max_calls'))):
            raise ValueError('任务已经按运行时间计时且上限未改变，无需重复调整。')
        at, before = now(), value['budget']
        after = {**before, 'time_basis': ACTIVE, 'max_seconds': value['max_seconds'], 'max_calls': value['max_calls'],
            'initial_status': before.get('initial_status', 'running'),
            'elapsed_seconds': value['usage']['active_seconds'],
            'excluded_seconds': value['usage']['excluded_seconds'], 'observed_at': value['usage']['as_of'],
            'status_events_sha256': value['usage']['event_sha256'], 'clock_adjustment_id': audit_id}
        changed = []
        if (after['max_seconds'] is None or after['elapsed_seconds'] < after['max_seconds']) and (after['max_calls'] is None or after['calls'] < after['max_calls']):
            for workflow in workflows:
                restored = _recovery(workflow) if workflow['id'] in value['recoverable_workflows'] else None
                if restored is not None:
                    _put(store, db, 'codex_workflows', workflow['id'], restored, at)
                    changed.append(workflow['id'])
        updated_run = {**run, 'config': {**run['config'], 'implementation_review_time_basis': ACTIVE,
                       'implementation_review_max_seconds': after['max_seconds'],
                       'implementation_review_max_calls': after['max_calls']}, 'updated_at': at}
        if changed and run.get('error') == '项目级审核时间预算耗尽':
            updated_run.update(status='paused', error=None)
        result = {'adjusted': True, 'run_id': run_id, 'time_basis': ACTIVE,
            'max_seconds': after['max_seconds'], 'elapsed_seconds': after['elapsed_seconds'],
            'remaining_seconds': _remaining(after['max_seconds'], after['elapsed_seconds']),
            'excluded_seconds': after['excluded_seconds'], 'calls': after['calls'], 'max_calls': after['max_calls'],
            'restored_workflows': changed, 'audit_id': audit_id, 'scientific_approval': False}
        previous_adjustment = _adjustment(db, run_id, before)
        replaced = [before['max_seconds'], *(previous_adjustment['replaced_time_limits'] if previous_adjustment else [])]
        replaced_calls = [before['max_calls'], *(previous_adjustment['replaced_call_limits'] if previous_adjustment else [])]
        audit = {'id': audit_id, 'run_id': run_id, 'created_at': at, 'preview': value,
            'replaced_time_limits': sorted(set(replaced), key=lambda v: (v is None, v or 0)), 'replaced_call_limits': sorted(set(replaced_calls), key=lambda v: (v is None, v or 0)),
            'before': {'budget': before, 'run': run, 'workflows': workflows},
            'after_budget': after, 'result': result, 'authorization': 'explicit_operator_time_basis_adjustment'}
        audit['sha256'] = digest(audit)
        _put(store, db, 'implementation_review_clock_adjustments', audit_id, audit, at)
        _put(store, db, COLLECTION, run_id, after, at)
        _put(store, db, 'runs', run_id, updated_run, at)
        # An explicit stopped-state observation anchors sparse legacy histories too.
        db.execute('INSERT INTO events(run_id,kind,message,data,created_at) VALUES(?,?,?,?,?)',
            (run_id, 'run.status_changed', '审核计时调整时确认停止状态。',
             json.dumps({'previous_status': run['status'], 'status': updated_run['status']}), at))
        db.execute('INSERT INTO events(run_id,kind,message,data,created_at) VALUES(?,?,?,?,?)',
            (run_id, 'implementation.review_clock_adjusted', '已按用户指定时间上限累计运行时间；保留调用和原记录。',
             json.dumps(result, ensure_ascii=False), at))
    return result
