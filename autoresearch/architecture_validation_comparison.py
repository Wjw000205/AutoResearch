"""Pair an activated revision's validation results with its frozen prior measurements."""
from collections import Counter, defaultdict

from .experiment_workflow import sha


def eligible(row, run_id, binding):
    splits = row.get('evaluation', {}).get('split_fingerprints', {})
    return (row.get('run_id') == run_id and row.get('status') == 'completed' and not row.get('superseded')
            and row.get('params', {}).get('phase') == 'validation' and row.get('evaluation', {}).get('dataset_id')
            and row.get('params_complete') and not row.get('transport_payload_omitted') and row.get('metrics_complete')
            and all(splits.get(part) for part in ('train', 'validation'))
            and (row.get('research_cycle') or 1) == binding['cycle'] and row.get('code_revision') == binding['revision']
            and row.get('code_sha256') == binding['code_sha256'] and row.get('bundle_sha256') == binding['bundle_sha256']
            and row.get('experiment_plan_sha256') == binding['plan_sha256'])


def comparison(store, run_id, program, binding, rows):
    from .experiment_analysis import _paired
    from .rapid_experiment_revision import COLLECTION
    revision = store.get(COLLECTION, program['rapid_revision_id'])
    ref = revision['evidence']
    archive = store.get('experiment_evidence', ref['id'])
    if (not archive or archive['run_id'] != run_id or sha(archive['summary']) != ref['sha256']
            or archive['summary']['binding'] != revision['binding'] or binding['plan_sha256'] != revision['binding']['plan_sha256']):
        raise ValueError('Prior validation comparison lost its frozen revision or plan binding')
    chosen = revision['decision'].get('architecture_review', {}).get('control_trial_ids')
    prior = [r for r in archive['summary']['records'] if eligible(r, run_id, revision['binding'])
             and (chosen is None or r['id'] in chosen)]
    current = [r for r in rows if eligible(r, run_id, binding)]
    def identity(row):
        identity = row['evaluation']
        return sha([row['params'], identity['dataset_id'], *[identity['split_fingerprints'][part] for part in ('train', 'validation')]])
    counts = Counter(identity(r) for r in prior)
    candidates = defaultdict(list)
    for row in current:
        candidates[identity(row)].append(row)
    pairs = []
    for control in prior:
        key = identity(control)
        matches = candidates[key]
        unique = len(matches) == 1 and counts[key] == 1
        pairs.append({'control_trial_id': control['id'], 'candidate_trial_ids': [r['id'] for r in matches],
                      'status': 'paired' if unique else 'ambiguous' if matches else 'unmatched',
                      'measurements': _paired([control], matches) if unique else []})
    return {'control_evidence': ref, 'revision_id': revision['id'], 'pairs': pairs,
            'scope': 'Descriptive prior-versus-current validation under identical recorded parameters and split; no final acceptance.'}
