"""Build one implementation author phase from explicit immutable inputs.

The full handoff remains the host's provenance and tool-authorization record.
Only a not-yet-frozen request gets this view; old prepared requests are replayed
unchanged. Directories and a compiled-view certificate grant no reading credit.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

from .implementation_review_routing import digest

PROTOCOL = 'implementation_phase_context_protocol'
CONTRACT = 'implementation_phase_context'
VERSION = 1
GUIDANCE = ('\n本次请求使用独立的实现文档阶段上下文。indexed_documents 只列当前批准来源、必要宿主合同和当前编写基线。'
    '决定性原文核对须绑定精确 SHA 与章节；已有作者上下文先复用，目录本身不代表已经读取。需追查批准来源的依赖时，'
    '使用 documents.list/search 按需定位原交接授权范围内的文档，不能继承历史候选或其他角色结论。'
    '修订只依据紧邻前版与本轮收齐的三方实际意见，逐项回应当前修改要求；异议身份保留为审核元数据。'
    '自己的工作笔记属于阅读连续性材料，不是新增证据或待阅读全文的来源。'
    '最新笔记已累计此前记录；按当前笔记目录恢复早期条目即可，不要逐版重读笔记历史。')
GUIDANCE += ('\n修订阶段优先处理本轮修改意见，复用作者上一版输出与阅读上下文，必要时对照批准架构。'
    'implementation_on_demand_documents 是可按需核对的当前提案、冻结计划、定义和宿主合同，'
    '不是每轮必须重新阅读全文的任务。修改涉及这些约束时，用精确版本和范围核对；'
    '不能因缩小默认目录而删除或改变已批准的科学条件。')
from .implementation_review_compaction import GUIDANCE as REVIEW_COMPACTION_GUIDANCE
GUIDANCE += REVIEW_COMPACTION_GUIDANCE
from .implementation_revision_memory import GUIDANCE as REVISION_MEMORY_GUIDANCE
GUIDANCE += REVISION_MEMORY_GUIDANCE
from .implementation_processed_context import GUIDANCE as PROCESSED_GUIDANCE
GUIDANCE += PROCESSED_GUIDANCE
GUIDANCE += ('\n历史成功写入的 archived_call 是宿主保存的调用摘要，完整参数保留在原交接账本；'
    '其 result 保留真实保存回执。旧替换正文已被后续版本取代，当前编辑以记忆中的最新稿和目录为准。'
    '摘要不是可执行调用，也不增加阅读、验证或批准记录。')


def _archive_saved_edits(context):
    """Bound old write payloads in new views, never mutate durable receipts.

    Keep the current write and every failed/partial write intact. A current
    version-bound memory and its matching save must exist before superseded
    successful payloads can leave the request. Save results remain exact.
    """
    memory = context.get('implementation_revision_memory', {})
    if memory.get('version') != 3:
        return
    from .implementation_revision_memory import validate
    validate(context)
    rows = context.get('tool_results', [])
    current = memory['freshness']['document_id']
    matching = [i for i, row in enumerate(rows)
        if isinstance(row, dict) and row.get('call', {}).get('name') == 'implementation.edit_document'
        and isinstance(row.get('result'), dict) and row['result'].get('saved') is True
        and row['result'].get('submission', {}).get('document_id') == current]
    if not matching:
        return
    for i, row in enumerate(rows[:max(matching)]):
        if not isinstance(row, dict):
            continue
        call, result = row.get('call', {}), row.get('result', {})
        if (call.get('name') != 'implementation.edit_document' or not isinstance(result, dict)
                or result.get('saved') is not True or result.get('error')
                or 'revision_progress' not in result
                or result['revision_progress'].get('failed_units') != []):
            continue
        args = call.get('arguments', {})
        archived = {'name': call['name'], 'arguments_sha256': digest(args),
            'receipt_sha256': digest(row), 'handoff_id': memory['freshness']['handoff_id'],
            'archive_collection': 'handoff_progress',
            'saved_index_ids': result['revision_progress'].get('saved_index_ids', []),
            'policy': 'Superseded successful write payload; original call and result remain in the archive.'}
        replacement = {**row, 'archived_call': archived}
        replacement.pop('call')
        if len(json.dumps(replacement)) < len(json.dumps(row)):
            rows[i] = replacement

_FIELDS = frozenset({
    'implementation_definition_repair_protocol', 'definition_repair', 'definition_review_feedback', 'definition_addition', 'definition_code_sources',
    'implementation_acceptance_author_protocol', 'implementation_acceptance_author_continuation',
    'acceptance_author_pending', 'acceptance_author_checkpoint', 'acceptance_author_saved_progress',
    'source_supplement_adoption', 'supplement_resolved_source_gaps',
    'acceptance_author_patch_manifest',
    'acceptance_author_save_retry',
    'implementation_author_self_check',
    'implementation_revision_memory',
    'implementation_acceptance_protocol', 'acceptance_contract', 'acceptance_budget', 'acceptance_issues',
    'objective', 'research_cycle', 'proposal_hash', 'design_hash', 'experiment_plan_hash',
    'document_index_protocol', 'indexed_document_context', 'source_tool_protocol',
    'document_input_protocol', 'document_read_window_protocol', 'document_read_budget', 'document_policy',
    'code_contract', 'format_contract_version', 'implementation_format_contract', 'execution_constraints',
    'implementation_indexed_review_protocol', 'implementation_document_authoring_protocol',
    'implementation_document_authoring', 'implementation_document_readability_protocol',
    'implementation_document_edit_protocol', 'implementation_document_repair_reads',
    'implementation_document_locate_protocol',
    'implementation_module_details_protocol',
    'implementation_document_validation_policy',
    'implementation_alignment_protocol', 'implementation_alignment_contract',
    'architecture_convergence_protocol', 'definition_adoption_id',
    'implementation_document_base', 'implementation_document_base_error',
    'implementation_document_preservation', 'implementation_document_completion_protocol',
    'implementation_document_delivery',
    'implementation_document_completion', 'predecessor_document_notes',
    'document_working_notes_protocol', 'document_working_notes_required', 'document_working_notes',
    '_purpose', '_output_token_limit', '_harness_control', '_tool_contract_version', 'current_date',
    'tool_results', 'document_read_window',
    'output_schema_repair',
    'implementation_author_revision_contract', 'implementation_author_revision_pending',
})
_PROJECTED = frozenset({
    PROTOCOL, CONTRACT, 'indexed_documents', 'architecture_document', 'approved_proposal_document',
    'frozen_experiment_plan_document', 'approved_definition_document', 'protocol_document',
    'implementation_capability_document', 'implementation_capability_evidence',
    'previous_specifications', 'previous_implementation_review', 'implementation_current_review_votes',
    'implementation_spec_progress_protocol', 'indexed_response_actions', 'implementation_revision_actions',
    'document_revision_objections', 'implementation_issue_response_protocol',
    'implementation_issue_response_contract', 'implementation_document_draft',
    'implementation_on_demand_documents',
    'implementation_review_compaction',
})


def _fail(message):
    from .engine import ResearchBlocked
    raise ResearchBlocked('实现阶段独立上下文：' + message)


def author(context, role='engineer', purpose=None):
    marker = context.get('implementation_indexed_review_protocol')
    return (role == 'engineer' and type(marker) is int and marker == 1
        and context.get('implementation_document_authoring_protocol') == 1
        and isinstance(context.get('implementation_document_authoring'), dict)
        and (purpose is None or purpose.startswith('implementation_spec:')))


def enabled(context):
    return type(context.get(PROTOCOL)) is int and context[PROTOCOL] == VERSION


def _revision(value, result, revision, *, source_certificate=None):
    """Current full opinions plus persistent objection identities, no old prose."""
    if not revision:
        result['previous_specifications'] = []
        return
    previous = [row for row in value.get('previous_specifications', []) if row.get('index') == revision - 1]
    if len(previous) != 1:
        _fail('当前修订必须绑定唯一紧邻前版，不能装配整条版本历史。')
    previous = previous[0]
    result['previous_specifications'] = [{key: copy.deepcopy(previous[key])
        for key in ('index', 'spec_hash', 'document') if key in previous}]
    if value.get('implementation_definition_repair_protocol') == 1:
        return  # Authenticated code findings replace the already-approved document's old votes.
    if value.get('implementation_acceptance_protocol') == 1:
        return  # The frozen adjudicated issue ledger replaces whole-round votes.
    votes = value.get('implementation_current_review_votes', previous.get('review_votes', []))
    if len(votes) != 3 or {vote.get('role') for vote in votes} != {'director', 'engineer', 'scout'}:
        _fail('修订输入必须保留本轮收齐的三方真实意见。')
    result['implementation_current_review_votes'] = [{key: copy.deepcopy(item)
        for key, item in vote.items() if key != 'reading_proof'} for vote in votes]
    actions = value.get('document_revision_objections', [])
    for name in ('implementation_spec_progress_protocol', 'indexed_response_actions',
                 'implementation_revision_actions', 'document_revision_objections',
                 'implementation_issue_response_protocol'):
        if name in value:
            result[name] = copy.deepcopy(value[name])
    previous_review = value.get('previous_implementation_review', {})
    current = {key: copy.deepcopy(previous_review[key])
        for key in ('index', 'spec_hash', 'document') if key in previous_review}
    current['objections'] = []
    for issue in previous_review.get('objections', []):
        metadata = {key: copy.deepcopy(issue[key]) for key in
            ('id', 'role', 'aliases', 'origin_id', 'origin_version', 'origin_refs',
             'continuation_of', 'disposition') if key in issue}
        metadata['milestone_checks'] = [{'id': item['id']} for item in issue.get('milestone_checks', [])]
        related = [item for item in actions if item.get('id') in {issue.get('id'), *issue.get('aliases', [])}]
        metadata['issue'] = related[0]['issue'] if related else '原异议身份；本轮没有新的修改要求。'
        metadata['fixes'] = [copy.deepcopy(fix) for item in related for fix in item.get('fixes', [])]
        current['objections'].append(metadata)
    result['previous_implementation_review'] = current
    contract = value.get('implementation_issue_response_contract')
    if isinstance(contract, dict):
        compact = copy.deepcopy(contract)
        # Only current actions need prose. Old origins stay exact identity
        # metadata so the host can validate bundles without sending old reviews.
        compact['obligations'] = [{key: copy.deepcopy(item[key]) for key in
            ('objection_id', 'role', 'origin_ref', 'origin_ref_sha256', 'disposition',
             'latest_action_sources', 'external_evidence_key') if key in item}
            for item in contract.get('obligations', [])]
        compact.pop('sha256', None)
        compact['sha256'] = digest(compact)
        result['implementation_issue_response_contract'] = compact
    from .implementation_review_compaction import project_revision
    result.update(project_revision(result, source_votes=votes, source_certificate=source_certificate))


def project_author(value):
    """Pure projection; never rewrite a frozen handoff or its original journal."""
    if not author(value):
        return copy.deepcopy(value)
    from . import implementation_review_compaction as reviews
    source_certificate = reviews.assert_projection(value)
    if source_certificate is not None:
        value = reviews.restore(value)
    from .implementation_alignment import _primary_documents
    binding = value['implementation_document_authoring']
    revision = binding.get('revision')
    if not isinstance(binding.get('workflow_id'), str) or type(revision) is not int or revision < 0:
        _fail('作者工作流和修订版本身份不完整。')
    result = {key: copy.deepcopy(value[key]) for key in _FIELDS if key in value}
    # A second projection may already separate navigation-only directories.
    # Reconstruct the exact approved pointer lookup without adding their text
    # or turning optional references into the default full-reading scope.
    available = [*value.get('indexed_documents', []), *value.get('implementation_on_demand_documents', [])]
    selected = _primary_documents({**value, 'indexed_documents': available})
    docs = []
    on_demand = []

    def add(document):
        if not isinstance(document, dict):
            _fail('必需来源缺少固定文档目录。')
        if 'text' in document or any('text' in section for section in document.get('sections', [])):
            _fail('固定目录不能带原文。')
        previous = next((item for item in docs if item.get('document_id') == document.get('document_id')), None)
        if previous is not None and previous != document:
            _fail('同一来源目录存在不同版本。')
        if previous is None:
            docs.append(copy.deepcopy(document))

    for field, document in selected:
        if not revision or field == 'architecture_document':
            add(document)
        else:
            on_demand.append(copy.deepcopy(document))
        result[field] = copy.deepcopy(document)
    capability = value.get('implementation_capability_document')
    if capability:
        if capability.get('kind') != 'host_implementation_contract':
            _fail('宿主能力必须是明确绑定的当前合同目录。')
        if revision:
            on_demand.append(copy.deepcopy(capability))
        else:
            add(capability)
        result['implementation_capability_document'] = copy.deepcopy(capability)
    facts = value.get('implementation_capability_evidence') or {}
    result['implementation_capability_evidence'] = {key: copy.deepcopy(facts[key]) for key in
        ('version', 'run_id', 'capability_fingerprint', 'experiment_plan_hash', 'provenance',
         'scientific_approval', 'execution_authorized', 'experiment_executed') if key in facts}
    _revision(value, result, revision, source_certificate=source_certificate)
    for previous in result['previous_specifications']:
        add(previous.get('document'))
    continuation = value.get('implementation_acceptance_author_continuation')
    if isinstance(continuation, dict):
        add(continuation['document'])
    supplement = value.get('source_supplement_adoption')
    if supplement is not None:
        # The authenticated author continuation explicitly added this source.
        # Preserve its candidate identity through repeated request projection;
        # a source pointer is not reading credit or implementation approval.
        if (not isinstance(supplement, dict)
                or supplement.get('scientific_approval') is not False
                or supplement.get('execution_authorized') is not False
                or supplement.get('document', {}).get('kind') != 'scientific_supplement_candidate'
                or supplement['document'] not in available):
            _fail('补充候选缺少作者续接绑定的精确来源目录。')
        add(supplement['document'])
    draft = value.get('implementation_document_draft')
    if isinstance(draft, dict):
        result['implementation_document_draft'] = copy.deepcopy(draft)
        if draft.get('document'):
            add(draft['document'])
    # Each immutable note version is cumulative. Tool authorization retains
    # the archived versions, but listing all of them makes the author reread
    # its own growing history and inflates the displayed source denominator.
    # The host has already checked the latest same-handoff record before this
    # projection. Keep that explicit directory only, without modifying notes,
    # receipts, frozen inputs or old prepared requests.
    latest_notes = (value.get('document_working_notes') or {}).get('document')
    if latest_notes is not None:
        if not isinstance(latest_notes, dict) or latest_notes.get('kind') != 'document_working_notes':
            _fail('当前工作笔记缺少明确的附属目录。')
        add(latest_notes)
    by_id = {document['document_id']: document for document in available}
    current_versions = {(document['document_id'], document['sha256']) for document in docs}
    # A dependency is shown only after this author explicitly expanded/read its
    # exact version. The host still authorizes the full original index and the
    # original tool ledger is never modified by this display projection.
    for item in value.get('tool_results', []):
        call = item.get('call', {})
        args = call.get('arguments', {})
        if call.get('name') in {'documents.read', 'documents.search', 'documents.list'}:
            directory = by_id.get(args.get('document_id'))
            if directory and directory['sha256'] == args.get('expected_sha256'):
                # Historical reads cannot select another draft or cumulative
                # note version. Only the explicit predecessor/current draft
                # and latest owned notes above belong to this author phase.
                if (directory.get('kind') in {'implementation_specification', 'document_working_notes'}
                        and (directory['document_id'], directory['sha256']) not in current_versions):
                    continue
                add(directory)
    result['indexed_documents'] = docs
    result['implementation_on_demand_documents'] = [document for index, document in enumerate(on_demand)
        if document['document_id'] not in {doc['document_id'] for doc in on_demand[:index]}]
    selected_ids = {document['document_id']: document['sha256'] for document in docs}
    for item in result.get('tool_results', []):
        if item.get('call', {}).get('name') == 'documents.list' and isinstance(item.get('result'), list):
            item['result'] = [directory for directory in item['result'] if isinstance(directory, dict)
                and selected_ids.get(directory.get('document_id')) == directory.get('sha256')]
    phase = {'version': VERSION, 'kind': 'author_save' if value.get('implementation_document_completion_protocol') == 1
             else ('author_revision' if revision else 'author_initial'),
        'binding': {key: copy.deepcopy(binding[key]) for key in ('workflow_id', 'revision')},
        'sources': [{'document_id': document['document_id'], 'sha256': document['sha256'],
                     'kind': document['kind']} for document in docs],
        'on_demand_sources': [{'document_id': document['document_id'], 'sha256': document['sha256'],
                              'kind': document['kind']} for document in result['implementation_on_demand_documents']],
        'reading_credit': False, 'scientific_approval': False,
        'source_response_contract_sha256': (value.get(CONTRACT) or {}).get('source_response_contract_sha256')
            or (value.get('implementation_issue_response_contract') or {}).get('sha256')}
    result[PROTOCOL] = VERSION
    result[CONTRACT] = {**phase, 'sha256': digest(phase)}
    _archive_saved_edits(result)
    from .implementation_processed_context import project as project_processed
    project_processed(result)
    assert_context(result)
    return result


def assert_context(context):
    if not enabled(context):
        return None
    from .implementation_revision_memory import validate as validate_memory
    validate_memory(context)
    contract = context.get(CONTRACT)
    if (not author(context) or not isinstance(contract, dict)
            or contract.get('version') != VERSION or contract.get('scientific_approval') is not False
            or contract.get('reading_credit') is not False
            or contract.get('sha256') != digest({key: value for key, value in contract.items() if key != 'sha256'})
            or set(context) - _FIELDS - _PROJECTED):
        _fail('请求不是独立的当前作者阶段，或混入了隐式历史。')
    binding = context['implementation_document_authoring']
    repair = context.get('output_schema_repair')
    if repair is not None:
        control = context.get('_harness_control', {})
        if (not isinstance(repair, dict)
                or set(repair) != {'request_id', 'transcript_id', 'original_text', 'original_sha256', 'diagnostic'}
                or any(not isinstance(repair.get(key), str) or not repair[key] for key in repair)
                or repair['request_id'] != control.get('json_syntax_correction_of')
                or not control.get('operation_id')
                or len(repair['original_text']) > 128_000
                or hashlib.sha256(repair['original_text'].encode()).hexdigest() != repair['original_sha256']):
            _fail('JSON 修正材料缺少原请求和原答复哈希绑定。')
    if contract.get('binding') != {key: binding.get(key) for key in ('workflow_id', 'revision')}:
        _fail('作者阶段绑定已经改变。')
    docs = context.get('indexed_documents', [])
    if (contract.get('sources') != [{'document_id': doc.get('document_id'), 'sha256': doc.get('sha256'),
            'kind': doc.get('kind')} for doc in docs]
            or len({doc.get('document_id') for doc in docs}) != len(docs)
            or any('text' in doc or any('text' in section for section in doc.get('sections', [])) for doc in docs)):
        _fail('当前来源目录丢失、换版或含内联正文。')
    # Old already-prepared phase requests lack this optional navigation field;
    # they keep their original contract rather than being rewritten on resume.
    if 'implementation_on_demand_documents' in context and 'on_demand_sources' not in contract:
        _fail('按需来源缺少阶段合同绑定。')
    if 'on_demand_sources' in contract:
        optional = context.get('implementation_on_demand_documents', [])
        if (contract['on_demand_sources'] != [{'document_id': doc.get('document_id'), 'sha256': doc.get('sha256'),
                'kind': doc.get('kind')} for doc in optional]
                or len({doc.get('document_id') for doc in optional}) != len(optional)
                or any('text' in doc or any('text' in section for section in doc.get('sections', [])) for doc in optional)):
            _fail('按需来源目录丢失、换版或含内联正文。')
    from .implementation_review_compaction import assert_projection
    assert_projection(context)
    return contract


def assert_input(context, instruction):
    """Check the actual compiled wire view, including lossless reference tables."""
    expected = assert_context(context)
    if expected is None:
        return None
    candidates = []
    decoder = json.JSONDecoder()
    for start in re.finditer(r'(?<!\\)\{\s*"(?:objective|task|context)"\s*:', instruction):
        try:
            active, _ = decoder.raw_decode(instruction, start.start())
        except (ValueError, TypeError):
            continue
        if not isinstance(active, dict) or not isinstance(active.get('context'), dict):
            continue
        tables = {'$text': active.get('shared_context_text', {}),
            '$catalog_record': active.get('shared_context_catalog_records', {}),
            '$record': active.get('shared_context_records', {})}

        def expand(value, seen=frozenset()):
            if isinstance(value, dict) and len(value) == 1 and next(iter(value)) in tables:
                kind, reference = next(iter(value.items()))
                table = tables[kind]
                if (not isinstance(reference, str) or reference not in table or (kind, reference) in seen):
                    _fail('编译输入的无损来源引用缺失或形成循环。')
                return expand(table[reference], seen | {(kind, reference)})
            if isinstance(value, dict):
                return {key: expand(item, seen) for key, item in value.items()}
            if isinstance(value, list):
                return [expand(item, seen) for item in value]
            return value

        source = expand(active['context'])
        if CONTRACT in source:
            candidates.append((source, active))
    if len(candidates) != 1:
        _fail('实际请求缺少唯一阶段合同。')
    source, active = candidates[0]
    assert_context(source)
    if any(source.get(key) != context.get(key) for key in context):
        _fail('编译输入改变了当前阶段的来源、基线、意见或实际阅读回执。')
    if active.get('recent_turns') or active.get('retrieved_memories'):
        _fail('实际请求混入角色历史或自动检索记忆。')
    return {'version': VERSION, 'contract_sha256': expected['sha256'],
        'instruction_sha256': hashlib.sha256(instruction.encode()).hexdigest(),
        'reading_credit': False, 'scientific_approval': False}
