import { useState } from 'react';
import { ArrowUpRight, Check, CheckCircle2, Circle, Code2, Copy, FolderOpen, Pause, RefreshCw } from 'lucide-react';
import { date, type Json } from './api';
import { useRemoteResource } from './useRemoteResource';

type Identity = { runId: string; workflowId: string; cycle: number; runStatus: string };
const phases: Record<string, [string, string]> = {
  implementing: ['编写与检查代码', '完成代码交付后，进行本地验收和独立代码审核。'],
  acceptance: ['本地代码验收', '核对交付文件及本地检查结果，再交独立审核。'],
  code_review: ['独立代码审核', '审核通过后，按批准的计划进入实验验证。'],
  completed: ['代码交付已通过', '正式实验的执行与结果在实验阶段记录。'],
  blocked: ['代码流程等待处理', '处理当前阻塞后，从已保存的执行记录继续。'],
};
const activityTypes: Record<string, string> = { commandExecution: '本地命令', fileChange: '代码文件变更', contextCompaction: '上下文整理' };
const activityStatuses: Record<string, string> = { inProgress: '进行中', completed: '已结束', failed: '失败', declined: '未执行' };
const rows = (value: unknown): Json[] => Array.isArray(value) ? value.filter(v => v && typeof v === 'object') : [];
const roles = ['director', 'engineer', 'scout'];
const approved = (value: unknown) => value === true || value === 1;
const hasFindings = (value: Json) => ['issues', 'fixes', 'required_changes'].some(key => Array.isArray(value[key]) && value[key].length > 0);
const matchesDelivery = (record: Json | undefined, attempt: Json) => ['code_sha256', 'delivery_hash'].every(key =>
  typeof attempt[key] === 'string' && !!attempt[key] && record?.[key] === attempt[key]);

// Count one independently bound verdict per role, just as the detailed review does.
function approvedReviews(current: Json, attempt: Json) {
  const binding = { proposal_hash: current.proposal_hash, spec_hash: current.spec_hash,
    code_sha256: attempt.code_sha256, delivery_hash: attempt.delivery_hash };
  const reviews = rows(attempt.review_votes);
  return roles.filter(role => {
    const votes = reviews.filter(v => v.role === role);
    return votes.length === 1 && approved(votes[0].approve) && !hasFindings(votes[0])
      && Object.entries(binding).every(([key, value]) => typeof value === 'string' && !!value && votes[0][key] === value);
  }).length;
}

export function nativeCodeProgress(value: Json | null | undefined, identity: Identity) {
  const current = value?.current;
  if (!current || current.run_id !== identity.runId || current.id !== identity.workflowId
      || current.cycle !== identity.cycle || !Object.hasOwn(phases, current.status)) return null;
  const attempt = rows(current.attempts).find(a => a.index === current.current_revision);
  if (!attempt) return null;
  const receipt = attempt.checkpoint ?? {};
  const reports = rows(receipt.public_messages).filter(m => m.phase === 'commentary' && typeof m.text === 'string' && m.text.trim()).slice(-3);
  const activities = rows(receipt.activities).filter(a => Object.hasOwn(activityTypes, a.type)).slice(-8);
  const stopped = identity.runStatus !== 'running' || current.status === 'blocked'
    || ['unknown', 'blocked', 'failed', 'interrupted'].includes(receipt.status);
  let [label, next] = phases[current.status];
  if (current.status === 'implementing' && receipt.status === 'completed') label = 'Codex 已返回，等待产物核验';
  if (stopped && current.status !== 'completed') label = identity.runStatus === 'paused' ? '代码流程已暂停' : '代码流程等待处理';
  if (stopped && current.status !== 'completed') next = '先核对原请求回执，恢复执行后继续代码交付与审核。';
  if (current.reason) next = String(current.reason);
  const host = attempt.execution_progress;
  const hostActive = host && (current.status === 'acceptance' || current.status === 'code_review'
    || (current.status === 'implementing' && receipt.status === 'completed'));
  return { label, next, reports, activities, stopped, phase: current.status,
    lastEventAt: hostActive ? host.updated_at : receipt.last_event_at ?? attempt.last_activity_at,
    host: hostActive ? host : null, previous: current.previous_attempt,
    startedAt: receipt.turn_dispatched_at, fileCount: attempt.snapshot_file_count ?? rows(attempt.files).length,
    deliverySelection: attempt.delivery_selection,
    receiptStatus: receipt.status, workspace: current.workspace, projectRoot: current.project_root,
    revision: attempt.index, approvedReviews: approvedReviews(current, attempt),
    acceptancePassed: approved(attempt.acceptance?.approved) && matchesDelivery(attempt.acceptance, attempt) && !hasFindings(attempt.acceptance),
    files: rows(attempt.files), acceptance: attempt.acceptance,
    reviews: rows(attempt.review_votes).filter(r => ['director', 'engineer', 'scout'].includes(r.role)), updatedAt: current.updated_at };
}

function ProgressReport({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(true);
  const lengthy = text.length > 100 || text.split('\n').length > 3;
  return <div className="native-code-report" aria-live="polite"><h4>最新进展 <span>Codex 上报</span></h4>
    <p className={lengthy && !expanded ? 'is-collapsed' : undefined}>{text}</p>
    {lengthy && <button type="button" className="native-code-text-button" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? '收起进展' : '展开完整进展'}</button>}
  </div>;
}

const variants: Record<string, string> = { baseline: '基线', proposed: '候选方法', ablation: '消融' };
function elapsed(start: unknown, now: number) {
  if (typeof start !== 'string' || !Number.isFinite(Date.parse(start))) return '';
  const seconds = Math.max(0, Math.floor((now - Date.parse(start)) / 1000));
  return seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

export function ActivityRows({ activities }: { activities: Json[] }) {
  return <ol className="native-code-activity-list">{[...activities].reverse().map((a, i) => <li key={a.id ?? i}>
    <div><strong>{a.summary || activityTypes[a.type]}</strong><span>{activityStatuses[a.status] ?? '状态未记录'}</span></div>
    {Array.isArray(a.paths) && a.paths.length > 0 && <code>{a.paths.join('\n')}</code>}
    <small>{date(a.observed_at, true)}{typeof a.duration_seconds === 'number' ? ` · ${Math.round(a.duration_seconds)} 秒` : ''}
      {Number.isInteger(a.exit_code) ? ` · 退出码 ${a.exit_code}` : ''}</small>
  </li>)}</ol>;
}

export function PreviousAttempt({ attempt }: { attempt?: Json }) {
  if (!attempt || attempt.status !== 'rejected') return null;
  const issues: string[] = Array.isArray(attempt.issues) ? [...new Set<string>(attempt.issues)] : [];
  return <div className="native-code-obstacles"><h4>本次需要修复的问题</h4>
    <p>第 {attempt.index + 1} 次交付未通过，当前修复仍待重新验收。</p>
    {issues.length > 0 && <ul>{issues.slice(0, 4).map(issue => <li key={issue}>{issue}</li>)}</ul>}
    {issues.length > 4 && <details><summary>其余 {issues.length - 4} 项问题</summary><ul>{issues.slice(4).map(issue => <li key={issue}>{issue}</li>)}</ul></details>}
    {rows(attempt.smoke).length > 0 && <div className="native-code-branches">{rows(attempt.smoke).map((check, i) => <p key={i}>
      {variants[check.variant] ?? check.variant}：{approved(check.passed) ? '通过' : '未通过'}
      {typeof check.duration_seconds === 'number' ? ` · ${Math.round(check.duration_seconds)} 秒` : ''}</p>)}</div>}
  </div>;
}

function CodeDirectory({ workspace, projectRoot }: { workspace: string; projectRoot?: string }) {
  const [copied, setCopied] = useState('');
  async function copy() {
    try { await navigator.clipboard.writeText(workspace); setCopied('目录已复制'); }
    catch { setCopied('复制失败，请选中下方路径复制'); }
  }
  return <details className="native-code-directory"><summary><FolderOpen size={14} /><span>代码目录{projectRoot && <small>{projectRoot.split(/[\\/]/).filter(Boolean).at(-1)}</small>}</span></summary>
    <code>{workspace}</code><button type="button" className="btn small" onClick={() => void copy()}><Copy size={13} />复制代码目录</button>
    {copied && <span className="native-code-copy-status" role="status">{copied}</span>}
  </details>;
}

export function NativeCodeProgressCard({ value, identity, error = '', loading = false, now = Date.now(), onDetails, onRefresh }: {
  value?: Json | null; identity: Identity; error?: string; loading?: boolean; now?: number; onDetails?: () => void; onRefresh?: () => void;
}) {
  const progress = nativeCodeProgress(value, identity);
  const heading = <header className="native-code-heading"><div><span className="native-code-icon"><Code2 size={18} /></span><div><h3>代码实现</h3><small>Codex 实时进度</small></div></div>
    {onRefresh && <button type="button" className="native-code-refresh" aria-label="刷新代码进度" title="刷新代码进度" disabled={loading} onClick={onRefresh}><RefreshCw size={14} className={loading ? 'spin' : ''} /></button>}</header>;
  if (!progress) return <section className="panel native-code-progress" aria-label="Codex 实时进度">
    {heading}<p role="status">{error ? '暂时无法读取执行进度，请重新同步。' : loading ? '正在读取当前代码任务的执行回执…' : '尚未取得当前代码任务的执行回执。'}</p>
  </section>;
  const last = progress.reports.at(-1);
  const stamp = typeof progress.lastEventAt === 'string' ? Date.parse(progress.lastEventAt) : NaN;
  const stale = !progress.stopped && progress.phase === 'implementing' && progress.receiptStatus === 'running'
    && Number.isFinite(stamp) && now - stamp > 180000;
  const completed = progress.phase === 'completed';
  const StatusIcon = completed ? CheckCircle2 : progress.stopped ? Pause : Code2;
  const checkpoints = [
    { label: '代码交付', value: progress.fileCount ? progress.deliverySelection?.scope === 'source_and_test_results' ? `${progress.fileCount} 项源码与测试已保存` : '旧版完整目录已保存' : '尚未提交', done: progress.fileCount > 0 },
    { label: '本地验收', value: progress.acceptancePassed ? '已通过' : approved(progress.acceptance?.approved) ? '版本待核对' : progress.acceptance?.present ? '未通过' : '尚未完成', done: progress.acceptancePassed },
    { label: '独立代码审核', value: `${progress.approvedReviews} / 3 通过`, done: progress.approvedReviews === 3 },
  ];
  return <section className="panel native-code-progress" aria-label="Codex 实时进度">
    {heading}
    <div className={`native-code-state ${completed ? 'is-complete' : progress.stopped ? 'is-stopped' : 'is-active'}`}><StatusIcon size={15} /><strong>{progress.label}</strong></div>
    <div className="native-code-meta"><span>第 {identity.cycle} 轮 · 第 {progress.revision + 1} 次实现</span><span>{progress.stopped ? '已保存记录' : '执行回执'}</span></div>
    {progress.receiptStatus === 'running' && !progress.stopped && <p className="native-code-time">本次执行已持续 {elapsed(progress.startedAt, now) || '时间待记录'}</p>}
    {error && <p className="inline-warning" role="status">进度读取失败，以下为上次取得的记录，可刷新重试。</p>}
    {stale && <p className="inline-warning" role="status">最近 3 分钟未收到新执行事件，暂不能确认仍在推进；正在等待下一条回执。</p>}
    <ol className="native-code-checkpoints" aria-label="代码验收状态">{checkpoints.map(item => <li key={item.label} className={item.done ? 'is-done' : ''}>
      {item.done ? <Check size={13} /> : <Circle size={13} />}<span>{item.label}</span><strong>{item.value}</strong>
    </li>)}</ol>
    {progress.host && <div className="native-code-host" aria-label="后端当前步骤"><h4>后端当前步骤</h4><p>{progress.host.label}</p>
      <small>本步骤已持续 {elapsed(progress.host.started_at, now)}{typeof progress.host.total === 'number' ? ` · 已完成 ${progress.host.completed ?? 0}/${progress.host.total} 个分支` : ''}
        {typeof progress.host.timeout_seconds === 'number' ? ` · 单分支时限 ${progress.host.timeout_seconds} 秒` : ''}</small>
      {rows(progress.host.checks).map((check, i) => <p key={check.id ?? i}>{i + 1}. {approved(check.passed) ? '通过' : '未通过'}
        {typeof check.duration_seconds === 'number' ? ` · ${Math.round(check.duration_seconds)} 秒` : ''}</p>)}
    </div>}
    <ProgressReport key={last?.id ?? last?.text ?? 'pending'} text={last?.text ?? (progress.stopped ? '本次请求尚无已保存的进展报告。' : '尚无具体进展报告；等待执行回执。')} />
    <p className="native-code-time">执行回执更新 <time>{Number.isFinite(stamp) ? date(progress.lastEventAt, true) : '尚未记录'}</time></p>
    {progress.activities.length > 0 && <div className="native-code-live-activity" aria-label="最近实际执行"><h4>最近实际执行</h4><ActivityRows activities={progress.activities.slice(-4)} /></div>}
    <PreviousAttempt attempt={progress.previous} />
    <div className="native-code-next"><h4>下一步</h4><p>{progress.next}</p></div>
    {typeof progress.workspace === 'string' && progress.workspace && <CodeDirectory key={progress.workspace} workspace={progress.workspace} projectRoot={progress.projectRoot} />}
    {(progress.activities.length > 0 || progress.reports.length > 1) && <details className="native-code-history"><summary>执行活动与历史进展</summary>
      {progress.reports.length > 1 && <div><h4>之前的进展报告</h4>{progress.reports.slice(0, -1).map((m, i) => <p key={m.id ?? i}>{m.text}</p>)}</div>}
      {progress.activities.length > 0 && <div><h4>最近执行活动</h4><ActivityRows activities={progress.activities} /></div>}</details>}
    {onDetails && <button type="button" className="btn native-code-details" onClick={onDetails}>查看实现与审核详情<ArrowUpRight size={14} /></button>}
    <footer>运行时每 5 秒同步。上报进展不代表验收通过。</footer>
  </section>;
}

export function NativeCodeProgress(props: Identity & { onDetails?: () => void }) {
  const [refresh, setRefresh] = useState(0);
  const request = useRemoteResource<Json>(`/runs/${encodeURIComponent(props.runId)}/local-codex`,
    `${props.workflowId}:${props.cycle}:${props.runStatus}:${refresh}`, props.runStatus === 'running' ? 5000 : 0);
  return <NativeCodeProgressCard value={request.data} identity={props} error={request.error} loading={request.loading}
    onDetails={props.onDetails} onRefresh={() => setRefresh(v => v + 1)} />;
}
