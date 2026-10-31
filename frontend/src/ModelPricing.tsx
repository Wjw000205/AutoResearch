import { useEffect, useState } from 'react';
import { api, type Json } from './api';
import { useRemoteResource } from './useRemoteResource';
import { RecordPagination } from './RecordPagination';

const fields = [['input_price', '输入'], ['output_price', '输出'], ['cached_input_price', '缓存输入'], ['cache_write_price', '缓存写入']];

export function nativeMoney(value: unknown) {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
    ? `$${value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 8 })}` : '待补齐单价或用量';
}

export function totalModelCost(billing: Json) {
  if (billing.native_estimate_complete === false && billing.local_codex?.operations > 0
      && billing.total_estimated_cost_usd === 0 && billing.local_codex?.estimated_cost_usd == null) return '待补齐单价或用量';
  const prefix = billing.native_estimate_complete === false && billing.local_codex?.operations > 0 ? '已计价 ' : '';
  return prefix + nativeMoney(billing.total_estimated_cost_usd ?? billing.cost_usd);
}

export function PricingTable({ models, onChange, disabled = false }: { models: Json[]; onChange: (index: number, key: string, value: unknown) => void; disabled?: boolean }) {
  return <div className="table-scroll"><table className="billing-table"><thead><tr><th>模型标识</th>{fields.map(([key, label]) => <th key={key}>{label}</th>)}<th>单价来源</th></tr></thead><tbody>{models.map((model, index) => <tr key={index}><td><input aria-label={`模型 ${index + 1}`} disabled={disabled} value={model.model} onChange={e => onChange(index, 'model', e.target.value)} /></td>{fields.map(([key, label]) => <td key={key}><input aria-label={`${model.model} ${label}单价`} disabled={disabled} type="number" min="0" max="10000" step="0.000001" placeholder="未定价" value={model[key] ?? ''} onChange={e => onChange(index, key, e.target.value === '' ? null : Number(e.target.value))} /></td>)}<td><input aria-label={`${model.model} 单价来源`} disabled={disabled} value={model.source ?? '用户配置'} onChange={e => onChange(index, 'source', e.target.value)} /></td></tr>)}</tbody></table></div>;
}

export function ModelPricingEditor({ onSaved }: { onSaved: () => void }) {
  const [refresh, setRefresh] = useState(0);
  const remote = useRemoteResource<Json>('/model-pricing', refresh);
  const [models, setModels] = useState<Json[]>([]);
  const [version, setVersion] = useState(0);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  useEffect(() => {
    if (!remote.data) return;
    const saved: Json[] = remote.data.models ?? [];
    const existing = new Set(saved.map(row => row.model));
    setModels([...saved, ...(remote.data.observed_models ?? []).filter((name: string) => !existing.has(name)).map((model: string) => ({ model, source: '用户配置' }))]);
    setVersion(remote.data.version);
  }, [remote.data]);
  const change = (index: number, key: string, value: unknown) => {
    setModels(rows => rows.map((row, i) => i === index ? { ...row, ...(key === 'model' || fields.some(([f]) => f === key) ? { source: '用户配置' } : {}), [key]: value } : row));
    setMessage('');
  };
  async function save() {
    setBusy(true); setError(''); setMessage('');
    try {
      await api('/model-pricing', { method: 'PUT', body: JSON.stringify({ version, models, currency: 'USD', tokens_per_unit: 1000000 }) });
      setMessage('单价已保存，费用已按模型与用量重新汇总。'); setRefresh(value => value + 1); onSaved();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }
  return <section className="panel" aria-label="模型单价配置"><h3>本机 Codex 模型单价</h3><p>单位：美元／每百万 token。输入、输出、缓存分别定价；留空表示未定价，0表示该项免费。</p><p>新操作保留当时的费率；没有费率快照的历史用量按当前单价估算。套餐实际扣费以账单为准。</p>{remote.loading ? <p>正在读取模型单价…</p> : <PricingTable models={models} onChange={change} disabled={busy} />}{(error || remote.error) && <p role="alert">{error || remote.error}</p>}{message && <p role="status">{message}</p>}<div className="button-row"><button className="btn" type="button" disabled={busy || remote.loading} onClick={() => setModels(rows => [...rows, { model: '', source: '用户配置' }])}>添加模型</button><button className="btn" type="button" disabled={busy} onClick={() => { setError(''); setRefresh(v => v + 1); }}>刷新单价</button><button className="btn btn-primary" type="button" disabled={busy || remote.loading || !remote.data} onClick={() => void save()}>{busy ? '保存中…' : '保存模型单价'}</button></div></section>;
}

const reasons: Record<string, string> = { unpriced_model: '模型未定价', missing_usage: '输入或输出用量未报告', missing_cache_usage: '缓存用量未报告', missing_cache_write_usage: '缓存写入用量未报告', invalid_usage: '用量不一致', missing_rate: '单价未补齐', ambiguous_ownership: '线程费用归属不明确', mixed_or_unknown_models: '线程模型发生变化或未报告', mixed_rates: '线程中途变更单价，缺少完整分段用量', context_tier_unknown: '线程累计跨越长上下文阈值，缺少逐次调用范围' };

export function NativeCostDetails({ summary, detailsOpen = false, onDetailsOpen, onOffset, loading = false }: {
  summary: Json; detailsOpen?: boolean; onDetailsOpen?: (open: boolean) => void;
  onOffset?: (offset: number) => void; loading?: boolean;
}) {
  const [limit, setLimit] = useState(50);
  const observations: Json[] = summary.thread_observations ?? [];
  return <><div className="table-scroll"><table className="billing-table"><thead><tr><th>模型</th><th>线程数／已计价</th><th>输入 token</th><th>输出 token</th><th>费用估算</th></tr></thead><tbody>{(summary.by_model ?? []).map((row: Json) => <tr key={row.model}><td>{row.model}</td><td>{row.threads} / {row.priced_threads}</td><td>{row.input_tokens?.toLocaleString() ?? '未报告'}</td><td>{row.output_tokens?.toLocaleString() ?? '未报告'}</td><td>{nativeMoney(row.estimated_cost_usd)}</td></tr>)}</tbody></table></div><details open={onDetailsOpen ? detailsOpen : undefined} onToggle={event => onDetailsOpen?.(event.currentTarget.open)}><summary>按线程核算的费用明细 · {summary.thread_observations_total ?? observations.length} 条</summary>{(!onDetailsOpen || detailsOpen) && <><p>普通输入 = 总输入 − 缓存输入 − 缓存写入。费用 = 各项 token × 对应单价 ÷ 1000000。推理输出已包含在输出中。</p><div className="table-scroll"><table className="billing-table"><thead><tr><th>线程／模型</th><th>总输入／其中缓存</th><th>输出</th><th>费用估算与费率</th></tr></thead><tbody>{observations.slice(0, limit).map(row => <tr key={row.thread_id}><td><code>{row.thread_id}</code><small>{row.model ?? '模型未报告'}</small></td><td>{row.total.inputTokens?.toLocaleString() ?? '未报告'} / {row.total.cachedInputTokens?.toLocaleString() ?? '未报告'}</td><td>{row.total.outputTokens?.toLocaleString() ?? '未报告'}</td><td>{row.estimated_cost_usd == null ? reasons[row.cost_status] ?? '待补齐' : nativeMoney(row.estimated_cost_usd)}{row.rates && <details><summary>费率版本 {row.rate_version} · {row.pricing_basis === 'operation_rate_snapshot' ? '操作发生时单价' : '历史用量按当前单价估算'}</summary>{fields.map(([key, label]) => <p key={key}>{label}：{row.rates[key] == null ? '未定价' : nativeMoney(row.rates[key])} / 百万 token</p>)}<p>来源：{row.rates.source}{row.rates.source_url && <a href={row.rates.source_url} target="_blank" rel="noreferrer">查看官方价格</a>} · {row.context_tier === 'long' ? '长上下文费率' : '基础费率'}</p></details>}</td></tr>)}</tbody></table></div>{observations.length > limit && <button className="btn small" type="button" onClick={() => setLimit(v => v + 50)}>继续显示费用明细</button>}{onOffset && <><p className="panel-note">线程费用明细与独立记录共用当前页；翻页会同步更新已展开的两处记录。</p><RecordPagination label="线程费用明细" offset={summary.thread_observations_offset ?? summary.records_offset ?? 0} count={observations.length} total={summary.thread_observations_total} loading={loading} onOffset={onOffset} /></>}</>}</details></>;
}
