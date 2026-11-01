"""Resume an unchanged, approved candidate after repairing host check generation."""
import copy

from .experiment_workflow import sha
from .storage import now


def prepare(engine, run_id, reviewed_archive, trial_id, *, host_fix_sha256, instruction):
    from .dataset_experiments import current, COLLECTION, _reviews
    from .experiment_decisions import _archive
    from .code_bundle import validate
    run, pending = engine.store.get('runs',run_id), current(engine,run_id)
    if not run or run['status'] not in {'paused','blocked'} or not instruction.strip():
        raise ValueError('Host check recovery requires a stopped task and explicit repair instruction')
    archive = engine.store.get('experiment_evidence',reviewed_archive['id']) or {}
    if archive.get('run_id') != run_id or sha(archive.get('summary')) != reviewed_archive['sha256']:
        raise ValueError('Reviewed configuration archive changed')
    reviewed = archive['summary'].get('setup',{})
    failure = archive['summary'].get('failure',{})
    result = engine.store.get('experiments',run_id+':'+trial_id) or {}
    if (not pending or reviewed.get('id') != pending['id'] or reviewed.get('cycle') != engine.cycle(run_id)
            or reviewed.get('status') != 'checking_data' or failure.get('kind') != 'interface_check'
            or failure.get('details',{}).get('trial_id') != trial_id
            or failure['details'].get('result_sha256') != sha(result)
            or result.get('run_id') != run_id or result.get('status') != 'failed'
            or result.get('params',{}).get('phase') != 'code_check'):
        raise ValueError('Host check recovery must bind the exact failed check and reviewed candidate')
    if len(host_fix_sha256)!=64 or any(c not in '0123456789abcdef' for c in host_fix_sha256):
        raise ValueError('Host repair source SHA required')
    _reviews(engine,run_id,reviewed)
    validate(reviewed['candidate']['bundle'], reviewed['program']['code'])
    if sha(reviewed['draft']) != reviewed['candidate']['amendment_sha256']:
        raise ValueError('Reviewed candidate content changed')
    previous = _archive(engine,run_id,{'setup':pending})
    evidence = {'run_id':run_id,'reviewed_archive':reviewed_archive,'superseded_setup':previous,
        'trial_id':trial_id,'result_sha256':sha(result),'host_fix_sha256':host_fix_sha256,
        'instruction':instruction,'candidate_sha256':reviewed['candidate']['bundle']['sha256'],
        'approval_reused_only_for_unchanged_candidate':True,'created_at':now()}
    evidence['id']=run_id+':host-check-recovery:'+sha(evidence)
    restored = copy.deepcopy(reviewed)
    restored.update(status='checking_data',previous_setup=previous,
        repair_sequence=pending.get('repair_sequence',0),host_check_recovery=evidence['id'],updated_at=now())
    engine.store.batch([(COLLECTION,restored['id'],restored),('dataset_host_check_recoveries',evidence['id'],evidence)])
    engine.store.event(run_id,'experiment.host_check_repaired',
        '宿主检查生成器已修复；复用同版本真实审批，重新执行接口检查。',
        {'recovery_id':evidence['id'],'candidate_sha256':evidence['candidate_sha256']})
    return evidence
