import { useEffect, useId, useRef, useState } from 'react';
import type { Json } from './api';
import { ExperimentResults } from './ExperimentView';
import { TrainingConsole } from './TrainingConsole';
import { Star } from 'lucide-react';
import { matrixGroups, matrixRows, measured, parameterKey, parameterName, parameterValue, scoreKey, type MatrixRow } from './ParameterMatrix';
import './parameter-grid.css';

const statuses: Record<string, string> = {
  reused: '复用原对照', completed: '已完成', awaiting_review: '待逐批评审',
  approved_pending: '已评审 · 待执行', partial: '部分完成', running: '运行中',
  blocked: '需处理', unavailable: '证据待核对',
};
const number = (v: unknown) => typeof v === 'number' && Number.isFinite(v) ? v.toPrecision(6) : '未测量';
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

function Matrix({ rows, experiments, runId }: { rows: MatrixRow[]; experiments: Json[]; runId?: string }) {
  const [selected, setSelected] = useState<{row: string; value: string | undefined} | null>(null);
  const [method, setMethod] = useState('all');
  const selectedButton = useRef<HTMLButtonElement>(null);
  const resultId = useId();
  const visible = rows.filter(r => method === 'all' || (r.grid.variant ?? 'shared') === method);
  const groups = matrixGroups(visible);
  const columns = Math.max(1, ...visible.map(r => r.candidates.length));
  const active = visible.find(r => r.id === selected?.row);
  const point: Json | undefined = active?.grid.points.find((p: Json) => parameterKey(p.value) === selected?.value);
  const metric = (grid: Json) => grid.metric === 'val_mse' ? 'MSE' : grid.metric ?? '验证值';
  function cell(row: MatrixRow, p?: Json) {
    if (!p) return <span className="matrix-empty" aria-label="没有此候选">—</span>;
    const grid = row.grid;
    const valid = measured(p, grid);
    const delta = valid && typeof p.improvement_vs_control === 'number' && Number.isFinite(p.improvement_vs_control)
      ? p.improvement_vs_control : null;
    const tone = !valid || p.control || delta === null || delta === 0 ? 'neutral' : delta > 0 ? 'better' : 'worse';
    const chosen = active?.id === row.id && point === p;
    const best = row.best === p;
    return <button type="button" className={`parameter-value matrix-cell ${tone}${best ? ' matrix-winner' : ''}`} ref={chosen ? selectedButton : undefined}
      aria-pressed={chosen} aria-controls={resultId}
      aria-label={`查看 ${grid.factor_path} = ${parameterValue(p.value)} 的实验结果`}
      title={`${variants[grid.variant] ?? '共享参数'} · ${grid.factor_path} · seed ${grid.validation_seed} · ${statuses[p.status] ?? '待核对'}`}
      onClick={() => setSelected({row: row.id, value: parameterKey(p.value)})}>
      {best && <span className="matrix-winner-tag"><Star size={10} fill="currentColor" aria-hidden="true" /> 实测最佳</span>}
      <span className="matrix-setting">
        {!p.control && row.control && <><span className="matrix-before">{parameterValue(row.control.value)}</span><span className="matrix-arrow">→</span></>}
        <strong>{parameterValue(p.value)}</strong>
      </span>
      <span className="matrix-score">{metric(grid)} <b>{number(p[scoreKey(grid)])}</b></span>
      {(grid.variant ?? 'shared') === 'shared' && <span className="matrix-secondary">基线 {number(p.baseline_score)}</span>}
      <span className="matrix-change">{p.control ? '原值对照' : delta === null ? statuses[p.status] ?? '待核对'
        : delta > 0 ? `改善 ${percent(delta)}` : delta < 0 ? `退化 ${percent(-delta)}` : '与原值持平'}</span>
      {!valid && <span className="matrix-secondary">{p.status === 'awaiting_review' ? '尚未测量' : '待完整比较'}</span>}
    </button>;
  }
  return <>
    <div className="matrix-toolbar"><div role="group" aria-label="筛选调参方法">
      {[['all', '全部'], ['baseline', '基线'], ['proposed', '候选方法'], ...(rows.some(r => !r.grid.variant || r.grid.variant === 'shared') ? [['shared', '共享']] : [])].map(([key, label]) =>
        <button type="button" key={key} aria-pressed={method === key} onClick={() => setMethod(key)}>{label}</button>)}
    </div><span>每格显示参数变化、验证分数和相对原值的改善</span></div>
    <p className="matrix-mobile-hint">横向滑动比较候选，右侧固定显示最佳点位。</p>
    <div className="parameter-grid-table parameter-matrix" tabIndex={0} role="region" aria-label="参数对比矩阵">
      <table><caption>同一参数下分别调整基线与 Proposed，每行与自身原值比较。点击单元格查看实验和日志。</caption>
        <thead><tr><th scope="col">参数</th><th scope="col">调整方法</th><th scope="col">原值对照</th>
          {Array.from({length: columns}, (_, i) => <th scope="col" key={i}>候选 {i + 1}</th>)}<th scope="col" className="matrix-best-head">★ 最佳点位</th></tr></thead>
        {groups.map(group => <tbody className="matrix-parameter-group" key={group.id}>{group.rows.map((row, index) => <tr key={row.id}>
          {index === 0 && <th className="matrix-parameter-name" scope="rowgroup" rowSpan={group.rows.length}>
            <strong>{parameterName(row.grid.factor_path)}</strong><code>{row.grid.factor_path}</code>
            <small>seed {row.grid.validation_seed} · {row.grid.binding?.revision != null ? `v${row.grid.binding.revision} · ` : ''}{metric(row.grid)} {row.grid.direction === 'maximize' ? '↑' : '↓'}</small>
          </th>}
          <th scope="row" className="matrix-method-name">
            <span className={`matrix-method ${row.grid.variant ?? 'shared'}`}>{row.grid.variant === 'baseline' ? '基线' : row.grid.variant === 'proposed' ? 'Proposed' : '共享参数'}</span>
            <small>{row.grid.variant === 'baseline' ? '仅调整基线' : row.grid.variant === 'proposed' ? '仅调整候选方法' : '两种方法同步'}</small>
          </th><td className="matrix-control">{cell(row, row.control)}</td>
          {Array.from({length: columns}, (_, i) => <td key={i}>{cell(row, row.candidates[i])}</td>)}
          <td className="matrix-best">{row.best ? <button type="button" aria-controls={resultId}
            aria-label={`查看${variants[row.grid.variant] ?? '共享参数'} ${row.grid.factor_path} 最佳点 ${parameterValue(row.best.value)}`}
            onClick={() => setSelected({row: row.id, value: parameterKey(row.best!.value)})}>
            <strong>{parameterValue(row.best.value)}</strong>
            <span>{metric(row.grid)} {number(row.best[scoreKey(row.grid)])}</span>
            <small>{row.best.control ? '保持原值' : '优于原值'} ↗</small>
          </button> : <span>待测量</span>}</td>
        </tr>)}</tbody>)}
      </table>
    </div>
    {!visible.length && <p>该方法尚无已评审网格。</p>}
    <p className="matrix-legend"><span className="winner">★ 实测最佳</span><span className="better">改善</span><span className="worse">退化</span><span>每行独立比较，平分保留原值。</span></p>
    {active && point && <PointResults key={active.id + parameterKey(point.value)} id={resultId} grid={active.grid} point={point} experiments={experiments} runId={runId}
      returnToGrid={() => { selectedButton.current?.focus({preventScroll: true}); selectedButton.current?.scrollIntoView?.({block: 'center'}); }} />}
    <details className="matrix-history"><summary>评审历史与完整绑定 · {rows.reduce((n, r) => n + r.sources.length, 0)} 条记录</summary>
      {rows.map(row => <section key={row.id}><h4>{parameterName(row.grid.factor_path)} · {variants[row.grid.variant] ?? '共享参数'} · seed {row.grid.validation_seed}</h4>
        <p>{improvementLabel(row.grid)}。对照批次 <code>{row.grid.base_unit_id}</code></p>
        {row.sources.map(grid => <details key={grid.id}><summary>{grid.id}</summary>
          <p>当时的候选顺序 {grid.grid_values.map(parameterValue).join('、')}</p><p>{grid.grid_rationale}</p>
          <p>代码 SHA <code>{grid.binding?.code_sha256}</code> · 范围 SHA <code>{grid.scope_sha256}</code></p>
          {grid.points.map((p: Json) => <p key={parameterKey(p.value)}>{parameterValue(p.value)} · {(p.jobs ?? []).join('、') || '尚未派发'}</p>)}
        </details>)}</section>)}
    </details>
  </>;
}

export function ParameterGrid({ value, experiments = [], runId }: { value?: Json | null; experiments?: Json[]; runId?: string }) {
  const grids: Json[] = value?.grids ?? [];
  const rows = matrixRows(grids);
  const candidates = rows.flatMap(row => row.candidates.map(p => ({p, grid: row.grid})));
  const done = candidates.filter(({p, grid}) => measured(p, grid)).length;
  return <section className="parameter-grid panel" aria-label="参数网格搜索">
    <header className="matrix-heading"><div><h3>参数对比矩阵</h3><p>一次调整一个参数，直接比较修改前后的验证结果。</p></div>
      {rows.length > 0 && <div className="matrix-counts"><span><b>{matrixGroups(rows).length}</b> 参数</span><span><b>{rows.length}</b> 方法对比</span><span><b>{done}/{candidates.length}</b> 本表候选已测</span></div>}
    </header>
    {value?.status === 'unavailable' && <p role="status">{value.reason ?? '网格证据待核对，暂不展示选参结论。'}</p>}
    {!value && <p role="status">正在读取已保存的参数网格状态。</p>}
    {value?.status === 'not_planned' && !grids.length && <p>尚未形成受评审的新参数网格。已有训练、消融和种子配对不代表已完成参数搜索。</p>}
    {rows.length > 0 && <><p className="matrix-scope-note">本表仅统计已形成的网格。相同控制、版本、种子与实验结果的评审记录已归并，历史可在下方展开。</p>
      <Matrix key={runId} rows={rows} experiments={experiments} runId={runId} /></>}
    <p className="muted matrix-footnote">新计划默认固定一个种子，留出测试集不参与选参。全部网格点完成也不自动判定稳定。</p>
  </section>;
}
