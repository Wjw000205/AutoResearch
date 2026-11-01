"""An explicit local architecture stage inside validation-driven optimization."""
from .storage import now
from .experiment_workflow import sha

COLLECTION='optimization_loops'
STAGE='architecture_tuning'


def current(store,run_id,cycle):
    return store.get(COLLECTION,f'{run_id}:{cycle}')


def begin(engine,run_id,review,step):
    from .experiment_decisions import revision_context
    from .training_investigation import scoped_repair
    decision=revision_context(engine,run_id,review)
    if not decision or decision['proposal']['action']!='revise_architecture' or scoped_repair(decision['proposal']):
        raise ValueError('Architecture tuning requires an approved architecture change, not log repair')
    cycle=engine.cycle(run_id);old=current(engine.store,run_id,cycle)
    if old and old.get('decision_id')==decision['decision_id']:
        return {'cycle':cycle,'next_node':STAGE}
    key=f'{run_id}:{cycle}'
    record={'id':key,'run_id':run_id,'cycle':cycle,'round':(old or {}).get('round',0)+1,
        'status':'architecture_pending','decision_id':decision['decision_id'],'review':review,
        'created_at':now(),'scientific_approval':False,'stable':False}
    puts=[(COLLECTION,key,record)]
    if old:puts.append((COLLECTION,key+':history:'+sha(old),{**old,'archived':True}))
    previous=engine.store.get('steps',run_id+':'+STAGE)
    if previous:puts.append(('cycle_steps',key+':architecture:'+sha(previous),previous))
    puts.append(('steps',step['id'],{**step,'status':'running','completed_at':None,
        'output':{'activity':'optimization_loop','tuning_pending':True,'architecture_decision_id':decision['decision_id']},
        'next_node':STAGE}))
    engine.store.batch(puts,deletes=[('steps',run_id+':'+STAGE)])
    engine.store.event(run_id,'optimization.architecture_requested','三方批准局部架构微调，进入实现、复审和小批验证',
        {'decision_id':decision['decision_id'],'round':record['round']})
    return {'cycle':cycle,'next_node':STAGE}


async def node(engine,harness,state):
    """A real graph node; numerical work still uses the existing execution gates."""
    from .engine import ResearchBlocked
    run_id=state['run_id'];cycle=engine.cycle(run_id)
    engine.check_active(run_id)
    record=current(engine.store,run_id,cycle)
    from .dataset_optimization import enabled
    if enabled(engine.store, run_id) and (not record or record.get('status') != 'architecture_pending'):
        # A real evidence review before tuning, reusing the same three-agent
        # decision/read gates. No architecture certificate is fabricated here.
        previous = engine.store.get('steps', run_id + ':' + STAGE)
        if previous and previous.get('status') == 'completed':
            engine.store.batch([('cycle_steps', run_id+':architecture-history:'+sha(previous), previous)],
                               deletes=[('steps', run_id+':'+STAGE)])
        return await engine.graph_node(STAGE, state, harness)
    previous=engine.store.get('steps',run_id+':'+STAGE) or {}
    if (previous.get('status')=='completed' and previous.get('cycle')==cycle
            and engine.store.get('runs',run_id)['stage']==STAGE):
        return {'cycle':cycle,'next_node':previous['next_node']}
    from .orchestration import STAGES
    engine.update_run(run_id,stage=STAGE,progress=round(STAGES.index(STAGE)/len(STAGES)*100))
    step={'id':run_id+':'+STAGE,'run_id':run_id,'cycle':cycle,'stage':STAGE,
          'status':'running','started_at':previous.get('started_at',now()),'output':None}
    engine.store.put('steps',step['id'],step)
    engine.store.put('graph_status',run_id,{'id':run_id,'run_id':run_id,'node':STAGE,'cycle':cycle,'updated_at':now()})
    engine.store.event(run_id,'stage.started',STAGE,{'cycle':cycle,'framework':'langgraph'})
    if record and record['status']=='architecture_pending':
        from .rapid_experiment_revision import run
        result=await run(engine,harness,run_id,record['review'])
        engine.check_active(run_id)
        next_node=result['next_node']
        if next_node not in {'experiments','tuning'}:raise ResearchBlocked('Unexpected architecture tuning route')
        record.update(status='validating' if next_node=='experiments' else 'adjustment_rejected',
            result=result,updated_at=now(),stable=False)
        engine.store.put(COLLECTION,record['id'],record)
        output={**result,'optimization_round':record['round'],'stable':False,
                'next_action':'验证新版本' if next_node=='experiments' else '依据具体异议重新决策'}
    else:
        # A completed tuning pass may proceed only with complete measured evidence.
        from .experiment_workflow import require_complete
        from .experiment_decisions import enabled,session,_assert_binding,_approved,snapshot,_binding
        await engine._read_consensus_check(run_id,require_complete,engine,run_id)
        if enabled(engine,run_id):
            active=session(engine,run_id)
            _assert_binding(engine,run_id,active)
            decision=engine.store.get('experiment_decisions',(active or {}).get('last_decision','')) or {}
            if not _approved(decision) or decision['proposal']['action']!='review':
                raise ResearchBlocked('架构微调尚无三方一致的结束调整意见，不能进入验证评审。')
            from .comparison_transport import runtime_transport
            basis,_=_binding(engine,run_id)
            program=basis['program']
            transport=None if program.get('dataset_runtime_id') else runtime_transport(engine,run_id,program=program)
            evidence=await engine._read_consensus_check(run_id,snapshot,engine,run_id,transport)
            if sha(evidence)!=decision['evidence']['sha256']:
                raise ResearchBlocked('结束调整意见所依据的验证证据已变化，需重新决策。')
        next_node='validation_review'
        output={'status':'ready_for_review','stable':False,'scientific_approval':False,
            'reason':('当前验证批次齐备，三方同意结束本轮调整并进入独立结果评审；不代表科学验收通过。'
                      if enabled(engine,run_id) else '演示数据批次齐备，进入模拟评审；不代表真实研究结果。')}
        if record:
            record.update(status='ready_for_review',updated_at=now());engine.store.put(COLLECTION,record['id'],record)
    step.update(status='completed',completed_at=now(),output=output,next_node=next_node)
    engine.store.put('steps',step['id'],step)
    engine.store.event(run_id,'stage.completed',STAGE,{'cycle':cycle,'next_node':next_node,'scientific_approval':False})
    return {'cycle':cycle,'next_node':next_node}


def progress(store, run, record):
    """Attempts are proposals; count deliveries, activations and real runs separately."""
    cycle=run.get('state',{}).get('research_cycle',1)
    loops=[r for r in store.list(COLLECTION,run['id']) if r.get('cycle')==cycle]
    start=min((r['created_at'] for r in loops if r.get('created_at')),default=None)
    result={'attempts':record.get('round',0),'since':start,'activated_versions':0,
            'code_deliveries':0,'successful_validation_runs':0,'failed_validation_runs':0,
            'passed_code_checks':0,'failed_code_checks':0,
            'current_program_revision':(store.get('programs',run['id']) or {}).get('revision'),
            'current_candidate_status':None,'scientific_approval':False}
    if not start:
        return result
    with store._read_db() as db:
        rows=db.execute("SELECT json_extract(data,'$.status'),coalesce(json_extract(data,'$.codex_work_item.continuations[#-1].checkpoint.status'),json_extract(data,'$.codex_work_item.checkpoint.status')) "
            "FROM records WHERE collection='rapid_experiment_revisions' AND key>=? AND key<? "
            "AND json_extract(data,'$.cycle')=? AND json_extract(data,'$.created_at')>=?",
            (run['id']+':',run['id']+';',cycle,start)).fetchall()
        for status,delivery in rows:
            result['activated_versions'] += status=='activated'
            result['code_deliveries'] += delivery=='completed'
        rows=db.execute("SELECT json_extract(data,'$.params.phase'),json_extract(data,'$.status'),count(*) "
            "FROM records WHERE collection='experiments' AND key>=? AND key<? "
            "AND json_extract(data,'$.started_at')>=? GROUP BY 1,2",(run['id']+':',run['id']+';',start)).fetchall()
        for phase,status,count in rows:
            name={('validation','completed'):'successful_validation_runs',('validation','failed'):'failed_validation_runs',
                  ('code_check','completed'):'passed_code_checks',('code_check','failed'):'failed_code_checks'}.get((phase,status))
            if name:result[name]=count
        if record.get('decision_id'):
            key=run['id']+':rapid:'+sha(record['decision_id'])
            row=db.execute("SELECT json_extract(data,'$.status') FROM records WHERE collection='rapid_experiment_revisions' AND key=?",(key,)).fetchone()
            result['current_candidate_status']=row[0] if row else None
    return result


def view(store,run):
    cycle=run.get('state',{}).get('research_cycle',1)
    record=current(store,run['id'],cycle) or {}
    from .dataset_optimization import enabled, COLLECTION as CERTIFICATES
    if enabled(store, run['id']):
        from .python_training import code_hash
        program = store.get('programs', run['id']) or {}
        plan = store.get('experiment_plans', run['id'])
        accepted = [r for r in store.list(CERTIFICATES, run['id'])
                    if r.get('body', {}).get('binding', {}).get('cycle') == cycle
                    and (not program or (
                        r['body']['binding'].get('code_sha256') == code_hash(program)
                        and r['body']['binding'].get('revision') == program.get('revision', 0)))
                    and (not plan or r['body']['binding'].get('plan_sha256') == sha(plan))]
        from .dataset_optimization import certificate
        valid = []
        for r in accepted:
            kind = next((k for k in ('accept_architecture', 'complete_optimization') if ':'+k+':' in r['id']), None)
            if kind:
                try:
                    certificate(store, run['id'], r['body']['binding'], kind)
                    valid.append(kind)
                except (ValueError, KeyError, TypeError):
                    pass  # A stale display claim must never invent approval.
        stable = 'complete_optimization' in valid
        architecture = 'accept_architecture' in valid
        return {'round': max(1, record.get('round', 0)), 'progress':progress(store,run,record), 'status': 'stable' if stable else (
                'parameters' if architecture else 'architecture_assessment'),
                'stage': run['stage'], 'stable': stable, 'scientific_approval': False,
                'sequence': ['experiments', STAGE, 'experiments', 'tuning', 'validation_review'],
                'completion_rule': '先验证架构，必要时调整并复验；架构通过后调参。三方核验参数稳定后才切换数据集；首数据集模型源码冻结复用，覆盖完成或停止均不等于稳定。',
                'latest_architecture_result': record.get('result')}
    status=record.get('status','validation_and_parameters')
    step=store.get('steps',run['id']+':'+STAGE) or {}
    if (step.get('cycle')==cycle and step.get('status')=='completed'
            and (step.get('output') or {}).get('status')=='ready_for_review'):
        status='ready_for_review'
    return {'round':max(1,record.get('round',0)+(status=='validating')),'progress':progress(store,run,record),'status':status,
        'stage':run['stage'],'stable':False,'scientific_approval':False,
        'sequence':['experiments','tuning',STAGE,'experiments'],
        'completion_rule':'验证批次齐备、三方一致确认无需继续局部调整后进入验证评审；停止或预算耗尽不等于稳定。',
        'latest_architecture_result':record.get('result')}
