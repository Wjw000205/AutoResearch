"""Confirm a reasoning request stopped before turn/start, including old receipts."""
import hashlib
import json

from .local_codex import RECEIPT_FIELDS, LocalCodexClient, _valid_initialization_diagnostic


def eligible_checkpoint(cp):
    if (not isinstance(cp, dict) or cp.get('version') != 1 or set(cp) - RECEIPT_FIELDS
            or cp.get('reasoning_only') is not True or cp.get('auth_mode') != 'chatgpt'
            or cp.get('status') not in {'prepared', 'unknown', 'blocked'}
            or cp.get('thread_id', False) is not None or cp.get('turn_id', False) is not None
            or cp.get('public_messages') != [] or cp.get('usage', False) is not None
            or any(k in cp for k in ('turn_dispatched_at', 'project_id', 'model_provider', 'model',
                'reported_model', 'reported_effort', 'model_reporting_scope', 'usage_scope',
                'usage_observed_at', 'activities', 'event_counts', 'last_event_at', 'server_errors',
                'interrupt_requested', 'native_tool_violation', 'non_retryable_server_error',
                'public_output_truncated', 'output_schema_policy', 'effective_output_schema_sha256'))
            or cp.get('recovered_read_only')):
        return False
    diag = cp.get('initialization_diagnostic')
    failure = cp.get('first_failure')
    errors = cp.get('rpc_errors', [])
    if not isinstance(errors, list) or any(not isinstance(error, dict)
            or error.get('method') not in {'initialize', 'account/read', 'model/list'} for error in errors):
        return False
    if (not _valid_initialization_diagnostic(diag) or diag['stage'] != 'authenticated'
            or diag['category'] != 'ready'):
        return False
    # Written with auth success and removed in the durable pre-thread marker.
    # A crash or cancellation while listing models cannot have sent the input.
    if cp.get('model_catalog_pending') is True and diag['next_action'] == 'continue':
        return not cp.get('recovery_history')
    if (not isinstance(failure, dict)
            or failure.get('thread_id', False) is not None or failure.get('turn_id', False) is not None):
        return False
    code = failure.get('error_code')
    history = cp.get('recovery_history', [])
    if not isinstance(history, list) or any(not isinstance(row, dict)
            or row.get('thread_id', False) is not None or row.get('turn_id', False) is not None
            or row.get('previous_error_code') not in (code, 'codex_dispatch_without_thread_id')
            for row in history):
        return False
    if cp.get('error_code') != ('codex_dispatch_without_thread_id' if history else code):
        return False
    if code in ('codex_selected_model_unavailable', 'codex_selected_effort_unsupported'):
        # The first model choice runs before thread/start. The second choice
        # requires an already persisted thread_ready receipt, excluded above.
        return failure.get('status') == 'blocked' and cp['status'] == 'blocked'
    if code != 'codex_cancelled_result_unknown' or failure.get('status') != 'unknown':
        return False
    # Legacy sibling cancelled during the authenticated retry of a proven
    # pre-auth initialization. The writer persists the thread ID and then a
    # turn_dispatched_at marker before it can hand the instruction to Codex.
    audit = cp.get('initialization_recovery')
    original = audit.get('source_checkpoint') if isinstance(audit, dict) else None
    return (LocalCodexClient._unsubmitted_initialization(original, True)
            and audit.get('operation_id') == cp.get('operation_id')
            and all(original.get(k) == cp.get(k) for k in ('run_id', 'operation_id', 'workspace',
                'project_root', 'instruction_sha256', 'output_schema_sha256', 'image_inputs_sha256',
                'requested_model', 'requested_effort', 'first_failure'))
            and audit.get('source_sha256') == hashlib.sha256(json.dumps(original,
                sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest())


def confirmed(store, saved):
    """Cross-check independent submission, budget and output records before reuse."""
    from .context_manifest import _sha
    cp = saved.get('checkpoint')
    reservation = saved.get('reservation') or {}
    rid, operation = saved['run_id'], saved['operation_id']
    request = reservation.get('id')
    if (not eligible_checkpoint(cp) or saved.get('result') is not None or not request
            or cp.get('run_id') != rid or cp.get('operation_id') != saved['id']
            or reservation.get('provider') != 'local_codex' or reservation.get('operation_id') != operation
            or reservation.get('run_id') != rid or reservation.get('role') != saved['role']):
        return False
    manifest = store.get('context_manifests', request) or {}
    binding = saved.get('memory_capture_binding') or {}
    if (manifest.get('request_id') != request or manifest.get('run_id') != rid
            or manifest.get('role') != saved['role'] or manifest.get('operation_id') != operation
            or manifest.get('status') != 'prepared' or manifest.get('submission_status') != 'prepared'
            or manifest.get('thread_id') or manifest.get('turn_id')
            or manifest.get('wire_sha256') != cp.get('instruction_sha256')
            or binding.get('original_instruction_sha256') != manifest.get('wire_sha256')
            or manifest.get('receipt_sha256') != _sha({k: v for k, v in manifest.items() if k != 'receipt_sha256'})):
        return False
    budget = store.get('budgets', rid, {})
    return (budget.get('pending', {}).get(request) == reservation
            and not any(row.get('id') == request for row in budget.get('records', []))
            and not store.has_output_transcript(rid, request_id=request, operation_id=operation))
