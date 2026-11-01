import { ReferenceText } from './ReferenceNames';
import { date, type Json, type RunListItem } from './api';
import { billingRunLabel } from './DeliveryViews';
import { NativeCostDetails, nativeMoney } from './ModelPricing';
import { HumanMessage } from './localization';
import { RecordPagination } from './RecordPagination';

const metrics = [['inputTokens', '输入词元'], ['outputTokens', '输出词元'], ['cachedInputTokens', '缓存读取词元'],
  ['cacheWriteInputTokens', '缓存写入词元'], ['reasoningOutputTokens', '推理输出词元'], ['totalTokens', '总词元']] as const;
export const codexCount = (value: unknown) => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
  ? value.toLocaleString('zh-CN') : '未报告';
const records = (value: unknown): Json[] => Array.isArray(value) ? value.filter((row): row is Json => !!row && typeof row === 'object' && !Array.isArray(row)) : [];
const phases: Record<string, string> = { prepared: '已准备', thread_start_dispatched: '已请求创建会话', thread_resume_dispatched: '已请求恢复会话', thread_ready: '会话已就绪', turn_start_dispatched: '请求已派发', running: '执行中', completed: '请求已完成', failed: '请求失败', interrupted: '已中断', blocked: '等待处理', unknown: '结果未知' };

export function CodexCostNotice() {
  return <p className="panel-note">本机 Codex 按模型单价与报告用量估算费用；API 费用与套餐实际扣费分别核对，未配置单价或缺失用量不按零计算。</p>;
}

function UsageFields({ value }: { value?: Json | null }) {
  return <dl className="provenance-grid">{metrics.map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{codexCount(value?.[key])}</dd></div>)}</dl>;
}

export function CodexReceiptUsage({ usage, observedAt }: { usage?: Json | null; observedAt?: string }) {
  return <section className="codex-recovery" aria-label="Codex 回执用量"><h5>接口报告的词元用量</h5><p>用量观测时间：<ReferenceText value={observedAt ? date(observedAt, true) : '未报告'} /></p><p>金额未核实。以下是本条回执观测：线程累计值可能已包含此前请求，不能将多条回执相加；最后一次模型调用不代表整个推理回合。</p><details><summary>线程累计用量（本次观测）</summary><UsageFields value={usage?.total} /></details><details><summary>最后一次模型调用用量</summary><UsageFields value={usage?.last} /></details>{!usage && <p>尚未收到词元用量，不能按零计算。</p>}</section>;
}

export function LocalCodexBilling({ summary, runs = [], runsLoaded = false, recordsOpen = false, onRecordsOpen, threadsOpen = false, onThreadsOpen, onOffset, loading = false }: {
  summary?: Json | null; runs?: RunListItem[]; runsLoaded?: boolean; recordsOpen?: boolean;
  onRecordsOpen?: (open: boolean) => void; threadsOpen?: boolean; onThreadsOpen?: (open: boolean) => void;
  onOffset?: (offset: number) => void; loading?: boolean;
}) {
  if (!summary) return <section className="panel compact-panel" aria-label="本机 Codex 用量"><h3>本机 Codex 用量与费用</h3><p>尚未取得本机 Codex 独立用量汇总。</p><CodexCostNotice /></section>;
  const rows = records(summary.records);
  return <section className="panel compact-panel" aria-label="本机 Codex 用量">
    <h3>本机 Codex 用量与费用</h3><CodexCostNotice />
    <p>费用估算：<strong>{nativeMoney(summary.estimated_cost_usd)}</strong>{summary.estimate_complete === false && ' · 已计价部分'} · 已计价 {summary.priced_threads ?? 0} 个线程，待补齐 {summary.unpriced_threads ?? 0} 个线程 · 最新已报告线程累计词元（每线程只计一次）</p>
    <NativeCostDetails summary={summary} detailsOpen={threadsOpen} onDetailsOpen={onThreadsOpen} onOffset={onOffset} loading={loading} /><UsageFields value={summary.observed_totals} />
    <p>已记录操作 <ReferenceText value={codexCount(summary.operations)} /> · 已派发操作 <ReferenceText value={codexCount(summary.dispatched_operations)} /> · 有请求标识的回合 <ReferenceText value={codexCount(summary.confirmed_turns)} /></p>
    <p>会话 <ReferenceText value={codexCount(summary.threads)} /> · 有累计用量 <ReferenceText value={codexCount(summary.threads_with_usage)} /> · 尚无累计用量 <ReferenceText value={codexCount(summary.threads_without_usage)} /> · 归属不明确 <ReferenceText value={codexCount(summary.ambiguous_threads)} /> · 已派发但无会话标识 <ReferenceText value={codexCount(summary.dispatched_without_thread)} /></p>
    <p>缺失字段显示“未报告”，汇总只覆盖已报告观测；不代表所有调用均已计量。各词元字段按接口原值分别显示，不再次相加。</p>
    <details open={onRecordsOpen ? recordsOpen : undefined} onToggle={event => onRecordsOpen?.(event.currentTarget.open)}>
      <summary>本机 Codex 独立用量记录 · {summary.records_total ?? rows.length} 条</summary>
      {(!onRecordsOpen || recordsOpen) && <>
        <p>同一线程可保留多条观测，不逐行相加。删除研究后的既有用量记录仍保留；这里不是供应商结算账单。</p>
        {rows.length ? rows.map((record, index) => <article className="provenance-card" key={record.id || `${record.operation_id}-${index}`}>
          <h4><ReferenceText value={billingRunLabel(record.run_id, runs, runsLoaded)} /></h4>
          <p><ReferenceText value={date(record.updated_at || record.created_at, true)} /> · <ReferenceText value={phases[record.status] ? phases[record.status] : '状态未识别'} /> · <ReferenceText value={record.dispatched === true ? '已记录派发' : record.dispatched === false ? '尚未记录派发' : '派发状态未记录'} /></p>
          <dl><div><dt>操作标识</dt><dd><code><ReferenceText value={record.operation_id || '未记录'} /></code></dd></div>
            <div><dt>会话／请求标识</dt><dd><code><ReferenceText value={record.thread_id || '未记录'} /> / <ReferenceText value={record.turn_id || '未记录'} /></code></dd></div>
            <div><dt>指定模型</dt><dd>{record.requested_model || '沿用本机默认'}</dd></div>
            <div><dt>指定推理强度</dt><dd>{record.requested_effort || '沿用本机默认'}</dd></div>
            <div><dt>线程报告的模型／推理等级</dt><dd>{record.reported_model || '未报告'} / {record.reported_effort || '未报告'}</dd></div>
          </dl><p>线程配置不代表逐次调用的底层执行已独立核实。</p>
          {!phases[record.status] && typeof record.status === 'string' && <HumanMessage text={record.status} />}
          <CodexReceiptUsage usage={record.usage} observedAt={record.usage_observed_at} />
        </article>) : <p>尚无独立用量记录；不能据此推断从未产生费用。</p>}
        {onOffset && <><p className="panel-note">独立记录与线程费用明细共用当前页；翻页会同步更新已展开的两处记录。</p>
          <RecordPagination label="本机 Codex 用量" offset={summary.records_offset ?? 0} count={rows.length}
            total={summary.records_total} loading={loading} onOffset={onOffset} /></>}
      </>}
    </details>
  </section>;
}
