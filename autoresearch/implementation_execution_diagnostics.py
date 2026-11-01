"""Carry real code-check failures into recovery without copying full run output."""
from __future__ import annotations

import copy
import json


def context(store, run_id, cycle, review):
    if (review.get('review_type') != 'execution_failure'
            or review.get('failure_stage') != 'implementation'):
        return None
    workflow_id = review.get('failure', {}).get('details', {}).get('workflow_id')
    workflow = store.get('codex_workflows', workflow_id) if workflow_id else None
    if (not workflow or workflow.get('run_id') != run_id or workflow.get('cycle') != cycle
            or workflow.get('proposal_hash') != review.get('proposal_hash')):
        return None
    attempt = next((a for a in reversed(workflow.get('attempts', []))
                    if a.get('acceptance', {}).get('smoke')), None)
    if not attempt:
        return None
    checks = []
    for smoke in attempt['acceptance']['smoke']:
        key = f"{run_id}:{smoke.get('id')}"
        check = store.get('codex_checks', key)
        if (not check or check.get('run_id') != run_id or check.get('id') != smoke.get('id')
                or any(check.get(k) != smoke.get(k) for k in ('status', 'stdout', 'params', 'exit_code'))):
            continue  # No unbound diagnostic can acquire a current identity.
        failure = None
        for line in reversed(check.get('stdout', '').splitlines()):
            try:
                output = json.loads(line)
            except ValueError:
                continue
            evaluation = output.get('evaluation') if isinstance(output, dict) else None
            if isinstance(evaluation, dict) and isinstance(evaluation.get('failure'), dict):
                failure = copy.deepcopy(evaluation['failure'])
                break
        limits = check.get('limits', {})
        checks.append({'check_id': key, 'status': check['status'], 'exit_code': check.get('exit_code'),
            'variant': check.get('params', {}).get('variant'),
            'error': check.get('error'), 'reported_failure': failure,
            'limits': {k: limits[k] for k in ('memory_mb', 'gpu_memory_mb', 'timeout_seconds', 'cpu_cores') if k in limits},
            'peak_memory_mb': check.get('peak_memory_mb'),
            'scope': 'Actual local code acceptance execution; not a scientific measurement or verified causal diagnosis.'})
    return {'workflow_id': workflow_id, 'attempt_number': attempt['index'] + 1,
        'delivery_files_hash': attempt.get('snapshot', {}).get('files_hash'), 'checks': checks,
        'scientific_failure_established': False,
        'policy': 'Execution failure, missing metrics after a failed process, and unchanged code are not '
                  'scientific falsification. Diagnose the recorded runtime/resource failure before changing '
                  'the method. Preserve dataset, splits, baselines and thresholds; any changed plan still '
                  'requires fresh independent approval and the unchanged execution gates.'}
