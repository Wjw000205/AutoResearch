import { useRef } from 'react';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import { type Json } from './api';
import './experiment-results.css';

const variants: Record<string, string> = {baseline: '基线', proposed: '候选方法', ablation: '消融'};
const statuses: Record<string, string> = {failed: '失败', running: '运行中', interrupted: '已中断', cancelled: '已取消', timeout: '超时', completed: '无此指标'};
export const resultMetric = (trial: Json, metric: string) => trial.status === 'completed'
  && typeof trial.metrics?.[metric] === 'number' && Number.isFinite(trial.metrics[metric]) ? trial.metrics[metric] as number : null;

export function ExperimentResultChart({trials, experiments, metric, phase, selectedId, onSelect}: {
  trials: Json[]; experiments: Json[]; metric: string; phase: string; selectedId?: string;
  onSelect: (trial: Json, origin: HTMLElement) => void;
}) {
  const viewport = useRef<HTMLDivElement>(null);
  const values = trials.map(t => resultMetric(t, metric)).filter((v): v is number => v != null);
  const min = Math.min(0, ...values), max = Math.max(...values, min + .001), range = max - min;
  const scroll = (direction: number) => viewport.current?.scrollBy({left: direction * viewport.current.clientWidth * .8, behavior: 'smooth'});
  return <div className="experiment-result-chart">
    <div className="result-chart-toolbar"><p>{trials.length} 条实验记录 · 左右滑动浏览，点击查看训练详情</p><div>
      <button type="button" className="btn small" aria-label="向左浏览实验结果" onClick={() => scroll(-1)}><ChevronLeft size={17} /></button>
      <button type="button" className="btn small" aria-label="向右浏览实验结果" onClick={() => scroll(1)}><ChevronRight size={17} /></button>
    </div></div>
    <div className="result-chart-layout" role="group" aria-label={`${phase} ${metric} 实验对比图`}>
      <div className="result-chart-axis" aria-hidden="true">{[0, 1, 2, 3].map(tick => <span key={tick} style={{top: `${tick / 3 * 100}%`}}>{(max - range * tick / 3).toPrecision(3)}</span>)}</div>
      <div ref={viewport} className="result-chart-scroll" tabIndex={0} role="region" aria-label="横向滚动实验结果">
        <div className="result-chart-columns" style={{minWidth: `${trials.length * 108}px`}}>
          <div className="result-chart-grid" aria-hidden="true">{[0, 1, 2, 3].map(tick => <i key={tick} />)}</div>
          {trials.map(trial => {
            const value = resultMetric(trial, metric), index = experiments.indexOf(trial) + 1;
            const height = value == null ? 0 : Math.max(2, (value - min) / range * 180);
            const variant = variants[trial.params?.variant] ?? trial.params?.variant ?? '实验';
            const state = value == null ? statuses[trial.status] ?? '无指标' : value.toPrecision(5);
            const label = `查看实验 ${index} · ${variant} · seed ${trial.params?.seed ?? '未报告'}${trial.superseded ? ' · 旧版本' : ''} · ${metric} ${state} 的训练详情`;
            return <button key={trial.id} type="button" className={`result-chart-column ${trial.superseded ? 'result-chart-historical' : ''}`}
              data-trial-id={trial.id} aria-label={label} aria-pressed={selectedId === trial.id} title={`${label}\n${trial.id}`}
              onClick={event => onSelect(trial, event.currentTarget)}>
              <span className="result-chart-plot">
                <span className="result-chart-value" style={{bottom: `${height + 8}px`}}>{state}</span>
                {value != null ? <span className={`result-chart-bar ${trial.params?.variant ?? ''}`} style={{height: `${height}px`}} />
                  : <span className="result-chart-missing">—</span>}
              </span>
              <strong>实验 {index}</strong><span>{variant} · seed {trial.params?.seed ?? '—'}</span>
              <small>{trial.superseded ? '旧版本' : '当前版本'}{trial.code_revision != null ? ` · v${trial.code_revision}` : ''}</small>
            </button>;
          })}
        </div>
      </div>
    </div>
    <div className="chart-legend"><span><i className="baseline" />基线</span><span><i className="proposed" />候选方法</span><span><i className="ablation" />消融</span><span>虚线边框：旧版本</span><span>—：无可绘制指标</span></div>
  </div>;
}
