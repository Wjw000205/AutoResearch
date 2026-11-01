"""Standalone atomic evidence for one bounded diagnostic, never auto-resume."""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import time


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def atomic_json(path, value):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.pending')
    with temp.open('w',encoding='utf-8') as out:
        json.dump(value,out,ensure_ascii=False,allow_nan=False,indent=2)
        out.flush();os.fsync(out.fileno())
    os.replace(temp,path)


def finite_state(value):
    import torch
    if isinstance(value,torch.Tensor):return bool(torch.isfinite(value).all())
    if isinstance(value,dict):return all(finite_state(v) for v in value.values())
    if isinstance(value,(tuple,list)):return all(finite_state(v) for v in value)
    if isinstance(value,float):return math.isfinite(value)
    return True


def snapshot(model,optimizer,torch_rng,permutation_rng,batches,next_index,protocol,item,batch_sha):
    return {'model':copy.deepcopy(model.state_dict()),'optimizer':copy.deepcopy(optimizer.state_dict()),
        'torch_rng':torch_rng.clone(),'permutation_rng':copy.deepcopy(permutation_rng),
        'remaining_epoch_batches':[x.tolist() for x in batches[next_index:]],
        'next_batch_index':next_index,'completed_updates':next_index,
        'protocol':copy.deepcopy(protocol),'input':copy.deepcopy(item),'batch_sequence_sha256':batch_sha}


def save_state(path,state):
    import torch
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.pending')
    with temp.open('wb') as out:
        torch.save(state,out);out.flush();os.fsync(out.fileno())
    os.replace(temp,path)
    return {'path':path.as_posix(),'sha256':digest(path)}


def load_state(ref):
    import torch
    path=Path(ref['path'])
    if path.is_absolute() or '..' in path.parts or path.parts[0]!='artifacts':raise ValueError('Invalid evidence path')
    if digest(path)!=ref['sha256']:raise ValueError('Evidence hash mismatch')
    return torch.load(path,map_location='cpu',weights_only=False)


class Journal:
    def __init__(self,protocol,report):
        self.protocol,self.report=protocol,report
        self.root=Path('artifacts');self.root.mkdir(exist_ok=True)
        self.started=time.monotonic()
        actual=json.loads(Path('bootstrap.json').read_text()) if Path('bootstrap.json').is_file() else protocol['limits']
        self.wall=min(actual['timeout_seconds'],protocol['limits']['timeout_seconds'])-15
        self.cpu_limit=min(actual['cpu_seconds'],protocol['limits']['cpu_seconds'])-15
        self.cpu_started=time.process_time()
        self.serial={}
        self.report['execution_status']='incomplete'
        atomic_json(self.root/'diagnostic_progress.json',self.report)

    def guard(self):
        if time.monotonic()-self.started>=self.wall or time.process_time()-self.cpu_started>=self.cpu_limit:
            raise RuntimeError('Diagnostic soft deadline; committed prefix retained, no automatic retry')

    def update(self,seed,factor,row):
        with (self.root/'diagnostic_updates.jsonl').open('a',encoding='utf-8') as out:
            out.write(json.dumps({'seed':seed,'lr_factor':factor,**row},allow_nan=False)+'\n')
            out.flush();os.fsync(out.fileno())

    def commit(self,slot,state,branch):
        # Alternate two slots: a hard stop cannot overwrite the latest committed state.
        serial=self.serial.get(slot,0);self.serial[slot]=serial+1
        ref=save_state(self.root/(slot+f'-{serial%2}.pt'),state)
        restored=load_state(ref)
        assert restored['completed_updates']==state['completed_updates']
        branch['continuation']={**ref,'completed_updates':state['completed_updates'],
            'resume_eligible':state.get('resume_eligible',False) and finite_state(state['model']) and finite_state(state['optimizer']),
            'scope':'Committed diagnostic prefix only; no permission to resume.'}
        atomic_json(self.root/'diagnostic_progress.json',self.report)
        return ref

    def event(self,seed,factor,row,before,after,indices):
        stem=f'event-{seed}-{factor}-{row["step"]}'
        row['state_evidence']={'before':save_state(self.root/(stem+'-before.pt'),before),
            'after':save_state(self.root/(stem+'-after.pt'),after),
            'batch_indices':indices.tolist(),'step':row['step'],
            'batch_sequence_sha256':before['batch_sequence_sha256']}
        self.update(seed,factor,row)
        atomic_json(self.root/'diagnostic_progress.json',self.report)


def verified_nonfinite(event,original,lower):
    """A Boolean log flag cannot establish finite-to-nonfinite state transition."""
    try:
        evidence=event['state_evidence'];before=load_state(evidence['before']);after=load_state(evidence['after'])
        step=event['step'];paired=next(x for x in lower['updates'] if x['step']==step)
        protocol=json.loads(Path('diagnostic_protocol.json').read_text(encoding='utf-8'))
        return bool(event['failure_stage']=='after_optimizer_step' and event['failure'] in {'parameters','optimizer_state'}
            and event['update_completed'] and evidence['step']==step
            and before['next_batch_index']==step-1 and after['next_batch_index']==step
            and before['completed_updates']==step-1 and after['completed_updates']==step
            and before['protocol']==after['protocol']==protocol
            and before['input']==after['input']==protocol['inputs'][0]
            and before['batch_sequence_sha256']==after['batch_sequence_sha256']==evidence['batch_sequence_sha256']
                ==original['batch_sequence_sha256']==lower['batch_sequence_sha256']
            and before['remaining_epoch_batches'][0]==evidence['batch_indices']
            and finite_state(before['model']) and finite_state(before['optimizer'])
            and (not finite_state(after['model']) if event['failure']=='parameters' else not finite_state(after['optimizer']))
            and paired['update_completed'] and not paired['nonfinite'])
    except (KeyError,ValueError,IndexError,StopIteration,OSError,RuntimeError):return False


def interrupted(error):
    atomic_json(Path('artifacts')/'diagnostic_interruption.json',{'status':'incomplete',
        'error_type':type(error).__name__,'error':str(error)[:1500],
        'scientific_approval':False,'automatic_retry':False,
        'scope':'Only committed snapshot and journal prefixes are retained; missing observations are not completed.'})
