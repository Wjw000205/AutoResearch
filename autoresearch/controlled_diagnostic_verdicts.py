"""Preregistered local contrasts; unknown is not a negative finding."""
import math

POINTS = (8, 16, 24, 32)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def branch_evidence(branch):
    curve = {p['step']: p for p in branch['curve']}
    start = curve.get(0, {})
    changes = []
    steps = branch['updates']
    norms = sorted(x['relative_update_norm'] for x in steps if finite(x.get('relative_update_norm')))
    n = len(norms)
    median = (norms[n//2] if n%2 else (norms[n//2-1]+norms[n//2])/2) if n else None
    spikes = [x['step'] for x in steps if finite(x.get('relative_update_norm')) and median is not None
              and x['relative_update_norm'] > 5*median]
    for step in POINTS:
        point = curve.get(step, {})
        delta = {}
        for metric in ('train_mse', 'validation_mse'):
            a, b = start.get(metric), point.get(metric)
            delta[metric] = (b-a)/a if finite(a) and a > 0 and finite(b) else None
        valid=all(finite(start.get(k)) and start[k]>0 and finite(point.get(k)) for k in delta)
        changes.append({'step':step, 'relative_change_from_step0':delta,
                        'tradeoff_thresholds_met':valid and point['train_mse']<start['train_mse']*.999 and point['validation_mse']>start['validation_mse']*1.01,
                        'both_rise_thresholds_met':valid and all(point[k]>start[k]*1.05 for k in delta),
                        'update_spikes_since_previous_evaluation':[s for s in spikes if step-8 < s <= step]})
    control = branch.get('control_curve', [])
    control_complete = {p['step'] for p in control} == {0,*POINTS} and len(control)==5
    control_stable = control_complete and all(p.get('weights_unchanged') is True and
        all(finite(p.get(k)) and finite(start.get(k)) and abs(p[k]-start[k])<=1e-12
            for k in ('train_mse','validation_mse')) for p in control)
    completed = (len(steps)==32 and [x['step'] for x in steps]==list(range(1,33))
        and all(x.get('update_completed') is True and not x['nonfinite'] for x in steps)
        and set(curve)=={0,*POINTS} and len(branch['curve'])==5 and control_stable)
    nonfinite = [x for x in steps if x['nonfinite']]
    return {'complete':completed,'attempted_steps':len(steps),
        'completed_updates':sum(x.get('update_completed',False) for x in steps),
        'missing_evaluation_steps':[s for s in POINTS if s not in curve],
        'control_complete':control_complete,'control_stable':control_stable,
        'median_relative_update_norm':median,'update_spike_steps':spikes,
        'changes':changes,'nonfinite_events':nonfinite}


def classify(trials):
    """Numerical diagnostics use strict inequalities and never select parameters."""
    evidence=[]
    for trial in trials:
        branches={b['lr_factor']:branch_evidence(b) for b in trial['branches']}
        overfit=[]
        for factor,branch in branches.items():
            hits=[p['step'] for p in branch['changes'] if p['tradeoff_thresholds_met']]
            overfit.append(branch['complete'] and len(hits)>=3 and not branch['update_spike_steps'])
            branch['generalization_tradeoff_steps']=hits
        original,lower=branches.get(1.,{}),branches.get(.25,{})
        matched=[]
        for a,b in zip(original.get('changes',[]),lower.get('changes',[])):
            da,db=a['relative_change_from_step0'],b['relative_change_from_step0']
            eligible=a['both_rise_thresholds_met'] and all(finite(db[k]) for k in da)
            ratios={k:db[k]/da[k] for k in da} if eligible else None
            if eligible and a['update_spikes_since_previous_evaluation'] and all(v<=.5 for v in ratios.values()):
                matched.append({'step':a['step'],'lower_lr_to_original_rise_ratios':ratios,
                                'spike_steps':a['update_spikes_since_previous_evaluation']})
        old_bad=original.get('nonfinite_events',[]);new_bad=lower.get('nonfinite_events',[])
        evidence.append({'seed':trial['seed'],'branches':branches,
            'generalization_tradeoff':set(branches)=={1.,.25} and all(overfit),
            'matched_instability_points':matched,
            'finite_instability':original.get('complete') and lower.get('complete') and len(matched)>=2,
            'original_nonfinite_failure':old_bad[0].get('failure') if (old_bad and not new_bad and lower.get('complete')
                and original.get('control_complete') and original.get('control_stable')
                and old_bad[0].get('failure_stage')=='after_optimizer_step'
                and old_bad[0].get('pre_update_finite') is True and old_bad[0].get('update_completed') is True) else None})
    seeds_ok=len(trials)==2 and {t['seed'] for t in trials}=={17,29}
    complete=seeds_ok and all(set(e['branches'])=={1.,.25} and all(b['complete'] for b in e['branches'].values()) for e in evidence)
    checkpoint_ok=seeds_ok and all(t['checkpoint']['matches_recorded_final_model'] and t['checkpoint']['metric_matches'] for t in trials)
    failures=[e['original_nonfinite_failure'] for e in evidence]
    replicated_nonfinite=seeds_ok and all(failures) and len(set(failures))==1
    instability=seeds_ok and (all(e['finite_instability'] for e in evidence) or replicated_nonfinite)
    overfit=complete and all(e['generalization_tradeoff'] for e in evidence)
    return {'complete_counterfactual':bool(complete),'checkpoint_restoration_consistent':bool(checkpoint_ok),
        'verdicts':{'checkpoint':'not_supported_in_this_check' if checkpoint_ok else 'inconclusive',
            'overfitting':'supported_locally' if overfit else 'inconclusive',
            'update_instability':'supported_locally' if instability else 'inconclusive'},
        'replicated_nonfinite_across_seeds':bool(replicated_nonfinite),'per_seed_criteria':evidence,
        'scope':'Heuristic local contrasts only. No parameter selection or global causal conclusion; absent support is not disproof.'}
