"""Validation execution ledger and failure routing, separate from scientific approval.

The ledger is reconstructed from saved receipts, never model declarations. It
does not launch, retry, prune, enlarge a search or change an execution budget.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import math
import re


PROTOCOL = 'experiment_workflow_protocol'
TOOLS = ('experiments.status', 'experiments.read', 'experiments.compare', 'experiments.record_notes',
         'experiments.analyze', 'experiments.inspect_module', 'experiments.cleanup')


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def sha(value):
    return hashlib.sha256(encoded(value).encode('utf-8')).hexdigest()


def failure(record):
    """Conservative routing hints. Pattern matches are not verified root causes."""
    status = record.get('status')
    if status == 'completed':
        return None
    reported = None
    phase = (record.get('params') or {}).get('phase')
    if phase == 'test' or any(k.startswith('test_') for k in (record.get('metrics') or {})):
        category, action = 'held_out_failure', 'audit_without_test_feedback'
    elif status in {'running', 'interrupted', 'cancelled'}:
        category, action = 'execution_state', 'reconcile_process_and_saved_attempt'
    elif status in {'timeout', 'memory_limit', 'cpu_limit', 'disk_limit', 'output_limit'}:
        category, action = status, 'review_resources_without_relaxing_limits'
    else:
        from .execution_diagnostics import failure_diagnostic
        reported = failure_diagnostic(record)
        text = (str(record.get('error') or '') + '\n' + str(record.get('stderr') or '')[-8000:]
                + '\n' + encoded(reported)).lower()
        if record.get('evaluation_errors'):
            category, action = 'evaluation_contract', 'repair_contract_with_independent_review'
        elif any(word in text for word in ('modulenotfounderror', 'importerror', 'dll load failed', 'no module named')):
            category, action = 'environment', 'verify_declared_environment'
        elif any(word in text for word in ('connectionerror', 'connection refused', 'urlerror', 'http error',
                                         'filenotfounderror', 'checksum mismatch', 'dataset not found')):
            category, action = 'data_or_io', 'verify_data_and_io_evidence'
        elif any(word in text for word in ('out of memory', 'cuda error', 'no space left', 'disk full',
                                          'timing_coverage_insufficient', 'resource_admission')):
            category, action = 'resource_failure', 'review_resources_without_relaxing_limits'
        elif re.search(r'\bnan\b', text) or any(word in text for word in ('floatingpointerror', 'nonfinite', 'non-finite',
                                         'singular matrix', 'overflowerror')):
            category, action = 'numerical', 'review_training_evidence'
        elif 'last stdout line must' in text:
            category, action = 'output_contract', 'repair_contract_with_independent_review'
        else:
            category, action = 'runtime_error', 'inspect_code_and_bounded_diagnostics'
    return {'category': category, 'next_action': action, 'status': status,
        **({'reported_failure': reported} if reported else {}),
        'root_cause_verified': False, 'automatic_retry_authorized': False,
        'method_change_authorized': False, 'scientific_negative_result': False}


def validation_records(store, run_id):
    """Read bounded columns for ALL validation records, without raw output/arrays.

    Unlike the UI's recent-256 window this cannot silently omit an old baseline.
    Oversized fields are explicit missing evidence and fail the coverage gate.
    """
    return _records(store, run_id, validation_only=True)


def report_records(store, run_id):
    """All phases for reporting only; never expose held-out rows to tuning."""
    return _records(store, run_id, validation_only=False)


def _records(store, run_id, *, validation_only):
    from .experiment_views import _projection
    with store._read_db() as db:
        heads = db.execute("SELECT key,updated_at FROM records WHERE collection='experiments' "
            "AND json_extract(data,'$.run_id')=? "
            + ("AND json_extract(data,'$.params.phase')='validation' " if validation_only else '') + "ORDER BY key",
            (run_id,)).fetchall()
    rows = []
    for key, updated in heads:
        row = _projection(store, key, updated)
        if row is None:
            raise ValueError('Experiment evidence changed during ledger read; refresh before deciding')
        rows.append(row)
    return rows


def coverage(plan, trials, *, cycle, revision, code_sha256=None, strict=False, primary_metric=None, additional_jobs=()):
    """Require every planned config/seed/ablation exactly once on this revision."""
    from .python_training import jobs
    from .comparison_transport import scientific_params
    expected = [(name, params) for stage in ('experiments', 'tuning') for name, params in jobs(plan, stage)]
    expected.extend(additional_jobs)
    by_params = defaultdict(list)
    extras = []
    expected_keys = {sha(params) for _, params in expected}
    for trial in trials:
        if trial.get('superseded') or (trial.get('params') or {}).get('phase') != 'validation':
            continue
        if (trial.get('research_cycle') or 1) != cycle:
            continue
        params = scientific_params(trial)
        # The database projection removes only the transport envelope; the
        # runner has already validated its binding before accepting the result.
        key = sha(params)
        by_params[key].append(trial)
        if key not in expected_keys:
            extras.append(trial['id'])
    rows, counts, identities = [], Counter(), {}
    primary = primary_metric or plan.get('primary_metric')
    for name, params in expected:
        matches = by_params.get(sha(params), [])
        reasons = []
        if len(matches) != 1:
            state = 'missing' if not matches else 'duplicate'
        else:
            trial = matches[0]
            state = trial.get('status') or 'unknown'
            if state == 'completed':
                if trial.get('code_revision') != revision:
                    reasons.append('code_revision_mismatch')
                if trial.get('experiment_plan_sha256') != sha(plan) and (strict or trial.get('experiment_plan_sha256')):
                    reasons.append('plan_binding_mismatch')
                if code_sha256 and trial.get('code_sha256') != code_sha256 and (strict or trial.get('code_sha256')):
                    reasons.append('code_binding_mismatch')
                if trial.get('params_complete') is False or trial.get('metrics_complete') in (False, 0):
                    reasons.append('bounded_projection_incomplete')
                if trial.get('evaluation_errors') or trial.get('evaluation_errors_complete') in (False, 0):
                    reasons.append('evaluation_contract_failed')
                if primary:
                    value = (trial.get('metrics') or {}).get(primary)
                    if type(value) not in (int, float) or not math.isfinite(value):
                        reasons.append('primary_metric_missing')
                if strict and not (trial.get('evaluation') or {}).get('dataset_id'):
                    reasons.append('dataset_identity_missing')
                evidence = trial.get('evaluation') or {}
                splits = evidence.get('split_fingerprints') or {}
                identity = (evidence.get('dataset_id'), splits.get('train'), splits.get('validation'))
                if strict and not all(identity):
                    reasons.append('split_identity_missing')
                task = params.get('benchmark_task_id')
                if all(identity):
                    if task in identities and identities[task] != identity:
                        reasons.append('dataset_or_split_drift')
                    else:
                        identities[task] = identity
                if reasons:
                    state = 'invalid'
        counts[state] += 1
        rows.append({'name': name, 'variant': params.get('variant'), 'seed': params.get('seed'),
            'task_id': params.get('benchmark_task_id'), 'params_sha256': sha(params),
            'state': state, 'trial_ids': [t['id'] for t in matches], 'issues': reasons})
    return {'plan_sha256': sha(plan), 'research_cycle': cycle, 'code_revision': revision,
        'expected': len(rows), 'counts': dict(counts), 'complete': bool(rows) and counts['completed'] == len(rows) and not extras,
        'unexpected_trial_ids': extras, 'jobs': rows, 'scientific_approval': False,
        'scope': 'All frozen validation configurations, seeds and ablations on one code revision.'}


def ledger(engine, run_id, *, plan=None, rows=None):
    from .training_adjustment import _basis
    from .python_training import code_hash
    basis = _basis(engine, run_id)
    plan = plan or basis['plan']
    if not plan:
        raise ValueError('A frozen experiment plan is required')
    program = basis['program'] or {}
    rows = validation_records(engine.store, run_id) if rows is None else rows
    run = engine.store.get('runs', run_id)
    current_sha = code_hash(program) if program.get('code') else None
    # Older saved executions predate the explicit code hash column. Resolve the
    # real saved environment; never infer success from a revision label alone.
    if run['mode'] == 'live' and current_sha:
        from .experiment_views import _json_file
        for row in rows:
            if not row.get('code_sha256') and row.get('status') == 'completed':
                environment, _ = _json_file(engine, run_id, row, 'environment.json', 1024 * 1024)
                row['code_sha256'] = (environment or {}).get('code_sha256')
    from .parameter_search import jobs as search_jobs
    return coverage(plan, rows, cycle=engine.cycle(run_id), revision=program.get('revision', 0), additional_jobs=search_jobs(engine, run_id, plan),
                    code_sha256=current_sha, strict=run['mode'] == 'live', primary_metric=basis['design']['primary_metric'])


def require_complete(engine, run_id, *, plan=None):
    from .engine import ResearchBlocked
    result = ledger(engine, run_id, plan=plan)
    if not result['complete']:
        raise ResearchBlocked('验证实验台账未闭合，不能选参或进入留出集：' + encoded({
            'counts': result['counts'], 'expected': result['expected'],
            'unexpected_trial_count': len(result['unexpected_trial_ids']),
            'inspect_with': 'experiments.status'}))
    return result


def compact_summary(summary):
    """Bound repeated inventories; exact full evidence is available by hash."""
    result = {k: v for k, v in summary.items() if k not in {
        'groups', 'comparisons', 'pairs', 'benchmark_tasks', 'issues', 'excluded_trial_ids'}}
    issues = summary.get('issues') or []
    result['issue_counts'] = dict(Counter(str(i.get('code', 'unknown')) for i in issues))
    result['issue_count'] = len(issues)
    provenance = result.get('provenance')
    if isinstance(provenance, dict):
        result['provenance'] = {k: v for k, v in provenance.items() if k not in {'included_trial_ids', 'excluded_trial_ids'}}
        for key in ('included_trial_ids', 'excluded_trial_ids'):
            result['provenance'][key + '_count'] = len(provenance.get(key, []))
    for field in ('best_baseline', 'best_proposed'):
        group = result.get(field)
        if group and len(encoded(group.get('params', {}))) > 2000:
            result[field] = {k: v for k, v in group.items() if k != 'params'}
            result[field]['params_reference'] = {'path': '/' + field + '/params', 'sha256': sha(group['params'])}
    for field in ('groups', 'comparisons', 'pairs', 'benchmark_tasks', 'excluded_trial_ids'):
        if field in summary:
            result[field + '_count'] = len(summary[field])
    # Matrix acceptance checks contain per-task tables. Keep the result and
    # counts, retrieve the exact task/check through experiments.compare.
    checks = result.get('acceptance_checks')
    if checks:
        result['acceptance_checks'] = {k: v for k, v in checks.items() if k not in {'checks', 'benchmark_tasks'}}
        result['acceptance_checks']['check_count'] = len(checks.get('checks', []))
        result['acceptance_checks']['failed_check_count'] = sum(c.get('passed') is not True for c in checks.get('checks', []) if c.get('required') is not False)
        result['acceptance_checks']['checks'] = checks.get('checks', [])[:8]
        result['acceptance_checks']['checks_truncated'] = len(checks.get('checks', [])) > 8
    return result


def review_packet(engine, run_id, summary, context):
    """Archive once, then send a small decision packet, not all trial history."""
    from .training_context import review
    from .storage import now
    selection = context.get('selection') or {}
    evidence = {**summary, 'selection': selection}
    digest = sha(evidence)
    identifier = run_id + ':validation:' + digest
    record = {'id': identifier, 'run_id': run_id, 'sha256': digest,
              'summary': evidence, 'created_at': now(), 'provenance': 'host_computed_validation_summary'}
    def immutable(old):
        if old and (old.get('sha256') != digest or sha(old['summary']) != digest):
            raise ValueError('Validation evidence archive changed')
        return old or record
    engine.store.mutate('experiment_evidence', identifier, immutable)
    result = review(context)
    result.update({k: context[k] for k in ('proposal_hash', 'revision_hash',
        'min_relative_improvement', 'loss_diagnostics') if k in context})
    result['selection'] = {k: v for k, v in selection.items() if k not in {'params', 'baseline_params', 'benchmark_tasks'}}
    result['selection'].update(params_sha256=sha(selection.get('params')),
        baseline_params_sha256=sha(selection.get('baseline_params')),
        task_count=len(selection.get('benchmark_tasks', [])), full_selection_path='/selection')
    result.update({PROTOCOL: 1, 'validation_summary': compact_summary(summary),
        'validation_evidence': {'id': identifier, 'sha256': digest,
            'read_with': 'experiments.compare', 'scope': 'Exact frozen validation review evidence; no held-out results.'}})
    return result
