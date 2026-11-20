"""Versioned, recoverable intent checks for already delivered review sources.

This is an execution-side reading policy, never a scientific judgment. New
ranges, failed/unknown deliveries and old paid requests retain their readers.
"""
from __future__ import annotations

import copy

from .working_context import sha

FIELD = 'code_review_reread_protocol'
VERSION = 1
INTENTS = 'reading_intents'
HISTORY = 'code_review_read_intent_history'
BINDING = 'code_review_read_intent_binding'
REASONS = ('missing_detail', 'source_conflict', 'exact_quote')
TOOLS = {'artifacts.read', 'code.read', 'documents.read'}
ITEM = {'type': 'object', 'properties': {
    'call_index': {'type': 'integer', 'minimum': 0},
    'reason': {'enum': list(REASONS)},
    'question': {'type': 'string', 'minLength': 8, 'maxLength': 1200, 'pattern': r'\S'},
    'expected_evidence': {'type': 'string', 'minLength': 8, 'maxLength': 1200, 'pattern': r'\S'}},
    'required': ['call_index', 'reason', 'question', 'expected_evidence'], 'additionalProperties': False}
SCHEMA = {'type': 'array', 'items': ITEM, 'uniqueItems': True}
GUIDANCE = ('\n同版本重查协议：优先复用本人已投递范围和已保存观察。'
    '仅因正文退出当前窗口不构成补读缺口。新范围和未投递正文可正常读取；'
    '重查 delivered_ranges 已覆盖的范围时，在 reading_intents 中按 tool_calls 的零基 call_index '
    '逐项填写 reason（missing_detail/source_conflict/exact_quote）、具体未解决 question 和预期核对的 '
    'expected_evidence；不得把理由塞进工具 arguments。没有具体缺口时不要重查。'
    '同一问题已成功取回且投递的范围不因改分页而重新读取；先处理返回内容，确有新问题再明确说明。'
    '没有新原文、输出或问题处置时不算进度。证据充分时提交本角色独立意见，仍可否决、要求修改或保留依赖；'
    '缺少执行回执应直接报告，不能反复读源码替代。诊断不代表审核通过，也不关闭工具。')


def _branches(schema):
    if not isinstance(schema, dict):
        return
    if 'tool_calls' in schema.get('properties', {}):
        yield schema
    for operator in ('anyOf', 'oneOf', 'allOf'):
        for branch in schema.get(operator, []):
            yield from _branches(branch)


def project(prepared, progress):
    """Only newly built, authenticated reading plans opt into this contract."""
    from .code_review_reading import PLAN
    from .code_review_catalog import PROTOCOL, VERSION as CATALOG_VERSION
    def decodable(schema):
        fields = schema.get('properties', {})
        return ({'code_evidence', 'alignment_findings', 'alignment_binding'} <= fields.keys()
            or any(decodable(branch) for operator in ('anyOf', 'oneOf', 'allOf') for branch in schema.get(operator, [])))
    if (PLAN not in prepared['context'] or not list(_branches(prepared['schema']))
            or type(prepared['context'].get(PROTOCOL)) is not int
            or prepared['context'][PROTOCOL] not in (1, CATALOG_VERSION)
            or not decodable(prepared['schema'])):
        return prepared
    for branch in _branches(prepared['schema']):
        # Conditionally mandatory in the host validator: new reads need no
        # rationale. Optional metadata also preserves tool-scope projection.
        branch['properties'][INTENTS] = copy.deepcopy(SCHEMA)
    prepared['context'][FIELD] = VERSION
    history = progress.get(HISTORY, [])
    if history:
        prepared['context']['previous_read_intents'] = [
            {'request_id': row['request_id'], 'intents': copy.deepcopy(row['intents'])}
            for row in history if row.get('intents')]
    prepared['task'] += GUIDANCE
    return prepared


def strip(result, prepared):
    if FIELD not in prepared.get('context', {}) or 'tool_calls' not in result:
        return result
    if type(prepared['context'][FIELD]) is not int or prepared['context'][FIELD] != VERSION:
        raise ValueError('Unsupported code-review reread protocol')
    result.pop(INTENTS, None)
    return result


def binding(prepared, original, canonical):
    if FIELD not in prepared.get('context', {}) or 'tool_calls' not in canonical:
        return None
    if type(prepared['context'][FIELD]) is not int or prepared['context'][FIELD] != VERSION:
        raise ValueError('Unsupported code-review reread protocol')
    # The decoder validates the entire original response against its own paid
    # schema before deriving canonical arguments. Retain both identities.
    intents = original['json'].get(INTENTS, [])
    value = {'version': VERSION, 'request_id': original.get('request_id'),
        **{k: prepared['context']['_code_review_reading_plan'][k] for k in ('run_id', 'role', 'handoff_id')},
        'prepared_sha256': sha(prepared), 'original_sha256': sha(original['json']),
        'canonical_calls': copy.deepcopy(canonical['tool_calls']), 'intents': copy.deepcopy(intents)}
    return {**value, 'sha256': sha(value)}


def _range(engine, run_id, frozen, call):
    """Authenticate the actual source and clamp to its real Unicode length."""
    from .context_compiler import encoded
    if call['name'] not in TOOLS:
        return None
    args = call['arguments']
    if call['name'] == 'reviews.read_material':
        from .independent_full_review import read_material
        page = read_material(frozen, args)
        return page['document_id'], page['sha256'], page['char_start'], page['char_end']
    if call['name'] == 'documents.read':
        matches = [d for d in frozen.get('indexed_documents', []) if
            d.get('document_id') == args.get('document_id') and d.get('sha256') == args.get('expected_sha256')]
        if len({encoded(d) for d in matches}) != 1:
            return None  # Preserve conflicting/unknown inputs for the normal reader.
        from .document_index import read
        page = read(engine, run_id, args)
        return page['document_id'], page['sha256'], page['char_start'], page['char_end']
    matches = [s for s in frozen.get('delivery_artifacts', []) if
        s.get('artifact_id') == args.get('id') and s.get('sha256') == args.get('expected_sha256')]
    if len({encoded(s) for s in matches}) != 1:
        return None
    from .code_navigation import body
    text = body(engine, run_id, frozen, matches[0])
    start = args.get('offset', 0)
    limit = args.get('limit', 4000 if call['name'] == 'code.read' else 16000)
    if type(start) is not int or type(limit) is not int or not 0 <= start < len(text) or limit < 1:
        return None
    return matches[0]['artifact_id'], matches[0]['sha256'], start, min(len(text), start + limit)


def _covers(ranges, start, end):
    for a, b in sorted(ranges):
        if a <= start < b:
            start = b
            if start >= end:
                return True
    return False


def _completed_rechecks(store, run_id, role, owner, history, intent):
    """Only actual successful archived tool results can suppress a recheck.

    A repeated model rationale, an unexecuted intent or a failed tool call
    never establishes that the requested recheck bytes were returned.
    """
    from .execution_archive import load
    from .evidence_delivery import delivery_spans, covering_receipts
    from .prose_schema import validator
    result = []
    question = {k: intent[k] for k in ('reason', 'question', 'expected_evidence')}
    # Bind each history item back to the actual schema-validated original
    # model output in this controller-owned commit ancestry, not its self SHA.
    from .working_context import validate_commit, marker
    progress = store.get('handoff_progress', owner, {})
    wanted = {r.get('request_id') for r in history if any(
        {k: item[k] for k in question} == question for item in r.get('intents', []))}
    authenticated, visited = {}, set()
    cid = progress.get('working_context_commit')
    while cid and wanted - authenticated.keys():
        if cid in visited:
            raise ValueError('Code-review working context ancestry contains a cycle')
        visited.add(cid)
        saved = validate_commit(store, run_id, role, marker(run_id, role, owner, {'working_context_commit': cid}))
        payload = saved['payload']
        if saved['id'] != cid or cid != f'{run_id}:context-commit:{sha(payload)}':
            raise ValueError('Code-review working context ancestry identity changed')
        if payload.get('response_id') in wanted:
            authenticated[payload['response_id']] = payload
        cid = payload.get('previous_commit')
    for row in history:
        value = {k: v for k, v in row.items() if k != 'sha256'}
        if (type(row.get('version')) is not int or row['version'] != VERSION or row.get('sha256') != sha(value)
                or row.get('run_id') != run_id or row.get('role') != role or row.get('handoff_id') != owner):
            raise ValueError('Code-review read-intent history identity changed')
        indices = [item['call_index'] for item in row['intents']
                   if type(item.get('call_index')) is int and validator(ITEM).is_valid(item)
                   and {k: item[k] for k in question} == question]
        if not indices:
            continue
        committed = authenticated.get(row['request_id'])
        if (not committed or committed.get('prepared_sha256') != row.get('prepared_sha256')
                or sha(committed.get('output')) != row.get('original_sha256')
                or committed['output'].get(INTENTS, []) != row['intents']):
            continue  # Unauthenticated history cannot suppress a valid read.
        if not _calls_match(store.get('handoff_inputs', owner)['context'],
                            committed['output'].get('tool_calls'), row['canonical_calls']):
            continue
        calls = [row['canonical_calls'][i] for i in indices if 0 <= i < len(row['canonical_calls'])]
        keys = store.record_keys('tool_invocations', run_id,
            predicate="json_extract(data,'$.role')=? AND json_extract(data,'$.handoff_id')=? AND json_extract(data,'$.request_id')=?",
            parameters=[role, owner, row['request_id']])
        for key in keys:
            inv = store.get('tool_invocations', key)
            if inv.get('status') != 'succeeded' or inv.get('call') not in calls or inv.get('tool') not in TOOLS:
                continue
            original = load(store, run_id, role, inv['archive_id'], inv['archive_sha256'])
            page = original['result']
            if original['call'] != inv['call'] or not isinstance(page, dict) or page.get('error') or not isinstance(page.get('text'), str):
                continue
            start = page.get('char_start', page.get('offset'))
            if type(start) is int:
                identity = (page.get('document_id') if inv['tool'] in {'documents.read', 'reviews.read_material'}
                            else page.get('artifact_id'))
                end = start + len(page['text'])
                # Earlier delivery of the original source is insufficient.
                # This recheck's exact page must have been submitted after
                # its tool execution completed, including queued >50k pages.
                from hashlib import sha256
                page_sha = sha256(page['text'].encode()).hexdigest()
                identity_key = 'document_id' if inv['tool'] in {'documents.read', 'reviews.read_material'} else 'artifact_id'
                candidates = store.record_keys('evidence_deliveries', run_id,
                    predicate="json_extract(data,'$.role')=? AND json_extract(data,'$.handoff_id')=? AND "
                        "json_extract(data,'$." + identity_key + "')=? AND json_extract(data,'$.page_sha256')=? "
                        "AND json_extract(data,'$.created_at')>=?",
                    parameters=[role, owner, identity, page_sha, inv['updated_at']])
                requests = {store.get('evidence_deliveries', key).get('request_id') for key in candidates}
                requests.discard(None)
                if not requests:
                    continue
                delivered = delivery_spans(store, run_id, role, identity, page['sha256'],
                    identity_key=identity_key, handoff_id=owner, request_ids=sorted(requests))
                delivered = [r for r in delivered if r.get('created_at', '') >= inv['updated_at']
                             and r.get('page_sha256') == page_sha
                             and r.get('request_id') != row['request_id']
                             and _later_input(store, r, inv['updated_at'])]
                if covering_receipts(delivered, start, end):
                    result.append((identity, page.get('sha256'), start, end))
    return result


def _later_input(store, receipt, completed_at):
    """A late acknowledgement of an old input is not a new delivery."""
    from datetime import datetime
    manifest = store.get('context_manifests', receipt.get('manifest_id', ''), {})
    snapshot = store.get('context_snapshots', receipt.get('snapshot_id', ''), {})
    try:
        completed = datetime.fromisoformat(completed_at)
        return (datetime.fromisoformat(manifest['created_at']) >= completed
            and datetime.fromisoformat(snapshot['created_at']) >= completed)
    except (ValueError, KeyError, TypeError):
        return False  # Missing legacy timing cannot suppress a necessary read.


def _calls_match(frozen, raw, canonical):
    """Rebind original path/alias calls to the same frozen catalog, strictly."""
    from .code_review_catalog import catalog
    from .scoped_file_reference import select
    from .context_compiler import encoded
    if not isinstance(raw, list) or len(raw) != len(canonical):
        return False
    docs, files = catalog(frozen)
    normalized = copy.deepcopy(raw)
    try:
        for i, call in enumerate(normalized):
            if call.get('name') not in TOOLS:
                continue
            document = call['name'] == 'documents.read'
            key = 'document_id' if document else 'id'
            args = call['arguments']
            selectors = {k: v for k, v in (
                ('document' if document else 'file', args.get(key)), ('path', args.get('path')),
                ('document_sha256' if document else 'sha256', args.get('expected_sha256'))) if v is not None}
            chosen = select(selectors, docs if document else files, document=document, location=f'tools/{i}')
            args.pop('path', None)
            args.update({key: chosen['document_id' if document else 'artifact_id'], 'expected_sha256': chosen['sha256']})
    except (ValueError, KeyError, TypeError):
        return False
    return encoded(normalized) == encoded(canonical)


def validate(engine, run_id, role, owner, frozen, prepared, progress, name, arguments):
    """Run after ordinary owner/schema authorization; never widen its scope."""
    from .code_review_reading import PLAN
    context = prepared.get('context', {})
    if name not in TOOLS or FIELD not in context:
        return
    if type(context[FIELD]) is not int or context[FIELD] != VERSION:
        raise ValueError('Unsupported code-review reread protocol')
    plan = context.get(PLAN, {})
    if (plan.get('run_id') != run_id or plan.get('role') != role or plan.get('handoff_id') != owner
            or type(plan.get('version')) is not int or plan.get('version') != 1
            or plan.get('sha256') != sha({k: v for k, v in plan.items() if k != 'sha256'})
            or not context.get('_purpose', '').startswith('code_review:')):
        raise ValueError('Code-review reread check lacks an authenticated source plan')
    call = {'name': name, 'arguments': arguments}
    scope = _range(engine, run_id, frozen, call)
    if scope is None:
        return
    identity, version, start, end = scope
    source = next((s for s in plan['sources'] if s['id'] == identity and s['sha256'] == version), None)
    if not source or not _covers(source['delivered_ranges'], start, end):
        return  # Actual missing ranges remain readable without a reread rationale.
    request = progress.get('normal_request', {})
    current = request.get('response', {}).get(BINDING, {})
    original = request.get('original_catalog_response', request.get('response', {}))
    value = {k: v for k, v in current.items() if k != 'sha256'}
    index = progress.get('tool_cursor', 0)
    valid_binding = (type(current.get('version')) is int and current['version'] == VERSION
        and current.get('sha256') == sha(value)
        and all(current.get(k) == v for k, v in (('run_id', run_id), ('role', role), ('handoff_id', owner)))
        and current.get('prepared_sha256') == sha(prepared)
        and current.get('original_sha256') == sha(original.get('json'))
        and current.get('request_id') == request.get('response', {}).get('request_id')
        and current.get('intents') == original.get('json', {}).get(INTENTS, [])
        and current.get('canonical_calls') == request.get('response', {}).get('json', {}).get('tool_calls')
        and type(index) is int and 0 <= index < len(current.get('canonical_calls', []))
        and current['canonical_calls'][index] == call)
    # A pause after journal completion and before cursor commit must recover
    # the exact paid invocation, even if a newer delivery now exists.
    from .engine import _TOOL_INVOCATION
    invocation = engine.store.get('tool_invocations', _TOOL_INVOCATION.get() or '', {})
    if (invocation.get('status') == 'succeeded' and invocation.get('run_id') == run_id
            and invocation.get('role') == role and invocation.get('handoff_id') == owner
            and invocation.get('request_id') == request.get('response', {}).get('request_id')
            and invocation.get('call') == call):
        return  # The ordinary journal reader still authenticates its archive.
    intents = [r for r in current.get('intents', []) if r.get('call_index') == index] if valid_binding else []
    from .prose_schema import validator
    valid_intent = (len(intents) == 1 and type(intents[0].get('call_index')) is int
                    and validator(ITEM).is_valid(intents[0]))
    code = 'review_reread_specific_gap_required'
    if valid_intent:
        history = [r for r in progress.get(HISTORY, []) if r.get('request_id') != current['request_id']]
        completed = _completed_rechecks(engine.store, run_id, role, owner, history, intents[0])
        if not _covers([(a, b) for i, s, a, b in completed if i == identity and s == version], start, end):
            return
        code = 'review_same_question_already_rechecked'
    from .tool_navigation import NavigationError
    raise NavigationError('该同版本范围已投递；本次重复读取未执行，请明确尚缺证据或使用已有原文和观察继续审核。',
        code=code, details={FIELD: VERSION, 'executed': False, 'withheld_read': copy.deepcopy(call),
            'source_id': identity, 'sha256': version, 'range': [start, end],
            'already_delivered_ranges': copy.deepcopy(source['delivered_ranges']),
            'required_intent_fields': list(ITEM['required']), 'scientific_approval': False,
            'instruction': '不要仅以当前窗口未展示为由重读。具体缺少细节、存在冲突或需精确引文时填写 reading_intents；'
                '同一问题已取回的正文先处理其返回内容。无新缺口时复用本人原文和笔记提交独立意见，'
                '保留否决、修改要求及执行依赖；未投递和新范围仍可读取。'})


def clarification_result(context, output):
    """Typed nonexecution is distinct from a failed source/tool contract.

    Retain the exact denial and diagnostic. It has neither text nor a save
    receipt and cannot earn reading/progress credit or trigger a fake repair
    of the underlying source contract after three identical policy notices.
    """
    recovery = output.get('recovery', {})
    if (not (context.get('implementation_alignment_protocol') == 1 and context.get('delivery_directory_protocol') == 1)
            or type(recovery.get(FIELD)) is not int or recovery[FIELD] != VERSION
            or recovery.get('code') not in {'review_reread_specific_gap_required', 'review_same_question_already_rechecked'}
            or recovery.get('executed') is not False or recovery.get('reading_credit') is not False
            or set(output) - {'error', 'recovery'}):
        return None
    return {'status': 'read_withheld', 'executed': False, 'reading_credit': False,
        'scientific_approval': False, 'withheld_reason': output['error'], 'recovery': copy.deepcopy(recovery)}


def withheld(page):
    """Only this version's explicit nonexecution has no source-read body."""
    return (isinstance(page, dict) and page.get('status') == 'read_withheld'
        and page.get('executed') is False and page.get('reading_credit') is False
        and type(page.get('recovery', {}).get(FIELD)) is int and page['recovery'][FIELD] == VERSION
        and page['recovery'].get('code') in {'review_reread_specific_gap_required', 'review_same_question_already_rechecked'}
        and 'text' not in page and 'saved' not in page)
