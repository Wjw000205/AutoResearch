import { ReferenceText } from './ReferenceNames';
import type { Json } from './api';

export const callBudgetCount = (value: unknown): number | null =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null;

export function taskCallBudget(runId: string, summary?: Json | null) {
  return summary?.run_id === runId && summary.source === 'budgets.calls'
    ? callBudgetCount(summary.used) : null;
}

export function TaskCallBudget({ runId, summary, limit }: { runId: string; summary?: Json | null; limit?: unknown }) {
  const used = taskCallBudget(runId, summary);
  const maximum = callBudgetCount(limit);
  return <div className="kv" aria-label="任务调用预算"><span>任务调用预算</span><strong>
    {used === null ? '未报告' : used.toLocaleString('zh-CN')} <em>/ {limit === null ? '无次数上限' : maximum === null ? '未报告' : maximum.toLocaleString('zh-CN')}</em>
  </strong></div>;
}

export function TaskCallBudgetNote({ providerRequests }: { providerRequests?: unknown }) {
  const count = callBudgetCount(providerRequests);
  return <p className="panel-note">调用预算包含本机 Codex 和供应商请求；已准入的等待回复与失败请求也计入，复用已保存回复不增加次数。
    供应商请求单独统计：<ReferenceText value={count === null ? '未报告' : `${count.toLocaleString('zh-CN')} 次`} />。</p>;
}
