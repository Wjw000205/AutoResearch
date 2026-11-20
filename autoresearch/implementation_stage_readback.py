"""Read old implementation turns without creating or continuing a model turn."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from .document_read_convergence import digest
from .storage import now

COLLECTION = 'implementation_restart_readbacks'
TERMINAL = {'completed', 'failed', 'interrupted', 'cancelled'}
UNSUCCESSFUL_TERMINAL = TERMINAL - {'completed'}


def targets(db, run_id, handoff_ids, *, verify_empty_terminal=False):
    owned = set(handoff_ids)
    for key, raw in db.execute("SELECT key,data FROM records WHERE collection='codex_reasoning' "
                              "AND json_extract(data,'$.run_id')=? ORDER BY key", (run_id,)):
        source = json.loads(raw)
        owner = (source.get('memory_capture_binding') or {}).get('handoff_id')
        checkpoint = source.get('checkpoint') or {}
        status = checkpoint.get('status')
        if owner in owned and (status not in TERMINAL or verify_empty_terminal
                               and status in UNSUCCESSFUL_TERMINAL
                               and checkpoint.get('recovered_read_only') is not True):
            yield key, source


def key_for(run_id, source):
    return f'{run_id}:implementation-readback:{digest(source)}'


def observed_checkpoint(db, run_id, source):
    from .implementation_restart import _get
    key = key_for(run_id, source)
    return _get(db, COLLECTION, key, {}).get('observed', {})


def terminal_proof(db, run_id, source):
    from .implementation_restart import _get
    key = key_for(run_id, source)
    receipt = _get(db, COLLECTION, key, {})
    current = source.get('checkpoint') or {}
    observed = receipt.get('observed') or {}
    return bool(receipt.get('id') == key and receipt.get('run_id') == run_id
        and receipt.get('source_id') == source.get('id')
        and receipt.get('source_sha256') == digest(source)
        and receipt.get('provenance') == 'local_codex_read_original_turn'
        and receipt.get('receipt_sha256') == digest({k: v for k, v in receipt.items() if k != 'receipt_sha256'})
        and observed.get('status') in TERMINAL and observed.get('recovered_read_only') is True
        and all(observed.get(field) == current.get(field) for field in (
            'run_id', 'operation_id', 'thread_id', 'turn_id', 'workspace', 'instruction_sha256')))


def _read_inputs(engine, run_id, key, source):
    from .deletion import _plain_path
    from .engine import ResearchBlocked
    from .local_codex import LocalCodexClient
    checkpoint = source.get('checkpoint') or {}
    selection = source.get('selection') or {}
    schema = source.get('transport_schema', source.get('schema'))
    instruction = source.get('instruction')
    workspace = (engine.data_dir / 'codex-reasoning' / key / source['role']).resolve()
    _plain_path(workspace, engine.data_dir)
    if (source.get('id') != key or source.get('run_id') != run_id
            or checkpoint.get('run_id') != run_id or checkpoint.get('operation_id') != key
            or checkpoint.get('reasoning_only') is not True or checkpoint.get('version') != 1
            or not checkpoint.get('thread_id') or not checkpoint.get('turn_id')
            or not isinstance(instruction, str) or checkpoint.get('instruction_sha256') != hashlib.sha256(instruction.encode()).hexdigest()
            or checkpoint.get('workspace') != str(workspace) or checkpoint.get('project_root') != str(workspace)
            or checkpoint.get('output_schema_sha256') != hashlib.sha256(json.dumps(schema, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            or checkpoint.get('requested_model') != selection.get('model')
            or checkpoint.get('requested_effort') != selection.get('effort')
            or checkpoint.get('image_inputs_sha256')
            or LocalCodexClient._unsubmitted_initialization(checkpoint, True)):
        raise ResearchBlocked('实现阶段原会话缺少完整的只读回执身份。')
    return workspace, copy.deepcopy(checkpoint), selection, schema


async def reconcile(engine, h, run_id, handoff_ids, *, verify_empty_terminal=False):
    from .engine import ResearchBlocked
    from .local_codex import LocalCodexClient, LocalCodexError
    with engine.store._read_db() as db:
        candidates = [(key, source) for key, source in targets(db, run_id, handoff_ids,
                      verify_empty_terminal=verify_empty_terminal)
                      if not terminal_proof(db, run_id, source)]
    for key, source in candidates:
        workspace, checkpoint, selection, schema = _read_inputs(engine, run_id, key, source)
        engine.check_active(run_id)
        provider_fields = [p.get('api_key_env', '') for p in getattr(h.intelligence, 'settings', {}).get('providers', [])]
        client = LocalCodexClient(timeout=90, request_timeout=30,
            blocked_env_names=[*provider_fields, 'OPENAI_API_KEY', 'CODEX_API_KEY'])
        try:
            observed = await client.run(run_id, workspace, source['instruction'], operation_id=key,
                checkpoint=checkpoint, on_progress=lambda receipt: None, check_active=lambda: engine.check_active(run_id),
                model=selection['model'], effort=selection['effort'], reasoning_only=True, output_schema=schema)
        except LocalCodexError as exc:
            raise ResearchBlocked('实现阶段原会话只读核对失败：' + exc.code) from exc
        engine.check_active(run_id)
        if (engine.store.get('codex_reasoning', key) != source
                or any(observed.get(field) != checkpoint.get(field) for field in (
                    'run_id', 'operation_id', 'thread_id', 'turn_id', 'workspace', 'instruction_sha256'))):
            raise ResearchBlocked('实现阶段原会话身份发生变化，未更新终态核对。')
        receipt = {'id': key_for(run_id, source), 'run_id': run_id, 'source_id': key,
            'source_sha256': digest(source), 'observed': observed, 'created_at': now(),
            'provenance': 'local_codex_read_original_turn'}
        receipt['receipt_sha256'] = digest(receipt)
        engine.store.put(COLLECTION, receipt['id'], receipt)
        engine.store.event(run_id, 'implementation.restart_native_readback', '已核对实现阶段原会话，未派发新回合。',
            {'readback_id': receipt['id'], 'native_status': observed.get('status'), 'error_code': observed.get('error_code')})
    with engine.store._read_db() as db:
        unresolved = [source for _, source in targets(db, run_id, handoff_ids,
                      verify_empty_terminal=verify_empty_terminal) if not terminal_proof(db, run_id, source)]
        if unresolved:
            errors = [observed_checkpoint(db, run_id, source).get('error_code') for source in unresolved]
            raise ResearchBlocked('实现阶段原会话仍有未知终态，已保留实际只读回执：'
                                  + ', '.join(code for code in errors if isinstance(code, str)))
