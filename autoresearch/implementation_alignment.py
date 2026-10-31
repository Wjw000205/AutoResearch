"""Version-bound architecture/spec/code traceability for newly frozen assignments.

Source references locate obligations; they are neither reading receipts nor proof
of semantic equivalence. Existing paid requests retain their original contract.
"""
from __future__ import annotations

import copy
import re

from jsonschema import ValidationError
from .prose_schema import validate


FIELD = 'implementation_alignment_protocol'
VERSION = 1
TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 6000, 'pattern': r'\S'}
HASH = {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}


def _object(fields):
    return {'type': 'object', 'properties': fields, 'required': list(fields),
            'additionalProperties': False}


SOURCE_REF = _object({'document_id': TEXT, 'document_sha256': HASH,
    'section_id': TEXT, 'offset': {'type': 'integer', 'minimum': 0},
    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 6000}})
SOURCE_REFS = {'type': 'array', 'minItems': 1, 'maxItems': 64, 'items': SOURCE_REF}
TRACEABILITY_SCHEMA = {'type': 'array', 'minItems': 1, 'maxItems': 64,
    'items': _object({'requirement_id': TEXT, 'source_refs': SOURCE_REFS, 'rationale': TEXT})}
BINDING_SCHEMA = _object({key: HASH for key in (
    'design_hash', 'experiment_plan_hash', 'architecture_document_sha256')})
FINDINGS_SCHEMA = {'type': 'array', 'maxItems': 128, 'items': _object({
    'requirement_id': TEXT, 'source_refs': SOURCE_REFS, 'implementation_evidence': TEXT,
    'status': {'enum': ['aligned', 'mismatch', 'pending']}})}
FINDINGS_DUPLICATE = '架构对齐评审重复返回同一验收项。'

AUTHOR_GUIDANCE = '''
implementation_alignment_contract 指明当前批准架构、定义补充和冻结计划的唯一来源版本。
architecture_traceability 为每个 acceptance_checks.id 记录批准来源的精确章节/字符范围及落实理由；
用 document_id、document_sha256、section_id、offset、limit 定位，不把历史规范或宿主能力当批准依据。
批准架构 approved_definition_binding 明确列出的 inheritance 可引用，但必须使用同一文档哈希，
且整个引用落在 retained_ranges 指定的章节字符区间内；未继承或已被替换的旧段不能引用。
先按需读取对应原文，再同时核对架构要求是否全部进入实现规范；不能只覆盖自己新列出的验收项而遗漏原要求。
重点保留机制、baseline/proposed/ablation、数据与隔离、参数矩阵、指标、资源及验收停止规则；
允许等价的实现细化，不能自行改变科研机制、删比较、改阈值或把尚未验证的计划写成结果。
需要改变批准科学要求时保留差异并交回批准流程；不要用改写实现文档制造等价或批准。
'''
REVIEW_GUIDANCE = '''
对照 implementation_alignment_contract 指定的当前批准来源审查，不能只检查实现规范内部自洽。
按本组职责读取决定性的架构/定义/冻结计划原文，检查实现细化是否忠实、是否遗漏来源义务；
不要求每组重复整份架构，未读后续组不因此否决本组，目录、映射、哈希不等于已读或已对齐。
alignment_binding 回传当前设计、计划和架构原文哈希；alignment_findings 逐项列来源范围、具体实现依据与
aligned/mismatch/pending。无科学义务的说明段可返回空 findings；最终完整评议必须覆盖全部验收 ID，
并独立检查来源要求有没有被整项遗漏，不能因作者没有列入映射就漏审。
同一 requirement_id 只能返回一项；多个来源或子条件应放在该项的 source_refs 和 implementation_evidence，
保留全部条件，状态采用最强未解决状态（mismatch 优先于 pending，再是 aligned）。
source_refs 使用 implementation_alignment_contract.primary_sources 中的精确当前版本，
也可使用批准架构 approved_definition_binding.inheritance 明确保留的同版本定义来源范围。
继承引用须完全落在 retained_ranges 的指定章节区间内；不能扩展到整份旧稿、已替换段或其他历史候选。
mismatch 必须给出冲突的源条款、实现位置、具体改法和验证方法；pending 给出决定性补读/复核步骤，
交入 issues/fixes，分别沿用 document_revision/review_incomplete 路由；外部依赖沿用 external_dependency。
任何 mismatch/pending 都不能批准。仅阻断科学范围、机制、泄漏、公平性、复现、资源或验收的实质缺口；
可选优化、命名和等价工程选型不作为否决理由。已读与来源引用本身不能代替独立语义核对。
'''
CODE_GUIDANCE = REVIEW_GUIDANCE + '''
代码评审沿用同一批准架构、实现规范和验收 ID，将来源义务逐项对应到实际冻结代码位置及验证证据。
同时检查“架构→规范”和“规范→代码”，不能以规范已获批准为由忽略明显架构偏离；
implementation_evidence 说明实际文件/函数及核对结果，计划位置、完成声明和 smoke 不证明科学等价。
code_evidence 按同一验收 ID 列实际交付源码或测试的 artifact_id、原字节 sha256、offset、limit 和 verification。
每项 implementation_refs 还须引用实际读过的当前 implementation_document 精确版本与范围，连接批准规范和代码证据。
offset/limit 是 UTF-8 解码后字符范围，必须先通过 artifacts.read 实际读到；一页可支持多个验收项，不能引用不存在的范围。
代码文件须属于该 ID 在真实 implementation.json 中的 locations；清单文本本身不能充当代码落实证据。
verification 如实区分静态检查、已经执行的有界测试、尚未执行的正式科研验证；未跑正式实验本身不阻止代码审查。
'''

# These strings are attached only after the caller has restored any already
# frozen handoff. New assignments carry the same task-standard review throughout.
from .comparison_task_policy import REVIEW_GUIDANCE as TASK_REVIEW_GUIDANCE
AUTHOR_GUIDANCE += TASK_REVIEW_GUIDANCE + (
    '\n实现规范解释并落实已批准的共同任务条件；不能给冻结实验计划自行增加或改写 comparison_task、'
    '长度、变量或划分。旧计划没有声明时按批准来源和真实参数核对；需改变任务时明确提出修订。')
REVIEW_GUIDANCE += TASK_REVIEW_GUIDANCE
CODE_GUIDANCE += TASK_REVIEW_GUIDANCE


def enabled(context):
    return context.get(FIELD) == VERSION


def _fail(message):
    from .engine import ResearchBlocked
    raise ResearchBlocked(message)


def _schema_check(value, schema, label):
    try:
        validate(value, schema)
    except (ValidationError, TypeError) as exc:
        _fail(label + '格式无效：' + (exc.message if isinstance(exc, ValidationError) else str(exc)))


def author_schema(base):
    result = copy.deepcopy(base)
    result['properties']['architecture_traceability'] = copy.deepcopy(TRACEABILITY_SCHEMA)
    if 'architecture_traceability' not in result['required']:
        result['required'].append('architecture_traceability')
    return result


def review_schema(base):
    result = copy.deepcopy(base)
    for name, schema in (('alignment_binding', BINDING_SCHEMA), ('alignment_findings', FINDINGS_SCHEMA)):
        result['properties'][name] = copy.deepcopy(schema)
        if name not in result['required']:
            result['required'].append(name)
    return result


def code_review_schema(base):
    result = review_schema(base)
    result['properties']['code_evidence'] = {'type': 'array', 'maxItems': 256, 'items': _object({
        'requirement_id': TEXT, 'artifact_id': TEXT, 'sha256': HASH,
        'offset': {'type': 'integer', 'minimum': 0},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 16000},
        'implementation_refs': SOURCE_REFS, 'verification': TEXT})}
    result['required'].append('code_evidence')
    return result


def attach(context, spec=None):
    """Called only after checking for an existing frozen assignment."""
    result = {**context, FIELD: VERSION,
              'implementation_alignment_contract': context_contract(context)}
    if spec is not None:
        result['implementation_alignment_acceptance_ids'] = [row['id'] for row in spec['acceptance_checks']]
        # The immutable document is the mapping source; keep just a locator here.
        result['implementation_alignment_mapping'] = {
            'document_id': (context.get('implementation_document') or {}).get('document_id'),
            'document_sha256': (context.get('implementation_document') or {}).get('sha256'),
            'field': 'architecture_traceability', 'present': 'architecture_traceability' in spec}
    return result


def _primary_documents(context):
    """Resolve explicit approved pointers, never newest-by-date or historic kind."""
    indexed = context.get('indexed_documents', [])
    if not isinstance(indexed, list) or any(not isinstance(doc, dict) for doc in indexed):
        _fail('架构对齐缺少固定输入文档索引。')
    by_id = {}
    for doc in indexed:
        did = doc.get('document_id')
        if not isinstance(did, str) or not did:
            _fail('架构对齐输入文档缺少身份。')
        if did in by_id and by_id[did] != doc:
            _fail('架构对齐输入文档存在同 ID 冲突版本。')
        by_id[did] = doc
    roles = (
        ('architecture_document', {'architecture_design'}, None),
        ('approved_proposal_document', {'approved_proposal'}, 'approved_proposal'),
        ('frozen_experiment_plan_document', {'frozen_experiment_plan'}, 'frozen_experiment_plan'),
        ('approved_definition_document', None, None),
        ('protocol_document', {'revised_experiment_protocol'}, None),
    )
    forbidden = {'implementation_specification', 'implementation_review_history',
                 'host_implementation_contract', 'original_experiment_plan',
                 'architecture_host_receipt', 'architecture_author_response'}
    selected = []
    for field, kinds, fallback in roles:
        pointer = context.get(field)
        if pointer is None and fallback:
            choices = [doc for doc in indexed if doc.get('kind') == fallback]
            if len(choices) > 1:
                _fail('架构对齐存在多个候选' + field + '，必须显式绑定当前批准版本。')
            pointer = choices[0] if choices else None
        if pointer is None:
            if field == 'architecture_document':
                _fail('架构对齐缺少当前架构文档。')
            continue
        if not isinstance(pointer, dict):
            _fail('架构对齐来源指针格式无效。')
        doc = by_id.get(pointer.get('document_id'))
        if (not doc or doc.get('sha256') != pointer.get('sha256')
                or doc.get('kind') in forbidden or kinds is not None and doc.get('kind') not in kinds):
            _fail('架构对齐来源不属于固定的当前批准文档。')
        if not isinstance(doc.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', doc['sha256']):
            _fail('架构对齐来源哈希无效。')
        if field == 'approved_definition_document' and doc.get('kind') != 'architecture_scientific_definitions':
            _fail('架构对齐定义补充必须绑定批准定义文档。')
        if (not isinstance(doc.get('sections'), list)
                or any(not isinstance(section, dict) for section in doc['sections'])):
            _fail('架构对齐来源缺少冻结章节目录。')
        selected.append((field, doc))
    return selected


def context_contract(context):
    """Small frozen source identity manifest; no body text or approval inference."""
    documents = _primary_documents(context)
    binding = {key: context.get(key) for key in ('design_hash', 'experiment_plan_hash')}
    binding['architecture_document_sha256'] = documents[0][1]['sha256']
    _schema_check(binding, BINDING_SCHEMA, '架构对齐绑定')
    return {'version': VERSION, **binding, 'primary_sources': [
        {'source_role': field, 'document_id': doc['document_id'],
         'document_sha256': doc['sha256'], 'kind': doc.get('kind')}
        for field, doc in documents]}


def _retained_ranges(value, document):
    """Parse only explicit absolute section ranges, never prose dependencies."""
    if not isinstance(value, str) or not isinstance(document.get('sections'), list):
        _fail('架构对齐批准继承范围必须明确指定章节字符区间。')
    matches = list(re.finditer(r'(?<![\w])(?P<section>s\d+)\s*\[\s*(?P<start>\d+)\s*[,，]\s*(?P<end>\d+)\s*[)）]', value))
    if not matches:
        _fail('架构对齐批准继承缺少明确的章节字符区间。')
    retained = {}
    for match in matches:
        section_id = match['section']
        sections = [section for section in document.get('sections', [])
                    if isinstance(section, dict) and section.get('id') == section_id]
        left, right = int(match['start']), int(match['end'])
        if (len(sections) != 1 or type(sections[0].get('start')) is not int
                or type(sections[0].get('end')) is not int
                or not sections[0]['start'] <= left < right <= sections[0]['end']):
            _fail('架构对齐批准继承范围超出冻结来源章节。')
        spans = retained.setdefault(section_id, [])
        spans.append((left, right))
    # Adjacent explicit spans form a union; a gap never becomes authority.
    for section_id, spans in retained.items():
        merged = []
        for left, right in sorted(spans):
            if merged and left <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], right))
            else:
                merged.append((left, right))
        retained[section_id] = merged
    return retained


def _inherited_documents(context, design, primary):
    """Extend validation from a host-supplied, hash-bound approved design only."""
    if design is None:
        return {}
    from .implementation_review_routing import digest
    if not isinstance(design, dict) or digest(design) != context.get('design_hash'):
        _fail('架构对齐批准继承的设计身份与冻结输入不一致。')
    approved = dict(primary).get('approved_definition_document')
    if approved is None:
        return {}  # A scoped review has not authorized the definition source.
    resource_plan = design.get('resource_plan')
    binding = resource_plan.get('approved_definition_binding') if isinstance(resource_plan, dict) else None
    if binding is None:
        return {}
    if not isinstance(binding, dict):
        _fail('架构对齐批准定义绑定格式无效。')
    inheritance = binding.get('inheritance', [])
    if not isinstance(inheritance, list):
        _fail('架构对齐批准继承清单格式无效。')
    if not inheritance:
        return {}
    adoption = context.get('definition_adoption_id')
    if (binding.get('document_id') != approved['document_id']
            or binding.get('expected_sha256') != approved['sha256']
            or not isinstance(adoption, str) or not adoption
            or binding.get('definition_adoption_id') != adoption):
        _fail('架构对齐批准继承没有绑定当前批准定义和裁决身份。')
    from .architecture_dependency_catalog import KINDS
    indexed = {doc['document_id']: doc for doc in context['indexed_documents']}
    primary_ids = {doc['document_id'] for _, doc in primary}
    inherited = {}
    for item in inheritance:
        if not isinstance(item, dict):
            _fail('架构对齐批准继承来源格式无效。')
        document = indexed.get(item.get('document_id'))
        if (not document or document.get('kind') not in KINDS
                or document['document_id'] in primary_ids
                or not isinstance(item.get('expected_sha256'), str)
                or not re.fullmatch('[0-9a-f]{64}', item['expected_sha256'])
                or document.get('sha256') != item['expected_sha256']):
            _fail('架构对齐批准继承来源不属于冻结的同版本定义目录。')
        ranges = _retained_ranges(item.get('retained_ranges'), document)
        if document['document_id'] in inherited:
            _fail('架构对齐批准继承来源重复，范围身份不唯一。')
        inherited[document['document_id']] = {**document, '_alignment_retained_ranges': ranges}
    return inherited


def _sources(context, design=None):
    contract = context_contract(context)
    supplied = context.get('implementation_alignment_contract')
    if supplied is not None and supplied != contract:
        _fail('架构对齐来源清单与冻结输入不一致。')
    primary = _primary_documents(context)
    return {**{doc['document_id']: doc for _, doc in primary},
            **_inherited_documents(context, design, primary)}


def _fail_references(problems):
    from .engine import ResearchBlocked
    details = '；'.join(f"{row.get('requirement_id', '来源')} source_refs[{row['reference_index']}]: "
                       + row['reason'] for row in problems)
    error = ResearchBlocked('架构对齐来源引用无效：' + details)
    error.alignment_reference_problems = copy.deepcopy(problems)
    raise error


def _validate_refs(refs, sources):
    _schema_check(refs, SOURCE_REFS, '架构对齐来源范围')
    seen, problems = set(), []
    for index, ref in enumerate(refs):
        doc = sources.get(ref['document_id'])
        reason = None
        if not doc or ref['document_sha256'] != doc['sha256']:
            reason = '架构对齐引用了历史、非批准来源或错误版本。'
        else:
            sections = [s for s in doc['sections'] if s.get('id') == ref['section_id']]
            if len(sections) != 1:
                reason = '架构对齐来源章节缺失或不唯一。'
            else:
                start, end = sections[0].get('start'), sections[0].get('end')
                if (type(start) is not int or type(end) is not int or end < start
                        or ref['offset'] + ref['limit'] > end - start):
                    reason = '架构对齐来源范围越过固定原文章节。'
                elif '_alignment_retained_ranges' in doc and not any(
                        left <= start + ref['offset']
                        and start + ref['offset'] + ref['limit'] <= right
                        for left, right in doc['_alignment_retained_ranges'].get(ref['section_id'], [])):
                    reason = '架构对齐来源范围超出批准定义明确保留的继承区间。'
        identity = tuple(ref[key] for key in SOURCE_REF['required'])
        if not reason and identity in seen:
            reason = '架构对齐重复引用同一来源范围。'
        seen.add(identity)
        if reason:
            problems.append({'reference_index': index, 'reference': copy.deepcopy(ref), 'reason': reason})
    if problems:
        _fail_references(problems)


def acceptance_ids(context):
    ids = context.get('implementation_alignment_acceptance_ids')
    if ids is None:
        spec = context.get('implementation_spec')
        if isinstance(spec, dict):
            checks = spec.get('acceptance_checks', [])
            if not isinstance(checks, list) or any(not isinstance(row, dict) for row in checks):
                _fail('架构对齐验收项格式无效。')
            ids = [row.get('id') for row in checks]
    if ids is None:
        return None
    if (not isinstance(ids, list) or not ids or any(not isinstance(v, str) or not v.strip() for v in ids)
            or len(set(ids)) != len(ids)):
        _fail('架构对齐验收项身份无效或重复。')
    return ids


def required_review_ids(context, role=None):
    """A scoped issue closure covers that role's original obligations.

    All other review protocols retain their full independent coverage rule.
    Allowed source/code IDs remain the full frozen scope; only the minimum
    needed for this explicitly code-only closure is narrowed.
    """
    expected=acceptance_ids(context)
    scope=context.get('definition_execution_acceptance')
    if not scope:return expected
    from .definition_execution_followup import validate_acceptance_scope
    from . import code_repair_contract as repair
    validate_acceptance_scope(scope,context)
    if role not in repair.ROLES or context.get('code_repair_clarification',{}).get('version')!=1:
        _fail('定向代码验收缺少原角色和原问题范围。')
    policy=context.get(repair.FIELD,{})
    if policy.get('version')!=repair.VERSION or policy.get('spec_hash')!=context.get('spec_hash'):
        _fail('定向代码验收没有绑定原问题的同一批准版本。')
    own={row['requirement_id'] for row in policy.get('issues',[]) if row.get('role')==role}
    if not own:return expected
    if not expected or not own<=set(expected):
        _fail('定向代码验收引用了本轮范围之外的原问题。')
    return [key for key in expected if key in own]


def traceability_validation_details(spec, context):
    """Report ID defects only; reading, binding and scientific review stay separate."""
    if not enabled(context) or not isinstance(spec, dict):
        return []
    checks, rows = spec.get('acceptance_checks'), spec.get('architecture_traceability')
    if (not isinstance(checks, list) or not isinstance(rows, list)
            or any(not isinstance(row, dict) or not isinstance(row.get('id'), str) for row in checks)
            or any(not isinstance(row, dict) or not isinstance(row.get('requirement_id'), str) for row in rows)):
        return []  # Schema validation supplies the malformed-field diagnostics.
    expected = [row['id'] for row in checks]
    mapped = [row['requirement_id'] for row in rows]
    indices = {}
    for index, requirement_id in enumerate(mapped):
        indices.setdefault(requirement_id, []).append(index)
    duplicates = {key: positions for key, positions in indices.items() if len(positions) > 1}
    missing, extra = sorted(set(expected) - set(mapped)), sorted(set(mapped) - set(expected))
    if expected and not duplicates and not missing and not extra:
        return []
    return [{'kind': 'structure', 'field_path': ['architecture_traceability'],
        'validator': 'acceptance_id_coverage',
        'reason': '实现规范架构对应表必须逐项覆盖全部验收 ID，不能遗漏、重复或改名。',
        'duplicate_ids': list(duplicates), 'duplicate_indices': duplicates,
        'index_base': 0, 'missing_ids': missing, 'extra_ids': extra,
        'expected_count': len(expected), 'actual_count': len(mapped)}]


def validate_spec(spec, context, *, design=None):
    if not enabled(context):
        return
    if not isinstance(spec, dict):
        _fail('架构对齐实现规范格式无效。')
    rows = spec.get('architecture_traceability')
    _schema_check(rows, TRACEABILITY_SCHEMA, '实现规范架构对应表')
    acceptance_ids({'implementation_spec': spec})
    problems = traceability_validation_details(spec, context)
    if problems:
        from .engine import ResearchBlocked
        error = ResearchBlocked(problems[0]['reason'])
        error.implementation_validation_details = problems
        raise error
    sources = _sources(context, design)
    for row in rows:
        _validate_refs(row['source_refs'], sources)


def validate_review(value, context, *, delivery=False, design=None, role=None):
    from .engine import ResearchBlocked
    if not enabled(context):
        return
    if not isinstance(value, dict):
        _fail('架构对齐评审格式无效。')
    binding, findings = value.get('alignment_binding'), value.get('alignment_findings')
    _schema_check(binding, BINDING_SCHEMA, '架构对齐评审绑定')
    _schema_check(findings, FINDINGS_SCHEMA, '架构对齐评审结论')
    contract = context_contract(context)
    if binding != {key: contract[key] for key in BINDING_SCHEMA['required']}:
        _fail('架构对齐评审没有绑定当前设计、冻结计划与架构原文。')
    sources = _sources(context, design)
    ids = [row['requirement_id'] for row in findings]
    duplicate_problems = [{'kind': 'duplicate_requirement_id', 'requirement_id': requirement,
                          'finding_indices': [index for index, value in enumerate(ids) if value == requirement]}
                         for requirement in dict.fromkeys(ids) if ids.count(requirement) > 1]
    problems = []
    for index, finding in enumerate(findings):
        if value.get('approve') is True and finding['status'] != 'aligned':
            _fail('架构对齐仍有冲突或待核实项，不能批准。')
        try:
            _validate_refs(finding['source_refs'], sources)
        except ResearchBlocked as exc:
            invalid = getattr(exc, 'alignment_reference_problems', None)
            if not invalid:
                raise
            problems.extend({**row, 'finding_index': index, 'requirement_id': finding['requirement_id']}
                            for row in invalid)
    expected = required_review_ids(context,role)
    if (value.get('approve') is True and (delivery or context.get('review_unit') is None)
            and expected is not None and not set(expected) <= set(ids)):
        _fail('最终架构对齐评审必须覆盖全部实现验收 ID。')
    if problems:
        _fail_references(problems)
    if duplicate_problems:
        error = ResearchBlocked(FINDINGS_DUPLICATE)
        error.alignment_findings_format_problems = duplicate_problems
        raise error


def validate_findings_format_correction(value, original):
    """A reviewer may regroup duplicate IDs, never revise the original judgment.

    Preserve each source pointer and original condition verbatim. The host
    verifies the submitted correction; it never deduplicates or chooses a vote.
    Still-duplicated answers remain eligible for the shared bounded correction
    loop only when they retain the same information and strongest status.
    """
    _schema_check(value.get('alignment_findings'), FINDINGS_SCHEMA, '架构对齐格式修正')
    if ({key: item for key, item in value.items() if key != 'alignment_findings'}
            != {key: item for key, item in original.items() if key != 'alignment_findings'}):
        _fail('架构对齐格式修正改变了原判断、异议或非格式字段，未登记批准。')
    def groups(rows):
        result = {}
        for row in rows:
            result.setdefault(row['requirement_id'], []).append(row)
        return result
    before, after = groups(original['alignment_findings']), groups(value['alignment_findings'])
    if set(before) != set(after):
        _fail('架构对齐格式修正遗漏或增加了原验收 ID，未登记批准。')
    strength = {'aligned': 0, 'pending': 1, 'mismatch': 2}
    for requirement, prior in before.items():
        corrected = after[requirement]
        pointers = lambda rows: {tuple(ref[key] for key in SOURCE_REF['required'])
                                 for row in rows for ref in row['source_refs']}
        if pointers(prior) != pointers(corrected):
            _fail('架构对齐格式修正改变了原来源范围，未登记批准。')
        if max(strength[row['status']] for row in prior) != max(
                strength[row['status']] for row in corrected):
            _fail('架构对齐格式修正改变了原未解决状态，未登记批准。')
        original_text = [row['implementation_evidence'] for row in prior]
        corrected_text = [row['implementation_evidence'] for row in corrected]
        allowed_text = ['\n\n'.join(original_text)] if len(corrected) == 1 else original_text
        if corrected_text != allowed_text:
            _fail('架构对齐格式修正没有逐字保留原具体条件与实现依据，或添加了新判断，未登记批准。')
        if len(prior) == 1 and corrected != prior:
            _fail('架构对齐格式修正改变了未重复的原验收项，未登记批准。')
