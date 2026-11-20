"""Local readback of a completed delivery rejected only by a retired presentation limit."""
import asyncio
import hashlib
import json

from jsonschema import ValidationError

from .experiment_workflow import sha
from .storage import now


def inspect(engine, run_id, saved):
    from . import dataset_codex as native
    from .dataset_experiments import _base_bundle, _source_gate
    ref = saved.get('previous_setup') or {}
    if not ref or saved.get('codex_job_id') or saved.get('draft') or not saved.get('previous_codex_job_id'):
        return None
    archive = engine.store.get('experiment_evidence', ref['id']) or {}
    history = archive.get('summary', {})
    failure, old = history.get('failure', {}), history.get('setup', {})
    issues = failure.get('details', {}).get('issues', [])
    if (failure.get('kind') != 'codex_delivery' or len(issues) != 1 or not
            (issues[0] == 'Configuration delivery file exceeds its bounded size: rationale.txt'
             or issues[0].startswith('/adapter_files: maxItems=8; actual_length='))):
        return None
    key = saved['previous_codex_job_id']
    job = engine.store.get(native.COLLECTION, key) or {}
    cp = job.get('checkpoint', {})
    if (sha(history) != ref['sha256'] or archive.get('run_id') != run_id or old.get('id') != saved['id']
            or old.get('codex_job_id') != key or failure['details'].get('job_id') != key
            or job.get('setup_id') != saved['id'] or job.get('run_id') != run_id or job.get('id') != key
            or cp.get('status') != 'completed' or cp.get('run_id') != run_id or cp.get('operation_id') != key
            or cp.get('instruction_sha256') != hashlib.sha256(job['instruction'].encode()).hexdigest()
            or job['identity'].get('request_sha256') != sha(old['request'])
            or not cp.get('thread_id') or not cp.get('turn_id') or job.get('draft')):
        raise ValueError('Original native delivery receipt cannot be authenticated')
    request_id = job['reservation']['id']
    usage = engine.store.get('usage', request_id) or {}
    budget = engine.store.get('budgets', run_id) or {}
    if (usage.get('operation_id') != key or usage.get('status') != 'completed' or usage.get('run_id') != run_id
            or usage.get('role') != 'engineer' or usage.get('provider') != 'local_codex'
            or [r for r in budget.get('records', []) if r.get('operation_id') == key] != [usage]
            or any(r.get('operation_id') == key for r in budget.get('pending', {}).values())):
        raise ValueError('Native delivery does not have one settled original request')
    bundle = _base_bundle(engine, run_id, _source_gate(engine, run_id))
    if bundle['sha256'] != job['identity']['source_bundle_sha256']:
        raise ValueError('Native delivery is for a different source version')
    files = job['partial_delivery']
    legacy = {'configuration_json': files['_experiment_delivery/configuration.json'],
              'checks': files['_experiment_delivery/checks.py'],
              'adapter_files': [{'path': n, 'source': v} for n, v in sorted(files.items())
                                if n.startswith('dataset_runtime/') and n.endswith('.py')],
              'budget_plan': json.loads(files['_experiment_delivery/budget_plan.json'])
                  if '_experiment_delivery/budget_plan.json' in files else None}
    # The old repair checkpoint sealed the native capture plus its then-readable
    # draft. Earlier versions could capture just the files when budget_plan was absent.
    materials = [{'native_files': files, **legacy}]
    if '_experiment_delivery/budget_plan.json' not in files:
        materials += [{'native_files': files}, {'native_files': files, **{k:v for k,v in legacy.items() if k != 'budget_plan'}}]
    if saved.get('repair_progress', {}).get('material_sha256') not in {sha(m) for m in materials}:
        raise ValueError('Captured native files differ from the archived repair checkpoint')
    try:
        draft = native.collect(job)
    except (OSError, ValueError, ValidationError):
        return None  # Remaining real delivery defects follow the normal repair workflow.
    if native._file_draft(files, job) != draft:
        raise ValueError('Retained delivery differs from the captured native output')
    return draft, {'previous_setup': ref, 'job_id': key, 'job_sha256': sha(job), 'request_id': request_id,
                   'original_failure': issues[0], 'captured_files_sha256': sha(files),
                   'draft_sha256': sha(draft), 'original_request_sha256': sha(old['request']),
                   'execution_authorized': False}


async def recover(engine, run_id, saved):
    from .dataset_experiments import COLLECTION
    engine.check_active(run_id)
    checked = await asyncio.to_thread(inspect, engine, run_id, saved)
    if checked is None:
        return saved
    draft, receipt = checked
    original_job = engine.store.get('dataset_codex_jobs', receipt['job_id'])
    if sha(original_job) != receipt['job_sha256']:
        raise ValueError('Original native job changed during local delivery readback')
    updated = {**saved, 'draft': draft, 'codex_job_id': receipt['job_id'], 'delivery_readback': receipt,
               'status': 'checking_configuration', 'updated_at': now()}
    updated.pop('error', None)
    engine.check_active(run_id)
    engine.store.batch(puts=[(COLLECTION, saved['id'], updated)], expected=[
        (COLLECTION, saved['id'], saved), ('dataset_codex_jobs', receipt['job_id'], original_job)])
    engine.store.event(run_id, 'experiment.configuration_delivery_revalidated',
        '已从原始完成回执重新核验配置、适配增量和完整说明，进入独立核验。', receipt)
    return updated


def restore_inherited_entry(engine, run_id, saved):
    """Restore the reviewed-input branch after a historical delta-only entry rejection."""
    from .dataset_codex import collect
    from . import dataset_experiments as setup
    from .experiment_decisions import _archive
    run = engine.store.get('runs', run_id)
    if run.get('status') != 'paused' or saved.get('draft'):
        raise ValueError('Entry recovery requires a paused setup with no newer accepted draft')
    ref = saved['previous_setup']
    archive = engine.store.get('experiment_evidence', ref['id']) or {}
    history = archive.get('summary', {})
    old = history.get('setup', {})
    failure = history.get('failure', {})
    if (archive.get('run_id') != run_id or sha(history) != ref['sha256']
            or old.get('id') != saved['id'] or old.get('run_id') != run_id or old.get('cycle') != saved['cycle']
            or failure.get('kind') != 'configuration' or failure.get('message') != '缺少统一数据适配入口 run(config)。'
            or old.get('source_program_sha256') != saved.get('source_program_sha256')
            or old.get('codex_job_id') != saved.get('previous_codex_job_id')):
        raise ValueError('No matching archived entry rejection for this setup')
    job = engine.store.get('dataset_codex_jobs', old['codex_job_id'])
    receipt = old.get('delivery_readback', {})
    draft = collect(job)
    if (sha(job) != receipt.get('job_sha256') or draft != old.get('draft') or sha(draft) != receipt.get('draft_sha256')
            or any(row['path'] == 'dataset_runtime/adapter.py' for row in draft['adapter_files'])):
        raise ValueError('Prior validated delivery or native receipt changed')
    expected = [('runs', run_id, run), (setup.COLLECTION, saved['id'], saved), ('dataset_codex_jobs', job['id'], job)]
    parked = saved.get('codex_job_id')
    if parked:
        current = engine.store.get('dataset_codex_jobs', parked) or {}
        if (current.get('setup_id') != saved['id'] or current.get('run_id') != run_id or current.get('draft')
                or current.get('checkpoint', {}).get('status') not in ('interrupted', 'failed', 'unknown')):
            raise ValueError('A newer implementation is running or delivered; cannot replace it')
        expected.append(('dataset_codex_jobs', parked, current))
    original = setup._source_gate(engine, run_id)
    if sha(original['program']) != old['source_program_sha256']:
        raise ValueError('Dataset source changed since the rejected composition')
    _, plan = setup.compile_configuration(engine, run_id, json.loads(draft['configuration_json']), old['dataset_packet'], draft.get('budget_plan'))
    candidate = setup.candidate(engine, run_id, draft, original)
    entry_sha = next(row['sha256'] for row in candidate['bundle']['files'] if row['path'] == 'dataset_runtime/adapter.py')
    base = setup._base_bundle(engine, run_id, original)
    if not any(row['path'] == 'dataset_runtime/adapter.py' and row['sha256'] == entry_sha for row in base['files']):
        raise ValueError('Assembled candidate entry is detached from the frozen base')
    saved_state = _archive(engine, run_id, {'setup': saved, 'parked_job_id': parked, 'source_failure': ref})
    proof = {'archived_successor': saved_state, 'parked_job_id': parked, 'original_job_id': job['id'],
             'base_entry_sha256': entry_sha, 'assembled_bundle_sha256': candidate['bundle']['sha256'],
             'planned_trials': plan['planned_trials'], 'execution_authorized': False}
    restored = {**old, 'inherited_entry_recovery': proof, 'updated_at': now()}
    engine.store.batch(puts=[(setup.COLLECTION, old['id'], restored)], expected=expected)
    engine.store.event(run_id, 'experiment.configuration_entry_recovered',
                       '已核对完整候选的继承入口，保留后续任务记录，恢复原交付等待独立审核。', proof)
    return proof
