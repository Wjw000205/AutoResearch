"""Frozen, read-only observability of prepared model input and its memory refs.

This is an audit view, not scientific validation, source-reading credit, or
proof that a provider received a request. Dispatch comes from separate receipts.
"""
from __future__ import annotations

import copy
import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar


_MODEL_INPUT_SCOPE = ContextVar('model_input_observation_scope', default=None)


@contextmanager
def handoff_scope(run_id, role, handoff_id):
    """Host-only association; never add observability fields to model input."""
    token = _MODEL_INPUT_SCOPE.set((run_id, role, handoff_id))
    try:
        yield
    finally:
        _MODEL_INPUT_SCOPE.reset(token)


def current_handoff_id(run_id, role):
    scope = _MODEL_INPUT_SCOPE.get()
    if (not isinstance(scope, tuple) or len(scope) != 3 or scope[:2] != (run_id, role)
            or not isinstance(scope[2], str) or not scope[2]):
        return None
    return scope[2]


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def sha_text(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def render_active_v1(active):
    """Frozen serializer recipe; never rebuild historical prompts from code."""
    order = ('objective', 'task', 'evidence_policy', 'context', 'retrieved_memories',
             'recent_turns', 'context_snippets', 'compaction')
    fields = [key for key in order if key in active] + sorted(set(active) - set(order))
    rendered = []
    for key in fields:
        value = active[key]
        if key == 'context' and isinstance(value, dict):
            tail = ('tool_results', 'current_date', '_harness_control')
            names = sorted(set(value) - set(tail)) + [name for name in tail if name in value]
            body = '{' + ','.join(_json(name) + ':' + _json(value[name]) for name in names) + '}'
        else:
            body = _json(value)
        rendered.append(_json(key) + ':' + body)
    return '{' + ','.join(rendered) + '}'


class PreparedContext(tuple):
    """Preserve the original three-item context API without ambient state."""
    def __new__(cls, system, user, cap, binding=None):
        result = super().__new__(cls, (system, user, cap))
        result.memory_capture_binding = copy.deepcopy(binding)
        return result


class PreparedInstruction(tuple):
    def __new__(cls, instruction, cap, binding=None):
        result = super().__new__(cls, (instruction, cap))
        result.memory_capture_binding = copy.deepcopy(binding)
        return result


def _ids(rows):
    return list(dict.fromkeys(row['id'] for row in rows if isinstance(row, dict)
                              and isinstance(row.get('id'), str)))


def _tool_refs(active):
    context = active.get('context') or {}
    rows = context.get('tool_results', []) if isinstance(context, dict) else []
    refs = []
    for index, row in enumerate(rows if isinstance(rows, list) else []):
        if not isinstance(row, dict):
            continue
        call = row.get('call') or {}
        call = call if isinstance(call, dict) else {}
        result = row.get('result')
        ref = {'index': index, 'tool': call.get('name'), 'receipt_sha256': sha_text(_json(row)),
               'call_sha256': sha_text(_json(call)) if call else None,
               'representation': 'receipt_in_context'}
        if isinstance(result, dict):
            for name in ('receipt_id', 'artifact_id', 'document_id', 'source_id', 'paper_id',
                         'rag_version_id', 'sha256', 'section', 'offset', 'limit'):
                if name in result:
                    ref[name] = copy.deepcopy(result[name])
            if result.get('primary_text_displayed') is False:
                ref['representation'] = 'projected_receipt'
            for name in ('source_metadata_ref', 'text_ref'):
                if isinstance(result.get(name), str):
                    ref[name] = result[name]
                    ref['representation'] = 'shared_context_reference'
        if call.get('name') == 'memory.search' and isinstance(result, list):
            ref['memory_ids'] = _ids(result)
        if row.get('catalog_ref'):
            ref['catalog_ref'] = row['catalog_ref']
            ref['representation'] = 'reference_only'
        refs.append(ref)
    return refs


def make_capture(*, run_id, role, operation_id, transcript_id, snapshot_id,
                 provider, system, user, original_active, active, history, clean,
                 handoff_id=None, native_envelope=None):
    """Capture identities from both sides of compilation, without model writes."""
    scoped_history = [row for row in history
        if row.get('run_id') == run_id and row.get('role') == role]
    clean_active = clean(active)
    # user is this exact JSON object. The request redactor canonicalizes JSON
    # only after a change, so a second parse/copy of large evidence is avoidable.
    clean_user = _json(clean_active) if clean_active != active else user
    clean_system = clean(system)
    if render_active_v1(clean_active) == clean_user:
        user_format = 'active-json-v1'
    elif _json(clean_active) == clean_user:
        user_format = 'canonical-json-v1'
    else:
        raise ValueError('Memory capture cannot bind serialized user input')
    original_context = original_active.get('context') or {}
    original_tools = _tool_refs(clean({'context': {'tool_results':
        original_context.get('tool_results', []) if isinstance(original_context, dict) else []}}))
    effective_tools = _tool_refs(clean_active)
    by_call = {}
    for original in original_tools:
        if original['call_sha256']:
            by_call.setdefault(original['call_sha256'], []).append(original)
    for effective in effective_tools:
        originals = by_call.get(effective['call_sha256'], [])
        if len(originals) == 1:
            effective['original_receipt_sha256'] = originals[0]['receipt_sha256']
            if effective['receipt_sha256'] != originals[0]['receipt_sha256']:
                effective['representation'] = 'projected_receipt'
    initial_memories = _ids(original_active.get('retrieved_memories', []))
    effective_memories = _ids(active.get('retrieved_memories', []))
    capture = {'version': 1, 'run_id': run_id, 'role': role, 'operation_id': operation_id,
        'input_transcript_id': transcript_id, 'context_snapshot_id': snapshot_id,
        'provider': provider.get('id'), 'model': provider.get('model'),
        'capture_stage': 'prepared', 'dispatch_status': 'prepared_only',
        'system_text': clean_system, 'system_sha256': sha_text(clean_system),
        'user_format': user_format, 'user_sha256': sha_text(clean_user),
        'original_user_sha256': sha_text(user), 'redacted': True,
        'history_ids': _ids(scoped_history),
        'effective_history_ids': [key for key in _ids(active.get('recent_turns', []))
                                  if key in _ids(scoped_history)],
        'retrieved_memory_ids': list(dict.fromkeys(initial_memories + [mid for ref in original_tools
            for mid in ref.get('memory_ids', [])])),
        'effective_memory_ids': list(dict.fromkeys(effective_memories + [mid for ref in effective_tools
            for mid in ref.get('memory_ids', [])])),
        'tool_result_refs': original_tools, 'effective_tool_result_refs': effective_tools,
        'usage_definition': 'Included in this prepared input; not proof of dispatch, model attention, '
                            'scientific validation or primary-source reading.'}
    unbound = [key for key in _ids(active.get('recent_turns', [])) if key not in _ids(scoped_history)]
    if unbound:
        capture['unbound_history_ids'] = unbound
    if handoff_id:
        capture['handoff_id'] = handoff_id
    if native_envelope is not None:
        capture['native_envelope_text'] = native_envelope
        capture['original_instruction_sha256'] = sha_text(system + '\n\n' + user + native_envelope)
    # The existing snapshot holds active_context. Duplicate it only when secret
    # removal changes its structure; the original request remains unchanged.
    if clean_active != active:
        capture['redacted_active_context'] = clean_active
    return capture


def capture_binding(capture):
    return {name: capture[name] for name in ('version', 'run_id', 'role', 'operation_id',
        'input_transcript_id', 'context_snapshot_id', 'handoff_id', 'system_sha256', 'user_sha256',
        'original_user_sha256', 'original_instruction_sha256')
        if name in capture}


def read_captured_input(snapshot, run_id, role):
    """Return frozen redacted system/user; reject mismatched or altered records."""
    capture = snapshot.get('model_input_capture')
    if not isinstance(capture, dict):
        return None  # Legacy data does not contain a frozen system prompt.
    if (capture.get('version') != 1 or snapshot.get('run_id') != run_id
            or snapshot.get('role') != role or capture.get('run_id') != run_id
            or capture.get('role') != role or capture.get('context_snapshot_id') != snapshot.get('id')
            or capture.get('input_transcript_id') != snapshot.get('original_transcript_id')):
        raise ValueError('Model input capture binding mismatch')
    active = capture.get('redacted_active_context', snapshot.get('active_context'))
    if not isinstance(active, dict):
        raise ValueError('Model input capture context missing')
    if capture.get('user_format') == 'active-json-v1':
        user = render_active_v1(active)
    elif capture.get('user_format') == 'canonical-json-v1':
        user = _json(active)
    else:
        raise ValueError('Unknown frozen model input serializer')
    system = capture.get('system_text')
    if (not isinstance(system, str) or sha_text(system) != capture.get('system_sha256')
            or sha_text(user) != capture.get('user_sha256')):
        raise ValueError('Model input capture text hash mismatch')
    return {'system': system, 'user': user, 'active_context': copy.deepcopy(active),
            'capture': copy.deepcopy(capture), 'binding': capture_binding(capture),
            'capture_stage': 'compiled_snapshot', 'dispatch_status': 'prepared_only', 'redacted': True}


def native_dispatch_status(record):
    """Only exact native receipt markers establish submission/completion."""
    checkpoint = record.get('checkpoint') or {}
    if not isinstance(checkpoint, dict):
        return 'unknown'
    if (checkpoint.get('run_id') != record.get('run_id')
            or checkpoint.get('operation_id') != record.get('id')):
        return 'unknown' if checkpoint else 'prepared_only'
    thread, turn = checkpoint.get('thread_id'), checkpoint.get('turn_id')
    if thread and turn and checkpoint.get('status') == 'completed':
        return 'completed'
    if thread and (turn or checkpoint.get('turn_dispatched_at')):
        return 'submitted'
    return 'prepared_only'
