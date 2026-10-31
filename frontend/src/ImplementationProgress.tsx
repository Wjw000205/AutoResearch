import { date, textOf, type Json } from './api';

const statuses: Record<string, string> = { running: '进行中', paused: '已暂停', blocked: '等待处理', failed: '执行失败', cancelled: '已取消', completed: '已完成' };
const roleNames: Record<string, string> = { director: '研究总监', engineer: '实验工程师', scout: '文献研究员' };

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

export function ImplementationProgress({ progress, runId }: { progress?: Json | null; runId: string }) {
  if (!progress || progress.run_id !== runId) return null;
  const workflow = progress.workflow;
  if (progress.phase === 'document_review' && !['indexed_review', 'acceptance_contract'].includes(workflow?.review_mode)) return null;
  const authoring = progress.phase === 'document_authoring' ? progress.authoring : null;
  const indexedMode = progress.phase === 'document_review' && workflow?.review_mode === 'indexed_review';
  const whole = indexedReviewSummary(progress);
  return <section className="panel implementation-progress" aria-label="代码实现进度">
    <header><h3>代码实现进度</h3><span>{statuses[progress.run_status] ?? '状态未报告'}</span></header>
    <h4>{progress.label}</h4><p>{progress.message}</p>
    {workflow?.review_mode === 'acceptance_contract' && <AcceptanceProgress value={workflow.acceptance} />}
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
    <p><strong>代码生成：</strong>{progress.code_generation_started ? '已派发，实际文件与验收记录见“代码实现与验收”。' : '尚未派发。'}</p>
    {workflow && <p>实现尝试 {workflow.attempt_count} 次 · 当前交付文件 {workflow.file_count} 个</p>}
    {progress.block_reason && <p className="inline-warning">{progress.block_reason}</p>}
    <p><strong>下一步：</strong>{progress.next_action}</p>
    <small>最近保存：{date(progress.updated_at, true)} · 进度随研究状态自动更新</small>
  </section>;
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

