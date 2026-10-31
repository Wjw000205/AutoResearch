"""Bounded recovery of authenticated host check-contract failures."""
import copy
import hashlib
import re
from pathlib import Path

from .experiment_workflow import sha
from .storage import now

OLD = '; __rapid_passed.add('
NEW = "; globals()['__rapid_passed'].add("
PATH_OLD = "P.cwd()/'src/dataset_runtime/training.py'"
PATH_NEW = "P.cwd()/'dataset_runtime/training.py'"


def path_contract_failure(record, trial):
    """Recognize only the proven root-vs-src assertion, with staged byte identity."""
    candidate = record.get('candidate') or {}
    if (record.get('patch', {}).get('checks', '').count(PATH_OLD) != 1
            or not trial.get('stderr', '').rstrip().endswith('AssertionError: 实际加载路径不符')
            or PATH_OLD not in trial.get('stderr', '')
            or trial.get('limits', {}).get('code_bundle') != candidate.get('check_bundle')
            or 'dataset_runtime/training.py' not in
                (record.get('decision', {}).get('training_response') or {}).get('module_paths', [])):
        return False
    files = {f['path']: f for f in (candidate.get('check_bundle') or {}).get('files', [])}
    name = 'dataset_runtime/training.py'
    if name not in files or 'src/' + name in files or not trial.get('artifact_path'):
        return False
    directory = Path(trial['artifact_path'])
    actual = directory / name
    try:
        if (not directory.is_absolute() or (directory / ('src/' + name)).exists()
                or any(p.is_symlink() or getattr(p, 'is_junction', lambda: False)()
                       for p in (actual, actual.parent, directory))):
            return False
        data = actual.read_bytes()
        return len(data) == files[name]['bytes'] and hashlib.sha256(data).hexdigest() == files[name]['sha256']
    except OSError:
        return False


def review_scope(record):
    if (record.get('host_check_repair') or {}).get('reason') == 'full_acceptance_budget':
        budget=record['candidate']['check_budget']
        return (f'The user explicitly requested that full CPU acceptance use the task limit of {budget["timeout_seconds"]} seconds instead of the host 60-second smoke-check cap. '
                'Review the new host_check_budget.json and check_execution_limits. This fresh vote must assess that requested time budget; prior 60-second votes are not reused. '
                'The task ceiling and fresh resource admission may only tighten it. CPU remains one core, RAM at most 1024 MiB, disk at most 256 MiB and GPU disabled. '
                'Model, YAML, raw checks, assertions and seeds remain unchanged; script-local guards are retained. '
                'The resource-contract hash is embedded in the launcher, giving this explicitly reviewed retry a new trial identity. The original timed-out receipt and all artifacts remain immutable.')
    if (record.get('host_check_repair') or {}).get('reason') == 'windows_cwd_compatibility':
        return ('Only new rapid CPU checks use a compact plain attempt directory under the same experiment run root: _c/<full 24-hex trial suffix>/<attempt>. '
                'All original directories and failed receipts remain untouched; IDs and paths are stored in each new runner receipt. '
                'The launcher requires the rapid-v1 workspace marker in the actual host environment receipt. '
                'No junction or drive alias is introduced. The worker keeps the same reviewed model, YAML, raw assertions, seeds and resource limits. '
                'The new launcher hash and directory adapter require fresh independent review; existing same-role source reads may be reused only by matching bytes and SHA.')
    if (record.get('host_check_repair') or {}).get('reason') == 'delivery_invocation_receipts':
        return ('The host launcher relocated checks into _delivery_snapshot but omitted params.json and environment.json. '
                'It now verifies the actual runner argv path, code hash and params hash, then exclusively copies the two exact receipt byte strings into that snapshot. '
                'Occupied or linked receipt paths are rejected. The host rereads and compares both copies after worker exit. '
                'This changes only host invocation wiring; model, YAML, raw checks, assertions, seeds and resource limits are unchanged. '
                'The new launcher hash requires fresh independent votes. All original failure receipts and approval decisions remain immutable.')
    if (record.get('host_check_repair') or {}).get('reason') in {'pathlib_api_compatibility','windows_path_compatibility'}:
        return ('Only the CPU-check launcher gains Python 3.10/3.11 Path.is_junction compatibility. '
                'It uses os.lstat().st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT, matching Python 3.12; '
                'symlink/junction rejection remains enabled. Native implementations are untouched. '
                'The launcher uses the same physical working directory with a Win32 extended path so durable atomic writes can exceed MAX_PATH; the host verifier uses extended I/O paths too. '
                'Model, YAML, raw checks, assertions, seeds and resource limits are unchanged. '
                'The new launcher hash requires fresh independent votes; old failure receipts remain immutable.')
    if (record.get('host_check_repair') or {}).get('reason') == 'runtime_module_path_contract':
        return ('Only the expected training module path changes from src/dataset_runtime/training.py '
                'to the manifest path dataset_runtime/training.py under cwd. The failed staged file '
                'matches the reviewed SHA. All assertion conditions except this path operand, model '
                'source and configuration remain unchanged. New check hash requires fresh votes.')
    return 'Only host assertion-counter lookup changed. Raw checks, assertions and model source are unchanged; prior votes do not approve the new check hash.'


def authentic_failed_check_read(engine, run_id, context, call, result):
    """Authenticate a supplementary failure read, never source coverage."""
    if call.get('name') != 'experiments.read':
        return False
    audit = context.get('rapid_revision', {}).get('host_check_repair') or {}
    ref = audit.get('original') or {}
    args = call.get('arguments') or {}
    if args.get('trial_id') != audit.get('failed_trial_id') or not ref.get('id'):
        return False
    archive = engine.store.get('experiment_evidence', ref['id']) or {}
    summary = archive.get('summary') or {}
    trial = summary.get('failed_check') or {}
    original = summary.get('rapid_revision_attempt') or {}
    if (archive.get('run_id') != run_id or sha(summary) != ref.get('sha256')
            or trial.get('run_id') != run_id or trial.get('id') != args.get('trial_id')
            or trial.get('status') not in {'failed','timeout'} or trial.get('params', {}).get('phase') != 'code_check'
            or sha(trial) != original.get('check', {}).get('receipt_sha256')
            or trial.get('code_sha256') != original.get('candidate', {}).get('checks_sha256')
            or engine.store.get('experiments', run_id + ':' + trial['id']) != trial):
        return False
    from .experiment_tools import read
    try:
        return read(engine, run_id, original['cycle'], args) == {
            k: v for k, v in result.items() if k != 'receipt_sha256'}
    except (KeyError, ValueError, TypeError):
        return False


def eligible(engine, run_id, record):
    if record.get('status') != 'check_failed' or record.get('host_check_repair'):
        return False
    saved = record.get('check') or {}
    trial = engine.store.get('experiments', run_id + ':' + saved.get('id', '')) or {}
    return (record.get('run_id') == run_id and record.get('cycle') == engine.cycle(run_id)
        and trial.get('run_id') == run_id and trial.get('status') == 'failed'
        and trial.get('params', {}).get('phase') == 'code_check'
        and sha(trial) == saved.get('receipt_sha256')
        and trial.get('code_sha256') == record.get('candidate', {}).get('checks_sha256')
        and ((OLD in record.get('candidate', {}).get('check_code', '')
            and bool(re.search(r"NameError: name '_\w+__rapid_passed' is not defined", trial.get('stderr', ''))))
            or path_contract_failure(record, trial)))


async def repair(engine, run_id, record):
    from . import rapid_experiment_revision as rapid
    from .experiment_decisions import _archive
    rapid._base_unchanged(engine, run_id, record)
    if not record.get('host_check_repair'):
        if not eligible(engine, run_id, record) or not rapid._reviewed(record):
            raise ValueError('Host counter repair requires the original reviewed candidate and exact failed check')
        context = rapid._review_context(engine, run_id, record)
        for vote in record['votes']:
            rapid.review_receipts(engine, run_id, record, vote, context)
        rapid._candidate_unchanged(record)
        trial = engine.store.get('experiments', run_id + ':' + record['check']['id'])
        ref = _archive(engine, run_id, {'rapid_revision_attempt': copy.deepcopy(record), 'failed_check': trial})
        reason = 'runtime_module_path_contract' if path_contract_failure(record, trial) else 'python_class_name_mangling'
        record['host_check_repair'] = {'original': ref, 'failed_trial_id': trial['id'],
            'failed_receipt_sha256': sha(trial), 'reason': reason, 'created_at': now()}
        record['status'] = 'check_repairing'
        engine.store.put(rapid.COLLECTION, record['id'], record)
    audit = record['host_check_repair']
    archive = engine.store.get('experiment_evidence', audit['original']['id']) or {}
    if archive.get('run_id') != run_id or sha(archive.get('summary')) != audit['original']['sha256']:
        raise ValueError('Original host-check recovery evidence changed')
    trial = engine.store.get('experiments', run_id + ':' + audit['failed_trial_id'])
    if sha(trial) != audit['failed_receipt_sha256']:
        raise ValueError('Original failed host check changed')
    original = archive['summary']['rapid_revision_attempt']
    patch = copy.deepcopy(original['patch'])
    if audit['reason'] == 'runtime_module_path_contract':
        if not path_contract_failure(original, trial):
            raise ValueError('Original module path failure or staged source identity changed')
        patch['checks'] = patch['checks'].replace(PATH_OLD, PATH_NEW, 1)
        expected_code = original['candidate']['check_code'].replace(PATH_OLD, PATH_NEW, 1)
    else:
        expected_code = original['candidate']['check_code'].replace(OLD, NEW)
    candidate = await engine._read_consensus_check(run_id, rapid.build_candidate,
        engine, run_id, record, patch)
    if (candidate['bundle']['sha256'] != original['candidate']['bundle']['sha256']
            or candidate['assertion_count'] != original['candidate']['assertion_count']
            or candidate['check_code'] != expected_code):
        raise ValueError('Host check recovery may only apply the authenticated contract correction')
    rapid._base_unchanged(engine, run_id, record)
    for key in ('check', 'review_reads', 'review_purpose', 'review_clarifications', 'source_read_recovery',
                'review_history', 'error', 'completed_at'):
        record.pop(key, None)
    record.update(patch=patch, candidate=candidate, votes=[], status='reviewing',
        review_purpose='rapid_review:' + candidate['amendment_sha256'] + ':host-check:' + candidate['checks_sha256'])
    audit.update(checks_sha256=candidate['checks_sha256'], completed_at=now())
    engine.store.put(rapid.COLLECTION, record['id'], record)
    engine.store.event(run_id, 'experiment.host_check_repaired',
        '已修正经回执核实的 CPU 检查契约问题；候选源码不变，保留失败回执，新检查须重新复核并执行。',
        {'revision_id': record['id'], 'original': audit['original'], 'checks_sha256': candidate['checks_sha256']})
