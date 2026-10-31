import { useEffect, useId, useRef, useState } from 'react';
import type { Json } from './api';
import { ExperimentResults } from './ExperimentView';
import { TrainingConsole } from './TrainingConsole';
import './parameter-grid.css';

const statuses: Record<string, string> = {
  reused: '复用原对照', completed: '已完成', awaiting_review: '待逐批评审',
  approved_pending: '已评审 · 待执行', partial: '部分完成', running: '运行中',
  blocked: '需处理', unavailable: '证据待核对',
};
const number = (v: unknown) => typeof v === 'number' && Number.isFinite(v) ? v.toPrecision(6) : '未测量';
const parameterValue = (v: unknown) => Array.isArray(v) ? JSON.stringify(v) : String(v);
const parameterKey = (v: unknown) => JSON.stringify(v);
const improvementLabel = (grid: Json) => grid.selection_score === 'baseline_score' ? '相对同种子原基线改善' : '相对同种子原候选改善';
const percent = (v: unknown) => typeof v === 'number' && Number.isFinite(v) ? `${(v * 100).toFixed(3)}%` : '未报告';
const variants: Record<string, string> = { baseline: '基线', proposed: '候选方法', ablation: '消融' };
const sources: Record<string, string> = { real_experiment_execution: '真实训练', fixture: '测试样例', offline_fixture: '离线测试样例', simulation: '模拟记录', demo: '演示记录' };

function PointResults({ grid, point, experiments, runId, id, returnToGrid }: {
  grid: Json; point: Json; experiments: Json[]; runId?: string; id: string; returnToGrid: () => void;
}) {
  const [logTrial, setLogTrial] = useState<string | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const logHeading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    heading.current?.focus({preventScroll: true});
    heading.current?.scrollIntoView?.({block: 'start'});
  }, []);
  useEffect(() => {
    if (logTrial) {
      logHeading.current?.focus({preventScroll: true});
      logHeading.current?.scrollIntoView?.({block: 'start'});
    }
  }, [logTrial]);
  // The saved grid projection supplies exact receipt IDs. Do not infer a match
  // from a parameter value shared by other datasets, seeds or revisions.
  const jobs: string[] = point.jobs ?? [];
  const trials = experiments.filter(trial => jobs.includes(trial.id));
  const missing = jobs.filter(job => !trials.some(trial => trial.id === job));
  const selectedLog = trials.find(trial => trial.id === logTrial);
  return <section id={id} className="parameter-point-results" aria-label={`${grid.factor_path} = ${parameterValue(point.value)} 的实验结果`}>
    <header><h4 ref={heading} tabIndex={-1}>{grid.factor_path} = {parameterValue(point.value)}{point.control ? '（原值）' : ''} · 实验结果</h4><span>{statuses[point.status] ?? '待核对'}</span></header>
    <button type="button" className="btn small" onClick={returnToGrid}>返回参数网格</button>
    <p>固定种子 {grid.validation_seed} · 验证指标 {grid.metric} · {grid.direction === 'maximize' ? '越高越好' : '越低越好'}</p>
    <p>基线 {number(point.baseline_score)} · 候选 {number(point.proposed_score)} · {improvementLabel(grid)} {percent(point.improvement_vs_control)}</p>
    {['blocked', 'unavailable'].includes(point.status) && <p className="warn-text" role="status">此参数点存在失败或证据待核对，以下原始记录不代表有效的成对比较。</p>}
    {!jobs.length && <p role="status">此参数点尚未派发实验，暂无实验结果。需完成逐批评审后执行。</p>}
    {missing.length > 0 && <p role="status">以下任务尚无可读取的实验记录，可能尚未执行或未包含在当前快照中：{missing.join('、')}。已有记录仍可查看。</p>}
    {trials.length > 0 && <>
      <ul className="parameter-point-sources">{trials.map(trial => <li key={trial.id}>
        <strong>{variants[trial.params?.variant] ?? '实验'} · {trial.params?.algorithm ?? trial.params?.method ?? '方法未报告'}</strong>
        <span>{trial.params?.dataset ?? '数据集未报告'} · seed {trial.params?.seed ?? '未报告'} · {sources[trial.provenance] ?? trial.provenance ?? '来源未报告'}</span>
        <code>{trial.id}</code>
        {trial.error && <p className="warn-text">{trial.error}</p>}
        {runId && <button type="button" className="btn small" aria-pressed={selectedLog?.id === trial.id}
          aria-controls={`${id}-logs`} onClick={() => setLogTrial(trial.id)}>查看{variants[trial.params?.variant] ?? '实验'}日志与曲线</button>}
      </li>)}</ul>
      {runId && selectedLog && <div id={`${id}-logs`}>
        <h5 ref={logHeading} tabIndex={-1}>所选实验日志与曲线 · {selectedLog.id}</h5>
        <button type="button" className="btn small" onClick={() => setLogTrial(null)}>收起训练日志</button>
        <TrainingConsole key={selectedLog.id} runId={runId} trialIds={[selectedLog.id]} initialTrialId={selectedLog.id} />
      </div>}
      <ExperimentResults experiments={trials} runId={runId} />
    </>}
  </section>;
}

function Grid({ grid, experiments, runId }: { grid: Json; experiments: Json[]; runId?: string }) {
  const [selected, setSelected] = useState<string | null>(null);
  const selectedButton = useRef<HTMLButtonElement>(null);
  const resultId = useId();
  const point = grid.points.find((p: Json) => parameterKey(p.value) === selected);
  return <article>
    <h4>{grid.factor_path} · {grid.variant === 'baseline' ? '仅基线' : grid.variant === 'proposed' ? '仅候选方法' : '共享参数'} · 固定种子 {grid.validation_seed}</h4>
    <p>候选值：{grid.grid_values.map(parameterValue).join('、')}{grid.lower_bound != null && grid.upper_bound != null ? ` · 范围 [${grid.lower_bound}, ${grid.upper_bound}]` : ' · 按已评审的离散取值'}</p>
    <p>{grid.grid_rationale}</p>
    <p>新参数点已完成 {grid.completed_new_points} / {grid.grid_values.length - 1}；待评审点最多新增 {grid.new_trials_needed} 次训练。每点完成后重新判断。</p>
    <p className="muted">点击参数值，查看该点对应的基线、候选实验及训练日志。</p>
    <div className="parameter-grid-table" tabIndex={0} role="region" aria-label={`${grid.factor_path} 网格结果`}>
      <table><caption>{grid.metric} · {grid.direction === 'maximize' ? '越高越好' : '越低越好'}</caption>
        <thead><tr><th>参数值</th><th>状态</th><th>基线</th><th>候选</th><th>{improvementLabel(grid)}</th></tr></thead>
        <tbody>{grid.points.map((p: Json) => <tr key={parameterKey(p.value)} className={point === p ? 'parameter-point-selected' : undefined}>
          <th scope="row"><button ref={point === p ? selectedButton : undefined} type="button" className="parameter-value" aria-pressed={point === p}
            aria-controls={resultId} aria-label={`查看 ${grid.factor_path} = ${parameterValue(p.value)} 的实验结果`}
            onClick={() => setSelected(parameterKey(p.value))}>{parameterValue(p.value)}{p.control ? '（原值）' : ''}</button></th>
          <td>{statuses[p.status] ?? '待核对'}</td>
          <td>{number(p.baseline_score)}</td><td>{number(p.proposed_score)}</td>
          <td>{percent(p.improvement_vs_control)}</td>
        </tr>)}</tbody>
      </table>
    </div>
    <p>当前实测最优值：{grid.best_measured_value == null ? '尚无可比较结果' : parameterValue(grid.best_measured_value)}。平分优先保留原值；未执行点不参与排名。</p>
    {point && <PointResults key={parameterKey(point.value)} id={resultId} grid={grid} point={point} experiments={experiments} runId={runId}
      returnToGrid={() => { selectedButton.current?.focus({preventScroll: true}); selectedButton.current?.scrollIntoView?.({block: 'center'}); }} />}
    <details><summary>评审绑定与实验记录</summary>
      <p>对照批次：<code>{grid.base_unit_id}</code></p>
      <p>代码版本：<code>{grid.binding?.code_sha256}</code></p>
      {grid.points.map((p: Json) => <p key={parameterKey(p.value)}>{parameterValue(p.value)}：{p.jobs.join('、') || '尚未派发'}</p>)}
    </details>
  </article>;
}

export function ParameterGrid({ value, experiments = [], runId }: { value?: Json | null; experiments?: Json[]; runId?: string }) {
  const grids: Json[] = value?.grids ?? [];
  return <section className="parameter-grid panel" aria-label="参数网格搜索">
    <h3>参数网格搜索</h3>
    <p>架构评审通过 → 声明有界网格 → 三方逐批评审 → 成对验证 → 同种子比较 → 稳定性复核</p>
    <p className="muted">由智能体制定当前数据集的参数范围和搜索顺序。每批改变一个已评审因素；共享参数同步调整，方法专有参数分别调整，另一侧对照与已完成配置直接复用。新计划固定一个种子。</p>
    {value?.status === 'unavailable' && <p role="status">{value.reason ?? '网格证据待核对，暂不展示选参结论。'}</p>}
    {!value && <p role="status">正在读取已保存的参数网格状态。</p>}
    {value?.status === 'not_planned' && !grids.length && <p>尚未形成受评审的新参数网格。已有训练、消融和种子配对不代表已完成参数搜索。</p>}
    {grids.map(grid => <Grid key={`${runId}:${grid.id}`} grid={grid} experiments={experiments} runId={runId} />)}
    <p className="muted">全部网格点完成也不自动判定稳定。需核验训练诊断和预先批准的退化容限，通过三方评审后才能切换数据集；留出测试集不参与选参。</p>
  </section>;
}
