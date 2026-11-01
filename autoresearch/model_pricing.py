"""User-configured model rates and auditable token-based cost estimates."""
import copy
import math
import json
from decimal import Decimal
from pathlib import Path

from .storage import now

FIELDS = ('input_price', 'output_price', 'cached_input_price', 'cache_write_price')
OFFICIAL = json.loads(Path(__file__).with_name('official_model_prices.json').read_text(encoding='utf-8'))
EMPTY = {'version': 0, 'currency': 'USD', 'tokens_per_unit': 1_000_000, 'models': OFFICIAL, 'updated_at': None}


class PricingConflict(ValueError):
    pass


def load(store):
    value = store.get('settings', 'model_pricing', EMPTY)
    return {key: copy.deepcopy(value[key]) for key in EMPTY}


def save(store, payload):
    if type(payload.get('version')) is not int or payload['version'] < 0:
        raise ValueError('须提供费率版本号')
    if payload.get('currency', 'USD') != 'USD' or payload.get('tokens_per_unit', 1_000_000) != 1_000_000:
        raise ValueError('单价单位为美元／每百万 token')
    rows = payload.get('models')
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError('模型费率须为最多100项的列表')
    models, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('model'), str):
            raise ValueError('模型标识须为文本')
        model = row['model'].strip()
        if not model or len(model) > 160 or model in seen:
            raise ValueError('模型标识不得为空或重复')
        seen.add(model)
        item = {'model': model}
        for key in FIELDS:
            rate = row.get(key)
            if rate is not None and (type(rate) not in (int, float) or not math.isfinite(rate) or not 0 <= rate <= 10000):
                raise ValueError('单价须为0至10000的有限数值，留空表示未定价')
            item[key] = rate
        source = row.get('source', '用户配置')
        if not isinstance(source, str) or not source.strip() or len(source) > 500:
            raise ValueError('须注明单价来源（最多500字符）')
        item['source'] = source.strip()
        official = next((r for r in OFFICIAL if r['model'] == model), None)
        if official and all(item[k] == official[k] for k in (*FIELDS, 'source')):
            item = copy.deepcopy(official)
        models.append(item)
    def update(previous):
        previous = previous or EMPTY
        if previous['version'] != payload['version']:
            raise PricingConflict('费率已由另一页面更新，请刷新后再保存')
        snapshot = {**EMPTY, 'version': previous['version'] + 1, 'models': models, 'updated_at': now()}
        return {**snapshot, 'history': [*previous.get('history', []),
            {key: copy.deepcopy(previous[key]) for key in EMPTY}]}
    value = store.mutate('settings', 'model_pricing', update, default=EMPTY)
    return {key: value[key] for key in EMPTY}


def model_of(row):
    return row.get('reported_model') or row.get('requested_model')


def rate_basis(row, current):
    model = model_of(row)
    snapshot = row.get('pricing_snapshot') or {'version': 0, 'models': []}
    rate = next((r for r in snapshot['models'] if r['model'] == model), None)
    basis = 'operation_rate_snapshot'
    if not rate or all(rate[key] is None for key in FIELDS):
        rate = next((r for r in current['models'] if r['model'] == model), None)
        snapshot, basis = current, 'current_rates_for_unpriced_history'
    return snapshot, rate, basis


def estimate(row, totals, current):
    """Native input includes cache; reasoning output is already in output."""
    model = model_of(row)
    snapshot, rate, basis = rate_basis(row, current)
    result = {'model': model, 'estimated_cost_usd': None, 'cost_status': 'unpriced_model',
        'pricing_basis': basis, 'rate_version': snapshot['version'], 'rates': copy.deepcopy(rate),
        'currency': 'USD', 'cost_estimated': True}
    if not rate:
        return result
    inp, out = totals.get('inputTokens'), totals.get('outputTokens')
    if type(inp) is not int or type(out) is not int or min(inp, out) < 0:
        return {**result, 'cost_status': 'missing_usage'}
    threshold = rate.get('long_context_threshold')
    result['context_tier'] = 'short' if threshold else 'flat'
    if threshold and inp > threshold:
        last = (row.get('usage') or {}).get('last', {})
        # Thread totals are not a prompt length. Apply the long-context rule
        # only when telemetry establishes one model call for the whole total.
        if any(totals.get(key, 0) != last.get(key, 0) for key in
               ('inputTokens', 'outputTokens', 'cachedInputTokens', 'cacheWriteInputTokens')):
            return {**result, 'cost_status': 'context_tier_unknown'}
        rate = {**rate, **{key: value * (rate['long_output_multiplier'] if key == 'output_price' else rate['long_input_multiplier'])
                          for key, value in rate.items() if key in FIELDS and value is not None}}
        result.update(rates=rate, context_tier='long')
    cached, written = totals.get('cachedInputTokens'), totals.get('cacheWriteInputTokens')
    if cached is None:
        # A missing cache split cannot affect cost when both rates are equal.
        if inp == 0 or rate['cached_input_price'] == rate['input_price']:
            cached = 0
        else:
            return {**result, 'cost_status': 'missing_cache_usage'}
    if written is None:
        if inp == cached or rate['cache_write_price'] == rate['input_price']:
            written = 0
        else:
            return {**result, 'cost_status': 'missing_cache_write_usage'}
    if (type(cached) is not int or type(written) is not int or min(cached, written) < 0
            or cached + written > inp):
        return {**result, 'cost_status': 'invalid_usage'}
    counts = (inp - cached - written, out, cached, written)
    if any(count and rate[key] is None for count, key in zip(counts, FIELDS)):
        return {**result, 'cost_status': 'missing_rate'}
    costs = {key: float(Decimal(count) * Decimal(str(rate[key] or 0)) / Decimal(1_000_000))
             for count, key in zip(counts, FIELDS)}
    total = sum(Decimal(str(value)) for value in costs.values())
    return {**result, 'estimated_cost_usd': float(total.quantize(Decimal('0.0000000001'))),
        'components_usd': costs, 'billable_tokens': dict(zip(FIELDS, counts)), 'cost_status': 'estimated'}


def combined(provider_cost, native):
    cost = native.get('estimated_cost_usd')
    return {'total_estimated_cost_usd': round((provider_cost or 0) + (cost or 0), 8),
        'native_estimate_complete': native.get('estimate_complete', False)}
