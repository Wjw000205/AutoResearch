"""New-generation, role-isolated reviews over immutable document indices.

Indices authenticate scope. Only this handoff's original documents.read ledger
authenticates reading; inline text and historical approval grant no coverage.
"""
from __future__ import annotations

import copy
import json
import re

from .architecture_validation import operation
from .implementation_review_routing import digest

VERSION = 1
PROTOCOL = 'implementation_indexed_review_protocol'
FIELD = 'implementation_indexed_review'
CONTRACT = 'implementation_indexed_review_contract'
REPLIES = 'implementation_review_response_documents'
RESPONSE_KIND = 'implementation_review_response'
SUFFIX = ':indexed-review-v1'
ROLES = ('director', 'engineer', 'scout')
PAGE_CHARS = 4000
TOOL_OVERHEAD = 8
READ_PENDING = '索引全文审核尚未完成实际读取，请继续同版本 documents.read'
_HASH = re.compile(r'^[0-9a-f]{64}$')
_FORBIDDEN = frozenset({
    'implementation_alignment_full_text_pack', 'implementation_whole_alignment_protocol',
    'implementation_whole_alignment', 'implementation_spec', 'previous_implementation_spec',
    'previous_implementation_document', 'implementation_review_source_scope',
    'section_reviews', 'review_unit', 'review_units', 'implementation_review_batch',
    'implementation_review_batch_binding', 'implementation_incremental_review_scope',
    'implementation_incremental_target', 'implementation_read_recovery',
    'implementation_review_followup', 'prior_scope_review', 'legacy_alignment_final_votes',
    'previous_specifications', 'previous_specification_count', 'implementation_history_snapshot',
    'implementation_issue_index', 'implementation_document_draft', 'implementation_author_scope',
    'indexed_response_actions',
    'previous_specifications', 'implementation_history_snapshot', 'implementation_issue_index',
    'implementation_document_draft', 'implementation_author_scope', 'indexed_response_actions',
})

GUIDANCE = '''
本轮首次输入仅包含批准架构、当前实现规范和本角色工程师回复文档的固定目录，不携带全文。
必须自主按目录使用 documents.read，核对 expected_sha256，按每章 next_offset 读完全部正文。
每页优先读取4000至6000字符；按需要一次提交多章或多页读取，重复已覆盖范围不增加工具预算。
tool_results中的document_read_reference保留已读document_id/SHA/section/字符范围；正文退出窗口后仍是本交接已读页。
按目录与这些范围查找未覆盖章节及next_offset继续补读，只有核对具体原文时才按reopen重读已覆盖页。
documents.list/search、目录、摘要、宿主内联文本和旧票都不是实际全文阅读。
本轮只审核当前实现规范，不读取或继承上一版规范、旧章节通过票或其他角色的投票。
第二轮及以后还必须实际读完本角色的全部工程师回复文档，逐项独立核对原异议和当前修订。
首轮完整审核一次列出全部实质阻塞；后续按冻结 convergence 范围验收原问题，fixes 给出具体行动及验证；fix_locations 为每条 fix
列当前SPEC的 document_id/document_sha256/section_id/全文绝对start/end和需要改动的field。
只有 document_revision 请求改写当前规范；external_dependency 定位现行相关合同、说明真实恢复条件，
review_incomplete 定位必须补读的范围。不得把外部依赖或尚未读取误转为改写科学条件。
正文未实际读完时继续使用 documents.read，不提交最终否决票；approve=false必须有具体issues及逐项fixes。
不要把可选优化、命名偏好或等价工程选择当成实质阻塞。保留原异议身份和未完成证据状态。
只有全文已实际读完、架构逐项对齐且所有实质问题解决，才可 approve=true；issues/fixes/fix_locations均为空。
作者回复、文档保存、阅读完成和三方投票不是代码实现、科研执行或科学成功。
'''


def _fail(message):
    from .engine import ResearchBlocked
    raise ResearchBlocked('实现文档索引审核：' + message)


def _version(value):
    return type(value) is int and value == VERSION


def enabled(engine, run_id, state, row=None):
    # A later config edit must never retrofit a paid legacy generation.
    return _version(state.get(PROTOCOL)) or isinstance(row, dict) and _version(row.get(PROTOCOL))


def binding(state, row):
    return {'workflow_id': state['id'], 'revision': row['index'],
        'proposal_hash': state['proposal_hash'], 'spec_hash': row['spec_hash'],
        'document_id': row['document']['document_id'], 'document_sha256': row['document']['sha256']}


def purpose(state, row):
    return (f'implementation_document_review:{digest(state["id"])}:{row["spec_hash"]}'
            f':round:{row["index"]}' + SUFFIX)


def _directory(engine, run_id, value, kind):
    from . import document_index
    if not isinstance(value, dict):
        _fail('固定目录缺少文档身份。')
    try:
        actual = document_index.directory(engine, run_id, value.get('document_id'), value.get('sha256'))
    except (ValueError, OSError) as exc:
        _fail('固定目录版本或原始产物校验失败：' + str(exc))
    if actual.get('kind') != kind or actual.get('artifact_id') != value.get('artifact_id'):
        _fail('固定目录种类或产物与实际来源不一致。')
    # Return the canonical metadata, never arbitrary caller-supplied text.
    return actual


def _response_directory(engine, run_id, state, row, role, pointer):
    directory = _directory(engine, run_id, pointer, RESPONSE_KIND)
    if (pointer.get('response_role') != role
            or pointer.get('target_document_sha256') != row['document']['sha256']
            or not isinstance(pointer.get('previous_review_sha256'), str)
            or not _HASH.fullmatch(pointer['previous_review_sha256'])
            or pointer.get('target_spec_hash', row['spec_hash']) != row['spec_hash']):
        _fail('工程师回复目录没有绑定本角色原审查和当前规范。')
    previous = [entry for entry in state.get('specifications', [])
                if entry.get('index') == row['index'] - 1]
    if len(previous) != 1:
        _fail('工程师回复缺少唯一前一轮审查来源。')
    old = previous[0]
    prior_votes = [vote for vote in old.get('review_votes', []) if vote.get('role') == role]
    if (len(prior_votes) != 1 or pointer['previous_review_sha256'] != digest(prior_votes[0])
            or pointer.get('previous_review_handoff_id', prior_votes[0].get('handoff_id')) != prior_votes[0].get('handoff_id')):
        _fail('工程师回复没有绑定本角色紧邻上一轮的完整真实意见。')
    previous_vote = prior_votes[0]
    previous_result = {key: value for key, value in previous_vote.items()
                       if key not in {'role', 'handoff_id', 'reading_proof'}}
    previous_context = engine.store.get('handoff_inputs', previous_vote.get('handoff_id'), {}).get('context', {})
    _assignment(engine, run_id, state, old, role, previous_vote.get('handoff_id'), previous_result, previous_context)
    if (pointer.get('previous_spec_hash', old['spec_hash']) != old['spec_hash']
            or pointer.get('previous_document_sha256', old['document']['sha256']) != old['document']['sha256']):
        _fail('工程师回复引用了不同的旧审查版本。')
    result = {**directory, **{key: copy.deepcopy(pointer[key]) for key in (
        'response_role', 'previous_review_sha256', 'target_document_sha256', 'target_spec_hash',
        'previous_spec_hash', 'previous_document_sha256', 'previous_review_handoff_id',
        'authoring_handoff_id', 'issue_response_receipt_sha256', 'receipt_sha256', 'response_bundle_sha256')
        if key in pointer}}
    return result


def _response_contract(value):
    """Keep identities needed by the existing confirmation gate, not reply prose."""
    from . import implementation_issue_responses as responses
    original = responses._contract(value)
    fields = ('origin_ref', 'origin_ref_sha256', 'role', 'objection_id', 'disposition',
              'response_sha256', 'external_evidence_key')
    result = {key: copy.deepcopy(original[key]) for key in
        ('version', 'workflow_id', 'revision', 'receipt_sha256', 'scientific_approval') if key in original}
    for name in ('requirements', 'obligations'):
        result[name] = []
        for entry in original[name]:
            item = {key: copy.deepcopy(entry[key]) for key in fields if key in entry}
            if name == 'requirements':
                reply = entry.get('author_response')
                item['author_response'] = ({key: copy.deepcopy(reply[key]) for key in
                    ('response_id', 'response_sha256') if key in reply} if isinstance(reply, dict) and reply else None)
            result[name].append(item)
    result['sha256'] = digest(result)
    return result


def _tool_budget(documents):
    pages = sum((section['end'] - section['start'] + PAGE_CHARS - 1) // PAGE_CHARS
                for doc in documents for section in doc['sections'])
    return {'page_chars': PAGE_CHARS, 'required_pages': pages, 'overhead_turns': TOOL_OVERHEAD,
            'max_turns': pages + TOOL_OVERHEAD}


def tool_turn_limit(context, progress):
    """Earn bounded turns only through new ranges of current successful reads."""
    if not _version(context.get(PROTOCOL)) or not isinstance(context.get(CONTRACT), dict):
        return None
    docs = context.get('indexed_documents', [])
    budget = context[CONTRACT].get('tool_turn_budget')
    if budget != _tool_budget(docs):
        _fail('冻结的全文工具预算与固定目录范围不一致。')
    documents = {doc['document_id']: doc for doc in docs}
    covered, earned = {}, 0
    for receipt in progress.get('tool_results', []):
        if not isinstance(receipt, dict) or receipt.get('predecessor_handoff_id') is not None:
            continue
        call, page = receipt.get('call', {}), receipt.get('result', {})
        if (not isinstance(call, dict) or call.get('name') != 'documents.read'
                or not isinstance(page, dict) or page.get('error') or not isinstance(page.get('text'), str)):
            continue
        args = call.get('arguments', {})
        if not isinstance(args, dict):
            continue
        doc = documents.get(args.get('document_id'))
        section = next((item for item in (doc or {}).get('sections', [])
                        if item['id'] == args.get('section_id')), None)
        start, end = page.get('char_start'), page.get('char_end')
        offset, limit = args.get('offset', 0), args.get('limit', 4000)
        if (not doc or not section or type(start) is not int or type(end) is not int
                or type(offset) is not int or type(limit) is not int or not 1 <= limit <= 6000
                or args.get('expected_sha256') != doc['sha256'] or page.get('sha256') != doc['sha256']
                or page.get('document_id') != doc['document_id'] or page.get('section_id') != section['id']
                or page.get('provenance') != 'exact_document_section'
                or not section['start'] <= start < end <= section['end']
                or start != section['start'] + offset or end - start != len(page['text']) or end > start + limit):
            continue
        key = (doc['document_id'], section['id'])
        prior = covered.get(key, [])
        overlap = sum(max(0, min(end, b) - max(start, a)) for a, b in prior)
        if overlap == end - start:
            continue
        earned += 1
        merged = []
        for a, b in sorted([*prior, (start, end)]):
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        covered[key] = merged
    return min(budget['max_turns'], TOOL_OVERHEAD + earned)


@operation
def frozen_context(engine, run_id, state, row, context, *, role, response_documents=()):
    """Prepare an index-only input once, before its immutable handoff exists."""
    from . import implementation_alignment as alignment, implementation_issue_responses as responses
    from . import implementation_review_convergence as convergence
    if (not enabled(engine, run_id, state, row) or role not in ROLES
            or state.get('run_id') != run_id or type(row.get('index')) is not int or row['index'] < 0
            or row.get('spec_hash') != digest(row.get('spec'))):
        _fail('索引审核没有绑定新generation的当前完整规范。')
    architecture = _directory(engine, run_id, context.get('architecture_document'), 'architecture_design')
    current = _directory(engine, run_id, row.get('document'), 'implementation_specification')
    if not isinstance(response_documents, (list, tuple)):
        _fail('工程师回复目录必须是固定目录数组。')
    own = [_response_directory(engine, run_id, state, row, role, pointer)
           for pointer in response_documents if isinstance(pointer, dict) and pointer.get('response_role') == role]
    if (row['index'] == 0 and own) or (row['index'] > 0 and not own):
        _fail('初轮不得带旧回复；第二轮起必须提供本角色工程师回复目录。')
    docs = [architecture, current, *own]
    if len({doc['document_id'] for doc in docs}) != len(docs):
        _fail('必读目录存在重复或角色回复混入当前规范。')
    keep = ('research_cycle', 'design_hash', 'experiment_plan_hash', 'implementation_format_contract',
            'implementation_review_routing_protocol', 'implementation_spec_progress_protocol')
    value = {key: copy.deepcopy(context[key]) for key in keep if key in context}
    value.update({PROTOCOL: VERSION, 'proposal_hash': state['proposal_hash'], 'spec_hash': row['spec_hash'],
        'document_sha256': current['sha256'], 'architecture_document': architecture,
        'implementation_document': current, 'indexed_documents': docs, REPLIES: own,
        'document_input_protocol': 2, 'document_index_protocol': 1, 'indexed_document_context': True, 'indexed_document_review': True,
        'document_read_window_protocol': 1, 'document_read_budget': {'prompt_chars': 12000, 'page_chars': 6000}})
    capability = context.get('implementation_capability_evidence') or {}
    value['implementation_capability_evidence'] = {'capability_fingerprint': capability.get('capability_fingerprint')}
    value['execution_constraints'] = {key: copy.deepcopy((context.get('execution_constraints') or {}).get(key))
                                    for key in ('max_trials_per_cycle', 'max_research_cycles', 'per_trial_seconds')}
    if context.get(responses.PROTOCOL) == responses.VERSION:
        value[responses.PROTOCOL] = responses.VERSION
        value[responses.CONTRACT] = _response_contract(context)
    old = (context.get('previous_implementation_review') or {}).get('objections', [])
    value['previous_implementation_review'] = {'objections': [{key: copy.deepcopy(item[key]) for key in
        ('id', 'role', 'aliases', 'origin_refs', 'milestone_checks') if key in item}
        for item in old if item.get('role') == role]}
    # Routing needs the original milestone IDs, never the previous fix prose.
    for item in value['previous_implementation_review']['objections']:
        item.setdefault('fixes', [])
        if 'milestone_checks' in item:
            item['milestone_checks'] = [{'id': milestone['id']} for milestone in item['milestone_checks']]
    value = alignment.attach(value, row['spec'])
    contract = {'version': VERSION, 'run_id': run_id, 'binding': binding(state, row), 'role': role,
        'required_documents': [{'document_id': doc['document_id'], 'sha256': doc['sha256'],
            'kind': doc['kind']} for doc in docs], 'scientific_approval': False,
        'reading_provenance': 'same_handoff_documents_read_only', 'tool_turn_budget': _tool_budget(docs),
        convergence.FIELD: convergence.freeze(state, row, role, value, own)}
    value[CONTRACT] = {**contract, 'sha256': digest(contract)}
    assert_context(state, row, value, role=role)
    return value


def assert_context(state, row, context, *, role):
    """No inline/old SPEC or inherited chapter evidence is admissible."""
    contract = context.get(CONTRACT)
    if (context.get(PROTOCOL) != VERSION or type(context.get(PROTOCOL)) is not int
            or not isinstance(contract, dict) or contract.get('version') != VERSION
            or contract.get('binding') != binding(state, row) or contract.get('role') != role
            or contract.get('scientific_approval') is not False
            or contract.get('reading_provenance') != 'same_handoff_documents_read_only'
            or contract.get('sha256') != digest({key: value for key, value in contract.items() if key != 'sha256'})
            or any(key in context for key in _FORBIDDEN)):
        _fail('冻结输入包含内联全文、旧规范、继承票或不同版本身份。')
    docs = context.get('indexed_documents')
    if (not isinstance(docs, list) or not docs or any(not isinstance(doc, dict) for doc in docs)
            or contract.get('required_documents') != [{'document_id': doc.get('document_id'),
                'sha256': doc.get('sha256'), 'kind': doc.get('kind')} for doc in docs]
            or len({doc.get('document_id') for doc in docs}) != len(docs)
            or any('text' in doc or any('text' in section for section in doc.get('sections', [])) for doc in docs)):
        _fail('冻结目录必须唯一且只有元数据，不能内联原文。')
    if contract.get('tool_turn_budget') != _tool_budget(docs):
        _fail('冻结的全文工具预算与固定目录范围不一致。')
    current = context.get('implementation_document') or {}
    if (context.get('proposal_hash') != state['proposal_hash'] or context.get('spec_hash') != row['spec_hash']
            or context.get('document_sha256') != row['document']['sha256']
            or current.get('document_id') != row['document']['document_id']
            or current.get('sha256') != row['document']['sha256']
            or len([doc for doc in docs if doc.get('kind') == 'implementation_specification']) != 1
            or current not in docs or (context.get('architecture_document') or {}) not in docs
            or any(doc.get('kind') not in {'architecture_design', 'implementation_specification', RESPONSE_KIND} for doc in docs)):
        _fail('本轮目录必须正是批准架构、唯一当前规范与本角色回复。')
    own = context.get(REPLIES, [])
    if (not isinstance(own, list) or (row['index'] > 0 and not own)
            or (row['index'] == 0 and own)
            or [doc for doc in docs if doc.get('kind') == RESPONSE_KIND] != own
            or any(doc.get('response_role') != role or doc.get('target_document_sha256') != current['sha256']
                   for doc in own)):
        _fail('第二轮起必须绑定当前规范和本角色完整回复目录。')
    from .implementation_review_convergence import assert_context as assert_convergence
    assert_convergence(state, row, context)
    return contract


def assert_input(context, instruction, *, prepared=None):
    """Preserve the frozen directory in the compiled input, without inline credit.

    Authors may carry the generation flag to preserve their recovery contract;
    only an actual reviewer directory contract opts into this dispatch check.
    """
    if CONTRACT not in context:
        return None
    expected = context[CONTRACT]
    bound = expected.get('binding', {}) if isinstance(expected, dict) else {}
    state = {'id': bound.get('workflow_id'), 'proposal_hash': bound.get('proposal_hash')}
    row = {'index': bound.get('revision'), 'spec_hash': bound.get('spec_hash'),
        'document': {'document_id': bound.get('document_id'), 'sha256': bound.get('document_sha256')}}
    role = expected.get('role') if isinstance(expected, dict) else None
    assert_context(state, row, context, role=role)
    if not isinstance(instruction, str):
        _fail('实际模型输入缺失。')
    decoder, candidates = json.JSONDecoder(), []
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
                if (not isinstance(reference, str) or not isinstance(table, dict) or reference not in table
                        or (kind, reference) in seen):
                    _fail('实际索引输入的无损引用缺失或形成循环。')
                return expand(table[reference], seen | {(kind, reference)})
            if isinstance(value, dict):
                return {key: expand(item, seen) for key, item in value.items()}
            if isinstance(value, list):
                return [expand(item, seen) for item in value]
            return value
        source = expand(active['context'])
        if CONTRACT in source:
            candidates.append((source, expand(active.get('task'))))
    if len(candidates) != 1:
        _fail('实际模型输入缺少唯一冻结索引合同。')
    source, task = candidates[0]
    assert_context(state, row, source, role=role)
    for key in (CONTRACT, REPLIES, PROTOCOL, 'indexed_documents', 'architecture_document',
            'implementation_document', 'implementation_alignment_contract',
            'implementation_alignment_acceptance_ids', 'implementation_issue_response_contract',
            'previous_implementation_review', '_purpose', 'document_input_protocol',
            'document_read_window_protocol', 'document_read_budget', 'document_read_window', 'tool_results'):
        if source.get(key) != context.get(key):
            _fail('实际模型输入遗漏或改变当前固定目录、原异议或回复身份。')
    if prepared is not None and task != prepared['task']:
        _fail('实际索引审核输入改变了独立审核任务。')
    import hashlib
    return {'version': VERSION, 'contract_sha256': expected['sha256'],
        'instruction_sha256': hashlib.sha256(instruction.encode('utf-8')).hexdigest(),
        'provenance': 'exact_compiled_document_index', 'scientific_approval': False}


def review_schema(base, context=None):
    from . import implementation_alignment as alignment
    result = alignment.review_schema(base)
    result['properties']['document_sha256'] = {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}
    text = {'type': 'string', 'minLength': 1, 'pattern': r'\S'}
    fields = {'fix_index': {'type': 'integer', 'minimum': 0}, 'document_id': text,
        'document_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}, 'section_id': text,
        'start': {'type': 'integer', 'minimum': 0}, 'end': {'type': 'integer', 'minimum': 1}, 'field': text}
    result['properties']['fix_locations'] = {'type': 'array', 'maxItems': 256, 'items': {
        'type': 'object', 'properties': fields, 'required': list(fields), 'additionalProperties': False}}
    for key in ('document_sha256', 'fix_locations'):
        if key not in result['required']:
            result['required'].append(key)
    from .implementation_review_convergence import schema as convergence_schema
    return convergence_schema(result, context) if context is not None else result


def _fix_locations(result, context):
    fixes, locations = result.get('fixes'), result.get('fix_locations')
    current = context['implementation_document']
    sections = {section['id']: section for section in current['sections']}
    if (not isinstance(fixes, list) or not isinstance(locations, list)
            or {entry.get('fix_index') for entry in locations if isinstance(entry, dict)} != set(range(len(fixes)))):
        _fail('每条实质修改必须给出当前规范中的精确位置。')
    for fix in fixes:
        if any(not isinstance(fix.get(key), str) or not fix[key].strip()
               for key in ('location', 'change', 'verification')):
            _fail('实质修改缺少详细位置、具体改法或验证步骤。')
    for entry in locations:
        section = sections.get(entry.get('section_id'))
        if (entry.get('document_id') != current['document_id'] or entry.get('document_sha256') != current['sha256']
                or section is None or type(entry.get('start')) is not int or type(entry.get('end')) is not int
                or not section['start'] <= entry['start'] < entry['end'] <= section['end']
                or not isinstance(entry.get('field'), str) or not entry['field'].strip()):
            _fail('修改位置引用了旧规范、错误SHA或不存在的当前范围。')


def _assignment(engine, run_id, state, row, role, hid, result, context, *, candidate=False):
    from .engine import Harness
    assignment = engine.store.get('handoffs', hid, {})
    frozen = engine.store.get('handoff_inputs', hid, {})
    payload = {key: frozen.get(key) for key in ('instruction', 'context', 'schema')}
    if (not enabled(engine, run_id, state, row) or role not in ROLES
            or assignment.get('id') != hid or assignment.get('run_id') != run_id or assignment.get('to_role') != role
            or frozen.get('id') != hid or frozen.get('run_id') != run_id
            or assignment.get('purpose') != purpose(state, row)
            or assignment.get('input_hash') != digest(payload) or digest(frozen.get('context')) != digest(context)
            or Harness.decision_key(run_id, role, purpose(state, row), **payload) != hid):
        _fail('审查缺少本角色同版本的新冻结交接，旧票不能通过。')
    contract = assert_context(state, row, context, role=role)
    if contract['run_id'] != run_id:
        _fail('冻结索引审核合同不属于当前任务。')
    progress = engine.store.get('handoff_progress', hid)
    # A first candidate may arrive before any tool has run. It is unread,
    # rather than a corrupt ledger; _reading supplies the precise missing pages.
    if candidate and progress is None:
        progress = {}
    elif not isinstance(progress, dict) or progress.get('id') != hid or progress.get('run_id') != run_id:
        _fail('实际工具读取账本不属于当前交接。')
    if not candidate:
        decision = engine.store.get('decisions', hid, {})
        if (assignment.get('status') != 'completed' or assignment.get('output_hash') != digest(result)
                or decision.get('id') != hid or decision.get('run_id') != run_id or decision.get('role') != role
                or decision.get('purpose') != purpose(state, row) or digest(decision.get('result')) != digest(result)):
            _fail('独立意见没有当前角色的完整原始交付。')
    return frozen, progress


def _reading(engine, run_id, context, hid):
    from .implementation_reading import coverage
    reports = []
    for doc in context['indexed_documents']:
        actual = _directory(engine, run_id, doc, doc['kind'])
        if any(actual.get(key) != doc.get(key) for key in ('sections', 'total_chars', 'title')):
            _fail('冻结目录范围与实际同SHA文档不一致。')
        required = [{'result': {'section_id': section['id'], 'char_start': section['start'],
            'char_end': section['end']}} for section in actual['sections']]
        report = coverage(engine, run_id, doc, required, [hid])
        if not report['complete']:
            from .engine import ResearchBlocked
            from .implementation_reading import READ_PENDING as alignment_pending
            missing = [{'document_id': doc['document_id'], 'sha256': doc['sha256'], **span,
                'offset': span['start'] - next(section['start'] for section in actual['sections']
                    if section['id'] == span['section_id']), 'limit': min(6000, span['end'] - span['start'])}
                for span in report['missing_ranges']]
            error = ResearchBlocked(alignment_pending + '；' + READ_PENDING + '：' + doc['document_id'])
            error.indexed_review_missing_document = doc['document_id']
            error.indexed_review_missing_ranges = missing
            raise error
        reports.append({'document_id': doc['document_id'], 'sha256': doc['sha256'],
            'covered_chars': actual['total_chars'], 'complete': True})
    return {'handoff_id': hid, 'documents': reports, 'reading_provenance': 'same_handoff_documents_read_only',
            'scientific_approval': False}


@operation
def validate_vote(engine, run_id, state, row, role, hid, result, context):
    from .intelligence import parse_json
    from . import implementation_documents as documents, implementation_alignment_evidence as evidence
    from . import implementation_review_routing as routing
    frozen, _ = _assignment(engine, run_id, state, row, role, hid, result, context)
    parse_json(json.dumps(result, ensure_ascii=False), frozen['schema'])
    documents._validate_result(result, context)
    _fix_locations(result, context)
    reading = _reading(engine, run_id, context, hid)
    routing.validate(result, context, role)
    from .implementation_review_convergence import validate as validate_convergence
    validate_convergence(result, context, role)
    evidence.validate_vote(engine, run_id, role, hid, result, context)
    return reading


@operation
def validate_candidate(engine, run_id, state, row, role, hid, result, context):
    """The same full-read gate may run before a candidate decision is sealed."""
    from .intelligence import parse_json
    from . import implementation_documents as documents, implementation_alignment_evidence as evidence
    from . import implementation_review_routing as routing
    frozen, _ = _assignment(engine, run_id, state, row, role, hid, result, context, candidate=True)
    parse_json(json.dumps(result, ensure_ascii=False), frozen['schema'])
    documents._validate_result(result, context)
    _fix_locations(result, context)
    reading = _reading(engine, run_id, context, hid)
    routing.validate(result, context, role)
    from .implementation_review_convergence import validate as validate_convergence
    validate_convergence(result, context, role)
    evidence.validate_candidate(engine, run_id, role, hid, result, context)
    return reading


@operation
def validate_review(engine, run_id, state, row):
    votes = row.get('review_votes')
    if (not enabled(engine, run_id, state, row) or not isinstance(votes, list) or len(votes) != 3
            or {vote.get('role') for vote in votes if isinstance(vote, dict)} != set(ROLES)
            or len({vote.get('handoff_id') for vote in votes}) != 3
            or any(vote.get('approve') is not True or vote.get('issues') or vote.get('fixes') for vote in votes)):
        _fail('必须取得本轮三方完整独立通过票，旧票和作者保存不能授权代码。')
    proofs = []
    for vote in votes:
        hid = vote['handoff_id']
        context = engine.store.get('handoff_inputs', hid, {}).get('context', {})
        result = {key: value for key, value in vote.items() if key not in {'role', 'handoff_id', 'reading_proof'}}
        proof = validate_vote(engine, run_id, state, row, vote['role'], hid, result, context)
        if 'reading_proof' in vote and vote['reading_proof'] != proof:
            _fail('已保存的本轮全文阅读证明已变化。')
        proofs.append(proof)
    return {'version': VERSION, 'binding': binding(state, row), 'votes': proofs, 'scientific_approval': False}
