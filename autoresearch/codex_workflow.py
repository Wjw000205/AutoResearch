"""Reviewed specifications and real local Codex deliveries, with durable replay.

Provider roles retain their configured identities. Local Codex is an executor;
its completion text is never accepted as a file, test, or scientific result.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path

from .storage import now, uid


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

SPEC_TASK = ('用中文给本机Codex制定可直接执行的具体实现规范，不直接代写代码。严格保留已批准提案、设计和冻结参数计划，'
    '解释算法步骤、张量/数据处理、baseline/proposed/ablation三个真实分支、固定划分与模型seed区别、全部验收项及对应实现位置。'
    '产物必须是按职责拆分的自包含项目目录，experiment.py 仅作入口，implementation.json 声明 code_layout=modular、runtime_files、explanation、dependencies、requirements:'
    '[{id,locations:string[],verification}]，逐项对应acceptance_checks。另交tests；只能依赖清单内的项目模块和已声明第三方依赖。'
    '禁止stub、常量指标、模拟科研结果、运行时安装依赖、用测试集选参。不能弱化方法或验收门槛来通过本地测试。')
REVIEW_TASK = ('独立审查同一实现规范是否忠实实现已批准科学方案和所有验收项，检查算法可执行性、资源、数据真实性、'
    '公平基线/消融、数据泄漏和完整目录的入口运行契约。只审批下一步代码实现，不要求新科研结果。用中文具体给出issues和fixes；'
    '每个issue必须至少一个对应issue_index的修复建议与验证方法。approve=true必须issues/fixes均空。'
    '精确回传proposal_hash/spec_hash，不能为推进流程勉强通过。')
DELIVERY_TASK = ('独立复审本机Codex实际磁盘产物及固定本地验收，逐项核对已批准规范、三个算法分支、参数使用、'
    '数据真实性/划分/防泄漏、资源和实验契约。completion文字不算证据，smoke仅检查可运行性，不是科学成功。'
    '精确回传proposal_hash/spec_hash/code_sha256/delivery_hash。中文issues/fixes必须具体可执行，每个issue有对应修复；'
    'approve=true必须issues/fixes均空。不得要求用held-out表现调参或弱化基线。')

# Persisted workflow inputs remain frozen; new dispatches also receive the
# current directory-layout contract without rewriting an approved SPEC.
# A plan describes intended locations; only the later delivered manifest can
# claim actual file locations and describe completed verification.
MANIFEST_SCHEMA = {'type': 'object', 'properties': {
    'explanation': TEXT, 'dependencies': STRINGS,
    'requirements': {'type': 'array', 'minItems': 1, 'items': {
        'type': 'object', 'properties': {'id': TEXT,
            'locations': {'type': 'array', 'minItems': 1, 'items': TEXT}, 'verification': TEXT},
        'required': ['id', 'locations', 'verification'], 'additionalProperties': True}}},
    'required': ['explanation', 'dependencies', 'requirements'], 'additionalProperties': True}
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
        '将该ID的计划 location 汇总并去重；交付时必须落实为真实冻结文件的相对位置数组 locations，例如 experiment.py:main。计划描述本身不是实际文件存在的证据。',
        'verification 以同ID acceptance_checks.verification 为验证要求，交付时描述实际检查和未完成部分；behavior 只描述实现职责，不能冒充验证方法或通过结果。',
        '计划阶段保留 implementation_map 的必需字段，不要求把 requirement_id/location/behavior 改成 manifest 字段；应审查转换说明、ID覆盖和计划合理性。',
        '交付阶段独立检查 requirements 完整覆盖所有验收项、实际文件位置、验证说明及固定本地验收；任何真实缺项、不存在的文件、未通过检查或否决都仍阻止接受。'],
    'illustration_only_not_artifact_evidence': {
        'acceptance_check': {'id': 'AC-example', 'requirement': '按固定参数计算真实分支', 'verification': '固定输入运行并核对分支与输出'},
        'planning_rows': [{'requirement_id': 'AC-example', 'location': 'experiment.py 的 main 分支', 'behavior': '选择并计算对应模型'}],
        'delivery_row_shape': {'id': 'AC-example', 'locations': ['experiment.py:main'],
            'verification': '记录实际执行的检查及结果；未执行时如实说明，不能复制这个示例作为通过证据。'}}}
FORMAT_GUIDANCE = ('\n两种格式分属不同阶段，必须遵守 context.implementation_format_contract：'
    'SPEC的implementation_map是计划映射，字段固定requirement_id/location/behavior；'
    '最终磁盘implementation.json的requirements才使用id/locations字符串数组/verification。'
    '实现规范必须明确按同一acceptance_checks完整ID分组，将计划位置落实为实际文件位置，并从该验收项的verification生成真实验证说明。'
    '不得要求把SPEC必需字段改成manifest字段，也不得把计划描述当作已存在文件或已通过测试。'
    '这不是格式豁免：两层都必须分别符合自己的schema，覆盖同一验收项，交付阶段继续核验真实文件与检查。')
SPEC_TASK_V2 = SPEC_TASK + FORMAT_GUIDANCE + (
    '\n先读取 implementation_capability_document 并核对 implementation_capability_evidence。'
    '若算法需要跨试次比较输入，必须在 SPEC.comparison_transport 中明确声明受支持的结构合同，'
    '按批准任务填写 payload_layout 的样本标识字段、分组字段、目标数组名及样本轴；'
    'payload_arrays 的名称、类型、形状与单位取自实际任务输出，诊断数组同样须明确声明。'
    '包括冻结计划中的消费者和完整数组协议；只有该结构字段通过同版本三方审计后才会启用。'
    '若批准来源明确区分验证与最终测试的验收范围，在 comparison_transport.acceptance_phases 中逐项列出'
    '全部冻结 metric 及适用 phases；每项保留 validation，主指标保留 test，测试范围必须逐项有批准来源。'
    '该字段不改变冻结阈值、方向、比较符或实验调用；不能根据名称猜测适用性，不能拿验证值代填测试值。'
    '旧文档未声明时仍沿用全部判据适用于两个阶段的合同；需要不同范围时提交显式修订并重新独立审查。'
    '不得靠自然语言提及接口来假定已启用，不得改变冻结调用数量、参数或把宿主能力当科研验证。'
    '宿主能力尚未满足时记录具体依赖；来源尚未读完时继续同版本核对，不重写正确条款。')
REVIEW_TASK_V2 = REVIEW_TASK + FORMAT_GUIDANCE
DELIVERY_TASK_V2 = DELIVERY_TASK + FORMAT_GUIDANCE


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


async def _reviews(engine, h, run_id, purpose, context, *, delivery=False, roles=ROLES):
    from .intelligence import parse_json
    from . import implementation_alignment as alignment
    if not delivery:
        _fail('实现规范只能经索引全文三方审核，旧 provider 评议已经删除。')
    schema, instruction = DELIVERY_REVIEW, DELIVERY_TASK
    if context.get('format_contract_version') == 2:
        instruction = DELIVERY_TASK_V2
    if context.get('execution_failure_review'):
        from .code_failure_review import GUIDANCE
        instruction += GUIDANCE
    if alignment.enabled(context):
        schema = alignment.code_review_schema(schema)
        instruction += alignment.CODE_GUIDANCE
    from . import definition_code_followup as definition_followup
    if context.get(definition_followup.FIELD):
        schema = definition_followup.review_schema(schema)
        instruction = (definition_followup.TASK
            + (alignment.CODE_GUIDANCE if alignment.enabled(context) else '')
            + (FORMAT_GUIDANCE if context.get('format_contract_version') == 2 else '')
            + '\n' + definition_followup.GUIDANCE)
    if context.get('architecture_convergence_protocol') == 1:
        from .design_review_policy import SCALE
        instruction += SCALE
    from . import code_repair_contract as repair
    if context.get(repair.FIELD):
        schema = repair.review_schema(schema)
        from . import code_repair_clarification as clarification
        if context.get(clarification.FIELD):
            schema = clarification.schema(schema)
            instruction += clarification.TASK
        else:
            instruction += repair.REVIEW_GUIDANCE
    if context.get('host_verification'):
        from .code_followup import GUIDANCE
        instruction += GUIDANCE
    if context.get('definition_execution_acceptance'):
        from .definition_execution_followup import ACCEPTANCE_GUIDANCE, validate_acceptance_scope
        validate_acceptance_scope(context['definition_execution_acceptance'],context)
        instruction += ACCEPTANCE_GUIDANCE
    if not roles or len(set(roles)) != len(roles) or set(roles) - set(ROLES):
        _fail('代码复审角色无效。')
    contexts = {role: context for role in roles}
    instructions, schemas = {role: instruction for role in roles}, {role: schema for role in roles}
    if delivery:
        for role in roles:
            frozen = _frozen_spec_assignment(engine, run_id, purpose, role)
            if frozen:
                if any(frozen['context'].get(field) != context[field] for field in
                       ('proposal_hash', 'spec_hash', *(['code_sha256', 'delivery_hash'] if delivery else []))):
                    _fail('实现规范原始评议输入绑定了不同版本。')
                instructions[role], contexts[role], schemas[role] = frozen['instruction'], frozen['context'], frozen['schema']
    if hasattr(h.intelligence, 'preflight_batch'):
        pending = []
        for role in roles:
            # Code evidence projection authenticates this exact assignment.
            # Freeze before preflight as well as before actual model dispatch.
            engine.check_active(run_id)
            h._handoff(run_id, role, purpose, instructions[role], contexts[role], schemas[role])
            await h.complete_pending_tools(run_id, role, purpose, instructions[role], contexts[role], schemas[role])
            key = h.decision_key(run_id, role, purpose, instructions[role], contexts[role], schemas[role])
            if not engine.store.get('decisions', key):
                pending.append(await h.next_request_async(run_id, role, purpose, instructions[role], contexts[role], schemas[role]))
        if pending:
            await engine._read_consensus_check(run_id, h.intelligence.preflight_batch, run_id, pending)
    tasks = [asyncio.create_task(h.ask(run_id, role, purpose, instructions[role], contexts[role], schemas[role])) for role in roles]
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    for result in results:
        if isinstance(result, BaseException):
            raise result
    reviews = []
    for role, result in zip(roles, results):
        parse_json(json.dumps(result, ensure_ascii=False), schemas[role])
        for field in ('proposal_hash', 'spec_hash', *(['code_sha256', 'delivery_hash'] if delivery else [])):
            if result[field] != context[field]:
                _fail(f'{role}评审未绑定同一{field}，未派发后续执行')
        issues, fixes = result['issues'], result['fixes']
        if result['approve'] and (issues or fixes):
            _fail(f'{role}批准票仍含未解决问题')
        from .code_repair_clarification import withdrawn_role
        withdrawn = withdrawn_role(result, contexts[role], role)
        if not result['approve'] and not withdrawn and (not issues or {fix['issue_index'] for fix in fixes} != set(range(len(issues)))):
            _fail(f'{role}否决必须对每个问题给出具体修复和验证方法')
        assignment_context = contexts[role]
        hid = h.decision_key(run_id, role, purpose, instructions[role], assignment_context, schemas[role])
        if alignment.enabled(assignment_context):
            from .implementation_alignment_evidence import validate_vote
            await engine._read_consensus_check(run_id, validate_vote,
                engine, run_id, role, hid, result, assignment_context, delivery=delivery)
            engine.check_active(run_id)
        repair.validate_review(result, assignment_context, role)
        definition_followup.validate_review(result, assignment_context)
        reviews.append({'role': role, **result,
                        **({'handoff_id': hid} if alignment.enabled(assignment_context) else {})})
    return reviews


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
    from .delivery_files import identity, copy_verified
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
    target.mkdir(parents=True, exist_ok=True)
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


def validate_program(engine, run_id, program, *, source_cycle=None, source_only=False):
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
            or record.get('cycle') != (source_cycle if source_only else engine.cycle(run_id))
            or record.get('proposal_hash') != consensus.get('proposal_hash')
            or record.get('proposal_hash') != _digest(record.get('common', {}).get('proposal'))
            or any(accepted.get(field) != program.get(field) for field in
                   ['code_sha256', 'spec_hash', 'delivery_hash', 'code', 'dependencies'])):
        _fail('待执行程序缺少同任务的完整Codex验收记录')
    if any(accepted.get(field) != program.get(field) for field in
           ('hyperparameter_yaml_protocol', 'hyperparameter_contract')):
        _fail('YAML 运行合同与已批准交付不一致')
    specification = next((s for s in record.get('specifications', [])
                          if s.get('approved') and s.get('spec_hash') == program.get('spec_hash')), None)
    _validate_approved_spec(specification, record.get('proposal_hash'))
    from .implementation_documents import validate_review, verify_bundle
    validate_review(engine, run_id, record, specification)
    verify_bundle(record['input_bundle'])
    if program.get('implementation_document_sha256') != specification['document']['sha256']:
        _fail('待执行代码没有绑定批准的实现 Markdown')
    votes = program.get('reviews', [])
    if (len(votes) != 3 or {v.get('role') for v in votes} != set(ROLES)
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
    from .runtime_training_policy import active as active_runtime_policy
    runtime_policy = None if source_only else active_runtime_policy(engine.store, run_id, program)
    if (attempt['acceptance'].get('code_conformance_only')
            and attempt['acceptance'].get('resource_admission',{}).get('approved') is not True
            and runtime_policy is None and not source_only):
        # An evidence hold is not a rejected scientific proposal. In particular,
        # _fail(kind=unknown) would route into ResearchRevisionRequired.
        from .engine import ResearchBlocked
        raise ResearchBlocked('代码符合性已核对；当前版本的资源准入证据尚未齐备，未启动训练。')
    from . import implementation_alignment as alignment
    from .implementation_alignment_evidence import validate_vote
    review_purpose = attempt.get('review_purpose') or f'code_review:{_digest(record["id"])}:{attempt["index"]}'
    if attempt.get('clarification_approval_votes'):
        from .code_repair_clarification import verify_saved
        clarification_record=attempt.get('verification_followup',attempt.get('repair_clarification',{}))
        resolved = verify_saved(engine, run_id, record, attempt, clarification_record)
        if resolved['issues'] or votes != clarification_record['votes']:
            _fail('原交付的意见澄清尚未取得三方无异议复审。')
        review_purpose = clarification_record['purpose']
    for vote in votes:
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
    _verify_snapshot(engine, run_id, attempt['snapshot'])
    from .code_repair_contract import validate_delivery as validate_repair_delivery
    validate_repair_delivery(record, attempt)
    code_path = Path(attempt['snapshot']['workspace']) / 'experiment.py'
    if hashlib.sha256(code_path.read_bytes()).hexdigest() != program['code_sha256']:
        _fail('已批准代码哈希与冻结交付快照不一致')
    from .code_bundle import describe
    manifest = json.loads((code_path.parent / 'implementation.json').read_text(encoding='utf-8'))
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
    if (len(votes) != 3 or {v.get('role') for v in votes} != set(ROLES)
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


def _delivery_feedback(state, *, before=None):
    """Carry real failed-check diagnostics into the next immutable repair input."""
    from .storage import redact
    from .code_repair_contract import previous_attempt, fail, sha
    feedback = []
    # Older rejections remain archived, but are not active edit instructions.
    # Replaying all attempts reopens already-resolved issues and contradictions.
    reviewed = previous_attempt(state, before, reviewed=True)
    if reviewed:
        packet = reviewed.get('repair_contract')
        if not packet:
            fail('否决票尚未通过逐条修改依据核对。')
        feedback.append({'index': reviewed['index'], 'repair_contract_sha256': sha(packet),
                         'repair_contract': copy.deepcopy(packet)})
    latest = previous_attempt(state, before)
    for attempt in ([latest] if latest else []):
        if attempt is reviewed:
            continue
        item = {key: attempt.get(key) for key in ('index', 'issues', 'review_votes')}
        if attempt.get('delivery_correction'):
            item['delivery_correction'] = copy.deepcopy(attempt['delivery_correction'])
        acceptance = attempt.get('acceptance')
        if isinstance(acceptance, dict):
            item['local_acceptance'] = {key: acceptance.get(key) for key in
                ('approved', 'code_sha256', 'delivery_hash', 'issues', 'scope')}
            checks = []
            for record in acceptance.get('smoke', []):
                check = {key: record.get(key) for key in
                    ('id', 'status', 'exit_code', 'error', 'passed', 'issues', 'command', 'artifact_path', 'scope')}
                from .execution_diagnostics import failure_diagnostic
                diagnostic = failure_diagnostic(record)
                if diagnostic:
                    check['failure_diagnostic'] = diagnostic
                # Full logs remain in the persisted run directory. Bound the repair
                # packet without replacing stdout's concrete failure with a summary.
                for key in ('stdout', 'stderr'):
                    value = record.get(key)
                    check[key] = value[:16384] if isinstance(value, str) else value
                    if isinstance(value, str) and len(value) > 16384:
                        check[key + '_truncated'] = True
                params = redact(record.get('params'))
                encoded = json.dumps(params, ensure_ascii=False, sort_keys=True)
                if len(encoded) <= 65536:
                    check['params'] = params
                else:
                    check['params_preview'] = encoded[:65536]
                    check['params_truncated'] = True
                checks.append(check)
            item['local_acceptance']['checks'] = checks
        feedback.append(item)
    return redact(feedback)


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


async def implement(engine, h, run_id, common, *, repair=None):
    """Use real author writes and three independent indexed full-text reviews."""
    from .codex_delivery import verify_codex_delivery
    from .local_codex import LocalCodexClient, LocalCodexError
    from . import implementation_documents as documents
    from . import implementation_indexed_review as indexed_review
    from . import implementation_authoring as author, implementation_alignment as alignment
    from . import implementation_indexed_replies as replies, implementation_issue_responses as responses
    from .implementation_failures import ImplementationSpecificationPending
    from . import implementation_acceptance as acceptance, implementation_acceptance_flow as acceptance_flow
    from .code_followup import enabled as bounded_followup

    await _read_execution_gate(engine, run_id)
    run = engine.store.get('runs', run_id)
    config = run['config']
    if (type(config.get(indexed_review.PROTOCOL)) is not int or config[indexed_review.PROTOCOL] != 1
            or config.get('implementation_document_protocol') != 2
            or config.get('implementation_document_authoring_protocol') != 1
            or config.get('implementation_review_strategy') not in {'whole_alignment', 'acceptance_contract'}):
        _fail('实现阶段需要受支持的验收合同或历史索引审核协议及真实作者保存流程；不能改写冻结输入。')
    cycle = engine.cycle(run_id)
    proposal_hash = _digest(common['proposal'])
    kind = 'runtime_repair' if repair else 'implementation'
    prior_program = (repair or {}).get('program', {})
    settings = {'project_root': prior_program.get('codex_project_dir') or common.get('codex_project_dir') or config.get('codex_project_dir'),
                'model': prior_program.get('codex_model', common.get('codex_model', config.get('codex_model', ''))),
                'effort': prior_program.get('codex_reasoning_effort', common.get('codex_reasoning_effort', config.get('codex_reasoning_effort', '')))}
    identity = {'cycle': cycle, 'kind': kind, 'proposal_hash': proposal_hash, 'design': common['design'],
                'experiment_plan': common.get('experiment_plan'), 'repair': repair, 'codex_settings': settings,
                'document_protocol': 2}
    from .implementation_restart import identity_generation
    implementation_generation = await engine._read_consensus_check(run_id,
        identity_generation, engine, run_id, common)
    if implementation_generation:
        identity['implementation_generation'] = implementation_generation
    key = f'{run_id}:codex:{_digest(identity)}'
    state = engine.store.get('codex_workflows', key)
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
            indexed_review.PROTOCOL: indexed_review.VERSION,
            'implementation_document_authoring_protocol': 1,
            'document_read_window_protocol': 1, 'codex_prompt_protocol': 1,
            'implementation_owner': 'codex', 'architecture_convergence_protocol': 1,
            'status': 'specification', 'workspace': str(workspace), 'project_root': str(project_root.resolve()),
            'proposal_hash': proposal_hash, 'model': settings['model'], 'reasoning_effort': settings['effort'],
            'thread_id': prior.get('codex_thread_id'), 'turn_id': None, 'current_revision': 0,
            'specifications': [], 'attempts': [], 'created_at': now(), 'input_hash': _digest(identity),
            'common': common, 'repair': repair}
        if config.get('hyperparameter_yaml_protocol') == 1:
            state['hyperparameter_yaml_protocol'] = 1
        if config.get('implementation_review_strategy') == 'acceptance_contract':
            state[acceptance.PROTOCOL] = acceptance.VERSION
            state[acceptance.FIELD] = acceptance.initial_ledger(config)
        if implementation_generation:
            state['implementation_generation'] = implementation_generation
        with engine.store._read_db() as db:
            previous_count, previous_version = db.execute(
                "SELECT count(*), max(CASE WHEN json_type(data,'$.output_version')='integer' "
                "THEN json_extract(data,'$.output_version') ELSE 0 END) FROM records "
                "WHERE collection='codex_workflows' AND json_extract(data,'$.run_id')=?",
                (run_id,)).fetchone()
        state['output_version'] = max(previous_count, previous_version or 0) + 1
    documents.require_indexed_review(engine, run_id, state)

    def save(**fields):
        state.update(fields, updated_at=now())
        from .codex_accounting import capture_usage
        records = [capture_usage(engine.store, run_id, attempt.get('checkpoint')) for attempt in state['attempts']]
        engine.store.batch([('codex_workflows', key, state),
            *[('codex_usage', row['id'], row) for row in records if row]])

    if state.get('status') == 'completed':
        validate_program(engine, run_id, state['result'])
        return state['result']
    # This bounds CODE delivery only. Document acceptance has its own finite
    # budget; frozen historical indexed reviews retain their original contract.
    maximum = config.get('codex_max_revisions', 3)
    code_contract = _code_contract(engine, format_v2=True)
    if state.get('hyperparameter_yaml_protocol') == 1:
        from .yaml_migration import INSTRUCTION as yaml_contract
        code_contract += '\n' + yaml_contract
    common = await documents.planning_context_async(engine, h, run_id, state['common'],
        code_contract, FORMAT_CONTRACT, repair, protocol=2, read_window_protocol=1)
    common['architecture_convergence_protocol'] = 1
    if acceptance.enabled(state):
        save()
        await acceptance_flow.ensure_contract(engine, h, run_id, state, common, save)
        if state[acceptance.FIELD].get('outcome') in acceptance.TERMINAL - acceptance.APPROVED:
            acceptance_flow.stop(state, save)
    from .design_review_policy import AUTHOR
    spec_task = SPEC_TASK_V2 + (
        '\n先按文档索引读取已批准架构、共识和冻结计划。输入不含全文；不要要求全文复制。'
        '首轮使用 implementation.write_document 创建实现文档，后续使用 implementation.edit_document '
        '按精确当前基线定向修改并生成下一版，再由三个角色各自通过工具阅读全文审核。'
        '必须收齐全部三方意见后统一修订并逐项回复；当前文档获三票通过才交给本机 Codex 写真实文件。'
        '不要声称已经完成代码或实验。') + AUTHOR
    if acceptance.enabled(state):
        spec_task = SPEC_TASK_V2 + acceptance.AUTHOR_GUIDANCE
    if state.get('hyperparameter_yaml_protocol') == 1:
        spec_task += '\n必须在 parameter_contract、实现位置及 acceptance_checks 中落实以下 YAML 契约：\n' + yaml_contract
    from . import implementation_definition_repair as definition_repair
    await definition_repair.advance(engine, h, run_id, state, common, save)
    specification = next((row for row in reversed(state['specifications']) if row.get('approved')), None)
    if specification is None:
        async def specification_handoff(index):
            engine.check_active(run_id)
            existing = next((row for row in state['specifications'] if row['index'] == index), None)
            if not existing:
                if index and not acceptance.enabled(state):
                    await engine._read_consensus_check(run_id, replies.validate_previous,
                        engine, run_id, state, index)
                save(status='specification', current_revision=index, blocked_kind=None, reason=None)
                context = {**common, 'code_contract': code_contract,
                    'implementation_document_authoring_protocol': 1,
                    'implementation_document_authoring': {'workflow_id': key, 'revision': index,
                                                          'specification_schema': SPEC}}
                purpose = f'implementation_spec:{_digest(key)}:{index}'
                frozen = _frozen_spec_author(engine, run_id, purpose)
                if frozen:
                    author_task, context = frozen['instruction'], frozen['context']
                    if (type(context.get(indexed_review.PROTOCOL)) is not int
                            or context[indexed_review.PROTOCOL] != indexed_review.VERSION):
                        _fail('作者冻结输入不属于新的索引流程；不能恢复旧派发或改写付费请求。')
                else:
                    context = alignment.attach(context)
                    context['implementation_document_authoring']['specification_schema'] = alignment.author_schema(SPEC)
                    context = (acceptance_flow.author_context(state, index, context) if acceptance.enabled(state)
                               else replies.author_context(state, index, context))
                    from .implementation_phase_context import GUIDANCE as PHASE_GUIDANCE
                    context['objective'] = run['objective']
                    from .implementation_review_routing import AUTHOR_GUIDANCE
                    if index:
                        context[author.EDIT_PROTOCOL] = author.EDIT_VERSION
                    author_task = (spec_task + (author.EDIT_TASK if index else author.TASK)
                        + alignment.AUTHOR_GUIDANCE + AUTHOR_GUIDANCE + replies.AUTHOR_GUIDANCE
                        + responses.AUTHOR_GUIDANCE + PHASE_GUIDANCE)
                    if acceptance.enabled(state):
                        author_task = spec_task + (author.EDIT_TASK if index else author.TASK) + alignment.AUTHOR_GUIDANCE + acceptance.AUTHOR_GUIDANCE
                    from .implementation_author_revision import KEY as revision_key, GUIDANCE as REVISION_GUIDANCE
                    if revision_key in context:
                        author_task += REVISION_GUIDANCE
                    from .implementation_document_readability import new_assignment
                    author_task, context = new_assignment(h, run_id, purpose, author_task, context,
                                                         author.submission_schema(context))
                from .implementation_authoring_completion import ask as ask_document_author
                result, hid = await ask_document_author(engine, h, run_id, state, purpose, author_task, context)
                if acceptance.enabled(state) and index:
                    from .implementation_acceptance_author import finish
                    result, hid = await finish(engine, h, run_id, state, hid, result)
                existing = await author.submit_async(engine, run_id, state, hid, result)
                engine.check_active(run_id)
                _validate_spec(existing['spec'], context, design=state['common']['design'])
                existing[indexed_review.PROTOCOL] = indexed_review.VERSION
                if acceptance.enabled(state):
                    existing[acceptance.PROTOCOL] = acceptance.VERSION
                state['specifications'].append(existing)
                save()
                engine.store.event(run_id, 'implementation.document_indexed',
                    '代码实现文档已真实保存并建立索引；正在按冻结协议审核。',
                    {'artifact_id': existing['document']['artifact_id'], 'sha256': existing['document']['sha256']})
            existing.setdefault('implementation_review_routing_protocol', 1)
            save(status='spec_review', spec_hash=existing['spec_hash'])
            await documents.reviews(engine, h, run_id, state, existing, common, save)
            engine.check_active(run_id)
            save()
            if existing['approved']:
                return existing
            save(status='specification', current_revision=index + 1, blocked_kind=None, reason=None)
            raise ImplementationSpecificationPending()

        incomplete = [row['index'] for row in state['specifications'] if row.get('approved') is None]
        index = min(incomplete) if incomplete else max([row['index'] for row in state['specifications']], default=-1) + 1
        specification = await specification_handoff(index)
    spec, spec_hash = specification['spec'], specification['spec_hash']
    _validate_approved_spec(specification, proposal_hash)
    await engine._read_consensus_check(run_id, documents.validate_review_snapshot,
                                       engine, run_id, state, specification)
    if state.get('spec_hash') != spec_hash:
        # Older definition transitions left this display/current-version pointer
        # on the base SPEC. Restore it only after authenticating the new approval.
        save(spec_hash=spec_hash)
    if not state.get('input_bundle'):
        save(input_bundle=documents.export_bundle(engine, run_id, state, specification))
    documents.verify_bundle(state['input_bundle'])
    secret_names = tuple(p.get('api_key_env') for p in getattr(h.intelligence, 'settings', {}).get('providers', []) if p.get('api_key_env'))
    client = getattr(engine, 'codex_client', None) or LocalCodexClient(
        timeout=run['config'].get('codex_timeout_seconds', 1800), blocked_env_names=secret_names)
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
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(acceptance['code'].encode('utf-8'))
        artifact = {'id': f'{run_id}:{name}', 'run_id': run_id, 'name': name, 'path': str(path), 'kind': 'code',
                    'created_at': now(), 'sha256': code_sha}
        engine.store.put('artifacts', artifact['id'], artifact)
        result = {'code': acceptance['code'], 'explanation': acceptance['explanation'], 'dependencies': acceptance['dependencies'],
            'artifact_id': artifact['id'], 'reviews': votes, 'implementation_backend': 'local_codex', 'codex_protocol': 1,
            'spec_hash': spec_hash, 'code_sha256': code_sha, 'delivery_hash': acceptance['delivery_hash'],
            'delivery_id': key, 'codex_workspace': state['workspace'], 'codex_thread_id': state.get('thread_id'),
            'codex_project_dir': state['project_root'], 'codex_model': state['model'],
            'codex_reasoning_effort': state['reasoning_effort']}
        documents.verify_bundle(state['input_bundle'])
        result.update(implementation_document_id=specification['document']['document_id'],
                      implementation_document_sha256=specification['document']['sha256'])
        if state.get('hyperparameter_yaml_protocol') == 1:
            from .yaml_migration import inspect_contract
            result['hyperparameter_contract'] = inspect_contract(acceptance['runtime_bundle'])
            result['hyperparameter_yaml_protocol'] = 1
        if acceptance.get('code_conformance_only'):
            result.update(code_acceptance_scope=acceptance['scope'],
                          resource_admission=copy.deepcopy(acceptance['resource_admission']))
        attempt['status'] = 'approved'
        save(status='completed', result=result, reason=None)
        return {'phase': 'done', 'result': result}

    async def delivery_handoff(cursor):
        index = cursor['payload']['index']
        if maximum is not None and index >= maximum:
            save(status='blocked', reason='Codex产物在有界修订内未通过本地验收与三方复审')
            _fail(state['reason'], kind='codex_delivery_rejected', details={'workflow_id': key, 'attempts': state['attempts']})
        attempt = next((row for row in state['attempts'] if row['index'] == index), None)
        if attempt and attempt.get('status') in {'rejected', 'superseded'}:
            engine.check_active(run_id)
            return {'phase': 'review', 'payload': {'index': index + 1}}
        await _read_execution_gate(engine, run_id)
        if attempt is None:
            from . import code_repair_contract as repair
            previous = repair.previous_attempt(state, index, reviewed=True)
            definition_transition = definition_repair.pending_dispatch(state, index)
            if previous and not definition_transition:
                # Even a cached plan is reauthenticated against sealed votes,
                # the approved document and independent review decisions.
                previous['execution_progress'] = {'step': 'repair_review',
                    'label': '核对修改意见与批准实现文档，再生成 Codex 修改指令',
                    'started_at': now(), 'updated_at': now()}
                save(status='code_review')
                packet = await repair.prepare(engine, h, run_id, state, specification, previous, save)
                await _read_execution_gate(engine, run_id)
                if packet is None:
                    previous['clarification_approval_votes'] = previous.get('verification_followup',
                        previous.get('repair_clarification', {}))['votes']
                    return finish_delivery(previous, previous['clarification_approval_votes'])
            feedback = [] if definition_transition else _delivery_feedback(state, before=index)
            instruction = (definition_repair.instruction(state, specification) if definition_transition
                           else documents.codex_instruction(state, specification, feedback))
            if state.get('hyperparameter_yaml_protocol') == 1:
                instruction += '\n' + yaml_contract
            from .code_bundle import DIRECTORY_LAYOUT_VERSION
            attempt = {'index': index, 'operation_id': uid(), 'status': 'implementing', 'instruction': instruction,
                       'directory_layout_version': DIRECTORY_LAYOUT_VERSION,
                       'instruction_sha256': hashlib.sha256(instruction.encode()).hexdigest(), 'created_at': now()}
            if definition_transition:
                attempt['definition_repair_id'] = state[definition_repair.FIELD]['id']
            elif previous and previous.get('repair_contract'):
                attempt['repair_contract_sha256'] = repair.sha(previous['repair_contract'])
            state['attempts'].append(attempt)
            save()

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
            _checked_workspace(state['project_root'], run_id, state['workspace'])
            documents.verify_bundle(state['input_bundle'])
            from .code_repair_contract import validate_dispatch as validate_repair_dispatch
            await engine._read_consensus_check(run_id, validate_repair_dispatch, engine, run_id, state, attempt)
            if not attempt.get('checkpoint'):
                from .code_repair_contract import previous_attempt, unchanged_failed_execution
                previous = previous_attempt(state, index)
                if previous and not bounded_followup(engine, run_id, state):
                    stalled = await engine._read_consensus_check(run_id, unchanged_failed_execution,
                        engine, run_id, state, previous, specification)
                    if stalled:
                        from .code_failure_review import prepare as prepare_failure_review
                        prepared = await engine._read_consensus_check(run_id, prepare_failure_review,
                            engine, run_id, state, specification, attempt, stalled)
                        engine.check_active(run_id)
                        if prepared:
                            attempt.update(**prepared, status='code_review')
                            host_progress({'step': 'code_review', 'label': '按原实现文档复审资源估算缺项，保留原失败记录'})
                            save(status='code_review', current_revision=index, blocked_kind=None, reason=None)
                            return {'phase': 'review', 'payload': {'index': index}}
                        attempt.update(status='blocked', issues=[stalled['message']])
                        host_progress({'step': 'blocked', 'label': stalled['message']})
                        save(status='blocked', blocked_kind='codex_code_no_progress', reason=stalled['message'])
                        _fail(stalled['message'], kind='codex_code_no_progress', details=stalled)
            try:
                receipt = await client.run(run_id, Path(state['workspace']), attempt['instruction'],
                    operation_id=attempt['operation_id'], thread_id=state.get('thread_id'), checkpoint=attempt.get('checkpoint'),
                    project_root=state['project_root'], model=state['model'] or None, effort=state['reasoning_effort'] or None,
                    reasoning_only=False,
                    on_progress=progress, check_active=lambda: engine.check_active(run_id))
            except LocalCodexError as exc:
                save(status='blocked', reason='本机Codex门禁：' + str(exc))
                _fail(state['reason'])
            progress(receipt)
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
                if receipt.get('first_failure', {}).get('error_code') == 'codex_operation_timeout':
                    reason = f"本机Codex达到单轮代码处理时限（{run['config'].get('codex_timeout_seconds', 1800)}秒）；" + reason
                save(status='blocked', reason=reason)
                _fail(state['reason'])
            host_progress({'step': 'snapshot', 'label': '保存交付快照并逐个校验文件'})
            snapshot, issues = await asyncio.to_thread(_snapshot, engine, run_id, state['workspace'], key, index, state['project_root'])
            if snapshot is None:
                attempt.update(status='rejected', issues=issues)
                save()
                return {'phase': 'review', 'payload': {'index': index + 1}}
            attempt['snapshot'] = snapshot
            save()
        snapshot = attempt['snapshot']
        _verify_snapshot(engine, run_id, snapshot)
        from .code_repair_contract import validate_delivery as validate_repair_delivery, reject_delivery
        from .implementation_failures import ImplementationDeliveryCorrection, ImplementationCodeBlocked
        try:
            repair_receipt = await engine._read_consensus_check(run_id, validate_repair_delivery, state, attempt)
        except ImplementationDeliveryCorrection as exc:
            engine.check_active(run_id)
            try:
                reject_delivery(state, attempt, exc)
            except ImplementationCodeBlocked as repeated:
                save(status='blocked', blocked_kind=repeated.failure['kind'], reason=str(repeated))
                raise
            host_progress({'step': 'rejected', 'label': '交付检查发现可修正问题，保留当前快照并交回 Codex'})
            save(status='implementing', blocked_kind=None, reason=None)
            return {'phase': 'review', 'payload': {'index': index + 1}}
        if repair_receipt:
            attempt['repair_receipt'] = repair_receipt
            save()
        failure_review = attempt.get('execution_failure_review')
        if failure_review:
            from .code_failure_review import validate as validate_failure_review
            await engine._read_consensus_check(run_id, validate_failure_review,
                engine, run_id, state, specification, attempt)
        elif (repair_receipt and repair_receipt.get('failed_issue_ids') and not attempt.get('acceptance')
                and not bounded_followup(engine, run_id, state)):
            # A newly finished native turn may still have changed only its
            # report. Diagnose before running identical training/checks again.
            from .code_repair_contract import unchanged_failed_execution
            stalled = await engine._read_consensus_check(run_id, unchanged_failed_execution,
                engine, run_id, state, attempt, specification)
            if stalled:
                from .code_failure_review import prepare as prepare_failure_review
                prepared = await engine._read_consensus_check(run_id, prepare_failure_review,
                    engine, run_id, state, specification, attempt, stalled)
                engine.check_active(run_id)
                if prepared:
                    attempt.update(**prepared, status='code_review')
                    host_progress({'step': 'code_review', 'label': '按原实现文档复审资源估算缺项，未重跑相同检查'})
                    save(status='code_review', current_revision=index, blocked_kind=None, reason=None)
                    return {'phase': 'review', 'payload': {'index': index}}
                attempt.update(status='blocked', issues=[stalled['message']])
                save(status='blocked', blocked_kind='codex_code_no_progress', reason=stalled['message'])
                _fail(stalled['message'], kind='codex_code_no_progress', details=stalled)
        actual = Path(snapshot['workspace']) / 'experiment.py'
        code_sha = hashlib.sha256(actual.read_bytes()).hexdigest()
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
            save(status='acceptance')
            attempt['acceptance'] = await verifier(engine, run_id, Path(snapshot['workspace']), spec,
                cancel=engine.cancellations[run_id],
                **({'on_progress': host_progress, 'yaml_protocol': state.get('hyperparameter_yaml_protocol', 0)}
                   if verifier is verify_codex_delivery else {}))
            manifest = json.loads((Path(snapshot['workspace']) / 'implementation.json').read_text(encoding='utf-8'))
            if manifest.get('implementation_document_sha256') != specification['document']['sha256']:
                attempt['acceptance'].setdefault('issues', []).append('实际交付没有绑定批准的实现 Markdown SHA256')
                attempt['acceptance']['approved'] = False
            if state.get('hyperparameter_yaml_protocol') == 1:
                from .yaml_migration import inspect_contract
                try:
                    inspect_contract(attempt['acceptance'].get('runtime_bundle') or {'files':[]})
                except (ValueError,KeyError,TypeError,OSError) as exc:
                    attempt['acceptance'].setdefault('issues',[]).append('YAML 超参数交付合同未通过：'+str(exc)[:1000])
                    attempt['acceptance']['approved']=False
            save()
        acceptance = attempt['acceptance']
        if (acceptance.get('code_sha256') not in {None, code_sha}
                or acceptance.get('approved') and acceptance.get('code_sha256') != code_sha
                or hashlib.sha256(actual.read_bytes()).hexdigest() != code_sha):
            _fail('本地验收前后代码哈希不一致，拒绝评审')
        attempt.update(delivery_hash=acceptance.get('delivery_hash'), files=acceptance.get('files'))
        if (not acceptance.get('approved') and not failure_review and not acceptance.get('bounded_checks_only')
                and not acceptance.get('definition_checks_deferred')):
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
        code_artifact = next(row['artifact_id'] for row in artifacts if row['path'] == 'experiment.py')
        attempt['delivery_artifacts'] = artifacts
        review_context = {**common, 'proposal_hash': proposal_hash, 'spec_hash': spec_hash,
            'code_sha256': code_sha, 'delivery_hash': acceptance['delivery_hash'],
            'implementation': {'artifact_id': code_artifact,
                               'dependencies': acceptance['dependencies']}, 'delivery_artifacts': artifacts,
            'delivery_directory_protocol': 1,
            'dependencies': acceptance['dependencies'],
            'local_acceptance': {key: value for key, value in acceptance.items() if key not in {'code', 'files'}}}
        if acceptance.get('protocol_repair'):
            review_context['host_protocol_repair'] = {
                **acceptance['protocol_repair'],
                'review_required': '核对新增诊断数组的实际代码、原实现文档和宿主声明一致性。宿主兼容修复不是科学批准；不得把复用的执行结果报告成重新运行。'}
        review_context.update(implementation_document=specification['document'],
            indexed_documents=[specification['document'], *common['indexed_documents']])
        from .code_bundle import INSTRUCTION as layout_instruction
        review_context['code_layout_contract'] = layout_instruction
        review_context = alignment.attach(review_context, spec)
        review_context['implementation_alignment_manifest_requirements'] = json.loads(
            (Path(snapshot['workspace']) / 'implementation.json').read_text(encoding='utf-8'))['requirements']
        from . import code_repair_contract as repair
        repair_scope = repair.scope(state, attempt, specification)
        if repair_scope:
            review_context[repair.FIELD] = repair_scope
        if repair_receipt:
            review_context['repair_verification'] = repair_receipt
        if failure_review:
            review_context['execution_failure_review'] = failure_review
        if acceptance.get('bounded_checks_only'):
            from .code_followup import evidence as verification_evidence
            review_context['host_verification'] = verification_evidence(acceptance['host_verification'], attempt)
        from . import definition_code_followup as definition_followup
        if definition_followup.applicable(state,specification):
            review_context = await engine._read_consensus_check(run_id,definition_followup.attach,
                engine,run_id,state,specification,attempt,review_context)
            attempt['review_purpose'] = definition_followup.purpose(state,attempt)
            attempt['review_scope'] = 'definition_followup'
            attempt['review_requirement_ids'] = review_context['implementation_alignment_acceptance_ids']
        save(status='code_review')
        host_progress({'step': 'code_review', 'label': '三个角色正在独立核对代码、实现规范和验收记录'})
        votes = await _reviews(engine, h, run_id, attempt.get('review_purpose') or f'code_review:{_digest(key)}:{index}', review_context, delivery=True)
        attempt['review_votes'] = votes
        if not all(v['approve'] for v in votes):
            attempt.update(status='rejected', issues=[issue for vote in votes for issue in vote['issues']])
            save()
            repair.check_progress(state, attempt)
            if identical_snapshot and not failure_review:
                _fail('代码交付未发生实际变化且复审仍否决，停止重复修改循环。',
                    kind='codex_code_no_progress', details={'workflow_id': key, 'attempt': index})
            return {'phase': 'review', 'payload': {'index': index + 1}}
        if failure_review:
            reason = '资源估算缺项复审未形成可执行修改；原 timing_coverage_insufficient 失败仍未解决。'
            attempt.update(status='blocked', issues=[reason])
            save(status='blocked', blocked_kind='codex_repair_unverified', reason=reason)
            _fail(reason, kind='codex_repair_unverified', details={'workflow_id': key, 'attempt': index})
        return finish_delivery(attempt, votes)

    return await dispatch(delivery_handoff, {'index': 0},
                          transition_limit=None if maximum is None else maximum + 2)
