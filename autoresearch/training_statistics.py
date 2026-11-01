"""Descriptive, bounded statistics from saved training observations only."""
from statistics import median
from .training_convergence import finite, _relative


def coverage(rows, field, stats):
    sampled = field in {'parameter_norm', 'update_norm'} and any('update_sampled' in r for r in rows)
    applicable = [r for r in rows if r.get('update_sampled') is True] if sampled else rows
    categorical = field == 'loss_scale'
    values = [None if categorical and isinstance(r.get(field), str) else r.get(field) for r in applicable]
    result = stats(values)
    labels = sum(isinstance(r.get(field), str) and bool(r[field]) for r in applicable) if categorical else 0
    nonfinite = sum(r.get(field + '_nonfinite_count', 0) for r in rows
                    if finite(r.get(field + '_nonfinite_count')) and r[field + '_nonfinite_count'] > 0)
    unknown_sampling = sum(type(r.get('update_sampled')) is not bool for r in rows) if sampled else 0
    result.update(expected_count=len(applicable), missing_count=sum(field not in r or r[field] is None for r in applicable),
        unsampled_count=len(rows)-len(applicable)-unknown_sampling, sampling_flag_missing_count=unknown_sampling,
        categorical_count=labels, reported_nonfinite_count=nonfinite,
        coverage_complete=bool(applicable) and result['count'] + labels == len(applicable)
                          and not nonfinite and not result['invalid_count'] and not unknown_sampling)
    return result


def validation_points(rows, validation):
    """Retain observed coordinates only; callers already select one validation fit."""
    return [{'step': r.get('step', r.get('epoch')),
             'epoch': r.get('epoch') if finite(r.get('epoch')) else None, 'value': v}
            for r, v in zip(rows, validation) if finite(v)]


def alignment(rows, epochs, validation, direction):
    complete = [e for e in epochs if e['epoch_complete'] is True and finite(e['loss'])]
    values = [e['loss'] for e in complete]
    trend = {'complete_epochs': len(values), 'decreasing_transitions': sum(b < a for a,b in zip(values,values[1:])),
        'window': min(5, len(values)//2), 'recent_relative_improvement': None}
    w = trend['window']
    if w:
        trend['recent_relative_improvement'] = _relative(median(values[-2*w:-w]), median(values[-w:]))
    points = validation_points(rows, validation)
    curve = {'count': len(points), 'best': None, 'last': points[-1] if points else None,
        'last_relative_degradation_from_best': None, 'training_after_best': None,
        'signal': 'insufficient_evidence', 'root_cause_verified': False}
    if points and direction in {'minimize','maximize'}:
        best = (min if direction == 'minimize' else max)(points, key=lambda p:p['value'])
        curve['best'] = best
        curve['last_relative_degradation_from_best'] = _relative(best['value'], points[-1]['value']) * (-1 if direction=='minimize' else 1)
        after = [e for e in complete if finite(best['epoch']) and e['epoch'] >= best['epoch']]
        if len(after) >= 2:
            curve['training_after_best'] = {'first_epoch': after[0]['epoch'], 'last_epoch': after[-1]['epoch'],
                'first_loss': after[0]['loss'], 'last_loss': after[-1]['loss'],
                'relative_improvement': _relative(after[0]['loss'],after[-1]['loss'])}
            if after[-1]['loss'] < after[0]['loss'] and curve['last_relative_degradation_from_best'] > 0:
                curve['signal'] = 'training_down_validation_worse_than_best'
    curve['scope'] = 'Descriptive same-fit validation alignment, not proof of overfitting or a causal defect.'
    return trend, curve


def critical_packet(evidence, trial):
    """One bounded essential report; full details/history remain SHA-addressable."""
    active = evidence['training_investigation']
    obs = active.get('observations', {}).get(trial) or {}
    diag = evidence['diagnostics'][trial]
    fields = ('count','expected_count','missing_count','unsampled_count','categorical_count',
              'invalid_count','reported_nonfinite_count','minimum','maximum','recent_median','coverage_complete')
    packet = {'version': 2, 'trial_id': trial, 'source': obs.get('source', diag.get('source')),
        'available': obs.get('available', False), 'unavailable_reason': obs.get('reason'),
        'batch': {k: obs.get('batch_trend', diag).get(k) for k in ('points','status','recent_relative_improvement')},
        'batch_observations': obs.get('batch_observations'),
        'complete_epoch_trend': obs.get('epoch_descriptive_trend'),
        'validation_alignment': obs.get('validation_alignment'),
        'checkpoint_alignment': obs.get('checkpoint_alignment'), 'host_binding_checks': obs.get('host_binding_checks'),
        'runtime': obs.get('runtime'), 'failure_event': obs.get('failure_event'),
        'telemetry': {k: {f: v[f] for f in fields if f in v} for k,v in obs.get('telemetry',{}).items()},
        'missing_telemetry': obs.get('missing_telemetry'), 'partial_telemetry': obs.get('partial_telemetry'),
        'loss_scale_labels': obs.get('loss_scale_labels'),
        'module_telemetry_count': len(obs.get('module_telemetry',{})),
        'full_observations_path': '/training_investigation/observations/' + trial.replace('~','~0').replace('/','~1'),
        'followup_path': '/training_investigation/followup', 'root_cause_verified': False,
        'scope': 'Saved validation training only. Missing module statistics remain unknown. No rerun or scientific approval.'}
    # Old frozen packets and their read-receipt hashes must remain unchanged.
    if 'validation_trajectory' in obs:
        packet['validation_trajectory_path'] = packet['full_observations_path'] + '/validation_trajectory'
    if 'epoch_trajectory' in obs:
        packet['epoch_trajectory_path'] = packet['full_observations_path'] + '/epoch_trajectory'
    return packet
