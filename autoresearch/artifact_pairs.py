"""Supply both sides of a requested ablation comparison from existing records."""
import copy

from .experiment_workflow import sha

FIELDS = ('shape', 'sha256', 'seed128', 'words', 'start_state', 'end_state')


def dependencies(evidence, selected):
    available = [r for r in evidence['records'] if r.get('status') == 'completed'
                 and not r.get('superseded') and r.get('params', {}).get('phase') == 'validation']
    selected_ids = {r['id'] for r in selected}
    dependencies, pairs = {}, []
    for row in selected:
        p = row['params']
        if p.get('variant') != 'ablation':
            continue
        matches = []
        for control in available:
            q = control['params']
            if (q.get('variant') != 'proposed' or p.get('seed') is None
                    or any(sha(p.get(k)) != sha(q.get(k)) for k in ('seed','dataset','benchmark_task_id'))
                    or any(not row.get(k) or row.get(k) != control.get(k)
                           for k in ('code_sha256','experiment_plan_sha256'))):
                continue
            left, right = row.get('evaluation') or {}, control.get('evaluation') or {}
            if (not left.get('dataset_id') or left.get('dataset_id') != right.get('dataset_id')
                    or any(not left.get('split_fingerprints', {}).get(k)
                           or left['split_fingerprints'][k] != right.get('split_fingerprints', {}).get(k)
                           for k in ('train','validation'))):
                continue
            matches.append(control)
        pair = {'trial_id':row['id'],'seed':p['seed'],'candidate_control_ids':[r['id'] for r in matches],
                'status':'bound' if len(matches)==1 else 'ambiguous' if matches else 'missing'}
        if len(matches)==1:
            pair['control_trial_id']=matches[0]['id']
            if matches[0]['id'] not in selected_ids:
                dependencies[matches[0]['id']]=matches[0]
        pairs.append(pair)
    return [*selected,*[dependencies[k] for k in sorted(dependencies)]],pairs


def compare(material, pair):
    if pair['status'] != 'bound':
        return {**pair,'initialization_equal':None,'reason':'No unique current same-seed control; do not infer a match.'}
    by_id={t['summary']['trial_id']:(i,t) for i,t in enumerate(material['trials'])}
    left_index,left=by_id[pair['trial_id']];right_index,right=by_id[pair['control_trial_id']]
    operands=[x.get('model_provenance',{}).get('data') for x in (left,right)]
    if any(not isinstance(x,dict) or not isinstance(x.get('initialization'),list) for x in operands):
        return {**pair,'initialization_equal':None,'reason':'Saved initialization artifact unavailable'}
    lists=[x['initialization'] for x in operands]
    maps=[{v.get('name'):(i,v) for i,v in enumerate(items) if isinstance(v,dict) and isinstance(v.get('name'),str)} for items in lists]
    names=sorted(set(maps[0])&set(maps[1]))
    unique=all(len(m)==len(items) for m,items in zip(maps,lists))
    declarations=[x.get('common_initialization_names') for x in operands]
    declared_match=all(isinstance(x,list) and sorted(x)==names for x in declarations)
    rows=[]
    for name in names:
        (li,a),(ri,b)=maps[0][name],maps[1][name]
        missing=[k for k in FIELDS if a.get(k) is None or b.get(k) is None]
        different={k:{'trial':copy.deepcopy(a[k]),'control':copy.deepcopy(b[k])}
                   for k in FIELDS if k in a and k in b and sha(a[k])!=sha(b[k])}
        rows.append({'name':name,'equal':not missing and not different,'missing_fields':missing,
            'differences':different,'trial_index':li,'control_index':ri})
    complete=bool(names) and unique and all(isinstance(x,list) for x in declarations) and not any(r['missing_fields'] for r in rows)
    return {**pair,'trial_path':f'/trials/{left_index}','control_path':f'/trials/{right_index}',
        'artifact_sha256s':[t['model_provenance']['sha256'] for t in (left,right)],
        'common_parameter_count':len(names),'names_unique':unique,'declarations_match_intersection':declared_match,
        'fields_compared':['name',*FIELDS],'rows':rows,
        'initialization_equal':(declared_match and all(x['equal'] for x in rows)) if complete else None,
        'capacity_records':[copy.deepcopy(t['summary']['parameter_counts']) for t in (left,right)],
        'scope':'Exact comparison of saved same-seed records; not independent historical execution, mechanism proof or approval.'}


def enrich(material, pairs, requested_ids):
    if not pairs:return material
    result=copy.deepcopy(material)
    result['paired_review_protocol']=1
    result['pair_comparisons']=[compare(result,p) for p in pairs]
    result['pair_scope']={'requested_trial_ids':list(requested_ids),
        'dependency_trial_ids':[t['summary']['trial_id'] for t in result['trials']
                                if t['summary']['trial_id'] not in requested_ids],
        'selection_rule':'Only a unique completed current proposed control with the same seed/task, code/plan and train/validation identities.',
        'raw_record_paths':['/trials/'+str(i)+'/model_provenance/data' for i in range(len(result['trials']))],
        'comparison_method':'Names are intersected, declarations checked, then all six remaining initialization fields are compared with exact typed canonical JSON hashes. No model imports or execution.',
        'execution_authorized':False,'scientific_approval':False}
    return result
