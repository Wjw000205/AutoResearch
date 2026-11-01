import { ChevronDown, Database, FlaskConical } from 'lucide-react';
import { type Json } from './api';
import { latestComparison } from './ExperimentComparison';
import './experiment-summary.css';

const finite = (n: unknown): n is number => typeof n === 'number' && Number.isFinite(n);
const value = (n: unknown) => finite(n) ? n !== 0 && Math.abs(n) < 1e-6 ? n.toExponential(3) : n.toFixed(6).replace(/0+$/, '').replace(/\.$/, '') : '—';
const percent = (n: unknown) => finite(n) ? `${n > 0 ? '+' : ''}${(n * 100).toFixed(2)}%` : '未报告';
const phases: Record<string, string> = {validation: '验证集', test: '独立测试集'};
const variants: Record<string, string> = {baseline: '主基线', proposed: '候选方法', ablation: '对照 / 消融'};
const label = (name: string) => name;
const loss = (name: unknown) => typeof name === 'string' ? name : '训练损失未记录';
const source = (name: unknown) => typeof name === 'string' ? ({real_experiment_execution: '真实训练', fixture: '测试样例', offline_fixture: '离线测试样例', simulation: '模拟记录', demo: '演示记录'} as Record<string, string>)[name] ?? name : '来源未记录';

function Dataset({ data }: {data: Json}) {
  const counts = data.configured_counts ?? {};
  const testMissing = data.split_materialization?.test === 'not_materialized';
  const title = /:[a-f0-9]{32,}/i.test(data.name ?? '') ? data.name.split(':')[0] : data.name;
  const dimensions = Object.entries(data.task_dimensions ?? {}).filter(([, dimension]) =>
    typeof dimension === 'number' || typeof dimension === 'boolean' || typeof dimension === 'string' && dimension.length < 80);
  return <div className="result-dataset"><Database size={18} /><div>
    <strong>{title}</strong><span className="result-tag">{phases[data.phase] ?? '划分未记录'}</span>
    <p>数据配置：{finite(counts.train) && <>训练 {counts.train.toLocaleString('zh-CN')} · </>}{finite(counts.validation) && <>验证 {counts.validation.toLocaleString('zh-CN')} · </>}
      {dimensions.map(([name, dimension]) => <span key={name}>{name}: {String(dimension)} · </span>)}</p>
    <small>{testMissing ? '独立测试数据尚未生成；当前结果来自验证集。' : '任务条件来自已保存的数据配置。'}</small>
    <details className="result-source"><summary>数据来源与划分标识<ChevronDown size={12} /></summary><code>{data.id ?? '未记录数据集标识'}</code>
      {data.task_id && <p>基准任务：{data.task_id}</p>}
      {data.paper_protocol && <><p>论文参数来源：{data.paper_protocol.protocol_id}</p><code>SHA256 {data.paper_protocol.protocol_sha256}</code></>}
      <p>数据种子（训练 / 验证 / 测试配置）：{(data.data_seeds ?? []).join(' / ') || '未记录'}</p>
      <p>完整任务条件与数据处理配置</p><pre>{JSON.stringify(data.task_dimensions ?? {}, null, 2)}</pre>
      <pre>{JSON.stringify(data.split_fingerprints ?? {}, null, 2)}</pre></details>
  </div></div>;
}

function Criteria({ review }: {review: Json}) {
  const checks: Json[] = review.checks ?? [];
  const failures = checks.filter(row => row.passed === false && row.required !== false);
  return <details className="result-criteria"><summary><span>查看 {checks.length} 项验收指标{failures.length ? ` · ${failures.length} 项未满足` : ''}</span><ChevronDown size={14} /></summary>
    <p>改善比例沿用已保存评估的聚合规则；区间上界与均值改善分别展示。</p>
    <div className="table-scroll"><table><caption>已保存的验证验收结果</caption><thead><tr><th>指标</th><th>结果</th><th>原定门槛</th><th>数值检查</th></tr></thead><tbody>{checks.map((row, i) => {
      return <tr key={`${row.metric}-${i}`} className={row.passed === false ? 'result-failed-row' : ''}>
        <th scope="row"><strong>{label(row.metric)}</strong><small>{row.metric}{row.required === false ? ' · 诊断项，不否决整体结论' : ''}</small></th>
        <td className={finite(row.relative_improvement) && row.relative_improvement < 0 ? 'result-negative' : ''}>{percent(row.relative_improvement)}
          {row.acceptance_inputs && <small>保存的比较输入：候选 {value(row.acceptance_inputs.candidate)} / 基线 {value(row.acceptance_inputs.reference)}</small>}</td>
        <td>{`${row.comparison === 'greater_than' ? '>' : '≥'} ${percent(row.minimum_relative_improvement)}`}</td>
        <td>{row.required === false ? '仅供诊断' : row.passed === true ? '满足' : row.passed === false ? '未满足' : '待核对'}</td></tr>;
    })}</tbody></table></div>
    {review.criteria_complete === false && <p className="result-note">验收完整性仍待核对；已满足的单项指标不代表整体验收通过。</p>}
    <small>来源：{review.source === 'operator_policy_reassessment' ? '用户调整规则后的整体性能重评' : `第 ${review.cycle} 轮已保存验证评审`} · {review.record_id}</small>
  </details>;
}

export function ExperimentSummary({ summary }: {summary?: Json | null}) {
  if (!summary?.groups?.length && !summary?.benchmark_tasks?.length && !summary?.performance_policy_status) return null;
  const groups: Json[] = summary.groups ?? [];
  const sources = [...new Set(groups.map(g => source(g.provenance)))];
  const historicalReview = summary.review;
  const policyUnavailable = summary.performance_policy_status?.state === 'unavailable';
  const assessment = policyUnavailable ? null : summary.performance_assessment;
  const review = policyUnavailable ? null : assessment ? {...historicalReview, source: 'operator_policy_reassessment',
    record_id: assessment.policy_id, criteria_complete: assessment.criteria_complete,
    checks: [...(assessment.checks ?? []), ...(historicalReview?.checks ?? [])
      .filter((row: Json) => row.metric !== assessment.primary_metric).map((row: Json) => ({...row, required: false}))]} : historicalReview;
  const baselines = groups.filter(g => g.variant === 'baseline'), candidates = groups.filter(g => g.variant === 'proposed');
  const primary = review?.checks?.find((row: Json) => row.metric === summary.primary_metric);
  const comparisons: Json[] = summary.observed_comparisons ?? [];
  const observed = !primary ? latestComparison(comparisons) : null;
  const metric = primary?.metric ?? observed?.metric ?? summary.primary_metric;
  const direction = observed?.direction ?? summary.direction;
  const comparison = primary ? review : observed ?? review;
  const assessedGroup = (values: Json[]) => values.find(g => g.trial_ids?.length && g.trial_ids.every((id: string) => assessment?.trial_ids?.includes(id)));
  const baseline = assessment ? assessedGroup(baselines) : comparison ? groups.find(g => g.id === comparison.baseline_group) : baselines.length === 1 ? baselines[0] : null;
  const proposed = assessment ? assessedGroup(candidates) : comparison ? groups.find(g => g.id === comparison.proposed_group) : candidates.length === 1 ? candidates[0] : null;
  const improvement = primary ?? observed;
  const failed: Json[] = (review?.checks ?? []).filter((row: Json) => row.passed === false && row.required !== false);
  const diagnosticLosses: Json[] = (review?.checks ?? []).filter((row: Json) => row.passed === false && row.required === false);
  const metricNames: string[] = [...new Set(groups.flatMap(g => Object.keys(g.metrics ?? {})))];
  metricNames.sort((a, b) => Number(b === metric) - Number(a === metric) || a.localeCompare(b));
  const visibleMetrics = metricNames.slice(0, 4);
  return <section className="experiment-summary" aria-label="实验结果汇总">
    <header className="result-heading"><div><FlaskConical size={20} /><h3>实验结果汇总</h3></div><span>{sources.length === 1 ? sources[0] : sources.length ? '混合来源，详见各组' : '尚无实验记录'} · 已有训练记录 {summary.completed} / {summary.total} 次完成</span></header>
    {policyUnavailable && <div className="result-note" role="status"><strong>当前性能规则待复核</strong><p>{summary.performance_policy_status.message}</p><p>已保存的实验数据仍可查看；当前规则下的性能重评暂不可用，历史评审不代表当前结论。</p></div>}
    {summary.benchmark_tasks?.length > 0 && <div className="table-scroll"><table><caption>按任务协议分别计划、选优和测试</caption>
      <thead><tr><th>任务与来源</th><th>任务条件</th><th>计划进度</th><th>验证选优</th><th>独立测试</th></tr></thead>
      <tbody>{summary.benchmark_tasks.map((task: Json) => <tr key={task.task_id}>
        <th scope="row">{task.dataset}<small>{task.task_id}</small><details className="result-source"><summary>论文参数来源</summary><code>{task.protocol_id}</code><code>SHA256 {task.protocol_sha256}</code></details></th>
        <td>{Object.entries(task.task_dimensions ?? {}).map(([key, val]) => `${key}: ${JSON.stringify(val)}`).join(' · ') || task.task_id}</td><td>{task.completed_trials} / {task.planned_trials}</td>
        <td>{task.validation_complete ? '验证覆盖完整' : '验证未完成'}{task.selection_status === 'frozen' ? <details className="result-source"><summary>本任务已冻结配置</summary><pre>{JSON.stringify({baseline: task.best_baseline?.params, proposed: task.best_proposed?.params}, null, 2)}</pre></details> : <small>尚未冻结选优</small>}</td>
        <td>{({not_run: '未运行', incomplete: '未完成', failed: '执行失败', completed: '执行完成'} as Record<string, string>)[task.test_status] ?? '未核对'}
          <small>数值条件：{({positive: '满足', negative: '未满足', inconclusive: '证据不足'} as Record<string, string>)[task.test_outcome] ?? '证据不足'}</small></td>
      </tr>)}</tbody></table><p className="result-note">每个任务独立选优与配对测试；不同任务条件分别报告比较结果。执行完成和数值条件满足不代表科学审批。</p></div>}
    <div className="result-methods"><div><span>Baseline · 主基线</span><strong>{baseline?.name ?? (baselines.length ? `${baselines.length} 组基线配置` : '尚无基线记录')}</strong><small>{baseline ? loss(baseline.loss) : '详见下表各配置'}</small></div>
      <div><span>候选方法</span><strong>{proposed?.name ?? (candidates.length ? `${candidates.length} 组候选配置` : '尚无候选记录')}</strong><small>{proposed ? loss(proposed.loss) : '详见下表各配置'}</small></div>
      <div className="result-primary" aria-live="polite" aria-atomic="true"><span>{label(metric)} 改善 · 相对主基线</span><strong className={finite(improvement?.relative_improvement) && improvement.relative_improvement < 0 ? 'result-negative' : ''}>{improvement ? percent(improvement.relative_improvement) : comparisons.length > 1 ? `${comparisons.length} 组配对结果` : '未报告'}</strong><small>{primary ? `已保存评估 · ${primary.n_pairs ?? '—'} 对训练种子` : observed ? `${comparisons.length > 1 ? '最新完成配对 · ' : ''}验证集描述性比较 · ${observed.n_pairs} 对训练种子；不代表验收通过` : comparisons.length > 1 ? '各配置独立比较，完成顺序尚不明确' : '尚无与当前记录匹配的验收比较'}</small></div></div>
    {!primary && comparisons.length > 1 && <details className="result-source"><summary>查看全部 {comparisons.length} 组配对改善<ChevronDown size={12} /></summary><p>各配置分别计算；上方随已完成的配对结果更新，未完成实验不会替换已有结果。</p>
      <div className="table-scroll"><table><caption>各配置的验证集描述性比较</caption><thead><tr><th>配对范围</th><th>主指标</th><th>相对改善</th><th>训练种子</th></tr></thead><tbody>{comparisons.map(row => <tr key={`${row.baseline_group}:${row.proposed_group}`}>
        <th scope="row">{row === observed ? '当前显示 · ' : ''}{groups.find(g => g.id === row.baseline_group)?.name ?? '基线'} / {groups.find(g => g.id === row.proposed_group)?.name ?? '候选'}<small>第 {row.cycle ?? '—'} 轮 · {row.n_pairs} 对</small></th><td>{row.metric}</td><td className={row.relative_improvement < 0 ? 'result-negative' : ''}>{percent(row.relative_improvement)}</td><td>{(row.pairs ?? []).map((pair: Json) => pair.seed).join('、')}</td>
      </tr>)}</tbody></table></div></details>}
    {observed && <details className="result-source"><summary>查看改善计算与配对实验<ChevronDown size={12} /></summary>
      <p>先对相同种子的指标取均值，再计算相对改善：{observed.direction === 'maximize' ? '（候选均值 − 基线均值）' : '（基线均值 − 候选均值）'} / |基线均值|。基线 {value(observed.baseline_mean)}，候选 {value(observed.proposed_mean)}。</p>
      <p>这是已保存验证测量的描述性结果；正式验收仍采用原定聚合口径、阈值和独立评审。</p>
      {(observed.pairs ?? []).map((pair: Json) => <div key={String(pair.seed)}><strong>种子 {String(pair.seed)}</strong><p>基线 {value(pair.baseline)} / 候选 {value(pair.proposed)}</p><code>{pair.baseline_trial_id}</code><code>{pair.proposed_trial_id}</code></div>)}
    </details>}
    {(summary.datasets ?? []).map((dataset: Json) => <Dataset key={dataset.key} data={dataset} />)}
    {assessment && <div className="result-note" role="note"><strong>{assessment.research_outcome === 'positive' ? '整体主指标已改善' : assessment.research_outcome === 'negative' ? '整体主指标尚未改善' : '整体性能证据待核对'}</strong><p>当前任务按整体指标判断，局部指标允许取舍。这里是用户调整规则后的重评；原始结果和旧评审保留。</p></div>}
    {diagnosticLosses.length > 0 && <p className="result-note">需说明的性能取舍：{diagnosticLosses.map(row => `${label(row.metric)}${finite(row.relative_improvement) && row.relative_improvement < 0 ? `退化 ${(Math.abs(row.relative_improvement) * 100).toFixed(2)}%` : '未达原门槛'}`).join('；')}。这些诊断项不自动否决整体改善。</p>}
    {failed.length > 0 && <div className="result-verdict" role="note"><strong>{failed.map(row => `${label(row.metric)}${finite(row.relative_improvement) && row.relative_improvement < 0 ? `退化 ${(Math.abs(row.relative_improvement) * 100).toFixed(2)}%` : '未达门槛'}`).join('；')}</strong><span>当前必需的性能条件未满足。</span></div>}
    <div className="result-table-heading"><h4>各方法结果</h4><span>{label(metric)} {direction === 'maximize' ? '越高越好' : '越低越好'} · 同一配置的训练种子均值</span></div>
    <div className="table-scroll result-method-table"><table><caption>方法、训练种子与原始指标汇总</caption><thead><tr><th>方法</th><th>训练种子</th>{visibleMetrics.map(m => <th key={m}>{label(m)}{m === metric ? direction === 'maximize' ? ' ↑' : direction === 'minimize' ? ' ↓' : '' : ''}</th>)}<th>完成记录</th></tr></thead><tbody>{groups.map(group => <tr key={group.id} className={group.variant === 'proposed' ? 'result-candidate-row' : ''}>
      <th scope="row"><strong>{group.name}<span className="result-tag">{variants[group.variant] ?? group.variant}</span></strong><small>{loss(group.loss)} · {phases[group.phase] ?? '划分未记录'}{group.cycle ? ` · 第 ${group.cycle} 轮` : ''}</small>{summary.datasets.length > 1 && <small>{summary.datasets.find((d: Json) => d.key === group.dataset_key)?.name} · 预测 {summary.datasets.find((d: Json) => d.key === group.dataset_key)?.horizon ?? '未记录'}</small>}
        <details className="result-source"><summary>对应实验与版本<ChevronDown size={12} /></summary><p>{source(group.provenance)} · 配置 {group.id} · 代码版本 {group.code_revision ?? '未记录'}</p>{(group.trial_ids ?? []).map((id: string) => <code key={id}>{id}</code>)}</details></th>
      <td>{group.seeds.length ? group.seeds.map((s: unknown) => s ?? '未记录').join('、') : '未记录'}</td>
      {visibleMetrics.map(m => <td className="result-number" key={m}>{value(group.metrics[m]?.mean)}{group.metrics[m]?.mean == null && group.metrics[m] && <small>{group.metrics[m].count}/{group.metrics[m].total} 已报告</small>}</td>)}
      <td>{group.completed} / {group.total}{group.issues.map((issue: string, i: number) => <small className="warn-text" key={i}>{issue}</small>)}</td></tr>)}</tbody></table></div>
    <p className="result-note">表内为原始指标均值。{observed ? '当前改善按配对种子的指标均值计算，仅描述验证结果，不替代正式验收比较。' : assessment ? '当前整体提升比例按配对种子均值计算；历史比较保留原有聚合口径。' : '提升比例来自已保存验收比较，可能采用逐种子比例再平均，因此不一定等于两个均值直接相除。'}不同配置、划分和版本分别列出。</p>
    {summary.truncated && <p className="warn-text">当前仅展示最近 256 次实验，未展示完整验收比较。</p>}
    {summary.superseded_count > 0 && <p className="result-note">另有 {summary.superseded_count} 次旧版本实验，保留在下方分次记录中。</p>}
    {review && <Criteria review={review} />}
    {(assessment || policyUnavailable) && historicalReview && <details className="result-source"><summary>查看规则调整前的历史评审</summary><p>以下为原规则结论；不替代当前整体性能重评。</p><Criteria review={historicalReview} /></details>}
  </section>;
}
