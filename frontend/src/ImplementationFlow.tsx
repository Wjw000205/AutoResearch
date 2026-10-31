import { BookOpen, Check, CircleAlert, Code2, FileCheck2, LoaderCircle } from 'lucide-react';
import { useId, useState, type ReactNode } from 'react';
import { date, type Json } from './api';
import { IndexedDocument } from './IndexedDocument';
import { AcceptanceProgress } from './ImplementationProgress';
import { ActivityRows, PreviousAttempt } from './NativeCodeProgress';
import { CodeWorkspaceAccess } from './CodeWorkspaceAccess';

const rows = (value: unknown): Json[] => Array.isArray(value) ? value.filter(item => item && typeof item === 'object') : [];
const responseHash = (value: unknown) => typeof value === 'string' && /^[a-f0-9]{64}$/i.test(value);
const strings = (value: unknown): string[] => Array.isArray(value) ? value.filter(item => typeof item === 'string') : [];
const roles: Record<string, string> = { director: '研究总监', engineer: '实验工程师', scout: '文献研究员' };
const mappingNames: Record<string, string> = { matched: '已映射交付', complete: '已映射交付', mapped: '已映射交付', partial: '部分映射', missing: '尚无交付映射', planned: '待交付', mismatch: '映射待核对' };
const stateNames: Record<string, string> = {
  pending: '待审计', missing_record: '尚无意见', approved: '已审阅', passed: '已通过',
  changes_requested: '需要修改', changes_required: '需要修改', version_mismatch: '版本待核对',
  binding_mismatch: '版本待核对', conflicting_records: '意见记录冲突', running: '审计中',
  queued: '待接收', blocked: '等待处理', paused: '已暂停', failed: '失败', interrupted: '已中断',
};
const hasFindings = (review: Json) => ['issues', 'fixes', 'required_changes'].some(key => Array.isArray(review[key]) && review[key].length > 0);
const passed = (review: Json) => review.binding_valid === true && ['approved', 'passed'].includes(review.status) && !hasFindings(review);
const requested = (status: unknown) => ['changes_requested', 'changes_required'].includes(String(status));
const mismatch = (status: unknown) => ['version_mismatch', 'binding_mismatch', 'conflicting_records'].includes(String(status));

export function sidebarDetailMatches(progress: Json, response: Json | null | undefined, runId: string): boolean {
  if (!response || response.run_id !== runId || progress.run_id !== runId || response.phase !== progress.phase) return false;
  const expected = progress.workflow?.id;
  if (!expected || response.workflow?.id !== expected || response.details?.identity?.workflow_id !== expected) return false;
  if (progress.workflow?.document_sha256 && response.details?.identity?.document_sha256 !== progress.workflow.document_sha256) return false;
  return !['document_authoring', 'document_review'].includes(progress.phase)
    || response.details?.identity?.document_revision === progress.workflow?.revision;
}

/** All sections require the current three-role full-document review. */
export function sectionAuditStatus(section: Json, audit?: Json | null): string {
  if (!audit || audit.mode !== 'indexed_review' || !Number.isInteger(section.start)
    || !Number.isInteger(section.end) || section.end <= section.start) return 'missing_record';
  const value = boundIndexedReview(audit);
  if (!value) return 'version_mismatch';
  if (rows(value.roles).some(review => hasFindings(review) || requested(review.status))) return 'changes_requested';
  return indexedReviewPassed(audit) ? 'approved' : 'pending';
}

export function implementationSteps(progress: Json, detail?: Json | null) {
  const current = { protocol_review: 'preparing', completed: 'code_review' }[progress.phase as string] ?? progress.phase;
  const document = detail?.document;
  const delivery = detail?.code_delivery;
  const complete: Record<string, boolean> = {
    preparing: progress.protocol?.status === 'approved' || !!document?.document_id,
    document_authoring: document?.status === 'submitted',
    document_review: detail?.document_audit?.mode === 'acceptance_contract'
      ? detail.document_audit.binding_valid === true && ['APPROVED', 'APPROVED_WITH_NOTES'].includes(detail.document_audit.status)
        && detail.document_audit.run_id === progress.run_id && detail.document_audit.workflow_id === progress.workflow?.id
        && detail.document_audit.document_sha256 === document?.sha256 && detail.document_audit.spec_hash === document?.spec_hash
        && detail.document_audit.revision === document?.revision
      : detail?.document_audit?.mode === 'indexed_review' ? indexedReviewPassed(detail.document_audit)
      && detail.document_audit.run_id === progress.run_id && detail.document_audit.workflow_id === progress.workflow?.id
      && detail.document_audit.revision === document?.revision && detail.document_audit.document_id === document?.document_id
      && detail.document_audit.document_sha256 === document?.sha256 && detail.document_audit.spec_hash === document?.spec_hash
      : detail?.document_audit?.mode === 'definition_repair'
        && detail.document_audit.approved_recorded === true && detail.document_audit.status === 'approved'
        && detail.document_audit.run_id === progress.run_id && detail.document_audit.workflow_id === progress.workflow?.id
        && detail.document_audit.document_sha256 === document?.sha256 && detail.document_audit.spec_hash === document?.spec_hash
        && detail.document_audit.revision === document?.revision,
    code_generation: progress.code_generation_started === true && rows(delivery?.files).length > 0,
    acceptance: delivery?.acceptance?.binding_valid === true && delivery.acceptance.status === 'passed',
    code_review: delivery?.review_scope !== 'definition_followup'
      && delivery?.review_status === 'passed' && rows(delivery.reviews).length === 3
      && new Set(rows(delivery.reviews).map(review => review.role)).size === 3
      && rows(delivery.reviews).every(review => Object.hasOwn(roles, review.role) && passed(review)),
  };
  return [['preparing', '实现准备'], ['document_authoring', '撰写文档'], ['document_review', '文档审计'],
    ['code_generation', '代码实现'], ['acceptance', '本地验收'], ['code_review', '代码复审']].map(([id, label]) => ({
      id, label, status: complete[id] ? 'completed' : current === id
        ? ['blocked', 'paused', 'failed', 'cancelled'].includes(progress.run_status) ? 'blocked' : 'active' : 'pending',
    }));
}

function ReviewStatus({ review, active = true }: { review: Json; active?: boolean }) {
  const activity = review.handoff_status ?? review.status;
  const recordedStatus = ['approved', 'passed'].includes(review.status) && hasFindings(review) ? 'changes_requested'
    : ['pending', 'missing_record'].includes(review.status) && activity === 'running' ? 'running' : review.status;
  const status = !active && recordedStatus === 'running' && review.approve == null && !hasFindings(review) ? 'blocked' : recordedStatus;
  return <span className={`implementation-review-state state-${status}`}>
    {['approved', 'passed'].includes(status) ? <Check size={12} /> : requested(status) || mismatch(status) ? <CircleAlert size={12} />
      : status === 'running' ? <LoaderCircle size={12} className="spin" /> : <span className="implementation-status-dot" />}
    {roles[review.role] ?? review.role}：{stateNames[status] ?? '待处理'}
  </span>;
}

function Findings({ value }: { value: Json }) {
  const issues = strings(value.issues);
  const fixes = rows(value.fixes);
  return <div className="implementation-findings">
    {issues.map((issue, index) => <article key={index}><strong>问题 {index + 1}</strong><p>{issue}</p>
      {fixes.filter(fix => fix.issue_index === index).map((fix, n) => <Fix key={n} value={fix} />)}</article>)}
    {fixes.filter(fix => !Number.isInteger(fix.issue_index) || fix.issue_index < 0 || fix.issue_index >= issues.length)
      .map((fix, index) => <article key={`unlinked-${index}`}><strong>补充修改建议</strong><Fix value={fix} /></article>)}
    {strings(value.required_changes).map((change, index) => <p key={`change-${index}`}>{change}</p>)}
    {value.error && <p className="inline-warning">{value.error}</p>}
  </div>;
}

function Fix({ value }: { value: Json }) {
  return <dl className="implementation-fix-fields">{[['location', '修改位置'], ['change', '修改办法'], ['verification', '验证要求']]
    .filter(([key]) => typeof value[key] === 'string' && value[key]).map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{value[key]}</dd></div>)}</dl>;
}

function DocumentProgress({ document }: { document: Json }) {
  const sections = rows(document.sections);
  const authoring = document.authoring;
  const latestRead = rows(authoring?.recent_reads)[0];
  const saved = document.document_id && ['draft', 'submitted'].includes(document.status);
  return <section className="implementation-flow-card" aria-label="文档撰写与章节进度">
    <header><h4><BookOpen size={17} />文档撰写与章节</h4><span className="implementation-pill">版本 {Number(document.revision ?? 0) + 1} · {saved ? document.status === 'submitted' ? '已提交审计' : '草稿已保存' : '待保存'}</span></header>
    {saved ? <p>已保存 {sections.length} 个章节。目录记录保存情况，当前版本三方全文意见在审核记录中显示。</p>
      : <><p>{document.status === 'save_unconfirmed' ? '已记录草稿目录，保存回执仍待核对。' : '当前稿尚未保存。工程师完成后会一次保存整份实现文档。'}</p>
        {authoring && <p>已记录 {authoring.tool_turns ?? 0} 轮工具交互 · {authoring.request_status === 'dispatching' ? '正在等待工程师回复' : '沿用已保存的读取与工具回执'}</p>}
        {latestRead && <p>最近实际读取：<strong>{latestRead.title ?? latestRead.section_id}</strong> · 字符 {latestRead.char_start}–{latestRead.char_end}{latestRead.predecessor_handoff_id ? '（继承记录）' : ''}</p>}
        <p className="muted">尚无逐章节写入回执，因此不显示推测的写作百分比。</p></>}
    {sections.length > 0 && <details>
      <summary>章节目录 · {sections.length} 项</summary><ol className="implementation-section-list">{sections.map((section, index) =>
        <li key={section.id}><span className="implementation-section-number">{String(index + 1).padStart(2, '0')}</span><div><strong>{section.title}</strong><small>{section.chars ?? section.end - section.start} 字符 · {saved ? '已保存' : '保存待核对'}</small></div></li>
      )}</ol></details>}
    {document.previous_document && !saved && <details><summary>上一版文档参考 · 版本 {Number(document.previous_document.prior_revision) + 1}</summary><p className="muted">这是历史版本目录，当前稿仍待保存。</p><ol>{rows(document.previous_document.sections).map(section => <li key={section.id}>{section.title}</li>)}</ol></details>}
  </section>;
}

export function boundIndexedReview(audit: Json) {
  const value = audit.indexed_review, counts = value?.counts;
  if (audit.mode !== 'indexed_review' || value?.version !== 1 || value.binding_valid !== true
    || value.scientific_approval !== false || !Number.isInteger(audit.revision) || audit.revision < 0
    || ['run_id', 'workflow_id', 'document_id', 'document_sha256', 'spec_hash'].some(key =>
      typeof audit[key] !== 'string' || !audit[key] || value[key] !== audit[key])
    || value.revision !== audit.revision || counts?.expected_reviews !== 3
    || ![counts.saved_reviews, counts.approved_reviews, counts.changes_required_reviews]
      .every(n => Number.isInteger(n) && n >= 0 && n <= 3)
    || counts.approved_reviews + counts.changes_required_reviews > counts.saved_reviews
    || !Array.isArray(value.roles) || value.roles.length !== 3
    || new Set(value.roles.map((role: Json) => role?.role)).size !== 3
    || value.roles.some((role: Json) => !role || !Object.hasOwn(roles, role.role))) return null;
  return value;
}

function indexedReviewPassed(audit: Json) {
  const value = boundIndexedReview(audit);
  return !!value && ['approved', 'passed'].includes(audit.status) && value.counts.approved_reviews === 3
    && value.roles.every((review: Json) => review.reading_complete === true && review.approve === true && !hasFindings(review));
}

function IndexedReviewProgress({ audit, active, runId }: { audit: Json; active: boolean; runId: string }) {
  const value = boundIndexedReview(audit);
  const reviews = value ? rows(value.roles) : [];
  const counts = value?.counts ?? {};
  const rawDocuments = audit.response_documents;
  const entries: Json[] = Array.isArray(rawDocuments) ? rows(rawDocuments) : rawDocuments && typeof rawDocuments === 'object'
    ? Object.entries(rawDocuments).map(([role, item]): Json => ({ role, ...((item as Json)?.document ? item as Json : { document: item }) })) : [];
  const documents: Json[] = entries.map(item => ({ ...item, role: item.response_role ?? item.role,
    document: item.document ?? item }));
  const responses = value ? documents.filter(item => Object.hasOwn(roles, item.role) && item.binding_valid !== false
    && typeof item.document?.document_id === 'string' && item.document.document_id
    && responseHash(item.document.sha256) && Array.isArray(item.document.sections)
    && (item.document.run_id == null || item.document.run_id === runId)
    && (item.document.response_role ?? item.response_role ?? item.role) === item.role
    && (item.document.target_spec_hash ?? item.target_spec_hash) === audit.spec_hash
    && (item.document.target_document_sha256 ?? item.target_document_sha256) === audit.document_sha256
    && documents.filter(other => other.role === item.role).length === 1) : [];
  return <section className="implementation-flow-card" aria-label="实现文档索引全文审核">
    <header><h4><FileCheck2 size={17} />三方索引全文审核</h4><span className={`implementation-pill state-${audit.status}`}>{stateNames[audit.status] ?? '待审计'}</span></header>
    <p>三方分别从文档目录通过工具阅读全文，核对已批准架构与当前实现。每轮收齐三份完整意见后，工程师统一修改并为每位审查者保存独立回复文档；修订版与回复通过目录交给三方复审。</p>
    <div className="implementation-counts"><span><strong>{counts.saved_reviews ?? 0} / 3</strong>全文审核意见已保存</span><span><strong>{counts.approved_reviews ?? 0}</strong>同意</span><span><strong>{counts.changes_required_reviews ?? 0}</strong>需要修改</span></div>
    {!value && <p className="inline-warning">索引全文意见与当前文档版本的绑定待核对。</p>}
    <div className="implementation-reviewers">{reviews.map(review => <ReviewStatus key={review.role} review={review} active={active} />)}</div>
    {reviews.map(review => <details key={review.role}><summary>{roles[review.role]} · {stateNames[review.status] ?? '待审计'}</summary>
      <p>{review.reading_complete === true ? '全文工具读取已完成。' : review.reading_complete === false ? '全文工具读取尚未完成。' : '全文读取状态尚未报告。'}</p>
      <p>{review.rationale ?? '尚无已保存全文意见。'}</p><Findings value={review} />
    </details>)}
    {responses.length > 0 && <div aria-label="工程师独立回复文档">{responses.map(item => <details key={`${item.role}:${item.document.document_id}:${item.document.sha256}`}>
      <summary>工程师给{roles[item.role]}的回复文档</summary>
      <IndexedDocument runId={runId} document={item.document} label={`给${roles[item.role]}的回复`} />
    </details>)}</div>}
    <p className="muted">阅读记录和审核票分别保存；同一版本三方均读完且一致同意，并通过原有执行门禁后，才派发代码。</p>
  </section>;
}

function FunctionProgress({ functions, delivery }: { functions: Json[]; delivery?: Json | null }) {
  return <section className="implementation-flow-card" aria-label="功能与验收项进度">
    <header><h4><Code2 size={17} />功能与验收项</h4><span className="implementation-pill">{functions.length} 项要求</span></header>
    <p>每项展示计划位置、实际交付映射和验证说明。{delivery?.review_scope === 'definition_followup'
      ? `本轮仅复审补充定义及相关 ${strings(delivery.review_requirement_ids).join('、')} 问题；其他验收项不在本轮范围。`
      : '代码审查针对整体交付；逐功能独立验证结果尚未单独记录。'}</p>
    {delivery && <p>{delivery.review_scope === 'definition_followup' ? '补充定义代码复审' : '整体交付代码复审'}：<strong>{stateNames[delivery.review_status] ?? '尚未开始'}</strong> · 本地验收：<strong>{stateNames[delivery.acceptance?.status] ?? '待验收'}</strong></p>}
    {functions.length ? <div className="implementation-function-list">{functions.map((item, index) => <details key={`${item.id}:${index}`}>
      <summary><span><code>{item.id}</code><strong>{item.requirement}</strong></span><span className={`implementation-pill mapping-${item.mapping_status}`}>{mappingNames[item.mapping_status] ?? '待核对映射'}</span></summary>
      <dl className="implementation-fix-fields"><div><dt>计划实现位置</dt><dd>{strings(item.planned_locations).length ? strings(item.planned_locations).map((location, n) => <code key={n}>{location}</code>) : '尚无位置计划'}</dd></div>
        <div><dt>实际交付位置</dt><dd>{strings(item.declared_locations).length ? strings(item.declared_locations).map((location, n) => <code key={n}>{location}</code>) : '尚无实际交付映射'}</dd></div>
        <div><dt>要求的验证</dt><dd>{item.verification ?? item.verification_requirement ?? '未记录'}</dd></div>
        {item.declared_verification && <div><dt>交付方验证说明</dt><dd>{item.declared_verification}</dd></div>}
        <div><dt>逐功能独立验证</dt><dd>尚未单独记录</dd></div></dl>
    </details>)}</div> : <p>实现文档保存后，这里展示具体功能与验收要求。</p>}
  </section>;
}

function CurrentIssues({ issues }: { issues: string[] }) {
  const [requestedPage, setPage] = useState(0);
  const listId = useId();
  const pageSize = 4;
  const pageCount = Math.max(1, Math.ceil(issues.length / pageSize));
  const page = Math.min(requestedPage, pageCount - 1);
  const start = page * pageSize;
  return <section className="implementation-current-issues" aria-label="本次验收发现的问题">
    <div className="implementation-issues-heading"><strong>本次验收发现的问题</strong><span>{issues.length} 项</span></div>
    <p className="implementation-issues-hint">点击问题展开全文{pageCount > 1 ? ` · 每页 ${pageSize} 项` : ''}</p>
    <ol id={listId} className="implementation-issue-list" start={start + 1}>
      {issues.slice(start, start + pageSize).map((issue, index) => <li key={`${page}:${issue}`}>
        <details className="implementation-issue" name={listId}>
          <summary><span className="implementation-issue-number">{String(start + index + 1).padStart(2, '0')}</span>
            <span className="implementation-issue-preview">{issue}</span>
            <span className="implementation-issue-toggle" aria-hidden="true"><span>展开</span><span>收起</span></span>
          </summary>
          <p className="implementation-issue-body">{issue}</p>
        </details>
      </li>)}
    </ol>
    {pageCount > 1 && <nav className="implementation-issues-pagination" aria-label="验收问题分页">
      <span role="status">{start + 1}–{Math.min(start + pageSize, issues.length)} / {issues.length} 项 · {page + 1}/{pageCount} 页</span>
      <div><button type="button" className="btn small" aria-controls={listId} aria-label="上一页验收问题" disabled={page === 0} onClick={() => setPage(page - 1)}>上一页</button>
        <button type="button" className="btn small" aria-controls={listId} aria-label="下一页验收问题" disabled={page === pageCount - 1} onClick={() => setPage(page + 1)}>下一页</button></div>
    </nav>}
  </section>;
}

function CodeProgress({ delivery, started, phase, active, runId, workflowId }: { delivery?: Json | null; started: boolean; phase: string; active: boolean; runId: string; workflowId?: string }) {
  const reports = rows(delivery?.public_messages), activities = rows(delivery?.activities), reviews = rows(delivery?.reviews);
  const latest = reports.at(-1);
  const count = delivery?.snapshot_file_count ?? rows(delivery?.files).length;
  const selection = delivery?.delivery_selection;
  const sourceDelivery = selection?.scope === 'source_and_test_results';
  const host = ['acceptance', 'code_review'].includes(phase) ? delivery?.execution_progress : null;
  const checks = rows(delivery?.acceptance?.smoke).length ? rows(delivery?.acceptance?.smoke) : rows(host?.checks);
  const issues = [...new Set(strings(delivery?.acceptance?.issues).concat(strings(delivery?.issues)))];
  const accepted = delivery?.acceptance?.binding_valid === true && delivery?.acceptance?.status === 'passed';
  const reviewCount = Object.keys(roles).filter(role => {
    const votes = reviews.filter(review => review.role === role);
    return votes.length === 1 && passed(votes[0]);
  }).length;
  const currentAction = !active && phase !== 'completed' ? '代码流程已停止，以下为最近执行记录' : host?.label ?? ({ acceptance: '正在核对代码与运行结果', code_review: '正在进行三方独立审核',
    completed: '代码交付流程已完成' }[phase] ?? (delivery?.checkpoint_status === 'completed' ? '代码已返回，等待交付核验' : '正在编写与验证代码'));
  return <section className="implementation-flow-card implementation-execution" aria-label="实际代码交付进度">
    <header><h4><Code2 size={17} />代码实现与验收</h4><span className="implementation-pill">{started ? delivery ? `第 ${Number(delivery.index ?? 0) + 1} 次实现` : '已派发实现' : '待文档审计通过'}</span></header>
    {!delivery ? <p>{started ? '实现请求已派发，正在同步实际活动和交付记录。' : '代码生成尚未派发；实现文档同版本审计通过后开始。'}</p> : <>
      <div className="implementation-current-work"><div className="implementation-current-label"><span>当前正在做什么</span>
        <time>{date(host?.updated_at ?? delivery.last_activity_at, true)}</time></div>
        <h3>{currentAction}</h3><p>{latest?.text ?? '等待执行端上报具体动作，暂不推测已完成的工作。'}</p>
        {host?.total > 0 && <small>已完成 {host.completed ?? 0} / {host.total} 个验收分支{host.timeout_seconds ? ` · 当前分支时限 ${host.timeout_seconds} 秒` : ''}</small>}
        <small>来自实际执行回执；开发测试与作者说明不代表独立审核通过。</small>
      </div>
      <div className="implementation-execution-stats" aria-label="交付与验收概况">
        <div><span>本次代码交付</span><strong>{count ? sourceDelivery ? `${count} 项交付文件` : '旧版完整目录' : '待提交'}</strong><small>{count ? sourceDelivery ? `${selection.runtime_file_count} 项运行源码与配置 · ${selection.test_result_count} 份测试结果` : '历史快照包含开发运行产物，文件数不等于源码数' : '当前仍在编写和检查'}</small></div>
        <div><span>本地验收</span><strong>{accepted ? '已通过' : delivery.acceptance?.present ? '需要修复' : phase === 'acceptance' ? '正在检查' : '待运行'}</strong><small>{phase === 'acceptance' ? host?.label ?? '核对交付与输出契约' : '固定分支与输出检查'}</small></div>
        <div><span>独立代码审核</span><strong>{reviewCount} / 3</strong><small>同一交付版本的通过票</small></div>
      </div>
      <CodeWorkspaceAccess key={`${runId}:${workflowId}`} runId={runId} workflowId={workflowId} />
      <div className="implementation-execution-grid">
        <div className="implementation-execution-log"><h5>最近实际执行 <span>{activities.length} 条记录</span></h5>
          {activities.length ? <ActivityRows activities={activities.slice(-3)} /> : <p className="muted">等待命令或文件变更记录。</p>}
          {activities.length > 3 && <details><summary><span>更早的执行记录 · {activities.length - 3} 条</span></summary><ActivityRows activities={activities.slice(0, -3)} /></details>}
          {reports.length > 1 && <details><summary><span>之前的进展说明</span></summary>{reports.slice(0, -1).reverse().map((message, n) => <p key={message.id ?? n}>{message.text}</p>)}</details>}
        </div>
        <div className="implementation-execution-verification"><h5>验收与修复</h5>
          {issues.length > 0 && <CurrentIssues key={`${runId}:${workflowId}:${delivery.index}:${delivery.code_sha256 ?? ''}:${delivery.delivery_hash ?? ''}`} issues={issues} />}
          {!accepted && <PreviousAttempt attempt={delivery.previous_attempt} />}
          {checks.length > 0 ? <ul className="implementation-branch-results">{checks.map((check, n) => <li key={check.id ?? n}>
            <span>{{ baseline: '基线分支', proposed: '候选方法', ablation: '消融分支' }[check.variant as string] ?? `验收分支 ${n + 1}`}</span>
            <strong className={check.passed ? 'is-passed' : 'is-failed'}>{check.passed === true || check.passed === 1 ? '通过' : '未通过'}</strong>
            {typeof check.duration_seconds === 'number' && <small>{Math.round(check.duration_seconds)} 秒</small>}</li>)}</ul>
            : <p className="muted">{phase === 'acceptance' ? '本地检查结果将逐项同步到这里。' : '本次交付完成后，开始固定分支验收。'}</p>}
          <h5>三方实际代码复审</h5><div className="implementation-reviewers">{reviews.map((review, index) => <ReviewStatus key={`${review.role}:${index}`} review={review} active={active} />)}</div>
          {!reviews.some(review => review.rationale || hasFindings(review)) && <p className="muted">尚无独立审核结论。验收通过后，三个角色分别核对实际代码。</p>}
          {reviews.filter(review => review.rationale || hasFindings(review)).map((review, index) => <details key={`${review.role}:${index}`}><summary><span>{roles[review.role] ?? review.role} · 代码复审意见</span></summary>
            <p>{review.rationale}</p><Findings value={review} /></details>)}
        </div>
      </div>
      {rows(delivery.files).length > 0 && <details><summary><span>{sourceDelivery ? '源码与测试交付清单' : '历史完整快照（含运行产物）'} · {count} 个文件</span></summary><div className="implementation-delivery-files">{rows(delivery.files).map((file, n) => <p key={n}><code>{file.path}</code><span>{file.bytes ?? '未记录'} 字节</span></p>)}</div></details>}
    </>}
  </section>;
}

function InitializationProgress({ value }: { value: Json }) {
  const entries = rows(value.roles);
  if (!entries.length) return null;
  const categories: Record<string, string> = { checking: '正在初始化', ready: '本机账户已就绪', account_read_timeout: '账户读取超时',
    internal_error_unclassified: '账户读取内部错误', authentication_required: '本机登录需要恢复', request_rejected: '账户读取请求被拒绝', rpc_error: '初始化通信错误',
    initialize_timeout: '初始化握手超时', initialize_internal_error_unclassified: '初始化握手内部错误',
    initialization_transport_closed: '初始化连接已关闭', initialization_transport_eof: '初始化连接流已结束',
    initialization_read_timeout: '初始化通信读取超时' };
  const stages: Record<string, string> = { initialize: '通信握手', account_read: '读取本机账户', authenticated: '账户检查完成',
    reconnect: '重新建立初始化连接' };
  const actions: Record<string, string> = { retry_account_read: '正在重试账户读取', check_login: '需恢复本机 Codex 登录',
    retry_exhausted: '自动重试额度已用尽，等待处理', inspect_initialization: '需核对初始化原因', read_original_turn: '核对原任务回执', continue: '继续当前流程',
    retry_initialization_connection: '下一步重试初始化连接' };
  const problem = entries.some(entry => entry.diagnostic && !['checking', 'ready'].includes(entry.diagnostic.category));
  return <section className="implementation-flow-card" aria-label="本机 Codex 初始化与重试">
    <header><h4><LoaderCircle size={17} />本机 Codex 初始化</h4><span className="implementation-pill">当前版本请求</span></header>
    <details open={problem}><summary>查看初始化状态与重试记录</summary>
    <p className="muted">按已保存诊断恢复初始化连接或账户读取，两类重试额度分别累计。重新连接不清零账户读取重试记录。登录拒绝会停下；已派发的模型任务按原回执恢复。</p>
    <div className="implementation-initialization-list">{entries.map((entry, n) => {
      const diagnostic = entry.diagnostic;
      return <article key={entry.role ?? n}><strong>{roles[entry.role] ?? entry.role}</strong>{diagnostic ? <>
        <span>{diagnostic.category === 'ready' && diagnostic.stage === 'initialize' ? '通信握手已完成' : categories[diagnostic.category] ?? '诊断待核对'}</span>
        <small>{stages[diagnostic.stage] ?? '初始化阶段待核对'} · {diagnostic.stage === 'reconnect' ? '重连' : '重试'} {diagnostic.retry_count} / {diagnostic.retry_limit}</small>
        <p>{diagnostic.stage === 'reconnect' && diagnostic.next_action === 'retry_exhausted' ? '初始化连接重试额度已用尽，等待处理' : actions[diagnostic.next_action] ?? '等待处理'}</p>
      </> : <small>此请求尚无已保存的初始化诊断</small>}</article>;
    })}</div></details>
  </section>;
}

function FlowDisclosure({ label, meta, children }: { label: string; meta?: string; children: ReactNode }) {
  return <details className="implementation-fold"><summary><span>{label}</span>{meta && <small>{meta}</small>}</summary>{children}</details>;
}

export function ImplementationFlow({ progress, detail, runId }: { progress: Json; detail?: Json | null; runId: string }) {
  if (progress.run_id !== runId) return null;
  const document = detail?.document;
  const originalAudit = detail?.document_audit?.mode === 'indexed_review' ? detail.document_audit : null;
  const audit = originalAudit && (originalAudit.run_id !== runId
    || originalAudit.workflow_id !== progress.workflow?.id || originalAudit.revision !== document?.revision
    || originalAudit.document_id !== document?.document_id || originalAudit.document_sha256 !== document?.sha256
    || originalAudit.spec_hash !== document?.spec_hash)
    ? { ...originalAudit, status: 'version_mismatch',
        indexed_review: { ...originalAudit.indexed_review, binding_valid: false, status: 'version_mismatch' } } : originalAudit;
  const authoring = progress.phase === 'document_authoring';
  const reviewing = progress.phase === 'document_review';
  const coding = ['code_generation', 'acceptance', 'code_review', 'completed'].includes(progress.phase);
  const documentView = document && <DocumentProgress document={document} />;
  const acceptance = progress.workflow?.review_mode === 'acceptance_contract' ? progress.workflow.acceptance : null;
  const definition = detail?.document_audit?.mode === 'definition_repair' && detail.document_audit.run_id === runId
    && detail.document_audit.workflow_id === progress.workflow?.id && detail.document_audit.document_sha256 === document?.sha256
    && detail.document_audit.spec_hash === document?.spec_hash ? detail.document_audit : null;
  const auditView = definition ? <section aria-label="补充定义审核">
    <h3>补充定义审核</h3><p>原批准内容保留；本轮只核对资源估算补充及其批准依据。未通过的具体意见交回作者，三方通过后交给 Codex 修改。</p>
    {rows(definition.roles).map(review => <div key={review.role}>
      <ReviewStatus review={review} active={progress.run_status === 'running' && reviewing} />
      {strings(review.issues).map((issue, index) => <p key={index}>{issue}</p>)}
    </div>)}
  </section> : acceptance ? <AcceptanceProgress value={acceptance} /> : audit && <IndexedReviewProgress audit={audit} runId={runId} active={progress.run_status === 'running' && reviewing} />;
  const codeView = <CodeProgress delivery={detail?.code_delivery} started={progress.code_generation_started === true} phase={progress.phase} active={progress.run_status === 'running'} runId={runId} workflowId={progress.workflow?.id} />;
  const cards = [
    { id: 'document', priority: authoring ? 0 : 3, view: documentView && (authoring ? documentView : <FlowDisclosure label="文档撰写与章节" meta={`章节目录 · ${rows(document.sections).length} 项`}>{documentView}</FlowDisclosure>) },
    { id: 'audit', priority: reviewing ? 0 : 4, view: auditView && (reviewing ? auditView : <FlowDisclosure label="实现文档审核" meta={acceptance?.outcome === 'APPROVED' ? '文档已通过' : acceptance?.outcome ?? stateNames[audit?.status] ?? '待审计'}>{auditView}</FlowDisclosure>) },
    { id: 'code', priority: coding ? 0 : 5, view: coding || progress.code_generation_started === true ? codeView
      : <FlowDisclosure label="代码实现与验收" meta="待文档审核通过">{codeView}</FlowDisclosure> },
  ].sort((a, b) => a.priority - b.priority);
  return <div className="implementation-flow" aria-label="代码实现流程明细">
    <ol className="implementation-step-list" aria-label="代码实现步骤">{implementationSteps(progress, detail).map((step, index) => <li key={step.id} className={`step-${step.status}`} aria-current={step.status === 'active' || step.status === 'blocked' ? 'step' : undefined}>
      <span>{step.status === 'completed' ? <Check size={14} /> : index + 1}</span><div><strong>{step.label}</strong><small>{step.status === 'completed' ? '已有完成记录' : step.status === 'active' ? '当前阶段' : step.status === 'blocked' ? '等待处理' : '待推进'}</small></div></li>)}</ol>
    {!detail && <p className="muted" role="status">正在同步文档、审核意见和交付记录…</p>}
    {cards.map(card => card.view && <div key={card.id} className="implementation-flow-section">{card.view}</div>)}
    {detail?.initialization && <FlowDisclosure label="连接与初始化记录" meta="技术诊断"><InitializationProgress value={detail.initialization} /></FlowDisclosure>}
    {detail && <FlowDisclosure label="功能与验收项" meta={`${rows(detail.functions).length} 项要求`}><FunctionProgress functions={rows(detail.functions)} delivery={detail.code_delivery} /></FlowDisclosure>}
  </div>;
}
