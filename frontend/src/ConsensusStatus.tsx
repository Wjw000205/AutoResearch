import { ReferenceText } from './ReferenceNames';
import { ArrowRight, CheckCircle2, Clock3, LoaderCircle } from 'lucide-react';
import { useEffect, useState } from 'react';
import { api, date, type Json } from './api';
import { HumanMessage } from './localization';
import './consensus-status.css';

const roles: Record<string, string> = { director: '研究总监', engineer: '实验工程师', scout: '文献研究员' };
const phases: Record<string, string> = { integrating: '整合新版方案', supplementing: '补充证据与方案', voting: '三方独立复审', ready: '准备进入架构设计', awaiting_revision: '核对推进条件' };
export function consensusElapsed(seconds?: number | null) {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return '尚未记录';
  return seconds >= 60 ? `${Math.floor(seconds / 60)} 分 ${Math.floor(seconds % 60)} 秒` : `${Math.floor(seconds)} 秒`;
}

export interface ConsensusSnapshot {
  run_id: string; derived: boolean; checked_at: string; run_status: string;
  stage: string; stage_label: string; activity: Json | null;
}

export function currentConsensusStatus(runId: string, snapshot?: ConsensusSnapshot | null, fallback?: Json | null) {
  const saved = fallback?.run_id === runId && fallback.derived === true ? fallback : null;
  const checked = Date.parse(snapshot?.checked_at ?? '');
  const savedAt = Date.parse(saved?.checked_at ?? '');
  if (!snapshot || snapshot.run_id !== runId || snapshot.derived !== true || !Number.isFinite(checked)
    || (Number.isFinite(savedAt) && checked < savedAt)) return { activity: saved, stageLabel: null };
  if (snapshot.stage !== 'debate') {
    return snapshot.activity === null
      ? { activity: null, stageLabel: saved ? snapshot.stage_label : null }
      : { activity: saved, stageLabel: null };
  }
  const activity = snapshot.activity;
  if (!activity || activity.run_id !== runId || activity.derived !== true || activity.run_status !== snapshot.run_status)
    return { activity: saved, stageLabel: null };
  return { activity, stageLabel: null };
}

export function LiveConsensusStatus({ runId, revision, fallback, onDetails }: {
  runId: string; revision: number; fallback?: Json | null; onDetails?: () => void;
}) {
  const [snapshot, setSnapshot] = useState<ConsensusSnapshot | null>(null);
  const [refreshError, setRefreshError] = useState(false);
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;
    setSnapshot(null); setRefreshError(false);
    async function load() {
      controller = new AbortController();
      const timeout = setTimeout(() => controller?.abort(), 15000);
      try {
        const value = await api<ConsensusSnapshot>(`/runs/${encodeURIComponent(runId)}/consensus-status`, { signal: controller.signal });
        if (alive && value.run_id === runId && value.derived === true) { setSnapshot(value); setRefreshError(false); }
      } catch { if (alive) setRefreshError(true); }
      finally {
        clearTimeout(timeout);
        if (alive) timer = setTimeout(() => void load(), 5000);
      }
    }
    void load();
    return () => { alive = false; clearTimeout(timer); controller?.abort(); };
  }, [runId, revision]);
  const selected = currentConsensusStatus(runId, snapshot, fallback);
  return <>
    {selected.stageLabel && <div className="panel consensus-status" role="status">当前阶段：<strong>{selected.stageLabel}</strong>。完整研究记录正在刷新。</div>}
    <ConsensusStatus runId={runId} activity={selected.activity} onDetails={onDetails} />
    {refreshError && selected.activity && <p className="panel-note" role="status">状态暂未刷新，以上保留最近一次读取结果。正在重新读取；这不代表研究请求已停止。</p>}
  </>;
}

export function ConsensusStatus({ runId, activity, onDetails }: { runId: string; activity?: Json | null; onDetails?: () => void }) {
  if (!activity || activity.run_id !== runId || activity.derived !== true) return null;
  const running = activity.run_status === 'running';
  const ready = activity.ready_for_next_stage === true;
  const votes: Json[] = Array.isArray(activity.votes) ? activity.votes : [];
  const requests: Json[] = Array.isArray(activity.ongoing_requests) ? activity.ongoing_requests : [];
  const blockers: string[] = Array.isArray(activity.blocking_issues) ? activity.blocking_issues : [];
  const delivered: string[] = activity.work_progress?.delivered_roles ?? [];
  return <section className={`consensus-status panel ${ready ? 'consensus-ready' : ''}`} aria-label="共识当前状态">
    <div className="consensus-status-heading"><div>{ready ? <CheckCircle2 size={20} /> : running ? <LoaderCircle size={20} className="spin" /> : <Clock3 size={20} />}<div><small>想法共识 · 当前状态</small><h3><ReferenceText value={phases[activity.phase] ?? '核对推进条件'} /><ReferenceText value={!running && !ready ? ' · 已停止' : ''} /></h3></div></div><button className="btn" disabled={!onDetails} onClick={onDetails}>{onDetails ? '查看评议详情' : '评议详情加载中'}<ArrowRight size={14} /></button></div>
    <HumanMessage text={activity.action} />
    <div className="consensus-status-metrics"><span>本项已耗时 <strong>{consensusElapsed(activity.elapsed_seconds)}</strong></span><span>{activity.phase === 'integrating' ? '新版投票' : '当前版本投票'} <strong>{activity.approved_votes ?? 0} / {activity.expected_votes ?? 3} 同意</strong></span><span>下一阶段 <strong>{activity.next_stage_label}</strong> · {ready ? '已满足共识条件' : '尚未满足共识条件'}</span></div>
    {activity.phase === 'integrating' && <p className="panel-note">第 <ReferenceText value={activity.previous_round} /> 轮评议已保存。补证据交付：<ReferenceText value={delivered.map(role => roles[role] ?? role).join('、') || '尚未交付'} />；新版方案尚未提交，随后开始第 <ReferenceText value={activity.next_round} /> 轮独立复审。</p>}
    <div className="consensus-role-states">{votes.map(vote => <span key={vote.role}>{roles[vote.role] ?? vote.role}<strong>{vote.status === 'approved' ? '同意' : vote.status === 'changes_requested' ? '要求修改' : '待交付评议'}</strong></span>)}</div>
    {requests.map((request, index) => <div key={index}><p className="consensus-request"><Clock3 size={14} /><ReferenceText value={roles[request.role] ?? '研究角色'} />：<ReferenceText value={request.action} /> · 当前请求 <ReferenceText value={consensusElapsed(request.elapsed_seconds)} />{request.last_activity_at && <small>最近活动 {date(request.last_activity_at, true)}</small>}</p>{request.connection_note && <p className="panel-note"><HumanMessage text={request.connection_note} /></p>}</div>)}
    {blockers.length > 0 && <details className="consensus-blockers"><summary>{activity.blocker_scope === 'previous_proposal_pending_recheck' ? '上一版阻塞项，待新版复核' : '当前证据阻塞项'}：{blockers.length} 项</summary><ul>{blockers.map((issue, index) => <li key={index}><HumanMessage text={issue} /></li>)}</ul></details>}
    {activity.stop_reason && <HumanMessage text={activity.stop_reason} />}
    <p className="consensus-next">推进条件：<ReferenceText value={activity.advance_condition} /></p><p className="panel-note">完成时间待定：<ReferenceText value={activity.eta_reason} /> 状态核对于 <ReferenceText value={date(activity.checked_at, true)} />。</p>
  </section>;
}
