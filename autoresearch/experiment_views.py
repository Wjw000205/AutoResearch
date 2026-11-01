"""Bounded, read-only experiment views. Never load comparison arrays into a page."""
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from threading import RLock

from .storage import redact

_cache = OrderedDict()
_lock = RLock()
MAX_TRIALS = 256
TAIL_BYTES = 48 * 1024


def _bounded(path, limit=32768):
    value = f"json_extract(data,'$.{path}')"
    return f"CASE WHEN length({value})<={limit} THEN {value} ELSE NULL END"


def _projection(store, key, updated):
    cache_key = (str(store.path.resolve()), key, updated)
    with _lock:
        if cache_key in _cache:
            _cache.move_to_end(cache_key)
            return deepcopy(_cache[cache_key])
        fields = {name: f"json_extract(data,'$.{name}')" for name in (
            'id', 'run_id', 'status', 'attempt', 'started_at', 'completed_at', 'duration_seconds',
            'pid', 'process_started', 'exit_code', 'peak_memory_mb', 'cpu_seconds', 'superseded', 'research_cycle')}
        for name in ('artifact_path', 'error', 'provenance', 'code_revision', 'code_sha256', 'fingerprint', 'experiment_plan_sha256', 'superseded_reason'):
            fields[name] = _bounded(name, 4096)
        fields['bundle_sha256'] = _bounded('limits.code_bundle.sha256', 128)
        fields['metrics'] = _bounded('metrics')
        fields['metrics_complete'] = "length(json_extract(data,'$.metrics'))<=32768"
        fields['evaluation_errors'] = _bounded('evaluation_errors', 4096)
        fields['failure_diagnostic'] = _bounded('failure_diagnostic', 4096)
        fields['evaluation_errors_complete'] = "coalesce(length(json_extract(data,'$.evaluation_errors')),0)<=4096"
        # Transport arrays remain in the immutable execution record. Removing this
        # non-parameter payload in SQLite prevents a 600 MiB Python/HTTP object.
        params = "json_remove(json_extract(data,'$.params'),'$.comparison_inputs')"
        fields['params'] = f"CASE WHEN length({params})<=65536 THEN {params} ELSE NULL END"
        fields['transport_payload_omitted'] = "json_type(data,'$.params.comparison_inputs') IS NOT NULL"
        identity = ('dataset_id', 'split_fingerprints', 'phase', 'split_identity_protocol', 'split_materialization', 'tuning_budget')
        fields['evaluation'] = 'json_object(' + ','.join("'" + name + "'," + _bounded('evaluation.' + name, 4096) for name in identity) + ')'
        fields['has_split_identity_protocol'] = "json_type(data,'$.evaluation.split_identity_protocol') IS NOT NULL"
        fields['time_limits'] = "json_object('wall_seconds',json_extract(data,'$.limits.timeout_seconds'),'cpu_seconds',json_extract(data,'$.limits.cpu_seconds'),'policy',json_extract(data,'$.limits.runtime_training_policy.mode'))"
        expression = ','.join("'" + name + "'," + value for name, value in fields.items())
        with store._read_db() as db:
            row = db.execute(f"SELECT json_object({expression}) FROM records WHERE collection='experiments' AND key=? AND updated_at=?", (key, updated)).fetchone()
        if row is None:
            return None  # A newer saved result will appear on the next read.
        value = json.loads(row[0])
        protocol_present = value.pop('has_split_identity_protocol')
        value['evaluation'] = {k: v for k, v in value['evaluation'].items()
                               if v is not None or k == 'split_identity_protocol' and protocol_present}
        value.update(source_updated_at=updated, details_loaded=False, params_complete=value['params'] is not None)
        value['params'] = value['params'] or {}
        value['metrics'] = value['metrics'] or {}
        _cache[cache_key] = value
        while len(_cache) > MAX_TRIALS:
            _cache.popitem(last=False)
        return deepcopy(value)


def trials(store, run_id):
    # Trial keys are owned by run_id. Head reads do not parse the giant JSON body.
    with store._read_db() as db:
        heads = db.execute("SELECT key,updated_at FROM records WHERE collection='experiments' AND key>=? AND key<? ORDER BY updated_at DESC,key DESC LIMIT ?",
                           (run_id + ':', run_id + ';', MAX_TRIALS + 1)).fetchall()
    values = []
    for key, updated in reversed(heads[:MAX_TRIALS]):
        value = _projection(store, key, updated)
        if value and value['run_id'] == run_id:
            values.append(value)
    return values, len(heads) > MAX_TRIALS


def view(engine, run_id):
    # Trial heads, cycle, plan and policy must belong to one saved revision,
    # including when an operator changes the research cycle during a read.
    with engine.store.read_snapshot() as store:
        return _view(store, run_id)


def _view(store, run_id):
    from .evaluation import evaluate_trials, unconfigured_evaluation
    from .experiment_summary import build
    from .run_views import _one, _field
    values, truncated = trials(store, run_id)
    current_cycle = (store.get('runs', run_id) or {}).get('state', {}).get('research_cycle', 1)
    current_values = [r for r in values if (r.get('research_cycle') or 1) == current_cycle]
    design = _one(store, 'steps', {'metric': _field('output.primary_metric'),
                                       'direction': _field('output.direction')}, key=run_id + ':design') or {}
    evaluation = (evaluate_trials([r for r in current_values if r['params_complete']], design['metric'], design['direction'])
                  if design.get('metric') and design.get('direction') else unconfigured_evaluation())
    from . import benchmark_matrix
    # Read the saved matrix projection only. Display must not trigger architecture
    # authorization/source checks or parse giant legacy execution inventories.
    with store._read_db() as db:
        head = db.execute("SELECT CASE WHEN json_extract(data,'$.version')=3 THEN data ELSE NULL END "
            "FROM records WHERE collection='experiment_plans' AND json_extract(data,'$.run_id')=? "
            "AND coalesce(json_extract(data,'$.cycle'),1)=coalesce((SELECT json_extract(data,'$.state.research_cycle') "
            "FROM records WHERE collection='runs' AND key=?),1) ORDER BY updated_at DESC,key DESC LIMIT 1", (run_id, run_id)).fetchone()
    plan = json.loads(head[0]) if head and head[0] else None
    if benchmark_matrix.enabled(plan) and design.get('metric') and design.get('direction'):
        evaluation = benchmark_matrix.evaluation(plan, [r for r in current_values if r['params_complete']],
            design['metric'], design['direction'])
    evaluation['display_projection'] = True
    if truncated or any(not r['params_complete'] for r in values):
        evaluation['paired_comparisons'] = []
        for task in evaluation.get('benchmark_tasks', []):
            task['evaluation']['paired_comparisons'] = []
        evaluation['issues'].append({'code': 'display_projection_incomplete',
            'message': '页面只读取最近 256 条实验及大小受限的参数，当前记录不完整，暂不展示配对结论；原始记录保留。'})
    token = hashlib.sha256(json.dumps([[(r['id'], r['source_updated_at']) for r in values], plan]).encode()).hexdigest()[:24]
    summary = build(store, run_id, values, primary_metric=design.get('metric'),
                    direction=design.get('direction'), truncated=truncated)
    if benchmark_matrix.enabled(plan) and design.get('metric') and design.get('direction'):
        from .parameter_search import display_jobs
        supplemental, search_status = display_jobs(store, run_id, plan)
        summary['parameter_search_status'] = search_status
        summary['benchmark_tasks'] = benchmark_matrix.progress(plan, current_values, design['metric'], design['direction'],
            truncated=truncated or any(not r['params_complete'] for r in values) or search_status['state'] == 'unavailable',
            selection=store.get('selections', run_id), additional_jobs=supplemental)
    from .parameter_grid import view as grid_view
    grid = grid_view(store, run_id, current_values, plan=plan,
                     truncated=truncated or any(not r['params_complete'] for r in values))
    return {'projection': 'view', 'section': 'experiments', 'loaded_sections': ['experiments'],
            'parameter_grid': grid,
            'revision': token, 'experiments': values, 'evaluation': evaluation,
            'experiments_truncated': truncated, 'experiment_summary': summary}


def _safe_file(engine, run_id, trial, relative):
    allowed = (engine.runner.root / run_id).resolve()
    base = Path(trial.get('artifact_path') or '')
    if not base.is_absolute():
        return None
    candidate = base / relative
    if not candidate.resolve().is_relative_to(allowed):
        return None
    for part in (candidate, *candidate.parents):
        if part.is_symlink() or getattr(part, 'is_junction', lambda: False)():
            return None
        if part == allowed:
            break
    return candidate if candidate.is_file() else None


def _json_file(engine, run_id, trial, relative, limit):
    path = _safe_file(engine, run_id, trial, relative)
    if path is None:
        return None, None
    try:
        stat = path.stat()
        if stat.st_size > limit:
            return None, '文件超出页面读取上限，原文件仍保留。'
        with path.open('rb') as source:
            raw = source.read(limit + 1)
        if len(raw) > limit:
            return None, '文件正在增长，等待下一次刷新。'
        def finite_float(text):
            number = float(text)
            return number if math.isfinite(number) else None
        return json.loads(raw, parse_constant=lambda _: None, parse_float=finite_float), None
    except (OSError, ValueError):
        return None, '文件正在写入或暂时不可读，等待下一次刷新。'


def _tail(engine, run_id, trial, name):
    path = _safe_file(engine, run_id, trial, name + '.txt')
    if path is None:
        return {'text': '', 'bytes': 0, 'truncated': False, 'available': False}
    try:
        with path.open('rb') as source:
            size = source.seek(0, 2)
            source.seek(max(0, size - TAIL_BYTES))
            text = source.read(TAIL_BYTES).decode('utf-8', errors='replace')
            source.seek(0)
            head = source.read(2048).lstrip()
        structured = (name == 'stdout' and size > TAIL_BYTES and head.startswith(b'{')
                      and b'"evaluation"' in head and '\n' not in text.rstrip('\r\n'))
        return {'text': redact(text), 'bytes': size, 'truncated': size > TAIL_BYTES, 'available': True,
                'large_structured_output': structured}
    except OSError:
        return {'text': '', 'bytes': None, 'truncated': False, 'available': False}


def epoch_observations(events):
    """Project the last saved observation per epoch; never infer epochs from steps.

    Separate fits, objectives, stages and counter resets so unrelated training
    segments are not joined. Raw batch observations remain in the source log.
    """
    result, previous, segment = [], None, 0
    for event in events:
        epoch = event.get('epoch')
        valid_epoch = type(epoch) in (int, float) and math.isfinite(epoch) and epoch >= 0
        identity = tuple(event.get(k) for k in ('stage', 'fit_id', 'loss_name'))
        step = event.get('step')
        valid_step = type(step) in (int, float) and math.isfinite(step)
        reset = previous is not None and (
            identity != previous[0] or not valid_epoch or previous[1] is None
            or epoch < previous[1]
            or (valid_step and previous[2] is not None and step < previous[2]))
        if reset:
            segment += 1
        row = {k: v for k, v in event.items() if k != 'step'}
        row['segment'] = segment
        if valid_epoch:
            row['observation_count'] = 1
            if (result and result[-1].get('epoch') == epoch
                    and result[-1]['segment'] == segment):
                row['observation_count'] += result[-1].get('observation_count', 0)
                # A sparse non-loss event must not discard the last saved loss.
                row = {**result.pop(), **row}
            result.append(row)
        else:
            row.pop('epoch', None)
            result.append(row)
        previous = (identity, epoch if valid_epoch else None, step if valid_step else None)
    return result


def console(engine, run_id, trial_id=None):
    values, truncated = trials(engine.store, run_id)
    trial = next((row for row in values if row['id'] == trial_id), None) if trial_id else (
        next((row for row in reversed(values) if row['status'] == 'running'), None) or (values[-1] if values else None))
    if trial_id and trial is None:
        raise KeyError(trial_id)
    choices = [{**{k: row.get(k) for k in ('id', 'status', 'attempt')},
                'params': {k: row['params'].get(k) for k in ('algorithm', 'method', 'variant', 'phase', 'seed')}} for row in values]
    result = {'trials': choices, 'truncated': truncated, 'selected': trial,
              'observed_at': datetime.now(timezone.utc).isoformat(), 'source': 'saved_experiment_records_and_files'}
    if trial is None:
        return result
    state, state_error = _json_file(engine, run_id, trial, 'artifacts/training_state.json', 256 * 1024)
    log, log_error = _json_file(engine, run_id, trial, 'artifacts/training_log.json', 16 * 1024 * 1024)
    observation, observation_error = _json_file(engine, run_id, trial, 'resource_observation.json', 32768)
    events = log.get('events', []) if isinstance(log, dict) else log if isinstance(log, list) else []
    if not isinstance(events, list):
        events = []
    from .training_convergence import task_monitor, monitor_values
    monitor = task_monitor(engine, run_id)
    keys = ('step', 'epoch', 'stage', 'fit_id', 'loss_name', 'loss', 'monitor', 'learning_rate', 'reason', 'event')
    events = [row for row in events if isinstance(row, dict)]
    measured = monitor_values(events, **monitor)
    event_view = [{k: row[k] for k in keys if k in row and isinstance(row[k], (str, int, float, bool, type(None)))
                  and len(str(row[k])) <= 500} for row in events if isinstance(row, dict)]
    for row, value in zip(event_view, measured):
        if value is not None:
            row.update(validation_metric=monitor['metric'], validation_value=value, validation_direction=monitor['direction'])
    event_view = epoch_observations(event_view)
    points = [row for row in event_view if type(row.get('epoch')) in (int, float)
              and any(type(row.get(k)) in (int, float) and math.isfinite(row[k]) for k in ('loss', 'validation_value'))]
    stride = max(1, math.ceil(len(points) / 180))
    curve = points[::stride]
    if points and (not curve or points[-1] != curve[-1]):
        curve.append(points[-1])
    result.update(training=state if isinstance(state, dict) else None,
                  counters=(log.get('counters') if isinstance(log, dict) else None),
                  resource_observation=observation, events=event_view[-250:], event_count=len(event_view),
                  events_truncated=len(event_view) > 250, curve=curve,
                  source_event_count=len(events), event_projection='last_observation_per_epoch',
                  stdout=_tail(engine, run_id, trial, 'stdout'), stderr=_tail(engine, run_id, trial, 'stderr'),
                  notices=[e for e in (state_error, log_error, observation_error) if e])
    from .training_convergence import analyze
    from .training_adjustment import records
    result['convergence'] = analyze(log, phase=trial['params'].get('phase'), **monitor)
    saved = records(engine.store, run_id, trial_id=trial['id'])
    result['loss_discussion'] = saved[0] if saved else None
    from .training_review_recovery import view as loss_review_view
    result['loss_review_recovery'] = loss_review_view(engine, run_id)
    return result
