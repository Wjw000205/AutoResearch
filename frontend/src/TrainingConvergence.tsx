import { type Json } from './api';

const roles: Record<string, string> = {director: 'Director · 目标与方法', engineer: 'Engineer · 训练与架构', scout: 'Scout · 反证与公平性'};
const states: Record<string, string> = {observed: '诊断已记录', diagnosing: '三方分析原因', proposing: '拟定最小调整', reviewing: '三方复核方案', accepted_for_revision: '三方同意 · 待修订与复验', reviewed_advisory: '建议已复核 · 训练仍停止', continue: '三方同意按原计划继续', disputed: '存在异议 · 调整未批准', needs_evidence: '等待补充证据', stopped: '三方建议停止'};
const actions: Record<string, string> = {adjust: '最小调整方案', continue: '继续原计划', collect_evidence: '补证据方案', stop: '停止建议'};

export function TrainingConvergence({ diagnosis, discussion, running = false }: {diagnosis?: Json; discussion?: Json; running?: boolean}) {
  if (!diagnosis) return null;
  const proposal = discussion?.proposal;
  return <section className="training-convergence" aria-label="loss 收敛诊断与三方微调">
    <header><strong>Loss 收敛诊断</strong><span data-alert={diagnosis.needs_discussion ? 'true' : 'false'}>{diagnosis.label}</span></header>
    <p>{diagnosis.reason}</p>
    <div className="loss-diagnostic-facts"><span>当前阶段：{diagnosis.identity?.[0] ?? '未报告'}</span><span>有效观测：{diagnosis.points ?? 0}</span><span>趋势规则 v{diagnosis.policy?.version ?? '—'} · 科学验收另行判断</span></div>
    <details><summary>查看判断依据</summary><p>{diagnosis.scope}</p><p>每个窗口 {diagnosis.policy?.window ?? '—'} 点，至少 {diagnosis.policy?.minimum_points ?? '—'} 点；近期改善阈值 1%，初期改善参考 5%，连续上升阈值 10%。这些阈值仅用于触发诊断。</p>{(diagnosis.segments ?? []).map((segment: Json, i: number) => <p key={i}>{segment.identity?.[0] ?? '未报告阶段'} · {segment.points} 点 · {segment.reason}</p>)}</details>
    {(!discussion || discussion.status === 'observed') && <p className="loss-discussion-note">{running ? '正在记录训练趋势；本次验证训练结束后，告警会触发三方快速讨论。' : discussion?.discussion_skipped ?? '出现明确 loss 告警时进入三方快速讨论；缺少日志不视为已收敛。'}</p>}
    {discussion && discussion.status !== 'observed' && <>
      <h4>三 agent 快速微调 <span>{states[discussion.status] ?? discussion.status}</span></h4>
      <p className="loss-discussion-note">{discussion.review_only ? '本轮只复核既有证据；原负结果与停止规则保留。建议须另行预注册和审核，当前不授权训练。' : '一轮独立分析 → 一个最小方案 → 三方复核。讨论同意后仍需完成修订、审核和重新验证。'}</p>
      <div className="loss-role-grid">{Object.entries(roles).map(([role, label]) => {
        const analysis = (discussion.analyses ?? []).find((row: Json) => row.role === role);
        const vote = (discussion.votes ?? []).find((row: Json) => row.role === role);
        return <article key={role}><strong>{label}</strong><span>{vote ? vote.approve && !vote.issues?.length ? '同意方案' : '保留异议' : analysis ? '已提交分析' : '等待分析'}</span><p>{analysis?.assessment ?? '尚无已保存意见。'}</p>
          {analysis && <details><summary>原因与验证建议</summary>{(analysis.hypotheses ?? []).map((hypothesis: Json, i: number) => <div key={i}><p><b>原因假设：</b>{hypothesis.cause}</p><p><b>观测依据：</b>{hypothesis.evidence}</p><p><b>验证方法：</b>{hypothesis.verification}</p></div>)}{analysis.limitations?.length > 0 && <p>局限：{analysis.limitations.join('；')}</p>}</details>}
          {vote && <p>{vote.rationale}{vote.issues?.length > 0 ? `；异议：${vote.issues.join('；')}` : ''}</p>}</article>;
      })}</div>
      {proposal && <details className="loss-proposal" open><summary>{actions[proposal.action] ?? '调整方案'}</summary><p>{proposal.rationale}</p>{(proposal.changes ?? []).map((change: Json, i: number) => <div className="loss-change" key={i}><b>{change.target}</b><p>修改前：{change.before}</p><p>修改后：{change.after}</p><p>{change.reason}</p></div>)}<p><b>验证条件：</b>{proposal.validation?.join('；')}</p><p><b>回退条件：</b>{proposal.rollback_condition}</p></details>}
      <details><summary>证据与版本绑定</summary><p>实验：{discussion.trial_id} · 周期 {discussion.cycle} · 来源 {discussion.provenance}</p><p>证据 SHA：{discussion.evidence_sha256}</p><p>方案 SHA：{discussion.proposal_sha256 ?? '尚未生成'}</p><p>此处显示已保存的讨论记录，不代表修改已执行或验证通过。</p></details>
    </>}
    {discussion?.followup && <div className="loss-followup"><strong>调整后的训练观察</strong><p>原实验：{discussion.followup.previous_trial_id} · 当前诊断：{diagnosis.label}</p><p>仍需核验：{discussion.followup.validation?.join('；')}</p><p>回退条件：{discussion.followup.rollback_condition}</p></div>}
  </section>;
}
