"""One bounded observation of existing states. Never trains or resumes them."""
import copy
import hashlib
import json
import math
from pathlib import Path
import sys
import time


def file_sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(exist_ok=True,parents=True)
    temp=path.with_suffix(path.suffix+'.pending')
    temp.write_text(json.dumps(value,ensure_ascii=False,allow_nan=False,indent=2),encoding='utf8')
    temp.replace(path)


class Guard:
    def __init__(self,limits):
        self.started=time.monotonic();self.cpu=time.process_time();self.limits=limits
    def check(self,stage):
        if (time.monotonic()-self.started>=self.limits['timeout_seconds']-15
                or time.process_time()-self.cpu>=self.limits['cpu_seconds']-15):
            raise RuntimeError('Fixed-state diagnostic deadline at '+stage+'; no automatic retry')


def main():
    report={'execution_status':'incomplete','scientific_approval':False,'states':[],
            'optimizer_steps':0,'fit_calls':0,'historical_root_cause':'unknown'}
    try:
        run(report)
    except BaseException as exc:
        report.update(error_type=type(exc).__name__,error=str(exc),automatic_retry=False)
        atomic_json('artifacts/fixed_state_interrupted.json',report)
        raise


def run(report):
    import numpy as np
    import torch
    import psutil
    from directional_math import state_hash,history_direction,full_gradient,signature,UnresolvedDirection
    sys.path.insert(0,str(Path.cwd()/'src'))
    from models.factory import build_model
    from dataset_runtime.data import load_data
    from dataset_runtime.training import predict_metrics
    invocation=json.loads(Path(sys.argv[1]).read_text(encoding='utf8'))
    protocol=json.loads(Path('diagnostic_protocol.json').read_text(encoding='utf8'))
    manifest=json.loads(Path('diagnostic_inputs.json').read_text(encoding='utf8'))
    assert hashlib.sha256(json.dumps(protocol,ensure_ascii=False,sort_keys=True,allow_nan=False).encode()).hexdigest()==manifest['protocol_sha256']
    limits=json.loads(Path('bootstrap.json').read_text(encoding='utf8'))
    assert invocation['phase']=='code_check' and protocol['seed']==29
    assert len(protocol['states'])==3 and len(manifest['states'])==3
    assert protocol['hard_limits']['optimizer_steps']==protocol['hard_limits']['fit_calls']==0
    assert limits['gpu_allowed'] is False and 0<len(psutil.Process().cpu_affinity())<=2
    assert torch.__version__=='2.5.1+cu121' and np.__version__=='1.26.4', 'Frozen numerical environment required'
    for field in ('cpu_cores','memory_mb','timeout_seconds','cpu_seconds','disk_mb'):
        assert limits[field]<=protocol['hard_limits'][field]
    guard=Guard(limits)
    torch.set_num_threads(2);torch.set_num_interop_threads(1)
    torch.use_deterministic_algorithms(True);torch.backends.mkldnn.enabled=False
    config_path=Path(manifest['config']['path'])
    assert file_sha(config_path)==manifest['config']['sha256']==protocol['config']['sha256']
    config=json.loads(config_path.read_text(encoding='utf8'))
    assert config['phase']=='validation' and config['seed']==29 and config['variant']=='proposed' and config['loss']=='mse'
    assert config['common']['weight_decay']==0
    data=load_data(config,phase='validation',check_config=invocation)
    assert data.fingerprints==protocol['data']['fingerprints'] and set(data.fingerprints)=={'train','validation'}
    report.update(protocol=protocol,protocol_sha256=manifest['protocol_sha256'],
        provenance='live_fixed_state_cpu_observation',numerical_environment={'torch':torch.__version__,'numpy':np.__version__},
        startup=json.loads(Path('diagnostic_startup.json').read_text()),limitations=protocol['limitations'])
    original_files={str(config_path):file_sha(config_path)}
    source_hashes={row['path']:row['sha256'] for row in manifest['runtime_sources']}
    for name,expected in source_hashes.items():assert file_sha(name)==expected
    for item,definition in zip(manifest['states'],protocol['states']):
        guard.check('state_start')
        assert item['label']==definition['label'] and item['sha256']==definition['sha256']
        path=Path(item['path']);assert file_sha(path)==item['sha256'];original_files[str(path)]=item['sha256']
        state=torch.load(path,map_location='cpu',weights_only=False)
        frozen_state_hash=state_hash(state)
        assert state['completed_updates']==definition['completed_updates']
        assert state['input']['seed']==29 and state['input']['split_fingerprints']==data.fingerprints
        assert state['protocol']==manifest['source_protocol'] and state['input']==manifest['source_input']
        model,_,_=build_model(config,29);model=model.cpu()
        model.load_state_dict(copy.deepcopy(state['model']),strict=True);model.eval()
        assert state_hash(model.state_dict())==state_hash(state['model'])
        named=list(model.named_parameters());parameters=[p for _,p in named]
        torch.set_rng_state(state['torch_rng'])
        base_state=copy.deepcopy(model.state_dict());rng=torch.get_rng_state().clone()
        base_hash=state_hash(base_state);optimizer_hash=state_hash(state['optimizer'])
        def evaluate():
            return {split+'_mse':predict_metrics(model,data,split,config['runtime']['evaluation_batch_size'],guard)[0]['mse']
                    for split in ('train','validation')}
        initial=evaluate();repeat=evaluate()
        assert max(abs(initial[k]-repeat[k]) for k in initial)<=1e-12,'Zero-state repeat mismatch'
        assert all(abs(initial[k]-definition['recorded_metrics'][k])<=1e-6+1e-4*abs(definition['recorded_metrics'][k]) for k in initial),'Saved-state identity metric mismatch'
        try:
            gT,lT,nT=full_gradient(model,data,'train',config['runtime']['evaluation_batch_size'],guard)
            gV,lV,nV=full_gradient(model,data,'validation',config['runtime']['evaluation_batch_size'],guard)
            direction=history_direction(named,state['optimizer'])
        except UnresolvedDirection as exc:
            assert state_hash(model.state_dict())==base_hash and state_hash(state)==frozen_state_hash
            assert torch.equal(torch.get_rng_state(),rng)
            report['states'].append({'label':item['label'],'source_sha256':item['sha256'],
                'baseline':initial,'repeat':repeat,'label_result':'inconclusive',
                'reason':str(exc),'original_state_unchanged':True})
            atomic_json('artifacts/fixed_state_progress.json',report)
            continue
        assert all(abs(x-initial[k])<=1e-6*max(abs(initial[k]),1) for x,k in ((lT,'train_mse'),(lV,'validation_mse'))),'Gradient objective differs from evaluation'
        normT=float(torch.linalg.vector_norm(gT));normA=float(torch.linalg.vector_norm(direction))
        assert math.isfinite(normT) and math.isfinite(normA),'Nonfinite direction norm'
        row={'label':item['label'],'source_sha256':item['sha256'],'model_sha256':base_hash,
             'optimizer_sha256':optimizer_hash,'baseline':initial,'repeat':repeat,
             'gradient_coverage':{'train_windows':nT,'validation_windows':nV},
             'gradient_norms':{'train':normT,'validation':float(torch.linalg.vector_norm(gV)),
                               'adam_history_displacement':normA}}
        report['states'].append(row)
        if normT==0 or normA==0:
            row.update(label_result='inconclusive',reason='Zero gradient or saved moment direction')
        else:
            directions={'train_descent':-gT/normT,'adam_history':direction/normA}
            theta=torch.cat([p.detach().to(torch.float64).reshape(-1) for p in parameters])
            rho=1e-4*max(float(torch.linalg.vector_norm(theta)),1.)
            points={};contributions=[];offset=0
            for name,param in named:
                end=offset+param.numel()
                contributions.append({'parameter':name,'elements':param.numel(),
                    'train_validation_dot':float(torch.dot(gT[offset:end],gV[offset:end])),
                    'train_adam_dot':float(torch.dot(gT[offset:end],directions['adam_history'][offset:end])),
                    'validation_adam_dot':float(torch.dot(gV[offset:end],directions['adam_history'][offset:end]))})
                offset=end
            assert offset==len(theta)
            for name,d in directions.items():
                points[name]={}
                for alpha in (-1.,-.5,.5,1.):
                    guard.check('finite_difference')
                    model.load_state_dict(base_state,strict=True)
                    offset=0
                    with torch.no_grad():
                        for param in parameters:
                            end=offset+param.numel()
                            param.copy_((theta[offset:end]+alpha*rho*d[offset:end]).reshape(param.shape).to(param.dtype))
                            offset=end
                    points[name][str(alpha)]=evaluate()
                    model.load_state_dict(base_state,strict=True)
                    assert state_hash(model.state_dict())==base_hash
            row.update(rho=rho,points=points,parameter_contributions=contributions,
                       findings=signature(initial,repeat,(gT,gV),directions,points,rho))
            for field,expected in [('train_validation_dot',float(torch.dot(gT,gV))),
                ('train_adam_dot',float(torch.dot(gT,directions['adam_history']))),
                ('validation_adam_dot',float(torch.dot(gV,directions['adam_history'])))]:
                assert abs(sum(c[field] for c in contributions)-expected)<=1e-10*max(abs(expected),1)
        assert state_hash(model.state_dict())==base_hash and state_hash(state['optimizer'])==optimizer_hash
        assert state_hash(state)==frozen_state_hash and torch.equal(torch.get_rng_state(),rng)
        row['original_state_unchanged']=True
        atomic_json('artifacts/fixed_state_progress.json',report)
    assert all(file_sha(p)==h for p,h in original_files.items())
    assert all(file_sha(p)==h for p,h in source_hashes.items())
    report.update(input_hashes_unchanged=True,execution_status='completed')
    report['summary']={'states':[{k:v for k,v in r.items() if k not in ('parameter_contributions','points')}
                               for r in report['states']],
        'input_hashes_unchanged':True,'optimizer_steps':0,'fit_calls':0,
        'historical_root_cause':'unknown','scientific_approval':False,'limitations':report['limitations']}
    target=Path('artifacts/fixed_state_diagnostic.json');atomic_json(target,report)
    print(json.dumps({'metrics':{'fixed_state_observation_completed':1,'states_observed':len(report['states'])},
        'artifacts':[str(target)],'diagnostic_report_sha256':file_sha(target)},allow_nan=False))


if __name__=='__main__':main()
