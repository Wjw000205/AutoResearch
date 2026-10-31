"""Durable diagnostic subworkflow; never a completed tuning/review stage."""
from .storage import now
from .experiment_workflow import sha

COLLECTION = 'tuning_diagnostics'


def handoff_stage_matches(store, run, handoff):
    """An audited routing correction does not rewrite a frozen assignment."""
    if handoff.get('stage') == run.get('stage'):
        return True
    if run.get('stage') != 'tuning' or handoff.get('stage') != 'revision':
        return False
    cycle = run.get('state', {}).get('research_cycle', 1)
    record = store.get(COLLECTION, f'{run["id"]}:{cycle}') or {}
    from .experiment_decisions import _approved
    from .training_investigation import scoped_repair
    decision = store.get('experiment_decisions', record.get('decision_id', '')) or {}
    inputs = store.get('handoff_inputs', handoff.get('id', '')) or {}
    return (record.get('status') == 'running' and record.get('run_id') == run['id']
        and handoff.get('run_id') == run['id'] and handoff.get('to_role') == 'engineer'
        and _approved(decision) and decision.get('cycle') == cycle
        and decision.get('run_id') == run['id'] and scoped_repair(decision['proposal'])
        and handoff.get('purpose') == 'rapid_implement:' + decision['proposal_sha256']
        and inputs.get('context', {}).get('rapid_revision', {}).get('decision_sha256') == decision['proposal_sha256']
        and inputs.get('context', {}).get('validation_evidence') == decision['evidence'])


def current(engine, run_id):
    return engine.store.get(COLLECTION, f'{run_id}:{engine.cycle(run_id)}')


def begin(engine, run_id, review, step):
    from .experiment_decisions import revision_context
    from .training_investigation import scoped_repair
    decision = revision_context(engine, run_id, review)
    if not decision or not scoped_repair(decision['proposal']):
        raise ValueError('Only approved training diagnostics belong to this subworkflow')
    key = f'{run_id}:{engine.cycle(run_id)}'
    old = current(engine, run_id)
    if old and old.get('decision_id') == decision['decision_id']:
        return old
    record = {'id': key, 'run_id': run_id, 'cycle': engine.cycle(run_id),
        'decision_id': decision['decision_id'], 'review': review, 'status': 'running',
        'created_at': now(), 'scientific_approval': False, 'plan_sha256': sha(engine.experiment_plan(run_id)),
        **({'graph_thread_id': old['graph_thread_id']} if old and old.get('graph_thread_id') else {})}
    archive = key + ':diagnostic-entry:' + sha(step)
    puts = [(COLLECTION, key, record), ('cycle_steps', archive, {**step, 'id': archive}),
        ('steps', run_id + ':tuning', {**step, 'id': run_id + ':tuning', 'stage': 'tuning',
            'status': 'running', 'completed_at': None, 'next_node': 'tuning',
            'output': {'activity': 'diagnostic_repair', 'decision_id': decision['decision_id'],
                       'semantic_completion': False}})]
    if old:
        archive = key + ':diagnostic-history:' + sha(old)
        puts.append((COLLECTION, archive, {**old, 'id': archive, 'archived': True}))
    engine.store.batch(puts)
    engine.store.event(run_id, 'tuning.diagnostic_started', '参数微调内诊断补修，尚未完成选参或验证评审',
                       {'decision_id': decision['decision_id']})
    return record


def graph_entry(engine, run_id):
    """Migrate only the old incorrect route, preserving paid requests verbatim."""
    run = engine.store.get('runs', run_id)
    existing = current(engine, run_id)
    if existing and existing.get('graph_thread_id') and existing.get('plan_sha256') == sha(engine.experiment_plan(run_id)):
        return (existing['graph_thread_id'], 'tuning')
    if run.get('stage') != 'revision':
        return None
    review = engine.store.get('research_cycles', f'{run_id}:{engine.cycle(run_id)}') or {}
    if review.get('failure_stage') != 'tuning':
        return None
    from .experiment_decisions import revision_context
    from .training_investigation import scoped_repair
    decision = revision_context(engine, run_id, review)
    if not decision or not scoped_repair(decision['proposal']):
        return None
    # Once an old amendment actually activated, it must keep its original route.
    rapid = engine.store.get('rapid_experiment_revisions', run_id + ':rapid:' + sha(decision['decision_id'])) or {}
    if rapid.get('status') == 'activated':
        return None
    step = engine.store.get('steps', run_id + ':tuning') or {'started_at': now()}
    record = begin(engine, run_id, review, step)
    record['graph_thread_id'] = run_id + ':tuning-diagnostic:' + sha(decision['decision_id'])
    engine.store.put(COLLECTION, record['id'], record)
    revision = engine.store.get('steps', run_id + ':revision')
    if revision:
        archive = record['id'] + ':legacy-revision:' + sha(revision)
        engine.store.batch([('cycle_steps', archive, {**revision, 'id': archive})],
                           [('steps', run_id + ':revision')])
    engine.update_run(run_id, stage='tuning')
    return (record['graph_thread_id'], 'tuning')


async def execute(engine, harness, run_id):
    from .bounded_diagnostics import execute as bounded_diagnostic
    bounded = await bounded_diagnostic(engine, harness, run_id)
    if bounded:
        return bounded
    from .fixed_state_diagnostic import execute as fixed_state_diagnostic
    fixed_state = await fixed_state_diagnostic(engine, harness, run_id)
    if fixed_state:
        return fixed_state
    from .causal_training_followup import execute as causal_followup
    causal = await causal_followup(engine, harness, run_id)
    if causal:
        return causal
    from .controlled_training_checks import execute as controlled_checks
    controlled = await controlled_checks(engine, harness, run_id)
    if controlled:
        return controlled
    record = current(engine, run_id)
    if not record or record['status'] != 'running':
        return None
    step = engine.store.get('steps', run_id + ':tuning') or {}
    engine.store.put('steps', run_id + ':tuning', {**step, 'output': {
        'activity': 'diagnostic_repair', 'semantic_completion': False, 'decision_id': record['decision_id']}})
    from .rapid_experiment_revision import run
    result = await run(engine, harness, run_id, record['review'])
    record.update(status='finished', result=result, completed_at=now())
    engine.store.put(COLLECTION, record['id'], record)
    # No stage completion event: CPU checking is not parameter search completion.
    return {**result, 'next_node': 'tuning', 'tuning_pending': True, 'activity': 'diagnostic_repair'}
