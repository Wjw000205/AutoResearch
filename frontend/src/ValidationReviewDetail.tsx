import { useState } from 'react';
import { CheckCircle2, ChevronRight, FileText, RotateCcw } from 'lucide-react';
import { textOf, type Json, type Run } from './api';
import { ReferenceText } from './ReferenceNames';
import { ValidationHoldFacts, validationResumeReason } from './ValidationHold';
import './validation-review.css';

// Translate only the engine's exact, persisted messages. Unknown prose stays intact.
const reasonLabels: [string, string, string][] = [
  ['Comparable validation evidence does not demonstrate a positive improvement meeting the required relative threshold.', '改善证据不足', '现有可比验证证据尚不能证明正向改善达到要求的相对阈值。'],
  ['The recorded quantitative acceptance checks are malformed; repair the evidence before making a research decision.', '验收记录格式异常', '量化验收记录格式不正确，需要修复证据后再作研究决定。'],
  ['The predeclared quantitative acceptance contract is incomplete; resolve the missing conditions before final testing.', '验收条件不完整', '预先声明的量化验收条件尚未补齐，进入最终测试前需解决缺失条件。'],
  ['The selected validation pair failed or lacks evidence for at least one predeclared primary/secondary numeric criterion.', '部分指标未通过或缺少证据', '所选验证配对至少有一项预先声明的主指标或次指标未通过，或缺少对应证据。'],
  ['Director, engineer and scout must each explicitly approve the same current proposal and revision with no required changes.', '尚未取得同版本三方批准', '研究总监、实验工程师和文献研究员必须明确批准同一当前方案与修订，且不再有必改项。'],
  ['The configured cycle limit has already been exceeded.', '研究轮次超限', '已超过配置的研究轮次上限。'],
  ['The validation improvement meets the threshold and all three roles accept this exact candidate; freeze it before held-out evaluation.', '具备冻结条件', '验证改善已达到阈值，三位角色均接受这一候选；独立测试前仍需冻结方案。'],
  ['The predeclared stop policy ends this study with a descriptive negative validation result. This does not establish method inferiority, generalization, held-out test success or research success.', '按预设策略停止', '本研究以描述性的负验证结果结束。这不证明方法整体更差、泛化表现、独立测试成功或研究成功。'],
  ['The frozen stop policy forbids automatically revising a reported negative validation. The evidence is not yet sufficient for the formal negative-stop classifier; reconcile the recorded acceptance completeness and comparison evidence without new training or final tests.', '停止规则阻止自动修订', '冻结的停止规则禁止自动修订已报告的负验证结果。现有证据尚不足以正式判定负结果停止，需核对验收完整性与比较证据，不新增训练或最终测试。'],
  ['Revise the design, code or parameters using validation evidence and unresolved objections, then evaluate another cycle.', '记录中的后续建议', '依据验证证据和未解决的异议修订设计、代码或参数，再进行下一轮评估。'],
  ['The cycle limit is exhausted; research success is not established.', '研究轮次已用尽', '已用尽研究轮次，尚未确立研究成功。'],
];

export function validationReasons(value: unknown) {
  let remaining = textOf(value).trim();
  const result: { title: string; text: string }[] = [];
  while (remaining) {
    const match = reasonLabels.map(([source, title, text]) => ({ source, title, text, at: remaining.indexOf(source) }))
      .filter(item => item.at >= 0).sort((a, b) => a.at - b.at)[0];
    if (!match) { result.push({ title: '', text: remaining }); break; }
    const before = remaining.slice(0, match.at).trim();
    if (before) result.push({ title: '', text: before });
    result.push({ title: match.title, text: match.text });
    remaining = remaining.slice(match.at + match.source.length).trim();
  }
  return result;
}

const decisions: Record<string, { label: string; tone: string }> = {
  iterate: { label: '建议继续迭代', tone: 'warning' },
  finalize: { label: '可准备独立测试', tone: 'success' },
  blocked: { label: '等待处理', tone: 'warning' },
  stop: { label: '按策略停止', tone: 'neutral' },
};
const roles: Record<string, string> = { director: '研究总监', engineer: '实验工程师', scout: '文献研究员' };
const runStates: Record<string, string> = { running: '进行中', blocked: '等待处理', paused: '已暂停', failed: '失败', completed: '已完成', cancelled: '已取消' };
const percentage = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? `${(value * 100).toFixed(2)}%` : '未记录';
const checkLabel = (value: unknown) => value === true ? '通过' : value === false ? '未通过' : '未记录';

export function StageRawRecord({ output }: { output: Json }) {
  const [open, setOpen] = useState(false);
  return <details className="stage-record-disclosure" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary><FileText size={17} aria-hidden="true" /><span>完整阶段记录<small>查看原始字段与完整证据</small></span><ChevronRight size={17} aria-hidden="true" /></summary>
    {open && <pre tabIndex={0} aria-label="完整阶段记录 JSON">{JSON.stringify(output, null, 2)}</pre>}
  </details>;
}

export function ValidationReviewDetail({ output, run }: { output?: Json; run: Run }) {
  if (!output) return <p className="validation-empty">本阶段尚无已保存的评审结果。</p>;
  const decision = decisions[String(output.decision)] ?? { label: textOf(output.decision) || '尚未记录', tone: 'neutral' };
  const held = !!validationResumeReason(run);
  const acceptance = output.validation_summary?.acceptance_checks;
  const checks: Json[] = Array.isArray(acceptance?.checks) ? acceptance.checks.filter((item: unknown) => item && typeof item === 'object') : [];
  const votes: Json[] = Array.isArray(output.review_votes) ? output.review_votes.filter((item: unknown) => item && typeof item === 'object') : [];
  const reasons = validationReasons(output.rationale);
  return <div className="validation-review-detail">
    <section className={`validation-decision ${decision.tone}`} aria-label="本轮评审结论">
      <div className="validation-decision-icon" aria-hidden="true">{output.decision === 'finalize' ? <CheckCircle2 size={24} /> : <RotateCcw size={24} />}</div>
      <div><div className="validation-eyebrow">已保存的阶段决定{output.cycle != null && <span>第 {textOf(output.cycle)} 轮</span>}</div>
        <h3>{held ? '验证结果待核对，自动修订已停止' : decision.label}</h3><p>{held ? `原保存决定：${decision.label}；与停止规则冲突，当前不能执行。` : `本轮评审记录 · 当前任务：${runStates[run.status] ?? run.status}`}</p></div>
      {output.decision && <code>{textOf(output.decision)}</code>}
    </section>

    {held && <ValidationHoldFacts hold={run.validation_hold} />}

    <dl className="validation-gates">
      <div><dt>量化验收</dt><dd>{output.threshold_met === true ? '已达标' : output.threshold_met === false ? '未达标' : '未记录'}</dd></div>
      <div><dt>三方共识</dt><dd>{output.unanimous === true ? '一致批准' : output.unanimous === false ? '尚未一致批准' : '未记录'}</dd></div>
      <div><dt>验收条件</dt><dd>{acceptance?.criteria_complete === true ? '完整' : acceptance?.criteria_complete === false ? '尚不完整' : '未记录'}</dd></div>
    </dl>

    <section className="validation-section" aria-label="判断依据">
      <div className="validation-section-heading"><h3>判断依据</h3><span>按已保存记录整理</span></div>
      {reasons.length ? <ol className="validation-reasons">{reasons.map((reason, i) => <li key={i}>
        <span className="validation-reason-number" aria-hidden="true">{i + 1}</span><div>{reason.title && <h4>{reason.title}</h4>}<p><ReferenceText value={reason.text} /></p></div>
      </li>)}</ol> : <p>尚未保存判断依据。</p>}
      {output.rationale && <details className="validation-original"><summary>查看判断依据原文</summary><p>{textOf(output.rationale)}</p></details>}
    </section>

    {checks.length > 0 && <details className="validation-evidence">
      <summary><span><strong>逐项验收结果</strong><small>{checks.length} 项指标 · {checks.filter(check => check.passed === true).length} 项通过 · {checks.filter(check => check.passed === false).length} 项未通过{checks.some(check => typeof check.passed !== 'boolean') && ' · 含未记录结果'}</small></span><ChevronRight size={18} aria-hidden="true" /></summary>
      <div className="validation-checks"><p>以下为已保存的验收判定；相对改善按百分比显示，完整精度与证据见原始记录。</p>
        <div className="validation-table-scroll" tabIndex={0} role="region" aria-label="验收指标表"><table><thead><tr><th scope="col">指标</th><th scope="col">相对改善</th><th scope="col">要求</th><th scope="col">记录结果</th></tr></thead><tbody>{checks.map((check, i) => <tr key={i} className={check.passed === false ? 'validation-check-failed' : ''}>
          <th scope="row"><code>{textOf(check.metric) || '未记录指标'}</code></th><td>{percentage(check.relative_improvement)}</td><td>{check.comparison === 'greater_than' ? '> ' : check.comparison === 'at_least' ? '≥ ' : `${textOf(check.comparison)} `}{percentage(check.minimum_relative_improvement)}</td><td><span className={`validation-result ${check.passed === true ? 'success' : check.passed === false ? 'warning' : 'neutral'}`}>{checkLabel(check.passed)}</span></td>
        </tr>)}</tbody></table></div>
      </div>
    </details>}

    {votes.length > 0 && <section className="validation-section validation-votes" aria-label="三方评审意见">
      <div className="validation-section-heading"><h3>三方评审意见</h3><span>{votes.length} 份已保存意见</span></div>
      {votes.map((vote, i) => <details className="validation-evidence" key={i}>
        <summary><span><strong>{roles[vote.role] ?? textOf(vote.role) ?? '评审角色'}</strong><small>{Array.isArray(vote.required_changes) ? `${vote.required_changes.length} 项修改与核查要求` : '修改要求未记录'}</small></span><span className={`validation-result ${vote.approve === true ? 'success' : vote.approve === false ? 'warning' : 'neutral'}`}>{vote.approve === true ? '批准' : vote.approve === false ? '不批准' : '未记录'}</span><ChevronRight size={18} aria-hidden="true" /></summary>
        <div className="validation-vote-body"><h4>评审依据</h4><p><ReferenceText value={vote.rationale ?? '未记录'} /></p>{Array.isArray(vote.required_changes) && vote.required_changes.length > 0 && <><h4>修改与核查要求</h4><ol>{vote.required_changes.map((change: unknown, j: number) => <li key={j}><ReferenceText value={change} /></li>)}</ol></>}</div>
      </details>)}
    </section>}
    <StageRawRecord output={output} />
  </div>;
}
