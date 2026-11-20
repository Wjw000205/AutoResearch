import { type Json } from './api';
import { currentCodeReview, codeReviewStatus } from './implementationCodeReview';
import { useRemoteResource } from './useRemoteResource';

const names: Record<string, string> = { director: '研究总监', engineer: '实验工程师', scout: '文献研究员' };
const states: Record<string, string> = { running: '正在审核', pending: '等待审核', queued: '等待接收',
  passed: '已通过', approved: '已通过', changes_required: '要求修改', changes_requested: '要求修改',
  interrupted: '已中断', blocked: '等待处理', failed: '调用失败', paused: '已暂停',
  binding_mismatch: '版本待核对', version_mismatch: '版本待核对', conflicting_records: '意见记录冲突' };

export function CodeReviewSnapshot({ progress, response, runId, error = '' }: {
  progress: Json; response?: Json | null; runId: string; error?: string;
}) {
  if (progress.run_id !== runId || !['code_review', 'completed'].includes(progress.phase)) return null;
  const value = currentCodeReview(progress, response, runId);
  return <section className="code-review-live" aria-label="当前代码审核进度">
    <header><strong>当前代码审核</strong><span>{value ? `第 ${value.revision + 1} 次交付` : '正在核对当前版本'}</span></header>
    {value ? <><p className="code-review-counts"><strong>最终意见已返回 {value.saved} / {value.expected}</strong>
      <span>通过 {value.approved} · 要求修改 {value.changes}</span></p>
      <progress aria-label="最终代码审核意见返回数量" max={value.expected} value={value.saved} />
      <ul className="code-review-roles">{value.rows.map(row => {
        const recorded = codeReviewStatus(row);
        const state = recorded === 'running' && progress.run_status !== 'running'
          ? progress.run_status : recorded;
        const label = row.binding_valid !== true && ['passed', 'approved', 'changes_required', 'changes_requested'].includes(state)
          ? '意见版本待核对' : states[state] ?? '状态待同步';
        return <li key={row.role}><span>{names[row.role]}</span><strong>{label}</strong></li>;
      })}</ul>
      <small>计数表示最终意见返回情况，阅读与工具调用不计为审核通过。</small></>
      : <p role="status">{error ? '代码审核进度读取失败，请稍后刷新。' : '正在同步审核者的当前版本回执…'}</p>}
    {error && value && <p className="muted" role="status">进度刷新失败，当前展示最近保存的记录。</p>}
  </section>;
}

export function LiveCodeReviewProgress({ progress, runId }: { progress: Json; runId: string }) {
  const visible = progress.run_id === runId && ['code_review', 'completed'].includes(progress.phase);
  const resource = useRemoteResource<Json>(visible ? `/runs/${encodeURIComponent(runId)}/implementation-progress` : null,
    `${runId}:${progress.workflow?.id ?? ''}:${progress.workflow?.revision ?? ''}:${progress.phase}`,
    progress.run_status === 'running' ? 5000 : 15000);
  return visible ? <CodeReviewSnapshot progress={progress} response={resource.data} runId={runId} error={resource.error} /> : null;
}
