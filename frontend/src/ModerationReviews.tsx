import { ReferenceText } from './ReferenceNames';
import { ArrowDownToLine, CircleAlert, Play } from 'lucide-react';
import { textOf, type Json } from './api';

export const MODERATION_PENDING_LABEL = '内容审核待人工复核 · 来源未确认（网关本地或上游）';
export const MODERATION_RECOVERY_REASON = '此研究有待复核的内容审核记录，需要管理员复核后才可恢复。';

export function isContentPolicyViolation(record: Json) {
  const details = record.error_details;
  return !!details && typeof details === 'object' && [details.code, details.type].includes('content_policy_violation');
}

// Explicit review records take precedence over historical billing receipts for
// the same local request. Neither HTTP 403 nor prose alone establishes moderation.
export function collectModerationReviews(runId: string, reviews?: Json[], billingRecords?: Json[]) {
  const receipts = (Array.isArray(billingRecords) ? billingRecords : []).filter(record => record.run_id === runId && isContentPolicyViolation(record));
  const result: Json[] = [];
  const knownRequests = new Set<string>();
  for (const review of Array.isArray(reviews) ? reviews : []) {
    if (review.run_id !== runId) continue;
    const requestId = typeof review.request_id === 'string' ? review.request_id : typeof review.id === 'string' ? review.id : '';
    if (requestId && knownRequests.has(requestId)) continue;
    if (requestId) knownRequests.add(requestId);
    const receipt = receipts.find(record => (record.request_id ?? record.id) === requestId);
    result.push({ ...receipt, ...review, request_id: requestId, review_status: review.review_status ?? 'pending' });
  }
  for (const receipt of receipts) {
    const requestId = typeof receipt.request_id === 'string' ? receipt.request_id : typeof receipt.id === 'string' ? receipt.id : '';
    if (requestId && knownRequests.has(requestId)) continue;
    if (requestId) knownRequests.add(requestId);
    result.push({ ...receipt, request_id: requestId, source: 'unknown', review_status: 'pending', request_body_available: false, request_audit_id: null, provenance: receipt.provenance ?? 'historical_billing_receipt', legacy_billing: true });
  }
  return result;
}

export const hasPendingModerationReview = (reviews: Json[]) => reviews.some(review => review.review_status === 'pending');

export function ResumeResearchButton({ status, busy, reviews, resume, blockReason, blockReasonId }: { status: string; busy: boolean; reviews: Json[]; resume: () => void; blockReason?: string; blockReasonId?: string }) {
  if (!['paused', 'blocked', 'failed'].includes(status)) return null;
  const pending = hasPendingModerationReview(reviews);
  return <button className="btn btn-primary" disabled={busy || pending || !!blockReason} title={pending ? MODERATION_RECOVERY_REASON : blockReason} aria-describedby={pending ? 'moderation-recovery-reason' : blockReason ? blockReasonId ?? 'definition-resume-reason' : undefined} onClick={() => { if (!busy && !pending && !blockReason) resume(); }}><Play size={15} />恢复执行</button>;
}

export function ModerationReviewPanel({ runId, reviews }: { runId: string; reviews: Json[] }) {
  if (!reviews.length) return null;
  const pending = hasPendingModerationReview(reviews);
  return <section className="moderation-review-panel" aria-label="内容审核诊断"><header><CircleAlert size={21} /><div><h2><ReferenceText value={pending ? MODERATION_PENDING_LABEL : '内容审核记录 · 来源未确认（网关本地或上游）'} /></h2><p id="moderation-recovery-reason"><ReferenceText value={pending ? MODERATION_RECOVERY_REASON : '保留已有复核与请求诊断，恢复资格仍受其他执行门禁约束。'} /></p><p>返回的内容审核错误不能确定发生于网关本地还是上游；当前记录不代表上游封禁。请求标识仅供追踪。</p></div></header><div className="moderation-review-list">{reviews.map((review, index) => {
    const details = review.error_details ?? {};
    const responseIds = Object.entries(review.response_request_ids ?? {}).flatMap(([location, value]) => {
      const ids = (Array.isArray(value) ? value : [value]).filter((id): id is string => typeof id === 'string' && id.trim().length > 0);
      return ids.map((id, index) => [ids.length > 1 ? `${location} [${index + 1}]` : location, id] as const);
    });
    const bodyAvailable = review.request_body_available === true && !!review.request_audit_id;
    return <article key={review.id ?? review.request_id ?? index}><div className="moderation-review-meta"><strong>{({ director: '研究总监', engineer: '实验工程师', scout: '文献研究员' } as Record<string, string>)[review.role] ?? review.role ?? '角色未记录'}</strong><span>{review.provider ?? '供应商未记录'} · {review.model ?? '模型未记录'}</span><span className="pill">{review.review_status === 'pending' ? '待人工复核' : `复核状态：${review.review_status}`}</span>{review.http_status && <span>HTTP {review.http_status}</span>}</div><div className="moderation-error"><span>原始错误文案</span><p><ReferenceText value={textOf(review.error || details.message) || '未保存原始错误文案'} /></p><div>{details.type && <code>类型： <ReferenceText value={details.type} /></code>}{details.code && <code>代码： <ReferenceText value={details.code} /></code>}</div></div><dl className="moderation-request-ids"><div><dt>本地请求标识</dt><dd><code><ReferenceText value={review.request_id || '未保存'} /></code></dd></div><div><dt>网关 / 上游响应请求标识（来源未确认）</dt><dd>{responseIds.length ? responseIds.map(([location, value]) => <div key={location}><small>{location}</small><code>{value}</code></div>) : <span>未保存响应请求标识，不能用本地标识代替。</span>}</dd></div></dl><div className="moderation-audit"><p><ReferenceText value={bodyAvailable ? review.request_body_truncated ? '已保存的脱敏请求快照存在截断；导出不包含未保存的部分。' : '可下载已保存的脱敏请求数据（JSON）与诊断信息，用于管理员核查。' : '未保存完整请求数据（JSON），无法从旧错误记录还原；仅可导出已有诊断。'} /></p>{review.request_id && <a className="btn small" href={`/api/runs/${encodeURIComponent(runId)}/moderation/${encodeURIComponent(review.request_id)}/audit`} download><ArrowDownToLine size={14} />{bodyAvailable ? '下载脱敏请求与诊断' : '导出已有诊断'}</a>}<small>记录来源：{review.provenance ?? '未记录'}</small></div></article>;
  })}</div></section>;
}
