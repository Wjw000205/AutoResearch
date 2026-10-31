"""Engineer-owned immutable implementation drafts, submitted to independent audit."""
from __future__ import annotations

import copy
import json
import math
import re

from jsonschema import ValidationError
from .prose_schema import validate
from jsonschema.validators import validator_for

from . import implementation_documents as documents
from .storage import now
from .architecture_validation import operation

VERSION = 1
TOOL = 'implementation.write_document'
EDIT_TOOL = 'implementation.edit_document'
EDIT_PROTOCOL = 'implementation_document_edit_protocol'
EDIT_VERSION = 1
COLLECTION = 'implementation_document_drafts'
SUBMISSION = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'document_id': {'type': 'string', 'minLength': 1},
        'document_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
        'spec_hash': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}},
    'required': ['document_id', 'document_sha256', 'spec_hash']}
HELP = ('arguments={specification:object,base_spec_hash?:string,expected_sha256?:string} writes the complete implementation SPEC '
    'from context.implementation_document_authoring.specification_schema to immutable task-owned Markdown '
    'and returns its version and section index. Only the assigned engineer may write. To revise the latest '
    'draft, pass its document SHA256 as expected_sha256; old versions remain archived. Do not pass paths, '
    'approval flags, code patches or execution requests. Saving is not approval. Return the latest '
    'submission pointer only after checking the saved document; the controller independently audits it '
    'before dispatching any code implementation. For targeted revisions use '
    '{base_spec_hash:string,patch:[{op:"add"|"replace"|"remove",path:"/JSON/Pointer",value?:any}],expected_sha256?:string} '
    'instead of specification. At most 64 operations, zero-based array indices; remove omits value. '
    'Use implementation_document_base for the exact current base. Unchanged content is preserved by the host. '
    'Every saved SPEC must have unique acceptance IDs with matching implementation mappings and approved-source alignment. '
    'An unapproved draft is not a frozen scientific source; correct, remove or merge erroneous author-added requirements. '
    'Saving neither validates scientific equivalence nor supersedes frozen approved sources. '
    'Every new saved version requires three independent full-text reviews and role-specific replies.')
TASK = ('\n使用 implementation.write_document 编写实现文档，specification 严格遵循 '
    'implementation_document_authoring.specification_schema。工具保存 Markdown 并返回版本和章节索引；'
    '可以用 documents.read/search 检查自己的草稿，修改时 expected_sha256 必须等于当前草稿版本。'
    '完成后只返回工具回传的 submission 对象。未成功保存、旧版本或自行声明批准均不能提交。'
    '保存与提交后仍需同一版本三方独立审计，不代表代码已实现或获准执行。')

PRESERVATION_TASK = ('\n可基于 implementation_document_base.spec_hash 使用真实基线的 JSON Pointer patch，'
    '也可完整重写 specification。CAS 只校验当前实际草稿版本；未通过前稿的条款或作者自增验收 ID 不具有批准地位。'
    '允许纠正、删除或合并错误要求，但本版验收 ID 必须唯一，implementation_map 与 architecture_traceability 完整覆盖本版验收项，'
    '科学定义必须对照已批准架构、共识与冻结计划，且仍由三个角色独立阅读全文审核。'
    '写入返回 saved=false 时按 validation 中的具体错误修复，不得把错误当成功回执或重复原样请求。')
EDIT_HELP = ('arguments={document_id:string,expected_sha256:string,base_spec_hash:string,'
    'patch:[{op:"add"|"replace"|"remove",path:"/JSON/Pointer",value?:any}]} edits the exact '
    'implementation_document_base with at most 64 JSON Pointer operations. All three base identifiers '
    'are required and must match the current draft, otherwise the frozen previous reviewed version. '
    'Only the assigned engineer may edit. Do not send specification or regenerate untouched fields. '
    'The host copies unchanged data and saves a new immutable complete Markdown version; old text '
    'remains archived. Use zero-based array indices and omit value for remove. A successful save '
    'returns submission and is not approval: the new complete document requires three independent '
    'same-version full-text reviews before any implementation dispatch.')
EDIT_HELP += (' When implementation_document_locate_protocol=1, prefer revisions:[{index_id:string,value:any}] '
    'instead of patch to replace complete directory units. Resolve current index IDs through '
    'implementation.locate_document; the host resolves positions, rejects overlaps and preserves all other units. '
    'For local changes to a string unit, use text_replacements:[{before:string,after:string}] instead of value. '
    'Each before must uniquely match the current base; spans cannot overlap. Empty after deletes the span. '
    'Unmatched text is preserved and the complete result still passes the original SPEC validation.')
EDIT_TASK = ('\n已有实现文档时只使用 implementation.edit_document 定向修改，不重新生成整份 specification。'
    '以 implementation_document_base 中 document_id、document_sha256、spec_hash 分别填写 '
    'document_id、expected_sha256、base_spec_hash，只提交本请求工具格式允许的定向修订。首次修改上一审稿也必须填写这三个真实标识；'
    '保存后继续修改时改用当前草稿的新标识。宿主精确保留未改字段并保存新的完整不可变版本，旧原文仍可恢复。'
    '按三方真实审核意见修订，缺少科学依据明确保留未决；既有批准设计与冻结参数不因编辑而改变。'
    '可用 documents.read/search 核对原文与保存后的完整文档，保存成功后只提交真实回执中的 submission；'
    '新版本仍须三方独立阅读全文审核，保存不代表批准、代码实现或执行完成。'
    'saved=false 时按 validation 修复，不得把失败或旧版本指针当成功。')


def write_arguments_schema(spec_schema):
    """Strict tool schema for the bounded save-only continuation, including patches."""
    hash_schema = {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}
    operation = {'oneOf': [
        {'type': 'object', 'additionalProperties': False, 'properties': {
            'op': {'enum': ['add', 'replace']}, 'path': {'type': 'string', 'pattern': '^/'},
            'value': {}}, 'required': ['op', 'path', 'value']},
        {'type': 'object', 'additionalProperties': False, 'properties': {
            'op': {'const': 'remove'}, 'path': {'type': 'string', 'pattern': '^/'}},
         'required': ['op', 'path']}]}
    return {'oneOf': [
        {'type': 'object', 'additionalProperties': False, 'properties': {
            'specification': copy.deepcopy(spec_schema), 'expected_sha256': hash_schema,
            'base_spec_hash': hash_schema}, 'required': ['specification']},
        {'type': 'object', 'additionalProperties': False, 'properties': {
            'patch': {'type': 'array', 'minItems': 1, 'maxItems': 64, 'items': operation},
            'expected_sha256': hash_schema, 'base_spec_hash': hash_schema},
         'required': ['patch', 'base_spec_hash']}]}


def edit_arguments_schema(context=None):
    """Exact-base patch only; reuse the existing bounded JSON Pointer schema."""
    patch = write_arguments_schema({})['oneOf'][1]['properties']['patch']
    sha = {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}
    result = {'type': 'object', 'additionalProperties': False, 'properties': {
        'document_id': {'type': 'string', 'minLength': 1},
        'expected_sha256': copy.deepcopy(sha), 'base_spec_hash': copy.deepcopy(sha),
        'patch': patch}, 'required': ['document_id', 'expected_sha256', 'base_spec_hash', 'patch']}
    from .implementation_document_locator import enabled as locate_enabled
    if not locate_enabled(context or {}):
        return result
    indexed = copy.deepcopy(result)
    del indexed['properties']['patch']
    indexed['required'][-1] = 'revisions'
    indexed['properties']['revisions'] = {'type': 'array', 'minItems': 1, 'maxItems': 64,
        'items': {'type': 'object', 'additionalProperties': False,
            'properties': {'index_id': {'type': 'string', 'minLength': 1, 'maxLength': 2048}, 'value': {},
                'text_replacements': {'type': 'array', 'minItems': 1, 'maxItems': 32,
                    'items': {'type': 'object', 'additionalProperties': False,
                        'properties': {'before': {'type': 'string', 'minLength': 1},
                                       'after': {'type': 'string'}},
                        'required': ['before', 'after']}}},
            'required': ['index_id'], 'oneOf': [{'required': ['value']}, {'required': ['text_replacements']}]}}
    from .implementation_acceptance_author import enabled as batch_enabled, plan_schema
    if batch_enabled(context or {}):
        indexed['properties']['revisions']['minItems'] = 0
        indexed['properties']['revision_plan'] = plan_schema()
        indexed['required'].append('revision_plan')
        return indexed
    return {'oneOf': [result, indexed]}


def editing_enabled(context):
    return type(context.get(EDIT_PROTOCOL)) is int and context[EDIT_PROTOCOL] == EDIT_VERSION


def preferred_tool(context):
    return EDIT_TOOL if (enabled(context) and editing_enabled(context)
                        and context.get('implementation_document_base')) else TOOL


def edit_context(context):
    """Opt in only when preparing a new request, never edit a frozen packet."""
    if not enabled(context) or not context.get('implementation_document_base'):
        return context
    from .implementation_authoring_preservation import EDIT_EXPECTED_SHA256_CONTRACT
    from .implementation_document_locator import PROTOCOL
    return {**context, EDIT_PROTOCOL: EDIT_VERSION, PROTOCOL: 1,
        'implementation_document_preservation': EDIT_TASK,
        'implementation_document_base': {**context['implementation_document_base'],
            'locate_tool': 'implementation.locate_document',
            'expected_sha256_contract': EDIT_EXPECTED_SHA256_CONTRACT}}


def enabled(context):
    return context.get('implementation_document_authoring_protocol') == VERSION


def _assignment(engine, run_id, role, handoff_id):
    from .engine import ResearchBlocked
    from .implementation_indexed_review import PROTOCOL as INDEXED_PROTOCOL
    assignment = engine.store.get('handoffs', handoff_id or '', {})
    context = engine.store.get('handoff_inputs', handoff_id or '', {}).get('context', {})
    binding = context.get('implementation_document_authoring', {})
    state = engine.store.get('codex_workflows', binding.get('workflow_id', ''), {})
    active_generation = engine.store.get('runs', run_id, {}).get('state', {}).get('implementation_generation')
    if (role != 'engineer' or assignment.get('to_role') != role
            or assignment.get('run_id') != run_id
            or not assignment.get('purpose', '').startswith('implementation_spec:')
            or not enabled(context) or state.get('run_id') != run_id
            or state.get('cycle') != engine.cycle(run_id)
            or state.get('implementation_document_authoring_protocol') != VERSION
            or state.get('document_protocol') != 2
            or type(binding.get('revision')) is not int
            or type(state.get(INDEXED_PROTOCOL)) is not int or state[INDEXED_PROTOCOL] != 1
            or type(context.get(INDEXED_PROTOCOL)) is not int or context[INDEXED_PROTOCOL] != 1
            or context.get('implementation_author_scope') is not None
            or context.get('implementation_incremental_target') is not None
            or active_generation is not None and state.get('implementation_generation') != active_generation):
        raise ResearchBlocked('实现文档写入仅限当前工程师的实现规范编写交接')
    return assignment, context, state, binding


def pointer(record):
    doc = record['row']['document']
    return {'document_id': doc['document_id'], 'document_sha256': doc['sha256'],
            'spec_hash': record['row']['spec_hash']}


def submission_schema(context):
    from .implementation_issue_responses import author_schema
    from .implementation_acceptance_flow import submission_schema as acceptance_schema
    return acceptance_schema(author_schema(SUBMISSION, context), context)


def submitted_result(record):
    return copy.deepcopy(record.get('acceptance_patch_submission', record.get('issue_response_submission', pointer(record))))


def _checked(engine, run_id, record):
    from .engine import digest
    row, context = record['row'], record['context']
    if (record.get('run_id') != run_id or record.get('write_tool', TOOL) not in {TOOL, EDIT_TOOL}
            or digest(row['spec']) != row['spec_hash']
            or record.get('context_sha256') != digest(context)
            or ('write_arguments' in record
                and digest(record['write_arguments']) != record.get('write_arguments_sha256'))
            or ('revision_progress' in record
                and digest(record['revision_progress']) != record.get('revision_progress_sha256'))):
        raise ValueError('Implementation draft identity or fingerprint changed')
    doc = row['document']
    documents.document_index.load(engine, run_id, doc['document_id'], doc['sha256'])
    return record


def _rejected(engine, run_id, state, handoff_id, args, exc):
    from .engine import digest
    details = {'code': exc.code, **exc.details}
    engine.store.event(run_id, 'implementation.document_save_rejected', str(exc),
        {'handoff_id': handoff_id, 'workflow_id': state['id'],
         'arguments_sha256': digest(args), 'validation': details, 'saved': False})
    result = {'saved': False, 'error': str(exc), 'validation': details,
              'approved': False, 'implementation_dispatched': False}
    from .implementation_acceptance_author import enabled as batch_enabled
    if batch_enabled(engine.store.get('handoff_inputs', handoff_id, {}).get('context', {})):
        result['revision_retry'] = {'instruction':
            '本次未保存；此前已保存版本保留。修正 validation 后重试失败项，再补齐遗漏意见。',
            'index_ids': [r['index_id'] for r in args.get('revisions', [])
                          if isinstance(r, dict) and isinstance(r.get('index_id'), str)]}
    from .implementation_author_revision import KEY, root
    if KEY in engine.store.get('handoff_inputs', handoff_id, {}).get('context', {}):
        operations = args.get('patch', []) if isinstance(args, dict) else []
        operations = operations if isinstance(operations, list) else []
        if isinstance(args, dict) and isinstance(args.get('revisions'), list):
            operations = [{'path': '/' + item['index_id'].split('/', 1)[0]}
                          for item in args['revisions'] if isinstance(item, dict)
                          and isinstance(item.get('index_id'), str)]
        result['retry_whole_patch'] = {'fields': sorted({root(item.get('path'))
            for item in operations if isinstance(item, dict) and isinstance(item.get('path'), str)
            and root(item['path'])}),
            'instruction': '整批未保存；修正报错后重交全部必要修改，不能只重交报错字段而遗漏其余字段。'}
    return result


def _schema_validation_detail(error):
    """Never echo the invalid instance: JSON Schema messages can contain a full SPEC."""
    detail = {'kind': 'schema', 'field_path': list(error.absolute_path),
              'validator': error.validator, 'reason': '字段不符合 SPEC 格式约束。'}
    if error.validator in {'maxLength', 'minLength'}:
        detail.update(actual_length=len(error.instance), limit=error.validator_value,
                      reason='文本长度超出允许范围。')
    elif error.validator in {'maxItems', 'minItems', 'maxProperties', 'minProperties'}:
        detail.update(actual_count=len(error.instance), limit=error.validator_value,
                      reason='项目数量超出允许范围。')
    elif error.validator == 'type':
        detail.update(expected_type=error.validator_value, reason='字段类型不符合要求。')
    elif error.validator == 'required' and isinstance(error.instance, dict):
        detail.update(missing_fields=[key for key in error.validator_value if key not in error.instance],
                      reason='缺少必需字段。')
    elif error.validator == 'additionalProperties' and isinstance(error.instance, dict):
        properties = error.schema.get('properties', {})
        patterns = error.schema.get('patternProperties', {})
        detail.update(extra_fields=[key for key in error.instance if key not in properties
            and not any(re.search(pattern, key) for pattern in patterns)], reason='存在未声明字段。')
    elif error.validator in {'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum'}:
        detail.update(limit=error.validator_value, reason='数值超出允许范围。')
    return detail


def save_validation_details(spec, specification_schema, context):
    """Pure, compact format/ID diagnostics; it grants no scientific or save approval."""
    from . import implementation_alignment as alignment
    from . import implementation_module_details as module_details
    specification_schema = module_details.validation_schema(specification_schema, context)
    from .prose_schema import relax
    specification_schema = relax(specification_schema)
    validator_type = validator_for(specification_schema)
    validator_type.check_schema(specification_schema)
    details = [_schema_validation_detail(error)
               for error in validator_type(specification_schema).iter_errors(spec)]

    def finite_values(value, path):
        if isinstance(value, float) and not math.isfinite(value):
            details.append({'kind': 'schema', 'field_path': path, 'validator': 'finite_json',
                            'reason': 'JSON 数值必须有限。'})
        elif isinstance(value, dict):
            for key, item in value.items():
                finite_values(item, [*path, key])
        elif isinstance(value, list):
            for index, item in enumerate(value):
                finite_values(item, [*path, index])
    finite_values(spec, [])
    details.sort(key=lambda item: tuple(str(part) for part in item['field_path']))
    if not isinstance(spec, dict):
        return details
    checks = spec.get('acceptance_checks')
    if (isinstance(checks, list)
            and all(isinstance(row, dict) and isinstance(row.get('id'), str) for row in checks)):
        ids = [row['id'] for row in checks]
        indices = {}
        for index, requirement_id in enumerate(ids):
            indices.setdefault(requirement_id, []).append(index)
        duplicates = {key: positions for key, positions in indices.items() if len(positions) > 1}
        if duplicates:
            details.append({'kind': 'structure', 'field_path': ['acceptance_checks'],
                'validator': 'unique_acceptance_ids', 'reason': '实现规范验收项 ID 不能重复。',
                'duplicate_ids': list(duplicates), 'duplicate_indices': duplicates, 'index_base': 0})
        rows = spec.get('implementation_map')
        if (isinstance(rows, list) and all(isinstance(row, dict)
                and isinstance(row.get('requirement_id'), str) for row in rows)):
            mapped = [row['requirement_id'] for row in rows]
            missing, extra = sorted(set(ids) - set(mapped)), sorted(set(mapped) - set(ids))
            if missing or extra:
                details.append({'kind': 'structure', 'field_path': ['implementation_map'],
                    'validator': 'acceptance_id_coverage', 'reason': '实现位置映射必须覆盖全部验收 ID。',
                    'missing_ids': missing, 'extra_ids': extra})
    branches = spec.get('branches')
    if (isinstance(branches, list) and all(isinstance(row, dict)
            and isinstance(row.get('variant'), str) for row in branches)):
        expected, actual = {'baseline', 'proposed', 'ablation'}, {row['variant'] for row in branches}
        if expected != actual:
            details.append({'kind': 'structure', 'field_path': ['branches'],
                'validator': 'variant_coverage', 'reason': '实现规范必须逐一覆盖 baseline/proposed/ablation。',
                'missing_variants': sorted(expected - actual), 'extra_variants': sorted(actual - expected)})
    details.extend(alignment.traceability_validation_details(spec, context))
    details.extend(module_details.validation_details(spec, context))
    return details



def _validate_candidate(spec, base, binding, context, state, tool_name, args):
    from .codex_workflow import SPEC, _validate_spec
    from .engine import digest, ResearchBlocked
    from . import implementation_authoring_preservation as preservation
    algorithm_max_length = None
    if context.get('implementation_definition_repair_protocol') == 1:
        from .implementation_definition_repair import validate_candidate
        validate_candidate(spec, state, context['definition_repair'])
        algorithm_max_length = binding['specification_schema']['properties']['algorithm']['maxLength']
    problems = save_validation_details(spec, binding.get('specification_schema', SPEC), context)
    schema_problems = [problem for problem in problems if problem['kind'] == 'schema']
    if schema_problems:
        raise preservation.SaveConflict(
            'invalid_document_patch_result' if 'patch' in args else 'invalid_document_specification',
            '文档不符合 SPEC 格式；未保存，请修复 validation.errors 中指出的字段。',
            **schema_problems[0], errors=problems)
    if tool_name == EDIT_TOOL:
        from .implementation_document_locator import duplicate_targets
        duplicates = duplicate_targets(spec, base)
        if duplicates:
            raise preservation.SaveConflict('invalid_implementation_structure', duplicates[0]['reason'],
                **duplicates[0], errors=duplicates, base=preservation.descriptor(base, edit=True),
                locate_tool='implementation.locate_document')
    # Preserve JSON serializability and the existing requirement-retention gate.
    json.dumps(spec, ensure_ascii=False, allow_nan=False)
    if problems:
        raise preservation.SaveConflict('invalid_implementation_structure', problems[0]['reason'],
                                        **problems[0], errors=problems)
    try:
        _validate_spec(spec, context, design=(state.get('common') or {}).get('design'),
                       algorithm_max_length=algorithm_max_length)
    except ResearchBlocked as exc:
        problems = getattr(exc, 'implementation_validation_details', None) or []
        details = {**problems[0], 'errors': problems} if problems else {}
        references = getattr(exc, 'alignment_reference_problems', None)
        if references:
            details.update(field_path=['architecture_traceability'], reference_problems=references)
        raise preservation.SaveConflict(
            'invalid_implementation_structure', str(exc), **details)
    except ValueError as exc:
        if spec.get('comparison_transport') is None:
            raise
        raise preservation.SaveConflict(
            'invalid_comparison_transport', '运输合同格式或数组声明无效；未保存。',
            field_path=['comparison_transport'], reason=str(exc))
    if spec.get('comparison_transport') is not None:
        from .comparison_transport import validate_contract
        plan = (state.get('common') or {}).get('experiment_plan')
        if not isinstance(plan, dict) or not plan:
            raise preservation.SaveConflict(
                'comparison_transport_plan_unavailable', '本工作流缺少冻结实验计划，无法核对运输消费者；未保存。')
        if context.get('experiment_plan_hash') != digest(plan):
            raise preservation.SaveConflict(
                'comparison_transport_plan_conflict', '运输合同所需计划与本次编写输入的冻结计划哈希不一致；未保存。')
        try:
            validate_contract(spec['comparison_transport'], plan=plan,
                primary_metric=(state.get('common') or {}).get('design', {}).get('primary_metric'))
        except (ValueError, ValidationError) as exc:
            raise preservation.SaveConflict(
                'invalid_comparison_transport', '运输合同与冻结计划不一致或格式无效；未保存。',
                field_path=['comparison_transport'], reason=str(exc))


def write(engine, h, run_id, role, handoff_id, args):
    return _save(engine, h, run_id, role, handoff_id, args, tool_name=TOOL)


def edit(engine, h, run_id, role, handoff_id, args):
    return _save(engine, h, run_id, role, handoff_id, args, tool_name=EDIT_TOOL)


def _save(engine, h, run_id, role, handoff_id, args, *, tool_name):
    from .codex_workflow import SPEC, _validate_spec
    from .engine import digest, ResearchBlocked
    from .implementation_authoring_commit import publish, run_guard, same_request
    control = run_guard(engine.store.get('runs', run_id, {}))
    assignment, context, state, binding = _assignment(engine, run_id, role, handoff_id)
    if (assignment.get('status') == 'completed' or state.get('status') != 'specification'
            or state.get('current_revision') != binding['revision']):
        raise ResearchBlocked('实现文档编写已提交或进入审计，不能覆盖原版本')
    if (tool_name == TOOL and (not isinstance(args, dict)
            or set(args) - {'specification', 'expected_sha256', 'base_spec_hash', 'patch'})):
        raise ValueError('Implementation document arguments are outside the authoring contract')
    if tool_name == TOOL and 'expected_sha256' in args and (not isinstance(args['expected_sha256'], str)
            or not re.fullmatch('[0-9a-f]{64}', args['expected_sha256'])):
        raise ValueError('expected_sha256 must be the current document SHA256')
    latest = engine.store.get(COLLECTION, handoff_id)
    if latest:
        if tool_name == EDIT_TOOL and (
                latest.get('id') != handoff_id or latest.get('role') != role
                or latest.get('workflow_id') != state['id']
                or latest.get('revision') != binding['revision'] or latest.get('context') != context):
            from .implementation_authoring_preservation import SaveConflict
            return _rejected(engine, run_id, state, handoff_id, args, SaveConflict(
                'authoring_base_conflict', '当前草稿不属于本次冻结编写交接；未应用修改。'))
        _checked(engine, run_id, latest)
        # A completed patch is replayed by its exact saved request, never applied twice.
        if same_request(latest, tool_name, args):
            return _result(latest)
    from . import implementation_authoring_preservation as preservation
    revision_progress = None
    try:
        base = preservation.baseline(engine, run_id, state, binding, context, latest)
        from .implementation_acceptance_author import enabled as batch_enabled, validate_batch
        edit_args = args
        if batch_enabled(context) and base:
            if tool_name != EDIT_TOOL:
                raise preservation.SaveConflict('incomplete_revision_batch', '本轮须使用索引计划与整批修订工具。')
            preservation.check_edit_base(args, base)
            validate_batch(context, base, args)
            from .implementation_revision_batch import resolve, progress, status
            spec, saved_units, failed_units = resolve(base, args,
                lambda candidate: _validate_candidate(candidate, base, binding, context, state, tool_name, args))
            prior = status(engine, handoff_id, context, latest)
            revision_progress = progress(context, base, args, prior, saved_units, failed_units)
            if failed_units and not saved_units:
                raise preservation.SaveConflict('revision_units_failed',
                    '本次待改位置未通过保存校验；此前成功项保留，请只修复失败位置。', revision_progress=revision_progress)
        else:
            spec = (preservation.resolve_edit(edit_args, base) if tool_name == EDIT_TOOL
                    else preservation.resolve(args, base))
            _validate_candidate(spec, base, binding, context, state, tool_name, args)
    except preservation.SaveConflict as exc:
        return _rejected(engine, run_id, state, handoff_id, args, exc)
    sh = digest(spec)
    expected = args.get('expected_sha256')
    if latest:
        _checked(engine, run_id, latest)
        if (revision_progress is None and latest['row']['spec_hash'] == sh
                and expected in (latest['row']['document']['sha256'], latest.get('previous_sha256'))):
            return _result(latest)
        if expected != latest['row']['document']['sha256']:
            raise ValueError('修改实现文档必须绑定当前草稿 expected_sha256')
    elif expected is not None and tool_name == TOOL:
        raise ValueError('首次编写实现文档不能指定不存在的旧版本')
    row = {'index': binding['revision'], 'spec': spec, 'spec_hash': sh}
    try:
        if base and base['index'] == row['index'] and base['spec_hash'] == sh:
            # A continuation may only record a blocked/already-satisfied item.
            # Its new source directory must not re-render an unchanged SPEC
            # under the predecessor's immutable file identity. The baseline
            # has already been authenticated; reuse its exact document bytes.
            for key in ('document', 'document_protocol', 'input_documents',
                        'implementation_document_readability_protocol'):
                if key in base:
                    row[key] = copy.deepcopy(base[key])
        else:
            documents.save_specification(engine, h, run_id, state, row, context)
    except documents.SpecificationTooLarge as exc:
        return _rejected(engine, run_id, state, handoff_id, args, preservation.SaveConflict(
            'document_size_limit', '完整文档超过本地索引大小限制；未保存，请精简本批修改后重试。',
            actual_bytes=exc.actual_bytes, limit_bytes=exc.limit_bytes))
    record = {'id': handoff_id, 'run_id': run_id, 'role': role,
        'workflow_id': state['id'], 'revision': binding['revision'],
        'status': 'draft', 'row': row, 'context': copy.deepcopy(context),
        'context_sha256': digest(context), 'previous_sha256': expected,
        'write_arguments': copy.deepcopy(args), 'write_arguments_sha256': digest(args),
        'write_tool': tool_name,
        'base_spec_hash': base['spec_hash'] if base else None,
        'versions': [*(latest or {}).get('versions', []), pointer({'row': row})],
        'provenance': 'engineer_document_tool', 'created_at': (latest or {}).get('created_at', now()),
        'updated_at': now()}
    if revision_progress is not None:
        record['revision_progress'] = revision_progress
        record['revision_progress_sha256'] = digest(revision_progress)
    from .implementation_author_revision import save_changes
    changes = save_changes(context, base, spec)
    if changes is not None:
        record['save_changes'] = changes
    from .implementation_document_delivery import location
    record['delivery'] = location(engine, run_id, row['document'], revision=row['index'], spec_hash=sh)
    try:
        committed = publish(engine, run_id, handoff_id, record, latest=latest,
            assignment=assignment, context=context, state=state, control=control)
    except preservation.SaveConflict as exc:
        return _rejected(engine, run_id, state, handoff_id, args, exc)
    return _result(_checked(engine, run_id, committed))


def _result(record):
    return {'saved': True, 'submission': pointer(record), 'document': record['row']['document'],
            'audit_status': 'pending', 'approved': False, 'implementation_dispatched': False,
            'provenance': record['provenance'],
            **({'revision_progress': copy.deepcopy(record['revision_progress'])} if 'revision_progress' in record else {}),
            **({'delivery': copy.deepcopy(record['delivery'])} if 'delivery' in record else {}),
            **({'save_changes': copy.deepcopy(record['save_changes'])} if 'save_changes' in record else {})}


def scoped_context(engine, run_id, role, handoff_id, context):
    if not enabled(context) or role != 'engineer':
        return context
    _, frozen_context, state, binding = _assignment(engine, run_id, role, handoff_id)
    directories = {d['document_id']: d for d in context['indexed_documents']}
    for previous in state.get('specifications', []):
        if previous.get('index') != binding['revision'] - 1:
            continue
        doc = previous['document']
        directories[doc['document_id']] = documents.document_index.directory(
            engine, run_id, doc['document_id'], doc['sha256'])
    record = engine.store.get(COLLECTION, handoff_id)
    from .implementation_authoring_preservation import baseline, descriptor, SaveConflict
    if record:
        _checked(engine, run_id, record)
    try:
        base_row = baseline(engine, run_id, state, binding, frozen_context, record)
        base = descriptor(base_row, edit=editing_enabled(context))
    except SaveConflict as exc:
        # A context projection is not a write. Expose the conflict for repair;
        # write() and successor identity checks still fail closed at their gates.
        base = None
        base_row = None
        context = {**context, 'implementation_document_base_error': {'code': exc.code, 'error': str(exc)}}
    context = {**context, 'implementation_document_base': base,
               'implementation_document_preservation': EDIT_TASK if editing_enabled(context) else PRESERVATION_TASK}
    if base_row:
        from .implementation_self_check import project as self_check_project, KEY as SELF_CHECK
        checks = self_check_project(engine, run_id, handoff_id, frozen_context, base_row)
        if checks is not None:
            context[SELF_CHECK] = checks
    from .implementation_acceptance_author import enabled as batch_enabled
    if batch_enabled(context):
        from .implementation_revision_batch import status
        context['acceptance_author_saved_progress'] = status(engine, handoff_id, frozen_context, record)
        if base_row and context.get('acceptance_contract'):
            from .implementation_acceptance_author import patch_manifest
            previous = next(row for row in state['specifications'] if row['index'] == binding['revision'] - 1)
            context['acceptance_author_patch_manifest'] = patch_manifest(context, previous, base_row)
            if not record:
                from .implementation_acceptance_author import unchanged_save_retry
                retry = unchanged_save_retry(engine, handoff_id, context, base_row)
                if retry:
                    context['acceptance_author_save_retry'] = retry
    from .implementation_author_revision import KEY
    if KEY in context:
        from .implementation_author_patch_intents import derive
        previous = next((row for row in state['specifications'] if row['index'] == binding['revision'] - 1), {})
        prior_spec = previous.get('spec', {})
        current_spec = record['row']['spec'] if record else prior_spec
        pending = derive(context, engine.store.get('handoff_progress', handoff_id, {}).get('tool_results', []),
                         prior_spec, current_spec)
        context['implementation_author_revision_pending'] = {**pending,
            'instruction': '失败批次中这些字段仍未落盘。分步保存可以保留，但全部原修改要求完成前不提交三方审核。'}
    if not record:
        return {**context, 'indexed_documents': list(directories.values())}
    if frozen_context.get('implementation_definition_repair_protocol') == 1:
        from .implementation_definition_repair import addition_ref
        context['definition_addition'] = addition_ref(engine, run_id, state, record['row'])
    for version in record['versions']:
        doc = documents.document_index.directory(engine, run_id, version['document_id'], version['document_sha256'])
        directories[doc['document_id']] = doc
    return {**context, 'implementation_document_draft': _result(record),
            'indexed_documents': list(directories.values())}


@operation
def _prepare_submission(engine, run_id, state, handoff_id, result):
    """A final pointer cannot fabricate a saved document or an audit approval."""
    from .engine import ResearchBlocked
    assignment, frozen_context, bound_state, _ = _assignment(engine, run_id, 'engineer', handoff_id)
    record = engine.store.get(COLLECTION, handoff_id)
    from . import implementation_issue_responses as responses
    new_contract = frozen_context.get(responses.PROTOCOL) == responses.VERSION
    from . import implementation_acceptance as acceptance, implementation_acceptance_flow as flow
    submitted_pointer = ({key: result.get(key) for key in SUBMISSION['properties']}
                         if (new_contract or acceptance.enabled(frozen_context)) and isinstance(result, dict) else result)
    if not record or submitted_pointer != pointer(_checked(engine, run_id, record)):
        raise ResearchBlocked('实现文档提交缺少本交接成功写入的同版本工具记录')
    decision = engine.store.get('decisions', handoff_id, {})
    progress = engine.store.get('handoff_progress', handoff_id, {})
    receipts = progress.get('tool_results', [])
    context = engine.store.get('handoff_inputs', handoff_id, {}).get('context')
    original = copy.deepcopy(record)
    guards = {'handoffs': assignment, 'handoff_inputs': engine.store.get('handoff_inputs', handoff_id),
        'decisions': decision, 'handoff_progress': progress, COLLECTION: original}
    configuration = engine.store.get('runs', run_id, {}).get('config')
    workflow_guard = {key: bound_state.get(key) for key in ('id', 'run_id', 'cycle', 'status', 'current_revision',
        'input_hash', 'proposal_hash', 'design_hash', 'experiment_plan_hash')}
    if (assignment.get('status') != 'completed'
            or record['workflow_id'] != state['id'] or record['context'] != context
            or decision.get('role') != 'engineer'
            or decision.get('run_id') != run_id or decision.get('result') != result
            or not any(r.get('call', {}).get('name') == record.get('write_tool', TOOL)
                and (r['call'].get('arguments') == record['write_arguments']
                     if 'write_arguments' in record else
                     r['call'].get('arguments', {}).get('specification') == record['row']['spec'])
                and ('write_arguments' in record
                     or r['call']['arguments'].get('expected_sha256') == record.get('previous_sha256'))
                and r.get('result', {}).get('saved') is True
                and r['result'].get('submission') == submitted_pointer for r in receipts)):
        raise ResearchBlocked('实现文档提交缺少真实写入回执或工程师最终交付')
    receipt = responses.validate_author(engine, run_id, state, record['row'],
        handoff_id, result, frozen_context) if new_contract else None
    if receipt is not None:
        if record.get('issue_response_submission') is not None and (
                record['issue_response_submission'] != result or record.get(responses.RECEIPT) != receipt):
            raise ResearchBlocked('工程师已经提交的逐项回应或原读取回执已改变。')
        record['issue_response_submission'] = copy.deepcopy(result)
        record[responses.RECEIPT] = copy.deepcopy(receipt)
    row = {**copy.deepcopy(record['row']), 'authoring_handoff_id': handoff_id,
            'implementation_document_authoring_protocol': VERSION,
            **({responses.PROTOCOL: responses.VERSION, responses.RECEIPT: receipt} if receipt is not None else {})}
    if acceptance.enabled(frozen_context):
        patches = flow.validate_patch(engine, run_id, row, handoff_id, result, frozen_context)
        row[acceptance.PROTOCOL] = acceptance.VERSION
        row['acceptance_patch_reports'] = patches or []
        if record.get('acceptance_patch_submission') not in (None, result):
            raise ResearchBlocked('工程师已提交的验收问题回应发生变化。')
        record['acceptance_patch_submission'] = copy.deepcopy(result)
    return {'record': record, 'row': row, 'guards': guards, 'configuration': configuration,
        'workflow_guard': workflow_guard}


def _commit_submission(engine, run_id, handoff_id, prepared):
    from .engine import ResearchBlocked
    workflow = engine.store.get('codex_workflows', prepared['workflow_guard']['id'], {})
    if (any(engine.store.get(collection, handoff_id) != value for collection, value in prepared['guards'].items())
            or engine.store.get('runs', run_id, {}).get('config') != prepared['configuration']
            or any(workflow.get(key) != value for key, value in prepared['workflow_guard'].items())):
        raise ResearchBlocked('工程师提交核验期间原冻结输入、读取、交付或配置已改变，未提交新版本。')
    record = prepared['record']
    if record['status'] != 'submitted':
        record.update(status='submitted', updated_at=now())
        engine.store.put(COLLECTION, handoff_id, record)
    return prepared['row']


def submit(engine, run_id, state, handoff_id, result):
    """Compatibility sync API; production uses the guarded async preparation."""
    return _commit_submission(engine, run_id, handoff_id,
        _prepare_submission(engine, run_id, state, handoff_id, result))


async def submit_async(engine, run_id, state, handoff_id, result):
    prepared = await engine._read_consensus_check(run_id, _prepare_submission,
        engine, run_id, state, handoff_id, result)
    engine.check_active(run_id)
    return _commit_submission(engine, run_id, handoff_id, prepared)


def validate_submission(engine, run_id, state, row):
    from .engine import ResearchBlocked
    if state.get('implementation_document_authoring_protocol') != VERSION:
        return
    handoff_id = row.get('authoring_handoff_id')
    record = engine.store.get(COLLECTION, handoff_id or '')
    if (not record or record.get('status') != 'submitted'
            or record.get('revision') != row.get('index')):
        raise ResearchBlocked('实现文档审计缺少已提交的工程师版本')
    expected = submitted_result(record)
    checked = _prepare_submission(engine, run_id, state, handoff_id, expected)['row']
    if checked['spec'] != row['spec'] or checked['document'] != row['document']:
        raise ResearchBlocked('实现文档审计内容与工程师提交不一致')
    if checked.get('acceptance_patch_reports') != row.get('acceptance_patch_reports'):
        raise ResearchBlocked('工程师修复证据与原提交不一致')
    from . import implementation_issue_responses as responses
    if (checked.get(responses.PROTOCOL) != row.get(responses.PROTOCOL)
            or checked.get(responses.RECEIPT) != row.get(responses.RECEIPT)):
        raise ResearchBlocked('实现文档逐项回应与原工程师完整提交不一致。')
