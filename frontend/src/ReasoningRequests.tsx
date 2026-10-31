import { ReferenceText } from './ReferenceNames';
import { useState } from 'react';
import { date, type Json } from './api';
import { useRemoteResource } from './useRemoteResource';
import { codexCount } from './LocalCodexUsage';
import { RecordPagination, RECORD_PAGE_SIZE } from './RecordPagination';
import './reasoning-requests.css';

export function SessionTokens({ usage }: { usage?: Json | null }) {
  return <dl className="session-token-grid" aria-label="会话 token 用量">{[
    ['inputTokens', '输入 token'], ['cachedInputTokens', '其中缓存命中'],
    ['outputTokens', '输出 token'], ['reasoningOutputTokens', '其中推理输出'],
    ['totalTokens', '总 token'],
  ].map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{codexCount(usage?.[key])}</dd></div>)}</dl>;
}

const roles: Record<string, string> = { director: '研究总监', engineer: '实验工程师', scout: '文献研究员' };
const statuses: Record<string, string> = { prepared: '已准备', thread_start_dispatched: '创建会话中', thread_ready: '会话就绪', turn_start_dispatched: '已提交', running: '推理中', completed: '已完成', unknown: '等待核对回执', blocked: '等待处理', failed: '失败', interrupted: '已中断' };
const purposes: Record<string, string> = { literature_reading: '补读论文与保存笔记', literature: '文献分析', evidence: '补充证据', related_work_queries: '规划相关论文检索', innovation_candidate: '提出创新候选', innovation_compare: '比较与审查创新候选', discussion_action: '补充机制与实验设计', design: '架构设计', design_document_section: '分段评议架构文档', design_document_review: '汇总架构文档评议', design_revision: '根据具体意见修订架构文档', implementation_spec: '编写代码实现文档', implementation_document_review: '三方全文审核' };

export const reasoningRequestsPath = (runId: string, offset = 0) =>
  `/runs/${encodeURIComponent(runId)}/reasoning-requests?limit=${RECORD_PAGE_SIZE}&offset=${offset}`;

export function latestActiveReasoningRequest(rows: Json[]) {
  const priority: Record<string, number> = { running: 0, turn_start_dispatched: 0, thread_ready: 1, thread_start_dispatched: 1, prepared: 2 };
  const time = (row: Json) => Date.parse(row.submitted_at ?? row.created_at ?? '') || 0;
  return rows.filter(row => priority[row.status] !== undefined).sort((left, right) =>
    priority[left.status] - priority[right.status] || time(right) - time(left))[0];
}

function elapsed(start: string | undefined, now: number) {
  const seconds = Math.max(0, Math.floor((now - Date.parse(start ?? '')) / 1000));
  if (!Number.isFinite(seconds)) return '等待计时';
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

export function ReasoningProgressContent({ rows, runStatus, now = Date.now(), onDetails }: { rows: Json[]; runStatus: string; now?: number; onDetails: () => void }) {
  if (!rows.length || runStatus !== 'running') return null;
  const active = latestActiveReasoningRequest(rows);
  const completed = rows.filter(row => row.status === 'completed').length;
  const purpose = String(active?.selection?.purpose ?? '').split(':')[0];
  return <section className="panel compact-panel" aria-label="当前推理进度">
    <h3><ReferenceText value={active ? `${roles[active.role] ?? active.role} · ${purposes[purpose] ?? '等待模型回复'}` : '最近请求页中暂无活动推理'} /></h3>
    <p>最近 {RECORD_PAGE_SIZE} 条请求页中已完成 {completed} 次回复{active && <> · 当前 {active.selection?.model} / {active.selection?.effort} · 已等待 {elapsed(active.submitted_at ?? active.created_at, now)}</>}</p>
    {active?.last_event_at && <p>最近回执：<ReferenceText value={date(active.last_event_at, true)} /></p>}
    {active && active.retry_count > 0 && <p>当前请求已记录 <ReferenceText value={active.retry_count} /> 次连接恢复尝试；尚未收到完整回复。系统仍在等待本次请求。</p>}
    <button className="btn small" onClick={onDetails}>查看请求与执行进度</button>
  </section>;
}

export function ReasoningProgress({ runId, runStatus, onDetails }: { runId: string; runStatus: string; onDetails: () => void }) {
  const resource = useRemoteResource<Json[]>(runStatus === 'running' ? reasoningRequestsPath(runId) : null, 0, 10000);
  return <ReasoningProgressContent rows={resource.data ?? []} runStatus={runStatus} onDetails={onDetails} />;
}

export function ReasoningRequestContent({ value }: { value: Json }) {
  return <div><p><ReferenceText value={value.scope} /></p><dl><div><dt>模型／推理强度</dt><dd>{value.selection?.model} / {value.selection?.effort}</dd></div><div><dt>会话／回合</dt><dd><code>{value.receipt?.thread_id ?? '尚未返回'} / {value.receipt?.turn_id ?? '尚未返回'}</code></dd></div></dl>
    <h4>本会话累计 token</h4><SessionTokens usage={value.receipt?.usage?.total} /><p>按 Codex 回执记录；缓存命中属于输入，推理输出属于输出，不重复相加。未返回的字段显示“未报告”。</p>
    <details open><summary>返回结果与回执</summary><pre tabIndex={0}>{JSON.stringify({ receipt: value.receipt, result: value.result ?? '尚未返回完整结果' }, null, 2)}</pre></details>
    <details><summary>发送给模型的完整任务正文（脱敏）</summary><pre tabIndex={0}>{value.instruction}</pre></details>
    <details><summary>输出 schema 与传输格式</summary><pre tabIndex={0}>{JSON.stringify({ schema: value.schema, transport_schema: value.transport_schema }, null, 2)}</pre></details>
  </div>;
}

export function ReasoningRequests({ runId, tokenSummary }: { runId: string; tokenSummary?: Json }) {
  const [page, setPage] = useState({ runId, offset: 0, revision: 0 });
  const offset = page.runId === runId ? page.offset : 0;
  const revision = page.runId === runId ? page.revision : 0;
  const list = useRemoteResource<Json[]>(reasoningRequestsPath(runId, offset), revision, 0);
  const [request, setRequest] = useState<{ runId: string; id: string; revision: number } | null>(null);
  const selectedId = request?.runId === runId ? request.id : null;
  const detail = useRemoteResource<Json>(selectedId ? `/runs/${encodeURIComponent(runId)}/reasoning-requests/${encodeURIComponent(selectedId)}` : null, request?.revision ?? 0);
  return <ReasoningRequestsContent tokenSummary={tokenSummary} rows={list.data ?? []} offset={offset}
    listLoading={list.loading} detailLoading={detail.loading} selectedId={selectedId} selected={detail.data} error={list.error} detailError={detail.error}
    onSelect={id => setRequest({ runId, id, revision: 0 })} onClose={() => setRequest(null)}
    onRetry={() => setRequest(current => current ? { ...current, revision: current.revision + 1 } : null)}
    onOffset={next => { setRequest(null); setPage({ runId, offset: next, revision }); }}
    onRefresh={() => setPage({ runId, offset, revision: revision + 1 })} />;
}

export function ReasoningRequestsContent({ tokenSummary, rows, offset, listLoading, detailLoading, selectedId, selected, error, detailError = '',
  onSelect, onClose, onOffset, onRefresh, onRetry }: {
  tokenSummary?: Json; rows: Json[]; offset: number; listLoading: boolean; detailLoading: boolean; selectedId?: string | null; selected: Json | null; error: string; detailError?: string;
  onSelect: (id: string) => void; onClose: () => void; onOffset: (offset: number) => void; onRefresh: () => void; onRetry?: () => void;
}) {
  const completed = rows.filter(row => row.status === 'completed').length;
  return <section className="panel compact-panel"><h3>本机 Codex 推理请求</h3><p>按角色查看实际提交的材料、schema、推理强度及返回结果。</p>
    <h4>本研究 Codex 累计用量</h4><SessionTokens usage={tokenSummary?.observed_totals} /><p>每个会话的最新累计回执只统计一次。会话清理后仍保留用量记录；这些 token 不等同于套餐额度百分比。</p>
    <p>本页 {rows.length} 条请求，已完成 {completed} 次回复。历史列表在翻页或刷新时读取。</p>
    <RecordPagination label="推理请求" offset={offset} count={rows.length} loading={listLoading} onOffset={onOffset} />
    <button type="button" className="btn small" disabled={listLoading} onClick={onRefresh}>刷新当前页</button>
    {error && <p role="alert"><ReferenceText value={error} /></p>}
    {listLoading && <p role="status">正在读取请求记录…</p>}
    {!listLoading && !rows.length && !error && <p>{offset ? '本页暂无推理请求，可返回上一页。' : '尚无本机 Codex 推理请求。'}</p>}
    {rows.map(row => {
      const expanded = (selectedId ?? selected?.id) === row.id;
      const detailId = `reasoning-request-${encodeURIComponent(row.id)}`;
      return <div className="artifact-row reasoning-request-row" key={row.id}><div><strong>{roles[row.role] ?? row.role} · {row.selection?.model} / {row.selection?.effort}</strong><p><ReferenceText value={date(row.created_at, true)} /> · {statuses[row.status] ?? row.status}<ReferenceText value={row.selection?.purpose && ` · ${purposes[String(row.selection.purpose).split(':')[0]] ?? row.selection.purpose}`} /><ReferenceText value={row.retry_count > 0 && ` · 连接恢复 ${row.retry_count} 次`} /></p><p className="reasoning-usage-label">会话累计 token</p><SessionTokens usage={row.usage?.total ?? tokenSummary?.records?.find((record: Json) => record.operation_id === row.id)?.usage?.total} /></div><button type="button" className="btn small" aria-expanded={expanded} aria-controls={expanded ? detailId : undefined} onClick={() => expanded ? onClose() : onSelect(row.id)}>{expanded ? '收起请求' : '查看请求与结果'}</button>
        {expanded && <section className="reasoning-request-detail" id={detailId} aria-label="请求正文与返回结果" aria-busy={detailLoading}>
          <h4>请求正文与返回结果</h4>
          {detailLoading && <p role="status">正在读取请求正文与返回结果…</p>}
          {detailError && <div role="alert"><p>请求详情读取失败：<ReferenceText value={detailError} /></p>{onRetry && <button type="button" className="btn small" onClick={onRetry}>重新读取请求</button>}</div>}
          {selected && <ReasoningRequestContent value={selected} />}
        </section>}
      </div>;
    })}
  </section>;
}
