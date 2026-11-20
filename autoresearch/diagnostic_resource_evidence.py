"""Reviewable limits for the Windows continuation with a reserved saving window."""
import hashlib
import inspect
import json
from pathlib import Path

from .bounded_diagnostic_contract import CAPS
from .experiment_workflow import sha


def effective(assessed, requested, reviewed):
    if not assessed.get('approved'):
        raise ValueError('Diagnostic resource assessment rejected: ' + str(assessed.get('reasons')))
    result = {}
    for key, ceiling in CAPS.items():
        values = [assessed['limits'][key], requested[key], reviewed[key], ceiling]
        if any(type(v) not in (int, float) or not 0 < v or not float(v).is_integer() for v in values):
            raise ValueError('Missing finite positive diagnostic resource limit: ' + key)
        result[key] = int(min(values))
    if result['timeout_seconds'] <= 120:
        raise ValueError('No compute time remains after reserving 120 seconds for saving')
    return {**result, 'gpu_allowed': False, 'gpu_memory_mb': 0, 'execution_mode': 'local',
            'diagnostic_save_reserve_seconds': 120}


def capture(engine, rid, record):
    import os
    from . import lab, bounded_diagnostics, bounded_diagnostic_inputs, code_bundle, runtime_training_policy
    from . import diagnostic_supervisor, diagnostic_review_issues, storage, bounded_review_reading, controlled_training_checks, bounded_evidence_review
    from .training_adjustment import _basis
    from .python_environments import apply_execution_preferences
    config = engine.store.get('runs', rid)['config']
    if os.name != 'nt' or config.get('execution_mode', 'local') != 'local':
        raise ValueError('This resource evidence requires the local Windows runner')
    plan = _basis(engine, rid)['design']['resource_plan']
    required = sorted(set(plan.get('required_packages', []) + record['base_program'].get('dependencies', [])))
    assessed = lab.assess_resources({**plan, 'required_packages': required},
                                    apply_execution_preferences(config, lab.hardware_profile()))
    limits = effective(assessed, CAPS, CAPS)
    run = inspect.getsource(lab.ExperimentRunner.run)
    def excerpt(start, end):
        return run[run.index(start):run.index(end, run.index(start))]
    sources = {Path(m.__file__).name: hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest()
               for m in (lab, bounded_diagnostics, bounded_diagnostic_inputs, code_bundle, runtime_training_policy,
                         diagnostic_supervisor, diagnostic_review_issues, storage, bounded_review_reading, controlled_training_checks, bounded_evidence_review)}
    sources[Path(__file__).name] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    proof = {'supervised_closeout':1, 'shared_cpu_job':1, 'terminal_commit_protocol':2,
        'plan_sha256': record['plan_sha256'], 'binding': record['binding'], 'assessed_at': assessed['hardware_checked_at'],
        'assessed_limits': {k: assessed['limits'][k] for k in CAPS}, 'review_ceiling': limits,
        'compute_seconds': limits['timeout_seconds'] - 120, 'max_selected_input_bytes': 256 * 1024**2,
        'source_sha256': sources,
        'scope': 'Pre-dispatch assessment. Dispatch takes the minimum of reviewed, candidate and freshly assessed limits. '
                 'One Windows job owns the runner tree and the one-core seal process. Its cumulative user+kernel '
                 'counter includes exited members. A watchdog retains a CPU margin; the seal wall window is also '
                 'limited by remaining CPU seconds. The shared counter is checked inside the SQLite transaction '
                 'before COMMIT. The prepared receipt becomes authoritative only after the supervisor final checks '
                 'and the guarded publication transaction; no post-publication resource rejection rewrites that decision. '
                 'RAM has an OS job limit. Directory bytes and wall '
                 'time use an external watchdog with an 80 ms sleep between checks, not a filesystem quota. '
                 'The independent Windows supervisor gates stdin after assigning the whole runner tree to a kill-on-close job. '
                 'One monotonic clock covers preparation, compute, process wait, output, disk scans, SQLite and exit. '
                 'Closeout is bounded by min(start + wall limit, compute stop + reserve), with termination headroom. '
                 'Seal stages a prepared confirmation while canonical rows remain incomplete. Final CPU, wall and '
                 'cancellation checks precede a guarded atomic publication. Publication admission is the linearization '
                 'boundary: cancellation admitted earlier prevents publication; later cancellation observes its transaction '
                 'outcome. Lost publication acknowledgements require exact-receipt reconciliation, never a training retry. '
                 'Durable timing records final-check values; return_elapsed_seconds also observes publication and exit. '
                 'An unavailable disk can prevent a new write; the dispatch claim and last committed progress remain incomplete.',
        'source': {'limit_intersection': inspect.getsource(effective), 'admission_and_dispatch': inspect.getsource(bounded_diagnostics.execute_once),
            'selected_input_check': inspect.getsource(bounded_diagnostic_inputs.selected),
            'windows_job': inspect.getsource(lab._WindowsJob), 'termination': inspect.getsource(lab.ExperimentRunner._terminate),
            'environment': inspect.getsource(lab.ExperimentRunner._environment), 'bootstrap': lab._BOOTSTRAP,
            'normalization': excerpt('        from .runtime_training_policy', '        key = f'),
            'compute_clock': excerpt('            bootstrap =', "            if normalized.get('execution_interface')"),
            'startup_gate': excerpt('                if os.name == "nt" and mode == "local":','                output_size ='),
            'watchdog': excerpt('                reason, peak_memory', '                    parsed = None'),
            'failure_archive': run[run.index('            except asyncio.CancelledError:\n                if process:'):],
            'independent_deadline_and_commit': inspect.getsource(diagnostic_supervisor),
            'atomic_commit_guard':inspect.getsource(storage.Store.batch)}}
    receipt = engine.store.path.parent / 'diagnostics' / ('bounded-runner-' + sources['lab.py'] + '.json')
    if receipt.is_file():
        checks = json.loads(receipt.read_bytes())
        if (checks.get('runner_sha256') != sources['lab.py'] or checks.get('sha256') != sha(checks.get('body'))
                or checks['body'].get('scope') != 'offline_windows_resource_probes'
                or not all(checks['body'].get('cases', {}).get(k, {}).get('passed') is True
                           for k in ('clock','cpu','disk','memory','invalid_reserve','normal'))):
            raise ValueError('Offline Windows resource probe receipt does not match the current runner')
        proof['offline_checks'] = checks
    evidence_path = engine.store.path.parent / 'diagnostics' / ('bounded-closeout-' + sources['diagnostic_supervisor.py'] + '.json')
    if evidence_path.exists():
        checks = json.loads(evidence_path.read_bytes())
        if (checks.get('sha256') != sha(checks['body']) or checks['body']['runner_sha256'] != sources['lab.py']
                or checks['body']['supervisor_sha256'] != sources['diagnostic_supervisor.py']
                or checks['body']['probe_sha256'] != hashlib.sha256((Path(__file__).resolve().parent.parent /
                    'scripts' / 'verify_bounded_closeout.py').read_bytes()).hexdigest()
                or not all(checks['body']['cases'].get(k,{}).get('passed') is True for k in
                           ('normal','slow_disk','blocked_store','blocked_seal','save_failure','cancel','blocked_wait',
                            'cpu_near','cpu_exhausted','cpu_meter_failure','after_stage_meter','after_stage_cpu',
                            'after_stage_clock','after_stage_cancel','publish_failure','publish_lost_ack'))):
            raise ValueError('Invalid supervised closeout fault-test evidence')
        proof['closeout_checks'] = checks
    return {**proof, 'sha256': sha(proof)}


def verify(proof):
    if sha({k:v for k,v in proof.items() if k != 'sha256'}) != proof['sha256']:
        raise ValueError('Reviewed diagnostic enforcement evidence changed')
    for name, digest in proof['source_sha256'].items():
        if Path(name).name != name or hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() != digest:
            raise ValueError('Reviewed diagnostic enforcement source changed: ' + name)
