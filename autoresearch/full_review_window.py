"""Disposable index-first views and a combined raw-source window for full reviews."""
from __future__ import annotations

import copy
import hashlib

from . import independent_full_review as full


def requested_indices(progress, tool_results):
    """Locate the completed response's exact batch, never infer reading credit.

    Diagnostics can be interleaved with receipts. A mismatched or incomplete
    batch supplies no priority hint; submission authentication remains the
    evidence layer's responsibility. No timestamp or acquisition counter is
    used as proof of delivery.
    """
    progress = progress or {}
    rows = [(index, row['call']) for index, row in enumerate(tool_results or [])
        if isinstance(row, dict) and isinstance(row.get('call'), dict)]
    batch = progress.get('completed_source_tool_batch')
    if batch is not None:
        from .engine import digest
        hashes = batch.get('call_sha256s') if isinstance(batch, dict) else None
        if (not isinstance(batch, dict) or type(batch.get('version')) is not int or batch['version'] != 1
                or type(batch.get('turn')) is not int or type(progress.get('turn')) is not int
                or batch['turn'] + 1 != progress['turn'] or not isinstance(hashes, list)
                or not hashes or len(rows) < len(hashes)):
            return []
        tail = rows[-len(hashes):]
        return [index for index, _ in tail] if [digest(call) for _, call in tail] == hashes else []
    # Historical checkpoints have no host batch marker. Their saved normal
    # response supplies the same exact call sequence without rewriting them.
    calls = progress.get('normal_request', {}).get('response', {}).get('json', {}).get('tool_calls')
    if not isinstance(calls, list) or not calls or len(rows) < len(calls):
        return []
    tail = rows[-len(calls):]
    return [index for index, _ in tail] if [call for _, call in tail] == calls else []


def project(frozen, prepared, *, tool_results=None, progress=None, requested_tool_result_indices=None):
    from . import implementation_definition_review_scope as definitions
    if definitions.citation(frozen):
        return prepared
    if definitions.enabled(frozen):
        rows = tool_results if tool_results is not None else prepared['context'].get('tool_results', [])
        indices = (list(range(max(0, len(rows) - 8), len(rows))) if progress is None
            else requested_indices(progress, rows))
        return definitions.project(prepared, rows, indices)
    if not full.tool_available(frozen):
        if (frozen.get('whole_code_source_clarification', {}).get('version') == 1
                or frozen.get('scientific_author_protocol') == 1):
            result=copy.deepcopy(prepared)
            if tool_results is not None:
                result['context']['tool_results']=copy.deepcopy(tool_results)
                for key in ('document_read_window','document_read_fragments'):
                    result['context'].pop(key,None)
            result['context']['full_review_source_window'] = {'requested_tool_result_indices':
                requested_indices(progress, result['context'].get('tool_results', []))}
            result['context']=bound(result['context'])
            return result
        return prepared
    result = copy.deepcopy(prepared)
    active = result['context']
    view = full.project(frozen,role=prepared.get('role'))
    # Preserve controls and derived navigation, never forward frozen bodies.
    for key, value in active.items():
        if (key.startswith('_') or key == 'tool_results' or key not in frozen
                or key.endswith('_protocol') or key in {'indexed_document_context', 'code_navigation_protocol'}):
            view[key] = copy.deepcopy(value)
    for key in ('indexed_documents', 'delivery_artifacts'):
        if key in frozen:
            view[key] = copy.deepcopy(frozen[key])  # metadata only
    if full.code_delivery_scope(frozen):
        from .code_whole_review import reading_documents, restricted_view
        view['indexed_documents'] = copy.deepcopy(list(reading_documents(frozen)))
        if restricted_view(view):
            acceptance = frozen.get('local_acceptance', {})
            view['local_acceptance'] = {k: acceptance[k] for k in ('approved', 'delivery_hash', 'code_sha256')
                if k in acceptance}
    if tool_results is not None:
        # One chronological window owns all source kinds. Independent document
        # and code projections must not hide newly acquired pages behind an
        # acquisition-only read index or duplicate fragment representations.
        view['tool_results'] = copy.deepcopy(tool_results)
        for key in ('document_read_window','document_read_fragments','code_review_fresh_document_delivery'):
            view.pop(key,None)
    # The source identity for this tool always comes from immutable inputs.
    view.update(full.project(frozen,role=prepared.get('role')))
    from .full_review_reading_context import reuse_view
    view = reuse_view(view)
    view['full_review_source_window'] = {'requested_tool_result_indices':
        requested_indices(progress, view.get('tool_results', [])) if requested_tool_result_indices is None
        else requested_tool_result_indices}
    result['context'] = bound(view)
    if 'code_revision_review' in frozen and (
            active.get('_harness_control', {}).get('mode') == 'final_only'
            or frozen.get(full.READING, {}).get('recovery_kind') == 'response_correction'):
        from .code_revision_review import answer_sources, constrain_answer_schema
        result['context']['revision_answer_sources'] = answer_sources(frozen,role=prepared['role'])
        # A correction may return its final answer during a tool-enabled turn.
        # Keep the tool branch unchanged and use the same final answer contract.
        for branch in result['schema'].get('anyOf', [result['schema']]):
            if 'previous_issues' in branch.get('properties', {}):
                constrain_answer_schema(branch, frozen, role=prepared['role'])
    def cap(node):
        if isinstance(node, dict):
            calls = node.get('properties', {}).get('tool_calls')
            if isinstance(calls, dict):
                calls['maxItems'] = min(calls.get('maxItems', 8), 8)
            for key, child in node.items():
                if key not in {'const', 'enum', 'default', 'examples'}:
                    cap(child)
        elif isinstance(node, list):
            for child in node: cap(child)
    cap(result['schema'])
    return result


def bound(view):
    """Retain newest exact text fragments without altering any durable receipt.

    Whole fragments are evicted, not truncated/relabelled as original tool
    results. The model can reopen their exact call; omitted bytes earn no
    submitted-delivery credit.
    """
    result = copy.deepcopy(view)
    candidates = []
    def visit(node):
        if isinstance(node, dict):
            for key, value in list(node.items()):
                body_fields = {'text', 'excerpt', 'stdout', 'stderr', 'conclusion'} | (
                    {'statement'} if result.get('review_memory_context_policy') == 'index-v1'
                        or 'code_reading_memory' in result else set())
                if key in body_fields and isinstance(value, str):
                    candidates.append((node, key, value))
                elif key == 'open_questions' and isinstance(value, list):
                    for index, question in enumerate(value):
                        if isinstance(question, str): candidates.append((value, index, question))
                        else: visit(question)
                elif isinstance(value, (dict, list)):
                    visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)
    visit(result)
    used = 0
    # Deliver the agent's explicit batch before automatically re-exhibited
    # history. Otherwise old decisive pages can starve every newly read page.
    # Keep this hint through the second bound after code-memory projection.
    requested = result.get('full_review_source_window', {}).get('requested_tool_result_indices', [])
    reused = result.get('_review_evidence_reuse', {}).get('fragments', [])
    pinned = {id(row) for row in reused}
    memory = result.get('code_reading_memory', {})
    priorities = {}
    note_reopen = {row['id']: row['read_with'] for row in memory.get('directory', [])
        if row.get('id') and row.get('read_with')}
    reopen = {}
    for row in memory.get('decisive_reads', []):
        priorities[id(row.get('reexhibited_original_read', {}).get('result'))] = 5 if row.get('selection_reason') == 'current_focus' else 1
    for row in memory.get('observations', []):
        priority = 6 if row.get('selection_reason') == 'current_focus' else 2
        priorities[id(row)] = priority
        priorities.update((id(q), priority) for q in row.get('open_questions', []))
        if row.get('id') in note_reopen:
            for parent in [row, *row.get('open_questions', [])]:
                reopen[id(parent)] = note_reopen[row['id']]
    def prioritize(node):
        if isinstance(node, dict):
            priorities[id(node)] = 7
            for child in node.values(): prioritize(child)
        elif isinstance(node, list):
            priorities[id(node)] = 7
            for child in node: prioritize(child)
    rows = result.get('tool_results', [])
    for index in requested:
        if type(index) is int and 0 <= index < len(rows): prioritize(rows[index])
    candidates.sort(key=lambda item: priorities.get(id(item[0]), 4 if id(item[0]) in pinned else 0))
    for parent, key, text in reversed(candidates):
        if used + len(text) <= full.READ_LIMIT:
            used += len(text)
        else:
            archived = {
                'sha256': hashlib.sha256(text.encode()).hexdigest(), 'chars': len(text),
                'reopen': copy.deepcopy(reopen.get(id(parent),
                    'Use this receipt\'s original read call; archived text is not delivered in this request.'))}
            if isinstance(parent, list):
                parent[key] = {'archived_source_fields': {'text': archived}}
            else:
                parent.pop(key)
                parent.setdefault('archived_source_fields', {})[key] = archived
    result['full_review_source_window'] = {'version': 1, 'visible_source_chars': used,
        'maximum_chars': full.READ_LIMIT, 'archived_receipts_preserved': True,
        'requested_tool_result_indices': requested}
    return result


def batch(allowed):
    from .engine import obj
    names = sorted(set(allowed) & {full.TOOL, 'documents.read', 'code.read', 'artifacts.read', 'materials.request_read'})
    if not names:
        return None
    return obj({'tool_calls': {'type': 'array', 'minItems': 1, 'maxItems': 8,
        'items': obj({'name': {'enum': names}, 'arguments': {'type': 'object'}})}})


def read_capacity(progress):
    """Distinct inspected ranges earn continuation, never duplicate pagination."""
    import math
    groups={}
    for row in progress.get('tool_results',[]):
        page=row.get('result',{})
        if not isinstance(page,dict) or page.get('error') or not isinstance(page.get('text'),str):continue
        call=row.get('call',{});args=call.get('arguments',{})
        start=page.get('char_start',page.get('offset'))
        if type(start) is not int or not page['text']:continue
        identity=(call.get('name'), page.get('document_id') or page.get('artifact_id')
            or page.get('source_id') or args.get('trial_id') or args.get('evidence_id'),
            page.get('sha256') or page.get('text_sha256'),args.get('path'))
        groups.setdefault(identity,[]).append((start,start+len(page['text'])))
    chars=0
    for spans in groups.values():
        cursor=-1
        for start,end in sorted(spans):
            chars+=max(0,end-max(cursor,start));cursor=max(cursor,end)
    return 8+math.ceil(chars/full.PAGE_LIMIT)
