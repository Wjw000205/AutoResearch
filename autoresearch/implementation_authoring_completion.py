"""Bounded document saving after reading, with immutable predecessor receipts.

Reading exhaustion cannot produce a submission before the author writes a draft.
Only one save successor is allowed; ordinary authorship and audit gates still apply.
"""
from __future__ import annotations

import copy
import hashlib
import json

from . import implementation_authoring as author, document_index
from .storage import now

VERSION = 1
LIMIT = 4
TASK = ('\n实现文档保存阶段：原交接的读取已经结束，但没有保存实现 Markdown。'
    '无基线时用 implementation.write_document 首次创建；已有基线时用 implementation.edit_document；'
    '依据冻结的最新前版与评议，'
    '优先把 document_revision 必改项定向落实到对应规范字段、验收要求和实现映射，'
    '不要只把修改意见转述成 limitations。已有基线时绑定 '
    'implementation_document_base.spec_hash 作为 base_spec_hash 做定向 patch，'
    '由宿主原样保留 patch 未修改的字段。edit 同时绑定基线 document_id、document_sha256 '
    '（作为 expected_sha256）和 spec_hash（作为 base_spec_hash）。'
    '本请求尚无已保存草稿时，该基线是上一已审版本；已有草稿时绑定当前草稿。'
    '无基线首次创建须提供完整 specification 并省略 expected_sha256；'
    '未批准草稿的错误内容和作者自增验收 ID 可纠正、删除或合并；'
    '本版 ID 须唯一，映射与来源对齐须完整覆盖，已批准科学来源与冻结计划保持不变。'
    '决定性证据的必要首次读取应在原编写阶段完成；本保存阶段可定位当前基线并修改文档，'
    '成功保存后可读取或搜索自己的当前草稿；保存校验失败时，仅按宿主授权的字段范围恢复基线原文，'
    '不能重新开启所有原输入的读取循环。'
    'predecessor_document_notes 是你原先保存的带原文锚点观察，继承回执保留实际查看过的范围。'
    '引用冻结文档的精确版本与条款，不另立科学定义、不补造参数或批准；'
    '仅依据已有真实证据落实修订。若仍缺决定性证据，应精确保留未解决项与缺少的证据，'
    '不得用新增 limitations 或保存错误说明冒充问题已修复、已有实质进展或可以通过审计。'
    '写入成功后可以检查自己的草稿，最终仅提交工具实际回传的 submission。'
    '最多四轮保存/检查，保存本身不代表审计通过、代码已实现或实验获准执行。')


class SaveRequired(RuntimeError):
    """An author needs a real write, rather than an impossible final pointer."""


def enabled(context):
    return context.get('implementation_document_completion_protocol') == VERSION


def _terminal_failure(h, run_id, request):
    """Verify a settled, completed native refusal; uncertain work stays blocked."""
    from .codex_reasoning import key_for, TRANSPORT_SCHEMA
    from .json_format_recovery import completed_output
    from .intelligence import parse_provider_json, ProviderError
    from .engine import digest
    prepared = request.get('prepared') or {}
    if (request.get('status') != 'blocked' or request.get('provider_id') != 'local_codex'
            or not prepared or request.get('response')):
        return None
    intelligence = h.intelligence
    if not hasattr(intelligence, '_operation_identity'):
        return None
    key, operation = key_for(intelligence, run_id, **{
        field: prepared[field] for field in ('role', 'task', 'context', 'schema')})
    saved = h.engine.store.get('codex_reasoning', key, {})
    checkpoint = saved.get('checkpoint') or {}
    transcript = h.engine.store.get('transcripts', key + ':output', {})
    reservation = saved.get('reservation', {})
    records = [r for r in h.engine.store.get('budgets', run_id, {}).get('records', [])
               if r.get('id') == reservation.get('id')]
    if len(records) != 1:
        return None
    ledger = records[0]
    metadata = transcript.get('response_metadata', {})
    fields = {'run_id': run_id, 'role': 'engineer', 'operation_id': operation,
              'provider': 'local_codex', 'model': request.get('model')}
    if (saved.get('id') != key or saved.get('run_id') != run_id
            or saved.get('role') != 'engineer' or saved.get('operation_id') != operation
            or checkpoint.get('status') != 'completed' or checkpoint.get('public_output_truncated')
            or checkpoint.get('error_code') or checkpoint.get('native_tool_violation')
            or saved.get('schema') != author.submission_schema(prepared.get('context') or {})
            or saved.get('selection', {}).get('model') != request.get('model')
            or any(transcript.get(k) != v or ledger.get(k) != v for k, v in fields.items())
            or transcript.get('kind') != 'output' or transcript.get('request_id') != reservation.get('id')
            or ledger.get('status') != 'completed' or not completed_output(metadata)
            or ledger.get('response_metadata') != metadata
            or ledger.get('thread_id') != checkpoint.get('thread_id')
            or ledger.get('turn_id') != checkpoint.get('turn_id')):
        return None
    messages = checkpoint.get('public_messages', [])
    final = [m for m in messages if m.get('phase') == 'final_answer']
    if not final:
        final = [m for m in messages if m.get('phase') is None]
    if not final or any(not isinstance(m.get('text'), str) for m in final):
        return None
    text = '\n'.join(m['text'] for m in final)
    try:
        if saved.get('transport_schema'):
            envelope, _ = parse_provider_json(text, TRANSPORT_SCHEMA, metadata)
            text = envelope['result_json']
        result, _ = parse_provider_json(text, {
            'type': 'object', 'additionalProperties': False,
            'properties': {'error': {'type': 'string'}}, 'required': ['error']}, metadata)
    except (ProviderError, ValueError, KeyError):
        return None
    if text != transcript.get('text') or not any(tool in result['error'] for tool in (author.TOOL, author.EDIT_TOOL)):
        return None
    return {'native_request_id': key, 'transcript_id': transcript['id'],
            'original_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'settlement_sha256': digest(ledger), 'provider_id': 'local_codex', 'model': request['model']}


def _source(h, run_id, key, progress):
    if (progress.get('mode') != 'final_only' or progress.get('pending_tool_calls')
            or h.engine.store.get(author.COLLECTION, key) or h.engine.store.get('decisions', key)):
        return None
    final = progress.get('finalization', {})
    original = progress.get('schema_correction', {}).get('original_request', final)
    reason = original.get('reason', '')
    if not ('tool-turn limit' in reason or 'Repeated tool call without progress:' in reason):
        return None
    if final.get('status') == 'ready' and not final.get('dispatched_at') and not progress.get('schema_correction'):
        normal = progress.get('normal_request', {})
        if (normal.get('turn', -1) >= progress.get('turn', 0)
                and normal.get('status') in {'dispatching', 'blocked'} and not normal.get('response')):
            return None
        identity = None
        if hasattr(h.intelligence, 'provider_for'):
            provider = h.intelligence.provider_for('engineer')
            identity = {'provider_id': normal.get('provider_id', provider['id']),
                        'model': normal.get('model', provider['model'])}
            if (identity['provider_id'], identity['model']) != (provider['id'], provider['model']):
                from .engine import ResearchBlocked
                raise ResearchBlocked('实现文档保存须继续使用原工程师的供应方和模型。')
        return {'kind': 'unsent_final_after_reading', 'charges_added': 0, 'provider_identity': identity}
    requests = [original, final] if original != final else [final]
    receipts = [_terminal_failure(h, run_id, request) for request in requests]
    if not all(receipts):
        return None
    provider = h.intelligence.provider_for('engineer')
    if any((r['provider_id'], r['model']) != (provider['id'], provider['model']) for r in receipts):
        from .engine import ResearchBlocked
        raise ResearchBlocked('实现文档保存须继续使用原工程师的供应方和模型，未重复发送原请求。')
    return {'kind': 'completed_refusal_without_write', 'receipts': receipts, 'charges_added': 0,
            'provider_identity': {'provider_id': provider['id'], 'model': provider['model']}}


def validate_identity(h, context):
    if not enabled(context):
        return
    identity = context.get('implementation_document_completion', {}).get('provider_identity')
    if not identity and not hasattr(h.intelligence, 'provider_for'):
        return  # Explicit offline fixtures have no provider identity or paid dispatch.
    provider = h.intelligence.provider_for('engineer')
    if not identity or (identity.get('provider_id'), identity.get('model')) != (provider['id'], provider['model']):
        from .engine import ResearchBlocked
        raise ResearchBlocked('实现文档保存交接的原供应方或模型配置已改变，未派发新请求。')


def require_save(h, run_id, role, purpose, context, key, progress):
    """Called before generic final inference, including budget preview."""
    if role != 'engineer' or not purpose.startswith('implementation_spec:') or not author.enabled(context):
        return
    if h.engine.store.get(author.COLLECTION, key):
        return
    if enabled(context):
        from .engine import ResearchBlocked
        raise ResearchBlocked('实现文档保存阶段未取得成功写入回执；已保留具体错误，尚未提交审计或派发代码。')
    if _source(h, run_id, key, progress) is not None:
        raise SaveRequired('实现文档读取已结束，须先保存真实文档再提交审计。')


def _merge_directories(target, directories):
    from .engine import ResearchBlocked
    for directory in directories:
        did = directory['document_id']
        previous = target.get(did)
        if previous and previous['sha256'] != directory['sha256']:
            raise ResearchBlocked('继承文档目录包含同一文档的不同版本。')
        target[did] = directory


def _inherited_scope(engine, h, run_id, source_id, context, progress, state, binding):
    """Rebuild the exact tool scope, keeping revision inputs version-bound.

    Previous specifications and private working notes are deliberately exposed
    after the initial handoff is frozen. Reuse that authorization, never the
    global document catalogue, to validate their actual reading receipts.
    """
    from .engine import ResearchBlocked
    from .document_working_notes import scoped_context

    frozen = context.get('previous_specifications', [])
    from . import implementation_indexed_review as indexed
    if (type(context.get(indexed.PROTOCOL)) is not int or context[indexed.PROTOCOL] != indexed.VERSION
            or not indexed.enabled(engine, run_id, state)):
        raise ResearchBlocked('旧作者保存继承流程已经删除，不能恢复旧冻结请求。')
    if binding['revision']:
        from . import implementation_acceptance as acceptance
        from . import implementation_definition_repair as definitions
        if context.get(definitions.PROTOCOL) == 1:
            previous = [definitions.validate_previous(engine, run_id, state, context, binding['revision'])]
        elif acceptance.enabled(context) and acceptance.enabled(state):
            from .implementation_acceptance_flow import validate_repair_previous
            previous = [validate_repair_previous(engine, run_id, state, binding['revision'])]
        else:
            from . import implementation_indexed_replies as replies
            replies.validate_previous(engine, run_id, state, binding['revision'])
            previous = [replies.previous_round(state, binding['revision'])]
    else:
        previous = []

    def identity(row):
        doc = row.get('document') or {}
        return row.get('index'), row.get('spec_hash'), doc.get('document_id'), doc.get('sha256')

    if (not isinstance(frozen, list) or len(frozen) != len(previous)
            or any(identity(saved) != identity(bound)
                   for saved, bound in zip(frozen, previous))):
        raise ResearchBlocked('继承的前版实现文档与原交接冻结版本不一致。')
    allowed = {}
    _merge_directories(allowed, context['indexed_documents'])
    scoped = scoped_context(h, run_id, 'engineer', source_id, progress, context)
    _merge_directories(allowed, scoped['indexed_documents'])
    scoped = author.scoped_context(engine, run_id, 'engineer', source_id, scoped)
    _merge_directories(allowed, scoped['indexed_documents'])
    return allowed


def _successor(engine, h, run_id, state, source_id):
    from .engine import ResearchBlocked, digest
    inputs = engine.store.get('handoff_inputs', source_id, {})
    progress = engine.store.get('handoff_progress', source_id, {})
    assignment, context, bound, binding = author._assignment(engine, run_id, 'engineer', source_id)
    if (bound['id'] != state['id'] or binding['revision'] != state['current_revision']
            or state['status'] != 'specification' or assignment.get('input_hash') != digest({
                k: inputs[k] for k in ('instruction', 'context', 'schema')})
            or inputs.get('schema') != author.submission_schema(context)):
        raise ResearchBlocked('实现文档保存前驱与当前工作流版本不一致。')
    audit_id = source_id + ':save-predecessor:v1'
    audit = engine.store.get('stage_contexts', audit_id)
    if audit:
        payload = audit['payload']
        if (audit.get('run_id') != run_id or digest(payload) != audit['sha256']
                or engine.store.get('handoff_inputs', source_id) != payload['input']
                or engine.store.get('handoff_progress', source_id) != payload['progress']):
            raise ResearchBlocked('实现文档保存前驱的输入或原回执已改变，未派发新请求。')
        _inherited_scope(engine, h, run_id, source_id, context, progress, bound, binding)
        child = engine.store.get('handoffs', audit['successor_id'])
        inputs = engine.store.get('handoff_inputs', audit['successor_id'])
        if not child or not inputs or child.get('input_hash') != digest({k: inputs[k] for k in ('instruction','context','schema')}):
            raise ResearchBlocked('实现文档保存交接身份已改变。')
        return child, inputs
    source = _source(h, run_id, source_id, progress)
    if source is None:
        raise ResearchBlocked('原实现文档请求尚无确定完成回执，不能派发保存交接。')
    originals = {}
    allowed = _inherited_scope(engine, h, run_id, source_id, context, progress, bound, binding)
    docs = {}
    _merge_directories(docs, context['indexed_documents'])
    notes = progress.get('document_working_notes')
    if notes:
        from .document_working_notes import _checked, HOT_LIMIT
        _checked(engine, run_id, 'engineer', source_id, progress)
        doc = notes['document']
        _merge_directories(docs, [doc])
        _merge_directories(allowed, [doc])
        selected = []
        for entry in reversed(notes['payload']['entries']):
            candidate = [entry, *selected]
            if len(json.dumps(candidate, ensure_ascii=False, sort_keys=True)) <= HOT_LIMIT:
                selected = candidate
        note_view = {'source_handoff_id': source_id, 'selected': selected,
                     'document': doc, 'sha256': notes['sha256'],
                     'scientific_approval': False, 'scientific_claims_verified': False,
                     'primary_source_reading': False}
    else:
        note_view = None
    receipts = []
    for row in progress.get('tool_results', []):
        call, page = row.get('call', {}), row.get('result', {})
        if call.get('name') not in {'documents.read', 'documents.search'} or page.get('error'):
            continue
        doc = allowed.get(page.get('document_id'))
        if (not doc or page.get('sha256') != doc['sha256']
                or call.get('arguments', {}).get('document_id') != doc['document_id']
                or call.get('arguments', {}).get('expected_sha256') != doc['sha256']):
            raise ResearchBlocked('继承读取记录不属于原交接的冻结文档。')
        did = doc['document_id']
        if did not in originals:
            originals[did] = document_index.load(engine, run_id, did, doc['sha256'])[1]
        if call['name'] == 'documents.read':
            section = next((s for s in doc['sections'] if s['id'] == page.get('section_id')), None)
            start, end = page.get('char_start'), page.get('char_end')
            if (not section or type(start) is not int or type(end) is not int
                    or not section['start'] <= start < end <= section['end']
                    or start != section['start'] + call['arguments'].get('offset', 0)
                    or originals[did][start:end] != page.get('text')):
                raise ResearchBlocked('继承读取记录的精确原文范围已改变。')
        # Keep only supplemental versions actually read, rather than reopening
        # every historical note in the new author's bounded save context.
        _merge_directories(docs, [doc])
        receipts.append({**copy.deepcopy(row), 'predecessor_handoff_id': source_id})
    payload = {'input': copy.deepcopy(inputs), 'progress': copy.deepcopy(progress), 'source': source}
    sha = digest(payload)
    next_context = {**copy.deepcopy(context), 'indexed_documents': list(docs.values()),
        'document_read_window_protocol': 2, 'implementation_document_completion_protocol': VERSION,
        'implementation_document_completion': {'source_handoff_id': source_id,
            'predecessor_audit_id': audit_id, 'predecessor_sha256': sha,
            'phase': 'save', 'saved_read_receipts': len(receipts), 'tool_turn_limit': LIMIT,
            'scientific_approval': False, 'provider_identity': source.get('provider_identity')}}
    if note_view:
        next_context['predecessor_document_notes'] = note_view
    task = TASK
    from .implementation_acceptance_author import tool_limit as acceptance_author_limit, GUIDANCE as COMPLETION_GUIDANCE
    acceptance_limit = acceptance_author_limit(next_context)
    if acceptance_limit is not None:
        next_context['implementation_document_completion']['tool_turn_limit'] = acceptance_limit
        task = task.replace('最多四轮保存/检查', f'本次最多 {acceptance_limit} 轮保存/检查') + COMPLETION_GUIDANCE
    from .implementation_author_revision import contract as revision_contract
    revision = revision_contract(next_context)
    if revision is not None:
        next_context['implementation_document_completion']['tool_turn_limit'] = revision['save_tool_turn_budget']
        task = task.replace('最多四轮保存/检查',
            f"本次最多 {revision['save_tool_turn_budget']} 轮保存/检查，逐项完成冻结修改清单")
        task = task.replace('最终仅提交工具实际回传的 submission',
            '最终提交工具实际回传的 submission 身份、全部 issue_responses 和 revision_completion')
    next_instruction = inputs['instruction'] + task
    from . import implementation_definition_repair as definitions
    if next_context.get(definitions.PROTOCOL) == 1:
        next_context = definitions.author_capacity(next_context, state)
        next_instruction += ('\n本保存续接允许补充原批准正文之外的资源估算定义，正文不设字符上限；'
            '以本请求 specification_schema 的 algorithm.maxLength 为准，旧24000总长度限制不再阻碍此补充。'
            '原批准正文仍逐字保留；不要把保存长度诊断写进科学定义。'
            '依据继承的读取和工作笔记保存候选补充并回读核对，由独立审核判断其是否解决原缺口。')
    from .implementation_document_readability import new_save_instruction
    next_instruction, next_context = new_save_instruction(next_instruction, next_context)
    purpose = assignment['purpose'] + ':save:1'
    child = h._handoff(run_id, 'engineer', purpose, next_instruction, next_context, author.submission_schema(next_context))
    child_inputs = engine.store.get('handoff_inputs', child['id'])
    audit = {'id': audit_id, 'run_id': run_id, 'sha256': sha, 'payload': payload,
             'successor_id': child['id'], 'created_at': now(), 'scientific_approval': False}
    # Commit the snapshot and seeded ledger together before any model dispatch.
    engine.store.batch([('stage_contexts', audit_id, audit), ('handoff_progress', child['id'], {
        'id': child['id'], 'run_id': run_id, 'turn': 0, 'seen': {}, 'tool_results': receipts,
        'predecessor_audit_id': audit_id})])
    engine.store.event(run_id, 'implementation.document_save_prepared',
        '已保留原读取和费用回执，工程师进入实现文档保存阶段；成功保存后仍需同版本独立审计。',
        {'handoff_id': child['id'], 'source_handoff_id': source_id, 'saved_read_receipts': len(receipts),
         'charges_added': 0, 'scientific_approval': False})
    return child, child_inputs


def validate_call(engine, run_id, key, context, call, *, request_context=None):
    """Completion cannot reopen the original source-reading loop."""
    # A legacy paid request retains its original tools even when a later
    # projection exposes the new edit capability. Use its dispatched contract.
    requested = request_context if request_context is not None else context
    if (requested.get(author.EDIT_PROTOCOL) == author.EDIT_VERSION
            and call.get('name') == author.TOOL):
        raise ValueError('此请求须用 implementation.edit_document 按当前基线修改，不能重写完整文档。')
    if not enabled(context):
        return
    if call.get('name') in {author.TOOL, author.EDIT_TOOL}:
        return
    from .implementation_document_locator import TOOL as LOCATE, enabled as locate_enabled
    if call.get('name') == LOCATE and locate_enabled(requested):
        return  # Handler independently checks the exact current author/base identity.
    from .implementation_save_repair import validate_call as validate_repair_read
    if validate_repair_read(engine, run_id, key, requested, call):
        return
    record = engine.store.get(author.COLLECTION, key, {})
    doc = record.get('row', {}).get('document', {})
    args = call.get('arguments', {})
    if (call.get('name') not in {'documents.read', 'documents.search'}
            or not doc or record.get('run_id') != run_id
            or args.get('document_id') != doc.get('document_id')
            or args.get('expected_sha256') != doc.get('sha256')):
        raise ValueError('实现文档保存阶段只能写文档和检查自己实际保存的当前草稿，不能重复读取原输入。')


async def ask(engine, h, run_id, state, purpose, instruction, context):
    """Return the actual submitting handoff, including one save successor."""
    from .implementation_document_readability import new_assignment
    schema = author.submission_schema(context)
    instruction, context = new_assignment(h, run_id, purpose, instruction, context, schema)
    original = h._handoff(run_id, 'engineer', purpose, instruction, context, schema)
    source_id = original['id']
    if not engine.store.get('stage_contexts', source_id + ':save-predecessor:v1'):
        progress = engine.store.get('handoff_progress', source_id, {})
        if _source(h, run_id, source_id, progress) is None:
            try:
                result = await h.ask(run_id, 'engineer', purpose, instruction, context, schema)
                return result, source_id
            except SaveRequired:
                pass
    child, inputs = await engine._read_consensus_check(run_id, _successor, engine, h, run_id, state, source_id)
    engine.check_active(run_id)
    # A completed successor is replayed from its frozen decision without model
    # dispatch. Its historical provider need not match today's research model.
    # Unfinished saves still require the original author/model identity.
    if not engine.store.get('decisions', child['id']):
        validate_identity(h, inputs['context'])
    result = await h.ask(run_id, 'engineer', child['purpose'], inputs['instruction'], inputs['context'], inputs['schema'])
    return result, child['id']
