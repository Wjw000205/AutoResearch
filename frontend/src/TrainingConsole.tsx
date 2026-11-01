import { useEffect, useRef, useState } from 'react';
import { ArrowDownToLine, Pause, Play, RefreshCw, Terminal } from 'lucide-react';
import { type Json } from './api';
import { useRemoteResource } from './useRemoteResource';
import { TrainingConvergence } from './TrainingConvergence';
import { TrainingLossRecovery } from './TrainingLossRecovery';
import './training-console.css';
import { ReferenceText } from './ReferenceNames';

const statusNames: Record<string, string> = { running: '训练运行中', completed: '已完成', failed: '失败', interrupted: '已中断', cancelled: '已取消', memory_limit: '触发内存限制', disk_limit: '触发磁盘限制', timeout: '触发时间限制' };
const variants: Record<string, string> = { baseline: '基线', proposed: '候选方法', ablation: '消融' };
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
const numeric = (value: unknown) => finite(value) ? Number.isInteger(value) ? String(value) : value.toPrecision(6) : '—';
const mib = (value: unknown) => finite(value) ? `${(value / 1048576).toFixed(1)} MiB` : '未报告';

export function trainingLines(value: Json | null): string {
  if (!value?.selected) return '等待正式实验记录。';
  const row = value.selected;
  const lines = [`> ${row.id} · ${row.params?.algorithm ?? row.params?.method ?? variants[row.params?.variant] ?? '实验'} · seed ${row.params?.seed ?? '未报告'}`,
    `[状态] ${statusNames[row.status] ?? row.status} · attempt ${row.attempt ?? '—'}`];
  if (row.time_limits?.policy === 'observed_resources_without_timing') lines.push('[运行策略] 计时门槛与训练时限已取消，资源保护保留');
  if (value.events_truncated) lines.push(`[训练日志] 显示最近 ${value.events.length} / ${value.event_count} 条，原记录保留`);
  for (const event of value.events ?? []) {
    const parts = [['epoch', event.epoch], ['stage', event.stage], ['fit', event.fit_id], ['loss', event.loss], [event.validation_metric ?? 'validation', event.validation_value], ['lr', event.learning_rate], ['event', event.event], ['reason', event.reason]]
      .filter(([, v]) => v !== undefined && v !== null).map(([key, v]) => `${key}=${typeof v === 'number' ? numeric(v) : String(v)}`);
    if (parts.length) lines.push(parts.join('  '));
  }
  if (!value.event_count) lines.push('[训练日志] 此程序尚未保存训练损失日志；运行观察与最终结果会按实际记录显示。');
  const observation = value.resource_observation;
  if (observation) lines.push(`[运行观察] ${observation.location ?? '未报告位置'} · 检查 ${observation.checks ?? '—'} 次 · RSS ${mib(observation.rss_bytes)} · 显存 ${mib(observation.torch_peak_reserved_bytes)}`);
  if (value.training?.completed || value.training?.runtime_stability?.completed) {
    const counters = value.training.counters ?? value.counters;
    lines.push(`[训练完成] ${counters ? `${counters.global_epoch ?? '—'} epoch` : `选择拟合 ${value.training.selection_fits ?? '—'} / 最终拟合 ${value.training.final_fits ?? '—'}`}`);
    if (value.training.model_sha256) lines.push(`[模型] ${value.training.model_sha256}`);
  }
  for (const [name, metric] of Object.entries(row.metrics ?? {})) lines.push(`[结果] ${name}=${numeric(metric)}`);
  if (row.error) lines.push(`[错误] ${row.error}`);
  for (const issue of row.evaluation_errors ?? []) lines.push(`[评估检查] ${issue}`);
  for (const notice of value.notices ?? []) lines.push(`[读取提示] ${notice}`);
  return lines.join('\n');
}

export function TrainingCurve({ points }: { points: Json[] }) {
  const valid = points.filter(p => finite(p.epoch) && finite(p.loss));
  if (valid.length < 2) return null;
  const x0 = Math.min(...valid.map(p => p.epoch)), x1 = Math.max(...valid.map(p => p.epoch));
  const y0 = Math.min(...valid.map(p => p.loss)), y1 = Math.max(...valid.map(p => p.loss));
  const segments: Json[][] = [];
  for (const point of valid) {
    const current = segments.at(-1), previous = current?.at(-1);
    if (!previous || previous.segment !== point.segment || point.epoch < previous.epoch) segments.push([point]);
    else current!.push(point);
  }
  const coordinate = (p: Json) => `${40 + (p.epoch - x0) / (x1 - x0 || 1) * 680},${12 + (y1 - p.loss) / (y1 - y0 || 1) * 70}`;
  return <figure className="training-curve"><figcaption>训练 loss <span>每个 epoch 的末次保存观测 · 显示 {valid.length} 个点</span></figcaption><svg viewBox="0 0 740 110" role="img" aria-label="已保存的训练损失曲线"><line x1="40" x2="720" y1="82" y2="82" stroke="currentColor" opacity=".2" />{segments.map((segment, index) => <polyline key={index} points={segment.map(coordinate).join(' ')} fill="none" stroke="#4ebca9" strokeWidth="2" />)}<text x="4" y="20">{y1.toPrecision(3)}</text><text x="4" y="82">{y0.toPrecision(3)}</text><text x="40" y="104">epoch {x0}</text><text x="720" y="104" textAnchor="end">{x1}</text></svg></figure>;
}

export function TrainingConsole({ runId, trialIds, initialTrialId, curveFirst = false }: { runId: string; trialIds?: string[]; initialTrialId?: string; curveFirst?: boolean }) {
  const [selected, setSelected] = useState('');
  const [channel, setChannel] = useState('training');
  const [refresh, setRefresh] = useState(0);
  const [live, setLive] = useState(true);
  const [follow, setFollow] = useState(true);
  const [showRaw, setShowRaw] = useState(false);
  const viewport = useRef<HTMLPreElement>(null);
  const effectiveTrial = selected || initialTrialId;
  const path = `/runs/${encodeURIComponent(runId)}/training-console${effectiveTrial ? `?trial_id=${encodeURIComponent(effectiveTrial)}` : ''}`;
  const remote = useRemoteResource<Json>(path, refresh, live ? 3000 : 0);
  const [saved, setSaved] = useState<{ path: string; data: Json } | null>(null);
  useEffect(() => { if (remote.data) setSaved({ path, data: remote.data }); }, [path, remote.data]);
  const value = remote.data ?? (saved?.path === path ? saved.data : null);
  const row = value?.selected;
  const structured = channel === 'stdout' && value?.stdout?.large_structured_output;
  useEffect(() => { setShowRaw(false); }, [selected, channel]);
  const text = channel === 'training' ? trainingLines(value) : value?.[channel]?.text || `尚无${channel === 'stderr' ? '错误' : '标准'}输出。`;
  useEffect(() => { if (follow && viewport.current) viewport.current.scrollTop = viewport.current.scrollHeight; }, [text, follow, selected, channel, showRaw]);
  const counters = value?.training?.counters ?? value?.counters;
  return <section className="training-console" aria-label="训练控制台">
    <header className="training-console-header"><div><Terminal size={17} /><h3>训练控制台</h3><span className={live ? 'console-live' : 'console-paused'}>{live ? '每 3 秒刷新' : '刷新已暂停'}</span></div><div className="console-actions">
      <button type="button" onClick={() => setLive(!live)} aria-label={live ? '暂停控制台刷新' : '恢复控制台刷新'}>{live ? <Pause size={14} /> : <Play size={14} />}{live ? '暂停刷新' : '继续刷新'}</button>
      <button type="button" onClick={() => setRefresh(v => v + 1)} aria-label="重新读取训练控制台"><RefreshCw size={14} /></button></div></header>
    <div className="training-console-toolbar"><label>实验<select aria-label="选择训练实验" value={selected} onChange={e => { setSelected(e.target.value); setFollow(true); }}><option value="">{trialIds?.length === 1 ? "当前选定实验" : trialIds ? "当前数据集最近实验" : "自动跟随当前 / 最近实验"}</option>{(value?.trials ?? []).filter((trial: Json) => !trialIds || trialIds.includes(trial.id)).map((trial: Json) => <option key={trial.id} value={trial.id}>{trial.id} · {trial.params?.method ?? trial.params?.algorithm ?? variants[trial.params?.variant] ?? '实验'} · seed {trial.params?.seed ?? '—'}</option>)}</select></label><span className="console-trial-state">{row ? statusNames[row.status] ?? row.status : '等待实验'}</span></div>
    {row && <div className="console-facts"><span>当前 <b>{row.id}</b></span><span>epoch <b>{counters?.global_epoch ?? '未报告'}</b></span><span>进程内存 <b>{mib(value?.resource_observation?.rss_bytes)}</b></span><span>模型显存 <b>{mib(value?.resource_observation?.torch_peak_reserved_bytes)}</b></span></div>}
    {row?.params && <details className="console-parameters"><summary>训练参数 · 当前选定实验</summary>{row.params_complete === false && <p>参数摘要不完整，完整配置保留在原实验记录。</p>}<pre><ReferenceText value={row.params} /></pre></details>}
    {remote.error && <p className="console-read-error" role="alert">读取失败：{remote.error}{value ? '（保留上次读取内容）' : ''} <button type="button" onClick={() => setRefresh(v => v + 1)}>重试</button></p>}
    {value?.truncated && <p className="console-tail-note">显示最近 256 条实验；更早记录保留在研究归档中。</p>}
    {remote.loading && !value && <p className="console-loading" role="status">正在读取训练记录…</p>}
    {curveFirst && channel === 'training' && <TrainingCurve points={value?.curve ?? []} />}
    <div className="console-channel-tabs" role="tablist" aria-label="控制台输出通道">{[['training', '训练日志'], ['stdout', '标准输出'], ['stderr', '错误输出']].map(([key, label]) => <button key={key} type="button" role="tab" aria-selected={channel === key} onClick={() => setChannel(key)}>{label}{key === 'stderr' && value?.stderr?.bytes ? ' •' : ''}</button>)}<button type="button" className={follow ? 'following' : ''} onClick={() => setFollow(!follow)} aria-pressed={follow}><ArrowDownToLine size={13} />{follow ? '跟随末尾' : '已暂停滚动'}</button></div>
    {channel !== 'training' && value?.[channel]?.truncated && <p className="console-tail-note">仅展示最近 48 KiB；完整输出保存在原实验目录。</p>}
    {structured && <div className="console-structured"><p>标准输出是大型 JSON 结果（{mib(value?.stdout?.bytes)}），原始片段默认收起。训练过程请看“训练日志”，方法与指标请看结果汇总。</p><button type="button" aria-expanded={showRaw} onClick={() => setShowRaw(v => !v)}>{showRaw ? '收起原始输出' : '显示原始输出片段'}</button></div>}
    {(!structured || showRaw) && <pre ref={viewport} className="console-output" role="log" aria-label="训练控制台输出" tabIndex={0} onScroll={e => { const p = e.currentTarget; if (follow && p.scrollHeight - p.scrollTop - p.clientHeight > 60) setFollow(false); }}>{text}</pre>}
    {!curveFirst && channel === 'training' && <TrainingCurve points={value?.curve ?? []} />}
    {channel === 'training' && <TrainingConvergence diagnosis={value?.convergence} discussion={value?.loss_discussion} running={row?.status === 'running'} />}
    {channel === 'training' && <TrainingLossRecovery runId={runId} value={value?.loss_review_recovery} refresh={() => setRefresh(v => v + 1)} />}
    <footer>来源：{channel === 'training' ? '保存的 training_log / training_state 与运行观察' : `${channel}.txt`} · {value?.observed_at ? `读取于 ${new Date(value.observed_at).toLocaleTimeString('zh-CN')}` : '尚未读取'}<span>刷新与滚动控制只影响显示</span></footer>
  </section>;
}
