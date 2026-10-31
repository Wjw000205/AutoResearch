import { useEffect, useId, useState } from 'react';
import { Clock3, RefreshCw, Wrench } from 'lucide-react';
import { humanizeMessage } from './localization';
import { RecordPagination, RECORD_PAGE_SIZE } from './RecordPagination';
import { useRemoteResource } from './useRemoteResource';
import { api } from './api';
import { createResourceSession, RESOURCE_READ_TIMEOUT_MS } from './RemoteResource';
import type { ToolCall, ToolCallDetail, ToolCallPage, ToolCatalog } from './ToolTypes';
import { toolName } from './ToolLabels';
import './tools.css';

const callStatuses: Record<string, string> = { started: '仅记录开始 · 结果未知', completed: '调用已完成', failed: '调用失败' };
const sourceNames: Record<string, string> = { correlated_events: '已关联调用与结果事件', legacy_event: '历史事件记录', runtime_harness: '运行时工具事件' };

export function toolCallPagePath(runId: string, offset = 0, role = '', tool = '', status = '') {
  const params = new URLSearchParams({ limit: String(RECORD_PAGE_SIZE), offset: String(offset) });
  if (role) params.set('role', role);
  if (tool) params.set('tool', tool);
  if (status) params.set('status', status);
  return `/runs/${encodeURIComponent(runId)}/tool-calls?${params}`;
}

export function toolCallDetailPath(runId: string, callId: string) {
  return `/runs/${encodeURIComponent(runId)}/tool-calls/${encodeURIComponent(callId)}`;
}

export function toolCallDetailKey(runId: string | undefined, call: ToolCall, retry = 0) {
  return JSON.stringify([runId, call.id, call.status, call.evidence.finished_event_id, retry]);
}

export function createToolCallDetailSession(runId: string, callId: string,
  success: (call: ToolCallDetail) => void, failure: (error: Error) => void) {
  return createResourceSession<ToolCallDetail>({
    request: async signal => {
      const detail = await api<ToolCallDetail>(toolCallDetailPath(runId, callId), { signal });
      if (!detail || detail.id !== callId || detail.run_id !== runId || detail.truncated !== false) {
        throw new Error('完整记录响应与本次调用不匹配，请重新读取。');
      }
      return detail;
    },
    success, failure, visible: () => true, timeoutMs: RESOURCE_READ_TIMEOUT_MS,
  });
}

function callTime(value: string | null) {
  if (!value) return '未记录';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false });
}

function readableValue(value: unknown, summary: string) {
  if (value == null) return summary || '未记录';
  return typeof value === 'string' ? value : JSON.stringify(value, null, 2);
}

function ToolValue({ label, value, summary }: { label: string; value: unknown; summary: string }) {
  const text = readableValue(value, summary);
  return <section className="tool-value"><h4>{label}</h4><pre className="tool-readable-text" tabIndex={0} aria-label={label}>{text}</pre></section>;
}

export function toolCallJson(call: ToolCall) {
  return JSON.stringify(call, null, 2);
}

function downloadCall(call: ToolCall) {
  const url = URL.createObjectURL(new Blob([toolCallJson(call)], { type: 'application/json;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = `tool-call-${call.id.replace(/[^a-z0-9_-]/gi, '_')}.json`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function ToolCallDetailBody({ call }: { call: ToolCall }) {
  const result = call.result && typeof call.result === 'object' && !Array.isArray(call.result)
    ? call.result as Record<string, unknown> : null;
  const nextOffset = result?.next_offset;
  const partialResult = result?.truncated === true || result?.page_truncated === true || nextOffset != null;
  return <>
    {!call.truncated && <div className="tool-detail-heading"><p>完整已保存记录（已脱敏）</p><button type="button" className="btn small" onClick={() => downloadCall(call)}>下载完整 JSON</button></div>}
    {partialResult && <p className="tool-truncation">工具返回本身标记为截断或分页；这里保留本次返回的完整记录。{nextOffset != null && <> 后续读取位置：<code>next_offset={String(nextOffset)}</code>。</>}</p>}
    <div className="tool-value-grid"><ToolValue label="调用参数" value={call.arguments} summary={call.arguments_summary} /><ToolValue label="工具返回结果" value={call.result} summary={call.result_summary} /></div>
    {call.error && <ToolValue label="完整错误记录" value={call.error} summary="" />}
    <dl className="tool-evidence"><div><dt>开始时间</dt><dd title={call.started_at ?? undefined}>{callTime(call.started_at)}</dd></div><div><dt>结束时间</dt><dd title={call.finished_at ?? undefined}>{callTime(call.finished_at)}</dd></div><div><dt>分派记录</dt><dd><code>{call.handoff_id ?? '未记录'}</code></dd></div><div><dt>调用记录 ID</dt><dd><code>{call.id}</code></dd></div><div><dt>记录来源</dt><dd>{sourceNames[call.provenance.source] ?? call.provenance.source}<code>{call.provenance.source}</code></dd></div><div><dt>结果来源</dt><dd>{call.provenance.result ? <>{humanizeMessage(call.provenance.result)}<code>{call.provenance.result}</code></> : '未记录'}</dd></div><div><dt>事件关联</dt><dd>{call.evidence.correlated ? '已关联开始与结束事件' : '事件关联不完整'} · 开始 {call.evidence.started_event_id ?? '未记录'} / 结束 {call.evidence.finished_event_id ?? '未记录'}</dd></div></dl>
  </>;
}

export function ToolCallCard({ call, runId }: { call: ToolCall; runId?: string }) {
  const [expanded, setExpanded] = useState(false);
  const [retry, setRetry] = useState(0);
  const key = toolCallDetailKey(runId, call, retry);
  const [detail, setDetail] = useState<{ key: string; data: ToolCallDetail | null; error: string }>({ key: '', data: null, error: '' });
  useEffect(() => {
    if (!expanded || !runId) return;
    setDetail({ key, data: null, error: '' });
    const session = createToolCallDetailSession(runId, call.id,
      data => setDetail({ key, data, error: '' }), error => setDetail({ key, data: null, error: error.message }));
    void session.start();
    return () => session.stop();
  }, [expanded, runId, call.id, key]);
  const currentDetail = detail.key === key ? detail : { key, data: null, error: '' };
  const status = callStatuses[call.status] ?? humanizeMessage(call.status);
  return <article className={`tool-call-card tool-call-${call.status}`}>
    <header><div className="tool-call-title"><Wrench size={17} aria-hidden="true" /><h3><code>{call.tool_name}</code>{toolName(call.tool_name) !== call.tool_name && <small>{toolName(call.tool_name)}</small>}</h3></div><span className={`tool-call-status status-${call.status}`}>{status}</span></header>
    <div className="tool-call-meta"><span className={`tool-role role-${call.role}`}>调用智能体：{call.role ? humanizeMessage(call.role) : '未记录'}{call.role && <code>{call.role}</code>}</span><span><Clock3 size={13} aria-hidden="true" />{callTime(call.started_at ?? call.finished_at)}</span><span>{call.provenance.mode ? humanizeMessage(call.provenance.mode) : '运行模式未记录'}</span></div>
    <div className="tool-call-summary"><p><strong>参数：</strong>{call.arguments_summary || '未记录'}</p><p><strong>结果：</strong>{call.result_summary || (call.status === 'started' ? '未记录结果事件' : '未记录')}</p></div>
    {call.interrupted && <p className="tool-call-error">调用被中断，结果以已保存的事件为准。</p>}
    {call.error && <p className="tool-call-error">错误：{call.error}{call.error_type && <> · <code>{call.error_type}</code></>}</p>}
    {call.truncated && <p className="tool-truncation">列表仅展示预览，展开可查看完整记录。</p>}
    <details className="tool-call-details" onToggle={event => setExpanded(event.currentTarget.open)}><summary>参数、结果与来源</summary>
      {runId ? expanded && (currentDetail.data ? <ToolCallDetailBody call={currentDetail.data} />
        : currentDetail.error ? <div className="tool-error" role="alert"><p>完整记录读取失败：{currentDetail.error}</p><button type="button" className="btn small" onClick={() => setRetry(value => value + 1)}>重新读取完整记录</button></div>
        : <p className="tool-loading" role="status">正在读取完整已保存记录…</p>) : <ToolCallDetailBody call={call} />}
    </details>
  </article>;
}

export function ToolCallRecords({ page, loading = false, onOffset }: { page: ToolCallPage; loading?: boolean; onOffset: (offset: number) => void }) {
  return <><div className="tool-results-heading"><h3>实际工具调用</h3><span role="status">共 {page.total} 条记录</span></div>
    {page.items.length ? <div className="tool-call-list">{page.items.map(call => <ToolCallCard key={call.id} call={call} runId={page.run_id} />)}</div> : <div className="tool-empty"><Wrench size={26} aria-hidden="true" /><h3>暂无工具调用记录</h3><p>当前研究或筛选条件下，没有已记录的工具事件。</p></div>}
    <RecordPagination label="工具调用" offset={page.offset} count={page.items.length} total={page.total} limit={page.limit} loading={loading} onOffset={onOffset} />
  </>;
}

export function ToolCallsPanel({ runId, revision = 0, running = false }: { runId: string; revision?: string | number; running?: boolean }) {
  const [refresh, setRefresh] = useState(0);
  const [selection, setSelection] = useState({ runId, offset: 0, role: '', tool: '', status: '' });
  const [toolDraft, setToolDraft] = useState({ runId, value: '' });
  const toolHintId = useId();
  const toolInputId = useId();
  useEffect(() => {
    setSelection({ runId, offset: 0, role: '', tool: '', status: '' });
    setToolDraft({ runId, value: '' });
  }, [runId]);
  // Reset the query synchronously when switching studies so a previous role/page cannot hide the new run.
  const filters = selection.runId === runId ? selection : { runId, offset: 0, role: '', tool: '', status: '' };
  const draft = toolDraft.runId === runId ? toolDraft.value : '';
  const change = (field: 'role' | 'tool' | 'status', value: string) => setSelection({ ...filters, [field]: value, offset: 0 });
  const catalog = useRemoteResource<ToolCatalog>(`/tools?run_id=${encodeURIComponent(runId)}`, revision);
  const calls = useRemoteResource<ToolCallPage>(toolCallPagePath(runId, filters.offset, filters.role, filters.tool, filters.status), `${revision}:${refresh}`, running ? 5000 : 0);
  const roles = [...new Set([...(catalog.data?.agents.map(agent => agent.role) ?? []), ...(calls.data?.items.map(call => call.role).filter(Boolean) ?? [])])];
  return <section className="panel tool-calls-panel" aria-label="研究工具调用">
    <header className="tool-panel-heading"><div><h2><Wrench size={18} aria-hidden="true" />工具调用</h2><p>查看本研究调用过的具体工具、调用智能体及参数和返回结果。</p></div><button type="button" className="btn small" disabled={calls.loading} onClick={() => setRefresh(value => value + 1)}><RefreshCw size={14} aria-hidden="true" />刷新记录</button></header>
    <div className="tool-filters">
      <label><span>调用智能体</span><select value={filters.role} onChange={event => change('role', event.target.value)}><option value="">全部智能体</option>{roles.map(role => <option value={role} key={role}>{humanizeMessage(role)} · {role}</option>)}</select></label>
      <form className="tool-name-filter" aria-label="按工具名称筛选调用" onSubmit={event => { event.preventDefault(); change('tool', draft.trim()); }}>
        <label htmlFor={toolInputId}><span>工具名称</span></label>
        <div className="tool-name-filter-controls"><input id={toolInputId} list={`run-tool-names-${runId}`} value={draft} onChange={event => setToolDraft({ runId, value: event.target.value })} aria-describedby={toolHintId} placeholder="输入完整工具标识，留空查看全部" /><button className="btn small" type="submit">筛选工具</button></div>
        <datalist id={`run-tool-names-${runId}`}>{(catalog.data?.tools ?? []).map(tool => <option key={tool.name} value={tool.name} />)}</datalist>
        <p id={toolHintId}>按 Enter 或点击“筛选工具”应用名称；当前：<code>{filters.tool || '全部工具'}</code></p>
      </form>
      <label><span>调用状态</span><select value={filters.status} onChange={event => change('status', event.target.value)}><option value="">全部状态</option>{Object.entries(callStatuses).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
    </div>
    {calls.loading && <p className="tool-loading" role="status">正在读取工具调用记录…</p>}
    {catalog.error && <p className="tool-scope-note">工具目录暂未读取：{catalog.error}。仍可查看调用记录并输入工具标识筛选。</p>}
    {calls.error && <div className="tool-error" role="alert"><p>工具调用记录读取失败：{calls.error}</p><button type="button" className="btn small" onClick={() => setRefresh(value => value + 1)}>重新读取</button></div>}
    {calls.data && <ToolCallRecords page={calls.data} loading={calls.loading} onOffset={offset => setSelection({ ...filters, offset })} />}
    <p className="tool-scope-note">工具注册目录表示角色具备的工具能力；调用列表依据本研究保存的事件生成。缺失的参数、结果或事件关联会按原记录显示。</p>
  </section>;
}
