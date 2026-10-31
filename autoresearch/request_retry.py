"""Explicit, single-dispatch consent for terminal unknown-charge final requests
and one verified buffered-524 to Responses-stream migration.

Preview is read-only. Authorization uses one SQLite transaction, archives every
old finalization, and does not start the worker or reset its billing/history.
Consent binds the visible memory at preview time. The original handoff, schema,
and prepared context stay frozen (except the controller operation ID); actual
inference still assembles current RAG/history and applies the normal budget and
compression gates. This is not a byte-for-byte replay of an old HTTP body.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import sqlite3
from contextlib import contextmanager

from .config import get_settings
from .intelligence import ProviderError, is_moderation_error, _redact_request_body, _hash as _request_hash
from .storage import Store, now, uid
from .record_reads import iter_records, hash_records, output_exists, receipt_headers, receipt_source_store


UNKNOWN = {'network_error_unknown_charge', 'cancelled_unknown_charge', 'interrupted_unknown_charge'}


class RequestRetryError(ValueError):
    pass


NATIVE_PENDING_REASON = ('本机 Codex 预留记录待核对；这些记录不代表请求仍在运行。'
                         '请使用顶部“恢复执行”，系统会在继续相关交接时读取原会话。'
                         '当前不能授权最终答复重试，原回执与未知用量保持保留。')


def _native_pending_only(pending):
    return (isinstance(pending, dict) and bool(pending)
            and all(isinstance(row, dict) and row.get('provider') == 'local_codex'
                    for row in pending.values()))


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


class _SnapshotStore(Store):
    """All preflight reads share one transaction; any attempted write fails."""
    _memory_search_scan_visible = True
    def __init__(self, connection, path):
        self.connection, self.path = connection, path

    @contextmanager
    def _db(self):
        yield self.connection

    _read_db = _db

    def _deny(self, *args, **kwargs):
        raise RequestRetryError('Request retry preview cannot modify stored records')

    put = mutate = batch = event = delete = _deny

    def memory_needing_index(self, version):
        return []  # A read-only preflight cannot migrate persistent indexes.

    def memory_candidates(self, terms, run_id=None):
        # memory_search recomputes the tokenizer and exact overlap itself.
        # Scan visible rows so legacy, not-yet-indexed memory is not omitted.
        return super().memory_candidates([], run_id)


def _identity(provider):
    return {field: provider.get(field) for field in ('id', 'model', 'protocol', 'base_url', 'api_key_env')}


def _next_prepared(final, operation_id):
    prepared = copy.deepcopy(final['prepared'])
    prepared['context']['_harness_control']['operation_id'] = operation_id
    return prepared


def _streaming_migration_used(view, run_id, key, progress):
    """A migration is spent when authorized, even if its next outcome is unknown."""
    if any(not isinstance(item, dict) or item.get('streaming_migration') for item in
           [progress.get('finalization', {}), *progress.get('finalization_history', [])]):
        return True
    return any(not isinstance(target, dict) or target.get('handoff_id') == key
               and target.get('recovery_kind') == 'streaming_gateway_timeout'
               for saved in view.list('operator_resumes', run_id)
               if saved.get('kind') == 'explicit_request_retry_confirmation'
               for target in saved.get('targets', []))


def _assert_idle(engine, view, run_id):
    if view.get('run_deletions', run_id):
        raise RequestRetryError('Research task deletion is in progress or already completed')
    run = view.get('runs', run_id)
    if not run:
        raise RequestRetryError('Run not found')
    if run.get('status') not in {'blocked', 'paused'}:
        raise RequestRetryError('Only a blocked or paused research task can authorize a request retry')
    if any(not task.done() for task in engine.tasks.values()):
        raise RequestRetryError('A research worker is still active; wait for it to stop')
    lease = view.get('runtime', 'worker', {})
    if lease.get('held') and engine.owner_alive(lease):
        raise RequestRetryError('A research worker holds the workstation execution lease')
    if run.get('owner_instance') != engine.instance_id and engine.owner_alive(run):
        raise RequestRetryError('Another process still owns this research task')
    return run


def _derive(engine, view, run_id):
    from .engine import Harness

    run = _assert_idle(engine, view, run_id)
    if run.get('mode') != 'live':
        raise RequestRetryError('Only canonical provider requests qualify for explicit request retry')
    settings = get_settings(view)
    intelligence = engine.intelligence_factory(view, settings)
    budget = view.get('budgets', run_id, {})
    if budget.get('pending'):
        if _native_pending_only(budget['pending']):
            raise RequestRetryError(NATIVE_PENDING_REASON)
        raise RequestRetryError('Provider requests are still pending; their receipts must settle first')
    billing = intelligence.billing(run_id)['records']
    reviews = view.list('moderation_reviews', run_id)
    if (any(row.get('review_status') == 'pending' for row in reviews)
            or any(is_moderation_error(row.get('error_details')) for row in billing)):
        raise RequestRetryError('Content moderation requires review; this retry cannot bypass that gate')
    records = budget.get('records', [])
    if not isinstance(records, list) or any(not isinstance(row, dict) or not row.get('id') for row in records):
        raise RequestRetryError('Canonical billing receipts are incomplete')
    if len({row['id'] for row in records}) != len(records):
        raise RequestRetryError('Canonical billing request IDs are inconsistent')
    evidence, targets, prepared_requests = [], [], []
    cycle = run.get('state', {}).get('research_cycle', 1)
    for handoff in iter_records(view, 'handoffs', run_id,
            where="json_extract(data,'$.status') IN ('blocked','interrupted') AND json_extract(data,'$.stage')=?",
            args=(run.get('stage'),), order_by="json_extract(data,'$.id')"):
        if handoff.get('status') not in {'blocked', 'interrupted'} or handoff.get('stage') != run.get('stage'):
            continue
        key = handoff['id']
        progress = view.get('handoff_progress', key, {})
        final = progress.get('finalization', {})
        if _streaming_migration_used(view, run_id, key, progress):
            continue
        if progress.get('mode') != 'final_only' or final.get('status') not in {'blocked', 'dispatching'}:
            continue
        assignment = view.get('handoff_inputs', key, {})
        context = assignment.get('context', {})
        if context.get('research_cycle', 1) != cycle:
            continue
        prepared = final.get('prepared')
        role = handoff.get('to_role')
        if (view.get('decisions', key) or final.get('response') is not None
                or not isinstance(prepared, dict) or not isinstance(prepared.get('context'), dict)
                or prepared.get('role') != role or prepared.get('schema') != assignment.get('schema')
                or prepared['context'].get('_harness_control', {}).get('mode') != 'final_only'
                or prepared['context']['_harness_control'].get('operation_id') != final.get('operation_id')):
            continue
        identity = Harness.decision_key(run_id, role, handoff['purpose'], assignment.get('instruction'),
                                        context, assignment.get('schema'))
        if identity != key or handoff.get('input_hash') != _hash({field: assignment.get(field) for field in ('instruction', 'context', 'schema')}):
            continue
        operation = intelligence._operation_identity(role, prepared['task'], prepared['context'], prepared['schema'])
        candidates = [row for row in records if row.get('operation_id') == operation
                      or row.get('controller_operation_id') == final['operation_id']]
        http_prefix = None
        streaming_source = None
        terminal_candidates = candidates
        if any(row.get('status') not in UNKNOWN for row in candidates):
            from .streaming_http_recovery import derive_streaming_source
            source_store = receipt_source_store(view, intelligence, run_id, progress, records)
            streaming_source = derive_streaming_source(source_store, intelligence, run_id, key, progress)
            if streaming_source is None:
                from .final_http_retry import verified_unknown_suffix
                http_prefix = verified_unknown_suffix(source_store, intelligence, run_id, key, progress, candidates, UNKNOWN)
                if http_prefix is None:
                    continue
                terminal_candidates = [row for row in candidates if row['id'] not in http_prefix['previous_http_request_ids']]
        if not candidates or (streaming_source is None and any(
            row.get('run_id') != run_id or row.get('role') != role or row.get('operation_id') != operation
            or row.get('controller_operation_id') != final['operation_id'] or row.get('status') not in UNKNOWN
            or row.get('http_status') is not None or not row.get('created_at') or not row.get('completed_at')
            or row.get('provenance') != 'live_provider' or row.get('cost_estimated') is not True
            or not isinstance(row.get('usage'), dict) or type(row.get('cost_usd')) not in (int, float)
            or not math.isfinite(row['cost_usd']) or row['cost_usd'] < 0
            for row in terminal_candidates
        )):
            continue
        if output_exists(view, run_id, operation, request_ids=tuple(item['id'] for item in candidates)):
            continue
        identities = {(row.get('provider'), row.get('model'), row.get('request_output_token_limit')) for row in candidates}
        if len(identities) != 1:
            continue
        provider_id, model, cap = identities.pop()
        if (not provider_id or not model or type(cap) is not int or cap <= 0
                or provider_id != final.get('provider_id') or model != final.get('model')):
            continue
        provider = intelligence.provider_for(role)
        if provider['id'] != provider_id or provider['model'] != model:
            raise RequestRetryError('The original provider and model must remain selected for every retry target')
        endpoint, _, _ = intelligence._wire(provider, role, '', '', cap)
        audits = [view.get('request_audits', row['id'], {}) for row in candidates]
        if any(audit.get('request_id') != row['id'] or audit.get('id') != row['id']
               or any(audit.get(field) != row.get(field) for field in
                      ('run_id', 'role', 'provider', 'model', 'operation_id', 'controller_operation_id'))
               or audit.get('protocol') != provider.get('protocol') or audit.get('endpoint') != endpoint
               for row, audit in zip(candidates, audits)):
            raise RequestRetryError('Original request audit does not establish the selected provider channel')
        candidate = _next_prepared(final, _hash(['request-retry-preview', final['operation_id']])[:32])
        prepared_requests.append({**candidate, 'cache': False, 'max_attempts': 1})
        target = {'handoff_id': key, 'role': role, 'provider': provider_id, 'model': model,
                        'request_ids': [row['id'] for row in candidates],
                        'last_error': _redact_request_body(str(final.get('error') or handoff.get('error') or 'Unknown provider charge'),
                                                           intelligence._provider_secrets())[:1200],
                        'unknown_cost_usd': round(sum(row['cost_usd'] for row in terminal_candidates), 10),
                        'output_token_limit': cap, **(http_prefix or {})}
        if streaming_source:
            target.update({field: streaming_source[field] for field in ('recovery_kind', 'source_transport',
                'destination_transport', 'source_request_ids', 'previous_http_request_ids', 'previous_http_cost_usd',
                'estimated_cost_usd', 'unknown_cost_usd')})
        targets.append(target)
        evidence.append({'handoff': handoff, 'assignment': assignment, 'progress': progress,
                         'receipts': candidates, 'request_audits_sha256': _hash(audits),
                         'provider_identity': _identity(provider),
                         **({'streaming_migration': streaming_source} if streaming_source else {})})
    if not targets:
        raise RequestRetryError('No terminal final request has complete canonical unknown-charge receipts without a saved output')
    preflight = intelligence.preflight_batch(run_id, prepared_requests)
    if len(preflight['requests']) != len(targets) or any(
        item['max_output_tokens'] != target['output_token_limit']
        for item, target in zip(preflight['requests'], targets)
    ):
        raise RequestRetryError('Retry must preserve every original effective output limit')
    # Whole budget and task state bind late receipts, changed limits, and graph
    # progress. Settings are hashed, never copied into the authorization record.
    fingerprint = {'version': 1, 'run': run, 'budget': budget, 'targets': evidence,
                   'settings_sha256': _hash(intelligence.settings), 'reviews': reviews,
                   'transcripts_sha256': hash_records(iter_records(view, 'transcripts', run_id)),
                   'visible_memory_sha256': hash_records(row for row in iter_records(view, 'memory')
                       if row.get('run_id') in {run_id, None, 'global'} or row.get('metadata', {}).get('shared')),
                   'lease': view.get('runtime', 'worker', {}),
                   'decisions_sha256': hash_records(iter_records(view, 'decisions', run_id))}
    snapshot_id = _hash(fingerprint)
    public = {'run_id': run_id, 'eligible': True, 'snapshot_id': snapshot_id, 'targets': targets,
              'reason': 'Explicit confirmation authorizes one attempt per listed request. Historical unknown charges remain estimated; ordinary resume does not grant another retry.',
              'estimated_new_reservation_usd': preflight['reserved_usd']}
    if any(target.get('recovery_kind') == 'streaming_gateway_timeout' for target in targets):
        public['reason'] = ('已核验非流式524失败链；明确确认后仅授权一次流式迁移续答，仍需点击恢复执行。'
                            '原请求与未核实费用全部保留，新请求可能再次计费；失败后不会自动重发或再次授权迁移。')
    return public, evidence, intelligence


def _ready_authorizations(view, run_id):
    """Project persisted consent, not readiness to bypass execution gates."""
    run = view.get('runs', run_id, {})
    if (run.get('status') not in {'blocked', 'paused'} or run.get('mode') != 'live'
            or view.get('run_deletions', run_id)):
        return []
    cycle = run.get('state', {}).get('research_cycle', 1)
    budget = view.get('budgets', run_id, {})
    dispatched = [*budget.get('records', []), *budget.get('pending', {}).values(),
                  *receipt_headers(view, 'usage', run_id), *receipt_headers(view, 'request_audits', run_id)]
    projected = []
    for saved in sorted(view.list('operator_resumes', run_id), key=lambda row: (row.get('created_at', ''), row['id'])):
        identifier, snapshot_id = saved.get('id'), saved.get('snapshot_id')
        targets = saved.get('targets')
        if (saved.get('kind') != 'explicit_request_retry_confirmation'
                or saved.get('acknowledge_duplicate_charge') is not True
                or not isinstance(snapshot_id, str) or len(snapshot_id) != 64
                or identifier != f'{run_id}:request-retry:{snapshot_id}'
                or not isinstance(targets, list) or not targets
                or any(not isinstance(item, dict) or not isinstance(item.get('handoff_id'), str) for item in targets)
                or len({item['handoff_id'] for item in targets}) != len(targets)):
            continue
        ready = []
        for target in targets:
            key = target['handoff_id']
            handoff = view.get('handoffs', key, {})
            assignment = view.get('handoff_inputs', key, {})
            progress = view.get('handoff_progress', key, {})
            final = progress.get('finalization', {})
            context, prepared = assignment.get('context'), final.get('prepared')
            role, operation = target.get('role'), target.get('operation_id')
            if (any(row.get('run_id') != run_id for row in (handoff, assignment, progress))
                    or handoff.get('stage') != run.get('stage')
                    or handoff.get('status') not in {'blocked', 'interrupted'}
                    or not isinstance(context, dict) or context.get('research_cycle', 1) != cycle
                    or handoff.get('to_role') != role or role not in {'director', 'engineer', 'scout'}
                    or progress.get('mode') != 'final_only' or final.get('status') != 'ready'
                    or not isinstance(operation, str) or not operation or final.get('operation_id') != operation
                    or final.get('request_retry_authorization') != identifier
                    or any(field in final for field in ('dispatched_at', 'received_at', 'completed_at'))
                    or final.get('response') is not None or view.get('decisions', key)
                    or not isinstance(prepared, dict) or not isinstance(prepared.get('context'), dict)
                    or prepared.get('role') != role or not isinstance(prepared.get('task'), str)
                    or prepared.get('schema') != assignment.get('schema')
                    or _hash(prepared) != target.get('prepared_sha256')
                    or final.get('provider_identity') != target.get('provider_identity')
                    or final.get('provider_id') != target.get('provider') or final.get('model') != target.get('model')
                    or final.get('effective_output_limit') != target.get('output_token_limit')):
                continue
            control = prepared['context'].get('_harness_control')
            fingerprint = _hash({field: assignment.get(field) for field in ('instruction', 'context', 'schema')})
            if (not isinstance(control, dict) or control.get('mode') != 'final_only'
                    or control.get('operation_id') != operation or handoff.get('input_hash') != fingerprint
                    or key != f"{run_id}:{handoff.get('purpose')}:{role}:{fingerprint}"):
                continue
            previous = [item for item in progress.get('finalization_history', [])
                        if isinstance(item, dict) and item.get('operation_id') == target.get('previous_operation_id')]
            if (len(previous) != 1 or final.get('confirmed_retry_of') != target.get('previous_operation_id')
                    or _hash(previous[0]) != target.get('previous_finalization_sha256')):
                continue
            if (target.get('recovery_kind') == 'streaming_gateway_timeout'
                    and (not final.get('streaming_migration')
                         or final['streaming_migration'] != target.get('streaming_migration'))):
                continue
            request_operation = _request_hash([role, prepared['task'], prepared['context'], prepared['schema']])
            if (any(row.get('operation_id') == request_operation or row.get('controller_operation_id') == operation
                    for row in dispatched)
                    or output_exists(view, run_id, request_operation, controller_operation_id=operation)):
                continue
            ready.append({'handoff_id': key, 'role': role, 'operation_id': operation})
        if ready:
            projected.append({'run_id': run_id, 'authorization_id': identifier, 'snapshot_id': snapshot_id,
                              'created_at': saved.get('created_at'), 'status': 'authorized', 'requires_resume': True,
                              'target_count': len(targets), 'ready_target_count': len(ready), 'ready_targets': ready})
    return projected


def _normal_recovery(engine, view, run_id):
    """Read-only guidance for the existing explicit-resume recovery path.

    This neither creates consent nor changes a normal request into final-only.
    The existing worker performs that one-shot transition only after Resume.
    All qualification and advisory budget reads share the preview transaction.
    """
    from .engine import Harness

    def unavailable(reason):
        return {'available': False, 'next_action': None, 'targets': [], 'reason': reason}

    try:
        run = _assert_idle(engine, view, run_id)
    except RequestRetryError:
        return unavailable('当前任务状态、删除状态或工作进程租约不允许恢复；请先等待任务停止。')
    if run.get('mode') != 'live':
        return unavailable('仅真实供应商请求的完整失败回执可以申请单次续答。')
    try:
        intelligence = engine.intelligence_factory(view, get_settings(view))
        budget = view.get('budgets', run_id, {})
        records = budget.get('records')
        if (budget.get('id') != run_id or budget.get('run_id') != run_id
                or not isinstance(records, list) or not isinstance(budget.get('pending'), dict)
                or any(not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id'] for row in records)
                or len({row['id'] for row in records}) != len(records)):
            return unavailable('缺少完整、一致的任务费用账本，不能确认普通请求可恢复。')
        if budget['pending']:
            if _native_pending_only(budget['pending']):
                return unavailable(NATIVE_PENDING_REASON)
            return unavailable('仍有未结算的请求，请等待回执结算后再检查恢复条件。')
        if (any(row.get('review_status') == 'pending' for row in view.list('moderation_reviews', run_id))
                or any(is_moderation_error(row.get('error_details'))
                       for row in intelligence.billing(run_id)['records'])):
            return unavailable('存在内容审核门禁；普通恢复不能绕过审核。')
        snapshot_engine = copy.copy(engine)
        snapshot_engine.store = view
        harness = Harness(snapshot_engine, intelligence)
        cycle = run.get('state', {}).get('research_cycle', 1)
        targets, requests = [], []
        for handoff in iter_records(view, 'handoffs', run_id,
                where="json_extract(data,'$.status') IN ('blocked','interrupted') AND json_extract(data,'$.stage')=?",
                args=(run.get('stage'),), order_by="json_extract(data,'$.id')"):
            key, role = handoff['id'], handoff.get('to_role')
            if (handoff.get('status') not in {'blocked', 'interrupted'}
                    or handoff.get('stage') != run.get('stage') or role not in {'director', 'engineer', 'scout'}):
                continue
            progress = view.get('handoff_progress', key, {})
            normal = progress.get('normal_request', {})
            assignment = view.get('handoff_inputs', key, {})
            context, prepared = assignment.get('context'), normal.get('prepared')
            if (any(row.get('run_id') != run_id for row in (handoff, progress, assignment))
                    or progress.get('mode') == 'final_only' or progress.get('finalization')
                    or progress.get('finalization_history') or progress.get('normal_migrations')
                    or progress.get('pending_tool_calls') or normal.get('migration_of')
                    or normal.get('legacy_source_input_id')
                    or not isinstance(normal.get('provider_id'), str) or not normal['provider_id']
                    or not isinstance(normal.get('model'), str) or not normal['model']
                    or normal.get('status') not in {'blocked', 'dispatching'}
                    or type(progress.get('turn')) is not int or progress['turn'] < 0
                    or normal.get('turn') != progress['turn'] or normal.get('response') is not None
                    or any(normal.get(field) for field in ('received_at', 'completed_at', 'recovered_at'))
                    or not isinstance(normal.get('operation_id'), str) or not normal['operation_id']
                    or not isinstance(normal.get('dispatched_at'), str)
                    or view.get('decisions', key) or not isinstance(context, dict)
                    or context.get('research_cycle', 1) != cycle
                    or not isinstance(assignment.get('instruction'), str) or not isinstance(assignment.get('schema'), dict)
                    or not isinstance(prepared, dict) or not isinstance(prepared.get('context'), dict)
                    or not isinstance(prepared.get('task'), str) or prepared.get('role') != role
                    or not isinstance(progress.get('tool_results'), list)):
                continue
            fingerprint = _hash({field: assignment[field] for field in ('instruction', 'context', 'schema')})
            if (key != Harness.decision_key(run_id, role, handoff['purpose'], assignment['instruction'], context, assignment['schema'])
                    or handoff.get('input_hash') != fingerprint
                    or progress['turn'] >= Harness._tool_turn_limit(handoff['purpose'], context, progress)):
                continue
            expected = Harness.prepare(role, handoff['purpose'], assignment['instruction'], context,
                                       assignment['schema'], progress['tool_results'])
            if any('output_limit' in item for item in progress['tool_results']):
                expected['context']['_output_token_limit'] *= 2
            stable = lambda value: {**value, 'context': {k: v for k, v in value['context'].items() if k != 'current_date'}}
            if stable(expected) != stable(prepared):
                continue
            source = harness._uncertain_normal_source(run_id, normal)
            gateway_timeout = False
            if source is None:
                from .normal_http_recovery import derive_normal_http_source
                source = derive_normal_http_source(receipt_source_store(view, intelligence, run_id, progress, records),
                    intelligence, run_id, key, progress)
                gateway_timeout = bool(source and source.get('kind') == 'normal_gateway_timeout')
            if not source or source.get('linkage') != 'canonical_operation_id':
                continue
            operation = source['request_operation_id']
            controller = prepared['context'].get('_harness_control', {}).get('operation_id')
            related = lambda row: row.get('operation_id') == operation or bool(controller) and row.get('controller_operation_id') == controller
            candidates = [row for row in records if related(row)]
            if any(row.get('id') not in source['request_ids']
                   for row in view.list('usage', run_id) if related(row)):
                continue  # An extra usage-only receipt has no canonical settlement.
            if ({row['id'] for row in candidates} != set(source['request_ids']) or any(
                    row.get('run_id') != run_id or row.get('role') != role or row.get('operation_id') != operation
                    or row.get('controller_operation_id') != controller
                    or (row.get('status') != 'http_error' or row.get('http_status') != 524 if gateway_timeout
                        else row.get('status') not in UNKNOWN or row.get('http_status') is not None)
                    or row.get('provenance') != 'live_provider'
                    or (not gateway_timeout and row.get('cost_estimated') is not True) or not isinstance(row.get('usage'), dict)
                    or type(row.get('cost_usd')) not in (int, float) or not math.isfinite(row['cost_usd']) or row['cost_usd'] < 0
                    or not isinstance(row.get('created_at'), str) or not isinstance(row.get('completed_at'), str)
                    or not normal['dispatched_at'] <= row['created_at'] <= row['completed_at']
                    for row in candidates)):
                continue
            if output_exists(view, run_id, operation, request_ids=tuple(source['request_ids'])):
                continue
            provider = intelligence.provider_for(role)
            if (provider['id'], provider['model']) != (source['provider_id'], source['model']):
                continue
            cap = source['effective_output_limit']
            endpoint, _, _ = intelligence._wire(provider, role, '', '', cap)
            audits = [view.get('request_audits', row['id'], {}) for row in candidates]
            related_audits = [row for row in iter_records(view, 'request_audits', run_id,
                where="json_extract(data,'$.operation_id') IS ?" +
                    (" OR json_extract(data,'$.controller_operation_id') IS ?" if controller else ''),
                args=(operation, controller) if controller else (operation,)) if related(row)]
            if (len(related_audits) != len(candidates)
                    or len({row.get('id') for row in related_audits}) != len(related_audits)
                    or any(row.get('id') != row.get('request_id') for row in related_audits)
                    or {row.get('request_id') for row in related_audits} != set(source['request_ids']) or any(
                    audit.get('id') != row['id'] or audit.get('request_id') != row['id']
                    or any(audit.get(field) != row.get(field) for field in
                           ('run_id', 'role', 'provider', 'model', 'operation_id', 'controller_operation_id'))
                    or audit.get('protocol') != provider.get('protocol') or audit.get('endpoint') != endpoint
                    for row, audit in zip(candidates, audits))):
                continue
            preview_operation = _hash(['normal-recovery-preview', key, normal['operation_id']])[:32]
            reason = 'One operator-authorized retry of an uncertain normal request; the original charge remains unknown'
            if gateway_timeout:
                from .normal_http_recovery import HTTP_RECOVERY_REASON
                reason = HTTP_RECOVERY_REASON
            final_request = Harness.prepare(role, handoff['purpose'], assignment['instruction'], context,
                assignment['schema'], progress['tool_results'], final_control={
                    'operation_id': preview_operation,
                    'reason': reason,
                    'operator_resume_generation': _hash(['normal-recovery-preview-generation', run_id])[:32]})
            final_request['context']['_output_token_limit'] = cap
            requests.append({**final_request, 'cache': False, 'max_attempts': 1})
            target = {'handoff_id': key, 'role': role, 'provider': provider['id'], 'model': provider['model'],
                            'request_ids': source['request_ids'], 'unknown_cost_usd': round(sum(row['cost_usd'] for row in candidates), 10),
                            'output_token_limit': cap, 'tool_result_count': len(progress['tool_results'])}
            if gateway_timeout:
                target.update(recovery_kind='gateway_timeout', http_statuses=source['http_statuses'],
                              estimated_cost_usd=target['unknown_cost_usd'])
            targets.append(target)
        if not targets:
            return unavailable('没有符合条件的普通未知费用或HTTP 524请求；已有输出、恢复历史、身份变化或证据不完整时不会再次派发。')
        preflight = intelligence.preflight_batch(run_id, requests)
        if len(preflight['requests']) != len(targets) or any(
                item['max_output_tokens'] != target['output_token_limit'] for item, target in zip(preflight['requests'], targets)):
            return unavailable('当前有效输出上限与原请求不同，不能确认单次续答可执行。')
        guidance = ('可使用顶部“恢复”明确授权每项普通请求的一次最终续答；该续答不调用工具，可能产生额外费用。'
                    '原未知费用和已完成工具结果保留。本预览不授权或启动研究，执行前仍会重新检查门禁。')
        if any(target.get('recovery_kind') == 'gateway_timeout' for target in targets):
            guidance = ('已收到HTTP 524错误回执，但未保存完整生成结果。可使用顶部“恢复”明确授权一次最终续答；'
                        '该续答不调用工具，可能产生额外费用。原失败记录、未核实的费用估算和工具结果保留。'
                        '本预览不授权或启动研究，执行前仍会重新核验。')
        return {'available': True, 'next_action': 'explicit_resume', 'targets': targets, 'reason': guidance}
    except (RequestRetryError, ProviderError, ValueError, KeyError, TypeError):
        # Never expose provider messages, request bodies or credentials. The
        # read-only adapter also makes a preflight write fail closed here.
        return unavailable('当前模型、上下文、回执完整性或预算预检未通过，不能确认普通请求可单次续答。')


def preview(engine, run_id):
    db = sqlite3.connect(engine.store.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        view = _SnapshotStore(db, engine.store.path)
        ready = _ready_authorizations(view, run_id)
        try:
            result = _derive(engine, view, run_id)[0]
        except (RequestRetryError, ProviderError) as exc:
            # Provider diagnostics can contain endpoint details; gate reasons
            # from this module contain no credentials or generated text.
            reason = str(exc) if isinstance(exc, RequestRetryError) else 'Provider configuration or budget preflight does not permit this retry'
            if ready:
                next_step = 'A saved one-attempt authorization is awaiting Resume; execution still checks provider, budget and other gates.'
                reason = next_step if reason.startswith('No terminal final request') else next_step + ' Current gate: ' + reason
            result = {'run_id': run_id, 'eligible': False, 'snapshot_id': None, 'targets': [], 'reason': reason}
        return {**result, 'ready_authorizations': ready, 'normal_recovery': _normal_recovery(engine, view, run_id)}
    finally:
        db.close()


def authorize(engine, run_id, snapshot_id, acknowledge_duplicate_charge):
    if acknowledge_duplicate_charge is not True:
        raise RequestRetryError('Explicit acknowledgement of possible duplicate charges is required')
    if not isinstance(snapshot_id, str) or len(snapshot_id) != 64 or any(c not in '0123456789abcdef' for c in snapshot_id):
        raise RequestRetryError('Invalid retry snapshot')
    authorization_id = f'{run_id}:request-retry:{snapshot_id}'
    with engine.store._db() as db:
        db.execute('BEGIN IMMEDIATE')
        view = _SnapshotStore(db, engine.store.path)
        engine.store._guard_run(db, run_id)
        saved = view.get('operator_resumes', authorization_id)
        if saved:
            # A repeated confirmation acknowledges the first authorization;
            # it never resets a dispatched, failed, or completed operation.
            return {**saved['result'], 'idempotent': True}
        public, evidence, intelligence = _derive(engine, view, run_id)
        if public['snapshot_id'] != snapshot_id:
            raise RequestRetryError('Retry preview is stale; reload it and confirm the current requests')
        timestamp = now()
        transitions, target_audits = [], []
        for target, source in zip(public['targets'], evidence):
            progress = copy.deepcopy(source['progress'])
            old = progress['finalization']
            operation = uid()
            prepared = _next_prepared(old, operation)
            final = {'status': 'ready', 'operation_id': operation, 'created_at': timestamp,
                     'reason': 'Explicit controller confirmation permits one retry; historical charges are retained; unknown charges remain estimated',
                     'provider_id': target['provider'], 'model': target['model'], 'prepared': prepared,
                     'effective_output_limit': target['output_token_limit'],
                     'confirmed_retry_of': old['operation_id'], 'request_retry_authorization': authorization_id,
                      'provider_identity': source['provider_identity']}
            from .tool_request_recovery import prepared_successor
            final = prepared_successor(view, target['handoff_id'], final)
            if source.get('streaming_migration'):
                final['streaming_migration'] = copy.deepcopy(source['streaming_migration'])
                final['reason'] = '明确确认一次非流式524到流式响应的迁移；历史请求与未核实费用保留。'
            progress['finalization_history'] = [*progress.get('finalization_history', []), copy.deepcopy(old)]
            progress['finalization'] = final
            transitions.append(('handoff_progress', target['handoff_id'], progress))
            target_audits.append({**target, 'previous_operation_id': old['operation_id'], 'operation_id': operation,
                                  'previous_finalization_sha256': _hash(old),
                                  'receipts_sha256': _hash(source['receipts']),
                                   'prepared_sha256': _hash(prepared), 'provider_identity': source['provider_identity'],
                                   **({'streaming_migration': copy.deepcopy(source['streaming_migration'])}
                                      if source.get('streaming_migration') else {})})
        # Preflight the actual newly assigned IDs before any durable transition.
        intelligence.preflight_batch(run_id, [{**value['finalization']['prepared'], 'cache': False, 'max_attempts': 1}
                                               for _, _, value in transitions])
        result = {'run_id': run_id, 'snapshot_id': snapshot_id, 'authorization_id': authorization_id,
                  'status': 'authorized', 'requires_resume': True, 'idempotent': False, 'target_count': len(target_audits)}
        authorization = {'id': authorization_id, 'run_id': run_id, 'kind': 'explicit_request_retry_confirmation',
                         'created_at': timestamp, 'snapshot_id': snapshot_id, 'acknowledge_duplicate_charge': True,
                         'intent_provenance': 'explicit_controller_command_not_verified_human_identity',
                         'intent': 'Authorize one inference attempt per target using the frozen handoff task, schema and prepared context, '
                                   'with a new controller operation ID. Preview consent binds available memory; dispatch uses current '
                                   'RAG/history and normal budget/compression gates. Historical unknown costs are preserved; '
                                   'this does not assert byte-for-byte replay of the previous HTTP body.',
                         'targets': target_audits, 'result': result}
        for collection, key, value in [*transitions, ('operator_resumes', authorization_id, authorization)]:
            engine.store._guard_write(db, collection, key, value)
            db.execute('INSERT INTO records VALUES (?,?,?,?) ON CONFLICT(collection,key) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at',
                       (collection, key, json.dumps(value, ensure_ascii=False, allow_nan=False), timestamp))
        return result


def validate_dispatch(store, run_id, key, final, provider, preflight):
    """Recheck the confirmation immediately before the one dispatch marker."""
    identifier = final.get('request_retry_authorization')
    saved = store.get('operator_resumes', identifier, {})
    matches = [item for item in saved.get('targets', []) if item.get('handoff_id') == key]
    if (saved.get('run_id') != run_id or saved.get('kind') != 'explicit_request_retry_confirmation'
            or len(matches) != 1):
        raise RequestRetryError('Explicit retry authorization is missing or inconsistent')
    target = matches[0]
    if (target['operation_id'] != final['operation_id'] or target['prepared_sha256'] != _hash(final['prepared'])
            or target['provider_identity'] != _identity(provider)
            or not preflight or preflight['requests'][0]['max_output_tokens'] != target['output_token_limit']):
        raise RequestRetryError('Confirmed retry provider, frozen request, or output limit changed before dispatch')
    if target.get('recovery_kind') == 'streaming_gateway_timeout' or final.get('streaming_migration'):
        from .streaming_http_recovery import validate_streaming_dispatch
        if not validate_streaming_dispatch(store, run_id, key, final, provider, target):
            raise RequestRetryError('流式迁移的原始回执、通道或流式设置已变化；未发送请求。')
