"""Read-only account totals for status pages, without request bodies or history."""
from __future__ import annotations

import json


def summary(store, run_id=None, *, native_records=False, native_offset=0, native_limit=30):
    if not native_records or native_limit == 0:
        from .billing_projection_cache import totals
        result = totals(store, run_id, lambda snapshot: _summary(snapshot, run_id))
        if native_records:
            native = result['local_codex']
            native.update(records=[], records_total=native['operations'], records_offset=native_offset,
                records_limit=0, thread_observations=[], thread_observations_total=native['threads_with_usage'],
                thread_observations_offset=native_offset, thread_observations_limit=0)
        return result
    return _summary(store, run_id, native_records=True,
                    native_offset=native_offset, native_limit=native_limit)


def _summary(store, run_id=None, *, native_records=False, native_offset=0, native_limit=30):
    """Project precisely the ledger fields used by Intelligence.billing totals.

    This is a display projection, never a budget admission or payment receipt.
    Missing native Codex costs remain unknown and excluded from provider totals.
    """
    where = "collection=?" + (" AND json_extract(data,'$.run_id')=?" if run_id is not None else '')
    args = lambda collection: [collection, run_id] if run_id is not None else [collection]
    with store._read_db() as db:
        if not db.in_transaction:
            db.execute('BEGIN')
        # Expand the canonical ledger once and let NOT IN build an indexed ID
        # set. A correlated JSON scan would expand every budget for every usage
        # row. NULL IDs never matched the old equality join, so retain them on
        # the usage side and exclude them from the lookup set. Canonical native
        # rows still suppress their usage mirrors before the provider filter.
        records = db.execute(f"""
            WITH budgets AS (SELECT data FROM records WHERE {where}),
            ledger AS MATERIALIZED (
                SELECT item.value AS data, json_extract(item.value,'$.id') AS record_id
                FROM budgets, json_each(budgets.data,'$.records') item),
            usage AS (SELECT data FROM records WHERE {where}),
            combined AS (
                SELECT data FROM ledger
                UNION ALL SELECT data FROM usage
                WHERE json_extract(usage.data,'$.id') IS NULL
                    OR json_extract(usage.data,'$.id') NOT IN (
                        SELECT record_id FROM ledger WHERE record_id IS NOT NULL))
            SELECT json_object(
                'provider', coalesce(json_extract(data,'$.provider'),'unknown'),
                'cost_usd', json_extract(data,'$.cost_usd'),
                'usage', coalesce(json_extract(data,'$.usage'),json('{{}}')),
                'cache_hit', coalesce(json_extract(data,'$.cache_hit'),0),
                'cost_estimated', CASE WHEN json_type(data,'$.cost_estimated') IS NULL
                    THEN NOT coalesce(json_extract(data,'$.price_verified'),0)
                    ELSE json_extract(data,'$.cost_estimated') END
            ) AS projection FROM combined
            WHERE json_extract(data,'$.provider') IS NOT 'local_codex'
            """, [*args('budgets'), *args('usage')]).fetchall()
        ledgers = db.execute(f"""SELECT
            CASE WHEN json_type(data,'$.calls')='integer' AND json_extract(data,'$.calls')>=0
                THEN json_extract(data,'$.calls') ELSE NULL END AS calls,
            CASE WHEN json_type(data,'$.pending')='object'
                THEN (SELECT count(*) FROM json_each(records.data,'$.pending')) ELSE NULL END AS pending,
            (SELECT coalesce(sum(json_extract(value,'$.reserved_usd')),0)
                FROM json_each(records.data,'$.pending')) AS reserved,
            (SELECT count(*) FROM json_each(records.data,'$.pending')
                WHERE json_extract(value,'$.provider') IS NOT 'local_codex') AS provider_pending
            FROM records WHERE {where}""", args('budgets')).fetchall()
    rows = [json.loads(row['projection']) for row in records]

    def aggregate(values):
        return {'cost_usd': round(sum(row.get('cost_usd', 0) or 0 for row in values), 8),
                **{field: sum(row.get('usage', {}).get(field, 0) for row in values)
                   for field in ('input_tokens', 'output_tokens', 'cached_tokens', 'cache_write_tokens')},
                'requests': sum(not row.get('cache_hit', False) for row in values),
                'response_cache_hits': sum(bool(row.get('cache_hit', False)) for row in values),
                'estimated_records': sum(bool(row.get('cost_estimated')) for row in values)}

    from .codex_accounting import billing_summary
    from .model_pricing import combined
    native = billing_summary(store, run_id, include_records=native_records,
                             record_offset=native_offset, record_limit=native_limit, use_display_cache=True)
    return {**aggregate(rows), **combined(aggregate(rows)['cost_usd'], native),
            'by_provider': [{'provider': provider, **aggregate([r for r in rows if r['provider'] == provider])}
                            for provider in sorted({r['provider'] for r in rows})],
            'call_budget': {'run_id': run_id, 'source': 'budgets.calls',
                'used': sum(row['calls'] for row in ledgers) if ledgers and all(row['calls'] is not None for row in ledgers) else None,
                'pending': sum(row['pending'] for row in ledgers) if ledgers and all(row['pending'] is not None for row in ledgers) else None,
                'ledger_available': bool(ledgers),
                'scope': 'All admitted model attempts, including local Codex, pending and failed requests; cached reply reuse does not reserve another call.'},
            'pending_usd': round(sum(row['reserved'] for row in ledgers), 8),
            'pending_requests': sum(row['provider_pending'] for row in ledgers),
            'currency': 'USD', 'details_loaded': False,
            'local_codex': native,
            'pricing_note': 'Configured USD/million token rates; unverified rates and missing usage are estimates, not provider invoices.'}


def page(store, run_id=None, *, offset=0, limit=30, native_offset=0, native_limit=0):
    """Account totals plus the visible page of receipt fields, without bodies."""
    from .billing_projection_cache import totals
    return totals(store, run_id, lambda snapshot: _page(snapshot, run_id,
        offset=offset, limit=limit, native_offset=native_offset, native_limit=native_limit),
        variant=('page', offset, limit, native_offset, native_limit))


def _page(store, run_id, *, offset, limit, native_offset, native_limit):
    result = summary(store, run_id, native_records=True, native_offset=native_offset, native_limit=native_limit)
    where = "collection=?" + (" AND json_extract(data,'$.run_id')=?" if run_id is not None else '')
    args = lambda collection: [collection, run_id] if run_id is not None else [collection]
    cte = f"""WITH ledger AS MATERIALIZED (
        SELECT item.value AS data,json_extract(item.value,'$.id') AS record_id
        FROM records,json_each(records.data,'$.records') item WHERE {where}),
        combined AS (SELECT data FROM ledger UNION ALL
            SELECT data FROM records WHERE {where}
            AND (json_extract(data,'$.id') IS NULL OR json_extract(data,'$.id') NOT IN
                (SELECT record_id FROM ledger WHERE record_id IS NOT NULL))),
        provider AS (SELECT data FROM combined WHERE json_extract(data,'$.provider') IS NOT 'local_codex') """
    fields = ('id', 'run_id', 'provider', 'role', 'model', 'created_at', 'status', 'usage',
        'cost_usd', 'cost_estimated', 'price_verified', 'cache_hit', 'provenance', 'rates',
        'pricing_snapshot', 'rate_basis', 'input_tokens', 'output_tokens', 'cached_tokens')
    projection = ','.join(f"'{field}',json_extract(data,'$.{field}')" for field in fields)
    projection += ", 'error',substr(json_extract(data,'$.error'),1,1400), 'error_details',json_object('message',substr(json_extract(data,'$.error_details.message'),1,1400),'code',json_extract(data,'$.error_details.code'),'type',json_extract(data,'$.error_details.type'),'status_code',json_extract(data,'$.error_details.status_code'))"
    parameters = [*args('budgets'), *args('usage')]
    with store._read_db() as db:
        result['records_total'] = db.execute(cte + 'SELECT count(*) FROM provider', parameters).fetchone()[0]
        result['records'] = [json.loads(row[0]) for row in db.execute(cte +
            f"SELECT json_object({projection}) FROM provider ORDER BY json_extract(data,'$.created_at') DESC,json_extract(data,'$.id') DESC LIMIT ? OFFSET ?",
            [*parameters, limit, offset])]
    for row in result['records']:
        for field in ('cache_hit', 'cost_estimated', 'price_verified'):
            if row[field] is not None:
                row[field] = bool(row[field])
    result.update(records_offset=offset, records_limit=limit, details_loaded=True)
    return result
