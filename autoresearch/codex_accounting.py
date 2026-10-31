"""Native Codex observations, separate from priced provider requests.

App-server reports a thread cumulative counter and a last model call counter.
Neither is a per-turn invoice. Retain one record per operation, and count the
latest reported cumulative observation once per thread without inventing prices.
"""
from __future__ import annotations

from collections import defaultdict
import copy
import json

from . import model_pricing


TOKEN_FIELDS = ('inputTokens', 'outputTokens', 'cachedInputTokens',
                'cacheWriteInputTokens', 'reasoningOutputTokens', 'totalTokens')
STATES = {'prepared', 'thread_start_dispatched', 'thread_resume_dispatched', 'thread_ready',
          'turn_start_dispatched', 'running', 'completed', 'failed', 'interrupted', 'blocked', 'unknown'}


def _counts(value):
    return {key: value[key] for key in TOKEN_FIELDS
            if isinstance(value, dict) and type(value.get(key)) is int and value[key] >= 0}


def reported_usage(value):
    """Keep only reported native counters; missing measurements stay missing."""
    if not isinstance(value, dict):
        return None
    return {group: counts for group in ('last', 'total')
            if (counts := _counts(value.get(group)))} or None


def usage_record(run_id: str, receipt: dict) -> dict | None:
    if not isinstance(receipt, dict) or receipt.get('run_id') != run_id or not receipt.get('operation_id'):
        return None
    usage = reported_usage(receipt.get('usage'))
    return {'id': f"{run_id}:{receipt['operation_id']}", 'run_id': run_id,
        'operation_id': receipt['operation_id'], 'thread_id': receipt.get('thread_id'),
        'turn_id': receipt.get('turn_id'), 'status': receipt.get('status') if receipt.get('status') in STATES else 'unknown',
        'requested_model': receipt.get('requested_model'), 'requested_effort': receipt.get('requested_effort'),
        'reported_model': receipt.get('reported_model'), 'reported_effort': receipt.get('reported_effort'),
        'model_reporting_scope': 'thread_configuration_not_per_turn_telemetry',
        'created_at': receipt.get('created_at'), 'updated_at': receipt.get('updated_at'),
        'turn_dispatched_at': receipt.get('turn_dispatched_at'),
        'dispatched': bool(receipt.get('turn_dispatched_at') or receipt.get('turn_id')),
        'usage': usage or None, 'usage_observed_at': receipt.get('usage_observed_at') or receipt.get('updated_at'),
        'usage_scope': 'thread_cumulative_and_last_model_call_not_verified_turn_total',
        'cost_usd': None, 'price_verified': False, 'cost_status': 'not_reported_by_local_codex',
        'source': 'local_codex_app_server'}


def capture_usage(store, run_id, receipt):
    row = usage_record(run_id, receipt)
    if row:
        previous = store.get('codex_usage', row['id'], {})
        row['pricing_snapshot'] = copy.deepcopy(previous.get('pricing_snapshot') or model_pricing.load(store))
    return row


def _rows(store, *, display_cache=False):
    # Select safe receipts from legacy workflows without loading their prompts.
    fields = ('run_id', 'operation_id', 'thread_id', 'turn_id', 'status', 'requested_model',
        'requested_effort', 'reported_model', 'reported_effort', 'created_at', 'updated_at',
        'turn_dispatched_at', 'usage', 'usage_observed_at')
    projection = ','.join(f"'{field}',json_extract(attempt.value,'$.checkpoint.{field}')" for field in fields)
    saved_fields = ('id', *fields, 'dispatched', 'usage_scope', 'cost_usd', 'price_verified',
                    'cost_status', 'source', 'pricing_snapshot', 'model_reporting_scope')
    saved_projection = ','.join(f"'{field}',json_extract(data,'$.{field}')" for field in saved_fields)
    def decode(raw):
        record = json.loads(raw)
        for field in ('dispatched', 'price_verified'):
            if record[field] is not None:
                record[field] = bool(record[field])
        return record
    with store._read_db() as db:
        if display_cache:
            from .native_billing_display_cache import CACHE
            saved = CACHE.rows(store, db, saved_projection, decode)
        else:
            saved = (decode(r['data']) for r in db.execute(
                f"SELECT json_object({saved_projection}) AS data FROM records WHERE collection='codex_usage'"))
        records = {row['id']: row for row in saved}
        if display_cache:
            # Canonical safe observations usually already cover legacy attempts.
            # Read their identities first, rather than projecting every field
            # from large historical checkpoints that cannot change the total.
            identities = db.execute("SELECT records.key AS workflow_key,attempt.key AS attempt_key, "
                "json_extract(records.data,'$.run_id') AS run_id, "
                "json_extract(attempt.value,'$.checkpoint.operation_id') AS operation_id, "
                "json_type(attempt.value,'$.checkpoint.operation_id') AS operation_type "
                "FROM records,json_each(records.data,'$.attempts') attempt WHERE collection='codex_workflows'")
            def legacy():
                for item in identities:
                    if item['operation_type'] == 'text' and f"{item['run_id']}:{item['operation_id']}" in records:
                        continue
                    yield from db.execute(f"SELECT json_extract(records.data,'$.run_id') AS run_id, json_object({projection}) AS receipt "
                        "FROM records,json_each(records.data,'$.attempts') attempt "
                        "WHERE collection='codex_workflows' AND records.key=? AND attempt.key=?",
                        (item['workflow_key'], item['attempt_key']))
            legacy_rows = legacy()
        else:
            legacy_rows = db.execute(f"SELECT json_extract(records.data,'$.run_id') AS run_id, json_object({projection}) AS receipt "
                "FROM records, json_each(records.data,'$.attempts') attempt WHERE collection='codex_workflows'")
        for result in legacy_rows:
            row = usage_record(result['run_id'], json.loads(result['receipt']))
            if row:
                records.setdefault(row['id'], row)
    return list(records.values())


def billing_summary(store, run_id: str | None = None, *, include_records=True,
                    record_offset=0, record_limit=None, use_display_cache=False) -> dict:
    # Pure read: legacy observations can be displayed before the next atomic
    # workflow update, but GET must not create or overwrite accounting records.
    all_rows = _rows(store, display_cache=use_display_cache)
    owners = defaultdict(set)
    for row in all_rows:
        if row.get('thread_id') and row.get('dispatched'):
            owners[row['thread_id']].add(row['run_id'])
    rows = sorted((r for r in all_rows if run_id is None or r['run_id'] == run_id),
                  key=lambda r: (r.get('created_at') or '', r['id']))
    pricing = model_pricing.load(store)
    if use_display_cache:
        from .native_billing_display_cache import CACHE, pricing_revision
        price_revision = pricing_revision(pricing)
    dispatched = [row for row in rows if row.get('dispatched')]
    threads = defaultdict(list)
    for row in dispatched:
        if row.get('thread_id'):
            threads[row['thread_id']].append(row)
    observations, missing, ambiguous = [], [], []
    for thread, group in threads.items():
        if len({row['run_id'] for row in group}) != 1:
            ambiguous.append(thread)
            continue
        measured = [row for row in group if _counts((row.get('usage') or {}).get('total'))]
        if measured:
            row = max(measured, key=lambda r: (r.get('usage_observed_at') or '', r.get('updated_at') or '', r['id']))
            cost = (CACHE.estimate(row, _counts(row['usage']['total']), pricing, price_revision, model_pricing.estimate)
                    if use_display_cache else model_pricing.estimate(row, _counts(row['usage']['total']), pricing))
            observations.append({'thread_id': thread, 'run_id': row['run_id'], 'operation_id': row['operation_id'],
                'observed_at': row.get('usage_observed_at'), 'total': _counts(row['usage']['total']),
                **cost})
            models = {model_pricing.model_of(r) for r in group}
            if len(owners[thread]) != 1 or len(models) != 1 or None in models:
                observations[-1].update(estimated_cost_usd=None,
                    cost_status='ambiguous_ownership' if len(owners[thread]) != 1 else 'mixed_or_unknown_models')
            else:
                rates = [model_pricing.rate_basis(r, pricing)[1] for r in group]
                fingerprints = {json.dumps({key: rate.get(key) for key in (*model_pricing.FIELDS,
                    'long_context_threshold', 'long_input_multiplier', 'long_output_multiplier')}, sort_keys=True)
                    if rate else None for rate in rates}
                if len(fingerprints) != 1:
                    observations[-1].update(estimated_cost_usd=None, cost_status='mixed_rates')
        else:
            missing.append(thread)
    totals = {key: sum(row['total'].get(key, 0) for row in observations)
              if any(key in row['total'] for row in observations) else None for key in TOKEN_FIELDS}
    priced = [r for r in observations if r['estimated_cost_usd'] is not None]
    estimated = round(sum(r['estimated_cost_usd'] for r in priced), 10) if priced else (0.0 if not dispatched else None)
    complete = len(priced) == len(threads) and not any(not r.get('thread_id') for r in dispatched)
    by_model = []
    for model in sorted({r['model'] for r in observations if r.get('model')}):
        group = [r for r in observations if r['model'] == model]
        known = [r for r in group if r['estimated_cost_usd'] is not None]
        by_model.append({'model': model, 'threads': len(group), 'priced_threads': len(known),
            'estimated_cost_usd': round(sum(r['estimated_cost_usd'] for r in known), 10) if known else None,
            **{label: sum(r['total'].get(field, 0) for r in group) if any(field in r['total'] for r in group) else None
               for label, field in (('input_tokens', 'inputTokens'), ('output_tokens', 'outputTokens'), ('cached_tokens', 'cachedInputTokens'))}})
    result = {'operations': len(rows), 'dispatched_operations': len(dispatched),
        'confirmed_turns': len({(r.get('thread_id'), r['turn_id']) for r in dispatched if r.get('turn_id')}),
        'threads': len(threads), 'threads_with_usage': len(observations),
        'threads_without_usage': len(missing), 'ambiguous_threads': len(ambiguous),
        'dispatched_without_thread': sum(not row.get('thread_id') for row in dispatched),
        'observed_totals': totals,
        'usage_scope': 'latest_reported_cumulative_observation_per_thread_not_complete_request_accounting',
        'cost_usd': None, 'cost_status': 'not_reported_by_local_codex',
        'estimated_cost_usd': estimated, 'estimate_complete': complete,
        'priced_threads': len(priced), 'unpriced_threads': len(threads) - len(priced), 'by_model': by_model,
        'excluded_from_provider_totals': True,
        'note': '费用按模型单价和每线程最新累计用量估算；缓存输入从总输入扣除，推理输出不重复加算。套餐实际收费以账单为准。缺失用量或单价不按零计费。'}
    if include_records:
        if record_limit is None:
            result.update(records=rows, thread_observations=observations)
        else:
            result.update(records=list(reversed(rows))[record_offset:record_offset + record_limit],
                          records_total=len(rows), records_offset=record_offset, records_limit=record_limit,
                          details_loaded=bool(record_limit),
                          thread_observations=sorted(observations, key=lambda row: (row.get('observed_at') or '', row['thread_id']),
                              reverse=True)[record_offset:record_offset + record_limit],
                          thread_observations_total=len(observations),
                          thread_observations_offset=record_offset, thread_observations_limit=record_limit)
    else:
        result.update(details_loaded=False)
    # Public callers can freely edit returned details without poisoning a later
    # display. Totals-only views do not expose any cached operand or rate object.
    return copy.deepcopy(result) if use_display_cache and include_records else result
