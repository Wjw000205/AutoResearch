"""Operator iteration bounds, separate from frozen scientific configuration."""
import copy

from .experiment_workflow import sha
from .storage import now

POLICIES = 'research_iteration_policies'
RECOVERIES = 'research_iteration_recoveries'


def current(store, run_id):
    row = store.get(POLICIES,run_id)
    if row and (row.get('run_id')!=run_id or sha(row.get('body'))!=row.get('sha256')):
        raise ValueError('Research iteration policy changed')
    return row


def limit(store, run_id):
    row=current(store,run_id)
    value=row['body']['limit'] if row else (store.get('runs',run_id) or {}).get('config',{}).get('max_research_cycles',3)
    if value is not None and (type(value) is not int or value<1):
        raise ValueError('Research iteration limit must be null or a positive integer')
    return value


def allows(store, run_id, used):
    maximum=limit(store,run_id)
    return maximum is None or used<maximum


def effective_config(store, run_id, config):
    return {**config,'max_research_cycles':limit(store,run_id)}


def view(store, run_id):
    row=current(store,run_id);maximum=limit(store,run_id)
    return {'limit':maximum,'unlimited':maximum is None,'source':'operator_policy' if row else 'task_config',
            'scope':'all_task_datasets','policy_sha256':row['sha256'] if row else None}


def set_limit(store, run_id, maximum, instruction):
    run=store.get('runs',run_id)
    if not run or not str(instruction).strip() or maximum is not None and (type(maximum) is not int or maximum<1):
        raise ValueError('A valid task, explicit instruction and null/positive limit are required')
    if run['status']=='running':
        raise ValueError('Stop the task at its saved boundary before adjusting iteration policy')
    old=current(store,run_id)
    body={'run_id':run_id,'limit':maximum,'instruction':instruction,'scope':'all_task_datasets',
          'scientific_approval':False,'preserve_completed_experiments':True}
    if old and old['body']['limit']==maximum:return old
    if old:store.put(POLICIES,run_id+':history:'+old['sha256'],{**old,'archived':True})
    row={'id':run_id,'run_id':run_id,'body':body,'sha256':sha(body),'created_at':now()}
    store.put(POLICIES,run_id,row)
    store.event(run_id,'research.iteration_limit_updated',
        '科研迭代次数不设上限' if maximum is None else f'科研迭代次数上限设为 {maximum}',
        {'maximum':maximum,'policy_sha256':row['sha256'],'scientific_approval':False})
    return row


def recover(engine, run_id):
    """Reopen only a limit-blocked, already approved adjustment; no model call."""
    from .experiment_decisions import revision_context, record_revision
    from .dataset_optimization import revision_allowed
    policy=current(engine.store,run_id)
    if not policy:return None
    cycle=engine.cycle(run_id)
    review=engine.store.get('research_cycles',f'{run_id}:{cycle}') or {}
    key=f'{run_id}:{cycle}:iteration-limit:'+sha([policy['sha256'],review.get('experiment_decision_id')])
    saved=engine.store.get(RECOVERIES,key)
    if saved and sha(saved.get('body'))!=saved.get('sha256'):
        raise ValueError('Iteration recovery archive changed')
    if saved and saved.get('status')=='applied':return key,saved['next_node']
    if not saved and not (review.get('status')=='blocked' and review.get('decision')=='blocked'
            and review.get('review_type')=='experiment_decision'
            and review.get('failure',{}).get('kind')=='experiment_decision_revision'):
        return None
    if not revision_allowed(engine,run_id):return None
    if saved:
        if sha(saved['body'])!=saved['sha256']:raise ValueError('Iteration recovery archive changed')
        review=saved['body']['review'];step=saved['body']['step']
    else:
        step=engine.store.get('steps',run_id+':'+review['failure_stage'])
        if not step or step.get('output')!=review or step.get('next_node') is not None:
            raise ValueError('Iteration recovery requires the exact saved blocked stage')
        body={'review':copy.deepcopy(review),'step':copy.deepcopy(step),'policy_sha256':policy['sha256'],'cycle':cycle}
        saved={'id':key,'run_id':run_id,'body':body,'sha256':sha(body),'status':'prepared','created_at':now()}
        engine.store.put(RECOVERIES,key,saved)
    engine.check_active(run_id)
    revision_context(engine,run_id,review)
    loop=engine.store.get('optimization_loops',f'{run_id}:{cycle}') or {}
    if loop.get('decision_id')==review.get('experiment_decision_id') and loop.get('status')=='architecture_pending':
        next_node='architecture_tuning'
    else:
        result=record_revision(engine,run_id,review['failure_stage'],step,review['failure'])
        next_node=result['next_node']
    saved.update(status='applied',next_node=next_node,updated_at=now())
    engine.store.put(RECOVERIES,key,saved)
    engine.store.event(run_id,'research.iteration_limit_recovered',
        '已复用三方批准的调整，恢复修订流程；原阻塞记录和实验结果保留。',
        {'recovery_id':key,'decision_id':review['experiment_decision_id'],'next_node':next_node})
    return key,next_node


def graph_entry(engine, run_id):
    saved=recover(engine,run_id)
    if saved:return saved[0]+':graph',saved[1]
    previous=[r for r in engine.store.list(RECOVERIES,run_id) if r.get('status')=='applied'
              and r.get('body',{}).get('cycle')==engine.cycle(run_id)]
    if not previous:return None
    row=max(previous,key=lambda r:r['created_at'])
    if sha(row.get('body'))!=row.get('sha256'):raise ValueError('Iteration recovery archive changed')
    return row['id']+':graph',row['next_node']
