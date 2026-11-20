import { useState, type ReactNode } from 'react';
import { ChevronDown, Star } from 'lucide-react';
import type { Detail, Json } from './api';
import { ParameterGrid } from './ParameterGrid';
import { parameterName, parameterValue } from './ParameterMatrix';
import { ExperimentView } from './ExperimentView';
import './parameter-grid.css';

const methods = { baseline: '基线', proposed: '候选方法' };
const statusLabels: Record<string, string> = {configuring: '配置中', pending: '待开始', finished: '已结束',
  running: '运行中', ready: '已配置', deferred_by_operator: '已收束 · 稳定性未验收'};
const score = (n: number) => Number(n.toPrecision(7)).toString();
const labelMetric = (s: string) => s === 'val_mse' ? '验证 MSE' : s === 'val_mae' ? '验证 MAE' : s;
function atPath(params: Json, path: string) {
  return path.split('/').slice(1).reduce((v, k) => v?.[k.replace(/~1/g, '/').replace(/~0/g, '~')], params);
}

function Parameters({point, paths, trials}: {point: Json; paths: string[]; trials: Map<string, Json>}) {
  const params = point.params ?? trials.get(point.trial_id)?.params;
  const listed = params ? paths.filter(p => atPath(params, p) !== undefined) : [];
  return <details className="tuning-settings"><summary>查看参数与回执 <ChevronDown size={13} /></summary>
    {listed.length > 0 && <dl>{listed.map(p => <div key={p}><dt title={p}>{parameterName(p)} <code>{p}</code></dt>
      <dd>{parameterValue(atPath(params, p))}</dd></div>)}</dl>}
    <div className="tuning-receipt">验证实验 <code>{point.trial_id}</code></div>
    {params && <details><summary>完整实验参数</summary><pre>{JSON.stringify(params, null, 2)}</pre></details>}
  </details>;
}

function Comparison({group, trials, final = false}: {group: Json; trials: Map<string, Json>; final?: boolean}) {
  const points: Json[] = final ? group.winners : group.points;
  const primary = labelMetric(group.metric);
  return <section className="tuning-comparison">
    <header><h4>{final ? '贝叶斯搜索选定的参数点' : '当前实测最佳参数点'}</h4>
      <span>{primary} · {group.direction === 'maximize' ? '越高越好' : '越低越好'} · seed {group.seed} · 第 {group.cycle} 轮 · 代码 v{group.revision}
        {group.task && ' · '+group.task}{group.provenance && group.provenance !== 'real_experiment_execution' && ' · '+group.provenance}</span></header>
    <div className="tuning-winners">{(['baseline','proposed'] as const).map(method => {
      const winners = points.filter(p => p.variant === method && (final || p.best));
      if (!winners.length) return null;
      const params = winners[0].params ?? trials.get(winners[0].trial_id)?.params;
      const preview = (group.paths[method] as string[]).filter(p => params && atPath(params, p) !== undefined).slice(0, 3);
      return <article key={method}><div className="tuning-winner-label"><Star size={14} fill="currentColor" aria-hidden />{methods[method]}
        <span>{winners.length > 1 ? winners.length+' 个并列最佳' : '最佳'}</span></div>
        <div className="tuning-winner-score"><strong>{score(winners[0].score)}</strong><small>{primary}</small></div>
        {preview.length > 0 && <p className="tuning-param-preview">{preview.map(p =>
          <span key={p}>{parameterName(p)} <b>{parameterValue(atPath(params, p))}</b></span>)}</p>}
        {winners.map(p => <Parameters key={p.trial_id} point={p} paths={group.paths[method]} trials={trials} />)}
      </article>;
    })}</div>
    {!final && <details className="tuning-ranking"><summary>查看 {points.length} 个已测配置的排名</summary>
      <div className="tuning-ranking-scroll"><table><thead><tr><th>方法</th><th>{primary}</th><th>参数配置与实验记录</th></tr></thead><tbody>
        {points.map(p => <tr key={p.trial_id} className={p.best ? 'is-best' : ''}><td>{methods[p.variant as keyof typeof methods]}</td>
          <td>{score(p.score)} {p.best && <span>★ 最佳</span>}</td>
          <td><Parameters point={p} paths={group.paths[p.variant]} trials={trials}/></td></tr>)}
      </tbody></table></div></details>}
  </section>;
}

export function DatasetTuningDetail({data, currentConfig, stageResult}: {data: Detail; currentConfig: ReactNode; stageResult: ReactNode}) {
  const view = data.tuning_datasets!;
  const queue = data.dataset_configuration?.queue;
  const [selected, setSelected] = useState('');
  const current = view.datasets.find(d => d.name === selected) ?? view.datasets.find(d => d.name === view.active_dataset) ?? view.datasets[0];
  const name = current.name;
  const trials = (data.experiments ?? []).filter(t => (t.params?.dataset || queue?.batches?.find((b: Json) => b.cycle === (t.research_cycle ?? 1))?.dataset
    || `未标注数据集 · 第 ${t.research_cycle ?? 1} 轮`) === name);
  const byId = new Map(trials.map(t => [t.id as string, t]));
  const scopedQueue = {active_dataset: name, batches: (queue?.batches ?? []).filter((b: Json) => b.dataset === name),
    datasets: [{dataset: name, status: current.status}], attempts_unlimited: queue?.attempts_unlimited};
  return <div className="tuning-dataset-detail">
    <div className="tuning-dataset-tabs" role="group" aria-label="选择调参数据集">{view.datasets.map(d =>
      <button type="button" key={d.name} aria-pressed={name === d.name} onClick={() => setSelected(d.name)}>
        <strong>{d.name}</strong><small>{d.name === view.active_dataset ? '当前数据集' : statusLabels[d.status] ?? '历史记录'}</small>
      </button>)}</div>
    <div key={name} className="tuning-dataset-content" role="region" aria-label={name+' 参数微调'}>
      <header className="tuning-dataset-heading"><h3>{name}</h3>
        <span>{current.batch_count} 个调参批次{current.status ? ' · '+(statusLabels[current.status] ?? current.status) : ''}</span></header>
      {current.incomplete && <p role="status">部分调参记录不完整，暂不显示跨点排名。</p>}
      {(current.selections ?? []).map((g: Json) => <Comparison key={g.id} group={g} trials={byId} final />)}
      {(current.comparisons ?? []).map((g: Json) => <Comparison key={g.id} group={g} trials={byId} />)}
      {!current.incomplete && !current.comparisons?.length && !current.selections?.length &&
        <div className="tuning-no-measurement"><strong>暂无可比较的验证点</strong>
          <p>{name} 完成同一种子下的成对调参验证后，这里会显示基线与候选方法的最佳配置。</p></div>}
      {!!current.grids?.length && <ParameterGrid value={{grids:current.grids,status:current.incomplete ? 'unavailable' : 'saved'}}
        experiments={trials} runId={data.run.id} />}
      {name === view.active_dataset && currentConfig}
      {trials.length > 0 && <details className="tuning-experiment-details"><summary>{name} 的实验记录与训练日志 · {trials.length} 条</summary>
        <ExperimentView runId={data.run.id} experiments={trials} summary={data.experiment_summary} datasetQueue={scopedQueue}
          currentCycle={data.run.state?.research_cycle} singleDataset /></details>}
      {name === view.active_dataset && stageResult}
    </div>
  </div>;
}
