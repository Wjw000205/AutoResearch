"""Pure fixed-state directional measurements; no optimizer or training steps."""
from __future__ import annotations

import hashlib
import json
import math


class UnresolvedDirection(ValueError):
    """Missing or nonfinite directional evidence; no affirmative conclusion."""


def state_hash(value):
    import torch
    def pack(x):
        if isinstance(x, torch.Tensor):
            a=x.detach().cpu().contiguous().numpy()
            return {'dtype':str(a.dtype),'shape':list(a.shape),'sha256':hashlib.sha256(a.tobytes()).hexdigest()}
        if isinstance(x,dict):return {str(k):pack(v) for k,v in x.items()}
        if isinstance(x,(list,tuple)):return [pack(v) for v in x]
        return x
    return hashlib.sha256(json.dumps(pack(value),sort_keys=True,allow_nan=False).encode()).hexdigest()


def history_direction(named_parameters, saved):
    """Reconstruct the stored Adam moment direction, NOT the next Adam update."""
    import torch
    parameters=list(named_parameters)
    groups=saved['param_groups']
    if len(groups)!=1:raise ValueError('Only the original single Adam group is supported')
    group=groups[0]
    if (group.get('amsgrad',False) or group.get('maximize',False)
            or group.get('weight_decay',0)!=0 or group.get('decoupled_weight_decay',False)):
        raise ValueError('Optimizer mode differs from the reviewed Adam protocol')
    ids=group['params']
    if len(ids)!=len(parameters) or len(set(ids))!=len(ids):raise ValueError('Parameter mapping changed')
    beta1,beta2=group['betas'];pieces=[]
    for key,(name,param) in zip(ids,parameters):
        state=saved['state'].get(key)
        if not state:raise UnresolvedDirection('Missing Adam state: '+name)
        if not all(k in state for k in ('step','exp_avg','exp_avg_sq')):
            raise UnresolvedDirection('Missing Adam moment or counter: '+name)
        step=float(state['step'])
        if not math.isfinite(step) or step<1 or int(step)!=step:raise UnresolvedDirection('Invalid Adam counter')
        m=state['exp_avg'].detach().to(dtype=torch.float64,device='cpu')
        v=state['exp_avg_sq'].detach().to(dtype=torch.float64,device='cpu')
        if (m.shape!=param.shape or v.shape!=param.shape or not torch.isfinite(m).all()
                or not torch.isfinite(v).all() or (v<0).any()):raise UnresolvedDirection('Invalid Adam moments')
        d=-float(group['lr'])*(m/(1-beta1**step))/((v/(1-beta2**step)).sqrt()+float(group['eps']))
        pieces.append(d.reshape(-1))
    result=torch.cat(pieces)
    if not torch.isfinite(result).all():raise UnresolvedDirection('Nonfinite historical direction')
    return result


def full_gradient(model,data,split,batch_size,guard):
    """Full physical-scale MSE gradient; weight windows, not batch means."""
    import numpy as np
    import torch
    if split not in ('train','validation'):raise ValueError('Held-out split is forbidden')
    origins=data.origins(split)
    if len(origins)==0:raise ValueError('No gradient observations')
    params=list(model.parameters())
    accum=[torch.zeros_like(p,dtype=torch.float64,device='cpu') for p in params]
    total=0.;seen=0
    was_training=model.training;model.eval()
    try:
        for start in range(0,len(origins),batch_size):
            guard.check('gradient_'+split)
            indices=origins[start:start+batch_size]
            x,y=data.batch(indices)
            for p in params:p.grad=None
            pred=model(torch.from_numpy(x)).to(torch.float64)
            pred=pred*torch.as_tensor(data.std,dtype=torch.float64)+torch.as_tensor(data.mean,dtype=torch.float64)
            target=torch.from_numpy(np.asarray(y,dtype=np.float64))
            loss=((pred-target)**2).mean()*(len(indices)/len(origins))
            if not torch.isfinite(loss):raise ValueError('Nonfinite objective')
            loss.backward()
            total+=float(loss.detach());seen+=len(indices)
            for dest,p in zip(accum,params):
                if p.grad is not None:
                    if not torch.isfinite(p.grad).all():raise UnresolvedDirection('Nonfinite gradient')
                    dest.add_(p.grad.detach().to(dtype=torch.float64,device='cpu'))
    finally:
        for p in params:p.grad=None
        model.train(was_training)
    if seen!=len(origins):raise ValueError('Incomplete gradient coverage')
    return torch.cat([x.reshape(-1) for x in accum]),total,seen


def cosine(a,b):
    import torch
    denom=float(torch.linalg.vector_norm(a)*torch.linalg.vector_norm(b))
    return float(torch.dot(a,b))/denom if denom>0 and math.isfinite(denom) else None


def signature(base,repeat,gradients,directions,points,rho):
    """All thresholds are from the frozen protocol; no adaptive radius or ranking."""
    import torch
    splits=('train_mse','validation_mse')
    tau={k:1e-6*max(abs(base[k]),1.)+10*abs(repeat[k]-base[k]) for k in splits}
    checks={}
    for name,direction in directions.items():
        checks[name]={}
        for split,gradient in zip(splits,gradients):
            derivative=float(torch.dot(gradient,direction))
            rows=[]
            for scale in (.5,1.):
                plus=points[name][str(scale)][split];minus=points[name][str(-scale)][split]
                actual=(plus-minus)/2;predicted=scale*rho*derivative
                tolerance=.05*abs(predicted)+tau[split]
                rows.append({'scale':scale,'actual_central_difference':actual,'predicted_difference':predicted,
                    'error':abs(actual-predicted),'tolerance':tolerance,
                    'central_difference_passed':abs(actual-predicted)<=tolerance,
                    'positive_delta':plus-base[split],
                    'positive_ascent':plus-base[split]>tau[split],
                    'positive_descent':plus-base[split]<-tau[split]})
            checks[name][split]=rows
    def allcheck(direction,split,condition):return all(row[condition] for row in checks[direction][split])
    numerical=all(allcheck(d,s,'central_difference_passed') for d in checks for s in splits)
    cos_tv=cosine(*gradients);cos_ta=cosine(gradients[0],directions['adam_history'])
    conflict=(cos_tv is not None and cos_tv<=-.1
        and allcheck('train_descent','train_mse','positive_descent')
        and allcheck('train_descent','validation_mse','positive_ascent')
        and all(allcheck('train_descent',s,'central_difference_passed') for s in splits))
    mismatch=(cos_ta is not None and cos_ta>=.1 and numerical
        and all(allcheck('adam_history',s,'positive_ascent') and allcheck('train_descent',s,'positive_descent') for s in splits))
    return {'cos_train_validation':cos_tv,'cos_train_adam_history':cos_ta,'tau':tau,'checks':checks,
        'all_directions_numerically_resolved':numerical,
        'local_objective_conflict':bool(conflict),'local_adam_direction_misalignment':bool(mismatch),
        'label':'mixed' if conflict and mismatch else 'local_objective_conflict' if conflict else
                'local_adam_direction_misalignment' if mismatch else 'inconclusive',
        'historical_root_cause':'unknown','scientific_approval':False}
