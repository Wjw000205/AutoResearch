"""Disposable execution control for finishing an author's frozen revision.

Raw receipts establish retrieved ranges, never delivery or scientific credit.
This projection changes only a new request's next tool-batch shape; frozen
inputs, paid requests, source receipts, and final acceptance gates stay intact.
"""
from __future__ import annotations

import copy
import re

from .implementation_author_revision import KEY, contract
from .implementation_review_routing import digest

REPEAT_LIMIT = 6
HOST_DIAGNOSTIC = 'implementation_author_edit_required'
_WRITERS = frozenset({'implementation.edit_document', 'implementation.write_document'})
_READONLY = frozenset({'implementation_author_read_recovery',
    'implementation_author_delivery_recovery', 'implementation_author_inherited_delivery_recovery',
    'implementation_author_response_correction', 'implementation_author_origin_correction'})
_HASH = re.compile(r'^[0-9a-f]{64}$')
GUIDANCE = ('\n作者修订执行控制：按原 fix_id 和目标字段定向修正当前草稿，先落实尚未完成的修改，'
    '再核对保存后的相关章节并逐项自检。复用已取回范围索引；仅在需要核对具体原文时定向重开，'
    '不要循环重读固定原输入而耗尽修订预算。已取回不等于本次已展示、实际 Delivered 或科学批准。'
    '批准定义绑定明确保留的继承条款，按绑定的文档版本和精确保留范围落实；不得扩展到未批准的旧条款。'
    '缺少真正决定性依据时可如实返回 incomplete，或以真实证据反驳；不得造数据、批准或完成声明。')


def _hash(value):
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def _pointer(value):
    if not isinstance(value, dict):
        return None
    fields = ('document_id', 'document_sha256', 'spec_hash')
    if (not isinstance(value.get('document_id'), str) or not value['document_id']
            or not all(_hash(value.get(field)) for field in fields[1:])):
        return None
    return {field: value[field] for field in fields}


def _read(row):
    """Validate a full raw Unicode page shape without copying its text."""
    if not isinstance(row, dict):
        return None
    call, page = row.get('call'), row.get('result')
    if (not isinstance(call, dict) or call.get('name') != 'documents.read'
            or not isinstance(page, dict) or page.get('error')
            or page.get('text_returned') is False):
        return None
    args = call.get('arguments')
    if not isinstance(args, dict) or set(args) - {'document_id', 'expected_sha256', 'section_id', 'offset', 'limit'}:
        return None
    did, checksum, section = args.get('document_id'), args.get('expected_sha256'), args.get('section_id')
    offset, limit = args.get('offset', 0), args.get('limit', 4000)
    start, end, text = page.get('char_start'), page.get('char_end'), page.get('text')
    total = page.get('total_chars')
    if (not isinstance(did, str) or not did or not _hash(checksum)
            or not isinstance(section, str) or not section
            or page.get('document_id') != did or page.get('sha256') != checksum
            or page.get('section_id') != section or page.get('offset') != offset
            or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 6000
            or type(start) is not int or type(end) is not int or not 0 <= offset <= start < end
            or type(total) is not int or total < offset + end - start
            or not isinstance(text, str) or len(text) != end - start or len(text) > limit
            or page.get('provenance') != 'exact_document_section'
            or page.get('evidence_state') != 'retrieved'):
        return None
    return (did, checksum, section), start, end


def _merge(spans, start, end):
    merged = []
    for left, right in sorted([*spans, (start, end)]):
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
        else:
            merged.append((left, right))
    return merged


def _saved(row, known_spec):
    if (not isinstance(row, dict) or row.get('predecessor_handoff_id') is not None
            or not isinstance(row.get('call'), dict) or row['call'].get('name') not in _WRITERS):
        return None
    result = row.get('result')
    if not isinstance(result, dict) or result.get('saved') is not True or result.get('error'):
        return None
    pointer = _pointer(result.get('submission'))
    document, changes = result.get('document'), result.get('save_changes')
    if (pointer is None or not isinstance(document, dict)
            or document.get('document_id') != pointer['document_id']
            or document.get('sha256') != pointer['document_sha256']
            or not isinstance(changes, dict) or type(changes.get('version')) is not int
            or changes['version'] != 1 or changes.get('sha256') != digest({
                key: value for key, value in changes.items() if key != 'sha256'})
            or changes.get('base_spec_hash') != known_spec
            or changes.get('saved_spec_hash') != pointer['spec_hash']
            or pointer['spec_hash'] == known_spec):
        return None
    fields = changes.get('fields')
    if not isinstance(fields, list) or not fields:
        return None
    for field in fields:
        if (not isinstance(field, dict) or not isinstance(field.get('field'), str) or not field['field']
                or not _hash(field.get('before_sha256')) or not _hash(field.get('after_sha256'))
                or type(field.get('before_present')) is not bool or type(field.get('after_present')) is not bool
                or (field['before_sha256'] == field['after_sha256']
                    and field['before_present'] == field['after_present'])):
            return None
    if len({field['field'] for field in fields}) != len(fields):
        return None
    return pointer


def _failure(row):
    if (not isinstance(row, dict) or row.get('predecessor_handoff_id') is not None
            or not isinstance(row.get('call'), dict) or row['call'].get('name') not in _WRITERS):
        return None
    result = row.get('result')
    if not isinstance(result, dict) or result.get('saved') is not False:
        return None
    validation = result.get('validation')
    if not isinstance(validation, dict):
        return None
    paths = set()
    errors = validation.get('errors', [])
    errors = errors if isinstance(errors, list) else []
    for error in [validation, *errors]:
        if not isinstance(error, dict):
            continue
        path = error.get('field_path')
        if isinstance(path, list) and all(isinstance(part, str) or type(part) is int for part in path):
            path = '/' + '/'.join(str(part)[:120].replace('~', '~0').replace('/', '~1') for part in path[:16])
        if isinstance(path, str):
            paths.add(path[:240])
    references = []
    problems = validation.get('reference_problems', [])
    for problem in problems[:64] if isinstance(problems, list) else []:
        if not isinstance(problem, dict) or not isinstance(problem.get('reason'), str):
            continue
        reference = problem.get('reference')
        if not isinstance(reference, dict):
            continue
        compact = {key: value[:6000] if isinstance(value, str) else value
            for key, value in reference.items()
            if key in {'document_id', 'document_sha256', 'section_id', 'offset', 'limit'}
            and (isinstance(value, str) or type(value) is int)}
        references.append({'reference_index': problem.get('reference_index')
            if type(problem.get('reference_index')) is int else None,
            'reference': compact, 'reason': problem['reason'][:2000]})
    return {'code': validation.get('code') if isinstance(validation.get('code'), str) else None,
        'field_paths': sorted(paths)[:64], 'saved': False,
        'reference_problems': references,
        'instruction': '按真实 validation 修正并重交原必要修改；失败不算保存进展。'}


def _tools(schema):
    """Find actual tool branches without inventing a tool or weakening them."""
    if not isinstance(schema, dict):
        return []
    found = []
    calls = schema.get('properties', {}).get('tool_calls')
    if isinstance(calls, dict):
        names = calls.get('items', {}).get('properties', {}).get('name', {})
        enum = names.get('enum', [names['const']] if 'const' in names else [])
        writers = sorted(_WRITERS.intersection(enum)) if isinstance(enum, list) else []
        if calls.get('type') == 'array' and writers:
            found.append((calls, writers))
    for key in ('anyOf', 'oneOf', 'allOf'):
        for branch in schema.get(key, []):
            found.extend(_tools(branch))
    return found


def _rework(progress, value, ledger):
    entries = progress.get('alignment_candidate_history', [])
    if not isinstance(entries, list):
        return None
    latest = next((entry for entry in reversed(entries) if isinstance(entry, dict)
        and entry.get('author_revision_problems')), None)
    if latest is None:
        return None
    audit = latest.get('author_revision_audit', {})
    start = audit.get('source_tool_count') if isinstance(audit, dict) else None
    if (type(start) is not int or not 0 <= start <= len(ledger)
            or audit.get('source_tool_results_sha256') != digest(ledger[:start])):
        return None
    items = {item['fix_id']: item for item in value['items']}
    pending = {}
    for problem in latest['author_revision_problems']:
        if not isinstance(problem, dict) or not isinstance(problem.get('fields'), list):
            return None
        fix_id, fields = problem.get('fix_id'), problem['fields']
        if not fields or not all(isinstance(field, str) for field in fields):
            return None
        matches = [items[fix_id]] if isinstance(fix_id, str) and fix_id in items else (
            [item for item in items.values() if set(fields).intersection(item['fields'])] if fix_id is None else [])
        if not matches or set(fields) - {field for item in matches for field in item['fields']}:
            return None
        for item in matches:
            selected = set(fields).intersection(item['fields'])
            pending.setdefault(item['fix_id'], set()).update(selected)
    return start, [{'fix_id': fix_id, 'fields': sorted(fields)} for fix_id, fields in pending.items()]


def _readonly(*containers):
    return any(container.get(key) is not None for container in containers for key in _READONLY)


def can_redirect_repeat(context, progress):
    """Allow the host to withhold a redundant read, never execute a new tool."""
    if (progress.get('mode') == 'final_only'
            or context.get('_harness_control', {}).get('mode') == 'final_only'
            or _readonly(context, progress)):
        return False
    from .implementation_acceptance_author import enabled as batch_author
    if (batch_author(context) and context.get('implementation_document_authoring_protocol') == 1
            and context.get('implementation_document_authoring', {}).get('revision', 0) > 0):
        return True
    if KEY not in context:
        return False
    value = contract(context)
    ledger = progress.get('tool_results')
    return (value.get('revision', 0) > 0 and isinstance(ledger, list)
            and _rework(progress, value, ledger) is not None)


def prepare(context, progress, prepared):
    """Project a new tool-capable author request; never mutate source objects."""
    request_context = prepared.get('context', {})
    if (KEY not in context or prepared.get('role') != 'engineer'
            or not request_context.get('_purpose', '').startswith('implementation_spec:')
            or progress.get('mode') == 'final_only'
            or request_context.get('_harness_control', {}).get('mode') == 'final_only'
            or _readonly(context, request_context, progress)):
        return prepared
    value = contract(context)
    if value.get('revision', 0) <= 0:
        return prepared
    result = copy.deepcopy(prepared)
    tools = _tools(result.get('schema'))
    ledger = progress.get('tool_results', [])
    if not isinstance(ledger, list):
        ledger = []
    rework = _rework(progress, value, ledger)
    start, pending = rework if rework else (0, [])
    draft = request_context.get('implementation_document_draft', {})
    current = _pointer(draft.get('submission')) if isinstance(draft, dict) and draft.get('saved') is True else None
    known_spec, last_save, failure = value['previous_spec_hash'], None, None
    for index, row in enumerate(ledger):
        saved = _saved(row, known_spec)
        if saved is not None:
            known_spec, failure = saved['spec_hash'], None
            if saved == current:
                last_save = index
        diagnosed = _failure(row)
        if diagnosed is not None and index >= start:
            failure = diagnosed
    if rework and last_save is not None:
        start = max(start, last_save + 1)
    coverage = {}
    new, repeated, consecutive = 0, 0, 0
    withheld = False
    for index, row in enumerate(ledger):
        if (index >= start and isinstance(row, dict)
                and row.get('host_diagnostic') == HOST_DIAGNOSTIC):
            withheld = True
        page = _read(row)
        if page is None:
            continue
        source, left, right = page
        spans = coverage.setdefault(source, [])
        contained = any(a <= left and right <= b for a, b in spans)
        if index >= start:
            if contained:
                repeated, consecutive = repeated + 1, consecutive + 1
            else:
                new, consecutive = new + 1, 0
        coverage[source] = _merge(spans, left, right)
    edit_required = bool(rework and tools and (withheld or repeated >= REPEAT_LIMIT))
    control = {'version': 1, 'rework': rework is not None, 'current_draft_pointer': current,
        'pending_fixes': pending, 'pending_fields': sorted({field for item in pending for field in item['fields']}),
        'new_read_count': new, 'repeated_read_count': repeated, 'consecutive_repeated_reads': consecutive,
        'analysis_start_tool_count': start, 'last_saved_tool_index': last_save,
        'edit_required': edit_required, 'withheld_repeat_read': withheld, 'failed_writer': failure,
        'reading_credit_granted': False, 'scientific_approval': False}
    result['context'].setdefault('_harness_control', {})['author_revision_execution'] = control
    result['task'] = result.get('task', '') + GUIDANCE
    if edit_required:
        for calls, writers in tools:
            requirement = {'contains': {'type': 'object', 'properties': {'name': {'enum': writers}},
                'required': ['name']}, 'minContains': 1}
            if 'contains' in calls:
                calls.setdefault('allOf', []).append(requirement)
            else:
                calls.update(requirement)
        result['task'] += ('\n检测到重复读取循环且未成功保存新变化，宿主已要求定向编辑。下一工具批次必须包含当前已授权的'
            ' implementation.edit_document 或 implementation.write_document，按当前草稿真实身份定向修正；'
            '仍可附必要定向读取，原工具数量上限及CAS继续生效。最终答复仍允许真实反驳或如实 incomplete。')
    return result
