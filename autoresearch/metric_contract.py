"""One definition for authored metric bindings, compilation and consumption.

Criterion names are report identities; source_metric names a runner measurement.
This module never infers a scope or a metric from either name. Legacy plans
without this versioned contract retain their original interpretation.
"""
from __future__ import annotations

import copy
import math

from jsonschema import Draft202012Validator

VERSION = 1


class HostScopeRequired(ValueError):
    """The configured performance threshold lacks a unique approved scope."""

TEXT = {'type': 'string', 'minLength': 1}
BINDING_SCHEMA = {
    'type': 'object', 'properties': {
        'source_metric': {'type': 'string', 'pattern': '^val_.+'},
        'task_ids': {'type': 'array', 'items': TEXT, 'uniqueItems': True, 'minItems': 1},
        'reduction': {'enum': ['per_task', 'equal_mean_tasks']},
        'reference_variant': {'enum': ['baseline', 'ablation', 'none']},
        'reference_configurations': {'type': 'array', 'items': {
            'type': 'object', 'properties': {'task_id': TEXT, 'configuration_sha256': {
                'type': 'string', 'pattern': '^[a-f0-9]{64}$'}},
            'required': ['task_id', 'configuration_sha256'], 'additionalProperties': False}},
        'zero_reference': {'enum': ['undefined', 'absolute_noninferiority']},
        'value_bounds': {'type': 'object', 'properties': {
            'minimum': {'type': ['number', 'null']}, 'maximum': {'type': ['number', 'null']}},
            'required': ['minimum', 'maximum'], 'additionalProperties': False},
    }, 'required': ['source_metric', 'task_ids', 'reduction', 'reference_variant',
                   'reference_configurations', 'zero_reference', 'value_bounds'], 'additionalProperties': False,
}
GUIDANCE = (
    'Metric contract version 1: primary_metric/direction select checkpoints and configurations. '
    'Each acceptance criterion metric is a report identity and may differ from its raw measurement. '
    'Its binding explicitly names source_metric, exact task_ids, per_task or equal_mean_tasks reduction, '
    'reference_variant and exact reference_configurations (host inventory SHA256), and zero_reference policy. '
    'Declare value_bounds from the actual metric definition, using null for an unbounded side; signed metrics are allowed. '
    'The host improvement threshold is a separate condition on an explicitly authored primary acceptance scope. '
    'A required local constraint, a matching metric name or uniqueness cannot identify that scope by themselves. '
    'Use reference_variant=none and no reference configurations for descriptive items (required=false). '
    'Never infer scope, direction or a raw metric by parsing a metric name. Preserve complete and all scientific criteria. '
    'Acceptance bindings apply to validation only; held-out metrics cannot select parameters. '
    'Version 1 consumes ordinary scalar runner metrics; program-reported comparison transport needs its own explicit adapter. '
    'For a design not yet compiled, omit binding when host configuration identities are unavailable; '
    'the Harness will compile a separately reviewed binding before implementation.'
)


def signature(value):
    from .experiment_plan import _signature
    return _signature(value)


def tasks(plan):
    return plan.get('task_plans') or [{'task_id': 'single', 'plan': plan}]


def configuration(params):
    return {k: v for k, v in params.items() if k not in ('seed', 'phase', 'variant')}


def inventory(plan):
    from .python_training import jobs
    result = []
    for task in tasks(plan):
        seen = set()
        for stage in ('experiments', 'tuning'):
            for _, params in jobs(task['plan'], stage):
                key = (params['variant'], signature(configuration(params)))
                if key in seen:
                    continue
                seen.add(key)
                result.append({'task_id': task['task_id'], 'variant': key[0],
                               'configuration_sha256': key[1], 'params': configuration(params)})
    return result


def needs_binding(design, plan, acceptance, *, legacy_allowed=False):
    if any('binding' in c for c in acceptance['criteria']):
        return True
    # An explicit legacy adapter remains authoritative for its frozen protocol.
    if plan.get('matrix_acceptance'):
        return False
    if not legacy_allowed:
        return True
    return not any(c['metric'] == design['primary_metric'] and c['direction'] == design['direction']
                   for c in acceptance['criteria'])


def compile_contract(design, plan, acceptance, bindings, *, acceptance_source=None,
                     host_threshold=None, host_primary_criterion=None):
    """Compile an independently authored binding without changing source criteria."""
    if len(bindings) != len(acceptance['criteria']):
        raise ValueError('Every criterion needs exactly one ordered metric binding')
    if acceptance_source is not None:
        if (acceptance_source.get('version') != 1 or acceptance_source.get('kind') != 'protocol_amendment'
                or acceptance_source.get('design_sha256') != signature(design)
                or acceptance_source.get('acceptance') != acceptance):
            raise ValueError('Unknown or inconsistent acceptance source version')
        original = design.get('acceptance') or acceptance_source['parent_acceptance']
        if {k: v for k, v in original.items() if k not in ('complete', 'limitations')} != {
                k: v for k, v in acceptance.items() if k not in ('complete', 'limitations')}:
            raise ValueError('Protocol amendment v1 cannot change quantitative criteria')
    elif design.get('acceptance') is not None and design['acceptance'] != acceptance:
        raise ValueError('Acceptance differs from its approved design source')
    if len({c['metric'] for c in acceptance['criteria']}) != len(acceptance['criteria']):
        raise ValueError('Criterion identities must be unique')
    task_ids = {t['task_id'] for t in tasks(plan)}
    available = {(i['task_id'], i['variant'], i['configuration_sha256']) for i in inventory(plan)}
    for criterion, item in zip(acceptance['criteria'], bindings):
        if item.get('metric') != criterion['metric']:
            raise ValueError('Binding order/criterion identity differs from the approved acceptance')
        binding = {k: v for k, v in item.items() if k != 'metric'}
        Draft202012Validator(BINDING_SCHEMA).validate(binding)
        if 'binding' in criterion and criterion['binding'] != binding:
            fields = sorted(k for k in set(criterion['binding']) | set(binding)
                            if criterion['binding'].get(k) != binding.get(k))
            raise ValueError('Approved criterion.binding conflicts at: ' + ', '.join(fields))
        if not set(binding['task_ids']) <= task_ids:
            raise ValueError('Metric binding names an unknown task')
        bounds = binding['value_bounds']
        if (any(v is not None and not math.isfinite(v) for v in bounds.values())
                or bounds['minimum'] is not None and bounds['maximum'] is not None
                and bounds['minimum'] > bounds['maximum']):
            raise ValueError('Metric value bounds must be finite and ordered')
        refs = binding['reference_configurations']
        if binding['reference_variant'] == 'none':
            if criterion.get('required', True) is not False or refs:
                raise ValueError('A descriptive binding needs explicit required=false and no reference')
        elif (len(refs) != len(binding['task_ids'])
              or {r['task_id'] for r in refs} != set(binding['task_ids'])
              or any((r['task_id'], binding['reference_variant'], r['configuration_sha256'])
                     not in available for r in refs)):
            raise ValueError('Reference must select exactly one approved configuration per scoped task')
        if binding['zero_reference'] == 'absolute_noninferiority' and (
                criterion['minimum_relative_improvement'] != 0
                or criterion.get('comparison', 'at_least') != 'at_least'):
            raise ValueError('Absolute noninferiority at zero requires an inclusive zero threshold')
    host_gate = None
    if host_threshold is not None:
        if type(host_threshold) not in (int, float) or not math.isfinite(host_threshold) or host_threshold < 0:
            raise ValueError('Host performance threshold must be finite and nonnegative')
        eligible = [(c, b) for c, b in zip(acceptance['criteria'], bindings)
                    if c.get('required', True) and b['reference_variant'] != 'none']
        # A required local constraint is not automatically the host's overall
        # performance scope, even when it is the only one using the raw metric.
        matched = [(c, b) for c, b in eligible if c['metric'] == host_primary_criterion]
        if len(matched) != 1:
            raise HostScopeRequired('Host primary threshold scope requires an explicit source clarification')
        c, b = matched[0]
        host_gate = {'version': 1, 'metric': c['metric'], 'source_metric': b['source_metric'],
            'direction': c['direction'], 'task_ids': copy.deepcopy(b['task_ids']),
            'binding_sha256': signature(b), 'minimum_relative_improvement': host_threshold,
            'scope_rule': 'explicit_source'}
    return {'version': VERSION, 'design_sha256': signature(design),
            'base_plan_sha256': signature(plan), 'acceptance_sha256': signature(acceptance),
            'selection_metric': design['primary_metric'], 'selection_direction': design['direction'],
            'bindings': copy.deepcopy(bindings), 'phase': 'validation',
            **({'host_gate': host_gate} if host_gate is not None else {}),
            **({'host_primary_criterion': host_primary_criterion} if host_primary_criterion is not None else {}),
            **({'acceptance_source': copy.deepcopy(acceptance_source)} if acceptance_source is not None else {})}


def base_plan(plan):
    return {k: v for k, v in plan.items() if k not in (
        'metric_contract', 'metric_contract_work_order', 'id', 'run_id', 'cycle', 'design_hash',
        'acceptance', 'iteration_policy', 'protocol_votes', 'created_at', 'protocol_revision_hash',
        'protocol_handoffs', 'protocol_amendments')}


def validate_contract(plan, design):
    contract = plan.get('metric_contract')
    if not contract:
        return
    if contract.get('version') != VERSION:
        raise ValueError('Unsupported metric contract version; Harness contract repair required')
    base = base_plan(plan)
    source = contract.get('acceptance_source')
    original = source['acceptance'] if source is not None else design.get('acceptance') or plan['acceptance']
    validate_acceptance(contract, plan['acceptance'])
    if original != plan['acceptance']:
        raise ValueError('Consumed plan acceptance differs from the approved design source')
    rebuilt = compile_contract(design, base, original, contract['bindings'], acceptance_source=source,
        host_threshold=contract.get('host_gate', {}).get('minimum_relative_improvement'),
        host_primary_criterion=contract.get('host_primary_criterion'))
    if rebuilt != contract:
        raise ValueError('Metric contract no longer matches its design/base plan/acceptance')


def validate_acceptance(contract, acceptance):
    if (signature(acceptance) != contract.get('acceptance_sha256')
            or len(acceptance['criteria']) != len(contract['bindings'])
            or [c['metric'] for c in acceptance['criteria']] != [b['metric'] for b in contract['bindings']]):
        raise ValueError('Consumed acceptance criteria/order/flags differ from the compiled contract')


def runner_criteria(plan, params):
    """Require raw measurements only on their declared validation tasks."""
    if params['phase'] != 'validation':
        return []
    task_id = params.get('benchmark_task_id', 'single')
    return [{'metric': b['source_metric']} for b in plan['metric_contract']['bindings']
            if task_id in b['task_ids']]


def evaluate(plan, trials, summaries, admission=None):
    """Read-only validation view over records admitted by the existing host ledger.

    Admission is produced by experiment_workflow, not by a model response or
    matching pair hashes. The original trial metrics/parameters remain intact.
    """
    from .evaluation import _pair_provenance, _mean
    from .experiment_plan import validation_seeds
    from .performance_policy import outcome
    contract = plan['metric_contract']
    if contract.get('version') != VERSION:
        raise ValueError('Unsupported metric contract version')
    validate_acceptance(contract, plan['acceptance'])
    valid_admission = (admission is not None and admission.get('complete') is True
                       and admission.get('plan_sha256') == signature(plan))
    admitted = {tid for row in (admission or {}).get('jobs', []) if row['state'] == 'completed'
                for tid in row['trial_ids']} if valid_admission else set()
    by_task = {s['task_id']: s for s in summaries}
    checks = []
    for criterion, binding in zip(plan['acceptance']['criteria'], contract['bindings']):
        issues, proposed, reference = [], {}, {}
        refs = {r['task_id']: r['configuration_sha256'] for r in binding['reference_configurations']}
        for task in tasks(plan):
            tid = task['task_id']
            if tid not in binding['task_ids']:
                continue
            best = by_task.get(tid, {}).get('best_proposed') or {}
            winners = set(best.get('trial_ids', []))
            rows = [r for r in trials if r['id'] in admitted and not r.get('superseded')
                    and r.get('status') == 'completed' and r.get('params', {}).get('phase') == 'validation'
                    and r.get('params', {}).get('benchmark_task_id', 'single') == tid]
            for seed in validation_seeds(task['plan']):
                ours = [r for r in rows if r['id'] in winners and r['params']['seed'] == seed]
                controls = [r for r in rows if r['params']['seed'] == seed
                            and r['params']['variant'] == binding['reference_variant']
                            and signature(configuration(r['params'])) == refs.get(tid)]
                pair = ours + (controls if binding['reference_variant'] != 'none' else [])
                if len(ours) != 1 or (binding['reference_variant'] != 'none' and len(controls) != 1):
                    issues.append({'task_id': tid, 'code': 'missing_or_duplicate_admitted_measurement'})
                    continue
                values = [r.get('metrics', {}).get(binding['source_metric']) for r in pair]
                if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
                    issues.append({'task_id': tid, 'code': 'invalid_source_metric'})
                    continue
                bounds = binding['value_bounds']
                if any((bounds['minimum'] is not None and v < bounds['minimum'])
                       or (bounds['maximum'] is not None and v > bounds['maximum']) for v in values):
                    issues.append({'task_id': tid, 'code': 'source_metric_out_of_declared_bounds'})
                    continue
                if controls and _pair_provenance(ours[0], controls[0])[0] != 'consistent':
                    issues.append({'task_id': tid, 'code': 'incompatible_pair_identity'})
                    continue
                proposed.setdefault(tid, []).append(values[0])
                if controls:
                    reference.setdefault(tid, []).append(values[1])
        if not valid_admission:
            issues.append({'code': 'host_execution_ledger_required'})
        groups = [[tid] for tid in binding['task_ids']] if binding['reduction'] == 'per_task' else [binding['task_ids']]
        for group in groups:
            p = _mean([_mean(proposed[t]) for t in group]) if not issues else None
            b = (_mean([_mean(reference[t]) for t in group])
                 if not issues and binding['reference_variant'] != 'none' else None)
            delta = (p - b if criterion['direction'] == 'maximize' else b - p) if b is not None else None
            relative = delta / abs(b) if b else None
            if relative is not None and not math.isfinite(relative):
                relative = None
            passed = None
            if relative is not None:
                threshold = criterion['minimum_relative_improvement']
                passed = relative > threshold if criterion.get('comparison') == 'greater_than' else relative >= threshold
            elif b == 0 and binding['zero_reference'] == 'absolute_noninferiority':
                passed = delta >= 0
            checks.append({**criterion, 'task_ids': group, 'source_metric': binding['source_metric'],
                           'proposed_mean': p, 'baseline_mean': b, 'relative_improvement': relative,
                           'passed': passed, 'issues': copy.deepcopy(issues)})
    complete = plan['acceptance']['complete'] and valid_admission and not any(c['issues'] for c in checks)
    host_check = None
    gate = contract.get('host_gate')
    if gate is not None:
        selected = [c for c in checks if c['metric'] == gate['metric']]
        covered = {t for c in selected for t in c['task_ids']} == set(gate['task_ids'])
        statuses = []
        selected_binding = next(b for b in contract['bindings'] if b['metric'] == gate['metric'])
        for check in selected:
            relative = check['relative_improvement']
            if relative is not None:
                statuses.append(relative >= gate['minimum_relative_improvement'])
            elif (gate['minimum_relative_improvement'] == 0 and check['baseline_mean'] == 0
                  and selected_binding['zero_reference'] == 'absolute_noninferiority'):
                statuses.append(check['passed'])
            else:
                statuses.append(None)
        host_check = {'metric': gate['metric'], 'task_ids': copy.deepcopy(gate['task_ids']),
            'minimum_relative_improvement': gate['minimum_relative_improvement'],
            'complete_scope_coverage': bool(selected) and covered,
            'passed': (False if False in statuses else True if statuses and all(s is True for s in statuses) else None)
                      if covered else None}
    return {'phase': 'validation', 'criteria_complete': complete, 'checks': checks,
            **({'host_primary_check': host_check} if host_check is not None else {}),
            'research_outcome': outcome(checks, complete), 'test_reuse_for_tuning_allowed': False,
            'scope': 'Explicit validation bindings; no scientific novelty or held-out acceptance implied.'}


def decide(summary, reviews, cycle, max_cycles, threshold, policy):
    """Apply the frozen cycle policy to admitted bound criteria, not aliases."""
    from .independent_full_review import members
    if type(cycle) is not int or cycle < 1 or max_cycles is not None and (type(max_cycles) is not int or max_cycles < 1):
        raise ValueError('Invalid research cycle limit')
    if policy is not None and (policy.get('on_validation_failure') not in ('stop', 'revise')
                               or not isinstance(policy.get('rationale'), str) or not policy['rationale'].strip()):
        raise ValueError('Invalid frozen iteration policy')
    targets = (summary.get('proposal_hash'), summary.get('revision_hash'))
    bound = all(isinstance(x, str) and x.strip() for x in targets)
    unanimous = (bound and members(reviews)
                 and all(v.get('approve') is True and not v.get('required_changes')
                         and (v.get('proposal_hash'), v.get('revision_hash')) == targets for v in reviews))
    acceptance = summary['acceptance_checks']
    complete = (acceptance.get('criteria_complete') is True and acceptance.get('phase') == 'validation'
                and bool(acceptance.get('checks')) and all(not c.get('issues') for c in acceptance['checks']))
    if summary.get('benchmark_matrix'):
        complete = complete and all(t.get('plan_complete') and t.get('best_proposed') for t in summary['benchmark_tasks'])
    gate = summary['metric_contract'].get('host_gate')
    host = acceptance.get('host_primary_check') or {}
    host_valid = (gate is not None and gate['minimum_relative_improvement'] == threshold
                  and host.get('minimum_relative_improvement') == threshold
                  and host.get('metric') == gate['metric'] and host.get('task_ids') == gate['task_ids']
                  and host.get('complete_scope_coverage') is True)
    failure = (bound and complete and acceptance.get('research_outcome') == 'negative'
               and any(c.get('required', True) and c.get('passed') is False for c in acceptance['checks'])
               and all(type(c.get('passed')) is bool for c in acceptance['checks'] if c.get('required', True)))
    all_gates_known = all(type(c.get('passed')) is bool for c in acceptance['checks'] if c.get('required', True))
    failure = (bound and complete and host_valid and all_gates_known and type(host.get('passed')) is bool
               and (failure or host['passed'] is False))
    passed = complete and host_valid and host.get('passed') is True and acceptance.get('research_outcome') == 'positive'
    if max_cycles is not None and cycle > max_cycles:
        decision = 'blocked'
    elif passed and unanimous:
        decision = 'finalize'
    elif failure and policy and policy['on_validation_failure'] == 'stop':
        decision = 'stop'
    elif policy and policy['on_validation_failure'] == 'stop' and (
            acceptance.get('research_outcome') == 'negative' or host_valid and host.get('passed') is False):
        decision = 'blocked'
    elif complete and host_valid and type(host.get('passed')) is bool and (max_cycles is None or cycle < max_cycles):
        decision = 'iterate'
    else:
        decision = 'blocked'
    return {'decision': decision, 'cycle': cycle, 'max_cycles': max_cycles, 'threshold_met': bool(passed),
            'unanimous': bool(unanimous), 'proposal_hash': targets[0], 'revision_hash': targets[1],
            'iteration_policy': copy.deepcopy(policy), 'min_relative_improvement': threshold,
            'validation_failure_established': bool(failure), 'policy_applied': decision == 'stop',
            'host_threshold_scope_valid': bool(host_valid),
            **({'validation_outcome': 'negative', 'scope': 'Validation-only descriptive stop, not scientific acceptance.'} if decision == 'stop' else {}),
            'rationale': 'The same compiled criteria govern performance; frozen iteration/cycle and independent review gates remain active.'}
