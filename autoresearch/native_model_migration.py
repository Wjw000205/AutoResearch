"""Explicit model changes after a read-back, output-free native service failure.

Changing settings alone never authorizes dispatch. A subsequent start binds the
old request and destination, keeps effort/timeouts, and permits one successor.
"""
from __future__ import annotations

import copy
import hashlib
import json

from . import codex_reasoning
from .native_failure import retryable_failure
from .native_normal_recovery import _fresh_generation, _prepared, _source, _target
from .storage import now, uid

INITIALIZATION_KIND = 'native_pre_initialization_model_migration'
INITIALIZATION_VERSION = 1


def _initial_source(store, ai, run_id, normal):
    """Authenticate a pre-auth receipt against independent frozen records.

    No missing-ID heuristic authorizes this branch. The native writer's durable
    pre-auth invariant, prepared-only capture, untouched pending reservation and
    absence of output/usage must all agree before a new operation is considered.
    """
    from .engine import Harness, digest
    from .local_codex import LocalCodexClient
    from .context_manifest import validate_manifest
    from .memory_capture import read_captured_input
    try:
        prepared=normal['prepared'];role=prepared['role']
        native_key,operation=codex_reasoning.key_for(ai,run_id,**prepared)
        saved=store.get('codex_reasoning',native_key,{})
        cp=saved.get('checkpoint',{});selection=saved.get('selection',{})
        reservation=saved.get('reservation',{});binding=saved.get('memory_capture_binding',{})
        run=store.get('runs',run_id,{});hid=binding.get('handoff_id')
        owner=store.get('handoffs',hid,{}) if hid else {}
        inputs=store.get('handoff_inputs',hid,{}) if hid else {}
        payload={k:inputs.get(k) for k in ('instruction','context','schema')}
        cycle=run.get('state',{}).get('research_cycle',1)
        declared_cycles=[inputs.get('context',{})[field] for field in ('cycle','research_cycle') if field in inputs.get('context',{})]
        cycle_matches=(type(cycle) is int and cycle>=1
            and (all(type(value) is int and value==cycle for value in declared_cycles) if declared_cycles else cycle==1))
        schema=saved.get('transport_schema',saved.get('schema'))
        instruction=saved.get('instruction')
        instruction_sha=hashlib.sha256(instruction.encode()).hexdigest() if isinstance(instruction,str) else None
        workspace=str((store.path.parent/'codex-reasoning'/native_key/role).resolve())
        if (type(cp.get('version')) is not int or not LocalCodexClient._unsubmitted_initialization(cp,True)
                or normal.get('prepared_sha256')!=digest(prepared)
                or prepared['context'].get('_harness_control',{}).get('mode') not in (None,'continue')
                or saved.get('id')!=native_key or saved.get('operation_id')!=operation
                or saved.get('run_id')!=run_id or saved.get('role')!=role
                or saved.get('schema')!=prepared['schema'] or saved.get('result') is not None
                or selection.get('model')!=normal.get('model') or not selection.get('effort')
                or selection.get('stage')!=run.get('stage')
                or cp.get('run_id')!=run_id or cp.get('operation_id')!=native_key
                or cp.get('workspace')!=workspace or cp.get('project_root')!=workspace
                or cp.get('requested_model')!=normal.get('model') or cp.get('requested_effort')!=selection.get('effort')
                or not instruction_sha or cp.get('instruction_sha256')!=instruction_sha
                or cp.get('output_schema_sha256')!=hashlib.sha256(json.dumps(schema,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
                or ('transport_schema' in saved and schema!=codex_reasoning.TRANSPORT_SCHEMA)
                or ('transport_schema' not in saved and saved.get('native_task_schema') is not True)
                or type(saved.get('output_limit')) is not int or saved['output_limit']<=0
                or reservation.get('request_output_token_limit')!=saved['output_limit']
                or reservation.get('provider')!='local_codex' or reservation.get('model')!=normal.get('model')
                or reservation.get('run_id')!=run_id or reservation.get('role')!=role
                or reservation.get('operation_id')!=operation or not reservation.get('id')
                or owner.get('id')!=hid or owner.get('run_id')!=run_id or owner.get('to_role')!=role
                or owner.get('stage')!=run.get('stage') or owner.get('status') not in {'blocked','interrupted','running'}
                or inputs.get('run_id')!=run_id or inputs.get('id')!=hid or store.get('decisions',hid) is not None
                or owner.get('input_hash')!=digest(payload)
                or Harness.decision_key(run_id,role,owner.get('purpose'),**payload)!=hid
                or not cycle_matches):
            return None
        request=reservation['id'];manifest=store.get('context_manifests',request,{})
        snapshot=store.get('context_snapshots',binding.get('context_snapshot_id'),{})
        captured=read_captured_input(snapshot,run_id,role)
        if (not captured or any(binding.get(k)!=v for k,v in captured['binding'].items())
                or binding.get('operation_id')!=operation or binding.get('original_instruction_sha256')!=instruction_sha
                or not validate_manifest(store,manifest) or type(manifest.get('version')) is not int
                or manifest.get('request_id')!=request or manifest.get('run_id')!=run_id
                or manifest.get('role')!=role or manifest.get('handoff_id')!=hid
                or manifest.get('operation_id')!=operation or manifest.get('status')!='prepared'
                or manifest.get('submission_status')!='prepared' or manifest.get('thread_id') or manifest.get('turn_id')
                or manifest.get('wire_sha256')!=instruction_sha
                or manifest.get('snapshot_id')!=binding.get('context_snapshot_id')
                or manifest.get('compiled_manifest_id')!=binding.get('context_manifest_id')
                or manifest.get('compiled_manifest_sha256')!=binding.get('context_manifest_sha256')):
            return None
        budget=store.get('budgets',run_id,{})
        if (budget.get('pending',{}).get(request)!=reservation
                or sum(r.get('operation_id')==operation and r.get('provider')=='local_codex' for r in budget.get('pending',{}).values())!=1
                or any(r.get('id')==request or r.get('operation_id')==operation for r in budget.get('records',[]))
                or store.has_output_transcript(run_id,request_id=request,operation_id=operation)):
            return None
        usage=store.get('codex_usage',run_id+':'+native_key)
        if usage and (usage.get('run_id')!=run_id or usage.get('operation_id')!=native_key
                or usage.get('dispatched') is not False or usage.get('usage') is not None
                or usage.get('cost_usd') is not None or usage.get('price_verified') is not False
                or usage.get('thread_id') or usage.get('turn_id') or usage.get('turn_dispatched_at')
                or usage.get('requested_model')!=normal.get('model') or usage.get('requested_effort')!=selection['effort']):
            return None
        return {'version':INITIALIZATION_VERSION,'kind':INITIALIZATION_KIND,'status':'not_submitted',
            'native_key':native_key,'native_sha256':digest(saved),'request_id':request,
            'manifest_sha256':digest(manifest),'usage_sha256':digest(usage),
            'handoff_id':hid,'handoff_input_sha256':digest(inputs),'run_stage':run['stage'],'research_cycle':cycle,
            'model':normal['model'],'effective_output_limit':saved['output_limit'],
            'source_disposition':'superseded_before_submission','usage_scope':'original_pending_reservation_retained'}
    except (KeyError,TypeError,ValueError,AttributeError):
        return None


def _selection(store, ai, run_id, normal):
    provider = ai.provider_for(normal['prepared']['role'])
    if provider.get('id') != 'local_codex':
        return None
    selected = codex_reasoning.profile(store, run_id, provider, normal['prepared']['context'])
    return {field: selected[field] for field in ('model', 'effort', 'timeout')}


def resume_target(store, ai, run_id, progress):
    """Only a first failed normal request is eligible for model migration."""
    normal = progress.get('normal_request') or {}
    target = _target(normal, progress.get('turn'))
    if (target is None or progress.get('mode') == 'final_only'
            or any(progress.get(k) for k in ('native_model_migrations', 'native_normal_request_history',
                                             'finalization_history'))
            or any(normal.get(k) for k in ('native_interruption_of', 'native_model_migration_of',
                                           'json_syntax_correction_of'))):
        return None
    prepared = normal['prepared']
    native_key, operation = codex_reasoning.key_for(ai, run_id, prepared['role'],
        prepared['task'], prepared['context'], prepared['schema'])
    saved = store.get('codex_reasoning', native_key, {})
    cp, previous = saved.get('checkpoint', {}), saved.get('selection', {})
    destination = _selection(store, ai, run_id, normal)
    if (destination and destination['model']!=normal.get('model')
            and all(destination[k]==previous.get(k) for k in ('effort','timeout'))):
        source=_initial_source(store,ai,run_id,normal)
        if source is not None:
            return {'version':INITIALIZATION_VERSION,'kind':INITIALIZATION_KIND,
                'target':target,'destination':destination,'native_key':native_key,'source':source}
    if (not destination or destination['model'] == normal.get('model')
            or any(destination[k] != previous.get(k) for k in ('effort', 'timeout'))
            or saved.get('id') != native_key or saved.get('operation_id') != operation
            or saved.get('run_id') != run_id or saved.get('role') != prepared['role']
            or saved.get('schema') != prepared['schema'] or saved.get('result') is not None
            or previous.get('model') != normal.get('model') or not saved.get('reservation')
            or cp.get('operation_id') != native_key or cp.get('run_id') != run_id
            or not cp.get('thread_id') or not cp.get('turn_id') or not retryable_failure(cp)):
        return None
    return {'target': target, 'destination': destination, 'native_key': native_key}


async def recover(harness, run_id, key, progress):
    """Read the old turn using its original model, then archive and supersede it."""
    from .engine import ResearchBlocked, digest
    from .intelligence import ProviderError
    engine, ai = harness.engine, harness.intelligence
    store = engine.store
    run = store.get('runs', run_id, {})
    generation = store.get('operator_resumes', run.get('state', {}).get('resume_generation'), {})
    binding = resume_target(store, ai, run_id, progress)
    if (binding is None or run.get('status') != 'running'
            or generation.get('kind') != 'explicit_start_command' or generation.get('run_id') != run_id
            or generation.get('native_model_targets', {}).get(key) != binding
            or generation.get('targets', {}).get(key) != binding['target']
            or store.get('handoffs', key, {}).get('stage') != run.get('stage')
            or not _fresh_generation(generation, store.get('codex_reasoning', binding['native_key']))):
        return False
    original_progress = copy.deepcopy(progress)
    normal, prepared = progress['normal_request'], progress['normal_request']['prepared']
    if binding.get('kind')==INITIALIZATION_KIND:
        return _advance_initialization(harness,run_id,key,progress,binding,generation,original_progress)
    provider = {**ai.provider_for(prepared['role']), 'model': normal['model']}
    engine.check_active(run_id)
    # The verified existing checkpoint has both native IDs: this call can only
    # read the original turn. It cannot create a thread or reserve another call.
    try:
        response = await codex_reasoning.ask(ai, run_id, provider=provider, **prepared)
    except ProviderError:
        response = None
    if response is not None:
        normal.update(status='response_saved', response=response, recovered_at=now())
        harness._save_progress(run_id, key, progress)
        return True
    source = _source(store, ai, run_id, normal)
    if source is None or source['status'] != 'failed':
        raise ResearchBlocked('模型切换前未能确认原请求已失败且无回复；保留原回执，未发送新请求。')
    if resume_target(store, ai, run_id, progress) != binding:
        raise ResearchBlocked('模型切换目标已改变；未发送新请求。')
    operation = uid()
    successor = _prepared(normal, operation)
    preview = ai.preflight_batch(run_id, [successor])
    if preview['request_count'] != 1 or preview['requests'][0]['max_output_tokens'] != source['effective_output_limit']:
        raise ResearchBlocked('模型切换后的输出额度发生变化；未发送新请求。')
    audit = {'previous': copy.deepcopy(normal), 'source': source, 'binding': binding,
        'resume_generation': generation['id'], 'new_operation_id': operation,
        'prepared_sha256': digest(successor), 'tool_results_sha256': digest(progress['tool_results']),
        'created_at': now(), 'source_disposition': 'superseded_without_response'}

    def claim(current):
        if current != original_progress:
            raise ResearchBlocked('模型切换时任务进度已改变；未发送新请求。')
        from .tool_request_recovery import prepared_successor
        current.update(native_model_migrations=[audit], normal_request=prepared_successor(store, key, {
            'status': 'ready', 'turn': progress['turn'], 'operation_id': operation, 'prepared': successor,
            'provider_id': 'local_codex', 'model': binding['destination']['model'],
            'native_model_migration_of': normal['operation_id'], 'created_at': now()}))
        return current

    engine.check_active(run_id)
    progress.update(store.mutate('handoff_progress', key, claim))
    store.event(run_id, 'handoff.native_model_migration',
        '原请求已回读确认为过载等临时故障且无回复；按本次恢复指令切换模型，保持推理强度、工具进度和原始回执。',
        {'handoff_id': key, 'resume_generation': generation['id'], 'source': source,
         'destination': binding['destination'], 'new_operation_id': operation})
    return True


def validate(engine, ai, run_id, key, progress, preview):
    from .engine import ResearchBlocked, digest
    audits = progress.get('native_model_migrations', [])
    if isinstance(audits,list) and len(audits)==1 and isinstance(audits[0],dict) and any(k in audits[0] for k in ('kind','version')):
        if _valid_initialization(engine,ai,run_id,key,progress,preview):return
        raise ResearchBlocked('首次初始化模型迁移的冻结来源、版本或目标已改变；未发送新请求。')
    valid = isinstance(audits, list) and len(audits) == 1 and isinstance(audits[0], dict)
    if valid:
        audit, normal = audits[0], progress['normal_request']
        previous, binding = audit['previous'], audit['binding']
        source = _source(engine.store, ai, run_id, previous)
        generation = engine.store.get('operator_resumes', audit.get('resume_generation'), {})
        operation = normal.get('operation_id')
        expected = _prepared(previous, operation)
        valid = (progress.get('mode') != 'final_only' and source is not None and source['status'] == 'failed'
            and source == audit.get('source') and binding['native_key'] == source['native_key']
            and generation.get('kind') == 'explicit_start_command' and generation.get('run_id') == run_id
            and generation.get('native_model_targets', {}).get(key) == binding
            and generation.get('targets', {}).get(key) == binding['target']
            and binding['target'] == _target(previous, progress['turn'])
            and _fresh_generation(generation, engine.store.get('codex_reasoning', source['native_key']))
            and binding['destination'] == _selection(engine.store, ai, run_id, normal)
            and binding['destination']['model'] != previous['model']
            and all(binding['destination'][field] == engine.store.get('codex_reasoning',
                source['native_key'])['selection'].get(field) for field in ('effort', 'timeout'))
            and isinstance(operation, str) and bool(operation) and operation != previous['operation_id']
            and operation == audit.get('new_operation_id')
            and normal.get('native_model_migration_of') == previous['operation_id']
            and normal.get('turn') == previous['turn'] == progress['turn']
            and normal.get('provider_id') == 'local_codex' and normal.get('model') == binding['destination']['model']
            and normal.get('prepared') == expected and audit.get('prepared_sha256') == digest(expected)
            and audit.get('tool_results_sha256') == digest(progress['tool_results'])
            and preview.get('request_count') == 1 and len(preview.get('requests', [])) == 1
            and preview['requests'][0]['max_output_tokens'] == source['effective_output_limit'])
    if not valid:
        raise ResearchBlocked('模型切换的原回执、目标或冻结任务已改变；未发送新请求。')


def _advance_initialization(harness,run_id,key,progress,binding,generation,original_progress):
    from .engine import ResearchBlocked,digest
    engine,ai=harness.engine,harness.intelligence
    normal=progress['normal_request'];source=binding['source'];operation=uid()
    if source.get('handoff_id')!=key:
        raise ResearchBlocked('首次初始化迁移不属于当前冻结交接；未发送新请求。')
    successor=_prepared(normal,operation)
    preview=ai.preflight_batch(run_id,[successor])
    if (preview.get('request_count')!=1 or len(preview.get('requests',[]))!=1
            or preview['requests'][0]['max_output_tokens']!=source['effective_output_limit']
            or resume_target(engine.store,ai,run_id,progress)!=binding):
        raise ResearchBlocked('首次初始化迁移的冻结输入、输出额度或来源改变；未发送新请求。')
    audit={'version':INITIALIZATION_VERSION,'kind':INITIALIZATION_KIND,'previous':copy.deepcopy(normal),
        'source':source,'binding':binding,'resume_generation':generation['id'],'new_operation_id':operation,
        'prepared_sha256':digest(successor),'tool_results_sha256':digest(progress['tool_results']),
        'created_at':now(),'source_disposition':'superseded_before_submission'}
    def claim(current):
        if current!=original_progress:raise ResearchBlocked('首次初始化迁移进度已改变；未发送新请求。')
        from .tool_request_recovery import prepared_successor
        current.update(native_model_migrations=[audit],normal_request=prepared_successor(engine.store,key,{
            'status':'ready','turn':progress['turn'],'operation_id':operation,'prepared':successor,
            'provider_id':'local_codex','model':binding['destination']['model'],
            'native_model_migration_of':normal['operation_id'],'created_at':now()}))
        return current
    engine.check_active(run_id)
    progress.update(engine.store.mutate('handoff_progress',key,claim))
    engine.store.event(run_id,'handoff.native_initialization_model_migration',
        '原初始化未提交模型回合；按本次显式恢复指令冻结新模型请求，保留原检查点、预算预留与工具回执。',
        {'handoff_id':key,'resume_generation':generation['id'],'source':source,
            'destination':binding['destination'],'new_operation_id':operation})
    return True


def _valid_initialization(engine,ai,run_id,key,progress,preview):
    from .engine import digest
    try:
        audit=progress['native_model_migrations'][0];normal=progress['normal_request']
        previous,binding=audit['previous'],audit['binding'];source=_initial_source(engine.store,ai,run_id,previous)
        run=engine.store.get('runs',run_id,{});generation=engine.store.get('operator_resumes',audit['resume_generation'],{})
        operation=normal.get('operation_id');expected=_prepared(previous,operation)
        return (type(audit.get('version')) is int and audit['version']==INITIALIZATION_VERSION
            and audit.get('kind')==INITIALIZATION_KIND and type(binding.get('version')) is int
            and binding['version']==INITIALIZATION_VERSION and binding.get('kind')==INITIALIZATION_KIND
            and progress.get('mode')!='final_only' and source is not None and source==audit.get('source')==binding.get('source')
            and binding['native_key']==source['native_key'] and source['handoff_id']==key
            and audit.get('source_disposition')=='superseded_before_submission'
            and run.get('status')=='running' and source['run_stage']==run.get('stage')
            and generation.get('kind')=='explicit_start_command' and generation.get('run_id')==run_id
            and generation.get('native_model_targets',{}).get(key)==binding
            and generation.get('targets',{}).get(key)==binding['target']==_target(previous,progress['turn'])
            and _fresh_generation(generation,engine.store.get('codex_reasoning',source['native_key']))
            and binding['destination']==_selection(engine.store,ai,run_id,normal)
            and binding['destination']['model']!=previous['model']
            and all(binding['destination'][k]==engine.store.get('codex_reasoning',source['native_key'])['selection'][k] for k in ('effort','timeout'))
            and isinstance(operation,str) and bool(operation) and operation!=previous['operation_id']
            and operation==audit['new_operation_id'] and normal.get('native_model_migration_of')==previous['operation_id']
            and normal.get('turn')==previous['turn']==progress['turn']
            and normal.get('provider_id')=='local_codex' and normal.get('model')==binding['destination']['model']
            and normal.get('prepared')==expected and audit.get('prepared_sha256')==digest(expected)
            and audit.get('tool_results_sha256')==digest(progress['tool_results'])
            and preview.get('request_count')==1 and len(preview.get('requests',[]))==1
            and preview['requests'][0]['max_output_tokens']==source['effective_output_limit'])
    except (KeyError,TypeError,ValueError,AttributeError):
        return False
