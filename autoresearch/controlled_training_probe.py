"""Standalone, reviewed checkpoint/optimization counterfactual. Never calls fit()."""
import copy
import hashlib
import json
import math
import statistics
import sys
from pathlib import Path


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def compact_branch(branch):
    steps = branch['updates']
    updates = [x['relative_update_norm'] for x in steps if x.get('relative_update_norm') is not None]
    gradients = [x['grad_norm'] for x in steps if x.get('grad_norm') is not None]
    median = statistics.median(updates) if updates else None
    return {k: branch[k] for k in ('lr_factor', 'curve', 'batch_sequence_sha256')} | {
        'control_curve':branch.get('control_curve'), 'initial_state':branch.get('initial_state'),
        'status':branch.get('status'),
        'completed_update_count': sum(x.get('update_completed', False) for x in steps),
        'max_relative_update_norm': max(updates, default=None),
        'median_relative_update_norm': median,
        'max_to_median_update_ratio': max(updates)/median if median else None,
        'grad_norm_range': [min(gradients), max(gradients)] if gradients else None,
        'nonfinite_events': [x for x in steps if x['nonfinite']]}


def main():
    import numpy as np
    import torch
    import psutil
    from torch.nn import functional as F
    sys.path.insert(0, str(Path.cwd() / 'src'))
    from models.factory import build_model
    from training.checkpoints import load_checkpoint, model_digest
    from dataset_runtime.data import load_data
    from dataset_runtime.training import predict_metrics
    from diagnostic_verdicts import classify, branch_evidence
    def state_sha(value):
        def packed(x):
            if isinstance(x,torch.Tensor):
                a=x.detach().cpu().contiguous().numpy()
                return {'dtype':str(a.dtype),'shape':list(a.shape),'sha256':hashlib.sha256(a.tobytes()).hexdigest()}
            if isinstance(x,dict):return {str(k):packed(v) for k,v in x.items()}
            if isinstance(x,(list,tuple)):return [packed(v) for v in x]
            return x
        return hashlib.sha256(json.dumps(packed(value),sort_keys=True,allow_nan=False).encode()).hexdigest()
    invocation = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    protocol = json.loads(Path('diagnostic_protocol.json').read_text(encoding='utf-8'))
    assert invocation['phase'] == 'code_check' and protocol['steps'] == 32
    assert protocol['lr_factors'] == [1.0, 0.25] and protocol['seeds'] == [17, 29]
    process = psutil.Process()
    assert 0 < len(process.cpu_affinity()) <= 2, 'CPU affinity must be enforced, not best effort'
    torch.set_num_threads(2)
    torch.set_num_interop_threads(1)
    torch.use_deterministic_algorithms(True)
    torch.backends.mkldnn.enabled = False
    report = {'version': 1, 'protocol': protocol, 'trials': [], 'scientific_approval': False,
        'startup':json.loads(Path('diagnostic_startup.json').read_text(encoding='utf-8')) if Path('diagnostic_startup.json').exists() else None,
        'scope': 'New CPU counterfactuals from saved best checkpoints, not historical replay or parameter selection.',
        'limitations': ['Historical last-step weights were not saved.',
            'Two seeds and 32 local steps cannot rule out chronic instability or establish a global overfitting cause.',
            'No held-out values, no model activation, no additional full training.']}
    original_hashes = {}
    for item in protocol['inputs']:
        checkpoint_path, config_path = Path(item['checkpoint_path']), Path(item['config_path'])
        for path, expected in ((checkpoint_path, item['checkpoint_sha256']), (config_path, item['config_sha256'])):
            assert file_sha(path) == expected, 'Frozen input hash changed'
            original_hashes[str(path)] = expected
        config = json.loads(config_path.read_text(encoding='utf-8'))
        assert config['phase'] == 'validation' and config['variant'] == 'proposed' and config['loss'] == 'mse'
        assert config['seed'] == item['seed']
        data = load_data(config, phase='validation', check_config=invocation)
        assert set(data.fingerprints) == {'train', 'validation'}
        assert data.fingerprints == item['split_fingerprints']
        # These are locally produced, hash-pinned checkpoints from the original runner.
        state = torch.load(checkpoint_path, map_location='cpu', weights_only=False)
        assert state['config'] == config and state['seed'] == item['seed']
        assert len(state['optimizers']) == 1
        common = config['common']
        identities = ({'data_sha256': data.fingerprints['train']}, {'data_sha256': data.fingerprints['validation']})

        def restored():
            model, _, _ = build_model(config, config['seed'])
            model = model.cpu()
            load_checkpoint(model, state, config, config['seed'], *identities)
            optimizer = torch.optim.Adam(model.parameters(), lr=common['lr'], betas=tuple(common['betas']),
                eps=common['optimizer_eps'], weight_decay=common['weight_decay'])
            optimizer.load_state_dict(copy.deepcopy(state['optimizers'][0]))
            assert state_sha(optimizer.state_dict()) == state_sha(state['optimizers'][0]), 'Optimizer restore changed state'
            torch.set_rng_state(state['torch_rng'].cpu())
            return model, optimizer

        def evaluate(model):
            train = predict_metrics(model, data, 'train', config['runtime']['evaluation_batch_size'])[0]
            validation = predict_metrics(model, data, 'validation', config['runtime']['evaluation_batch_size'])[0]
            return {'train_mse': train['mse'], 'validation_mse': validation['mse']}

        model, optimizer = restored()
        restored_digest = model_digest(model)
        checkpoint_model_match = restored_digest == item['final_model_sha256']
        initial = evaluate(model)
        repeat = evaluate(model)
        control_model = model
        control_optimizer = optimizer
        control_optimizer_sha = state_sha(control_optimizer.state_dict())
        repeat_drift = {k: abs(repeat[k]-initial[k]) for k in initial}
        assert max(repeat_drift.values()) <= 1e-12, 'Zero-update CPU evaluation is not deterministic'
        assert model_digest(model) == restored_digest, 'Evaluation modified model weights'
        mismatch = abs(initial['validation_mse']-item['reported_val_mse'])
        tolerance = protocol['historical_metric_atol'] + protocol['historical_metric_rtol']*abs(item['reported_val_mse'])
        trial = {'trial_id': item['trial_id'], 'seed': config['seed'], 'checkpoint': {
            'saved_best_step': state['counters']['global_step'], 'restored_model_sha256': restored_digest,
            'matches_recorded_final_model': checkpoint_model_match, 'reported_val_mse': item['reported_val_mse'],
            'reevaluated_val_mse': initial['validation_mse'], 'absolute_metric_difference': mismatch,
            'predeclared_metric_tolerance': tolerance, 'metric_matches': mismatch <= tolerance,
            'zero_update': {'metrics': initial, 'repeat_drift': repeat_drift},
            'optimizer_state_loaded': True, 'restored_torch_rng': True}, 'branches': []}
        report['trials'].append(trial)
        if not checkpoint_model_match or mismatch > tolerance:
            trial['probe_status'] = 'not_run_checkpoint_mismatch'
            continue
        # A new, predeclared matched sequence. Do not claim to reconstruct the unsaved
        # mid-epoch cursor of the historical run, nor consume validation for sampling.
        rng = np.random.Generator(np.random.PCG64())
        rng.bit_generator.state = copy.deepcopy(state['permutation_rng'])
        permutation = rng.permutation(data.origins('train'))
        batches = [permutation[i*common['batch_size']:(i+1)*common['batch_size']] for i in range(protocol['steps'])]
        assert all(len(x) == common['batch_size'] for x in batches)
        batch_sha = hashlib.sha256(np.concatenate(batches).astype('<i8').tobytes()).hexdigest()
        for factor in protocol['lr_factors']:
            model, optimizer = restored()
            assert model_digest(model) == restored_digest
            assert optimizer.param_groups[0]['lr'] == common['lr']
            initial_state={'model_sha256':model_digest(model),'optimizer_sha256':state_sha(optimizer.state_dict()),
                           'torch_rng_sha256':state_sha(torch.get_rng_state())}
            optimizer.param_groups[0]['lr'] *= factor  # sole diagnostic intervention
            branch = {'lr_factor': factor, 'batch_sequence_sha256': batch_sha,
                'initial_state':initial_state,
                'curve': [{'step': 0, **initial, 'source': 'shared_verified_initial_state'}],
                'control_curve': [{'step':0, **repeat, 'weights_unchanged':True}], 'updates': []}
            trial['branches'].append(branch)
            for step, indices in enumerate(batches, 1):
                model.train()
                x, y = data.batch(indices)
                x, y = torch.from_numpy(x), torch.from_numpy(y.astype(np.float32))
                scale, center = torch.as_tensor(data.std, dtype=x.dtype), torch.as_tensor(data.mean, dtype=x.dtype)
                optimizer.zero_grad(set_to_none=True)
                loss = F.mse_loss(model(x)*scale+center, y)
                if not bool(torch.isfinite(loss)):
                    branch['updates'].append({'step': step, 'nonfinite': True, 'failure': 'loss',
                        'failure_stage':'before_optimizer_step','pre_update_finite':False,
                        'update_completed': False, 'grad_norm': None, 'relative_update_norm': None})
                    break
                loss.backward()
                if any(p.grad is not None and not bool(torch.isfinite(p.grad).all()) for p in model.parameters()):
                    branch['updates'].append({'step': step, 'nonfinite': True, 'failure': 'gradient',
                        'failure_stage':'before_optimizer_step','pre_update_finite':False,
                        'update_completed': False, 'grad_norm': None, 'relative_update_norm': None})
                    break
                grad_norm = float(torch.nn.utils.clip_grad_norm_(model.parameters(), common['grad_clip'], error_if_nonfinite=True))
                before = [p.detach().clone() for p in model.parameters()]
                pre_update_finite=bool(torch.isfinite(x).all() and torch.isfinite(y).all()
                    and all(bool(torch.isfinite(p).all()) for p in before))
                magnitude = math.sqrt(sum(float(p.double().square().sum()) for p in before))
                optimizer.step()
                if any(not bool(torch.isfinite(p).all()) for p in model.parameters()):
                    branch['updates'].append({'step': step, 'nonfinite': True, 'failure': 'parameters',
                        'failure_stage':'after_optimizer_step','pre_update_finite':pre_update_finite,
                        'update_completed': True, 'grad_norm': grad_norm, 'relative_update_norm': None})
                    break
                model.check('controlled_update')
                delta = math.sqrt(sum(float((p.detach()-b).double().square().sum()) for p,b in zip(model.parameters(),before)))
                branch['updates'].append({'step': step, 'train_batch_loss': float(loss.detach()),
                    'grad_norm': grad_norm, 'relative_update_norm': delta/max(magnitude,1e-12),
                    'nonfinite': False, 'update_completed': True})
                if step in protocol['evaluation_steps']:
                    branch['curve'].append({'step':step, **evaluate(model)})
                    saved_rng = torch.get_rng_state().clone()
                    try:
                        control_metrics = evaluate(control_model)
                        stable = (model_digest(control_model) == restored_digest
                                  and state_sha(control_optimizer.state_dict()) == control_optimizer_sha)
                        assert stable, 'Zero-update control weights or Adam state changed'
                        branch['control_curve'].append({'step':step, **control_metrics, 'weights_unchanged':stable})
                    finally:
                        torch.set_rng_state(saved_rng)
            # Controls do not perform updates. Finish their predeclared schedule
            # even if an update branch stops; resource limits still apply.
            done={p['step'] for p in branch['control_curve']}
            for step in protocol['evaluation_steps']:
                if step in done:continue
                saved_rng=torch.get_rng_state().clone()
                try:
                    control_metrics=evaluate(control_model)
                    stable=(model_digest(control_model)==restored_digest
                            and state_sha(control_optimizer.state_dict())==control_optimizer_sha)
                    branch['control_curve'].append({'step':step,**control_metrics,'weights_unchanged':stable})
                finally:
                    torch.set_rng_state(saved_rng)
            branch['final_model_sha256'] = None if any(x['nonfinite'] for x in branch['updates']) else model_digest(model)
            branch['execution'] = branch_evidence(branch)
            branch['status'] = 'completed' if branch['execution']['complete'] else 'incomplete'
        trial['probe_status'] = ('completed_bounded_counterfactual' if all(b['status']=='completed' for b in trial['branches'])
                                 else 'incomplete_counterfactual')
        assert trial['branches'][0]['batch_sequence_sha256'] == trial['branches'][1]['batch_sequence_sha256']
        assert trial['branches'][0]['initial_state'] == trial['branches'][1]['initial_state']
    assert all(file_sha(path) == digest for path,digest in original_hashes.items())
    report['input_hashes_unchanged'] = True
    report['classification'] = classify(report['trials'])
    report['summary'] = {'trials': [{k:t[k] for k in ('trial_id','seed','checkpoint','probe_status')} |
        {'branches':[compact_branch(b) for b in t['branches']]} for t in report['trials']],
        'input_hashes_unchanged': True, 'scientific_approval': False, 'limitations':report['limitations'],
        'classification':report['classification'],'startup':report['startup']}
    Path('artifacts').mkdir(exist_ok=True)
    target = Path('artifacts/controlled_diagnostic.json')
    target.write_text(json.dumps(report,ensure_ascii=False,allow_nan=False,indent=2),encoding='utf-8')
    print(json.dumps({'metrics': {'controlled_diagnostic_completed':int(report['classification']['complete_counterfactual']),
                                 'checkpoint_restoration_consistent':int(report['classification']['checkpoint_restoration_consistent'])},
        'artifacts': ['artifacts/controlled_diagnostic.json'],
        'diagnostic_report_sha256':file_sha(target)},allow_nan=False))


if __name__ == '__main__':
    main()
