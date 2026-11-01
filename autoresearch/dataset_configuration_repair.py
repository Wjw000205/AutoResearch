"""Persisted agent-led configuration repair; only compact work items are sent."""
from __future__ import annotations

import copy

from .experiment_workflow import sha
from .storage import now


class NeedsRepair(ValueError):
    def __init__(self, message, *, kind='configuration', details=None):
        super().__init__(message)
        self.kind = kind
        self.details = details or {}


def request(engine, run_id, candidate_sha256, message, details, *, codex_job_id=None):
    """Queue a concrete operator finding on a stopped candidate, without executing."""
    from .dataset_experiments import current
    run, saved = engine.store.get('runs',run_id), current(engine,run_id)
    if not run or run['status'] not in {'paused','blocked'} or not saved or saved.get('status') == 'ready':
        raise ValueError('A pending configuration repair requires a stopped, unapproved setup')
    if bool(candidate_sha256) == bool(codex_job_id):
        raise ValueError('Bind exactly one candidate or native delivery')
    if candidate_sha256 and saved.get('candidate',{}).get('bundle',{}).get('sha256') != candidate_sha256:
        raise ValueError('Requested repair candidate changed')
    if codex_job_id and saved.get('codex_job_id') != codex_job_id:
        raise ValueError('Requested native delivery changed')
    value = {'run_id':run_id,'setup_id':saved['id'],'candidate_sha256':candidate_sha256,
        'codex_job_id':codex_job_id,
        'message':message,'details':copy.deepcopy(details),'status':'pending','created_at':now()}
    value['id'] = run_id+':configuration-repair-request:'+sha(value)
    engine.store.put('dataset_configuration_repair_requests',value['id'],value)
    engine.store.event(run_id,'experiment.configuration_repair_requested',message,
        {'request_id':value['id'],'candidate_sha256':candidate_sha256})
    return value


def consume_requested(engine, run_id, saved):
    for row in engine.store.list('dataset_configuration_repair_requests',run_id):
        if row['status'] != 'pending' or not saved or row['setup_id'] != saved['id']:
            continue
        if (row.get('codex_job_id') and saved.get('codex_job_id') != row['codex_job_id'] or
                row.get('candidate_sha256') and saved.get('candidate',{}).get('bundle',{}).get('sha256') != row['candidate_sha256']):
            raise ValueError('Queued configuration repair no longer matches the candidate')
        engine.check_active(run_id)
        raise NeedsRepair(row['message'],kind='runtime_interface',details={**row['details'],'repair_request_id':row['id']})


def assignment(saved):
    return saved.get('configuration_assignment') or {
        'purpose': 'dataset_configure:' + sha(saved['request']), 'context': saved['request']}


def _observations(engine, run_id, saved):
    from .engine import Harness
    from .dataset_experiments import WRITE, DRAFT
    task = assignment(saved)
    key = saved.get('configuration_owner') or Harness.decision_key(run_id, 'engineer', task['purpose'], WRITE, task['context'], DRAFT)
    if not engine.store.get('handoff_progress', key):
        # Earlier saved deliveries predate the explicit owner field. Their
        # purpose contains the exact frozen request hash; retain that identity.
        with engine.store._read_db() as db:
            row = db.execute("SELECT key FROM records WHERE collection='handoff_progress' AND key LIKE ? LIMIT 1",
                (run_id + ':' + task['purpose'] + ':engineer:%',)).fetchone()
        if row:
            key = row[0]
    progress = engine.store.get('handoff_progress', key) or {}
    coverage, notes = set(), []
    for row in progress.get('tool_results', []):
        call, result = row.get('call', {}), row.get('result', {})
        if result.get('receipt_sha256') and result.get('text') and result['receipt_sha256'] == sha({
                'call': call, 'result': {k: v for k, v in result.items() if k != 'receipt_sha256'}}):
            # New evidence archives/optional-argument spelling do not earn progress.
            coverage.add(sha({'name': call.get('name'), 'path': result.get('path'),
                'view': result.get('view'), 'source_sha256': result.get('source_sha256'),
                'offset': result.get('offset'), 'end': result.get('end'), 'text_sha256': sha(result['text'])}))
        if call.get('name') == 'experiments.record_notes' and result.get('saved'):
            notes = result.get('notes', [])[-8:]
    return key, progress, coverage, notes


def prepare(engine, run_id, saved, failure):
    """Archive the exact failed version and create a new frozen work assignment."""
    from .dataset_experiments import COLLECTION, source_evidence
    from .experiment_decisions import _archive
    from .engine import ResearchBlocked
    from .dataset_configuration_queue import budget, batch
    engine.check_active(run_id)
    owner, progress, coverage, notes = _observations(engine, run_id, saved)
    previous_progress = saved.get('repair_progress') or {}
    known = set(previous_progress.get('coverage', []))
    draft = saved.get('draft') or {}
    if not draft and saved.get('codex_job_id'):
        from .dataset_codex import previous_files, _file_draft
        job = engine.store.get('dataset_codex_jobs', saved['codex_job_id']) or {}
        try:
            draft = _file_draft(previous_files(job))
        except (KeyError, ValueError):
            pass  # Incomplete files still seed the next native workspace.
    material = {k: draft.get(k) for k in ('configuration_json', 'adapter_files', 'checks', 'budget_plan') if k in draft}
    if saved.get('codex_job_id'):
        job = engine.store.get('dataset_codex_jobs', saved['codex_job_id']) or {}
        if job.get('partial_delivery'):
            material['native_files'] = job['partial_delivery']
    material_sha = sha(material) if material else None
    advanced = bool(coverage - known) or bool(material and material_sha != previous_progress.get('material_sha256'))
    stalled = 0 if advanced else previous_progress.get('stalled_rounds', 0) + 1
    limit = max(1, engine.store.get('runs', run_id)['config'].get('stagnation_rounds', 3))
    if stalled >= limit:
        saved.update(status='blocked', error='配置修复连续未取得新证据或实现变化：' + str(failure)[:1400])
        engine.store.put(COLLECTION, saved['id'], saved)
        raise ResearchBlocked(saved['error'])
    # Keep the full original/failing artifacts on disk, not nested into prompts.
    previous = _archive(engine, run_id, {'setup': saved, 'failure': {'kind': failure.kind,
        'message': str(failure), 'details': failure.details}, 'handoff_id': owner,
        'termination': (progress.get('finalization') or {}).get('reason')})
    original = source_evidence(engine, run_id, saved)
    evidence = {k: original[k] for k in ('binding', 'code_sources', 'source_design', 'source_plan', 'task_requirements') if k in original}
    evidence['configuration_repair'] = {'kind': failure.kind, 'message': str(failure),
        'details': failure.details, 'draft': draft, 'votes': saved.get('votes', []),
        'previous_setup': previous, 'notes': notes, 'handoff_id': owner,
        'notes_provenance': 'Earlier agent observations, not new reads, independent approval or execution.'}
    ref = _archive(engine, run_id, evidence)
    repair = {'kind': failure.kind, 'issues': (draft.get('blockers') or [str(failure)])[:8],
        'failure_path': '/configuration_repair', 'previous_setup': previous,
        'field_paths': ['/source_design/' + k for k in ('acceptance', 'baseline_params', 'proposed_params',
            'ablation_params', 'resource_plan', 'evaluation') if k in evidence['source_design']],
        'source_index': {'tool': 'experiments.inspect_module', 'evidence_id': ref['id'], 'expected_sha256': ref['sha256']},
        'observations': notes, 'observations_owner': owner,
        'instruction': '逐项补读或修正本次缺项，随后交付完整配置、适配代码和检查。'
            'field_paths 可直接读取，源码先查看目录再定向读取训练、损失和模型接口；'
            '归档页可按 SHA 重读，不需要重新阅读全部历史。缺少运行接口时在适配层补齐并调用已有模型。'
            '任务未限定的参数由你提出并说明依据，随后交独立核验。'}
    current_budget = budget(engine.store, run_id)
    if current_budget and current_budget.get('status') == 'approved':
        repair['frozen_budget_plan'] = copy.deepcopy(draft.get('budget_plan'))
        repair['instruction'] += '已批准预算及历史消耗继续有效；本次只修复实现和检查，不能扩额或删减任务要求。'
    context = {**saved['request'], 'validation_evidence': ref, 'evidence_sha256': ref['sha256'],
        'dataset_budget': current_budget,
        'dataset_configuration': {**saved['request']['dataset_configuration'], 'repair': repair}}
    sequence = saved.get('repair_sequence', 0) + 1
    task = {'purpose': f'dataset_configure:repair-{sequence}:' + sha(context), 'context': context}
    fresh = {k: v for k, v in saved.items() if k not in {
        'draft', 'candidate', 'program', 'design', 'plan', 'review_context', 'review_contexts', 'review_clarifications', 'review_instruction', 'review_schema', 'votes', 'check', 'error', 'blockers', 'configuration_owner', 'codex_job_id', 'agent_work_order', 'direct_repair'}}
    if saved.get('codex_job_id'):
        fresh['previous_codex_job_id'] = saved['codex_job_id']
    active = batch(engine.store, run_id)
    if active and active.get('budget_status') == 'approved':
        fresh['repair_budget_binding'] = copy.deepcopy(active['budget_binding'])
    fresh.update(request=context, configuration_assignment=task, status='repairing_configuration',
        repair_sequence=sequence, previous_setup=previous,
        repair_progress={'coverage': sorted(known | coverage), 'material_sha256': material_sha,
            'stalled_rounds': stalled}, updated_at=now())
    puts = [(COLLECTION, saved['id'], fresh)]
    request_id = failure.details.get('repair_request_id')
    requested = engine.store.get('dataset_configuration_repair_requests',request_id) if request_id else None
    if requested:
        if requested['run_id'] != run_id or requested['setup_id'] != saved['id']:
            raise ValueError('Configuration repair request ownership changed')
        puts.append(('dataset_configuration_repair_requests',request_id,{**requested,'status':'consumed'}))
    engine.store.batch(puts)
    engine.store.event(run_id, 'experiment.configuration_repair_started',
        'Agent 正在补齐实验配置缺项并修复接口，随后重新核验。',
        {'setup_id': saved['id'], 'repair_sequence': sequence, 'kind': failure.kind,
            'previous_setup': previous, 'new_read_pages': len(coverage - known), 'stalled_rounds': stalled})
    return fresh
