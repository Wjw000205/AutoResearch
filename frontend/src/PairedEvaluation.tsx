import { ReferenceText } from './ReferenceNames';
import { ChevronDown, CircleAlert, GitCompareArrows, ShieldCheck } from 'lucide-react';
import { textOf, type EvaluationProvenance, type PairedComparison, type TrialEvaluation } from './api';

const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
const metricValue = (value: unknown, signed = false) => finite(value) ? `${signed && value > 0 ? '+' : ''}${value.toPrecision(5)}` : '—';
const directionLabel = (direction: string | undefined) => ['min', 'minimize', 'lower'].includes(direction ?? '') ? '越低越好' : ['max', 'maximize', 'higher'].includes(direction ?? '') ? '越高越好' : '目标方向未声明';
const provenanceLabel = (status: string | undefined) => status === 'consistent' ? '来源指纹内部一致' : ['inconsistent', 'conflict', 'conflicting'].includes(status ?? '') ? '来源指纹冲突' : '划分来源未验证';
const issueLabels: Record<string, string> = {
  missing_pair: '缺少同种子配对', duplicate_seed: '同一配置存在重复种子', missing_seed: '种子未记录', invalid_metric: '指标值不可用', invalid_parameters: '实验参数不完整',
  split_fingerprint_mismatch: '数据划分指纹冲突', dataset_mismatch: '数据集来源不一致', unverified_evaluation: '评估来源未验证', small_sample: '配对样本不足',
  unequal_tuning_budget: '基线与候选调参预算不等', unverified_tuning_budget: '调参预算未验证', conflicting_tuning_budget: '调参预算记录冲突',
  mixed_provenance: '实验来源混合', simulation_records: '包含模拟实验记录', no_comparable_pairs: '没有可比较的配对',
  multiple_configurations: '多个配置分别比较，不合并为独立重复',
};

function Provenance({ value }: { value?: EvaluationProvenance }) {
  return <div className={`paired-provenance ${value?.status === 'consistent' ? 'is-consistent' : ''}`}><ShieldCheck size={14} /><div><strong>{provenanceLabel(value?.status)}</strong><p>指纹一致只能确认记录之间的一致性，不证明数据划分独立、无泄漏或科学结论成立。</p>{value?.verification_scope && <p><ReferenceText value={value.verification_scope} /></p>}</div></div>;
}

function Comparison({ value, index }: { value: PairedComparison; index: number }) {
  const pairs = Array.isArray(value.pairs) ? value.pairs : [];
  const limitations = Array.isArray(value.limitations) ? value.limitations : [];
  const interval = value.ci95 && finite(value.ci95.low) && finite(value.ci95.high) ? value.ci95 : null;
  return <article className="paired-comparison">
    <div className="paired-comparison-heading"><div><span>配置对 {index + 1}</span><h4><ReferenceText value={value.metric} /></h4></div><span className="pill">{directionLabel(value.direction)}</span><strong className="paired-count">{value.n_pairs}<small>对相同随机种子</small></strong></div>
    <div className="paired-means"><div><span>基线均值</span><strong>{metricValue(value.baseline_mean)}</strong></div><div><span>候选均值</span><strong>{metricValue(value.proposed_mean)}</strong></div><div><span>平均差（候选 − 基线）</span><strong>{metricValue(value.mean_delta, true)}</strong></div><div><span>按目标方向换算的改善量</span><strong>{metricValue(value.improvement, true)}</strong></div></div>
    <p className="paired-definition">差值保留原始方向；改善量是指标绝对差，不是百分比。正值仅表示当前配对结果按目标方向更好，不表示统计显著性。</p>
    <div className="paired-interval"><span>平均差的 95% 区间</span><strong>{interval ? `[${metricValue(interval.low)}, ${metricValue(interval.high)}]` : '未计算'}</strong><small>{interval ? `方法：${interval.method}；最低样本数 ${interval.n_min}` : value.n_pairs < 5 ? '配对样本不足（少于 5 对），不提供区间估计。' : '评估未提供可用区间，请查看限制说明。'}</small>{interval?.assumptions && <small>计算假设：{interval.assumptions}</small>}</div>
    {value.n_pairs < 5 && <div className="paired-small-sample"><CircleAlert size={14} /><span>当前仅 {value.n_pairs} 对种子，样本不足；结果仅作描述性比较。</span></div>}
    <Provenance value={value.provenance} />
    {pairs.length ? <div className="table-scroll paired-table"><table><caption>同一随机种子的基线与候选结果</caption><thead><tr><th>随机种子</th><th>基线</th><th>候选</th><th>差值（候选 − 基线）</th><th>来源状态</th></tr></thead><tbody>{pairs.map((pair, pairIndex) => <tr key={`${pair.seed}-${pair.baseline_trial_id}-${pair.proposed_trial_id}-${pairIndex}`}><td className="mono">{pair.seed}</td><td className="mono" title="基线实验结果">{metricValue(pair.baseline)}</td><td className="mono" title="候选实验结果">{metricValue(pair.proposed)}</td><td className="mono">{metricValue(pair.delta, true)}</td><td><span className={pair.provenance_status === 'consistent' ? 'muted' : 'warn-text'}>{provenanceLabel(pair.provenance_status)}</span></td></tr>)}</tbody></table></div> : <p className="paired-no-pairs">没有可展示的按随机种子配对的记录；不会从汇总值推造原始记录。</p>}
    {limitations.length > 0 && <div className="paired-limitations"><h5>本配置比较的限制</h5><ul>{limitations.map((limitation, limitationIndex) => <li key={limitationIndex}>{limitation}</li>)}</ul></div>}
    <details className="paired-configuration"><summary>配置参数、种子与实验标识<ChevronDown size={13} /></summary><code><ReferenceText value={value.configuration} /></code><div className="paired-parameter-grid"><div><h5>基线</h5><code><ReferenceText value={value.baseline_configuration} /></code><pre><ReferenceText value={textOf(value.baseline_parameters) || '未记录配置参数'} /></pre></div><div><h5>候选</h5><code><ReferenceText value={value.proposed_configuration} /></code><pre><ReferenceText value={textOf(value.proposed_parameters) || '未记录配置参数'} /></pre></div></div><p>种子：<ReferenceText value={(value.seeds ?? []).join('、') || '未记录'} /></p>{pairs.map((pair, pairIndex) => <div className="paired-trial-ids" key={pairIndex}><strong>随机种子 {pair.seed}</strong><code>基线：<ReferenceText value={pair.baseline_trial_id} /></code><code>候选：<ReferenceText value={pair.proposed_trial_id} /></code></div>)}</details>
  </article>;
}

export function PairedEvaluation({ evaluation }: { evaluation?: TrialEvaluation | null }) {
  if (!evaluation) return <section className="paired-evaluation paired-unavailable"><div className="paired-heading"><GitCompareArrows size={18} /><h3>同种子配对比较</h3></div><p>此任务尚无配对评估记录。逐次实验结果仍可查看，不会由前端推造统计结论。</p></section>;
  const comparisons = Array.isArray(evaluation.paired_comparisons) ? evaluation.paired_comparisons : [];
  const issues = Array.isArray(evaluation.issues) ? evaluation.issues : [];
  return <section className="paired-evaluation" aria-labelledby="paired-evaluation-title"><div className="paired-heading"><GitCompareArrows size={18} /><div><h3 id="paired-evaluation-title">同种子配对比较</h3><p>按后端确认的配置与随机种子配对；不同参数组分别计算。</p></div></div>
    {issues.length > 0 && <div className="evaluation-issues"><h4><CircleAlert size={15} />影响结果解释的问题 · {issues.length}</h4><ul>{issues.map((issue, index) => <li key={`${issue.code}-${index}`}><strong>{issueLabels[issue.code] ?? issue.code}</strong><p><ReferenceText value={issue.message} /></p><details><summary>问题代码与关联实验<ChevronDown size={12} /></summary><code><ReferenceText value={issue.code} /> · <ReferenceText value={issue.severity} /></code>{(issue.trial_ids ?? []).map(id => <code key={id}><ReferenceText value={id} /></code>)}</details></li>)}</ul></div>}
    {comparisons.length ? comparisons.map((comparison, index) => <Comparison value={comparison} index={index} key={`${comparison.comparison_id ?? comparison.configuration}-${comparison.metric}-${index}`} />) : <div className="paired-no-comparisons"><p>尚无满足配对条件的基线 / 候选记录。请查看问题清单与实验来源。</p><Provenance value={evaluation.provenance} /></div>}
    <div className="evaluation-footer"><ShieldCheck size={14} /><p>未作显著性判断。样本数量、调参预算和划分来源会影响比较；区间与改善量均不构成发表结论。</p></div>
    <details className="evaluation-provenance"><summary>评估来源与计算范围<ChevronDown size={13} /></summary><pre><ReferenceText value={evaluation.provenance} /></pre></details>
  </section>;
}
