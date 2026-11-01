import type { AgentHistoryEntry } from './api';
import { ReferenceText } from './ReferenceNames';

type RecordValue = Record<string, unknown>;
const object = (value: unknown): RecordValue => value && typeof value === 'object' && !Array.isArray(value) ? value as RecordValue : {};
const string = (value: unknown) => typeof value === 'string' ? value.trim() : '';
const list = (value: unknown): unknown[] => Array.isArray(value) ? value : [];
const count = (value: unknown, fallback: number) => typeof value === 'number' && Number.isInteger(value) && value >= fallback ? value : fallback;
const kindLabels: Record<string, string> = {document_revision: '修改文档', review_incomplete: '补充核查', external_dependency: '恢复依赖'};

/** Read only the feedback attached to this output, never infer fixes from a rejection. */
export function reviewFeedback(entry?: AgentHistoryEntry) {
  let parsed: RecordValue = {};
  if (entry?.kind === 'output') { try { parsed = object(JSON.parse(entry.text)); } catch { /* The lightweight entry contains a plain-text summary. */ } }
  const source = Object.keys(parsed).length && !entry?.truncated ? parsed : {...object(entry?.consensus), ...parsed};
  const issues = list(source.issues).length ? list(source.issues) : list(source.required_changes);
  const routes = list(object(source.review_routing).issues).map(object);
  const kinds = [...new Set([...list(source.review_routing_kinds).map(string), ...routes.map(route => string(route.kind))])].filter(kind => kind in kindLabels);
  const only = (kind: string) => kinds.length === 1 && kinds[0] === kind;
  const fixes = list(source.fixes).map(value => {
    const fix = object(value);
    const index = fix.issue_index ?? fix.objection_index;
    const issueIndex = typeof index === 'number' && Number.isInteger(index) && index >= 0 ? index : undefined;
    const route = issueIndex !== undefined ? routes.find(item => item.issue_index === issueIndex) : undefined;
    const kind = string(route?.kind);
    return {
      issueIndex, kind, kindLabel: kindLabels[kind] ?? '',
      issue: string(fix.issue) || (issueIndex !== undefined ? string(issues[issueIndex]) : '') || string(fix.reason),
      location: string(fix.location) || string(fix.target) || string(fix.target_field),
      change: string(fix.change) || string(fix.proposed_change),
      verification: string(fix.verification) || string(fix.verification_plan),
      recovery: string(route?.recovery_condition),
    };
  }).filter(fix => fix.issue || fix.location || fix.change || fix.verification);
  const total = count(source.fixes_count, fixes.length);
  const issueTotal = count(source.issues_count ?? source.required_changes_count, issues.length);
  const rejected = source.approve === false;
  const truncated = Boolean(entry?.truncated || source.feedback_truncated || total > fixes.length);
  return {
    fixes, total, issueTotal, rejected, truncated,
    title: only('review_incomplete') ? '待补充核查' : only('external_dependency') ? '依赖恢复条件' : only('document_revision') ? '具体修改' : '具体修改与核查',
    verdict: rejected ? only('review_incomplete') ? '需补充审查' : only('external_dependency') ? '等待依赖恢复' : '审核未通过' : source.approve === true ? '审核同意' : '',
  };
}

export function ReviewFeedback({ entry }: { entry: AgentHistoryEntry }) {
  if (entry.kind !== 'output') return null;
  const feedback = reviewFeedback(entry);
  if (!feedback.fixes.length && !feedback.rejected) return null;
  return <section className="command-review-feedback" aria-label="具体修改与核查">
    <h5>{feedback.title}{feedback.total > 0 && <span> · {feedback.total} 项</span>}</h5>
    {feedback.fixes.length ? <ol>{feedback.fixes.map((fix, index) => <li key={index}>
      <div className="command-fix-heading"><strong>{fix.issueIndex !== undefined ? `问题 ${fix.issueIndex + 1}` : `处理项 ${index + 1}`}</strong>{fix.kindLabel && <span>{fix.kindLabel}</span>}</div>
      {fix.issue && <p><ReferenceText value={fix.issue} /></p>}
      <dl>
        <dt>位置</dt><dd><ReferenceText value={fix.location || '此条未记录具体位置'} /></dd>
        <dt>{fix.kind === 'review_incomplete' ? '核查步骤' : fix.kind === 'external_dependency' ? '恢复步骤' : '具体修改 / 核查步骤'}</dt><dd><ReferenceText value={fix.change || '此条未记录具体处理步骤'} /></dd>
        <dt>通过条件</dt><dd><ReferenceText value={fix.verification || '此条未记录通过条件'} /></dd>
        {fix.recovery && <><dt>恢复条件</dt><dd><ReferenceText value={fix.recovery} /></dd></>}
      </dl>
    </li>)}</ol> : <p>{feedback.truncated ? '具体处理步骤尚未加载，请读取本次输出全文。' : '此条评议尚未保存具体修改或核查步骤。'}</p>}
    {feedback.truncated && feedback.fixes.length > 0 && <small>当前为部分摘要，正在读取或可刷新获取完整处理步骤。</small>}
  </section>;
}
