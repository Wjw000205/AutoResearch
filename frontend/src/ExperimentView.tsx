import { ReferenceText } from './ReferenceNames';
import { useEffect, useRef, useState } from 'react';
import { ChevronDown, FlaskConical, ShieldCheck, Terminal } from 'lucide-react';
import { number, type Json, type TrialEvaluation } from './api';
import { PairedEvaluation } from './PairedEvaluation';
import { TrainingConsole } from './TrainingConsole';
import { ExperimentSummary } from './ExperimentSummary';
import { comparisonCompletion } from './ExperimentComparison';
import { ExperimentResultChart } from './ExperimentResultChart';
export { PairedEvaluation } from './PairedEvaluation';

const phaseNames: Record<string, string> = { test: '最终测试集', validation: '验证集', all: '所有实验', code_check: '接口检查' };
const variantNames: Record<string, string> = { baseline: '基线', proposed: '候选方法', ablation: '消融' };
const statusNames: Record<string, string> = { completed: '已完成', failed: '失败', interrupted: '已中断', cancelled: '已取消', running: '运行中' };
const displayDuration = (value: unknown) => number(value) < 60 ? `${number(value).toFixed(2)} s` : `${(number(value) / 60).toFixed(1)} min`;

export function partitionExperiments(experiments: Json[], queue?: Json, currentCycle?: number) {
  const partitions = new Map<string, {name: string; historical: boolean; trials: Json[]; status?: string}>();
  for (const item of queue?.datasets ?? []) partitions.set(item.dataset, {name: item.dataset, historical: false, trials: [], status: item.status});
  for (const trial of experiments) {
    const cycle = trial.research_cycle ?? 1;
    const bound = queue?.batches?.find((b: Json) => b.cycle === cycle)?.dataset;
    const name = trial.params?.dataset || bound;
    const historical = !name && currentCycle != null && cycle < currentCycle;
    const key = name || `unlabelled-cycle-${cycle}`;
    const group = partitions.get(key) ?? {name: historical ? `历史实验 · 第 ${cycle} 轮` : `未标注数据集 · 第 ${cycle} 轮`, historical, trials: []};
    group.trials.push(trial);
    partitions.set(key, group);
  }
  return [...partitions.entries()].map(([id, group]) => ({id, ...group}));
}

function datasetSummary(summary: Json | null | undefined, trials: Json[]) {
  if (!summary) return null;
  const ids = new Set(trials.filter(t => t.params?.phase !== 'code_check').map(t => t.id));
  const groups = (summary.groups ?? []).filter((g: Json) => g.trial_ids?.length && g.trial_ids.every((id: string) => ids.has(id)));
  const keys = new Set(groups.map((g: Json) => g.dataset_key));
  const groupIds = new Set(groups.map((g: Json) => g.id));
  const assessment = summary.performance_assessment;
  const comparisons = (summary.observed_comparisons ?? []).filter((c: Json) => groupIds.has(c.baseline_group) && groupIds.has(c.proposed_group))
    .map((c: Json) => ({...c, completed_timestamp: comparisonCompletion(c, trials)}));
  const reviews = (summary.reviews ?? []).filter((r: Json) => groupIds.has(r.baseline_group) && groupIds.has(r.proposed_group));
  const contract = comparisons.length && comparisons.every((c: Json) => c.metric === comparisons[0].metric && c.direction === comparisons[0].direction) ? comparisons[0] : null;
  return {...summary, groups, datasets: (summary.datasets ?? []).filter((d: Json) => keys.has(d.key)),
    observed_comparisons: comparisons,
    primary_metric: contract?.metric ?? summary.primary_metric, direction: contract?.direction ?? summary.direction,
    total: groups.reduce((n: number, g: Json) => n + g.total, 0), completed: groups.reduce((n: number, g: Json) => n + g.completed, 0),
    superseded_count: trials.filter(t => t.superseded).length,
    review: summary.review && groupIds.has(summary.review.baseline_group) && groupIds.has(summary.review.proposed_group) ? summary.review : reviews.length === 1 ? reviews[0] : null,
    performance_assessment: assessment?.trial_ids?.length && assessment.trial_ids.every((id: string) => ids.has(id)) ? assessment : null,
    benchmark_tasks: (summary.benchmark_tasks ?? []).filter((t: Json) => trials.some(row => row.params?.dataset === t.dataset))};
}

export function ExperimentView({ experiments, evaluation, runId, summary, datasetQueue, currentCycle }: {
  experiments: Json[]; evaluation?: TrialEvaluation; runId?: string; summary?: Json | null; datasetQueue?: Json; currentCycle?: number;
}) {
  const [dataset, setDataset] = useState('');
  const [kind, setKind] = useState('comparison');
  const partitions = partitionExperiments(experiments, datasetQueue, currentCycle);
  const current = partitions.find(p => p.id === dataset) ?? partitions.find(p => p.id === datasetQueue?.active_dataset)
    ?? partitions.find(p => !p.historical) ?? partitions[0];
  if (!current) return <ExperimentResults experiments={[]} />;
  const rows = current.trials;
  const scientific = rows.filter(t => !t.superseded && t.params?.phase !== 'code_check');
  const categories = [{id: 'comparison', name: '结果对比'}, {id: 'baseline', name: 'Baseline'}, {id: 'proposed', name: '候选方法'}, {id: 'ablation', name: '消融实验'},
    {id: 'code_check', name: '接口检查'}, {id: 'history', name: '旧版本 / 其他'}];
  const category = (trial: Json) => trial.superseded ? 'history' : trial.params?.phase === 'code_check' ? 'code_check'
    : ['baseline', 'proposed', 'ablation'].includes(trial.params?.variant) ? trial.params.variant : 'history';
  const categoryRows = (id: string) => rows.filter(t => id === 'comparison' ? ['validation', 'test'].includes(t.params?.phase) : category(t) === id);
  const chosen = categories.some(c => c.id === kind && categoryRows(c.id).length) ? kind
    : categories.find(c => categoryRows(c.id).length)?.id ?? 'baseline';
  const selected = categoryRows(chosen);
  return <section className="dataset-experiments" aria-label="按数据集查看实验">
    <div className="dataset-switcher" role="group" aria-label="选择实验数据集">{partitions.filter(p => !p.historical).map(p => <button type="button" key={p.id} aria-pressed={current.id === p.id} onClick={() => setDataset(p.id)}>
      <strong>{p.name}</strong><span>{p.trials.filter(t => t.status === 'running').length ? '正在运行' : p.trials.length ? `${p.trials.filter(t => !t.superseded && t.params?.phase !== 'code_check' && t.status === 'completed').length} 次训练完成` : '待配置 / 运行'}</span></button>)}</div>
    {partitions.some(p => p.historical) && <details className="result-source dataset-history"><summary>历史数据与实验</summary>{partitions.filter(p => p.historical).map(p => <button type="button" key={p.id} aria-pressed={current.id === p.id} onClick={() => setDataset(p.id)}>{p.name} · {p.trials.length} 条记录</button>)}</details>}
    <header className="dataset-section-heading"><div><h3>{current.name}</h3><p>{scientific.filter(t => t.status === 'completed').length} 次训练完成 · {rows.filter(t => t.params?.phase === 'code_check').length} 次接口检查{rows.some(t => t.superseded) ? ` · ${rows.filter(t => t.superseded).length} 条旧版本记录` : ''}</p></div><span className="result-tag">{datasetQueue?.attempts_unlimited ? '实验不限次数' : '逐批验证'}</span></header>
    {!rows.length ? <p className="dataset-empty">本数据集尚未开始运行。配置、接口检查和训练结果会分别显示在这里。</p> : <>
      <ExperimentSummary summary={datasetSummary(summary, rows)} />
      <div className="experiment-kind-tabs" role="group" aria-label="选择实验类型">{categories.map(c => <button type="button" key={c.id} aria-pressed={chosen === c.id} disabled={!categoryRows(c.id).length} onClick={() => setKind(c.id)}>{c.name}<span>{categoryRows(c.id).length}</span></button>)}</div>
      <ExperimentResults key={`${runId}:${current.id}:${chosen}`} experiments={selected} runId={runId} evaluation={partitions.length === 1 ? evaluation : undefined} />
      {runId && <DatasetConsole key={`${runId}:${current.id}`} runId={runId} trials={rows} />}
    </>}
  </section>;
}

function DatasetConsole({runId, trials}: {runId: string; trials: Json[]}) {
  const [open, setOpen] = useState(false);
  const newest = [...trials].reverse().sort((a, b) => String(b.started_at ?? b.created_at ?? b.completed_at ?? '').localeCompare(String(a.started_at ?? a.created_at ?? a.completed_at ?? '')));
  const latest = newest.find(t => t.status === 'running') ?? newest.find(t => !t.superseded && t.params?.phase !== 'code_check') ?? newest[0];
  return <details className="experiment-raw-results" onToggle={event => setOpen(event.currentTarget.open)}><summary>训练日志与运行详情<ChevronDown size={14} /></summary>{open && <TrainingConsole runId={runId} trialIds={trials.map(t => t.id)} initialTrialId={latest?.id} />}</details>;
}

export function ExperimentResults({ experiments, evaluation, runId }: { experiments: Json[]; evaluation?: TrialEvaluation; runId?: string }) {
  const [metric, setMetric] = useState('');
  const [selectedPhase, setSelectedPhase] = useState('auto');
  const [versions, setVersions] = useState('all');
  const [selectedId, setSelectedId] = useState<string>();
  const origin = useRef<HTMLElement | null>(null);
  const selectedHeading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { setSelectedId(undefined); }, [runId]);
  useEffect(() => {
    if (selectedId) { selectedHeading.current?.focus({preventScroll: true}); selectedHeading.current?.scrollIntoView?.({block: 'start'}); }
  }, [selectedId]);
  const select = (trial: Json, element: HTMLElement) => { origin.current = element; setSelectedId(trial.id); };
  const close = () => { setSelectedId(undefined); origin.current?.focus({preventScroll: true}); origin.current?.scrollIntoView?.({block: 'center', inline: 'nearest'}); };
  if (!experiments.length) return <div className="empty-state"><div className="empty-illustration"><FlaskConical size={30} strokeWidth={1.3} /></div><h3>实验记录，等待真实运行</h3><p>通过资源、预算和共识门禁后，系统执行基线与参数实验，并保留每一次结果。</p></div>;
  const checksOnly = experiments.every(item => item.params?.phase === 'code_check');
  const phase = selectedPhase === 'auto' ? experiments.some(item => item.params?.phase === 'test' && !item.superseded) ? 'test' : experiments.some(item => item.params?.phase === 'validation') ? 'validation' : 'all' : selectedPhase;
  const phaseRows = experiments.filter(item => phase === 'all' || item.params?.phase === phase);
  const filtered = phaseRows.filter(item => versions === 'all' || !item.superseded);
  const selected = filtered.find(item => item.id === selectedId);
  const metrics = [...new Set(filtered.flatMap(item => Object.keys(item.metrics ?? {})))].sort((a, b) => {
    const rank = (key: string) => key.includes('samples') ? 4 : key.startsWith(phase === 'test' ? 'test_' : 'val_') ? 0 : key.startsWith('train_') ? 2 : 1;
    return rank(a) - rank(b);
  });
  const chosen = metrics.includes(metric) ? metric : metrics[0];
  return <div className="experiments-view">{checksOnly ? <p className="experiment-phase-note">接口检查用于验证配置与代码能否运行，检查项单独列示。</p> : <><div className="experiment-phase-toolbar"><div className="tabs">{['test', 'validation', 'all'].map(value => <button key={value} className={phase === value ? 'selected' : ''} onClick={() => { setSelectedPhase(value); setMetric(''); setSelectedId(undefined); }}>{phaseNames[value]}<span>{experiments.filter(item => value === 'all' || item.params?.phase === value).length}</span></button>)}</div></div><div className="experiment-phase-note"><ShieldCheck size={15} /><span>{phase === 'test' ? '测试集不用于选择候选参数；记录中的 test 标记不等于划分来源已核验，详见下方评估。' : phase === 'validation' ? '验证集记录用于比较候选参数；不同参数组不会当作重复种子合并。' : '不同划分的指标保持各自名称。请避免将训练、验证和测试数值直接混为同一结果。'}</span></div></>}
    {!checksOnly && (phase === 'test' || phase === 'all') && <PairedEvaluation evaluation={evaluation} />}
    {!checksOnly && <div className="experiment-version-tabs" role="group" aria-label="选择实验版本">{[['all', '全部版本'], ['current', '当前版本']].map(([value, label]) => <button type="button" key={value} aria-pressed={versions === value} onClick={() => { setVersions(value); setSelectedId(undefined); }}>{label} · {value === 'all' ? phaseRows.length : phaseRows.filter(t => !t.superseded).length}</button>)}</div>}
    {!checksOnly && filtered.length > 0 && <><div className="chart-heading"><div><h3><ReferenceText value={phaseNames[phase]} /> · 指标对比</h3><p>每列对应一条实验记录。旧版本单独标记，仅供历史查阅；未完成或没有指标的记录也可点击查看。</p></div>{metrics.length > 0 && <select aria-label="选择对比指标" value={chosen} onChange={event => setMetric(event.target.value)}>{metrics.map(key => <option key={key}>{key}</option>)}</select>}</div>
      <ExperimentResultChart trials={filtered} experiments={experiments} metric={chosen ?? '指标'} phase={phaseNames[phase]} selectedId={selectedId} onSelect={select} />
    </>}
    {selected && <section className="experiment-trial-detail" aria-label="实验训练详情"><header><div>
      <h4 ref={selectedHeading} tabIndex={-1}>实验 {experiments.indexOf(selected) + 1} · {variantNames[selected.params?.variant] ?? '运行记录'}</h4>
      <p className="mono">{selected.id}</p><p>{phaseNames[selected.params?.phase] ?? selected.params?.phase} · seed {selected.params?.seed ?? '—'} · {statusNames[selected.status] ?? selected.status}{selected.superseded ? ' · 旧版本，已被替代' : ''}</p>
      </div><button type="button" className="btn small" onClick={close}>返回实验结果</button></header>
      {selected.error && <p className="warn-text"><ReferenceText value={selected.error} /></p>}
      {runId ? <TrainingConsole key={`${runId}:${selected.id}`} runId={runId} trialIds={[selected.id]} initialTrialId={selected.id} curveFirst />
        : <><h4>实验参数</h4><pre><ReferenceText value={selected.params} /></pre><p>打开所属研究任务后可读取训练日志与 loss 曲线。</p></>}
    </section>}

    {!filtered.length ? <p className="handoff-no-match">当前还没有<ReferenceText value={phaseNames[phase]} />记录。</p> : <><div className="table-scroll"><table><thead><tr><th>{checksOnly ? '检查 / 记录' : '实验 / 参数组'}</th><th>状态</th>{metrics.map(key => <th key={key}>{key}</th>)}<th>耗时</th></tr></thead><tbody>{filtered.map((experiment, index) => <tr key={experiment.id ?? index} className="experiment-trial-row" aria-selected={selectedId === experiment.id} data-trial-id={experiment.id} onClick={event => { const button = event.currentTarget.querySelector<HTMLButtonElement>('.experiment-trial-link'); if (button) select(experiment, button); }}><td><button type="button" className="experiment-trial-link" aria-label={`查看实验 ${experiments.indexOf(experiment) + 1} 的训练详情`} onClick={event => { event.stopPropagation(); select(experiment, event.currentTarget); }}><strong>{checksOnly ? '接口检查' : '实验'} {experiments.indexOf(experiment) + 1}{!checksOnly && <> · {variantNames[experiment.params?.variant] ?? experiment.params?.variant ?? '未命名方法'}</>}</strong></button><small>{phaseNames[experiment.params?.phase] ?? experiment.params?.phase ?? '未指定划分'}{!checksOnly && <> · seed {experiment.params?.seed ?? '—'}</>}</small><small className="mono">{experiment.id}</small>{experiment.superseded && <small className="warn-text">已被新代码版本替代</small>}</td><td><span className={`status status-${experiment.status}`}><i />{statusNames[experiment.status] ?? experiment.status}</span></td>{metrics.map(key => <td className="mono" key={key}>{typeof experiment.metrics?.[key] === 'number' ? number(experiment.metrics[key]).toPrecision(5) : '—'}</td>)}<td className="mono">{displayDuration(experiment.duration_seconds)}</td></tr>)}</tbody></table></div><div className="experiment-logs">{filtered.map((experiment, index) => <details key={experiment.id ?? index}><summary><Terminal size={14} /><span>{checksOnly ? '检查' : '实验'} {experiments.indexOf(experiment) + 1} · 参数与记录{experiment.superseded ? ' · 已被替代' : ''}</span><ChevronDown size={14} /></summary><h4>实验参数</h4>{experiment.transport_payload_omitted && <p className="muted">页面省略大型比较数组，完整证据保留在原记录中。</p>}{experiment.params_complete === false && <p className="muted">参数超出页面读取上限，完整参数保留在原记录中。</p>}<pre><ReferenceText value={experiment.params} /></pre><h4>标准输出</h4>{experiment.details_loaded === false ? <p className="muted">在训练日志与运行详情中选择此记录，查看标准输出和错误输出。</p> : <pre>{experiment.stdout || '无标准输出'}</pre>}{experiment.stderr && <><h4>错误输出</h4><pre><ReferenceText value={experiment.stderr} /></pre></>}{experiment.artifact_path && <code><ReferenceText value={experiment.artifact_path} /></code>}</details>)}</div></>}
  </div>;
}
