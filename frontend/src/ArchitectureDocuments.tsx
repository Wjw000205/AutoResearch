import { FileText } from 'lucide-react';
import { type ReactNode, useState } from 'react';
import { type Json } from './api';
import { IndexedDocument } from './IndexedDocument';
import { ReferenceNames, ReferenceText } from './ReferenceNames';
import { ArchitectureDelivery, architectureDeliveryLabel, architectureReviewDeliveryLabel } from './ArchitectureDelivery';
import { ArchitectureDiagrams } from './ArchitectureDiagrams';
import { architectureDocumentVersions } from './documentVersions';

const roles: Record<string, string> = { director: '研究总监', scout: '文献研究员' };
const states: Record<string, string> = { pending: '等待分段评议', reviewing: '正在评议文档', approved: '评议通过', rejected: '需要修改' };
const revisionStates: Record<string, string> = { prepared: '已安排架构修订', drafting: '正在编写修订架构', reviewing: '新版本正在独立评议', approved: '修订架构已通过', blocked: '修订需处理', needs_consensus: '作者要求科学方案重审' };
const definitionStates: Record<string, string> = { prepared: '已安排具体定义补充', drafting: '正在编写具体定义', reviewing: '正在独立审议具体定义', approved: '具体定义取得三方意见', rejected: '具体定义仍须修改', blocked: '具体定义仍有未决项' };

function ArchitectureHistory({ historical, children }: { historical: boolean; children: ReactNode }) {
  return historical ? <details className="architecture-review-history"><summary>历史架构评议与作者交付</summary><p className="muted">这里保留定义补齐之前的架构意见与交付。当前处理进度见上方；这些历史记录不表示任务此刻已停止。</p>{children}</details> : <>{children}</>;
}

function ReviewAdvice({ review }: { review: Json }) {
  return <>{review.disposition === 'needs_evidence' && Array.isArray(review.evidence_requests) && review.evidence_requests.length > 0 && <div aria-label="评审请求的证据来源"><strong>待核对的证据来源范围</strong><p>以下是评审实际请求的来源定位，须与具体问题和修改意见核对；不代表来源充分或科学结论已通过。</p><ol>{review.evidence_requests.map((anchor: Json, i: number) => <li key={i}><p>文档：<code>{String(anchor.document_id ?? '')}</code></p><p>章节：<code>{String(anchor.section_id ?? '')}</code> · 字符范围：{String(anchor.char_start ?? '')}–{String(anchor.char_end ?? '')}</p><p>原文 SHA-256：<code>{String(anchor.expected_sha256 ?? '')}</code></p></li>)}</ol></div>}{review.fixes?.length > 0 && <div aria-label="完整修改方案">{review.fixes.map((fix: Json, i: number) =>
    <div key={i}><strong>修改项 {fix.issue_index + 1} · {fix.target}</strong><p>影响：<ReferenceText value={fix.reason} /></p>
      <p>具体修改：<ReferenceText value={fix.proposed_change} /></p><p>验收方法：<ReferenceText value={fix.verification_plan} /></p></div>)}</div>}
    {review.suggestions?.length > 0 && <details><summary>非阻塞建议 · {review.suggestions.length} 项</summary>
      <ul>{review.suggestions.map((text: string, i: number) => <li key={i}><ReferenceText value={text} /></li>)}</ul></details>}</>;
}

function HistoricalArchitectureDiagrams({ runId, rows, currentId }: { runId: string; rows?: Json[]; currentId?: string }) {
  const versions = (rows ?? []).filter(row => row.id !== currentId && row.status === 'approved' && row.document?.sha256)
    .sort((a, b) => (b.cycle ?? 0) - (a.cycle ?? 0) || String(b.created_at ?? '').localeCompare(String(a.created_at ?? '')));
  const [selectedId, setSelectedId] = useState(''), [open, setOpen] = useState(!currentId);
  const selected = versions.find(row => row.id === selectedId) ?? versions[0];
  if (!selected) return null;
  return <details className="historical-architecture-diagrams" open={open} onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>原架构图 · 第 {selected.cycle ?? '?'} 轮</summary>
    {open && <><p className="muted">展示原文档版本保存的网络图与流程图，本轮数据适配进度见当前阶段。</p>
      {versions.length > 1 && <label>架构版本 <select value={selected.id} onChange={event => setSelectedId(event.target.value)}>{versions.map(row => <option key={row.id} value={row.id}>第 {row.cycle ?? '?'} 轮 · {row.document.title?.split(/[\\/]/).pop() || '架构设计文档'} · {row.document.sha256.slice(0, 12)}</option>)}</select></label>}
      <ArchitectureDiagrams key={`${runId}:${selected.document.document_id}:${selected.document.sha256}`} runId={runId} document={selected.document} demo={selected.provenance === 'demo'} historical />
    </>}
  </details>;
}

function SavedArchitectures({ runId, rows, currentId }: { runId: string; rows?: Json[]; currentId?: string }) {
  const history = (rows ?? []).filter(row => row.id !== currentId && row.document);
  if (!history.length) return null;
  return <section aria-label="已保存架构版本"><h4>已保存架构版本 · {history.length}</h4>
    <p className="muted">原文档和独立评议保留；历史通过记录不代表本轮适配已经通过。</p>
    {history.map(row => <details key={row.id}><summary>第 {row.cycle ?? '?'} 轮 · {row.document.title?.split(/[\\/]/).pop() || '架构设计文档'} · {states[row.status] ?? row.status}</summary>
      <IndexedDocument runId={runId} document={row.document} label="历史架构文档" />
      {(row.effective_reviews ?? row.reviews ?? []).map((review: Json, i: number) => <details key={`${review.role}:${i}`}><summary>{roles[review.role] ?? review.role} · 原版本独立评议</summary><p><ReferenceText value={review.rationale} /></p><ReviewAdvice review={review} /></details>)}
    </details>)}</section>;
}

export function ArchitectureDocuments({ runId, value, datasetAdaptation = false, experimentConfiguration = false }: { runId: string; value?: Json | null; datasetAdaptation?: boolean; experimentConfiguration?: boolean }) {
  const current = value?.current;
  const doc = current?.document;
  const reading = value?.revision?.reading;
  const response = value?.revision?.author_response;
  const definitions = value?.revision?.definition_review;
  const unresolvedIssueCount = Math.max(definitions?.unresolved_issue_count ?? 0,
    definitions?.issue_documents?.filter((unit: Json) => unit.status === 'unresolved').length ?? 0);
  const hasDefinitionProblems = unresolvedIssueCount > 0 || (definitions?.scope_issues?.length ?? 0) > 0;
  const definitionPhase = Boolean(definitions && definitions.status !== 'approved');
  const convergence = definitionPhase ? definitions?.completion?.convergence : value?.revision?.convergence;
  const issues: Json[] = convergence?.issue_ledger ?? [];
  if (!current) return value?.required || value?.history?.length ? <section className="architecture-documents panel"><h3><FileText size={17} />架构文档与评议</h3><p>{experimentConfiguration ? '本轮沿用原架构。数据集切换与接口检查在实验验证阶段完成，原架构文档、图示及评议保留如下。' : datasetAdaptation ? '本轮复用已有架构，正在适配真实数据与实验协议。新增文档尚未完成，原架构可在下方查看。' : '本轮设计文档尚未完成；已有架构版本保留在下方。'}</p><HistoricalArchitectureDiagrams runId={runId} rows={value?.history} /><SavedArchitectures runId={runId} rows={value?.history} /></section> : null;
  const revisionDetails = value?.revision && <div className="inline-info"><strong>{revisionStates[value.revision.status] ?? '架构修订状态待核对'} · 已保存 {value.revision.revision_count ?? 0} 个新版本</strong><p><ReferenceText value={value.revision.status === 'approved' ? '修订架构已完成独立评议，后续状态请查看代码实现与实验阶段。' : '根据具体独立意见生成新 Markdown，再按索引评议。旧文档、否决意见和冻结计划保留；尚未启动代码或实验。'} /></p>{value.revision.reason && <p><ReferenceText value={value.revision.reason} /></p>}</div>;
  const headingState = architectureDeliveryLabel(value?.revision) ?? (!definitionPhase ? architectureReviewDeliveryLabel(current) : undefined) ?? (definitionPhase ? definitionStates[definitions.status] ?? '科学定义状态待核对'
    : value?.revision && value.revision.status !== 'needs_consensus' ? revisionStates[value.revision.status] ?? '架构修订状态待核对'
      : states[current.status] ?? '等待评议');
  return <ReferenceNames records={value}><section className="architecture-documents panel" aria-label="架构文档与评议"><h3><FileText size={17} />架构文档与评议 · <ReferenceText value={headingState} /></h3>
    <ArchitectureDiagrams key={`${runId}:${doc.document_id}:${doc.sha256}`} runId={runId} document={doc} demo={current.provenance === 'demo'} approved={current.status === 'approved'} />
    <ArchitectureDelivery revision={value?.revision} />
    {issues.length > 0 && <details aria-label="跨版本问题记录"><summary>问题记录 · 待闭合 {convergence.open_issue_count} 项 · 已闭合 {convergence.closed_issue_count} 项</summary>
      <p className="muted">问题编号沿用到后续版本；闭合需要独立核验依据，历史意见保留。</p>
      {issues.map(issue => <div key={issue.id}><strong>{issue.status === 'closed' ? '已闭合' : '待闭合'} · {roles[issue.role] ?? issue.role} · <ReferenceText value={issue.id} /></strong><p><ReferenceText value={issue.issue} /></p>{issue.last_update?.rationale && <p>核验说明：<ReferenceText value={issue.last_update.rationale} /></p>}</div>)}
    </details>}
    {!definitionPhase && architectureReviewDeliveryLabel(current) && <div className="inline-info" aria-label="评审交付接收状态"><strong><ReferenceText value={architectureReviewDeliveryLabel(current)} /></strong><p>原答复和读取记录已保留，补正须由原角色在同一文档版本上完成，不能因此登记批准。</p>{current.review_delivery_validation?.diagnostic && <details><summary>评审交付校验原因</summary><pre><ReferenceText value={current.review_delivery_validation.diagnostic} /></pre></details>}</div>}
    {!definitionPhase && !architectureDeliveryLabel(value?.revision) && revisionDetails}
    {definitions && <div className="inline-info" aria-label="科学定义补充与共识"><strong>{definitionStates[definitions.status] ?? '科学定义状态待核对'} · {definitions.issue_count} 项具体意见</strong>
      {definitions.authoring_protocol === 2 && <div aria-label="逐项科学定义交付"><p>逐项候选已保存 {definitions.saved_issue_count ?? 0} / <ReferenceText value={definitions.issue_count} /> 项。<ReferenceText value={definitions.status === 'drafting' && definitions.active_issue_number ? `当前编写第 ${definitions.active_issue_number} 项。` : ''} />逐项候选与同版本独立评议分开记录。</p>
        {definitions.reused_issue_count > 0 && <p>其中复用 <ReferenceText value={definitions.reused_issue_count} /> 项兼容候选，原交付与文档索引保留。</p>}
        {definitions.issue_documents?.map((unit: Json, number: number) => <details key={unit.issue_id}><summary>第 {number + 1} 项 · {unit.status === 'unresolved' ? '仍有未决内容' : definitions.status === 'approved' ? '已纳入通过的定义版本' : unit.delivery_provenance === 'reused_actual_delivery' ? '已复用候选，待评议' : '候选已保存，待评议'}</summary><IndexedDocument key={unit.document.sha256} runId={runId} document={unit.document} label={`第 ${number + 1} 项科学定义候选`} /></details>)}
      </div>}
      {definitions.review_protocol === 3 && <p>同一版本已保存 {definitions.saved_review_units ?? 0} / {definitions.expected_review_units ?? 0} 份分组意见。</p>}
      <p>同一文档版本已收到 {definitions.received_votes ?? 0} / {definitions.expected_votes ?? 3} 份<ReferenceText value={definitions.review_protocol === 3 ? '三方终审' : '独立意见'} />。</p>
      {unresolvedIssueCount > 0 && <p>本候选仍有 {unresolvedIssueCount} 项定义未闭合。</p>}
      {definitions.scope_issues?.length > 0 && <ul>{definitions.scope_issues.map((issue: string, i: number) => <li key={i}><ReferenceText value={issue} /></li>)}</ul>}
      <p><ReferenceText value={definitions.reason ?? '先形成具体定义 Markdown，再独立审议；尚未批准代码或实验。'} /></p>
      {definitions.activity && <div aria-label="当前科学定义处理进度"><strong>{definitions.activity.role === 'engineer' ? '实验工程师' : roles[definitions.activity.role] ?? definitions.activity.role} · {definitions.activity.handoff_status === 'completed' ? '本次交付已完成' : definitions.activity.finalizing ? '正在整理答复' : definitions.status === 'drafting' ? '读取并编写补充定义' : '读取并独立审议'} · 第 {definitions.activity.continuation_turn ?? 0} 轮</strong>
        <p>已读取 {definitions.activity.read_documents ?? 0} 份文档、{definitions.activity.read_pages ?? 0} 个分页，其中 {definitions.activity.rag_source_read_pages ?? 0} 次 RAG 原文读取；已保存 {definitions.activity.working_notes_count ?? 0} 条工作笔记。</p>
        {definitions.activity.working_notes_document && <IndexedDocument key={definitions.activity.working_notes_document.sha256} runId={runId} document={definitions.activity.working_notes_document} label="科学定义工作笔记" />}
      </div>}
      {definitions.votes?.map((vote: Json) => <details key={vote.role}><summary>{vote.role === 'engineer' ? '实验工程师' : roles[vote.role] ?? vote.role} · {vote.approve && !vote.required_changes?.length ? '同意具体定义' : '具体定义需修改'}</summary><p><ReferenceText value={vote.rationale} /></p>{vote.required_changes?.length > 0 && <ul>{vote.required_changes.map((fix: string, i: number) => <li key={i}>{fix}</li>)}</ul>}<ReviewAdvice review={vote} /></details>)}
      {definitions.document && <IndexedDocument key={definitions.document.sha256} runId={runId} document={definitions.document} label="科学定义补充" />}
      <p><ReferenceText value={definitions.completion?.delivery_recovery?.pending ? '下一步：处理本版已保存终审的交付校验，继续剩余评议。' : definitions.status === 'rejected' || definitions.status === 'blocked' ? '下一步：处理上方具体未决项或必改意见，再审议新的定义文档版本。' : definitions.status === 'drafting' || definitions.status === 'prepared' ? hasDefinitionProblems ? '下一步：保存剩余具体定义，并处理未决内容后再收集三方独立意见。' : '下一步：提交具体定义 Markdown，再收集三方对同一版本的独立意见。' : '下一步：具体定义取得一致意见后编写新架构，再评议架构与实现文档。'} />原科学问题、比较、种子及验收门槛保留。</p>
    </div>}
    <ArchitectureHistory historical={definitionPhase}>
    {definitionPhase && revisionDetails}
    {response && <div className="inline-info" aria-label="架构作者交付"><strong>作者交付已保存 · 作者申报已处理 {response.resolved_issue_count ?? 0} / {response.review_issue_count ?? 0} 条意见</strong>
      <p><ReferenceText value={response.change_summary} /></p>
      <p><ReferenceText value={response.requires_new_consensus ? '下一步：补齐未闭合的科学与实现定义，再取得三方独立共识；代码和实验尚未获准。' : '下一步：闭合具体意见并完成独立评议；代码和实验尚未获准。'} /></p>
      {(response.unresolved_issues?.length ?? 0) > 0 && <details open><summary>未闭合意见 · {response.unresolved_issues.length} 项</summary><ul>{response.unresolved_issues.map((issue: Json) => <li key={issue.id}><strong>{roles[issue.role] ?? issue.role}：</strong><ReferenceText value={issue.issue} /></li>)}</ul></details>}
      {response.document && <IndexedDocument key={response.document.sha256} runId={runId} document={response.document} label="架构作者交付" />}
    </div>}
    {reading && <div className="inline-info" aria-label="架构修订阅读进度"><strong>{reading.handoff_status === 'completed' ? '作者交付已完成' : reading.finalizing ? '正在整理架构答复' : '文档读取与修订'} · 第 {reading.continuation_turn ?? 0} 轮</strong>
      <p>已读取 {reading.read_documents ?? 0} 份文档、{reading.read_pages ?? 0} 个分页，去重正文范围 {reading.unique_inspected_chars ?? 0} 字符；已保存 {reading.working_notes_count ?? 0} 条工作笔记。本次需核对 {reading.review_issue_count ?? 0} 条修改意见。</p>
      <p><ReferenceText value={reading.handoff_status === 'completed' ? '本轮作者交付已经结束，阅读记录与工作笔记可用于复核。读取范围与作者笔记不代表评议通过。' : '下一步：作者提交新 Markdown，再由两位角色独立评议。读取范围与作者笔记不代表评议通过。'} /></p>
      {reading.working_notes_document && <IndexedDocument key={reading.working_notes_document.sha256} runId={runId} document={reading.working_notes_document} label="架构阅读工作笔记" />}
    </div>}
    <p><ReferenceText value={current.status === 'approved' ? '两位独立评议者已通过同一文档版本，准备推进代码实现。接下来先编写实现 Markdown、按索引评议，再交给本机 Codex。' : current.status === 'rejected' ? '存在未解决的修改意见，代码实现与实验暂不推进。' : current.status === 'pending' ? 'Markdown 已保存并索引，等待进入文档评议。' : '已保存 Markdown，正在逐段评议并汇总跨章节意见。'} /></p>
    {current.provenance === 'demo' && <p className="inline-info">演示评议记录，不构成真实智能体或科学批准。</p>}
    <p className="muted">文档：<ReferenceText value={doc.title || '架构设计文档'} /><ReferenceText value={current.version != null && ` · 第 ${current.version} 版`} /></p>
    <IndexedDocument key={doc.sha256} runId={runId} document={doc} label="架构" versions={architectureDocumentVersions(value)} />
    <div className="architecture-review-roles">{Object.entries(roles).map(([role, label]) => {
      const review = (current.effective_reviews ?? current.reviews ?? []).find((v: Json) => v.role === role);
      const sections = (current.effective_unit_reviews ?? current.unit_reviews ?? []).filter((v: Json) => v.role === role);
      const roleApproved = review?.approve && !review.issues?.length && sections.every((v: Json) => v.approve && !v.issues?.length);
      return <details key={role} open={Boolean(review && !roleApproved || sections.some((v: Json) => v.disposition === 'needs_evidence'))}><summary>{label} · {review ? roleApproved ? '已通过' : review.disposition === 'needs_evidence' ? '等待同版本补证据' : '需要修改' : `已评议 ${sections.length} / ${current.unit_count ?? '待计算'} 组片段 · 等待最终意见`}</summary>
        {review && <p><ReferenceText value={review.rationale} /></p>}{review?.issues?.length > 0 && <ul>{review.issues.map((issue: string, i: number) => <li key={i}><ReferenceText value={issue} /></li>)}</ul>}
        {review && <ReviewAdvice review={review} />}
        {sections.map((v: Json) => <div key={v.unit_id}><strong>第 {v.unit_number} 组 · {v.approve && !v.issues?.length ? '通过' : v.disposition === 'needs_evidence' ? '待补证据' : '待修改'}</strong><p><ReferenceText value={v.rationale} /></p>{v.issues?.length > 0 && <ul>{v.issues.map((issue: string, i: number) => <li key={i}><ReferenceText value={issue} /></li>)}</ul>}<ReviewAdvice review={v} /></div>)}
        {((current.unit_review_amendments?.length ?? 0) + (current.final_review_amendments?.length ?? 0) > 0) && <details><summary>补证据前的原始意见</summary>{[...(current.unit_reviews ?? []), ...(current.reviews ?? [])].filter((v: Json) => v.role === role).map((v: Json, i: number) => <div key={i}><p><ReferenceText value={v.rationale} /></p>{v.issues?.map((issue: string, j: number) => <p key={j}><ReferenceText value={issue} /></p>)}</div>)}</details>}
      </details>;
    })}</div>
    </ArchitectureHistory>
    <HistoricalArchitectureDiagrams runId={runId} rows={value?.history} currentId={current.id} />
    <SavedArchitectures runId={runId} rows={value?.history} currentId={current.id} />
  </section></ReferenceNames>;
}
