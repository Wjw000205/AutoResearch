import { date, textOf, type Json } from './api';
import { ArrowUpRight } from 'lucide-react';
import './implementation-progress.css';
import { LiveCodeReviewProgress } from './CodeReviewProgress';

const statuses: Record<string, string> = { running: '进行中', paused: '已暂停', blocked: '等待处理', failed: '执行失败', cancelled: '已取消', completed: '已完成' };
const roleNames: Record<string, string> = { director: '研究总监', engineer: '实验工程师', scout: '文献研究员' };

export function wholeReviewSummary(progress: Json) {
  const workflow = progress.workflow;
  const value = workflow?.whole_review;
  if (workflow?.review_mode !== 'whole_document' || value?.version !== 1 || value.binding_valid !== true
    || value.projection_only !== true || value.scientific_approval !== false
    || value.run_id !== progress.run_id || value.workflow_id !== workflow.id
    || !Number.isInteger(value.revision) || !value.document_id || !value.document_sha256
    || value.counts?.expected_reviews !== 2 || !Array.isArray(value.roles) || value.roles.length !== 2
    || value.roles[0].role !== 'director' || value.roles[1].role !== 'scout') return null;
  const saved = value.roles.filter((r: Json) => r.report_saved === true);
  if (saved.some((r: Json) => !r.handoff_id || !['APPROVE', 'REQUEST_CHANGES'].includes(r.verdict))
    || value.counts.saved_reviews !== saved.length
    || value.counts.approved_reviews !== saved.filter((r: Json) => r.verdict === 'APPROVE').length
    || value.counts.changes_required_reviews !== saved.filter((r: Json) => r.verdict === 'REQUEST_CHANGES').length
    || value.complete === true && ['APPROVED', 'APPROVED_WITH_NOTES'].includes(value.outcome)
      && value.counts.approved_reviews !== 2) return null;
  return value;
}

function indexedReviewSummary(progress: Json) {
  const workflow = progress.workflow;
  const value = workflow?.review_mode === 'indexed_review' ? workflow.indexed_review : null;
  const counts = value?.counts;
  if (workflow?.review_mode !== 'indexed_review' || value?.version !== 1 || value.binding_valid !== true
    || value.scientific_approval !== false || value.run_id !== progress.run_id || value.workflow_id !== workflow.id
    || value.revision !== workflow.document_revision || value.document_id !== workflow.document_id
    || value.document_sha256 !== workflow.document_sha256 || value.spec_hash !== workflow.spec_hash
    || counts?.expected_reviews !== 3 || ![counts.saved_reviews, counts.approved_reviews, counts.changes_required_reviews]
      .every(n => Number.isInteger(n) && n >= 0 && n <= 3)
    || counts.approved_reviews + counts.changes_required_reviews > counts.saved_reviews) return null;
  return value;
}

export function implementationReviewSummary(progress: Json | null | undefined, runId: string): {
  version: string; scopes: string; finals: string; inherited?: string;
} | null {
  if (!progress || progress.run_id !== runId || !['document_authoring', 'document_review'].includes(progress.phase)) return null;
  const workflow = progress.workflow;
  if (workflow?.review_mode === 'whole_document') {
    const value = wholeReviewSummary(progress);
    return { version: value?.document_title?.split(/[\\/]/).pop() ?? '本轮实现文档',
      scopes: `整篇独立审核 ${value?.counts.saved_reviews ?? 0} / 2`,
      finals: value?.complete && value.outcome === 'REPAIR' ? '待修改，尚未通过最终验收'
        : value?.complete && ['APPROVED', 'APPROVED_WITH_NOTES'].includes(value.outcome) ? '文档审核通过' : '等待同版本审核意见' };
  }
  if (progress.phase === 'document_review' && !['indexed_review', 'acceptance_contract', 'definition_repair'].includes(workflow?.review_mode)) return null;
  const revision = progress.phase === 'document_authoring' ? workflow?.revision : workflow?.document_revision;
  if (!Number.isInteger(revision) || revision < 0) return null;
  const indexedMode = workflow?.review_mode === 'indexed_review';
  const whole = indexedReviewSummary(progress);
  if (workflow?.review_mode === 'definition_repair') return {
    version: `第 ${revision + 1} 版`, scopes: progress.phase === 'document_authoring' ? '补齐资源估算定义' : '三方审核补充定义', finals: '',
  };
  if (workflow?.review_mode === 'acceptance_contract') return {
    version: `第 ${revision + 1} 版`, scopes: `合同验收 · ${workflow.acceptance?.outcome ?? workflow.acceptance?.phase ?? '准备中'}`, finals: '',
  };
  return {
    version: `第 ${revision + 1} 版`,
    scopes: progress.phase === 'document_authoring' ? '正在修订文档' : indexedMode
      ? `索引全文意见 ${whole?.counts.saved_reviews ?? 0} / 3` : '当前全文审核待同步',
    finals: '',
  };
}

export function ImplementationReviewSummary({ value }: { value: NonNullable<ReturnType<typeof implementationReviewSummary>> }) {
  return <span className="implementation-review-summary"><span>{value.version} · {value.scopes}</span>{value.inherited && <span>{value.inherited}</span>}{value.finals && <span>{value.finals}</span>}</span>;
}

export function implementationPhaseLabel(progress: Json) {
  return progress.phase === 'code_review' && progress.workflow?.review_mode === 'whole_document'
    ? '实际代码独立审核' : progress.label;
}

export function ImplementationProgressEntry({ progress, runId, onDetails }: {
  progress?: Json | null; runId: string; onDetails: () => void;
}) {
  if (!progress || progress.run_id !== runId) return null;
  return <section className="implementation-progress-entry" aria-label="代码实现进度摘要">
    <div><div className="implementation-entry-heading"><h3>代码实现进度</h3>
      <span className={`implementation-pill state-${progress.run_status}`}>{statuses[progress.run_status] ?? '状态未报告'}</span></div>
      <p>{implementationPhaseLabel(progress) || '等待进度回传'}</p>
      <small>查看流程进度、审核意见与交付记录</small></div>
    <button type="button" className="btn" aria-haspopup="dialog" onClick={onDetails}>查看实现详情<ArrowUpRight size={15} /></button>
    <LiveCodeReviewProgress progress={progress} runId={runId} />
  </section>;
}

export function ImplementationProgress({ progress, runId }: { progress?: Json | null; runId: string }) {
  if (!progress || progress.run_id !== runId) return null;
  const workflow = progress.workflow;
  if (progress.phase === 'document_review' && !['indexed_review', 'acceptance_contract', 'whole_document'].includes(workflow?.review_mode)) return null;
  const authoring = progress.phase === 'document_authoring' ? progress.authoring : null;
  const indexedMode = progress.phase === 'document_review' && workflow?.review_mode === 'indexed_review';
  const whole = indexedReviewSummary(progress);
  return <section className="panel implementation-progress" aria-label="代码实现进度">
    <header><h3>代码实现进度</h3><span>{statuses[progress.run_status] ?? '状态未报告'}</span></header>
    <div className="implementation-progress-intro"><h4>{implementationPhaseLabel(progress)}</h4><p>{progress.message}</p></div>
    {workflow?.review_mode === 'acceptance_contract' && <AcceptanceProgress value={workflow.acceptance} />}
    {workflow?.review_mode === 'whole_document' && <WholeDocumentProgress progress={progress} />}
    {authoring && <div className="implementation-review-progress" aria-label="实现文档编写活动">
      <p><strong>{authoring.completion_phase ? '文档保存阶段' : '源文档读取与编写'}：</strong>已记录 {authoring.tool_turns} 轮工具交互</p>
      <p>本交接已保存读取回执 {authoring.read_calls} 次 · {authoring.read_documents} 个文档 · {authoring.read_sections} 个不同章节 · {authoring.read_ranges} 个不同范围</p>
      {authoring.inherited_reads && <p>继承原编写阶段读取回执 {authoring.inherited_reads.read_calls} 次 · {authoring.inherited_reads.read_documents} 个文档 · {authoring.inherited_reads.read_sections} 个不同章节 · {authoring.inherited_reads.read_ranges} 个不同范围。这些已保存记录供本阶段复用，不计为本次新读取。</p>}
      <p><strong>实现文档：</strong>{authoring.document_saved ? '已保存，等待工程师检查并提交同版本文档。'
        : authoring.write_calls > 0 ? `已尝试写入 ${authoring.write_calls} 次，尚未取得成功写入回执。`
          : '尚未调用文档写入工具。'}</p>
      <p>读取回执仅记录已取得的正文范围，不代表文档已写完或代码已实现；实现文档提交后仍需独立审计。</p>
      <AuthorSaveError authoring={progress.details?.authoring ?? authoring} />
      {authoring.error && !progress.block_reason && <p className="inline-warning">{authoring.error}</p>}
    </div>}
    {indexedMode && <div className="implementation-review-progress" aria-label="三方索引全文审核进度">
      <p><strong>索引全文意见 {whole?.counts.saved_reviews ?? 0} / 3</strong> · 同意 {whole?.counts.approved_reviews ?? 0} 份 · 要求修改 {whole?.counts.changes_required_reviews ?? 0} 份</p>
      <progress aria-label="已保存索引全文意见" value={whole?.counts.saved_reviews ?? 0} max={3} />
      <p>三方分别从目录通过工具读完已批准架构与当前实现文档。收齐三份意见后，工程师统一修订并为每位审查者保存独立回复文档，再交三方阅读全文复审。</p>
      {Array.isArray(whole?.roles) && whole.roles.map((role: Json) => <p key={role.role}>
        <strong>{roleNames[role.role] ?? role.role}：</strong>
        {role.reading_complete === true ? '全文工具读取已完成' : role.reading_complete === false ? '全文工具读取尚未完成' : '全文读取状态尚未报告'}
      </p>)}
      <p>三方同版本阅读完整且全部同意、原有执行门禁通过后，才派发代码。</p>
    </div>}
    <section className="implementation-delivery-summary" aria-label="代码交付状态"><h4>代码交付</h4>
      <p><strong>代码生成：</strong>{progress.code_generation_started ? '已派发，实际文件与验收记录见“代码实现与验收”。' : '尚未派发。'}</p>
      {workflow && <p>实现尝试 {workflow.attempt_count} 次 · 当前交付文件 {workflow.file_count} 个</p>}
    </section>
    {progress.block_reason && <p className="inline-warning">{progress.block_reason}</p>}
    <p className="implementation-progress-next"><strong>下一步：</strong>{progress.next_action}</p>
    <small>最近保存：{date(progress.updated_at, true)} · 进度随研究状态自动更新</small>
  </section>;
}

export function WholeDocumentProgress({ progress }: { progress: Json }) {
  const value = wholeReviewSummary(progress);
  if (!value) return <p>整篇审核记录正在同步；尚无可核对的同版本结论。</p>;
  const issues: Json[] = (progress.workflow.acceptance?.issues ?? []).filter((i: Json) =>
    (i.document_sha256 === value.document_sha256 || i.whole_review_feedback?.document_sha256 === value.document_sha256)
    && ['PROPOSED', 'OPEN', 'PATCHED', 'VERIFIED'].includes(i.status));
  const verdicts: Record<string, string> = { APPROVE: '通过', REQUEST_CHANGES: '要求修改' };
  const outcomes: Record<string, string> = { REPAIR: '待修改，尚未通过最终验收', APPROVED: '本版本文档审核通过',
    APPROVED_WITH_NOTES: '本版本文档审核通过，保留非阻断意见', BLOCKED: '审核阻断',
    NEEDS_PROBE: '需要补充验证', NEEDS_REPLAN: '需要重新规划' };
  return <div className="implementation-review-progress" aria-label="总监与研究员整篇独立审核进度">
    <h4>实现文档审核</h4>
    <p><strong>整篇独立审核 {value.counts.saved_reviews} / 2</strong> · 通过 {value.counts.approved_reviews} 份 · 要求修改 {value.counts.changes_required_reviews} 份</p>
    <p className="implementation-review-document">审核文档：<span>{value.document_title ?? value.document_id}</span></p>
    <div className="implementation-review-role-list">{value.roles.map((role: Json) => <article key={role.role} className="implementation-review-opinion">
      <header><h5>{roleNames[role.role]}</h5><span className={`implementation-pill verdict-${role.verdict ?? 'pending'}`}>{role.report_saved ? verdicts[role.verdict] : '等待意见'}</span></header>
      {role.summary ? <details><summary>展开审核摘要</summary><p>{role.summary}</p></details>
        : <p className="muted">尚未收齐有效意见</p>}
    </article>)}</div>
    <p><strong>文档验收：</strong>{value.complete ? outcomes[value.outcome] ?? '结论待同步' : '尚未收齐同版本意见'}</p>
    {issues.length > 0 && <details open><summary>待处理修改意见（{issues.length}）</summary>
      {issues.map(issue => <article key={issue.issue_id}>
        <p><strong>{issue.issue_id} · {issue.severity}</strong> {issue.claim}</p>
        <p>修改要求：{issue.minimal_fix}</p>
        <p>修改位置：{(issue.change_locations ?? []).map((location: Json) =>
          `${location.section_id} · 偏移 ${location.offset} · 长度 ${location.limit}`).join('；') || '见原始审核记录'}</p>
        <p>关闭条件：{(issue.close_when ?? []).join('；')}</p>
      </article>)}
    </details>}
    <p className="implementation-review-scope-note">以上意见属于标示的已提交版本；工程师修订后须重新审核，不代表新版本、代码或实验已通过。</p>
  </div>;
}

export function AcceptanceProgress({ value }: { value: Json }) {
  const checks = Object.values(value?.coverage ?? {}) as Json[];
  const issues: Json[] = Array.isArray(value?.issues) ? value.issues : [];
  const blocking = issues.filter(i => ['P0', 'P1'].includes(i.severity) && ['PROPOSED', 'OPEN', 'PATCHED', 'VERIFIED'].includes(i.status));
  const labels: Record<string, string> = { CONTRACT: '冻结验收合同', AUTHOR: '编写文档', DISCOVER: '分工发现', ADJUDICATE: '总监裁决', VERIFY: '定向验证', REPAIR: '局部修复', APPROVED: '文档通过', APPROVED_WITH_NOTES: '文档通过，保留非阻断待办', BLOCKED: '阻断', NEEDS_PROBE: '需要有预算的验证', NEEDS_REPLAN: '需要重新规划' };
  return <div className="implementation-review-progress" aria-label="冻结合同验收进度">
    <p><strong>{labels[value.outcome ?? value.phase] ?? '准备验收'}</strong> · 合同版本 {value.contract?.version ?? '待冻结'} · 已审 {value.round_count ?? 0} 版</p>
    <p>必要验收项通过 {checks.filter(c => c.status === 'PASS').length} / {value.contract?.criteria?.length ?? 6} · 未解决阻断项 {blocking.length}</p>
    <p>实验工程师负责撰写和自检；总监、文献研究员按职责独立核验。作者声明修复后仍需验证关闭条件。</p>
    {value.budget_usage && <p>项目累计审核调用 {value.budget_usage.calls} / {value.budget_usage.max_calls} · 修复验证上限 {value.contract?.budget?.max_repair_verify_cycles ?? 2} 次</p>}
    <details><summary>验收合同与覆盖证据</summary><p>{value.contract?.goal}</p><p>{value.contract?.scope}</p>
      {(value.contract?.criteria ?? []).map((c: Json) => <p key={c.id}><strong>{c.id} {c.title}</strong> · {roleNames[c.reviewer]} · {value.coverage?.[c.id]?.status ?? '未检查'}<br />{c.requirement}<br />证据要求：{c.evidence_required}</p>)}
    </details>
    {issues.length > 0 && <details open={blocking.length > 0}><summary>问题台账（{issues.length}）</summary>
      {issues.map(issue => <article key={issue.issue_id}><p><strong>{issue.issue_id} · {issue.severity} · {issue.status}</strong> {issue.claim}</p>
        <p>影响：{issue.impact}</p><p>最小修复：{issue.minimal_fix}</p><p>关闭条件：{issue.close_when.join('；')}</p>
        <p>责任人：实验工程师 · 核验者：{roleNames[issue.verifier]}</p>
        <details><summary>原文证据与裁决</summary><pre>{textOf({ evidence: issue.evidence, adjudication: issue.adjudication, verification: issue.verification })}</pre></details>
      </article>)}
    </details>}
    <p>此处仅记录文档阶段验收，不代表代码已实现、实验已执行或科研结论成立。</p>
  </div>;
}

function AuthorSaveError({ authoring }: { authoring?: Json | null }) {
  if (!authoring?.last_tool_error && !authoring?.last_tool_validation && !authoring?.validation) return null;
  const validation = authoring.last_tool_validation ?? authoring.validation;
  return <div className="implementation-save-error" aria-label="文档保存具体错误">
    {authoring.last_tool_error && <><h4>具体保存错误</h4><pre tabIndex={0}>{textOf(authoring.last_tool_error)}</pre></>}
    {validation && <details><summary>保存校验详情</summary><pre tabIndex={0}>{textOf(validation)}</pre></details>}
  </div>;
}

