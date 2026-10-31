"""Read saved checkpoint tensors and receipts without importing experiment code.

The resulting supplement is evidence for a new decision, never an approval.
Original decisions, requests, observations and execution files stay immutable.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from .experiment_workflow import sha
from .record_archive import fingerprint

KEY = 'saved_runtime_audit'
COLLECTION = 'saved_runtime_audits'


def compact_sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def trial_audit(store, run_id, trial_id):
    import numpy as np
    import torch
    import yaml
    from .deletion import _plain_path

    key = run_id + ':' + trial_id
    record = store.get('experiments', key)
    if (not record or record.get('run_id') != run_id or record.get('status') != 'completed'
            or record.get('exit_code') != 0 or record.get('params', {}).get('phase') != 'validation'):
        raise ValueError('Audit requires an exact completed validation trial')
    before = fingerprint(store, 'experiments', key)
    root = store.path.parent.resolve() / 'experiments' / run_id / trial_id
    folder = Path(record['artifact_path']).resolve()
    if not folder.is_relative_to(root):
        raise ValueError('Audit path is outside the original trial')
    _plain_path(folder, store.path.parent.resolve())
    names = ['artifacts/best.pt', 'artifacts/result.json', 'artifacts/model_provenance.json',
        'artifacts/training_log.json', 'params.json', 'hyperparameters.yaml',
        'hyperparameters.receipt.json', 'environment.json', 'dataset_runtime/training.py',
        'dataset_runtime/adapter.py', 'dataset_runtime/hyperparameters.py',
        'src/training/checkpoints.py', 'src/models/factory.py', 'src/data/dataset.py',
        'src/utils/contracts.py']
    files = {}
    for name in names:
        path = folder / name
        _plain_path(path, store.path.parent.resolve())
        if not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError('Missing or oversized saved audit input: ' + name)
        files[name] = file_sha(path)
    def read(name):
        return json.loads((folder / name).read_text(encoding='utf8'))
    result, provenance = read('artifacts/result.json'), read('artifacts/model_provenance.json')
    log, params = read('artifacts/training_log.json'), read('params.json')
    receipt, environment = read('hyperparameters.receipt.json'), read('environment.json')
    profile = yaml.safe_load((folder / 'hyperparameters.yaml').read_text(encoding='utf8'))['profiles'][params['variant']]
    # Restricted unpickler and CPU storage only; no model construction or restore.
    state = torch.load(folder / 'artifacts/best.pt', map_location='cpu', weights_only=True)
    descriptors = {}
    for name, tensor in state['model'].items():
        if not isinstance(tensor, torch.Tensor) or tensor.device.type != 'cpu':
            raise ValueError('Checkpoint model must contain CPU tensors')
        array = np.ascontiguousarray(tensor.detach().numpy())
        descriptors[name] = {'shape': list(array.shape), 'dtype': array.dtype.str,
            'nbytes': array.nbytes, 'sha256': hashlib.sha256(array.tobytes()).hexdigest()}
    training = result['evaluation']['details']['training']
    inventory = provenance['inventory']['parameters']
    if len(state['optimizers']) != 1 or len(state['optimizers'][0]['param_groups']) != 1:
        raise ValueError('Audit adapter requires the saved single-optimizer, single-group contract')
    optimizer = state['optimizers'][0]
    group = optimizer['param_groups'][0]
    ids = group['params']
    common = profile['common']
    expected = {'lr': common['lr'], 'weight_decay': common['weight_decay'],
        'betas': common['betas'], 'eps': common['optimizer_eps']}
    actual = {k: list(group[k]) if isinstance(group[k], tuple) else group[k] for k in expected}
    ordered_names = [r['name'] for r in inventory]
    members = []
    for index, row in enumerate(inventory):
        slot = optimizer['state'].get(index, {})
        shapes = {k: list(v.shape) for k, v in slot.items() if k in ('exp_avg', 'exp_avg_sq')}
        members.append({'saved_parameter_id': index, 'name': row['name'], 'shape': row['shape'],
            'moment_shapes': shapes, 'step': float(slot['step']) if 'step' in slot else None,
            'shape_matches': len(shapes) == 2 and all(v == row['shape'] for v in shapes.values())})
    checks = {
        'host_params_match_record': params == record['params'],
        'host_params_digest_matches_receipt': sha(params) == receipt['params_sha256'] == environment['params_sha256'],
        'yaml_file_matches_receipt': files['hyperparameters.yaml'] == receipt['yaml_sha256'],
        'yaml_profile_matches_receipt': compact_sha(profile) == receipt['effective_profile_sha256'],
        'host_environment_binds_receipt': environment['hyperparameter_yaml'] == receipt,
        'optimizer_values_match_loaded_yaml': actual == expected,
        'optimizer_values_match_approved_mirror': all(common[k] == params['common'][k]
            for k in ('lr', 'weight_decay', 'betas', 'optimizer_eps', 'optimizer')),
        'optimizer_ids_cover_inventory_once': ids == list(range(len(inventory))) and set(optimizer['state']) == set(ids),
        'parameter_order_matches_checkpoint': ordered_names == list(state['model']),
        'moment_shapes_match_inventory': all(m['shape_matches'] for m in members),
        'checkpoint_file_matches_report': files['artifacts/best.pt'] == training['best_checkpoint_sha256'],
        'checkpoint_tensors_match_reported_post_restore_model': compact_sha(descriptors) == training['model_sha256'],
        'checkpoint_config_matches_invocation': state['config'] == params and state['config_sha256'] == compact_sha(params),
        'seed_matches_invocation': state['seed'] == params['seed'] == log['seed'],
        'split_identities_match_log': all(state[k+'_data_sha256'] == log['split_fingerprints'][k] for k in ('train', 'validation')),
        'best_counters_match_report': state['counters'] == training['best_counters'],
        'logged_learning_rates_match_group': bool(log['events']) and all(e['learning_rate'] == group['lr'] for e in log['events']),
    }
    if before != fingerprint(store, 'experiments', key) or any(file_sha(folder/n) != v for n,v in files.items()):
        raise ValueError('Saved trial evidence changed during read-only audit')
    return {'trial_id': trial_id, 'record': before, 'files': files,
        'seed': params['seed'], 'variant': params['variant'], 'checks': checks,
        'optimizer': {'actual': actual, 'yaml': expected, 'parameter_count': len(ids),
            'state_count': len(optimizer['state']), 'members': members,
            'member_binding': 'Checkpoint ordinal IDs are matched to the saved named-parameter order; moment shapes and source construction agree. Names were not serialized in optimizer.param_groups.'},
        'weights': {'checkpoint_model_sha256': compact_sha(descriptors), 'reported_model_sha256': training['model_sha256'],
            'tensor_count': len(descriptors), 'scope': 'Checkpoint tensors match the historical reported post-restore model digest. No separately saved final-evaluation state_dict or independent historical execution witness is asserted.'},
        'metrics': record['metrics'], 'best_step': state['counters']['global_step'],
        'source_scope': 'Static saved source inspection and saved tensors only; no candidate imports, model construction, training, restore or evaluation.'}


def build(engine, run_id, decision_id):
    from . import performance_policy
    record = engine.store.get('experiment_decisions', decision_id) or {}
    saved = record.get('artifact_evidence_handoff') or {}
    review = saved.get('independent_review') or {}
    base = engine.store.get('experiment_evidence', record['evidence']['id'])
    old = engine.store.get('experiment_evidence', saved['evidence']['id'])
    if (record.get('run_id') != run_id or saved.get('status') != 'unresolved'
            or sha(base['summary']) != record['evidence']['sha256']
            or sha(old['summary']) != saved['evidence']['sha256']
            or {r['role'] for r in review.get('results', [])} != {'director','engineer','scout'}):
        raise ValueError('Audit requires the completed original three-role unresolved review')
    ids = [t['summary']['trial_id'] for t in old['summary']['trials']]
    if not 1 <= len(ids) <= 32:
        raise ValueError('Audit trial scope is missing or too large')
    trials = [trial_audit(engine.store, run_id, tid) for tid in ids]
    policy = performance_policy.current(engine.store, run_id)
    prior = engine.store.list('performance_policies', run_id)
    gate = {'current_cycle': base['summary']['binding']['cycle'],
        'active_performance_policy': performance_policy.compact(policy),
        'active_acceptance': ({'criteria':[copy.deepcopy(policy['policy']['criterion'])],'policy':'aggregate_primary'}
            if policy else copy.deepcopy(base['summary']['acceptance'])),
        'historical_acceptance':copy.deepcopy(base['summary']['acceptance']),
        'historical_policies': [{'id': p['id'], 'sha256': p['sha256'], 'cycle': p['policy']['cycle'],
            'plan_id': p['policy']['plan_id'], 'plan_sha256': p['policy']['plan_sha256'],
            'criterion': p['policy']['criterion']} for p in prior],
        'interpretation': 'The current policy includes an explicitly authorized criterion carried in the authenticated dataset transition. That authority applies to its targets and current plan. Original plan thresholds and old comparisons stay historical; they do not override explicit operator direction. Performance acceptance is not permission to execute or proof of mechanism/stability.',
        'policy_modified': False}
    closed = [{'role': r['role'], 'findings': [{k:f[k] for k in ('check','outcome')}
        for f in r['findings'] if f['outcome'] in ('equal','different')]} for r in review['results']]
    from .experiment_analysis import _param_diff
    pending = []
    for unit in base['summary']['units']:
        if unit['kind'] != 'ablation' or not unit['available']:continue
        jobs = []
        for name in unit['jobs']:
            params = base['summary']['job_parameters'][name]
            controls = [r for r in base['summary']['records'] if r['status'] == 'completed'
                and r['params'].get('variant') == 'proposed'
                and all(r['params'].get(k) == params.get(k) for k in ('seed','dataset','benchmark_task_id','phase'))]
            jobs.append({'job':name,'seed':params['seed'],'params_sha256':sha(params),
                'same_seed_controls':[{'trial_id':r['id'],'params_sha256':sha(r['params']),
                    'differences':_param_diff(r['params'],params)} for r in controls]})
        pending.append({'unit_id':unit['id'],'jobs':jobs,'execution_authorized':False,
            'scope':'Frozen exact configuration differences, not a new grid or a single-factor causal claim. Actual pre-training capacity and ordinary independent approval remain required.'})
    binding=copy.deepcopy(base['summary']['binding'])
    if policy:binding['performance_policy_sha256']=policy['sha256']
    return {'version': 2, 'run_id': run_id, 'binding': binding,
        'audit_implementation_sha256':file_sha(__file__),
        'source_decision': fingerprint(engine.store,'experiment_decisions',decision_id),
        'source_evidence': record['evidence'], 'source_artifact_evidence': saved['evidence'],
        'source_review_sha256': sha(review['results']), 'trials': trials, 'gate_mapping': gate,
        'findings': [{k:copy.deepcopy(t[k]) for k in ('trial_id','seed','variant','checks','weights','best_step','metrics')}
            | {'optimizer':{k:copy.deepcopy(v) for k,v in t['optimizer'].items() if k!='members'},
               'details_path':'/training_investigation/'+KEY+'/trials/'+str(i)} for i,t in enumerate(trials)],
        'pending_batches':pending,
        'prior_review': {'closed_findings': closed, 'instruction': 'Reuse the completed initialization, capacity and metric checks. Review only new runtime links and gate applicability; original unknowns remain unless the new evidence resolves them.'},
        'execution': {'fit_calls': 0, 'optimizer_steps': 0, 'evaluation_calls': 0,
            'model_restore_calls': 0, 'candidate_imports': 0, 'completed_experiments_replayed': 0},
        'execution_authorized': False, 'scientific_approval': False}


def register(engine, run_id, audit, instruction):
    run = engine.store.get('runs', run_id)
    if run.get('status') not in {'blocked','paused'} or not instruction.strip() or audit.get('run_id') != run_id:
        raise ValueError('Explicit blocked-run audit registration required')
    if any(audit.get('execution', {}).values()) or audit.get('execution_authorized') is not False or audit.get('scientific_approval') is not False:
        raise ValueError('Runtime supplement must not represent execution or approval')
    with engine.store._read_db() as db:
        active=db.execute("SELECT key FROM records WHERE collection IN ('experiments','handoffs') "
            "AND json_extract(data,'$.run_id')=? AND json_extract(data,'$.status')='running' LIMIT 1",(run_id,)).fetchone()
    if active:raise ValueError('Wait for current experiments and paid reviews to settle')
    validate_sources(engine, run_id, audit)
    expected = build(engine,run_id,audit['source_decision']['key'])
    if audit != expected:raise ValueError('Runtime audit differs from fresh read-only recomputation')
    session_key=run_id+':'+str(audit['binding']['cycle'])
    session=engine.store.get('experiment_decision_sessions',session_key)
    session_archive=None
    if session and session.get('binding')!=audit['binding']:
        from .performance_policy import current as current_policy
        policy=current_policy(engine.store,run_id)
        original_binding=session.get('binding') or {}
        if (session.get('pending') or not policy or not policy['policy'].get('authority')
                or original_binding.get('performance_policy_sha256')
                or audit['binding']!={**original_binding,'performance_policy_sha256':policy['sha256']}):
            raise ValueError('Runtime audit cannot change an execution session outside the missing transition-policy binding')
        from .record_archive import save
        session_archive=save(engine.store,run_id,fingerprint(engine.store,'experiment_decision_sessions',session_key))
    identifier = run_id + ':saved-runtime-audit:' + sha(audit)
    row = {'id': identifier, 'run_id': run_id, 'sha256': sha(audit), 'audit': copy.deepcopy(audit),
        'operator_instruction': instruction, 'provenance': 'host_read_only_saved_runtime_evidence'}
    def retain(old):
        if old and old != row:raise ValueError('Immutable runtime audit changed')
        return old or row
    engine.store.mutate(COLLECTION, identifier, retain)
    already_bound=run.get('state',{}).get(KEY)==identifier
    def bind(current):
        if current != run:raise ValueError('Run changed before audit registration')
        return {**current, 'state': {**current['state'], KEY: identifier}}
    if not already_bound:engine.store.mutate('runs', run_id, bind)
    if session_archive:
        def bind_session(current):
            if current!=session:raise ValueError('Execution session changed during policy source repair')
            return {**current,'binding':copy.deepcopy(audit['binding']),
                'performance_authority_repair':{'original_session':session_archive,'audit_id':identifier,
                    'policy_sha256':audit['binding']['performance_policy_sha256'],
                    'execution_authorized':False,'scientific_approval':False}}
        engine.store.mutate('experiment_decision_sessions',session_key,bind_session)
    if already_bound and not session_archive:return row
    engine.store.event(run_id, 'experiment.saved_runtime_audit', '已补充既有检查点、优化器和门禁来源证据；待新的独立评审，未授权执行。',
        {'audit_id': identifier, 'sha256': row['sha256'], 'execution_authorized': False})
    return row


def validate_sources(engine, run_id, audit):
    if audit.get('run_id') != run_id:raise ValueError('Runtime audit belongs to another run')
    for ref in [audit['source_decision'], *[t['record'] for t in audit['trials']]]:
        if fingerprint(engine.store,ref['collection'],ref['key']) != ref:
            raise ValueError('Runtime audit source record changed')
    from .deletion import _plain_path
    for trial in audit['trials']:
        record = engine.store.get('experiments',run_id+':'+trial['trial_id'])
        folder = Path(record['artifact_path']).resolve()
        root = engine.store.path.parent.resolve()
        if not folder.is_relative_to(root/'experiments'/run_id/trial['trial_id']):
            raise ValueError('Runtime audit trial path changed')
        for name,digest in trial['files'].items():
            path = folder/name
            if not path.resolve().is_relative_to(folder):raise ValueError('Invalid audit file path')
            _plain_path(path,root)
            if file_sha(path) != digest:raise ValueError('Runtime audit file changed: '+name)


def attach(engine, run_id, evidence):
    identifier = (engine.store.get('runs',run_id).get('state') or {}).get(KEY)
    if not identifier:return
    row = engine.store.get(COLLECTION,identifier) or {}
    audit = row.get('audit') or {}
    if row.get('run_id') != run_id or sha(audit) != row.get('sha256'):
        raise ValueError('Saved runtime audit identity changed')
    if audit.get('binding') != evidence['binding']:
        return  # Retain old evidence without applying it to a later implementation.
    validate_sources(engine,run_id,audit)
    evidence['training_investigation'][KEY] = {'id':identifier,'sha256':row['sha256'], **copy.deepcopy(audit)}


def overview(value):
    return {'id': value['id'], 'sha256': value['sha256'],
        'details_path': '/training_investigation/'+KEY,
        'required_paths': ['/training_investigation/'+KEY+'/'+name for name in ('findings','gate_mapping','prior_review','pending_batches')],
        'instruction': 'New read-only evidence answers the completed artifact review. Read these four paths, reuse prior closed findings and original trials, then form a new independently voted next step. Do not request the same artifacts again, rerun completed experiments, infer independent historical witnessing, or change acceptance thresholds.',
        'execution_authorized':False,'scientific_approval':False}


def missing_pages(engine, run_id, context, rows):
    """Coverage comes from authenticated exact pages, never an answer's assertion."""
    requested=context.get('experiment_decision',{}).get('training_investigation',{}).get(KEY)
    if not requested:return []
    from .experiment_decisions import _pointer
    from .experiment_workflow import encoded
    ref=context['validation_evidence']
    record=engine.store.get('experiment_evidence',ref['id']) or {}
    if record.get('run_id')!=run_id or sha(record.get('summary'))!=ref['sha256']:
        raise ValueError('Runtime audit reading evidence changed')
    texts={p:encoded(_pointer(record['summary'],p)) for p in requested['required_paths']}
    spans={p:[] for p in texts}
    for row in rows:
        call,result=row.get('call',{}),row.get('result',{})
        args=call.get('arguments',{});path=args.get('path');start,end=result.get('offset'),result.get('end')
        if (call.get('name')!='experiments.compare' or path not in texts
                or args.get('evidence_id')!=ref['id'] or args.get('expected_sha256')!=ref['sha256']
                or result.get('receipt_sha256')!=sha({'call':call,'result':{k:v for k,v in result.items() if k!='receipt_sha256'}})
                or type(start) is not int or type(end) is not int or not 0<=start<end<=len(texts[path])
                or result.get('text')!=texts[path][start:end]):continue
        spans[path].append((start,end))
    missing=[]
    for path,windows in spans.items():
        cursor=0
        for start,end in sorted(windows):
            if start>cursor:
                missing.extend({'path':path,'offset':offset,'end':min(offset+6000,start),
                    'limit':min(6000,start-offset),'total_chars':len(texts[path])}
                    for offset in range(cursor,start,6000))
            cursor=max(cursor,end)
        if cursor<len(texts[path]):
            missing.extend({'path':path,'offset':offset,'end':min(offset+6000,len(texts[path])),
                'limit':min(6000,len(texts[path])-offset),'total_chars':len(texts[path])}
                for offset in range(cursor,len(texts[path]),6000))
    return missing


class MissingRuntimeAuditReads(ValueError):
    def __init__(self, pages):
        self.pages = pages
        super().__init__('Read new saved runtime audit pages before deciding: '
                         + str(list(dict.fromkeys(p['path'] for p in pages))))


def require_reads(engine, run_id, context, rows):
    pages = missing_pages(engine, run_id, context, rows)
    if pages:
        raise MissingRuntimeAuditReads(pages)
