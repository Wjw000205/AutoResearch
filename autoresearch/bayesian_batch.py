"""Admission, CLI execution and recovery of a complete independently reviewed Bayesian search."""
import asyncio
import json
import math
from pathlib import Path
import subprocess
import sys

from . import combination_validation as combos
from . import bayesian_script_tool as artifacts
from .experiment_workflow import sha
from .storage import now


def approved(engine, run_id, evidence):
    from . import tuning_scope, combination_settings
    scope = evidence['tuning_scope']
    record = engine.store.get(tuning_scope.COLLECTION, scope['id']) or {}
    if (record.get('status') != 'accepted' or record.get('binding') != evidence['binding']
            or record.get('plan_sha256') != scope['plan_sha256'] or tuning_scope.verify(engine, run_id, record) != scope['plan']):
        raise ValueError('Dataset script requires its original author and three independently approved scope reads')
    settings = combination_settings.verified(engine, run_id, evidence)
    progress = evidence['combination_validation']
    if progress.get('version') != 3 or settings != progress['settings']:
        raise ValueError('Batch Bayesian settings changed')
    row = engine.store.get(combos.PLANS, progress['plan_id']) or {}
    body = row['body']
    source = engine.store.get('experiment_evidence', body['source_evidence']['id']) or {}
    if (row.get('run_id') != run_id or row['sha256'] != sha(body) or row['sha256'] != progress['plan_sha256']
            or row['id'] != combos._key(run_id, evidence) or source.get('run_id') != run_id
            or sha(source.get('summary')) != body['source_evidence']['sha256']
            or combos._plan(source['summary']) != body['plan']
            or body['plan']['settings_sha256'] != sha(settings) or body['plan']['binding'] != evidence['binding']
            or body['plan']['scope_sha256'] != scope['plan_sha256']):
        raise ValueError('Reviewed Bayesian plan does not match the current dataset, script and budget')
    return row, artifacts.verified(engine, run_id, evidence, settings)


def frontier(plan, evidence, *, choose=None):
    from .scripted_tuning import bayesian_progress
    body = plan['body']['plan']
    controls = combos._controls(evidence, body['validation_seed'], body['task_id'], body['metric'], body['direction'])
    return bayesian_progress(body, evidence, plan['sha256'], controls, choose=choose)


def check_gates(evidence):
    if (evidence['dataset_optimization']['phase'] != 'parameters'
            or evidence.get('failure_categories') or evidence.get('training_investigation', {}).get('required')
            or evidence.get('iteration_policy', {}).get('on_validation_failure') == 'stop'
            or any(f['disposition'] == 'requires_implementation' for f in evidence['tuning_scope']['plan']['factors'])):
        raise ValueError('Bayesian batch needs resolved architecture, execution and training gates')


def verify_unit(engine, run_id, body):
    from .hyperparameter_yaml import validate_candidate
    from .parameter_search import propose
    admission = body['script_admission']
    archive = engine.store.get('experiment_evidence', admission['evidence']['id']) or {}
    evidence = archive.get('summary')
    if (archive.get('run_id') != run_id or sha(evidence) != admission['evidence']['sha256']
            or evidence['binding'] != body['binding']):
        raise ValueError('Bayesian admission is detached from its validation receipts')
    check_gates(evidence)
    plan, script = approved(engine, run_id, evidence)
    progress = evidence['combination_validation']
    if (admission['plan_id'] != plan['id'] or admission['script_id'] != script['id']
            or frontier(plan, evidence)['next_step'] != progress['next_step'] or not progress['next_step']):
        raise ValueError('Bayesian admission does not reproduce the approved script selection')
    spec = admission['point']
    unit = propose(spec, evidence)
    if sha(unit) != sha(body['unit']):
        raise ValueError('Bayesian training jobs differ from the script-selected point')
    if evidence.get('hyperparameter_yaml', {}).get('ready'):
        validate_candidate(engine, run_id, spec, evidence, unit)
    return unit


def register(engine, run_id, evidence, state, plan, script):
    from .experiment_decisions import _archive, _pending_batch, SESSIONS
    from .rapid_experiment_revision import _budget
    from .parameter_search import COLLECTION, propose
    from .scripted_tuning import draft
    ref = _archive(engine, run_id, evidence)
    spec = draft(engine, run_id, evidence, ref)
    unit = propose(spec, evidence)
    admission = {'plan_id': plan['id'], 'script_id': script['id'], 'evidence': ref, 'point': spec}
    body = {'binding': evidence['binding'], 'script_admission': admission, 'unit': unit}
    verify_unit(engine, run_id, body)
    _budget(engine, run_id, unit['admitted_trials'])
    key = run_id + ':' + unit['id']
    row = {'id': key, 'run_id': run_id, 'body': body, 'sha256': sha(body), 'scientific_approval': False, 'created_at': now()}
    pending = _pending_batch(engine, run_id, unit, None)
    pending.update(authorization='approved_bayesian_script', script_batch_id=key, script_id=script['id'])
    updated = {**state, 'pending': pending}
    engine.check_active(run_id)
    engine.store.batch(puts=[(COLLECTION, key, row), (SESSIONS, state['id'], updated)],
                       expected=[(COLLECTION, key, None), (SESSIONS, state['id'], state)])
    state.update(updated)


def invocation(engine, run_id, state, name, params):
    from .parameter_search import _authorized, _current_binding, COLLECTION
    from .engine import ResearchBlocked
    pending = state['pending']
    row = engine.store.get(COLLECTION, pending['script_batch_id']) or {}
    if (not row or row.get('id') != run_id + ':' + pending['unit_id']
            or row['body']['script_admission']['script_id'] != pending.get('script_id')):
        raise ResearchBlocked('Bayesian invocation lost its admitted manifest')
    unit = _authorized(engine, run_id, row, _current_binding(engine, run_id, engine.experiment_plan(run_id)))
    if sha(unit['jobs']) != sha(pending['jobs']) or dict(unit['jobs']).get(name) != params:
        raise ResearchBlocked('Bayesian invocation differs from its approved jobs')
    return True


def ready(engine, run_id, evidence):
    from .engine import ResearchBlocked
    combo = evidence.get('combination_validation', {})
    if combo.get('version') != 3:
        return False
    if combo.get('status') in ('waiting_trial_budget', 'waiting_measured_batch'):
        raise ResearchBlocked('贝叶斯批次等待预算或上一个点的权威测量回执，不能重复派发。')
    if combo.get('status') != 'validating_combinations':
        return False
    if evidence.get('failure_categories') or evidence.get('training_investigation', {}).get('required'):
        return False  # Return a real failure or alert to the normal diagnostic workflow.
    check_gates(evidence)
    return True


def cli_request(engine, run_id, args):
    from .experiment_decisions import snapshot, session
    from .comparison_transport import runtime_transport
    run = engine.store.get('runs', run_id)
    if set(args) != {'script_id'}:
        raise ValueError('Supply the exact reviewed script ID')
    for saved in engine.store.list('bayesian_search_results', run_id):
        if saved['body'].get('script_id') == args['script_id']:
            if saved.get('sha256') != sha(saved['body']):
                raise ValueError('Archived search result changed')
            return {'status': 'completed', 'run_status': run['status'], 'result': saved['body']}
    if (run['stage'] != 'tuning'
            or run['status'] not in ('paused', 'running') or not session(engine, run_id)):
        raise ValueError('The requested dataset script is not in its launchable tuning stage')
    evidence = snapshot(engine, run_id, runtime_transport(engine, run_id))
    current = evidence.get('combination_validation', {})
    if current.get('version') != 3 or not current.get('plan_id') or not current.get('settings'):
        raise ValueError('This dataset has no approved Bayesian batch')
    plan, script = approved(engine, run_id, evidence)
    if script['id'] != args['script_id']:
        raise ValueError('Only the current approved script can start this search')
    pending = session(engine, run_id).get('pending')
    if pending and pending.get('script_id') != script['id']:
        raise ValueError('Another operation already owns this validation session')
    if not (pending and pending.get('script_id') == script['id']) and not ready(engine, run_id, evidence):
        raise ValueError('The approved script has no executable batch')
    return {'status': 'admitted', 'run_status': run['status'], 'script_id': script['id'], 'plan_id': plan['id']}


async def execute_tool(harness, run_id, role, owner, args):
    from .tool_authorization import authorize
    from .experiment_decisions import session, snapshot, _execute
    from .comparison_transport import runtime_transport
    authorize(harness, run_id, role, owner, artifacts.START, args)
    engine = harness.engine
    state = session(engine, run_id)
    if role != 'engineer' or engine.store.get('runs', run_id)['stage'] != 'tuning' or not state or state.get('pending'):
        raise ValueError('Batch launch requires an idle, authorized tuning session')
    transport = await engine._read_consensus_check(run_id, runtime_transport, engine, run_id)
    evidence = await engine._read_consensus_check(run_id, snapshot, engine, run_id, transport)
    if not ready(engine, run_id, evidence):
        raise ValueError('No approved search point is ready for this dataset')
    plan, script = approved(engine, run_id, evidence)
    if args != {'script_id': script['id'], 'plan_id': plan['id']}:
        raise ValueError('Search command targets a different script or dataset')
    path = Path(script['body']['path'])
    command = [sys.executable, '-I', '-B', '-u', str(path), '--search', '--stdio']
    engine.store.event(run_id, 'experiment.bayesian_search_started', '工程角色已通过命令行启动三方批准的贝叶斯脚本。',
                       {**args, 'command': command, 'file_sha256': script['body']['file_sha256']})
    process = await asyncio.create_subprocess_exec(*command, cwd=str(path.parent.parent),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        while ready(engine, run_id, evidence):
            combo = evidence['combination_validation']
            if combo['plan_sha256'] != plan['sha256']:
                raise ValueError('Bayesian plan changed during the batch')
            await run_selection(process, plan, evidence)
            engine.check_active(run_id)
            register(engine, run_id, evidence, state, plan, script)
            await _execute(engine, harness, run_id, state, transport)
            if state['last_batch']['status'] != 'completed':
                raise ValueError('Bayesian search stopped at a failed training receipt')
            evidence = await engine._read_consensus_check(run_id, snapshot, engine, run_id, transport)
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
    combo = evidence['combination_validation']
    if combo.get('status') != 'complete' and (evidence.get('failure_categories') or evidence.get('training_investigation', {}).get('required')):
        return {**args, 'status': 'needs_diagnosis', 'scientific_approval': False}
    return finish(engine, run_id, evidence, plan, script)


def finish(engine, run_id, evidence, plan, script):
    combo = evidence['combination_validation']
    pair = combo.get('final_pair') or {}
    if combo['status'] != 'complete' or not pair.get('unit_id'):
        raise ValueError('Bayesian search ended without a verified measured selection')
    controls = combos._controls(evidence, combo['validation_seed'], combo['task_id'], combo['metric'], combo['direction'])
    variants = ('proposed',) if evidence.get('baseline_mode') == 'paper_reported' else ('baseline','proposed')
    winners = {variant: controls[pair[variant + '_unit_id']][variant] for variant in variants}
    result = {'script_id': script['id'], 'plan_id': plan['id'], 'scope': 'Best measured validation configurations within the approved search',
              'stop_reason': combo['stop_reason'], 'validation_seed': combo['validation_seed'],
              'metric': combo['metric'], 'direction': combo['direction'], 'pair': pair,
              'parameters': {v: w['params'] for v, w in winners.items()},
              'validation_scores': {v: w['score'] for v, w in winners.items()}, 'scientific_approval': False}
    row = {'id': plan['id'], 'run_id': run_id, 'body': result, 'sha256': sha(result), 'created_at': now()}
    saved = engine.store.mutate('bayesian_search_results', plan['id'], lambda old: old or row)
    if saved['body'] != result or saved['sha256'] != sha(result):
        raise ValueError('Saved Bayesian result differs from the current validation receipts')
    return result


async def run_selection(process, plan, evidence):
    packet = {}
    def capture(options, completed, budget, expected):
        packet.update(candidates=options, completed=float(completed), budget=float(budget))
        return expected
    progress = await asyncio.to_thread(frontier, plan, evidence, choose=capture)
    if not packet or progress['next_step'] != evidence['combination_validation']['next_step']:
        raise ValueError('Validation frontier changed before CLI selection')
    from .bayesian_policy import select
    expected = select(plan['body']['plan']['selection_script'], packet['candidates'], packet['completed'], packet['budget'])
    async with asyncio.timeout(60):
        process.stdin.write((json.dumps(packet, allow_nan=False) + '\n').encode())
        await process.stdin.drain()
        reply = json.loads(await process.stdout.readline())
    if type(reply) not in (float, int) or not math.isfinite(reply) or reply != expected:
        raise ValueError('CLI did not return the point selected by the reviewed script')
