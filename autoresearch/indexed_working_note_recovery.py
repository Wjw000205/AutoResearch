"""Repair only a proven unsent indexed-review working-note projection.

The frozen assignment and previous prepared request remain immutable evidence.
A dispatch intent is not a provider submission; any native, charge, audit, output
or uncertain submission marker prevents this narrow compatibility repair.
"""
from __future__ import annotations

import copy
import json

from .codex_reasoning import key_for
from .storage import now, uid


DIRECTORY_ERROR = '实现文档索引审核：冻结目录必须唯一且只有元数据，不能内联原文。'
HISTORY = 'indexed_working_note_recovery_history'


def _unsent(store, run_id, normal, native_key, operation):
    if store.get('codex_reasoning', native_key) is not None:
        return False
    identities = (operation, normal['operation_id'])
    ledger = store.get('budgets', run_id, {})
    if (not isinstance(ledger, dict) or not isinstance(ledger.get('pending', {}), dict)
            or not isinstance(ledger.get('records', []), list)):
        return False
    rows = [*ledger.get('pending', {}).values(), *ledger.get('records', [])]
    if any(not isinstance(row, dict) or any(row.get(key) in identities
            for key in ('operation_id', 'controller_operation_id')) for row in rows):
        return False
    related = ("(json_extract(data,'$.operation_id') IN (?,?) OR "
               "json_extract(data,'$.controller_operation_id') IN (?,?))")
    with store._read_db() as db:
        if db.execute("SELECT 1 FROM records WHERE collection='request_audits' "
                "AND json_extract(data,'$.run_id')=? AND " + related + ' LIMIT 1',
                (run_id, *identities, *identities)).fetchone():
            return False
        # Only an unbound compiled manifest can coexist with a preparation error.
        # Prepared, rejected and unknown submissions retain their original input.
        manifests = db.execute("SELECT json_extract(data,'$.status') AS status, "
            "json_extract(data,'$.submission_status') AS submission_status, "
            "json_extract(data,'$.request_id') AS request_id, "
            "json_extract(data,'$.thread_id') AS thread_id, "
            "json_extract(data,'$.turn_id') AS turn_id, "
            "json_extract(data,'$.materials') AS materials FROM records "
            "WHERE collection='context_manifests' AND json_extract(data,'$.run_id')=? AND "
            + related, (run_id, *identities, *identities)).fetchall()
        if any(row['status'] != 'compiled' or any(row[key] is not None for key in
                ('request_id', 'thread_id', 'turn_id')) or row['submission_status'] is not None
                for row in manifests):
            return False
        for row in manifests:
            if row['materials'] is not None:
                try:
                    materials = json.loads(row['materials'])
                except (ValueError, TypeError):
                    return False
                if not isinstance(materials, list) or any(not isinstance(item, dict)
                        or item.get('submitted') is not False for item in materials):
                    return False
        # Separate exact operation seeks preserve the existing partial indexes.
        for identity in identities:
            if db.execute("SELECT 1 FROM records WHERE collection='transcripts' "
                    "AND json_extract(data,'$.kind')='output' "
                    "AND json_extract(data,'$.operation_id')=? AND json_extract(data,'$.run_id')=? LIMIT 1",
                    (identity, run_id)).fetchone():
                return False
    return True


def _authorization(h, run_id, key, normal, progress):
    from .engine import Harness, digest
    store = h.engine.store
    run = store.get('runs', run_id, {})
    generation = store.get('operator_resumes', run.get('state', {}).get('resume_generation'), {})
    handoff = store.get('handoffs', key, {})
    inputs = store.get('handoff_inputs', key, {})
    payload = {field: inputs.get(field) for field in ('instruction', 'context', 'schema')}
    if (run.get('status') != 'running' or generation.get('kind') != 'explicit_start_command'
            or generation.get('run_id') != run_id or handoff.get('run_id') != run_id
            or handoff.get('stage') != run.get('stage')
            or handoff.get('to_role') != normal['prepared'].get('role')
            or generation.get('targets', {}).get(key) != Harness._resume_target(normal, progress['turn'])
            or inputs.get('run_id') != run_id or handoff.get('input_hash') != digest(payload)):
        return None
    return {'generation': copy.deepcopy(generation), 'inputs': copy.deepcopy(inputs),
            'input_hash': handoff['input_hash']}


async def recover(h, run_id, key, progress):
    """Reproject and claim one unsent continuation on an explicit resume only."""
    from .engine import ResearchBlocked, digest
    from . import implementation_indexed_review as indexed
    normal = progress.get('normal_request', {})
    prepared = normal.get('prepared', {})
    context = prepared.get('context', {})
    if (normal.get('status') != 'blocked' or normal.get('error') != DIRECTORY_ERROR
            or normal.get('provider_id') != 'local_codex' or normal.get('response') is not None
            or normal.get('turn') != progress.get('turn') or not normal.get('operation_id')
            or any(normal.get(field) for field in ('legacy_source_input_id', 'migration_of',
                'stream_retry_of', 'native_interruption_of', 'indexed_working_note_projection_of'))
            or context.get(indexed.PROTOCOL) != indexed.VERSION or indexed.CONTRACT not in context):
        return False
    if not {'role', 'task', 'schema'} <= prepared.keys():
        return False
    source_key, source_operation = key_for(h.intelligence, run_id, prepared['role'],
        prepared['task'], context, prepared['schema'])
    authorization = _authorization(h, run_id, key, normal, progress)
    if not authorization or not _unsent(h.engine.store, run_id, normal, source_key, source_operation):
        raise ResearchBlocked('Indexed working-note recovery cannot prove an authorized unsent request; original input retained')
    from .document_working_notes import project_review_context
    try:
        projected = project_review_context(h, run_id, prepared['role'], key, progress, context)
    except (ValueError, KeyError, TypeError) as exc:
        raise ResearchBlocked('Indexed working-note recovery scope or immutable source changed; original input retained') from exc
    if projected == context:
        return False
    allowed = {'indexed_documents', 'document_working_note_documents'}
    if any(projected.get(field) != context.get(field) for field in set(projected) | set(context)
            if field not in allowed):
        raise ResearchBlocked('Indexed working-note recovery changed non-directory context; original input retained')
    operation = uid()
    replacement = copy.deepcopy(prepared)
    replacement['context'] = projected
    replacement['context']['_harness_control'] = {
        **replacement['context'].get('_harness_control', {}), 'operation_id': operation}
    bound = projected[indexed.CONTRACT]['binding']
    indexed.assert_context({'id': bound['workflow_id'], 'proposal_hash': bound['proposal_hash']},
        {'index': bound['revision'], 'spec_hash': bound['spec_hash'],
         'document': {'document_id': bound['document_id'], 'sha256': bound['document_sha256']}},
        projected, role=prepared['role'])
    h.engine.check_active(run_id)
    preview = await h.engine._read_consensus_check(run_id, h.intelligence.preflight_batch, run_id, [replacement])
    if preview.get('request_count') != 1 or len(preview.get('requests', [])) != 1:
        raise ResearchBlocked('Indexed working-note recovery admission changed; nothing dispatched')
    h.engine.check_active(run_id)
    expected = copy.deepcopy(progress)
    history = progress.get(HISTORY, [])
    if not isinstance(history, list):
        raise ResearchBlocked('Indexed working-note recovery history is invalid; original input retained')
    audit = {'version': 1, 'source_normal_request': copy.deepcopy(normal),
        'source_prepared_sha256': digest(prepared), 'source_native_key': source_key,
        'source_operation_id': source_operation, 'operator_resume_generation': authorization['generation']['id'],
        'new_operation_id': operation, 'prepared_sha256': digest(replacement),
        'frozen_input_hash': authorization['input_hash'], 'created_at': now(),
        'scope': 'Unsent derived note-directory projection only; no reading or scientific approval credit'}

    def claim(current):
        # mutate holds BEGIN IMMEDIATE while these checks run, excluding another
        # writer from admitting the source between its proof and this claim.
        if (current != expected or _authorization(h, run_id, key, normal, current) != authorization
                or not _unsent(h.engine.store, run_id, normal, source_key, source_operation)):
            raise ResearchBlocked('Indexed working-note recovery changed before claim; nothing dispatched')
        current[HISTORY] = [*history, audit]
        from .tool_request_recovery import prepared_successor
        current['normal_request'] = prepared_successor(h.engine.store, key,
            {**copy.deepcopy(normal), 'prepared': replacement,
            'operation_id': operation, 'status': 'dispatching', 'error': None,
            'created_at': now(), 'dispatched_at': now(),
            'indexed_working_note_projection_of': normal['operation_id']})
        return current

    progress.update(h.engine.store.mutate('handoff_progress', key, claim))
    h.engine.store.event(run_id, 'handoff.indexed_working_notes_recovered',
        'Reprojected a proven unsent indexed-review note directory; original inputs and read receipts retained',
        {'handoff_id': key, 'role': prepared['role'], 'operator_resume_generation': authorization['generation']['id'],
         'source_operation_id': normal['operation_id'], 'new_operation_id': operation,
         'source_native_key': source_key, 'source_prepared_sha256': audit['source_prepared_sha256'],
         'prepared_sha256': audit['prepared_sha256']})
    return True
