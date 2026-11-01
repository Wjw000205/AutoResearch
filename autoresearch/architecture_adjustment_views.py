"""Read-only, bounded projections of approved changes and actual deliveries."""
import json

from .experiment_workflow import sha


def _record(store, run_id, key):
    if not key.startswith(run_id + ':rapid:'):
        raise KeyError('Unknown adjustment')
    paths = {'id':'id','run_id':'run_id','status':'status','created_at':'created_at','completed_at':'completed_at',
        'decision_id':'decision_id','decision':'decision','votes':'votes','check':'check','error':'error',
        'review_purpose':'review_purpose','repair_source':'candidate_repair_source',
        'runtime_files':'candidate.bundle.files','runtime_sha256':'candidate.bundle.sha256',
        'review_files':'candidate.review_bundle.files','control_files':'candidate.control_bundle.files',
        'amendment_sha256':'candidate.amendment_sha256','checks_sha256':'candidate.checks_sha256',
        'check_budget':'candidate.check_budget',
        'post_exit':'post_exit_verification','delivery_status':'codex_work_item.checkpoint.status'}
    expression=','.join("'"+name+"',json_extract(data,'$."+path+"')" for name,path in paths.items())
    with store._read_db() as db:
        row=db.execute("SELECT json_object("+expression+") FROM records WHERE collection='rapid_experiment_revisions' AND key=?",(key,)).fetchone()
    value=json.loads(row[0]) if row else None
    if not value or value['run_id']!=run_id or value['id']!=key:
        raise KeyError('Unknown adjustment')
    return value


def detail(store, run_id, key):
    value=_record(store,run_id,key)
    candidate=value['runtime_files'] or []
    baseline=value['control_files'] or []
    support_baseline=[]
    basis='active_control' if value['control_files'] is not None else 'unavailable'
    ref=value.get('repair_source') or {}
    if ref:
        # Read manifests only: source strings and multi-megabyte patch bodies
        # are never part of the drawer response.
        with store._read_db() as db:
            row=db.execute("SELECT json_extract(data,'$.run_id'),json_extract(data,'$.body.runtime.files'),"
                "json_extract(data,'$.body.support.files') FROM records WHERE collection='candidate_repair_sources' AND key=?",(ref['id'],)).fetchone()
        if row and row[0]==run_id:
            baseline=json.loads(row[1] or '[]');support_baseline=json.loads(row[2] or '[]');basis='previous_candidate'
        else:
            basis='unavailable';baseline=[]
    before={f['path']:f for f in [*baseline,*support_baseline]}
    files=[]
    for row in [*candidate,*[f for f in value['review_files'] or [] if f['path'].startswith('_delivery/')
                             and f['path'] not in {'_delivery/changes.json','_delivery/rationale.txt'}]]:
        name=row['path'];old=before.get(name)
        operation=('unknown' if basis=='unavailable' else 'added' if old is None else
                   'retained' if old['sha256']==row['sha256'] else 'modified')
        kind=('check' if name.endswith('.py') else 'metadata') if name.startswith('_delivery/') else 'runtime'
        files.append({'path':name,'kind':kind,'operation':operation,'sha256':row['sha256'],
                      'previous_sha256':old['sha256'] if old else None,'bytes':row['bytes']})
    purpose=value['review_purpose'] or ('rapid_review:'+value['amendment_sha256'] if value['amendment_sha256'] else None)
    received={v['role']:v for v in value['votes'] or []}
    roles=[]
    if purpose:
        with store._read_db() as db:
            for row in db.execute("SELECT key,json_extract(data,'$.to_role'),json_extract(data,'$.status') "
                "FROM records WHERE collection='handoffs' AND key>=? AND key<? AND json_extract(data,'$.purpose')=?",
                (run_id+':',run_id+';',purpose)):
                role,status=row[1],row[2]
                answer=(store.get('decisions',row[0]) or {}).get('result') if status=='completed' else None
                if answer and all(answer.get(k)==value.get(k) for k in ('amendment_sha256','checks_sha256')) and answer.get('candidate_sha256')==value['runtime_sha256']:
                    received[role]={'role':role,**answer}
                roles.append({'role':role,'status':status})
    states={r['role']:r['status'] for r in roles}
    reviews=[{'role':role,'status':states.get(role,'completed' if role in received else 'not_started'),
        'approve':received.get(role,{}).get('approve'),'rationale':received.get(role,{}).get('rationale'),
        'issues':received.get(role,{}).get('issues',[])} for role in ('director','engineer','scout')]
    program=store.get('programs',run_id) or {}
    post=store.get('rapid_post_exit_verifications',(value['post_exit'] or {}).get('id','')) or {}
    check=value['check']
    if check:
        with store._read_db() as db:
            row=db.execute("SELECT json_extract(data,'$.limits.timeout_seconds'),json_extract(data,'$.duration_seconds') FROM records WHERE collection='experiments' AND key=?",(run_id+':'+check['id'],)).fetchone()
        if row:check={**check,'timeout_seconds':row[0],'duration_seconds':row[1]}
    return {'id':key,'decision_id':value['decision_id'],'created_at':value['created_at'],
        'status':value['status'],'error':value['error'],'changes':(value['decision'] or {}).get('changes',[]),
        'rationale':(value['decision'] or {}).get('rationale'),'hypothesis':(value['decision'] or {}).get('hypothesis'),
        'verification':(value['decision'] or {}).get('verification'),'stop_condition':(value['decision'] or {}).get('stop_condition'),
        'candidate_delivered':bool(candidate),'is_current_active':program.get('rapid_revision_id')==key,
        'historically_activated':value['status']=='activated','comparison_basis':basis,'files':files,
        'file_counts':{kind:{op:sum(f['kind']==kind and f['operation']==op for f in files)
                            for op in ('added','modified','retained','unknown')} for kind in ('runtime','check','metadata')},
        'runtime_sha256':value['runtime_sha256'],'checks_sha256':value['checks_sha256'],
        'reviews':reviews,'check':check,'check_budget':value['check_budget'],
        'post_exit':{k:post.get('result',{}).get(k) for k in ('post_exit_verified','candidate_behavior_passed','error')} if post else None,
        'scientific_approval':False}


def view(store,run):
    from .optimization_loop import current
    cycle=run.get('state',{}).get('research_cycle',1)
    loop=current(store,run['id'],cycle) or {}
    loops=[r for r in store.list('optimization_loops',run['id']) if r.get('cycle')==cycle]
    start=min((r['created_at'] for r in loops if r.get('created_at')),default='')
    attempts={}
    # Several recoveries may archive the same work order at later loop counts.
    # Its first attempt remains its identity; the current loop has its own total.
    for r in loops:
        if r.get('decision_id') and type(r.get('round')) is int and r['round']>0:
            key=r['decision_id']
            attempts[key]=min(attempts.get(key,r['round']),r['round'])
    history=[]
    with store._read_db() as db:
        rows=db.execute("SELECT key,json_extract(data,'$.decision_id'),json_extract(data,'$.status'),"
            "json_extract(data,'$.created_at'),json_extract(data,'$.decision.changes[0].target') "
            "FROM records WHERE collection='rapid_experiment_revisions' AND key>=? AND key<? "
            "AND json_extract(data,'$.cycle')=? AND json_extract(data,'$.created_at')>=? ORDER BY json_extract(data,'$.created_at') DESC LIMIT 100",
            (run['id']+':',run['id']+';',cycle,start)).fetchall()
        history=[{'id':r[0],'attempt':attempts.get(r[1]),'status':r[2],'created_at':r[3],'target':r[4]} for r in rows]
    key=run['id']+':rapid:'+sha(loop['decision_id']) if loop.get('decision_id') else None
    try:
        selected=detail(store,run['id'],key) if key else None
    except KeyError:
        selected=None
    return {'current':selected,'history':history,'current_attempt':attempts.get(loop.get('decision_id')),
            'attempts_total':loop.get('round',0),'history_limit':100}
