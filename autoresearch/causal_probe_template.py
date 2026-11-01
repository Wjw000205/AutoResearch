"""Compile a reviewed single-seed follow-up without altering prior probe files."""
from pathlib import Path


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError('Diagnostic template changed; review its source before compiling')
    return text.replace(old, new, 1)


def sources(protocol):
    root = Path(__file__).parent
    probe = (root / 'controlled_training_probe.py').read_text(encoding='utf-8')
    probe = replace_once(probe, "assert invocation['phase'] == 'code_check' and protocol['steps'] == 32",
        "assert invocation['phase'] == 'code_check' and 64 <= protocol['steps'] <= 256")
    probe = replace_once(probe, "assert protocol['lr_factors'] == [1.0, 0.25] and protocol['seeds'] == [17, 29]",
        "assert len(protocol['seeds']) == 1 and protocol['lr_factors'][0] == 1.0\n    assert 0 < protocol['lr_factors'][1] < 1 and protocol['batch_offset'] >= 32")
    old = """        permutation = rng.permutation(data.origins('train'))
        batches = [permutation[i*common['batch_size']:(i+1)*common['batch_size']] for i in range(protocol['steps'])]
        assert all(len(x) == common['batch_size'] for x in batches)"""
    new = """        # New batches beyond the already executed 32-step probe prefix.
        batches = []
        needed = protocol['batch_offset'] + protocol['steps']
        while len(batches) < needed:
            permutation = rng.permutation(data.origins('train'))
            full = len(permutation) // common['batch_size']
            assert full > protocol['batch_offset'], 'Cannot safely skip prior probe prefix'
            batches.extend(permutation[i*common['batch_size']:(i+1)*common['batch_size']] for i in range(full))
        skipped = batches[:protocol['batch_offset']]
        future_batches = batches[needed:]
        batches = batches[protocol['batch_offset']:needed]
        assert len(batches) == protocol['steps']
        skipped_sha = hashlib.sha256(np.concatenate(skipped).astype('<i8').tobytes()).hexdigest()
        assert skipped_sha == item['prior_probe_batch_sha256'], 'Prior prefix identity differs'
        assert all(len(x) == common['batch_size'] for x in batches)"""
    probe = replace_once(probe, old, new)
    probe = replace_once(probe, '    from diagnostic_verdicts import classify, branch_evidence',
        '    from diagnostic_verdicts import classify, branch_evidence\n    from diagnostic_persistence import Journal, snapshot, save_state, finite_state, atomic_json')
    probe = replace_once(probe, '    original_hashes = {}', '    journal=Journal(protocol,report)\n    original_hashes = {}')
    probe = replace_once(probe, '        def evaluate(model):', '        def evaluate(model):\n            journal.guard()')
    probe = replace_once(probe, "        for factor in protocol['lr_factors']:", """        def capture(model,optimizer,index,completed=None,eligible=True):
            saved=snapshot(model,optimizer,torch.get_rng_state(),rng.bit_generator.state,
                batches+future_batches,index,protocol,item,batch_sha)
            saved['completed_updates']=index if completed is None else completed
            saved['resume_eligible']=eligible
            return saved
        zero=capture(control_model,control_optimizer,0)
        trial['zero_update_state']=save_state(Path('artifacts')/('zero-'+str(item['seed'])+'.pt'),zero)
        for factor in protocol['lr_factors']:""")
    probe = replace_once(probe, "            for step, indices in enumerate(batches, 1):", """            slot='continuation-'+str(item['seed'])+'-'+str(factor)
            journal.commit(slot,capture(model,optimizer,0),branch)
            for step, indices in enumerate(batches, 1):
                journal.guard()
                before_state=capture(model,optimizer,step-1)""")
    # All three exceptional transitions retain actual before/after state, not flags alone.
    probe = probe.replace('                    break\n', """                    row=branch['updates'][-1]
                    after_state=capture(model,optimizer,step,step if row['update_completed'] else step-1,False)
                    journal.event(item['seed'],factor,row,before_state,after_state,indices)
                    break
""")
    probe = replace_once(probe, "                    and all(bool(torch.isfinite(p).all()) for p in before))",
        "                    and all(bool(torch.isfinite(p).all()) for p in before) and finite_state(before_state['optimizer']))")
    probe = replace_once(probe, "                if any(not bool(torch.isfinite(p).all()) for p in model.parameters()):",
        "                parameters_finite=all(bool(torch.isfinite(p).all()) for p in model.parameters())\n                if not parameters_finite or not finite_state(optimizer.state_dict()):")
    probe = replace_once(probe, "'failure': 'parameters',",
        "'failure': 'parameters' if not parameters_finite else 'optimizer_state',")
    probe = replace_once(probe, "                if step in protocol['evaluation_steps']:",
        "                journal.update(item['seed'],factor,branch['updates'][-1])\n                if step in protocol['evaluation_steps']:")
    probe = replace_once(probe, "                        torch.set_rng_state(saved_rng)\n            # Controls",
        "                        torch.set_rng_state(saved_rng)\n                    journal.commit(slot,capture(model,optimizer,step),branch)\n            # Controls")
    probe = replace_once(probe, "            branch['final_model_sha256'] = None", """            journal.commit(slot,capture(model,optimizer,len(branch['updates']),
                sum(bool(x.get('update_completed')) for x in branch['updates']),
                all(not x['nonfinite'] for x in branch['updates'])),branch)
            branch['final_model_sha256'] = None""")
    probe = replace_once(probe, "'control_curve':branch.get('control_curve'), 'initial_state':branch.get('initial_state'),",
        "'control_curve':branch.get('control_curve'), 'initial_state':branch.get('initial_state'), 'continuation':branch.get('continuation'),")
    probe = probe.replace('Two seeds and 32 local steps', 'One seed and a bounded local horizon')
    probe = replace_once(probe, "{'branches':[compact_branch(b) for b in t['branches']]}",
        "{'zero_update_state':t.get('zero_update_state'), 'branches':[compact_branch(b) for b in t['branches']]}")
    probe = replace_once(probe, "    target = Path('artifacts/controlled_diagnostic.json')",
        "    report['execution_status']='finished'\n    atomic_json(Path('artifacts/diagnostic_progress.json'),report)\n    target = Path('artifacts/controlled_diagnostic.json')")
    probe = replace_once(probe, 'def main():', 'def _main():')
    probe = replace_once(probe, "if __name__ == '__main__':", """def main():
    try:
        return _main()
    except BaseException as error:
        from diagnostic_persistence import interrupted
        interrupted(error)
        raise


if __name__ == '__main__':""")
    verdict = (root / 'controlled_diagnostic_verdicts.py').read_text(encoding='utf-8')
    points = tuple(protocol['evaluation_steps']); steps = protocol['steps']; lower = protocol['lr_factors'][1]
    verdict = replace_once(verdict, 'POINTS = (8, 16, 24, 32)', f'POINTS = {points!r}\nSTEPS = {steps}\nLOWER = {lower!r}\nINTERVAL = {protocol["evaluation_every"]}')
    verdict = verdict.replace('step-8 < s', 'step-INTERVAL < s').replace('len(control)==5', 'len(control)==len(POINTS)+1')
    verdict = verdict.replace('len(steps)==32', 'len(steps)==STEPS').replace('list(range(1,33))', 'list(range(1,STEPS+1))')
    verdict = verdict.replace("len(branch['curve'])==5", "len(branch['curve'])==len(POINTS)+1")
    verdict = verdict.replace('len(hits)>=3', 'len(hits)>=max(3, math.ceil(.75*len(POINTS)))')
    verdict = verdict.replace('branches.get(.25,{})', 'branches.get(LOWER,{})').replace('{1.,.25}', '{1.,LOWER}')
    verdict = replace_once(verdict, "seeds_ok=len(trials)==2 and {t['seed'] for t in trials}=={17,29}",
        f"seeds_ok=len(trials)==1 and trials[0]['seed']=={protocol['seeds'][0]}")
    verdict = verdict.replace("'replicated_nonfinite_across_seeds':bool(replicated_nonfinite)",
        "'single_seed_nonfinite_contrast':bool(replicated_nonfinite)")
    verdict = verdict.replace('Heuristic local contrasts only.', 'Preregistered single-seed local contrasts only; no cross-seed replication.')
    verdict = replace_once(verdict, "    evidence=[]", "    from diagnostic_persistence import verified_nonfinite\n    evidence=[]")
    verdict = replace_once(verdict, "and old_bad[0].get('pre_update_finite') is True and old_bad[0].get('update_completed') is True)",
        "and old_bad[0].get('pre_update_finite') is True and old_bad[0].get('update_completed') is True\n                and verified_nonfinite(old_bad[0],next(b for b in trial['branches'] if b['lr_factor']==1.),\n                    next(b for b in trial['branches'] if b['lr_factor']==LOWER)))")
    compile(probe, 'diagnostic_probe.py', 'exec'); compile(verdict, 'diagnostic_verdicts.py', 'exec')
    return {'diagnostic_probe.py': probe.encode(), 'diagnostic_verdicts.py': verdict.encode(),
        'diagnostic_guard.py': (root / 'controlled_diagnostic_guard.py').read_bytes(),
        'diagnostic_persistence.py': (root / 'causal_diagnostic_persistence.py').read_bytes()}
