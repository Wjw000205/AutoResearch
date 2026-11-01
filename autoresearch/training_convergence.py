"""Versioned loss diagnostics from saved observations, never scientific acceptance."""
from __future__ import annotations

import hashlib
import json
import math
from statistics import median

POLICY = {'version': 2, 'window': 5, 'minimum_points': 15,
          'relative_plateau': .01, 'relative_improvement': .05,
          'relative_divergence': .10, 'relative_oscillation': .20}
ALERTS = {'plateau', 'diverging', 'oscillating', 'overfitting', 'invalid_loss'}
LABELS = {'decreasing': 'loss 持续下降', 'stable_after_improvement': '下降后趋稳',
          'plateau': 'loss 停滞', 'diverging': '疑似发散', 'oscillating': '明显震荡',
          'overfitting': '疑似过拟合', 'invalid_loss': 'loss 数值异常',
          'insufficient_evidence': '收敛证据不足', 'not_applicable': '无需迭代收敛判断',
          'held_out_excluded': '独立测试不用于调整'}


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def segment_identity(row):
    return [str(row.get(k, 'unspecified'))[:120] for k in
            ('stage', 'fit_id', 'loss_name', 'loss_scale', 'reduction')]


def failure_observation(log, identity=None):
    """Separate failure events may deliberately omit a non-finite scalar."""
    event = log.get('failure_event') if isinstance(log, dict) else None
    if not isinstance(event, dict) or event.get('phase') not in (None, 'train', 'validation'):
        return {}
    if identity and any(k in event and str(event[k])[:120] != identity[i]
                        for i, k in enumerate(('stage', 'fit_id', 'loss_name', 'loss_scale', 'reduction'))):
        return {}
    allowed = {'step', 'epoch', 'stage', 'fit_id', 'loss_name', 'loss_scale', 'reduction',
               'optimizer_step_completed', 'exception_type', 'loss'}
    return {k: v[:240] if isinstance(v, str) else v if not isinstance(v, float) or finite(v) else None
            for k, v in event.items() if (k in allowed or k.endswith('_nonfinite_count'))
            and (v is None or isinstance(v, (str, bool)) or type(v) in (int, float))}


def _relative(before, after):
    # Normalizing first avoids overflow even for finite very large losses.
    scale = max(abs(before), abs(after), 1e-12)
    return (before / scale - after / scale) / max(abs(before / scale), 1e-12)


def monitor_values(rows, metric, direction):
    if not isinstance(metric, str) or not metric or metric.startswith(('test_', 'held_out')) or direction not in ('minimize', 'maximize'):
        return []
    fields = [metric, metric.replace('val_', 'validation_', 1)]
    values = []
    for row in rows:
        value = next((row[k] for k in fields if finite(row.get(k))), None)
        if value is None and isinstance(row.get('metrics'), dict):
            value = row['metrics'].get(metric)
        if value is None and row.get('validation_metric') == metric:
            value = row.get('validation_value')
        values.append(value if finite(value) else None)
    return values


def task_monitor(engine, run_id):
    design = (engine.store.get('steps', run_id + ':design') or {}).get('output') or {}
    return {'metric': design.get('primary_metric'), 'direction': design.get('direction')}


def _series(rows, metric, direction):
    losses = [r['loss'] for r in rows if finite(r.get('loss'))]
    result = {'points': len(losses), 'status': 'insufficient_evidence',
              'reason': '同一训练阶段至少需要 15 个有序 loss 观测点。'}
    if any('loss' in r and not finite(r['loss']) for r in rows):
        return {**result, 'status': 'invalid_loss', 'reason': '已保存的 loss 含非有限值或无效数值；需检查数值稳定性。'}
    if len(losses) < POLICY['minimum_points']:
        return result
    w = POLICY['window']
    initial, previous, recent = (median(losses[:w]), median(losses[-2*w:-w]), median(losses[-w:]))
    earlier = median(losses[-3*w:-2*w])
    total, change = _relative(initial, recent), _relative(previous, recent)
    tail = losses[-2*w:]
    scale = max(abs(x) for x in tail) or 1e-12
    span = max(tail) / scale - min(tail) / scale
    signs = [1 if b > a else -1 if b < a else 0 for a, b in zip(tail, tail[1:])]
    turns = sum(a * b < 0 for a, b in zip(signs, signs[1:])) / max(1, len(signs) - 1)
    result.update(initial_median=initial, previous_median=previous, recent_median=recent,
                  relative_improvement=total, recent_relative_improvement=change,
                  relative_tail_range=span, direction_reversal_fraction=turns)
    validation = monitor_values(rows, metric, direction)
    validation = [v for v in validation if finite(v)]
    if len(validation) >= 2*w:
        val_change = _relative(median(validation[-2*w:-w]), median(validation[-w:])) * (-1 if direction == 'maximize' else 1)
        result['validation_relative_improvement'] = val_change
        if val_change < -POLICY['relative_divergence'] and change > POLICY['relative_plateau']:
            return {**result, 'status': 'overfitting', 'reason': '训练 loss 下降，但最近两个验证窗口的任务指标变差；需核查泛化与检查点策略。'}
    if change < -POLICY['relative_divergence'] and _relative(earlier, previous) < -POLICY['relative_divergence']:
        status, reason = 'diverging', '连续两个窗口的 loss 中位数上升超过 10%；需核查学习率、梯度与输入尺度。'
    elif span > POLICY['relative_oscillation'] and turns >= .5:
        status, reason = 'oscillating', '近期 loss 振幅较大且频繁反转；批次噪声、优化器或阶段配置可能需要检查。'
    elif abs(change) <= POLICY['relative_plateau'] and span <= POLICY['relative_oscillation']:
        status = 'stable_after_improvement' if total >= POLICY['relative_improvement'] else 'plateau'
        reason = ('loss 较初期下降后趋稳；仅为趋势信号，仍需验证指标和原验收条件。' if status == 'stable_after_improvement'
                  else '近期 loss 趋平且相对初期改善不足 5%；需区分初值已优、梯度停滞与模型表达限制。')
    elif change > POLICY['relative_plateau']:
        status, reason = 'decreasing', '近期 loss 中位数下降；尚不能据此认定已经收敛。'
    else:
        status, reason = 'insufficient_evidence', '当前趋势不足以可靠判断；需要更多同阶段观测或优化器诊断。'
    return {**result, 'status': status, 'reason': reason}


def analyze(log, *, phase='validation', metric=None, direction=None):
    declared = log.get('monitor', {}) if isinstance(log, dict) else {}
    if not isinstance(declared, dict):
        declared = {}
    if metric is None:
        metric, direction = declared.get('metric') or declared.get('name'), declared.get('direction')
    if metric is None:
        metric, direction = 'validation_loss', 'minimize'
    base = {'policy': POLICY.copy(), 'scientific_approval': False,
            'basis': 'saved_training_observations', 'heuristic': True, 'segments': [],
            'monitor': {'metric': metric, 'direction': direction}}
    if phase != 'validation':
        return {**base, 'status': 'held_out_excluded', 'label': LABELS['held_out_excluded'],
                'needs_discussion': False, 'reason': '独立测试记录不进入微调讨论。'}
    if isinstance(log, dict) and log.get('applicable') is False and isinstance(log.get('reason'), str) and log['reason'].strip():
        return {**base, 'status': 'not_applicable', 'label': LABELS['not_applicable'],
                'needs_discussion': False, 'reason': log['reason'][:500], 'reported_by_program': True}
    events = log.get('events', []) if isinstance(log, dict) else log if isinstance(log, list) else []
    segments, rows, identity, last_step = [], [], None, None
    for row in events if isinstance(events, list) else []:
        if not isinstance(row, dict) or row.get('phase') not in (None, 'train', 'validation') or not ('loss' in row or row.get('loss_nonfinite_count')):
            continue
        step = row.get('step', row.get('epoch'))
        if not finite(step):
            continue
        current = segment_identity(row)
        if rows and (current != identity or step <= last_step):
            segments.append({'identity': list(identity), **_series(rows, metric, direction)})
            rows = []
        identity, last_step = current, step
        rows.append(row)
    if rows:
        segments.append({'identity': list(identity), **_series(rows, metric, direction)})
    latest = segments[-1] if segments else {'status': 'insufficient_evidence', 'points': 0,
        'reason': '尚无可用的有序训练 loss 日志；不能把执行完成当作已收敛。'}
    status = latest['status']
    failure = failure_observation(log, identity)
    termination = log.get('termination', {}) if isinstance(log, dict) else {}
    terminated_by_exception = isinstance(termination, dict) and bool(termination.get('exception_type'))
    metadata_identity = log.get('identity') if isinstance(log, dict) else None
    if metadata_identity and identity and segment_identity(metadata_identity) != identity:
        terminated_by_exception = False
    nonfinite_fields = sorted({k for r in rows + [failure] for k,v in r.items()
                               if k.endswith('_nonfinite_count') and finite(v) and v > 0})
    invalid = any(finite(r.get('loss_nonfinite_count')) and r['loss_nonfinite_count'] > 0 for r in rows + [failure])
    if invalid or 'loss' in failure and not finite(failure['loss']):
        latest = {**latest, 'status': 'invalid_loss', 'reason': '保存的失败事件或数值计数报告非有限 loss；不能用之前的下降趋势掩盖异常。'}
        status = latest['status']
    return {**base, **latest, 'failure_event': failure, 'label': LABELS[status],
            'terminated_by_exception': terminated_by_exception,
            'reported_nonfinite_fields': nonfinite_fields,
            'needs_discussion': status in ALERTS or bool(failure) or terminated_by_exception or bool(nonfinite_fields),
            'segments': segments[-32:], 'segments_omitted': max(0, len(segments) - 32),
            'scope': '仅判断最近连续训练阶段；阶段切换、拟合重启和 loss 定义变化分别统计。'}


def read_evidence(engine, run_id, trial, *, metric=None, direction=None):
    """Hash exact bytes; bounded reads and path checks are shared with the console."""
    from .experiment_views import _safe_file
    path = _safe_file(engine, run_id, trial, 'artifacts/training_log.json')
    evidence = {'relative_path': 'artifacts/training_log.json', 'sha256': None}
    log = None
    try:
        if path is not None:
            with path.open('rb') as source:
                raw = source.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                evidence['unavailable_reason'] = '训练日志超出 16 MiB 诊断上限。'
            else:
                evidence['sha256'] = hashlib.sha256(raw).hexdigest()
                log = json.loads(raw, parse_constant=lambda _: None,
                    parse_float=lambda text: float(text) if math.isfinite(float(text)) else None)
    except (OSError, ValueError):
        evidence['unavailable_reason'] = '训练日志缺失、写入未完成或无法解析。'
    monitor = {'metric': metric, 'direction': direction} if metric else task_monitor(engine, run_id)
    return {**analyze(log, phase=trial.get('params', {}).get('phase'), **monitor), 'source': evidence}
