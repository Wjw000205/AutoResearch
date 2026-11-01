"""Read-only facts about existing planner and runner limits, before approval."""
from __future__ import annotations

import hashlib
import json

from .experiment_requirements import SEED_GUIDANCE


REPAIR_ATTEMPT_ALLOWANCE = 4
RULES = ('The task primary improvement threshold is a hard additional gate; a weaker proposal threshold cannot override it. '
    'Reserve every approved baseline/proposed/ablation candidate plus final paired trials; never silently drop grid points. '
    'Predeclare whether failed validation requires another revision or stops this method family and reports the negative evidence.')


def trial_accounting(config, dataset_budget=None):
    """Describe enforced semantics; this neither grants execution nor raises caps."""
    if dataset_budget and dataset_budget.get('status') == 'unlimited':
        return {'version': 4, 'scope': 'all_task_datasets', 'dataset_budget': dataset_budget,
            'attempt_limit': None, 'completion_rule': dataset_budget['completion_rule'],
            'counting_unit': 'one recorded runner invocation; counts are informational',
            'grants_experiment_authorization': False, 'internal_validation_grid_search_allowed': False,
            'required_gates': ['independent configuration reviews', 'code and evaluation contract',
                'current resource admission', 'task provider call and cost limits', 'per-batch decision']}
    if dataset_budget and dataset_budget.get('scope') == 'per_dataset_task':
        return {'version': 3, 'scope': 'per_dataset_task', 'dataset_budget': dataset_budget,
            'counting_unit': 'one runner invocation with a frozen variant, phase, seed and configuration',
            'budget_rule': 'Derive counts from this dataset task, review the configuration and budget together, then freeze. '
                'Checks, failures and retries consume this dataset allowance; other datasets have separate allowances.',
            'internal_validation_grid_search_allowed': False, 'grants_experiment_authorization': False,
            'required_gates': ['independent configuration and budget reviews', 'code and evaluation contract',
                'current resource admission', 'task provider call and cost limits', 'current dataset attempt limit']}
    return {'version': 2, 'source': 'experiment_plan.build_plan and Engine._trial',
        'max_planned_trials_per_cycle': config['max_trials'],
        'counting_unit': 'one runner invocation with one frozen variant, phase, model seed and concrete parameter configuration',
        'minimum_plan_formula': '2*N+K+2',
        'formula_terms': {'N': 'every shared grid point/model-seed combination, executed for both baseline and proposed',
            'K': 'every declared ablation configuration/model-seed combination', '2': 'one held-out baseline/proposed pair'},
        'model_seed_contract': {'validation_field': 'DESIGN.validation_seeds',
            'final_test_field': 'DESIGN.final_test_seeds', 'replication': 'Cartesian product with every validation and ablation configuration',
            'legacy_validation_seed': 17, 'legacy_final_seed_formula': '101*(index+1)',
            'explicit_final_seeds_preserved': True, 'total_plan_formula': '2*N+K+2*T',
            'T': 'number of paired final_test_seeds',
            'selection': 'All declared validation model seeds contribute; never select the most favorable individual seed.'},
        'additional_capacity': 'New plans must explicitly declare validation_seeds and final_test_seeds, each with one seed by default. '
            'Only an explicit user task requirement permits multiple seeds; spare capacity never justifies replication. '
            'Legacy frozen plans retain their original accounting, including implicit final pairs. An explicit list is never extended or shortened.',
        'internal_validation_grid_search_allowed': False,
        'named_ablation_contract': {
            'selector': 'An exact {variant: name} entry selects one fully expanded ablation_params.configurations[name] object with an explicit algorithm.',
            'runner_fields': 'variant remains ablation; ablation_configuration records the selected scientific configuration name.',
            'scope': 'Each invocation consumes only that selected configuration, inherited common parameters and contracts; never execute the entire catalogue inside one invocation.',
            'replication': 'Each named configuration is executed for every frozen validation model seed and counted separately in K.',
            'invalid': 'Unknown names, unresolved references, conflicting control fields or duplicate effective configurations are rejected.'},
        'fitting_scope': 'Training iterations and parameter estimation for the fixed recorded algorithm are part of its invocation. '
            'Selecting among additional hyperparameter configurations with validation scores inside that invocation is not supported; '
            'declare configurations in the complete experiment plan rather than hide them in a composite trial.',
        'max_runner_attempts_per_cycle': config['max_trials'] + REPAIR_ATTEMPT_ALLOWANCE,
        'repair_allowance': REPAIR_ATTEMPT_ALLOWANCE,
        'repair_scope': 'The separate attempt ceiling includes repairs and interrupted attempts; it does not enlarge the approved parameter grid.',
        'resource_assessment_scope': 'Single resource-plan admission does not approve the complete grid, scientific comparison or experiment execution.',
        'grants_experiment_authorization': False,
        'required_gates': ['exact-proposal unanimous consensus', 'complete committed parameter plan',
            'code and evaluation contract', 'current resource admission', 'current call and experiment limits']}


def constraints(config, previous=None, dataset_budget=None):
    """Refresh limits for unfrozen work without mutating previous assignments."""
    result = {**(previous or {}),
        'minimum_primary_relative_improvement': config.get('min_relative_improvement', .01),
        'max_trials_per_cycle': config['max_trials'],
        'max_research_cycles': config.get('max_research_cycles', 3),
        'per_trial_seconds': config['experiment_timeout'],
        'rules': RULES, 'new_experiment_requirements': SEED_GUIDANCE,
        'trial_accounting': trial_accounting(config, dataset_budget)}
    if dataset_budget and (dataset_budget.get('scope') == 'per_dataset_task' or dataset_budget.get('status') == 'unlimited'):
        result.pop('max_trials_per_cycle', None)
        result['dataset_budget'] = dataset_budget
    return result


def host_budget_snapshot(store, run_id, proposal, proposal_hash, cycle):
    """Bind current control facts to an exact proposal, without reserving work."""
    encoded = json.dumps(proposal, sort_keys=True, ensure_ascii=False).encode()
    if hashlib.sha256(encoded).hexdigest() != proposal_hash:
        raise ValueError('Host budget snapshot requires the exact proposal hash')
    run = store.get('runs', run_id)
    from .research_iteration import effective_config
    config = effective_config(store, run_id, run['config'])
    budget = store.get('budgets', run_id)
    used = budget.get('calls') if isinstance(budget, dict) else None
    count = lambda value: value if type(value) is int and value >= 0 else None
    used, maximum = count(used), count(config.get('max_calls'))
    total_cycles, current_cycle = count(config.get('max_research_cycles')), count(cycle)
    cpu = count(proposal.get('resource_plan', {}).get('cpu_seconds'))
    return {'version': 1, 'run_id': run_id, 'proposal_hash': proposal_hash,
        'source': 'current run config, budgets.calls and enforced planner/runner contract',
        'status': 'platform_limits_reported', 'trial_accounting': trial_accounting(config),
        'call_budget_snapshot': {'source': 'budgets.calls', 'used': used, 'maximum': maximum,
            'remaining': max(0, maximum - used) if used is not None and maximum is not None else None,
            **({'unlimited': True} if 'max_calls' in config and config['max_calls'] is None else {}),
            'scope': 'Frozen observation at assignment creation, not a reservation; actual admission rechecks the live ledger.'},
        'research_cycles': {'current': current_cycle, 'maximum': total_cycles,
            'unlimited': total_cycles is None,
            'additional_available': max(0, total_cycles - current_cycle)
                if total_cycles is not None and current_cycle is not None else None},
        'planned_cycle_upper_bounds': {'runner_invocations': config['max_trials'],
            'wall_seconds': config['max_trials'] * config['experiment_timeout'],
            'requested_cpu_seconds': config['max_trials'] * cpu if cpu is not None else None,
            'scope': 'Derived bounds for the configured planned invocations; separate repair attempts are described in trial_accounting. '
                'No independent whole-cycle wall/CPU ceiling or extra quota approval was configured here.'},
        'lifecycle': 'Consensus permits proceeding to design. Commit the complete concrete parameter plan before execution; '
            'do not demand experiment code or measured timings before proposal consensus. '
            'These facts settle the configured counting rules, not compatibility of an incomplete matrix with that plan.',
        'grants_experiment_authorization': False, 'scientific_claims_verified': False}


def _review_identity(record):
    value = {key: record.get(key) for key in ('round', 'proposal_hash', 'votes', 'discussion_progress')}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _refresh_key(engine, run_id):
    from .discussion_actions import scope
    return f'{run_id}:host-control-review:{scope(engine, run_id)}:1'


def _interrupted_revision(engine, run_id, record):
    """Bind an exhausted, read-back interrupted revision to this old review.

    This does not continue the failed generation. It identifies only a stopped
    exact-proposal revision whose missing platform facts can reach one fresh
    independent review while its native request and all paid receipts remain.
    """
    from .engine import digest
    from .intelligence import Intelligence
    from .native_normal_recovery import _history, _prepared, _target, _source, _fresh_generation
    store = engine.store
    purpose = f'revise:{record["round"]}'
    with store._db() as db:
        keys = [row['key'] for row in db.execute(
            "SELECT key FROM records WHERE collection='handoff_progress' AND json_extract(data,'$.run_id')=? "
            "AND json_extract(data,'$.normal_request.prepared.role')='director' "
            "AND json_extract(data,'$.normal_request.prepared.context._purpose')=?", (run_id, purpose))]
    if len(keys) != 1:
        return None
    key = keys[0]
    progress = store.get('handoff_progress', key)
    normal = progress.get('normal_request', {})
    history = _history(progress)
    handoff = store.get('handoffs', key) or {}
    if (progress.get('mode') == 'final_only' or history is None or len(history[0]) != 1
            or handoff.get('stage') != 'debate' or handoff.get('purpose') != purpose
            or handoff.get('status') != 'blocked' or normal.get('status') != 'blocked'
            or normal.get('provider_id') != 'local_codex' or not normal.get('native_interruption_of')
            or normal.get('turn') != progress.get('turn')):
        return None
    context = normal.get('prepared', {}).get('context', {})
    if (context.get('proposal') != record['proposal'] or context.get('proposal_hash') != record['proposal_hash']
            or context.get('votes') != record['votes']):
        return None
    previous, audit = history[0][0], history[1][0]
    identity = object.__new__(Intelligence)  # Only stateless operation identity is needed; no settings initialization.
    try:
        source = _source(store, identity, run_id, normal)
        prior_source = _source(store, identity, run_id, previous)
        if (source is None or prior_source is None
                or source['status'] != 'interrupted' or prior_source['status'] != 'interrupted'):
            return None
        native = store.get('codex_reasoning', source['native_key'])
        cp = native['checkpoint']
        failure = cp.get('first_failure', {})
        generation = store.get('operator_resumes', audit.get('resume_generation')) or {}
        target = _target(previous, progress['turn'])
        expected = _prepared(previous, normal['operation_id'])
        if (target is None or audit.get('source') != prior_source or audit.get('target') != target
                or audit.get('new_operation_id') != normal['operation_id']
                or normal['native_interruption_of'] != previous['operation_id']
                or normal['prepared'] != expected or audit.get('prepared_sha256') != digest(expected)
                or audit.get('continuation_number') != 1 or audit.get('continuation_limit') != 1
                or generation.get('kind') != 'explicit_start_command' or generation.get('run_id') != run_id
                or generation.get('targets', {}).get(key) != target
                or not _fresh_generation(generation, store.get('codex_reasoning', prior_source['native_key']))
                or failure.get('error_code') != 'codex_operation_timeout'
                or any(failure.get(k) != cp.get(k) for k in ('thread_id', 'turn_id'))):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return {'handoff_id': key, 'turn': progress['turn'], 'prepared_sha256': digest(normal['prepared']),
        'source': source, 'prior_source': prior_source, 'source_progress_sha256': digest(progress)}


def prepare_control_review(engine, run_id):
    """Preview one legacy control-contract upgrade during explicit resume only."""
    from .discussion_actions import enabled, resource_only_work
    from .storage import now
    store = engine.store
    run = store.get('runs', run_id)
    if (not enabled(run) or run['status'] not in {'paused', 'blocked'} or run.get('stage') != 'debate'
            or store.list('experiments', run_id)):
        return None
    key = _refresh_key(engine, run_id)
    if store.get('stage_contexts', key):
        return None
    record = store.get('consensus', run_id) or {}
    votes = record.get('votes') or []
    rejected = [v for v in votes if not v.get('approve') or v.get('required_changes')]
    if (record.get('unanimous')
            or {v.get('role') for v in votes} != {'director', 'engineer', 'scout'}
            or len(votes) != 3 or any(v.get('proposal_hash') != record.get('proposal_hash') for v in votes)
            or not rejected or not all(resource_only_work(v) for v in rejected)):
        return None
    prefix = f'{run_id}:{engine.cycle(run_id)}:{record["round"]}:{record["proposal_hash"]}'
    inputs = [row for row in store.list('debate_inputs', run_id) if row['id'].startswith(prefix)]
    if len(inputs) != 1:
        return None  # Do not guess which paid input is authoritative.
    frozen = inputs[0]
    context = frozen['context']
    if (context.get('proposal_hash') != record['proposal_hash'] or context.get('proposal') != record['proposal']
            or isinstance(context.get('execution_constraints', {}).get('host_budget_review'), dict)
                and context['execution_constraints']['host_budget_review'].get('status') == 'platform_limits_reported'):
        return None
    facts = host_budget_snapshot(store, run_id, record['proposal'], record['proposal_hash'], engine.cycle(run_id))
    remaining = facts['call_budget_snapshot']['remaining']
    if (remaining is None and not (facts['call_budget_snapshot'].get('unlimited')
            and facts['call_budget_snapshot']['used'] is not None)) or (remaining is not None and remaining < 3):
        return None
    interrupted = None
    if not record.get('discussion_progress', {}).get('stalled'):
        if run['status'] != 'blocked':
            return None
        interrupted = _interrupted_revision(engine, run_id, record)
        if interrupted is None:
            return None
    return {'id': key, 'run_id': run_id, 'kind': 'host_control_review', 'version': 1,
        'trigger': 'interrupted_revision_missing_control_facts' if interrupted else 'legacy_control_stall',
        **({'interrupted_revision': interrupted} if interrupted else {}),
        'from_round': record['round'], 'next_round': record['round'] + 1,
        'proposal_hash': record['proposal_hash'], 'original_review_sha256': _review_identity(record),
        'original_input_id': frozen['id'],
        'original_context_sha256': hashlib.sha256(json.dumps(context, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        'control_facts': facts, 'created_at': now(),
        'scope': 'One fresh independent review of the same proposal after previously missing platform control facts. '
            'No old input, vote, paid assignment, scientific evidence, limit or stagnation counter is replaced.',
        'grants_experiment_authorization': False, 'scientific_claims_verified': False}


def control_review_available(engine, run_id, record):
    """Replay the exact old checkpoint into its one successor; never reset stall."""
    store = engine.store
    audit = store.get('stage_contexts', _refresh_key(engine, run_id))
    if (not audit or audit.get('from_round') != record['round']
            or audit.get('proposal_hash') != record['proposal_hash']
            or audit.get('original_review_sha256') != _review_identity(record)):
        return False
    generation = store.get('operator_resumes', audit.get('operator_resume_generation', '')) or {}
    if generation.get('kind') != 'explicit_start_command' or generation.get('run_id') != run_id:
        return False
    frozen = store.get('debate_inputs', audit['original_input_id']) or {}
    context_hash = hashlib.sha256(json.dumps(frozen.get('context'), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if context_hash != audit['original_context_sha256']:
        from .engine import ResearchBlocked
        raise ResearchBlocked('Original control-review input changed; no successor review dispatched')
    if audit.get('interrupted_revision') is not None and _interrupted_revision(engine, run_id, record) != audit['interrupted_revision']:
        from .engine import ResearchBlocked
        raise ResearchBlocked('Original interrupted revision changed; no successor review dispatched')
    if not audit.get('successor_started_at'):
        from .storage import now
        audit['successor_started_at'] = now()
        store.put('stage_contexts', audit['id'], audit)
        store.event(run_id, 'discussion.control_facts_review',
            '按显式恢复补充平台计数事实，对同一提案进行一次新独立复审；原评议、异议和停滞记录保留。',
            {'audit_id': audit['id'], 'from_round': audit['from_round'], 'next_round': audit['next_round'],
                'proposal_hash': audit['proposal_hash']})
    return True
