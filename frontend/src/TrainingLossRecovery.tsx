import { useEffect, useRef, useState } from 'react';
import { api, post, type Json } from './api';

const labels: Record<string, string> = {running: '正在复核', completed: '复核已完成', interrupted: '复核已暂停', blocked: '复核待恢复'};

export function TrainingLossRecovery({ runId, value, refresh }: { runId: string; value?: Json; refresh: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const activeRun = useRef(runId);
  useEffect(() => { activeRun.current = runId; setBusy(false); setError(''); }, [runId]);
  if (!value?.available) return null;
  const record = value.record;
  const running = record?.status === 'running';
  const completed = record?.status === 'completed';
  const start = async () => {
    setBusy(true); setError('');
    try {
      const path = `/runs/${encodeURIComponent(runId)}/training-loss-review`;
      const preview = await api<Json>(path);
      if (activeRun.current !== runId) return;
      await post(path, {snapshot_id: preview.snapshot_id});
      if (activeRun.current === runId) refresh();
    } catch (e) {
      if (activeRun.current === runId) setError(e instanceof Error ? e.message : String(e));
    } finally { if (activeRun.current === runId) setBusy(false); }
  };
  return <section className="loss-review-recovery" aria-label="既有 loss 复核">
    <h4>停止训练后的三方诊断 {record && <span>· {labels[record.status] ?? record.status}</span>}</h4>
    <p>复核已有验证日志，由三位 agent 分析明确告警并审查建议。保留原验收结论与停止规则，不重跑实验；建议不构成继续训练的授权。</p>
    {record && <p>已记录 {record.completed_trial_ids?.length ?? 0} 项诊断。{completed ? '选择对应实验查看已保存的三方意见。' : '暂停后会复用已保存意见继续。'}</p>}
    {record?.error && <p role="alert">{record.error}</p>}
    {error && <p role="alert">{error}</p>}
    <button type="button" className="btn small" disabled={busy || running || completed} onClick={start}>{busy ? '正在核对证据…' : running ? '三方诊断进行中' : completed ? '本份证据已完成复核' : record ? '恢复三方 loss 诊断' : '复核已有 loss · 三 agent 讨论'}</button>
  </section>;
}
