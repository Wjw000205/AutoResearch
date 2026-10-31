import { ReferenceText } from './ReferenceNames';
import { CircleAlert, ShieldCheck } from 'lucide-react';
import { HumanMessage, humanizeMessage } from './localization';
import { compact, number, textOf, type Json, type RunListItem, type RunReport } from './api';
import { isContentPolicyViolation, MODERATION_PENDING_LABEL } from './ModerationReviews';

export function billingRunLabel(runId: unknown, runs: RunListItem[], runsLoaded = true) {
  if (typeof runId !== 'string' || !runId) return '未关联研究';
  const run = runs.find(item => item.id === runId);
  return run?.title ?? `${runsLoaded ? '已删除研究' : '研究'} · ${runId.slice(0, 10)}`;
}

export function billingRunOptions(runs: RunListItem[], records: Json[], runsLoaded = true) {
  const options = runs.map(run => ({ id: run.id, label: `${run.mode === 'demo' ? '[模拟演示]' : '[真实运行]'} ${run.title}` }));
  const knownIds = new Set(options.map(option => option.id));
  for (const record of records) {
    if (typeof record.run_id !== 'string' || !record.run_id || knownIds.has(record.run_id)) continue;
    knownIds.add(record.run_id);
    options.push({ id: record.run_id, label: billingRunLabel(record.run_id, runs, runsLoaded) });
  }
  return options;
}

export function currentRunReport(report: RunReport | null, run: {id: string; status: string; updated_at: string}) {
  if (!report || report.run_id !== run.id || report.status !== run.status || report.run_updated_at !== run.updated_at) return null;
  if (report.kind === 'stage' && !['blocked', 'failed'].includes(run.status)) return null;
  return report;
}

export function ReportKindNotice({ report }: { report: RunReport }) {
  if (!report.markdown || report.kind === 'none') return null;
  return <div className={`report-kind-notice report-kind-${report.kind}`} role="note">{report.kind === 'stage' ? <CircleAlert size={18} /> : <ShieldCheck size={18} />}<div><strong><ReferenceText value={report.title} /></strong><p><ReferenceText value={report.kind === 'stage' ? '本报告整理已完成的阶段证据、阻塞原因与未解决事项。研究尚未完成，恢复后结果可能更新。' : '最终报告保留实测结果与局限；完成报告不代表研究结论为正或达到投稿标准。'} /></p><small>报告对应状态：{({ blocked: '阻塞', failed: '失败', completed: '流程完成', running: '运行中', paused: '已暂停' } as Record<string, string>)[report.status] ?? humanizeMessage(report.status)}</small></div></div>;
}

const requestStatus: Record<string, string> = {
  completed: '已返回', http_error: '接口错误', parse_error: '解析失败',
  interrupted_unknown_charge: '进程中断 · 扣费未知', cancelled_unknown_charge: '请求取消 · 扣费未知',
  network_error_unknown_charge: '网络失败 · 扣费未知', demo: '模拟记录', cache_hit: '本地缓存命中',
};
export const isUnknownCharge = (record: Json) => String(record.status ?? '').includes('unknown_charge');
export function billingTokenLabel(record: Json, field: 'input_tokens' | 'output_tokens' | 'cached_tokens') {
  const unreported = !record.cache_hit && record.provenance !== 'demo' && record.usage?.reported === false;
  return unreported || isUnknownCharge(record) && !record.usage?.reported ? '未知' : compact(record[field] ?? record.usage?.[field]);
}

export function BillingRequestStatus({ record }: { record: Json }) {
  const unknown = isUnknownCharge(record);
  const moderation = isContentPolicyViolation(record);
  const status = moderation ? MODERATION_PENDING_LABEL : record.cache_hit ? '本地缓存命中' : requestStatus[record.status] ?? humanizeMessage(textOf(record.status) || '未记录状态');
  const error = record.error || record.error_details?.message;
  return <div className={`billing-request-state ${unknown ? 'unknown-charge' : ''} ${moderation ? 'moderation-request' : ''}`}><strong>{status}</strong>{moderation && <small>这是结构化内容审核错误，不代表已确认上游封禁。请查看研究详情中的审核诊断。</small>}{unknown && <small>可能已被上游计费；记录金额是保守估算，用量未知不等于零。</small>}{error && <details><summary>查看请求错误</summary><HumanMessage text={textOf(error)} /></details>}</div>;
}

export function BillingRateDetails({ record }: { record: Json }) {
  const rates = record.rate_snapshot;
  const verified = typeof rates?.price_verified === 'boolean' ? rates.price_verified : record.price_verified === true;
  const labels: Record<string, string> = { input_price: '输入', output_price: '输出', cached_input_price: '缓存读取', cache_write_price: '缓存写入' };
  return <div className="billing-rate-details"><small>{verified ? '费率已核实' : '费率未核实'}{isUnknownCharge(record) ? ' · 扣费未知' : record.cost_estimated || !record.price_verified ? ' · 费用估算' : ''}</small>{rates && <details><summary>调用时费率快照</summary><dl>{Object.entries(labels).map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{rates[key] == null ? '未定价' : `$${number(rates[key]).toLocaleString('en-US', { maximumFractionDigits: 6 })} / 100万词元`}</dd></div>)}</dl><p>历史记录采用请求发生时的费率，不随当前设置重算。</p></details>}</div>;
}
