"""Reviewed specifications and real local Codex deliveries, with durable replay.

Provider roles retain their configured identities. Local Codex is an executor;
its completion text is never accepted as a file, test, or scientific result.
"""
from __future__ import annotations

from .async_ownership import to_thread as owned_to_thread

import asyncio
import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path

from .storage import now, uid
from .delivery_files import io_path


ROLES = ('director', 'engineer', 'scout')
TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 6000, 'pattern': r'\S'}
STRINGS = {'type': 'array', 'items': TEXT, 'maxItems': 30}


def obj(fields):
    return {'type': 'object', 'properties': fields, 'required': list(fields), 'additionalProperties': False}


from .implementation_module_details import ALGORITHM_MAX_LENGTH

SPEC = obj({'summary': TEXT, 'algorithm': {**TEXT, 'maxLength': ALGORITHM_MAX_LENGTH},
    'branches': {'type': 'array', 'minItems': 3, 'maxItems': 3, 'items': obj({
        'variant': {'enum': ['baseline', 'proposed', 'ablation']}, 'steps': TEXT, 'expected_behavior': TEXT})},
    'data_contract': obj({key: TEXT for key in ['dataset', 'source', 'split_identity', 'leakage_prevention']}),
    'parameter_contract': TEXT, 'metrics': STRINGS, 'dependencies': STRINGS,
    'acceptance_checks': {'type': 'array', 'minItems': 1, 'maxItems': 64,
        'items': obj({'id': TEXT, 'requirement': TEXT, 'verification': TEXT})},
    'implementation_map': {'type': 'array', 'minItems': 1, 'maxItems': 64,
        'items': obj({'requirement_id': TEXT, 'location': TEXT, 'behavior': TEXT})},
    'limitations': STRINGS})
# This optional contract is part of the immutable SPEC and its independent
# approval. Old specifications remain valid and do not enable transport merely
# because their prose mentions it.
from .comparison_transport import CONTRACT_SCHEMA as COMPARISON_TRANSPORT_SCHEMA
SPEC['properties']['comparison_transport'] = {
    'anyOf': [{'type': 'null'}, copy.deepcopy(COMPARISON_TRANSPORT_SCHEMA)]}
SPEC['properties']['diagnostic_payload_arrays'] = {
    **copy.deepcopy(COMPARISON_TRANSPORT_SCHEMA['properties']['payload_arrays']), 'minItems': 1}
SPEC['properties']['supplemental_timing'] = obj({
    'entrypoint': TEXT, 'report_path': TEXT, 'deadline_check_entrypoint': TEXT,
    'transport_reserve_bytes': {'type': 'integer', 'minimum': 0},
    'resource_caps': obj({k: {'type': 'integer', 'minimum': 1}
                         for k in ('memory_mb', 'disk_mb', 'cpu_cores', 'gpu_memory_mb')}),
    'environment': {'type': 'object', 'minProperties': 1, 'additionalProperties': TEXT}})
FIX = obj({'issue_index': {'type': 'integer', 'minimum': 0}, 'location': TEXT, 'change': TEXT, 'verification': TEXT})
REVIEW = obj({'approve': {'type': 'boolean'}, 'proposal_hash': TEXT, 'spec_hash': TEXT,
    'rationale': TEXT, 'issues': STRINGS, 'fixes': {'type': 'array', 'items': FIX, 'maxItems': 30}})
DELIVERY_REVIEW = obj({**REVIEW['properties'], 'code_sha256': TEXT, 'delivery_hash': TEXT})

# Persisted workflow inputs remain frozen; new dispatches also receive the
# current directory-layout contract without rewriting an approved SPEC.
# A plan describes intended locations; only the later delivered manifest can
# claim actual file locations and describe completed verification.
from .delivery_manifest_contract import schema as manifest_schema
MANIFEST_SCHEMA = manifest_schema()
FORMAT_CONTRACT = {
    'version': 2,
    'planning_document': {'name': 'implementation_spec',
        'mapping_schema': obj({name: SPEC['properties'][name] for name in ('acceptance_checks', 'implementation_map')}),
        'scope': '审批前的计划：此处仅列映射相关字段，完整SPEC输出schema仍是规范作者的必需合同。implementation_map必须使用requirement_id/location/behavior；不是最终交付JSON。'},
    'delivery_document': {'name': 'implementation.json', 'schema': MANIFEST_SCHEMA,
        'scope': '代码交付后的真实文件清单：requirements 使用 id/locations[]/verification；不能直接复制计划行。'},
    'conversion': [
        '以 acceptance_checks 的完整 id 关联 implementation_map.requirement_id，不截短或改名验收项ID。',
        '同一验收项可以有多个计划行。最终为每个 acceptance_checks.id 形成一个 requirements 项，其 id 原样一致。',
        '将该ID的计划 location 汇总并去重；用明确的相对源码路径，可在冒号后注明符号，例如 models/predictor.py:predict。按职责列模块和测试；交付清单指向真实文件。',
        'verification 以同ID acceptance_checks.verification 为验证要求，交付时描述实际检查和未完成部分；behavior 只描述实现职责，不能冒充验证方法或通过结果。',
        '计划阶段保留 implementation_map 的必需字段，不要求把 requirement_id/location/behavior 改成 manifest 字段；应审查转换说明、ID覆盖和计划合理性。',
        '交付阶段独立检查 requirements 完整覆盖所有验收项、实际文件位置、验证说明及固定本地验收；任何真实缺项、不存在的文件、未通过检查或否决都仍阻止接受。'],
    'illustration_only_not_artifact_evidence': {
        'acceptance_check': {'id': 'AC-example', 'requirement': '按固定参数计算真实分支', 'verification': '固定输入运行并核对分支与输出'},
        'planning_rows': [{'requirement_id': 'AC-example', 'location': 'experiment.py:main', 'behavior': '选择并计算对应模型'}],
        'delivery_row_shape': {'id': 'AC-example', 'locations': ['experiment.py:main'],
            'verification': '记录实际执行的检查及结果；未执行时如实说明，不能复制这个示例作为通过证据。'}}}
# Used only by new planning packets. Old paid packets retain their own schema.
FORMAT_CONTRACT['delivery_document']['host_binding'] = (
    'When document_protocol=2, the writer contract additionally requires the exact '
    'approved implementation_document_sha256; planning has no approved draft SHA yet.')
def _code_contract(engine, *, format_v2):
    from .code_bundle import INSTRUCTION as layout_instruction
    original = engine.code_instruction() + '\n' + layout_instruction
    if not format_v2:
        return original
    start = 'Last stdout line must be JSON '
    end = '. Compute fingerprints from actual split sample IDs'
    if original.count(start) != 1 or original.count(end) != 1:
        _fail('无法明确更新新版本输出格式示例，未派发实现请求')
    prefix, remainder = original.split(start, 1)
    _, suffix = remainder.split(end, 1)
    example = {'metrics': {'<metric declared for this phase>': '<finite measured number>'}, 'evaluation': {
        'dataset_id': 'actual raw dataset content/version identity',
        'split_fingerprints': {phase: f'SHA256 of actual {phase} sample IDs' for phase in ('train', 'validation', 'test')},
        'seed': '<invocation seed>', 'phase': '<invocation phase>',
        'actual_params': '<complete invocation JSON object>'}}
    return (prefix + start + '(complete structural example only): ' + json.dumps(example, ensure_ascii=False) +
        '. These illustrative values are not measured results or a replacement parameter plan. '
        'Use the actual finite measured metric names/values for the requested phase; evaluation.actual_params '
        'must contain every entry of the actual input JSON unchanged, including any design-specific parameters, '
        'and match the parameters used. Each split fingerprint must be the actual 64-character SHA256, not the '
        'example text. Never omit actual_params or treat this example as permission to hardcode metrics' + end + suffix)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _fail(message, *, kind=None, details=None):
    from .engine import ResearchBlocked, ResearchRevisionRequired
    from .implementation_failures import ImplementationSpecificationBlocked, SPECIFICATION_FAILURE_KINDS
    from .implementation_failures import ImplementationCodeBlocked, CODE_FAILURE_KINDS
    if kind in CODE_FAILURE_KINDS:
        raise ImplementationCodeBlocked(message, kind=kind, details=details)
    if kind in SPECIFICATION_FAILURE_KINDS:
        raise ImplementationSpecificationBlocked(message, kind=kind, details=details)
    if kind:
        raise ResearchRevisionRequired(message, kind=kind, details=details)
    raise ResearchBlocked(message)


def _validate_spec(spec, context=None, *, design=None, algorithm_max_length=None):
    from .intelligence import parse_json
    from . import implementation_alignment as alignment
    schema = alignment.author_schema(SPEC) if 'architecture_traceability' in spec else SPEC
    if algorithm_max_length is not None:
        schema = copy.deepcopy(schema)
        schema['properties']['algorithm']['maxLength'] = algorithm_max_length
    parse_json(json.dumps(spec, ensure_ascii=False), schema)
    if {row['variant'] for row in spec['branches']} != set(('baseline', 'proposed', 'ablation')):
        _fail('实现规范必须逐一覆盖baseline/proposed/ablation')
    ids = [row['id'] for row in spec['acceptance_checks']]
    mapped = [row['requirement_id'] for row in spec['implementation_map']]
    if len(set(ids)) != len(ids) or set(mapped) != set(ids):
        _fail('实现规范验收项ID重复或实现映射不完整')
    if spec.get('comparison_transport') is not None:
        from .comparison_transport import validate_contract
        validate_contract(spec['comparison_transport'])
    if 'diagnostic_payload_arrays' in spec:
        from .comparison_protocol_repair import extend
        if not spec.get('comparison_transport'):
            _fail('诊断数组声明需要已声明的比较传输合同')
        extend(spec['comparison_transport'], spec['diagnostic_payload_arrays'])
    if 'supplemental_timing' in spec:
        from .hrp_host_timing import task_contract
        task_contract(spec)
    if context is not None:
        alignment.validate_spec(spec, context, design=design)


def _frozen_spec_assignment(engine, run_id, purpose, role):
    """Reuse exact saved specification assignments across a policy deployment."""
    from .engine import Harness, digest
    with engine.store._read_db() as db:
        rows = db.execute("SELECT h.key,json_extract(h.data,'$.input_hash'),i.data FROM records h JOIN records i "
            "ON i.collection='handoff_inputs' AND i.key=h.key "
            "WHERE h.collection='handoffs' AND json_extract(h.data,'$.run_id')=? "
            "AND json_extract(h.data,'$.to_role')=? "
            "AND json_extract(h.data,'$.purpose')=? ORDER BY h.updated_at DESC", (run_id, role, purpose)).fetchall()
    if len(rows) > 1:
        _fail('实现规范存在多个原始交接，须核对已保存输入后继续。')
    if not rows:
        return None
    key, input_hash, raw = rows[0]
    saved = json.loads(raw)
    payload = {name: saved.get(name) for name in ('instruction', 'context', 'schema')}
    if (saved.get('id') != key or saved.get('run_id') != run_id or input_hash != digest(payload)
            or Harness.decision_key(run_id, role, purpose, **payload) != key):
        _fail('实现规范冻结输入的身份或哈希已改变。')
    return saved


def _frozen_spec_author(engine, run_id, purpose):
    return _frozen_spec_assignment(engine, run_id, purpose, 'engineer')


def _checked_workspace(project_root, run_id, workspace, *, create=False):
    from .codex_workspace import checked
    try:
        return checked(project_root, run_id, workspace, create=create)
    except ValueError as exc:
        _fail(str(exc))


def _snapshot(engine, run_id, workspace, workflow_id, revision, project_root):
    """Stream regular files into an immutable snapshot without delivery size caps."""
    from .delivery_files import identity, copy_verified, io_path
    from .delivery_manifest import select
    root = engine.run_dir(run_id)
    workspace = _checked_workspace(project_root, run_id, workspace)
    if not workspace.is_dir():
        _fail('Codex工作目录不存在')
    try:
        sources, selection = select(workspace)
        files = [{'path': path.relative_to(workspace).as_posix(), **identity(path)} for path in sources]
    except ValueError as exc:
        _fail(str(exc))
    if not {'experiment.py', 'implementation.json'} <= {row['path'] for row in files}:
        return None, ['缺少实际experiment.py或implementation.json交付文件']
    target = root / 'codex' / _digest(workflow_id) / 'deliveries' / f'attempt-{revision}'
    io_path(target).mkdir(parents=True, exist_ok=True)
    try:
        for row in files:
            copy_verified(workspace / row['path'], target / row['path'],
                          {key: row[key] for key in ('sha256', 'bytes')})
        current_sources, current_selection = select(workspace)
        if current_selection != selection or [path.relative_to(workspace).as_posix() for path in current_sources] != [row['path'] for row in files]:
            _fail('冻结期间交付目录发生变化')
        if any(identity(workspace / row['path']) != {key: row[key] for key in ('sha256', 'bytes')} for row in files):
            _fail('冻结期间交付文件内容发生变化')
    except ValueError as exc:
        _fail(str(exc))
    return {'workspace': str(target), 'files': files, 'files_hash': _digest(files),
            **({'selection': selection} if selection else {})}, []


def _verify_snapshot(engine, run_id, snapshot):
    from .delivery_files import regular_files, identity
    root = engine.run_dir(run_id) / 'codex'
    directory = Path(snapshot['workspace'])
    if not directory.resolve().is_relative_to(root):
        _fail('交付快照不属于当前任务')
    expected = {row['path']: row for row in snapshot['files']}
    actual = set()
    try:
        for path in regular_files(directory, skip_git=False):
            name = path.relative_to(directory).as_posix()
            actual.add(name)
            if name not in expected or identity(path) != {key: expected[name][key] for key in ('sha256', 'bytes')}:
                _fail('冻结交付快照内容已变化')
    except ValueError as exc:
        _fail(str(exc))
    if actual != set(expected):
        _fail('冻结交付快照文件缺失')


def validate_program(engine, run_id, program, *, source_cycle=None, source_only=False, validation_inputs=False):
    """Recheck accepted bytes and both sets of exact three-role approvals."""
    if source_only and source_cycle is not None and engine.cycle(run_id) != source_cycle:
        # A read-only historical proof must replay its original cycle checks.
        # Never rewrite live state or turn this lens into execution authority.
        import copy
        from .architecture_validation import operation
        reader = copy.copy(engine)
        reader.cycle = lambda _: source_cycle
        return operation(validate_program)(reader, run_id, program, source_cycle=source_cycle, source_only=True)
    if program.get('dataset_runtime_id') and not program.get('rapid_revision_id'):
        from .dataset_experiments import validate_program as validate_dataset_program
        return validate_dataset_program(engine, run_id, program)
    if source_cycle is not None and not source_only:
        _fail('Historical code validation is read-only; it cannot authorize a new execution')
    if program.get('rapid_revision_id'):
        from .rapid_experiment_revision import validate_program as validate_amendment
        return validate_amendment(engine, run_id, program)
    if hashlib.sha256(program['code'].encode('utf-8')).hexdigest() != program.get('code_sha256'):
        _fail('待执行代码与Codex已批准代码哈希不一致')
    record = engine.store.get('codex_workflows', program.get('delivery_id', '')) or {}
    accepted = record.get('result', {})
    consensus = engine.store.get('consensus', run_id) or {}
    if (record.get('run_id') != run_id or record.get('status') != 'completed'
            or record.get('cycle') != (source_cycle if source_cycle is not None else engine.cycle(run_id))
            or record.get('proposal_hash') != consensus.get('proposal_hash')
            or record.get('proposal_hash') != _digest(record.get('common', {}).get('proposal'))
            or any(accepted.get(field) != program.get(field) for field in
                   ['code_sha256', 'spec_hash', 'delivery_hash', 'code', 'dependencies'])):
        _fail('待执行程序缺少同任务的完整Codex验收记录')
    if any(accepted.get(field) != program.get(field) for field in
           ('hyperparameter_yaml_protocol', 'hyperparameter_contract','reviewed_execution_phases')):
        _fail('YAML 运行合同与已批准交付不一致')
    specification = next((s for s in record.get('specifications', [])
                          if s.get('approved') and s.get('spec_hash') == program.get('spec_hash')), None)
    from . import implementation_cycle as cycle_flow
    compact = cycle_flow.active(record)
    if compact != (program.get('implementation_review_strategy') == cycle_flow.STRATEGY):
        _fail('程序与宿主保存的实现流程版本不一致。')
    if not compact:
        _validate_approved_spec(specification, record.get('proposal_hash'))
    from .implementation_documents import validate_review, verify_bundle
    from . import implementation_scope_review as scope_review
    required_scope = record.get('document_scope_protocol') == scope_review.VERSION or record.get(scope_review.ARCHITECTURE_FIELD)
    if compact and not specification:
        _fail('缺少同版本批准的实现文档。')
    if required_scope and not compact and not specification.get('implementation_definition_repair_protocol'):
        scope_review.authenticate_release(engine, run_id, record, specification, ROLES)
    if compact:
        cycle_flow.validate_document(engine,run_id,record,specification)
    elif record.get('pre_codex_spec_source') and not specification.get('implementation_definition_repair_protocol'):
        from .pre_codex_restart import inherited_spec
        inherited = inherited_spec(engine, run_id, record['id'])
        if not inherited or record['pre_codex_spec_source'] != inherited[0]['id'] or specification != inherited[1]:
            _fail('待执行代码缺少已回退工作流所绑定的原规范。')
    else:
        validate_review(engine, run_id, record, specification)
    verify_bundle(record['input_bundle'])
    if program.get('implementation_document_sha256') != specification['document']['sha256']:
        _fail('待执行代码没有绑定批准的实现 Markdown')
    votes = program.get('reviews', [])
    from . import independent_full_review as full_review
    from . import code_whole_review as whole_code
    whole_delivery = not compact and full_review.reviewers(votes) == full_review.REVIEWERS
    if compact:
        if not all(v['result']['approve'] for v in votes):
            _fail('待执行代码仍有未解决意见。')
    elif (not full_review.members(votes)
            or any(v.get('approve') is not True or v.get('issues') or v.get('fixes')
                   or any(v.get(field) != program.get(field) for field in ['spec_hash', 'code_sha256', 'delivery_hash'])
                   or v.get('proposal_hash') != record.get('proposal_hash') for v in votes)):
        _fail('待执行代码未获三方同哈希无异议复审')
    attempt = next((a for a in record['attempts'] if a.get('status') == 'approved'
                    and a.get('code_sha256') == program['code_sha256']
                    and a.get('delivery_hash') == program.get('delivery_hash')), None)
    if (not attempt or not attempt.get('acceptance', {}).get('approved')
            or attempt.get('clarification_approval_votes', attempt.get('review_votes')) != votes
            or attempt.get('delivery_hash') != program.get('delivery_hash')
            or attempt.get('acceptance', {}).get('delivery_hash') != program.get('delivery_hash')):
        _fail('待执行代码没有通过固定本地验收')
    if compact:
        cycle_flow.check_votes(engine,run_id,votes,cycle_flow.code_materials(engine,run_id,record,specification,attempt))
    from .code_model_migration import validate_program_profile
    try:
        validate_program_profile(engine, run_id, record, attempt, program)
    except ValueError as exc:
        _fail('代码 worker 模型来源校验失败：' + str(exc))
    from .runtime_training_policy import active as active_runtime_policy
    runtime_policy = None if source_cycle is not None else active_runtime_policy(engine.store, run_id, program)
    # The validation scheduler first binds the reviewed code. Each authorized
    # job still has to pass Engine._trial's live hardware assessment to launch.
    pending_validation = (validation_inputs and compact
        and engine.store.get('runs',run_id)['stage'] in ('experiments','tuning')
        and attempt['acceptance'].get('resource_admission')=={'approved':False,'reason':'current_delivery_execution_evidence_pending'})
    if (attempt['acceptance'].get('code_conformance_only') and not pending_validation
            and attempt['acceptance'].get('resource_admission',{}).get('approved') is not True
            and runtime_policy is None and not source_only):
        # An evidence hold is not a rejected scientific proposal. In particular,
        # _fail(kind=unknown) would route into ResearchRevisionRequired.
        from .engine import ResearchBlocked
        raise ResearchBlocked('代码符合性已核对；当前版本的资源准入证据尚未齐备，未启动训练。')
    from . import implementation_alignment as alignment
    from .implementation_alignment_evidence import validate_vote
    review_purpose = attempt.get('review_purpose') or f'code_review:{_digest(record["id"])}:{attempt["index"]}'
    from . import code_review_partition as review_partitions
    reviewed_batch = review_partitions.load(engine.store, run_id, review_purpose)
    plan = attempt.get(review_partitions.PLAN_FIELD)
    if plan and whole_delivery:
        review_partitions.validate_writer_instruction(attempt,record['id'],specification['spec_hash'])
        review_partitions.check_delivery_plan(plan,record['id'],attempt['index'],specification['spec_hash'],attempt['snapshot'])
    if not whole_delivery and plan and plan['body'].get('previous_review_batch_sha256'):
        review_partitions.validate_writer_instruction(attempt, record['id'], specification['spec_hash'])
        previous_batch = review_partitions.load(engine.store, run_id, plan['body']['previous_review_purpose'])
        if (not reviewed_batch or reviewed_batch['source_plan_sha256'] != plan['sha256']
                or not previous_batch or review_partitions.sha(previous_batch) != plan['body']['previous_review_batch_sha256']):
            _fail('代码复审没有绑定写码前冻结的原批次和接口义务。')
    if required_scope and not whole_delivery and not compact:
        release = scope_review.release_contract(record, specification)
        review_partitions.validate_writer_instruction(attempt, record['id'], specification['spec_hash'], release=release)
        if (not reviewed_batch or plan['sha256'] != reviewed_batch['source_plan_sha256']
                or plan['body'].get('scope_revision_document') != reviewed_batch.get('scope_revision_document')
                or plan['body']['document_scope_release_sha256'] != release['sha256']):
            _fail('代码复审未绑定原批准分工及实现文档修订。')
    if attempt.get('clarification_approval_votes'):
        from .code_repair_clarification import verify_saved
        from .code_repair_deferred import approval_record
        clarification_record = approval_record(attempt)
        resolved = verify_saved(engine, run_id, record, attempt, clarification_record)
        if resolved['issues'] or votes != clarification_record['votes']:
            _fail('原交付的意见澄清尚未取得三方无异议复审。')
        review_purpose = clarification_record['purpose']
    partition_contexts = {}
    if whole_delivery:
        if not whole_code.enabled(record):
            _fail('Full code review lacks its approved whole-document policy')
        whole_code.validate_release(engine, run_id, record, specification, attempt, votes)
    for vote in ([] if whole_delivery or compact else votes):
        frozen = _frozen_spec_assignment(engine, run_id,
            review_purpose, vote['role']) or {}
        context = frozen.get('context', {})
        hid = vote.get('handoff_id')
        if (not alignment.enabled(context) or hid != frozen.get('id')
                or context.get('design_hash') != _digest(record['common']['design'])
                or context.get('experiment_plan_hash') != _digest(record['common'].get('experiment_plan') or {})
                or context.get('spec_hash') != specification['spec_hash']
                or (context.get('implementation_document') or {}).get('sha256') != specification['document']['sha256']
                or context.get('code_sha256') != program['code_sha256']
                or context.get('delivery_hash') != program['delivery_hash']):
            _fail('待执行代码的架构、实现文档与交付评审来源不一致')
        validate_vote(engine, run_id, vote['role'], hid,
            {k: v for k, v in vote.items() if k not in {'role', 'handoff_id'}}, context, delivery=True)
        partition_contexts[vote['role']] = context
    from . import code_review_partition as partitions
    if any(partitions.FIELD in context for context in partition_contexts.values()):
        try:
            batch = partitions.load(engine.store, run_id, review_purpose)
            if not batch or any(batch['assignments'][role]['context'] != context
                                for role, context in partition_contexts.items()):
                raise ValueError('待执行评审与冻结职责范围不一致')
            partitions.validate_batch(batch, ROLES, votes)
        except (ValueError, KeyError) as exc:
            _fail(str(exc))
    _verify_snapshot(engine, run_id, attempt['snapshot'])
    from .code_repair_contract import validate_delivery as validate_repair_delivery
    validate_repair_delivery(record, attempt)
    code_path = Path(attempt['snapshot']['workspace']) / 'experiment.py'
    if hashlib.sha256(io_path(code_path).read_bytes()).hexdigest() != program['code_sha256']:
        _fail('已批准代码哈希与冻结交付快照不一致')
    from .code_bundle import describe
    manifest = json.loads(io_path(code_path.parent / 'implementation.json').read_text(encoding='utf-8'))
    original_bundle = describe(code_path.parent, manifest, attempt['snapshot']['files'])
    if runtime_policy:
        if runtime_policy['base_bundle'] != original_bundle:
            _fail('运行策略修订与原批准交付目录不一致')
        return runtime_policy['bundle']
    return original_bundle


def _validate_approved_spec(specification, proposal_hash):
    if not specification or _digest(specification.get('spec')) != specification.get('spec_hash'):
        _fail('已批准的Codex实现规范缺失或哈希已变化')
    from . import implementation_definition_repair as definitions
    length = None
    if specification.get(definitions.PROTOCOL) == 1:
        base_length = specification.get('definition_base_algorithm_length')
        if type(base_length) is not int or base_length < 1:
            _fail('补定义文档缺少原批准正文的容量绑定')
        length = base_length + definitions.MAX_ADDITION
    _validate_spec(specification['spec'], algorithm_max_length=length)
    from . import implementation_acceptance as acceptance
    if acceptance.enabled(specification):
        if (specification.get('approved') is not True
                or specification.get('review_outcome') not in acceptance.APPROVED
                or not specification.get('acceptance_contract_sha256')):
            _fail('实现文档没有当前合同的确定性批准结论')
        return  # The caller additionally authenticates the persisted release evidence.
    votes = specification.get('review_votes', [])
    from .independent_full_review import members
    if (not members(votes)
            or any(v.get('approve') is not True or v.get('issues') or v.get('fixes')
                   or v.get('spec_hash') != specification['spec_hash']
                   or v.get('proposal_hash') != proposal_hash for v in votes)):
        _fail('Codex实现规范未获三方同哈希无异议批准')
    if (specification.get('document_protocol') != 2 or specification.get('implementation_indexed_review_protocol') != 1
            or any(v.get('document_sha256') != specification['document']['sha256'] for v in votes)):
        _fail('Codex实现规范评议没有绑定同一 Markdown')


def _terminal_resume(engine, run_id, state, attempt):
    """A later explicit start may consume one confirmed terminal turn, never unknown."""
    receipt = attempt.get('checkpoint') or {}
    if (receipt.get('status') not in {'failed', 'interrupted'}
            or not receipt.get('thread_id') or not receipt.get('turn_id')
            or receipt.get('run_id') != run_id
            or receipt.get('operation_id') != attempt['operation_id']
            or receipt.get('instruction_sha256') != attempt['instruction_sha256']):
        return False
    run = engine.store.get('runs', run_id) or {}
    maximum = run.get('config', {}).get('codex_max_revisions', 3)
    if maximum is not None and attempt['index'] + 1 >= maximum:
        return False  # Do not spend an authorization when no successor can be admitted.
    generation = engine.store.get('operator_resumes', run.get('state', {}).get('resume_generation', '')) or {}
    if (generation.get('run_id') != run_id or generation.get('kind') != 'explicit_start_command'
            or not generation.get('id') or generation['id'] in state.get('consumed_resume_generations', [])
            or attempt.get('resumed_by_generation')):
        return False
    try:
        if datetime.fromisoformat(generation['created_at']) <= datetime.fromisoformat(attempt['confirmed_terminal_at']):
            return False
    except (KeyError, TypeError, ValueError):
        return False
    attempt.update(status='rejected', resumed_by_generation=generation['id'],
                   issues=[f"原Codex回合已确认{receipt['status']}，后续显式恢复授权一次有界续接。"
                           '先核对同一工作目录中已有代码、交付清单和开发检查结果，继续未完成的交付项；'
                           '保留已完成且符合批准规范的实现，不从头重写。开发检查不代表宿主验收或三方复审通过。'])
    state.setdefault('consumed_resume_generations', []).append(generation['id'])
    return True


def _snapshot_artifacts(engine, run_id, snapshot):
    records = []
    for row in snapshot['files']:
        path = Path(snapshot['workspace']) / row['path']
        name = path.relative_to(engine.run_dir(run_id)).as_posix()
        key = f'{run_id}:{name}'
        existing = engine.store.get('artifacts', key)
        if existing and (existing.get('sha256') != row['sha256'] or existing.get('path') != str(path)):
            _fail('交付文件artifact身份发生冲突')
        if not existing:
            engine.store.put('artifacts', key, {'id': key, 'run_id': run_id, 'name': name, 'path': str(path),
                'kind': 'codex_delivery', 'sha256': row['sha256'], 'created_at': now()})
        records.append({'artifact_id': key, **row})
    return records


def _execution_inputs(engine, run_id):
    """Bind mutable execution inputs without decoding archived stage payloads."""
    run = engine.store.get('runs', run_id)
    with engine.store._read_db() as db:
        records = db.execute("SELECT collection,key,data FROM records WHERE "
            "(collection='consensus' AND key=?) OR "
            "(collection='stage_contexts' AND key=?) ORDER BY collection,key",
            (run_id, run_id + ':implementation')).fetchall()
    return _digest({'config': run['config'], 'cycle': engine.cycle(run_id),
        'sources': [(collection, key, hashlib.sha256(raw.encode()).hexdigest())
                    for collection, key, raw in records]})


async def _read_execution_gate(engine, run_id):
    """Validate one admission off-loop; never retain its proof across awaits."""
    from .architecture_validation import operation
    from .engine import ResearchBlocked

    engine.check_active(run_id)
    guard = _execution_inputs(engine, run_id)
    await engine._read_consensus_check(run_id, operation(engine._execution_gate), run_id)
    engine.check_active(run_id)
    if guard != _execution_inputs(engine, run_id):
        raise ResearchBlocked('实现执行的任务配置、共识或冻结输入在校验期间发生变化，请按当前状态恢复。')


def writer_instruction(instruction, document_sha256):
    from . import delivery_manifest_contract, request_hash_policy
    return request_hash_policy.instruction(instruction + delivery_manifest_contract.instruction(document_sha256))


async def _workflow_identity(engine, run_id, common, config, repair):
    prior = (repair or {}).get('program', {})
    settings = {'project_root': prior.get('codex_project_dir') or common.get('codex_project_dir') or config.get('codex_project_dir'),
                'model': prior.get('codex_model', common.get('codex_model', config.get('codex_model', ''))),
                'effort': prior.get('codex_reasoning_effort', common.get('codex_reasoning_effort', config.get('codex_reasoning_effort', '')))}
    identity = {'cycle': engine.cycle(run_id), 'kind': 'runtime_repair' if repair else 'implementation',
                'proposal_hash': _digest(common['proposal']), 'design': common['design'],
                'experiment_plan': common.get('experiment_plan'), 'repair': repair,
                'codex_settings': settings, 'document_protocol': 2}
    from .implementation_restart import identity_generation
    generation = await engine._read_consensus_check(run_id, identity_generation, engine, run_id, common)
    if generation:
        identity['implementation_generation'] = generation
    return identity


async def implement(engine, h, run_id, common, *, repair=None):
    """Write and review the document, then write and review the code delivery."""
    from .codex_delivery import verify_codex_delivery
    from .local_codex import LocalCodexClient, LocalCodexError
    from . import implementation_documents as documents
    from . import implementation_cycle as cycle_flow

    await _read_execution_gate(engine, run_id)
    run = engine.store.get('runs', run_id)
    config = run['config']
    identity = await _workflow_identity(engine, run_id, common, config, repair)
    cycle, kind, proposal_hash = (identity[k] for k in ('cycle', 'kind', 'proposal_hash'))
    settings = identity['codex_settings']
    implementation_generation = identity.get('implementation_generation')
    key = f'{run_id}:codex:{_digest(identity)}'
    state = engine.store.get('codex_workflows', key)
    new_workflow = state is None
    inherited = None
    if implementation_generation and kind == 'implementation':
        from .pre_codex_restart import inherited_spec
        inherited = await engine._read_consensus_check(run_id, inherited_spec, engine, run_id, key)
    if not state:
        from .config import ROOT
        prior = (repair or {}).get('program', {})
        project_root = Path(settings['project_root'] or ROOT)
        from .codex_workspace import allocate
        workspace = (Path(prior['codex_workspace']) if prior.get('codex_workspace') else
                     allocate(project_root, run_id, key, run.get('title'), cycle, kind))
        workspace = _checked_workspace(project_root, run_id, workspace, create=True)
        state = {'id': key, 'run_id': run_id, 'cycle': cycle, 'kind': kind, 'backend': 'local_codex',
            'format_contract_version': 2, 'document_protocol': 2,
            'implementation_review_strategy': cycle_flow.STRATEGY,
            'implementation_document_authoring_protocol': 1,
            'document_read_window_protocol': 1, 'codex_prompt_protocol': 1,
            'implementation_owner': 'codex', 'architecture_convergence_protocol': 1,
            'status': 'specification', 'workspace': str(workspace), 'project_root': str(project_root.resolve()),
            'proposal_hash': proposal_hash, 'model': settings['model'], 'reasoning_effort': settings['effort'],
            'thread_id': prior.get('codex_thread_id'), 'turn_id': None, 'current_revision': 0,
            'specifications': [], 'attempts': [], 'created_at': now(), 'input_hash': _digest(identity),
            'common': common, 'repair': repair}
        from .dataset_contract import from_design
        request_catalog = from_design(common['design'])
        if request_catalog:
            state['common'] = {**common, 'dataset_catalog': copy.deepcopy(request_catalog), 'dataset_request_protocol': 2}
        if config.get('hyperparameter_yaml_protocol') == 1:
            state['hyperparameter_yaml_protocol'] = 1
            from .yaml_migration import requested as yaml_request
            requested_yaml = yaml_request(engine, run_id)
            if requested_yaml:
                state['yaml_configuration_request'] = copy.deepcopy(requested_yaml)
                state['yaml_configuration_scope'] = requested_yaml['body'].get('configuration_scope', 'hyperparameters')
        if implementation_generation:
            state['implementation_generation'] = implementation_generation
        if inherited:
            source, spec = inherited
            state['specifications'] = [copy.deepcopy(spec)]
            state['spec_hash'] = spec['spec_hash']
            state['pre_codex_spec_source'] = source['id']
            # Scientific approval stays attributable to the original author
            # and acceptance ledger; the new writer starts with an empty tree.
            state['common'] = copy.deepcopy(source['common'])
        with engine.store._read_db() as db:
            previous_count, previous_version = db.execute(
                "SELECT count(*), max(CASE WHEN json_type(data,'$.output_version')='integer' "
                "THEN json_extract(data,'$.output_version') ELSE 0 END) FROM records "
                "WHERE collection='codex_workflows' AND json_extract(data,'$.run_id')=?",
                (run_id,)).fetchone()
        state['output_version'] = max(previous_count, previous_version or 0) + 1
        if kind == 'implementation':
            # Preserve the original lookup/input identity. Only a genuinely new
            # workflow may inherit the run's explicit transport-only profile.
            from .code_model_migration import bind_workflow
            bind_workflow(engine, run_id, state)
    def save(**fields):
        state.update(fields, updated_at=now())
        from .codex_accounting import capture_usage
        records = [capture_usage(engine.store, run_id, attempt.get('checkpoint')) for attempt in state['attempts']]
        engine.store.batch([('codex_workflows', key, state),
            *[('codex_usage', row['id'], row) for row in records if row]])

    if new_workflow:
        save()
    if state.get('status') == 'completed':
        validate_program(engine, run_id, state['result'], source_only=bool(repair))
        return state['result']
    if not cycle_flow.active(state):
        from .engine import ResearchBlocked
        raise ResearchBlocked('旧实现请求已冻结。请通过实现阶段恢复入口归档未完成的交接，再按文档与代码流程继续。')
    maximum = config.get('codex_max_revisions', 3)
    yaml_contract = ''
    code_contract = _code_contract(engine, format_v2=True)
    if state['common'].get('dataset_catalog'):
        from .dataset_contract import CONTEXT_GUIDANCE
        code_contract += '\n' + CONTEXT_GUIDANCE
    if state.get('hyperparameter_yaml_protocol') == 1:
        from .yaml_migration import instructions as yaml_instructions
        yaml_contract = yaml_instructions(state.get('yaml_configuration_scope', 'hyperparameters'))
        if state.get('yaml_configuration_request'):
            yaml_contract += '\n用户明确配置要求：\n' + state['yaml_configuration_request']['body']['request']
        code_contract += '\n' + yaml_contract
    latest = state['specifications'][-1] if state['specifications'] else None
    from . import implementation_document_revision as revisions
    amendment = revisions.current(engine,run_id,state)
    if latest and latest.get('approved') is True and (not amendment or latest['index']!=amendment['base_index']):
        specification = latest
    else:
        common = await documents.planning_context_async(engine,h,run_id,state['common'],code_contract,
            FORMAT_CONTRACT,repair,protocol=2,read_window_protocol=1)
        specification = await cycle_flow.document(engine,h,run_id,state,common,save)
    spec, spec_hash = specification['spec'], specification['spec_hash']
    cycle_flow.validate_document(engine,run_id,state,specification)
    if state.get('spec_hash') != spec_hash:
        # Older definition transitions left this display/current-version pointer
        # on the base SPEC. Restore it only after authenticating the new approval.
        save(spec_hash=spec_hash)
    if not state.get('input_bundle') or state['input_bundle']['documents'][0]['sha256'] != specification['document']['sha256']:
        save(input_bundle=documents.export_bundle(engine, run_id, state, specification))
    documents.verify_bundle(state['input_bundle'])
    secret_names = tuple(p.get('api_key_env') for p in getattr(h.intelligence, 'settings', {}).get('providers', []) if p.get('api_key_env'))
    client = getattr(engine, 'codex_client', None) or LocalCodexClient(
        timeout=run['config'].get('codex_timeout_seconds', 1800), blocked_env_names=secret_names,
        resume_cancelled_writer=True)
    verifier = getattr(engine, 'codex_verifier', None) or verify_codex_delivery
    from .communication_graph import dispatch

    def finish_delivery(attempt, votes):
        acceptance, index = attempt['acceptance'], attempt['index']
        if acceptance.get('approved') is not True:
            _fail('实际本地验收尚未通过，复审或意见澄清不能替代失败检查。',
                kind='codex_repair_unverified', details={'workflow_id': key, 'attempt': index})
        code_sha = attempt['code_sha256']
        name = (f'cycle-{cycle}/implementation/{kind}-v{state["output_version"]}-r{index + 1}.py'
                if state.get('output_version') else
                f'cycle-{cycle}/codex-implementation-{_digest(key)[:12]}-{index}.py')
        path = engine.run_dir(run_id) / name
        io_path(path.parent).mkdir(parents=True, exist_ok=True)
        io_path(path).write_bytes(acceptance['code'].encode('utf-8'))
        artifact = {'id': f'{run_id}:{name}', 'run_id': run_id, 'name': name, 'path': str(path), 'kind': 'code',
                    'created_at': now(), 'sha256': code_sha}
        engine.store.put('artifacts', artifact['id'], artifact)
        from . import code_model_migration as worker_models
        try:
            worker_profile = worker_models.profile(engine, run_id, state, attempt)
        except ValueError as exc:
            _fail('代码 worker 交付模型绑定无效：' + str(exc))
        result = {'code': acceptance['code'], 'explanation': acceptance['explanation'], 'dependencies': acceptance['dependencies'],
            'artifact_id': artifact['id'], 'reviews': votes, 'implementation_backend': 'local_codex', 'codex_protocol': 1,
            'spec_hash': spec_hash, 'code_sha256': code_sha, 'delivery_hash': acceptance['delivery_hash'],
            'delivery_id': key, 'codex_workspace': attempt.get('writer_workspace',state['workspace']), 'codex_thread_id': state.get('thread_id'),
            'codex_project_dir': state['project_root'], 'codex_model': worker_profile['model'],
            'codex_reasoning_effort': worker_profile['effort']}
        if worker_models.FIELD in worker_profile:
            result[worker_models.FIELD] = copy.deepcopy(worker_profile[worker_models.FIELD])
        result['implementation_review_strategy'] = cycle_flow.STRATEGY
        if attempt.get('validation_repair'):
            result['reviewed_execution_phases']=['validation']
        documents.verify_bundle(state['input_bundle'])
        result.update(implementation_document_id=specification['document']['document_id'],
                      implementation_document_sha256=specification['document']['sha256'])
        if state.get('hyperparameter_yaml_protocol') == 1:
            from .yaml_migration import inspect_contract
            result['hyperparameter_contract'] = inspect_contract(acceptance['runtime_bundle'],
                **({'expected_scope': state['yaml_configuration_scope']} if state.get('yaml_configuration_scope') else {}))
            result['hyperparameter_yaml_protocol'] = 1
        if state['common'].get('dataset_catalog'):
            result['dataset_catalog'] = copy.deepcopy(state['common']['dataset_catalog'])
        if acceptance.get('code_conformance_only'):
            result.update(code_acceptance_scope=acceptance['scope'],
                          resource_admission=copy.deepcopy(acceptance['resource_admission']))
        attempt['status'] = 'approved'
        save(status='completed', result=result, reason=None)
        return {'phase': 'done', 'result': result}

    async def delivery_handoff(cursor):
        index = cursor['payload']['index']
        if maximum is not None and index >= maximum:
            save(status='blocked', reason='Codex产物在有界修订内未通过本地检查与独立审核')
            _fail(state['reason'], kind='codex_delivery_rejected', details={'workflow_id': key, 'attempts': state['attempts']})
        attempt = next((row for row in state['attempts'] if row['index'] == index), None)
        if attempt and attempt.get('status') in {'rejected', 'superseded'}:
            engine.check_active(run_id)
            return {'phase': 'review', 'payload': {'index': index + 1}}
        await _read_execution_gate(engine, run_id)
        if attempt is None:
            from .code_bundle import INSTRUCTION as layout_instruction
            from . import implementation_fixture as fixture
            writer_dir = state.get('writer_workspace',state['workspace'])
            path = Path(state['input_bundle']['manifest']).parent / state['input_bundle']['documents'][0]['file']
            instruction = f'按照 "{path}" 实现代码。按需查阅同目录已批准的架构和计划。\n'+layout_instruction
            if state.get('writer_workspace'):
                instruction += '\n本轮可写目录为 '+writer_dir+'；原文档和源工作区只读。'
            from .validation_repair import invocation
            failed_invocation = invocation(engine,run_id,repair)
            if failed_invocation:
                instruction += '\n原失败试次的实际 CLI 与输入身份（后续任务的配置路径由各自参数传入）：'+json.dumps(failed_invocation,ensure_ascii=False)
            if repair and repair.get('operator_instruction') and run['stage']=='experiments':
                from .validation_repair import review_inputs
                validation_inputs=await owned_to_thread(review_inputs,engine,run_id,state)
                inputs_path=path.parent/('validation-inputs-'+_digest(validation_inputs)[:16]+'.json')
                documents._write_immutable(inputs_path,json.dumps(validation_inputs,ensure_ascii=False).encode('utf-8'))
                instruction+=('\n当前是 validation 运行修订。失败试次的真实输入和同计划的验证参数见 '+str(inputs_path)+
                    '。使用这些输入核对配置读取与准入。未授权的 test 继续拒绝；后续阶段的权限和效果单独记录。')
            fixture_grant=fixture.current(engine,state,specification)
            if fixture_grant:
                instruction+='\n已获准的实现阶段核验：'+json.dumps(fixture_grant,ensure_ascii=False)+'\n'+fixture.GUIDE
            from .implementation_check_evidence import GUIDE as development_check_guide
            instruction+='\n'+development_check_guide
            interpreter = run['config'].get('python_executable')
            if interpreter:
                instruction += (f'\n宿主配置的实验解释器为 "{interpreter}"。'
                    '已批准的开发 CPU 检查优先使用该解释器；如本工作区无法访问，记录实际错误。'
                    '正式实验由 Harness 调度。')
            authored = cycle_flow.author_materials(engine,run_id,specification)
            row_contract = authored.get('validation_row_identity')
            if row_contract:
                instruction += '\n当前宿主暂存字段与可信对照来源：'+json.dumps(
                    row_contract['interface'],ensure_ascii=False)
            if specification.get('host_explanation'):
                instruction += '\n同版文档审核所用的宿主源码说明：'+json.dumps(
                    specification['host_explanation'],ensure_ascii=False)
            elif 'row_identity_host_sources' in authored:
                instruction += '\n宿主源码位置与摘录：'+json.dumps(authored['row_identity_host_sources'],ensure_ascii=False)
            development_exports=[]
            if state['attempts']:
                prior_attempt = state['attempts'][-1]
                if any('unavailable' in item for item in prior_attempt.get('development_check_evidence',[])):
                    from .implementation_check_evidence import export_sources,export_instruction
                    _checked_workspace(state['project_root'],run_id,writer_dir)
                    development_exports=export_sources(prior_attempt)
                    instruction+=export_instruction(writer_dir,development_exports)
                instruction+='\n当前任务已有实现的宿主登记信息：'+json.dumps(
                    cycle_flow.implementation_history(engine,run_id,state,specification),ensure_ascii=False)
                instruction += '\n按上一版实际退回意见修订，保留已经通过的部分。\n' + json.dumps(
                    cycle_flow.prior_votes(engine,run_id,prior_attempt) if prior_attempt.get('review_votes')
                    else prior_attempt.get('issues') or prior_attempt.get('checkpoint',{}).get('error_code'),
                    ensure_ascii=False)
                if not prior_attempt.get('review_votes'):
                    reviewed=next((a for a in reversed(state['attempts']) if a.get('review_votes')),None)
                    if reviewed:
                        instruction+='\n最近一次已完成的代码审核意见：'+json.dumps(
                            cycle_flow.prior_votes(engine,run_id,reviewed),ensure_ascii=False)
                if fixture_grant and prior_attempt.get('review_votes'):
                    admission_votes=prior_attempt.get('fixture_admission_votes') or prior_attempt['review_votes']
                    fixture_findings=[{'role':v['role'],**v['result']['fixture_check'],'findings':v['result']['findings']}
                                      for v in admission_votes
                                      if 'fixture_check' in v.get('result',{})]
                    if fixture_findings: instruction+='\n夹具独立意见：'+json.dumps(fixture_findings,ensure_ascii=False)
                if fixture_grant and prior_attempt.get('fixture_receipt_sha256'):
                    failure=fixture.failure_context(engine,run_id,prior_attempt)
                    if failure: instruction+='\n上轮宿主实际执行的首个失败：'+json.dumps(failure,ensure_ascii=False)
            if state.get('hyperparameter_yaml_protocol') == 1:
                instruction += '\n' + yaml_contract
            from .code_bundle import DIRECTORY_LAYOUT_VERSION
            from . import delivery_manifest_contract as manifest_contract
            delivery_schema = manifest_contract.schema(document_sha256=specification['document']['sha256'])
            instruction = writer_instruction(instruction, specification['document']['sha256'])
            attempt = {'index': index, 'operation_id': uid(), 'status': 'implementing', 'instruction': instruction,
                       'development_check_protocol':1,
                       'development_export_sources':development_exports,
                       'directory_layout_version': DIRECTORY_LAYOUT_VERSION,
                       'delivery_manifest_schema': delivery_schema,
                       'instruction_sha256': hashlib.sha256(instruction.encode()).hexdigest(), 'created_at': now()}
            if failed_invocation:
                attempt['repair_invocation']=failed_invocation
            if state.get('writer_workspace'):
                attempt['writer_workspace']=state['writer_workspace']
            if fixture_grant:
                attempt['fixture_grant_sha256']=fixture_grant['sha256']
            from .code_model_migration import bind_attempt
            try:
                bind_attempt(engine, run_id, state, attempt)
            except ValueError as exc:
                _fail('写码前无法绑定模型迁移回执：' + str(exc))
            state['attempts'].append(attempt)
            save()

        if attempt.get('delivery_manifest_schema'):
            from . import delivery_manifest_contract as manifest_contract
            expected = manifest_contract.schema(document_sha256=specification['document']['sha256'])
            if (attempt['delivery_manifest_schema'] != expected or
                    manifest_contract.instruction(specification['document']['sha256']) not in attempt['instruction']):
                _fail('冻结交付结构合同、写码指令与批准实现文档不一致')

        if (attempt.get('checkpoint') or {}).get('error_code')=='codex_thread_writer_busy':
            from .codex_workspace import isolate_busy_writer
            isolated=await owned_to_thread(isolate_busy_writer,engine,run_id,state,attempt)
            if isolated:
                save(writer_workspace=isolated,thread_id=None,turn_id=None)

        def progress(receipt):
            attempt['checkpoint'] = receipt
            if receipt.get('status') in {'failed', 'interrupted'}:
                attempt.setdefault('confirmed_terminal_at', now())
            save(status='implementing', current_revision=index, blocked_kind=None, thread_id=receipt.get('thread_id') or state.get('thread_id'),
                 turn_id=receipt.get('turn_id'))

        def host_progress(value):
            previous = attempt.get('execution_progress', {})
            stamp = now()
            # Keep only display metadata; never duplicate runner output or code.
            checks = [{key: row.get(key) for key in ('id', 'status', 'passed', 'duration_seconds', 'issues')}
                      for row in value.get('checks', previous.get('checks', []))]
            attempt['execution_progress'] = {**value, 'checks': checks, 'updated_at': stamp,
                'started_at': previous.get('started_at', stamp) if previous.get('step') == value['step'] else stamp}
            save()

        if not attempt.get('snapshot'):
            save(status='implementing', current_revision=index)
            workspace=_checked_workspace(state['project_root'], run_id, attempt.get('writer_workspace',state['workspace']))
            documents.verify_bundle(state['input_bundle'])
            try:
                from .code_model_migration import profile as worker_model_profile
                try:
                    worker_profile = worker_model_profile(engine, run_id, state, attempt)
                except ValueError as exc:
                    _fail('代码 worker 冻结模型校验失败：' + str(exc))
                receipt = await client.run(run_id, workspace, attempt['instruction'],
                    operation_id=attempt['operation_id'], thread_id=state.get('thread_id'), checkpoint=attempt.get('checkpoint'),
                    project_root=state['project_root'], model=worker_profile['model'] or None, effort=worker_profile['effort'] or None,
                    reasoning_only=False,
                    on_progress=progress, check_active=lambda: engine.check_active(run_id))
            except LocalCodexError as exc:
                save(status='blocked', reason='本机Codex门禁：' + str(exc))
                _fail(state['reason'])
            progress(receipt)
            try:
                worker_model_profile(engine, run_id, state, attempt)
            except ValueError as exc:
                _fail('代码 worker 原生回执与冻结模型不一致：' + str(exc))
            if receipt.get('status') != 'completed':
                if receipt.get('status') in {'failed', 'interrupted'} and maximum is not None and index + 1 >= maximum:
                    reason = (f'代码交付次数已用完（{index + 1}/{maximum}）；原Codex回合已确认终止。'
                              '请在执行配置中提高“代码交付次数上限”后恢复；原有回执和用量保留。')
                    save(status='blocked', blocked_kind='codex_delivery_limit', reason=reason)
                    _fail(reason)
                if _terminal_resume(engine, run_id, state, attempt):
                    save(status='implementing', blocked_kind=None, reason=None)
                    return {'phase': 'review', 'payload': {'index': index + 1}}
                reason = ('本机Codex回合已确认终止；请显式恢复以授权一次续接'
                          if receipt.get('status') in {'failed', 'interrupted'} else
                          '本机Codex尚无确定完成回执；恢复只读取原turn，不重新派发')
                if LocalCodexClient.unsubmitted_code_resume(receipt,cancelled_writer=True):
                    reason = '代码请求在恢复原线程时受阻，本轮尚未派发。恢复后将核对原线程并继续。'
                if receipt.get('first_failure', {}).get('error_code') == 'codex_operation_timeout':
                    reason = f"本机Codex达到单轮代码处理时限（{run['config'].get('codex_timeout_seconds', 1800)}秒）；" + reason
                save(status='blocked', reason=reason)
                _fail(state['reason'])
            host_progress({'step': 'snapshot', 'label': '保存交付快照并逐个校验文件'})
            snapshot, issues = await owned_to_thread(_snapshot, engine, run_id, workspace, key, index, state['project_root'])
            if snapshot is None:
                attempt.update(status='rejected', issues=issues)
                save()
                return {'phase': 'review', 'payload': {'index': index + 1}}
            attempt['snapshot'] = snapshot
            save()
        snapshot = attempt['snapshot']
        _verify_snapshot(engine, run_id, snapshot)
        actual = Path(snapshot['workspace']) / 'experiment.py'
        code_sha = hashlib.sha256(io_path(actual).read_bytes()).hexdigest()
        identical_snapshot = any(row.get('snapshot', {}).get('files_hash') == snapshot['files_hash']
            for row in state['attempts'] if row['index'] < index)
        attempt['code_sha256'] = code_sha
        if attempt.get('acceptance', {}).get('failure_kind') == 'codex_protocol_conflict':
            from .comparison_transport import runtime_transport
            current_transport = await asyncio.to_thread(runtime_transport, engine, run_id, spec=spec)
            if (current_transport or {}).get('protocol_repair') != attempt['acceptance'].get('protocol_repair'):
                attempt.setdefault('acceptance_history', []).append(attempt.pop('acceptance'))
                save()
        if 'acceptance' not in attempt:
            if attempt.get('directory_layout_version'):
                from .code_bundle import validate_directory_layout
                try:
                    await asyncio.to_thread(validate_directory_layout, Path(snapshot['workspace']))
                    engine.check_active(run_id)
                except (ValueError, OSError, SyntaxError) as exc:
                    attempt.update(status='rejected', issues=['代码目录架构未满足派发约束：' + str(exc)])
                    host_progress({'step': 'rejected', 'label': '目录架构检查未通过，交回 Codex 修复'})
                    save()
                    return {'phase': 'review', 'payload': {'index': index + 1}}
            if attempt.get('delivery_manifest_schema'):
                from .delivery_manifest_contract import validate as validate_manifest
                try:
                    validate_manifest(json.loads(io_path(Path(snapshot['workspace']) / 'implementation.json').read_bytes()),
                                      expected=attempt['delivery_manifest_schema'])
                except (ValueError, OSError) as exc:
                    attempt.update(status='rejected', issues=['交付结构合同未满足派发约束：' + str(exc)])
                    save()
                    return {'phase': 'review', 'payload': {'index': index + 1}}
            save(status='acceptance')
            attempt['acceptance'] = await verifier(engine, run_id, Path(snapshot['workspace']), spec,
                cancel=engine.cancellations[run_id],
                **({'on_progress': host_progress, 'yaml_protocol': state.get('hyperparameter_yaml_protocol', 0),
                    'workflow_id': key}
                   if verifier is verify_codex_delivery else {}))
            manifest = json.loads(io_path(Path(snapshot['workspace']) / 'implementation.json').read_text(encoding='utf-8'))
            if manifest.get('implementation_document_sha256') != specification['document']['sha256']:
                attempt['acceptance'].setdefault('issues', []).append('实际交付没有绑定批准的实现 Markdown SHA256')
                attempt['acceptance']['approved'] = False
            if state.get('hyperparameter_yaml_protocol') == 1:
                from .yaml_migration import inspect_contract
                try:
                    inspect_contract(attempt['acceptance'].get('runtime_bundle') or {'files':[]},
                        **({'expected_scope': state['yaml_configuration_scope']} if state.get('yaml_configuration_scope') else {}))
                except (ValueError,KeyError,TypeError,OSError) as exc:
                    attempt['acceptance'].setdefault('issues',[]).append('YAML 超参数交付合同未通过：'+str(exc)[:1000])
                    attempt['acceptance']['approved']=False
            save()
        acceptance = attempt['acceptance']
        if (acceptance.get('code_sha256') not in {None, code_sha}
                or acceptance.get('approved') and acceptance.get('code_sha256') != code_sha
                or hashlib.sha256(io_path(actual).read_bytes()).hexdigest() != code_sha):
            _fail('本地验收前后代码哈希不一致，拒绝评审')
        attempt.update(delivery_hash=acceptance.get('delivery_hash'), files=acceptance.get('files'))
        if (not acceptance.get('approved')):
            previous_same = any(row['index'] < index and row.get('acceptance', {}).get('runtime_sha256')
                == acceptance.get('runtime_sha256') and acceptance.get('runtime_sha256')
                and row.get('acceptance', {}).get('validation_policy_sha256') == acceptance.get('validation_policy_sha256')
                and row.get('acceptance', {}).get('issues') == acceptance.get('issues') for row in state['attempts'])
            blocked_kind = acceptance.get('failure_kind') or ('codex_code_no_progress' if previous_same or identical_snapshot else None)
            if blocked_kind:
                attempt.update(status='blocked', issues=acceptance.get('issues'))
                save(status='blocked', reason='；'.join(acceptance.get('issues') or ['同一运行代码和验收规则未取得进展']))
                _fail(state['reason'], kind=blocked_kind, details={'workflow_id': key, 'attempt': index})
            attempt.update(status='rejected', issues=acceptance.get('issues') or ['固定本地验收未通过'])
            host_progress({'step': 'rejected', 'label': '本地验收未通过，准备将具体问题交回修复'})
            save()
            return {'phase': 'review', 'payload': {'index': index + 1}}
        if hashlib.sha256(acceptance['code'].encode('utf-8')).hexdigest() != code_sha:
            _fail('验收返回代码与磁盘实际字节不一致')
        artifacts = _snapshot_artifacts(engine, run_id, snapshot)
        if 'delivery_artifacts' not in attempt:
            # The review input is frozen when the actual delivery is registered.
            attempt['review_material_version'] = 3
            if repair and repair.get('operator_instruction') and run['stage']=='experiments':
                from .validation_repair import review_inputs
                attempt['validation_repair']=await owned_to_thread(review_inputs,engine,run_id,state,attempt)
            if repair and repair.get('operator_instruction') and not any(a.get('review_votes') for a in state['attempts']):
                attempt['reviewed_parent_delivery'] = repair['program']['delivery_hash']
            attempt['implementation_history']=cycle_flow.implementation_history(engine,run_id,state,specification)
            if attempt.get('development_check_protocol')==1:
                from .implementation_check_evidence import capture
                attempt['development_check_evidence']=capture(engine,run_id,attempt,specification['document']['sha256'])
        attempt['delivery_artifacts'] = artifacts
        if attempt.get('fixture_grant_sha256'):
            from . import implementation_fixture as fixture
            if 'fixture_runtime' not in attempt:
                attempt['fixture_runtime']=fixture.runtime_binding()
                attempt['fixture_host_interface']=fixture.HOST_INTERFACE
                attempt['fixture_python_executable']=run['config'].get('python_executable')
            try:
                packet=fixture.declaration(engine,run_id,state,specification,attempt)
                if packet is None: raise ValueError('缺少已许可的夹具清单 '+fixture.MANIFEST)
                if (attempt.get('development_check_protocol')==1 and 'fixture_reuse' not in attempt
                        and not attempt.get('fixture_admission_votes')):
                    reused=fixture.matching_receipt(engine,run_id,state,specification,attempt,packet)
                    attempt['fixture_reuse']=reused[0] if reused else None
            except (ValueError,KeyError,TypeError) as exc:
                attempt.update(status='rejected',issues=['实现夹具声明无法执行：'+str(exc)])
                save(status='implementing',current_revision=index+1)
                return {'phase':'review','payload':{'index':index+1}}
        save(status='code_review',current_revision=index)
        if attempt.get('fixture_receipt_sha256') and not attempt.get('fixture_failure_context'):
            evidence=cycle_flow.pending_fixture_evidence(engine,run_id,state,specification,attempt,h.intelligence)
            if evidence:
                attempt['fixture_failure_context']=evidence
                save()
        votes = await cycle_flow.code_review(engine,h,run_id,state,specification,attempt)
        if (attempt.get('fixture_grant_sha256') and not attempt.get('fixture_receipt_sha256') and not attempt.get('fixture_reuse')
                and all(v['result']['fixture_check']['approve'] for v in votes)):
            from .implementation_fixture_runner import execute
            attempt['fixture_admission_votes']=votes
            save()
            receipt=await execute(engine,run_id,state,attempt,packet)
            attempt['fixture_receipt_sha256']=receipt['sha256']
            if not receipt['passed']:
                attempt['fixture_failure_context']=fixture.failure_context(engine,run_id,attempt)
            save()
            votes=await cycle_flow.code_review(engine,h,run_id,state,specification,attempt)
        attempt['review_votes'] = votes
        if all(v['result']['approve'] for v in votes):
            return finish_delivery(attempt,votes)
        attempt.update(status='rejected', issues=[f['location']+' '+f['change']
            for v in votes for f in v['result']['findings']])
        save(status='implementing',current_revision=index+1)
        if identical_snapshot:
            _fail('交付内容与上一版相同，复审仍有未解决的问题。',kind='codex_code_no_progress',
                  details={'workflow_id':key,'attempt':index})
        return {'phase':'review','payload':{'index':index+1}}

    start = next((a['index'] for a in state['attempts'] if a['status'] not in {'rejected','superseded'}),
                 max((a['index'] for a in state['attempts']),default=-1)+1)
    return await dispatch(delivery_handoff, {'index': start},
                          transition_limit=None if maximum is None else maximum + 2)
