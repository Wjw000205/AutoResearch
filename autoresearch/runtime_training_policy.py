"""Explicit, reviewed operational amendments; historical scientific approvals stay intact."""
import hashlib
import json
from pathlib import Path

COLLECTION = 'runtime_training_policies'
MODE = 'observed_resources_without_timing'
BINDINGS = ('delivery_id', 'delivery_hash', 'code_sha256', 'spec_hash', 'implementation_document_sha256')
HOST_FILES = {'autoresearch/runtime_training_policy.py', 'autoresearch/codex_workflow.py',
              'autoresearch/engine.py', 'autoresearch/lab.py'}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_record(store, run_id, record, program, *, files=True):
    """Authenticate the exact amendment, independent votes and executed source."""
    policy = record.get('policy', {})
    if (record.get('run_id') != run_id or policy.get('run_id') != run_id
            or record.get('sha256') != digest(policy) or record.get('approved') is not True
            or policy.get('mode') != MODE or not isinstance(policy.get('caps'), dict)
            or set(policy['caps']) != {'memory_mb', 'disk_mb', 'cpu_cores', 'gpu_memory_mb'}
            or any(type(v) is not int or v <= 0 for v in policy['caps'].values())
            or set(policy.get('host_files', {})) != HOST_FILES
            or not isinstance(policy.get('authorization'), str) or not policy['authorization'].strip()
            or policy.get('bindings') != {k: program.get(k) for k in BINDINGS}
            or policy.get('scientific_success') is not False):
        raise ValueError('Runtime training policy does not match this approved release')
    votes = record.get('votes', [])
    if len(votes) != 3 or {v.get('role') for v in votes} != {'director', 'engineer', 'scout'}:
        raise ValueError('Runtime training policy needs three independent reviews')
    for vote in votes:
        result = vote.get('result', {})
        transcript = store.get('transcripts', vote.get('transcript_id', '')) or {}
        if (result.get('policy_sha256') != record['sha256'] or result.get('approve') is not True
                or result.get('issues') != [] or result.get('scientific_success') is not False
                or transcript.get('run_id') != run_id or transcript.get('role') != vote['role']
                or transcript.get('request_id') != vote.get('request_id')
                or transcript.get('request_scope', {}).get('purpose') != 'code_repair_audit:' + record.get('review_packet_sha256', '')
                or json.loads(transcript.get('text', '{}')) != result):
            raise ValueError('Runtime training policy review evidence is incomplete')
    base, effective = policy['base_bundle'], policy['bundle']
    old = {r['path']: r for r in base['files']}
    new = {r['path']: r for r in effective['files']}
    changed = {p for p in old.keys() | new.keys() if old.get(p) != new.get(p)}
    if set(old) != set(new) or ('changed_files' in policy and changed != set(policy['changed_files'])):
        raise ValueError('Runtime amendment changes files outside its reviewed scope')
    from .code_bundle import digest as bundle_digest, validate
    if base['sha256'] != bundle_digest(base['files']) or effective['sha256'] != bundle_digest(effective['files']):
        raise ValueError('Runtime amendment bundle identity changed')
    if files:
        validate(base, program['code'])
        validate(effective, program['code'])
        root = Path(__file__).resolve().parents[1]
        for name, expected in policy['host_files'].items():
            if not (root / name).resolve().is_relative_to(root) or file_hash(root / name) != expected:
                raise ValueError('Reviewed host runtime changed: ' + name)
    return policy


def active(store, run_id, program=None, *, files=True):
    run = store.get('runs', run_id) or {}
    key = run.get('state', {}).get('runtime_training_policy')
    if not key:
        return None
    if program is None:
        program = store.get('programs', run_id) or (store.get('steps', run_id + ':implementation') or {}).get('output', {})
    record = store.get(COLLECTION, key) or {}
    from .rapid_continuation import validated
    continuation = validated(store, run_id)
    if continuation:
        base = continuation['sources']['program']
        policy = validate_record(store, run_id, record, base, files=False)
        # New host bytes have explicit, SHA-bound operator authority. The old
        # policy/votes stay immutable; their exact source bundles still verify.
        if files:
            from .code_bundle import validate
            validate(policy['base_bundle'], base['code'])
            validate(policy['bundle'], base['code'])
        if program.get('rapid_revision_id'):
            from .rapid_experiment_revision import runtime_bundle
            bundle = runtime_bundle(store, run_id, program, base, continuation['policy_amendment']['plan_sha256'])
            policy = {**policy, 'bundle': bundle}
        elif program != base:
            raise ValueError('Rapid continuation cannot authorize an unrelated base program')
        return {**policy, 'id': continuation['id'],
                'sha256': digest({'original_policy': record['sha256'],
                    'continuation': continuation['sha256'], 'bundle': policy['bundle']['sha256']})}
    policy = validate_record(store, run_id, record, program, files=files)
    return {**policy, 'id': key, 'sha256': record['sha256']}


def apply_limits(store, run_id, limits, program):
    policy = active(store, run_id, program)
    if policy is None:
        return
    if limits.get('code_bundle') != policy['bundle'] or limits.get('execution_mode') != 'local':
        raise ValueError('Runtime training policy requires its reviewed local bundle')
    for key, cap in policy['caps'].items():
        limits[key] = min(limits[key], cap)
    limits.update(timeout_seconds=None, cpu_seconds=None,
                  runtime_training_policy={'id': policy['id'], 'sha256': policy['sha256'],
                      'mode': MODE, 'bundle_sha256': policy['bundle']['sha256']})


def runner_authorized(store, run_id, limits):
    """A nullable timeout cannot be supplied through an unreviewed caller."""
    marker = limits.get('runtime_training_policy')
    if marker is None:
        return False
    policy = active(store, run_id)
    expected = ({'id': policy['id'], 'sha256': policy['sha256'], 'mode': MODE,
                 'bundle_sha256': policy['bundle']['sha256']} if policy else None)
    if (marker != expected or limits.get('code_bundle') != policy['bundle']
            or limits.get('timeout_seconds') is not None or limits.get('cpu_seconds') is not None
            or limits.get('execution_mode') != 'local'
            or any(type(limits.get(k)) not in (int, float) or not 0 < limits[k] <= cap for k, cap in policy['caps'].items())):
        raise ValueError('Runtime training policy limits or bindings changed')
    return True
