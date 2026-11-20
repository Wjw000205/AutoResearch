"""Durable Harness work orders for schema/runtime metric alignment.

A host compilation failure is not a failed model dispatch. No paid reply,
memory, product or scientific condition is edited/deleted by this workflow.
"""
from __future__ import annotations

import ast
import copy
import hashlib
from pathlib import Path

from .async_ownership import gather, to_thread
from .metric_contract import BINDING_SCHEMA, GUIDANCE, HostScopeRequired, compile_contract, inventory, signature
from .storage import now
from . import independent_full_review as full

KIND = 'metric_contract_alignment'
CAPABILITY_REVIEWS = 'metric_capability_reviews'
RETIRED_REVIEW_PREFIXES = ('metric_contract_capability_review:', 'metric_capability_evidence:')
HOST_SCOPE_SUPPLEMENT = (
    'As the assigned design author, propose an explicit missing host scope declaration '
    'for independent review. The old schema did not declare this field, so a previously '
    'approved declaration is not a prerequisite to proposing this supplement. '
    'Declare host_primary_criterion within the existing frozen comparison scopes. '
    'Preserve all existing metric bindings, criteria, thresholds, task/reference scopes, '
    'complete flag, jobs and seeds. Do not infer or silently approve the scope from names '
    'or uniqueness. Other roles must review the proposed declaration before adoption. '
    'Use unresolved only for real unresolved source semantics or unsupported capabilities; '
    'put ordinary pending-review requirements and preserved scientific limitations in rationale. '
    'If a genuine scientific conflict prevents a proposal, leave the scope null and identify '
    'the exact conflict or missing evidence for targeted clarification.'
)
AUTHOR = (
    'Compile explicit runtime bindings for the approved acceptance criteria. Read source_design and acceptance '
    'without changing any metric identity, direction, threshold, required flag, complete flag or scope. '
    'Use only host task/configuration inventory identities. Distinguish checkpoint selection from local '
    'comparisons and descriptive summaries. A host contract is a prospective implementation requirement; '
    'do not ask for measurements or code receipts before implementation. If source semantics are ambiguous, '
    'state the exact conflict in unresolved; use available reading tools to resolve it. '
    'Previous review/diagnostic evidence must be addressed explicitly. ' + GUIDANCE + ' ' + HOST_SCOPE_SUPPLEMENT
)
REVIEW = (
    'Independently check that the compiled metric bindings match the approved plan and source design. '
    'Verify task, variant, phase, seed, measurement domain, aggregation, direction and threshold. '
    'Report unclear source semantics to the author or specify the contract correction. '
    'Return the exact source_sha256 and candidate_sha256. Measured feasibility is assessed after validation.'
)


def author_schema(acceptance, *, host_scope=False):
    item = copy.deepcopy(BINDING_SCHEMA)
    item['properties']['metric'] = {'enum': [c['metric'] for c in acceptance['criteria']]}
    item['required'].append('metric')
    result = {'type': 'object', 'properties': {
        'version': {'const': 2 if host_scope else 1}, 'bindings': {'type': 'array', 'items': item,
            'maxItems': len(acceptance['criteria'])},
        'unresolved': {'type': 'array', 'items': {'type': 'string', 'minLength': 1}},
        'rationale': {'type': 'string', 'minLength': 1}},
        'required': ['version', 'bindings', 'unresolved', 'rationale'], 'additionalProperties': False}
    if host_scope:
        result['properties']['host_primary_criterion'] = {'enum': [None, *[
            c['metric'] for c in acceptance['criteria'] if c.get('required', True)]]}
        result['required'].append('host_primary_criterion')
    return result


def capability_sources():
    root = Path(__file__).resolve().parents[1]
    selected = {'autoresearch/engine.py': {'Engine.ensure_experiment_plan', 'Engine._trial', 'Engine.cycle_validation_summary',
                'Engine.code_instruction', 'Engine._stage','Engine.review_cycle','CYCLE_VOTE','obj',
                'Harness._request_projection', 'Harness._drain_tools'},
                'autoresearch/benchmark_matrix.py': {'validation_summary', 'final_outcome', 'decide', 'verify_sources'},
                'autoresearch/comparison_task_policy.py': {'assignment'},
                'autoresearch/performance_policy.py': {'classify', 'outcome'},
                'autoresearch/experiment_workflow.py': {'coverage', 'ledger_and_searches'},
                'autoresearch/architecture_revisions.py': {'validate_plan'},
                'autoresearch/experiment_plan.py': {'check_outcome', '_signature'}}
    selected['autoresearch/research_cycle.py'] = {'decide_cycle','review_request_schema','ROLES'}
    selected['autoresearch/protocol_amendments.py'] = {'materialize', 'replay', 'review', 'replay_threshold', 'revision_hash'}
    selected['autoresearch/capability_work_orders.py'] = {'research_binding', 'WorkOrder.submit', 'WorkOrder.run'}
    selected['autoresearch/capability_checks.py'] = {'runtime_identity', 'CORE_SHA256', 'SUPPORTED_CORE_SHA256S'}
    selected['autoresearch/capability_deployment.py'] = {'paused_binding', 'Sources.load'}
    selected['autoresearch/context_compiler.py'] = {'ContextCompiler._build'}
    result = []
    for name in [*selected, 'autoresearch/metric_contract.py', 'autoresearch/metric_contract_recovery.py',
                 'tests/test_metric_contract_alignment.py', 'docs/METRIC_CONTRACT_ALIGNMENT.md',
                 'scripts/run_source_bound_checks.py', 'autoresearch/metric_protocol_revisions.py',
                 'tests/test_metric_protocol_revisions.py', 'tests/test_metric_host_threshold.py', 'autoresearch/harness_delivery.py',
                 'autoresearch/review_material_packet.py','autoresearch/review_material_reuse.py',
                 'tests/test_review_material_range_mapping.py',
                 'autoresearch/review_code_materials.py','tests/test_review_code_materials.py','docs/REVIEW_CODE_MATERIALS.md',
                 'autoresearch/metric_review_sources.py','autoresearch/independent_full_review.py',
                 'autoresearch/full_review_receipts.py',
                 'autoresearch/evidence_delivery.py',
                 'autoresearch/document_index.py','autoresearch/implementation_reading.py',
                 'autoresearch/code_whole_review.py',
                 'autoresearch/code_repair_response_correction.py',
                 'autoresearch/implementation_alignment_recovery.py',
                 'autoresearch/implementation_alignment_evidence.py','autoresearch/code_review_outcome.py',
                 'autoresearch/harness_activity.py','autoresearch/async_ownership.py',
                 'autoresearch/architecture_validation.py',
                 'autoresearch/full_review_window.py','autoresearch/full_review_notes.py','autoresearch/tool_invocations.py',
                 'autoresearch/full_review_reading_context.py','autoresearch/code_review_reading.py',
                 'tests/test_full_review_reading_context.py',
                 'autoresearch/code_reading_memory.py','autoresearch/code_navigation.py','autoresearch/tool_contracts.py',
                 'autoresearch/code_repair_tool_review.py',
                 'autoresearch/code_whole_policy.py','autoresearch/review_inheritance.py','autoresearch/tool_views.py',
                 'tests/test_code_reading_memory.py','docs/CODE_READING_MEMORY.md',
                 'autoresearch/full_review_closeout.py','autoresearch/indexed_review_closeout.py',
                 'autoresearch/metric_review_evidence.py',
                 'autoresearch/metric_capability_evidence_recovery.py','autoresearch/program_capability_repair.py',
                 'docs/METRIC_CAPABILITY_EVIDENCE_RECOVERY.md',
                 'tests/test_metric_capability_evidence_recovery.py',
                 'tests/test_program_capability_repair.py',
                 'tests/test_metric_dual_review.py','tests/test_metric_review_evidence.py']:
        raw = (root / name).read_bytes()
        content = raw.decode('utf-8-sig')
        result.append({'path': name, 'sha256': hashlib.sha256(raw).hexdigest(), 'source': content,
                       **({'source_policy':{'version':2,'required_symbols':sorted(selected[name])}} if name in selected else {}),
                       'provenance': 'external_capability_bootstrap_candidate_pending_harness_review'})
    return result


def source_assignment(engine, run_id, run):
    """Resolve only the controller's declared design owner, never newest time."""
    from .engine import Harness, ResearchBlocked
    owner_id = run.get('state', {}).get('design_request_restart')
    if not owner_id:
        return None  # Ordinary historical designs are bound by the completed step.
    owner = engine.store.get('stage_contexts', owner_id) or {}
    if owner.get('run_id') != run_id or owner.get('cycle') != engine.cycle(run_id):
        raise ResearchBlocked('Declared design owner is outside the current run/cycle')
    task = owner['assignment']
    hid = Harness.decision_key(run_id, 'engineer', **task)
    decision = engine.store.get('decisions', hid) or {}
    authenticate(engine, run_id, 'engineer', task, decision.get('result'))
    from jsonschema import Draft202012Validator
    errors = list(Draft202012Validator(task['schema']).iter_errors(decision['result']))
    return {'owner_id': owner_id, 'handoff_id': hid,
            'author_role': 'engineer',
            'assignment_sha256': signature(task),
            'input_sha256': signature({k: task[k] for k in ('instruction', 'context', 'schema')}),
            'frozen_output_schema': copy.deepcopy(task['schema']),
            'original_response': copy.deepcopy(decision['result']),
            'conforms_to_frozen_schema': not errors}


def author_context(context):
    """Keep the scientific source contract separate from archived implementation notes."""
    return {k: copy.deepcopy(v) for k, v in context.items() if k not in (
        'runtime_capability', 'isolated_checks', 'existing_fixture_baseline_comparison', 'prior_alignment_reviews')}


def _freeze(engine, run_id, design, plan, acceptance, common):
    from .engine import ResearchBlocked
    run = engine.store.get('runs', run_id)
    step = engine.store.get('steps', run_id + ':design')
    if (run['mode'] != 'live' or run['stage'] != 'implementation' or not step
            or step['status'] != 'completed' or engine.experiment_plan(run_id)
            or engine.committed_outputs(run_id).get('implementation')):
        raise ResearchBlocked('Metric contract alignment requires an uncommitted preimplementation cursor')
    origin = source_assignment(engine, run_id, run)
    from .engine import ROLES
    author_role = (origin or {}).get('author_role', 'engineer')  # Declared design-stage producer.
    review_roles = [role for role in ROLES if role != author_role]
    if author_role not in ROLES or len(review_roles) != 2:
        raise ResearchBlocked('Metric source clarification has no declared independent role routing')
    source = {'design_sha256': signature(design), 'base_plan_sha256': signature(plan),
              'acceptance_sha256': signature(acceptance), 'config_sha256': signature(run['config']),
              'cycle': engine.cycle(run_id), 'design_step_sha256': signature(step),
              'operation': 'ensure_experiment_plan.metric_contract_compilation',
              'source_assignment_sha256': signature(origin),
              'host_primary_threshold': run['config'].get('min_relative_improvement', .01),
              'author_role': author_role, 'review_roles': review_roles,
              'independent_full_review_protocol': 1,
              'failed_model_handoff_id': None}
    key = f'{run_id}:{KIND}:{signature(source)}'
    saved = engine.store.get('stage_contexts', key)
    if saved:
        if saved['source'] != source:
            raise ResearchBlocked('Metric contract work order identity changed')
        return saved
    prior_records = [w for w in engine.store.list('stage_contexts', run_id)
                     if w.get('id', '').startswith(run_id + ':' + KIND + ':')
                     and w.get('source', {}).get('design_sha256') == signature(design)]
    prior = [{'work_order_id': w['id'], 'status': w.get('status'),
              'source': w.get('source'), 'rounds': [{k: r.get(k) for k in (
                  'author_handoff_id', 'reviews', 'review_handoff_ids', 'diagnostics')} for r in w.get('rounds', [])]}
             for w in prior_records]
    task_common = {k: copy.deepcopy(common[k]) for k in (
        'objective', 'publication_target', 'research_cycle', 'execution_constraints',
        'indexed_document_context', 'document_index_protocol', 'document_input_protocol',
        'document_read_window_protocol', 'source_tool_protocol', 'literature_notes_protocol',
        'paper_rag_protocol', 'pdf_tools_protocol', 'literature_generation') if k in common}
    context = {**task_common, 'metric_contract_protocol': 1, 'work_order_id': key,
               'source_design': copy.deepcopy(design), 'acceptance': copy.deepcopy(acceptance),
               'base_plan': copy.deepcopy(plan), 'configuration_inventory': inventory(plan),
               'original_assignment_evidence': origin,
               'host_primary_threshold': source['host_primary_threshold'],
               'host_scope_rule': HOST_SCOPE_SUPPLEMENT,
               'source_supplement_authority': {'author_role': author_role, 'review_roles': review_roles,
                    'source_assignment_id': (origin or {}).get('handoff_id'),
                    'authorized_fields': ['host_primary_criterion'], 'status': 'proposal_requires_independent_review'},
               'prior_alignment_reviews': prior,
               'contract_diagnostic': 'explicit_binding_required_between_authored_schema_and_runtime_consumers'}
    saved = {'id': key, 'run_id': run_id, 'source': source, 'status': 'pending',
             'author_role': author_role, 'review_roles': review_roles,
             'operator_requirement': 'Align Harness and schema generically; preserve science; program owns dispatch.',
             'authorized_scope': 'Compile/review a derived validation binding; no edits/deletion of research history.',
             'assignment': {'purpose': KIND + ':' + signature(source), 'instruction': AUTHOR,
                            'context': author_context(context), 'schema': author_schema(acceptance, host_scope=True)},
             'review_context': context,
             'rounds': [], 'created_at': now(), 'rollback_scope': {
                 'failed_handoff_ids': [], 'deleted_contexts': [], 'deleted_products': [],
                 'reason': 'Preimplementation contract compilation; no failed model dispatch owns deletable context.'}}
    # Capability code review is not a reason to ask the scientific author to
    # produce the same successful binding again. Reuse only one unambiguous,
    # authenticated reply for the identical source/plan/configuration/cycle;
    # the changed implementation still receives fresh independent review.
    reusable = {}
    legacy_bindings = {}
    legacy_roles = {}
    for old in prior_records:
        if any(old['source'].get(k) != source[k] for k in (
                'design_sha256', 'base_plan_sha256', 'acceptance_sha256', 'config_sha256', 'cycle', 'design_step_sha256')):
            continue
        task = old.get('assignment', {})
        packet = task.get('context', {})
        version2 = task.get('schema', {}).get('properties', {}).get('version', {}).get('const') == 2
        if (task.get('schema') != author_schema(acceptance, host_scope=version2) or packet.get('source_design') != design
                or packet.get('base_plan') != plan or packet.get('acceptance') != acceptance
                or (packet.get('original_assignment_evidence') or {}).get('handoff_id') != (origin or {}).get('handoff_id')):
            continue
        for old_round in old.get('rounds', []):
            reply = old_round['response']
            if reply['unresolved']:
                continue
            try:
                old_role = old.get('author_role', 'director')
                hid = authenticate(engine, run_id, old_role, task, reply)
                if reply['version'] == 1:
                    compile_contract(design, plan, acceptance, reply['bindings'])
                    legacy_bindings[hid] = reply['bindings']
                    legacy_roles[hid] = old_role
                    continue
                if old_role != author_role:
                    continue
                compile_contract(design, plan, acceptance, reply['bindings'],
                    host_threshold=source['host_primary_threshold'],
                    host_primary_criterion=reply.get('host_primary_criterion'))
            except (ValueError, ResearchBlocked):
                continue
            reusable[hid] = task
    if len(reusable) == 1:
        hid, task = next(iter(reusable.items()))
        saved.update(assignment=copy.deepcopy(task), reused_author_handoff_id=hid,
                     review_context=context)
    elif len(legacy_bindings) == 1:
        hid, bindings = next(iter(legacy_bindings.items()))
        context.update(frozen_metric_binding_handoff_id=hid, frozen_metric_bindings=copy.deepcopy(bindings))
        context['frozen_metric_binding_role'] = legacy_roles[hid]
        saved['assignment']['context'].update(frozen_metric_binding_handoff_id=hid, frozen_metric_bindings=copy.deepcopy(bindings),
                                            frozen_metric_binding_role=legacy_roles[hid])
        saved['assignment']['instruction'] += ('\nThe successful v1 metric bindings are supplied as immutable '
            'frozen_metric_bindings. Reuse them exactly; this request adds only the previously undeclared '
            'host primary threshold scope, without rewriting the original reply or criteria.')
    engine.store.put('stage_contexts', key, saved)
    engine.store.event(run_id, KIND + '.prepared', '已冻结 Harness 与 Schema 指标契约对齐工单',
                       {'work_order_id': key, 'source': source})
    return saved


def authenticate(engine, run_id, role, task, result):
    from .engine import Harness
    from .harness_delivery import authenticate as authenticate_delivery
    hid = Harness.decision_key(run_id, role, **{k:task[k] for k in ('purpose','instruction','context','schema')})
    authenticate_delivery(engine, run_id, role, hid, result)
    return hid


async def resolve(engine, harness, run_id, design, plan, acceptance, common):
    from .engine import ResearchBlocked
    record = await to_thread(_freeze, engine, run_id, design, plan, acceptance, common)
    if record.get('status') == 'accepted':
        adopted = {**plan, 'acceptance':acceptance, 'metric_contract':record['contract'],
                   'metric_contract_work_order':record['id']}
        await to_thread(checked, engine, run_id, adopted, design)
        return record['contract'],record['id']
    while True:
        engine.check_active(run_id)
        task = record['assignment']
        author_role = record['author_role']
        reused = record.get('reused_author_handoff_id')
        if record['source'].get('independent_full_review_protocol') == 1 and not reused:
            from . import scientific_author_materials as authors
            if authors.KEY not in task['context']:
                task = {**task,'context':authors.prepare(engine,harness,run_id,task['context'])}
                record['assignment']=task
                engine.store.put('stage_contexts',record['id'],record)
            authored=await authors.ask(engine,harness,run_id,task)
            authors.validate(engine,run_id,authored)
            response=authored['answer']
            task={k:authored[k] for k in ('purpose','instruction','context','schema')}
            record['assignment']=task
            record['author_source_request']=authored
        else:
            response = (engine.store.get('decisions', reused)['result'] if reused
                        else await harness.ask(run_id, author_role, **task))
        author_id = await to_thread(authenticate, engine, run_id, author_role, task, response)
        diagnostics = list(response['unresolved'])
        needs_host_scope = False
        contract = None
        if not diagnostics:
            try:
                fixed_bindings = task['context'].get('frozen_metric_bindings')
                if fixed_bindings is not None and response['bindings'] != fixed_bindings:
                    raise ValueError('Host scope clarification must preserve the successful frozen metric bindings')
                contract = compile_contract(design, plan, acceptance, response['bindings'],
                    host_threshold=record['source']['host_primary_threshold'],
                    host_primary_criterion=response.get('host_primary_criterion'))
            except HostScopeRequired as exc:
                needs_host_scope = True
                diagnostics.append(str(exc))
            except (ValueError, __import__('jsonschema').ValidationError) as exc:
                diagnostics.append(str(exc))
        candidate = signature(response)
        reviews, review_tasks, review_ids = [], {}, {}
        if contract is not None or diagnostics:
            schema = {'type': 'object', 'properties': {
                'source_sha256': {'const': signature(record['source'])},
                'candidate_sha256': {'const': candidate}, 'approve': {'type': 'boolean'},
                'resolution': {'enum': ['approve', 'clarify_source', 'repair_contract']},
                'rationale': {'type': 'string'}, 'required_changes': {'type': 'array', 'items': {'type': 'string'}}},
                'required': ['source_sha256', 'candidate_sha256', 'approve', 'resolution', 'rationale', 'required_changes'],
                'additionalProperties': False}
            for role in record['review_roles']:
                review_tasks[role] = {'purpose': KIND + '_review:' + signature(record['source']) + ':' + candidate,
                    'instruction': REVIEW + ' The missing host scope is a prospective source supplement '
                        'proposed by the assigned design author. Independently judge its fidelity and authority; '
                        'do not demand an already approved declaration for a field absent from the old schema. '
                        'If compiled_contract is null, this is a source clarification pass: do not approve; '
                        'return clarify_source with concrete author actions/targeted readings, or repair_contract '
                        'for a concrete error in the proposed bindings.',
                    'context': {**record.get('review_context', task['context']), 'source_sha256': signature(record['source']),
                        'candidate_sha256': candidate, 'candidate': response, 'compiled_contract': contract,
                        'compiler_diagnostics': copy.deepcopy(diagnostics)}, 'schema': schema}
            if record['source'].get('independent_full_review_protocol') == 1:
                request=review_tasks['director']
                reviews=await full.group(harness,run_id,request['purpose'],request['instruction'],request['context'],schema)
                full.authenticate_complete(engine.store,run_id,reviews)
                for vote in reviews:full.validate_delivery(engine.store,run_id,vote,engine=engine)
                review_ids={vote['role']:vote[full.KEY]['handoff_id'] for vote in reviews}
            else:
                reviews = await gather(*(harness.ask(run_id, role, **review_tasks[role]) for role in review_tasks))
                for role, review in zip(review_tasks, reviews):
                    review_ids[role] = await to_thread(authenticate, engine, run_id, role, review_tasks[role], review)
            for review in reviews:
                if not review['approve'] or review['resolution'] != 'approve' or review['required_changes']:
                    diagnostics.extend(review['required_changes'] or [review['rationale']])
        round_record = {'author_handoff_id': author_id, 'response': response,
                        'reviews': reviews, 'review_handoff_ids': review_ids, 'diagnostics': diagnostics}
        if not record['rounds'] or record['rounds'][-1] != round_record:
            record['rounds'].append(round_record)
        if not diagnostics:
            record.update(status='accepted', contract=contract, updated_at=now())
            engine.store.put('stage_contexts', record['id'], record)
            await to_thread(checked, engine, run_id, {**plan, 'acceptance': acceptance,
                'metric_contract': contract, 'metric_contract_work_order': record['id']}, design)
            engine.store.event(run_id, KIND + '.accepted', '独立评议通过指标契约对齐；原始研究记录保持不变',
                               {'work_order_id': record['id'], 'review_handoff_ids': review_ids})
            return contract, record['id']
        # A new durable child request, never a rewrite of the original reply.
        record['assignment'] = {**task, 'purpose': KIND + ':' + signature(record['source']) + ':clarify:' + str(len(record['rounds'])),
            'context': {**author_context(record.get('review_context', record['assignment']['context'])), 'previous_round': round_record}}
        if needs_host_scope:
            record['assignment']['schema'] = author_schema(acceptance, host_scope=True)
            record['assignment']['instruction'] = AUTHOR
        record.pop('reused_author_handoff_id', None)
        record.pop('author_source_request', None)
        record.update(status='clarifying', updated_at=now())
        engine.store.put('stage_contexts', record['id'], record)
        engine.store.event(run_id, KIND + '.clarification_dispatched', '程序自动重新派发来源或契约歧义澄清',
                           {'work_order_id': record['id'], 'round': len(record['rounds'])})


async def check_adoption(engine, run_id, plan, design, *, verify_plan_sources=False):
    """Keep the resume validation visible and own its worker until it settles."""
    from . import harness_activity
    checker = checked
    if verify_plan_sources:
        from .benchmark_matrix import verify_sources
        # Legacy non-matrix plans still require the metric contract check.
        from .benchmark_matrix import enabled
        if enabled(plan):
            checker = verify_sources
    return await harness_activity.validate(engine, run_id, checker, engine, run_id, plan, design,
        label='核验冻结计划与指标来源', phase='source_validation')


def checked(engine, run_id, plan, design):
    """Reauthenticate an adopted view on resume without rewriting its sources."""
    if not getattr(engine.store, 'is_read_snapshot', False) and hasattr(engine.store, 'read_snapshot'):
        with engine.store.read_snapshot() as snapshot:
            owner = copy.copy(engine)
            owner.store = snapshot
            return checked(owner, run_id, plan, design)
    from .metric_contract import validate_contract
    validate_contract(plan, design)
    if plan['metric_contract'].get('acceptance_source') is not None:
        from .metric_protocol_revisions import checked as checked_revision
        checked_revision(engine, run_id, plan, design)
        return
    record = engine.store.get('stage_contexts', plan.get('metric_contract_work_order', '')) or {}
    source = record.get('source', {})
    run = engine.store.get('runs', run_id)
    if (record.get('run_id') != run_id or record.get('status') != 'accepted'
            or record.get('id') != f'{run_id}:{KIND}:{signature(source)}'
            or record.get('contract') != plan['metric_contract']
            or source.get('config_sha256') != signature(run['config'])
            or plan['metric_contract'].get('host_gate', {}).get('minimum_relative_improvement') != run['config'].get('min_relative_improvement', .01)
            or source.get('cycle') != engine.cycle(run_id)
            or source.get('source_assignment_sha256') != signature(source_assignment(engine, run_id, run))):
        raise ValueError('Metric contract work order/source/configuration/cycle requires renewed Harness review')
    if source.get('independent_full_review_protocol') not in (None,1):
        raise ValueError('Unknown metric full-review protocol')
    last = record['rounds'][-1]
    task = record['assignment']
    authenticate(engine, run_id, record['author_role'], task, last['response'])
    if record.get('author_source_request'):
        from .scientific_author_materials import validate as validate_author
        request=record['author_source_request']
        if ({k:request[k] for k in ('purpose','instruction','context','schema')}!=task or request['answer']!=last['response']):
            raise ValueError('Metric author source packet differs from its actual frozen response')
        validate_author(engine,run_id,request)
    context = task['context']
    if context.get('frozen_metric_binding_handoff_id'):
        from .harness_delivery import authenticate as authenticate_delivery
        original_id = context['frozen_metric_binding_handoff_id']
        original_reply = engine.store.get('decisions', original_id)['result']
        original_context = authenticate_delivery(engine, run_id, context['frozen_metric_binding_role'], original_id, original_reply)
        if (original_reply['bindings'] != context['frozen_metric_bindings']
                or original_context.get('source_design') != design
                or original_context.get('base_plan') != context['base_plan']
                or original_context.get('acceptance') != plan['acceptance']):
            raise ValueError('Frozen metric binding reuse no longer matches its original source receipt')
    if (context['source_design'] != design or context['acceptance'] != plan['acceptance']
            or signature(context['base_plan']) != source['base_plan_sha256']
            or signature(design) != source['design_sha256']
            or signature(plan['acceptance']) != source['acceptance_sha256']
            or last['response']['version'] not in (1, 2) or last['response']['unresolved']
            or context.get('frozen_metric_bindings', last['response']['bindings']) != last['response']['bindings']
            or compile_contract(design, context['base_plan'], plan['acceptance'], last['response']['bindings'],
                host_threshold=run['config'].get('min_relative_improvement', .01),
                host_primary_criterion=last['response'].get('host_primary_criterion')) != plan['metric_contract']):
        raise ValueError('Metric contract differs from its authenticated author/source reply')
    from .engine import ROLES
    whole=source.get('independent_full_review_protocol')==1
    expected_roles = list(full.REVIEWERS) if whole else [role for role in ROLES if role != record['author_role']]
    if (record['author_role'] != source['author_role'] or record['review_roles'] != source['review_roles']
            or record['review_roles'] != expected_roles or len(last.get('reviews', [])) != 2
            or set(last.get('review_handoff_ids', {})) != set(expected_roles)):
        raise ValueError('Metric contract requires exactly two independent role approvals')
    candidate_hash, source_hash = signature(last['response']), signature(source)
    if whole:full.authenticate_complete(engine.store,run_id,last['reviews'])
    for role, review in zip(expected_roles, last['reviews']):
        hid = last['review_handoff_ids'][role]
        frozen = engine.store.get('handoff_inputs', hid) or {}
        task = {'purpose': (engine.store.get('handoffs', hid) or {})['purpose'],
                **{k: frozen[k] for k in ('instruction', 'context', 'schema')}}
        if whole:
            if review['role']!=role or review[full.KEY]['handoff_id']!=hid:
                raise ValueError('Metric full review changed role or handoff')
            full.validate_delivery(engine.store,run_id,review,engine=engine)
            review=full.answer(review)
        else:authenticate(engine, run_id, role, task, review)
        review_context = frozen['context']
        if (review.get('source_sha256') != source_hash or review.get('candidate_sha256') != candidate_hash
                or review_context.get('source_sha256') != source_hash
                or review_context.get('candidate_sha256') != candidate_hash
                or review_context.get('work_order_id') != record['id']
                or review_context.get('candidate') != last['response']
                or review_context.get('compiled_contract') != plan['metric_contract']
                or review['approve'] is not True or review['resolution'] != 'approve' or review['required_changes']):
            raise ValueError('Metric contract lacks matching independent approval')
        from .metric_runtime_scope import KEY
        if KEY in source and review_context.get('runtime_capability') != record['review_context']['runtime_capability']:
            raise ValueError('Metric contract approval does not bind the scoped runtime source')
    _reviewed_capability(record)


def _reviewed_capability(record):
    from . import metric_runtime_scope as scope
    source = record['source']
    reviewed = record.get('review_context', {}).get('runtime_capability', [])
    if reviewed and signature(reviewed) != source['capability_sha256']:
        raise ValueError('Original reviewed metric capability source no longer authenticates')
    if scope.KEY in source and scope.identity(reviewed) != source[scope.KEY]:
        raise ValueError('Original metric runtime scope does not authenticate')
    return reviewed
