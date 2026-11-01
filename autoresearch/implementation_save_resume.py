"""One explicit, bounded correction after a settled document save rejection.

No paid request is replayed and no author/reviewer identity is replaced. The
unsent final checkpoint is retained; only an operator resume may grant two
additional normal turns on the same save assignment. Unknown tools/requests,
changed frozen inputs and subsequent exhausted retries stay blocked.
"""
from __future__ import annotations

import copy

from . import implementation_authoring as author
from .implementation_authoring_completion import LIMIT, enabled
from .storage import now

KEY = 'implementation_document_save_resume'
VERSION = 1
EXTRA_TURNS = 2


def _settled_response(store, run_id, normal):
    """Verify the native completion and its settlement; tags are not receipts."""
    from .json_format_recovery import completed_output
    response = normal.get('response', {})
    if normal.get('provider_id') != 'local_codex':
        # Only explicitly identified offline fixtures have no paid provider.
        return (normal.get('provider_id') is None and response.get('provenance') == 'offline_test_fixture')
    metadata = response.get('response_metadata')
    tid = response.get('transcript_id')
    if (not isinstance(metadata, dict) or not completed_output(metadata)
            or not isinstance(tid, str) or not tid.endswith(':output')):
        return False
    native = store.get('codex_reasoning', tid[:-7], {})
    from .intelligence import Intelligence
    prepared = normal.get('prepared') or {}
    if prepared.get('role') != 'engineer' or not {'task', 'context', 'schema'} <= prepared.keys():
        return False
    operation = Intelligence._operation_identity(None, prepared['role'], prepared['task'],
                                                 prepared['context'], prepared['schema'])
    cp = native.get('checkpoint', {})
    transcript = store.get('transcripts', tid, {})
    reservation = native.get('reservation', {})
    ledger = store.get('budgets', run_id, {})
    records = [row for row in ledger.get('records', []) if row.get('id') == response.get('request_id')]
    if len(records) != 1:
        return False
    paid = records[0]
    expected = {'run_id': run_id, 'role': 'engineer', 'provider': 'local_codex', 'model': normal.get('model'),
                'request_id': response.get('request_id'), 'operation_id': native.get('operation_id')}
    return bool(native and native.get('id') == tid[:-7] and native.get('run_id') == run_id
        and native.get('operation_id') == operation
        and native.get('role') == 'engineer' and native.get('result') == response
        and native.get('schema') == normal.get('prepared', {}).get('schema')
        and native.get('selection', {}).get('model') == normal.get('model')
        and cp.get('status') == 'completed' and not cp.get('public_output_truncated')
        and not cp.get('error_code') and not cp.get('native_tool_violation')
        and all(transcript.get(key) == value for key, value in expected.items())
        and transcript.get('kind') == 'output' and transcript.get('text') == response.get('text')
        and transcript.get('response_metadata') == metadata and paid.get('response_metadata') == metadata
        and paid.get('status') == 'completed' and paid.get('id') == reservation.get('id')
        and paid.get('id') not in ledger.get('pending', {})
        and all(paid.get(key) == value for key, value in expected.items() if key != 'request_id')
        and paid.get('thread_id') == cp.get('thread_id') == response.get('thread_id')
        and paid.get('turn_id') == cp.get('turn_id') == response.get('turn_id'))


def target(engine, run_id, handoff_id, progress):
    """Read-only fingerprint of an eligible unsent save closeout."""
    from .engine import digest
    from .implementation_save_repair import _failure

    if (progress.get(KEY) is not None or progress.get('mode') != 'final_only'
            or type(progress.get('turn')) is not int or progress['turn'] != LIMIT
            or progress.get('pending_tool_calls') or progress.get('finalization_history')
            or engine.store.get(author.COLLECTION, handoff_id)
            or engine.store.get('decisions', handoff_id)):
        return None
    final = progress.get('finalization', {})
    normal = progress.get('normal_request', {})
    if (final.get('status') != 'ready' or final.get('dispatched_at')
            or final.get('response') is not None or final.get('prepared') is not None
            or final.get('withheld_tool_calls')
            or not final.get('operation_id') or 'tool-turn limit' not in final.get('reason', '')
            or normal.get('status') != 'response_saved' or normal.get('turn') != LIMIT - 1
            or not isinstance(normal.get('response'), dict)
            or not isinstance(normal.get('prepared'), dict)):
        return None
    inputs = engine.store.get('handoff_inputs', handoff_id, {})
    context = inputs.get('context', {})
    if not enabled(context):
        return None
    try:
        assignment, frozen, state, binding = author._assignment(engine, run_id, 'engineer', handoff_id)
    except (ValueError, RuntimeError):
        return None
    run = engine.store.get('runs', run_id, {})
    if (assignment.get('status') not in {'blocked', 'interrupted', 'running'}
            or assignment.get('stage') != 'implementation' or run.get('stage') != 'implementation'
            or state.get('status') != 'specification'
            or state.get('current_revision') != binding['revision']
            or assignment.get('input_hash') != digest({k: inputs.get(k) for k in ('instruction', 'context', 'schema')})):
        return None
    if engine.store.record_keys('tool_invocations', run_id,
            predicate="json_extract(data,'$.handoff_id')=? AND json_extract(data,'$.status') IN ('pending','running','unknown')",
            parameters=[handoff_id]) or not _settled_response(engine.store, run_id, normal):
        return None
    rows = progress.get('tool_results', [])
    if not rows or rows[-1].get('predecessor_handoff_id') is not None:
        return None
    if final.get('completed_result_hashes') != [digest(row) for row in rows]:
        return None
    row = rows[-1]
    call, result = row.get('call', {}), row.get('result', {})
    calls = normal['response'].get('json', {}).get('tool_calls', [])
    if (not isinstance(calls, list) or call not in calls
            or call.get('name') not in {author.TOOL, author.EDIT_TOOL}
            or result.get('saved') is not False or result.get('validation', {}).get('code') not in {
                'invalid_document_patch_result', 'invalid_implementation_structure', 'invalid_document_specification'}):
        return None
    failure = _failure(rows)
    if not failure or not failure['paths']:
        return None
    from .codex_workflow import SPEC
    if any(not isinstance(path, list) or not path or not isinstance(path[0], str)
           or path[0] not in SPEC['properties']
           or any(type(part) not in {str, int} for part in path) for path in failure['paths']):
        return None
    identity = context.get('implementation_document_completion', {}).get('provider_identity')
    if (identity and (normal.get('provider_id'), normal.get('model'))
            != (identity.get('provider_id'), identity.get('model'))):
        return None
    return {'version': VERSION, 'turn': LIMIT, 'workflow_id': state['id'],
        'revision': binding['revision'], 'input_sha256': digest(inputs),
        'checkpoint_sha256': digest(progress),
        'normal_request_sha256': digest(normal), 'finalization_sha256': digest(final),
        'tool_results_sha256': digest(rows), 'failure_sha256': failure['sha256'],
        'provider_identity': copy.deepcopy(identity)}


def activate(h, run_id, role, handoff_id, context, progress):
    """CAS-consume the exact explicit-start snapshot before preparing a request."""
    from .engine import ResearchBlocked, digest
    if role != 'engineer' or not enabled(context) or progress.get(KEY) is not None:
        return False
    engine = h.engine
    run = engine.store.get('runs', run_id, {})
    generation = engine.store.get('operator_resumes', run.get('state', {}).get('resume_generation', ''), {})
    expected = generation.get('document_save_targets', {}).get(handoff_id)
    if not expected:
        return False
    if (run.get('status') != 'running' or generation.get('run_id') != run_id
            or generation.get('kind') != 'explicit_start_command'
            or expected != target(engine, run_id, handoff_id, progress)):
        raise ResearchBlocked('实现文档保存续行的原回执、输入或版本已改变；未发送新请求。')
    from .implementation_authoring_completion import validate_identity
    validate_identity(h, context)
    before = copy.deepcopy(progress)
    final = copy.deepcopy(progress['finalization'])
    # Reuse the sanitized, content-addressed execution archive. Large packets
    # use its existing file/retention policy, not another large SQLite copy.
    from .execution_archive import save as archive_save
    archive_invocation = handoff_id + ':document-save-resume-source:v1'
    archive_payload = {'normal_request': copy.deepcopy(progress['normal_request'])}
    archive = archive_save(engine.store, run_id, role, archive_invocation, archive_payload,
        handoff_id=handoff_id, request_id=progress['normal_request']['response'].get('request_id'))
    audit = {'version': VERSION, 'run_id': run_id, 'handoff_id': handoff_id, 'role': role,
        'operator_resume_generation': generation['id'],
        'source': copy.deepcopy(expected), 'original_finalization': final,
        'source_archive_id': archive['id'], 'source_archive_sha256': archive['sha256'],
        'source_archive_redacted': archive.get('redacted', False),
        'turn_limit': LIMIT + EXTRA_TURNS, 'created_at': now(),
        'scientific_approval': False, 'old_request_replayed': False}
    audit['sha256'] = digest(audit)
    def consume(current):
        if current != before:
            raise ResearchBlocked('实现文档保存续行已被领取或检查点已改变；未重复派发。')
        result = {k: value for k, value in current.items() if k not in {'mode', 'finalization'}}
        result[KEY] = audit
        return result
    saved = engine.store.mutate('handoff_progress', handoff_id, consume)
    progress.clear()
    progress.update(saved)
    engine.store.event(run_id, 'implementation.document_save_resumed',
        '显式恢复后沿用原保存交接、读取和失败回执，最多两轮定向修正；原请求未重发，新稿仍须三方独立审核。',
        {'handoff_id': handoff_id, 'operator_resume_generation': generation['id'],
         'failure_sha256': expected['failure_sha256'], 'turn_limit': LIMIT + EXTRA_TURNS,
         'scientific_approval': False, 'old_request_replayed': False})
    return True


def tool_turn_limit(progress):
    """No caller-controlled extension: the sealed local audit has a fixed cap."""
    from .engine import ResearchBlocked, digest
    value = progress.get(KEY)
    if value is None:
        return LIMIT
    if (not isinstance(value, dict) or type(value.get('version')) is not int
            or value['version'] != VERSION or type(value.get('turn_limit')) is not int
            or value['turn_limit'] != LIMIT + EXTRA_TURNS
            or value.get('run_id') != progress.get('run_id') or value.get('handoff_id') != progress.get('id')
            or value.get('role') != 'engineer'
            or value.get('sha256') != digest({k: item for k, item in value.items() if k != 'sha256'})):
        raise ResearchBlocked('实现文档保存续行预算记录已改变。')
    return value['turn_limit']
