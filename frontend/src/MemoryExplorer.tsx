import { useEffect, useId, useRef, useState } from 'react';
import { ArrowLeft, ArrowRight, BrainCircuit, ChevronDown, Database, History, RefreshCw, Search } from 'lucide-react';
import { date, type Json, type RunListItem } from './api';
import { MemorySourceCitation } from './MemoryIndexPanel';
import { RecordPagination } from './RecordPagination';
import { useRemoteResource } from './useRemoteResource';
import { captureLabel, dispatchLabel, inclusionClassName, inclusionLabel, memoryEntriesPath, memoryExplorerPath, memoryGapLabel, memoryPurposeName, memoryReadableValue, memoryReferenceLabel, memoryRequestDetailPath, memoryRoleName, memoryRoleNames, memoryRunHref, memoryTextPath, requestStatusLabel, selectMemoryRequestId } from './MemoryExplorerHelpers';
import type { InputReference, MemoryHistoryEntry, MemoryOverview, MemoryPageResult, MemoryRequest, MemoryRequestDetail, MemoryTask, MemoryTextPage, StoredMemoryEntry } from './MemoryExplorerTypes';
import './memory-explorer.css';

function MemoryReadNotice({ loading, error, label }: { loading: boolean; error: string; label: string }) {
  return <>{loading && <p className="memory-explorer-loading" role="status">正在读取{label}…</p>}{error && <p className="memory-explorer-error" role="alert">{label}读取失败：{error}</p>}</>;
}

export function MemoryTaskSummary({ text, limit = 500 }: { text: string; limit?: number }) {
  const [expanded, setExpanded] = useState(false);
  return <div className="memory-task-summary"><p>{expanded || text.length <= limit ? text : `${text.slice(0, limit)}…`}</p>{text.length > limit && <button type="button" className="memory-summary-toggle" aria-expanded={expanded} onClick={() => setExpanded(value => !value)}>{expanded ? '收起任务说明' : `展开任务说明 · ${text.length.toLocaleString()} 字符`}<ChevronDown size={12} /></button>}</div>;
}

export function RequestEvidenceStatus({ request }: { request: MemoryRequest }) {
  return <div className="memory-evidence-state"><span className={`memory-dispatch dispatch-${request.dispatch_status}`}>{dispatchLabel(request.dispatch_status)}</span><span>{captureLabel(request.capture_stage)}</span>{request.legacy && <span>历史记录</span>}
    {request.dispatch_status === 'prepared_only' && <p>此记录保存了准备输入，尚无发送凭证，不能据此认定模型已经收到。</p>}
    {request.dispatch_status === 'unknown' && <p>此记录缺少可关联的发送凭证；输入内容与模型是否收到分别核对。</p>}
  </div>;
}

export function MemoryReferences({ rows, title }: { rows: InputReference[]; title: string }) {
  return <section className="memory-input-section"><h4>{title}<span>{rows.length} 条</span></h4>{rows.length ? <div className="memory-reference-list">{rows.map((row, index) => <article key={index}>
    <header><code>{row.id == null ? '来源标识未记录' : memoryReadableValue(row.id)}</code>{row.tool_name != null && <code>{memoryReadableValue(row.tool_name)}</code>}<span className={`memory-inclusion inclusion-${inclusionClassName(row.inclusion)}`}>{inclusionLabel(row.inclusion)}</span></header>
    {row.preview != null && row.preview !== '' && <pre className="memory-preview-text">{memoryReadableValue(row.preview)}</pre>}
    <dl className="memory-source-fields">{row.type != null && <div><dt>类型</dt><dd>{memoryReferenceLabel(row.type, 'type')}</dd></div>}{row.source != null && <div><dt>来源</dt><dd>{memoryReferenceLabel(row.source, 'source')}</dd></div>}{row.sha256 != null && <div><dt>版本 SHA256</dt><dd><code>{memoryReadableValue(row.sha256)}</code></dd></div>}{row.score != null && <div><dt>检索相关度</dt><dd>{memoryReadableValue(row.score)}</dd></div>}</dl>
    {row.provenance != null && <details><summary>来源记录</summary><pre className="memory-preview-text">{memoryReadableValue(row.provenance)}</pre></details>}
  </article>)}</div> : <p className="memory-empty-note">没有可关联的{title}记录。</p>}</section>;
}

export function MemoryTextContent({ page, offset, onOffset }: { page: MemoryTextPage; offset: number; onOffset: (offset: number) => void }) {
  if (page.available === false) return <aside className="memory-evidence-note"><p>此记录未保存所选正文部分，无法确认其内容。</p>{page.missing?.map(message => <p key={message}>{memoryGapLabel(message)}</p>)}</aside>;
  const current = page.offset ?? offset;
  return <><div className="memory-text-metadata"><span>{page.redacted === false ? '保存正文' : '脱敏正文'}</span>{page.source && <span>来源：{page.source}</span>}<span>{current + (page.text.length ? 1 : 0)}–{current + page.text.length} / {page.total_chars} 字符</span></div>
    <pre className="memory-full-input" tabIndex={0}>{page.text || '本段暂无正文。'}</pre>
    <nav className="memory-text-pagination" aria-label="正文字符分页"><button type="button" className="btn small" disabled={!current} onClick={() => onOffset(Math.max(0, current - (page.limit ?? 12000)))}><ArrowLeft size={13} />上一段</button><span>正文按字符分段读取</span><button type="button" className="btn small" disabled={!page.has_more || page.next_offset == null} onClick={() => { if (page.next_offset != null) onOffset(page.next_offset); }}>下一段<ArrowRight size={13} /></button></nav>
    {page.sha256 && <p className="memory-text-hash">当前脱敏正文 SHA256：<code>{page.sha256}</code></p>}
  </>;
}

function MemoryTextReader({ runId, id, kind, revision, title }: { runId: string; id: string; kind: 'context' | 'entries' | 'history'; revision: string | number; title: string }) {
  const [open, setOpen] = useState(false);
  const [part, setPart] = useState('combined');
  const [offset, setOffset] = useState(0);
  const page = useRemoteResource<MemoryTextPage>(open ? memoryTextPath(runId, kind, id, offset, part) : null, revision);
  return <details className="memory-text-reader" onToggle={event => setOpen(event.currentTarget.open)}><summary>{title}<ChevronDown size={13} /></summary>{open && <>
    {kind === 'context' && <div className="memory-input-parts" aria-label="选择输入正文部分">{[['combined', '合并正文'], ['system', '系统提示词'], ['user', '任务输入']].map(([value, label]) => <button type="button" key={value} className="btn small" aria-pressed={part === value} onClick={() => { setPart(value); setOffset(0); }}>{label}</button>)}</div>}
    <MemoryReadNotice loading={page.loading} error={page.error} label="正文片段" />
    {page.data && <MemoryTextContent page={page.data} offset={offset} onOffset={setOffset} />}
  </>}</details>;
}

export function MemoryRequestContent({ runId, detail, revision = 0 }: { runId: string; detail: MemoryRequestDetail; revision?: string | number }) {
  const request = detail.request;
  return <article className="memory-request-detail"><header><h3><BrainCircuit size={17} />所选模型请求</h3><span>{requestStatusLabel(request.status)}</span></header>
    <RequestEvidenceStatus request={request} />
    <dl className="memory-request-fields"><div><dt>角色</dt><dd>{memoryRoleName(request.role)} · <code>{request.role}</code></dd></div><div><dt>请求标识</dt><dd><code>{request.id}</code></dd></div><div><dt>任务／分派</dt><dd><code>{request.handoff_id ?? '未记录'}</code></dd></div><div><dt>任务类型</dt><dd>{request.purpose}</dd></div><div><dt>供应商／模型</dt><dd>{request.provider ?? '未记录'} / {request.model ?? '未记录'}</dd></div><div><dt>保存时间</dt><dd>{date(request.created_at, true)}</dd></div>{request.operation_id && <div><dt>模型操作</dt><dd><code>{request.operation_id}</code></dd></div>}{request.context_sha256 && <div><dt>输入版本 SHA256</dt><dd><code>{request.context_sha256}</code></dd></div>}</dl>
    <p className="memory-evidence-note">检索命中、保留的引用标识和完整正文进入输入是不同记录。以下状态依据已保存的输入及关联证据展示，不能代表模型已阅读或验证原始资料。</p>
    {detail.truncated && <p className="memory-evidence-gaps">详情摘录达到展示限额，以下记录仅展示部分内容；请按需读取已捕获的模型输入正文各段。</p>}
    {detail.counts && <p className="memory-empty-note">历史摘录展示 {detail.counts.history_displayed} / {detail.counts.history_total} 条 · 已捕获工具结果 {detail.counts.tool_results_total} 条 · 检索结果展示 {detail.counts.retrieval_results_displayed} 条 · 来源展示 {detail.counts.references_displayed} 条</p>}
    <MemoryReferences rows={detail.history ?? []} title="关联角色历史" />
    <section className="memory-input-section"><h4>已记录的记忆检索<span>{detail.retrievals.length} 次</span></h4>{detail.retrievals.length ? detail.retrievals.map((retrieval, index) => <article className="memory-retrieval" key={index}><header><code>{memoryReadableValue(retrieval.tool_name)}</code><span className={`memory-inclusion inclusion-${inclusionClassName(retrieval.inclusion)}`}>{inclusionLabel(retrieval.inclusion)}</span></header><pre className="memory-preview-text">检索式：{memoryReadableValue(retrieval.query)}</pre><MemoryReferences rows={retrieval.results ?? []} title="检索结果" /></article>) : <p className="memory-empty-note">此请求没有可关联的检索记录。</p>}</section>
    <MemoryReferences rows={detail.references ?? []} title="输入关联的记忆与来源" />
    {detail.missing?.length > 0 && <aside className="memory-evidence-gaps"><h4>记录缺口</h4><ul>{detail.missing.map((message, index) => <li key={index}>{memoryGapLabel(message)}{memoryGapLabel(message) !== message && <code> · {message}</code>}</li>)}</ul></aside>}
    {request.source_ids != null && <details className="memory-text-reader"><summary>输入与来源绑定</summary><pre className="memory-preview-text">{memoryReadableValue(request.source_ids)}</pre></details>}
    {request.context_available ? <MemoryTextReader key={request.id} runId={runId} id={request.id} kind="context" revision={revision} title="按需查看捕获的模型输入（脱敏）" /> : <p className="memory-empty-note">此请求尚无可读取的输入正文。</p>}
  </article>;
}

export function MemoryRequestList({ page, selectedId, loading, onSelect, onOffset }: { page: MemoryPageResult<MemoryRequest>; selectedId: string; loading: boolean; onSelect: (id: string) => void; onOffset: (offset: number) => void }) {
  return <section className="memory-request-list" aria-label="角色模型请求列表"><h3>模型请求<span>{page.total} 条</span></h3>{page.items.length ? <div>{page.items.map(request => <button type="button" key={request.id} className={`memory-request-choice ${selectedId === request.id ? 'selected' : ''}`} aria-pressed={selectedId === request.id} onClick={() => onSelect(request.id)}><strong>{memoryPurposeName(request.purpose)}</strong><span>{dispatchLabel(request.dispatch_status)} · {requestStatusLabel(request.status)}</span><small>{request.model ?? '模型未记录'}</small><time>{date(request.created_at, true)}</time><code>{request.id}</code></button>)}</div> : <p className="memory-empty-note">当前角色或任务没有模型请求记录。</p>}<RecordPagination label="模型请求" {...page} count={page.items.length} loading={loading} onOffset={onOffset} /></section>;
}

function StoredMemoryCard({ runId, entry, revision }: { runId: string; entry: StoredMemoryEntry; revision: string | number }) {
  return <article className="memory-storage-card"><header><strong>{entry.scope === 'shared' ? '共享记忆' : '本研究记忆'}</strong><span>{entry.role ? memoryRoleName(entry.role) : '未指定角色'}</span><time>{date(entry.created_at, true)}</time></header><pre className="memory-preview-text">{entry.text_preview}</pre><MemorySourceCitation memory={{ metadata: entry.metadata } as Json} /><p><code>{entry.id}</code> · {entry.text_chars} 字符</p><MemoryTextReader runId={runId} id={entry.id} kind="entries" revision={revision} title="查看完整记忆（分页）" /></article>;
}

function RoleHistoryCard({ runId, entry, revision }: { runId: string; entry: MemoryHistoryEntry; revision: string | number }) {
  return <article className="memory-storage-card"><header><strong>{memoryPurposeName(entry.purpose)}</strong><time>{date(entry.created_at, true)}</time></header><p>{entry.provider ?? '供应商未记录'} / {entry.model ?? '模型未记录'}</p><pre className="memory-preview-text">{entry.text_preview}</pre>{entry.truncated && <p className="memory-empty-note">此条为历史摘要，全文请按需读取。</p>}<dl className="memory-source-fields"><div><dt>历史记录</dt><dd><code>{entry.id}</code></dd></div><div><dt>关联请求</dt><dd><code>{entry.request_id ?? '未记录'}</code></dd></div>{entry.operation_id && <div><dt>模型操作</dt><dd><code>{entry.operation_id}</code></dd></div>}</dl><MemoryTextReader runId={runId} id={entry.id} kind="history" revision={revision} title="查看完整历史（分页）" /></article>;
}

export function MemoryStudyEntry({ runs, runId = '' }: { runs: RunListItem[]; runId?: string }) {
  const [selected, setSelected] = useState('');
  const id = runId || selected;
  return <section className="panel memory-study-entry" aria-label="研究记忆与上下文入口"><div><BrainCircuit size={21} /><div><h2>研究记忆与模型上下文</h2><p>按智能体和任务追溯角色历史、共享记忆、检索结果与已捕获的模型输入。</p></div></div><div><label><span>选择研究</span><select value={id} disabled={Boolean(runId)} onChange={event => setSelected(event.target.value)}><option value="">请选择研究任务</option>{runs.map(run => <option key={run.id} value={run.id}>{run.title}</option>)}</select></label>{id ? <a className="btn btn-secondary" href={memoryRunHref(id)}>查看记忆与上下文<ArrowRight size={14} /></a> : <span className="memory-empty-note">选择研究后查看具体请求记录。</span>}</div></section>;
}

function MemoryExplorerSession({ runId, revision = 0 }: { runId: string; revision?: string | number }) {
  const [role, setRole] = useState('director');
  const [view, setView] = useState('requests');
  const [refresh, setRefresh] = useState(0);
  const version = `${revision}:${refresh}`;
  const [tasksOpen, setTasksOpen] = useState(false);
  const [taskId, setTaskId] = useState('');
  const [taskOffset, setTaskOffset] = useState(0);
  const [requestOffset, setRequestOffset] = useState(0);
  const [requestId, setRequestId] = useState('');
  const [entryOffset, setEntryOffset] = useState(0);
  const [historyOffset, setHistoryOffset] = useState(0);
  const [scope, setScope] = useState('all');
  const [entryRole, setEntryRole] = useState('');
  const [query, setQuery] = useState('');
  const [submittedQuery, setSubmittedQuery] = useState('');
  const queryLabelId = useId();
  const overview = useRemoteResource<MemoryOverview>(memoryExplorerPath(runId, 'overview'), version);
  const tasks = useRemoteResource<MemoryPageResult<MemoryTask>>(tasksOpen && view === 'requests' ? memoryExplorerPath(runId, 'tasks', { role, offset: taskOffset }) : null, version);
  const requests = useRemoteResource<MemoryPageResult<MemoryRequest>>(view === 'requests' ? memoryExplorerPath(runId, 'requests', { role, handoff_id: taskId, offset: requestOffset }) : null, version);
  useEffect(() => {
    if (view === 'requests' && !requests.loading && !requests.error && requests.data) {
      const items = requests.data.items;
      setRequestId(previous => selectMemoryRequestId(previous, items));
    }
  }, [view, requests.loading, requests.error, requests.data]);
  const selectedRequest = !requests.loading && !requests.error && requests.data ? selectMemoryRequestId(requestId, requests.data.items) : requestId;
  const selected = useRemoteResource<MemoryRequestDetail>(view === 'requests' && selectedRequest ? memoryRequestDetailPath(runId, selectedRequest) : null, version);
  const savedDetails = useRef(new Map<string, MemoryRequestDetail>());
  if (selected.data?.request.id === selectedRequest) savedDetails.current.set(selectedRequest, selected.data);
  const detail = selected.data ?? savedDetails.current.get(selectedRequest);
  const entries = useRemoteResource<MemoryPageResult<StoredMemoryEntry>>(view === 'entries' ? memoryEntriesPath(runId, scope, entryRole, submittedQuery, entryOffset) : null, version);
  const history = useRemoteResource<MemoryPageResult<MemoryHistoryEntry>>(view === 'history' ? memoryExplorerPath(runId, 'history', { role, offset: historyOffset }) : null, version);
  const current = overview.data?.roles.find(agent => agent.role === role);
  const chooseTask = (id: string) => { setTaskId(id); setRequestId(''); setRequestOffset(0); };
  const chooseRole = (next: string) => { setRole(next); setTaskId(''); setRequestId(''); setTaskOffset(0); setRequestOffset(0); setEntryOffset(0); setHistoryOffset(0); };
  return <div className="memory-explorer"><header className="memory-explorer-heading"><div><h2><BrainCircuit size={20} />记忆与上下文</h2><p>从角色历史到检索结果，再核对单次模型请求保存了哪些输入。</p></div><button type="button" className="btn small" onClick={() => setRefresh(value => value + 1)}><RefreshCw size={14} />刷新记录</button></header>
    <div className="memory-role-tabs" aria-label="选择记忆所属智能体">{Object.entries(memoryRoleNames).map(([id, name]) => <button type="button" key={id} className={`memory-role-${id}`} aria-pressed={role === id} onClick={() => chooseRole(id)}><strong>{name}</strong><code>{id}</code>{overview.data && <small>{overview.data.roles.find(agent => agent.role === id)?.request_count ?? 0} 条请求</small>}</button>)}</div>
    <MemoryReadNotice loading={overview.loading} error={overview.error} label="角色记忆概况" />
    {current && <section className="memory-current-task"><div><h3>当前任务<span>{current.current_task ? requestStatusLabel(current.current_task.status) : '未记录当前任务'}</span></h3>{current.current_task && <h4>{memoryPurposeName(current.current_task.purpose)}</h4>}<MemoryTaskSummary text={current.current_task?.summary || '此角色没有已记录的当前任务。可查看历史模型请求与角色历史。'} />{current.current_task && <code>{current.current_task.id}</code>}</div>{current.current_task && <button type="button" className="btn small" onClick={() => { setView('requests'); chooseTask(current.current_task!.id); }}>查看此任务的请求</button>}</section>}
    <nav className="memory-view-tabs" aria-label="选择记忆视图">{[['requests', '请求输入与检索', BrainCircuit], ['history', '角色历史', History], ['entries', '持久与共享记忆', Database]].map(([id, label, Icon]) => { const ViewIcon = Icon as typeof BrainCircuit; return <button type="button" key={String(id)} className="btn small" aria-pressed={view === id} onClick={() => setView(String(id))}><ViewIcon size={15} />{String(label)}</button>; })}</nav>
    {view === 'requests' && <>
      <div className="memory-task-filter"><span>{taskId ? <>筛选任务：<code>{taskId}</code></> : '查看此角色全部任务的请求'}</span>{taskId && <button type="button" className="btn small" onClick={() => chooseTask('')}>全部任务</button>}<button type="button" className="btn small" aria-expanded={tasksOpen} onClick={() => setTasksOpen(value => !value)}>选择历史任务<ChevronDown size={13} /></button></div>
      {tasksOpen && <section className="memory-task-list" aria-label="角色历史任务"><MemoryReadNotice loading={tasks.loading} error={tasks.error} label="历史任务" />{tasks.data && <><div>{tasks.data.items.map(task => <article key={task.id} className="memory-task-choice"><button type="button" className="memory-task-select" aria-pressed={taskId === task.id} onClick={() => chooseTask(task.id)}><strong>{memoryPurposeName(task.purpose)}</strong><span>{requestStatusLabel(task.status)}</span></button><MemoryTaskSummary text={task.summary || '未记录任务说明。'} limit={300} /><code>{task.id}</code></article>)}</div>{!tasks.data.items.length && <p className="memory-empty-note">此角色暂无历史任务记录。</p>}<RecordPagination label="历史任务" {...tasks.data} count={tasks.data.items.length} loading={tasks.loading} onOffset={setTaskOffset} /></>}</section>}
      <MemoryReadNotice loading={requests.loading} error={requests.error} label="模型请求列表" />
      <div className="memory-request-columns">{requests.data ? <MemoryRequestList page={requests.data} selectedId={selectedRequest} loading={requests.loading} onSelect={setRequestId} onOffset={offset => { setRequestOffset(offset); setRequestId(''); }} /> : <div />}<div><MemoryReadNotice loading={selected.loading} error={selected.error} label="模型请求详情" />{detail && <MemoryRequestContent key={selectedRequest} runId={runId} detail={detail} revision={version} />}{!selectedRequest && <p className="memory-empty-note">选择一个模型请求后，可查看检索记录与输入证据。</p>}</div></div>
    </>}
    {view === 'history' && <><p className="memory-evidence-note">本角色保存的公开输出与决策历史；是否用于某次模型输入请在该请求详情中核对。</p><MemoryReadNotice loading={history.loading} error={history.error} label="角色历史" />{history.data && <><div className="memory-storage-list">{history.data.items.map(entry => <RoleHistoryCard key={entry.id} runId={runId} entry={entry} revision={version} />)}</div>{!history.data.items.length && <p className="memory-empty-note">该角色尚无已保存的历史内容。</p>}<RecordPagination label="角色历史" {...history.data} count={history.data.items.length} loading={history.loading} onOffset={setHistoryOffset} /></>}</>}
    {view === 'entries' && <><div className="memory-storage-counts"><span>本研究 {overview.data?.memory_counts.run ?? '未读取'} 条</span><span>共享 {overview.data?.memory_counts.shared ?? '未读取'} 条</span><span>保存数量与本次模型输入数量分别核对。</span></div><p className="memory-evidence-note">上方角色选择用于请求与历史。这里默认包含全部写入来源角色，角色筛选不代表私有访问权限。关键词只过滤已保存文字，不会触发智能体检索。当前任务实际检索了哪些内容，请查看所选请求的检索记录。</p><form className="memory-entry-search" onSubmit={event => { event.preventDefault(); setSubmittedQuery(query.trim()); setEntryOffset(0); }}><label><span>记忆范围</span><select value={scope} onChange={event => { setScope(event.target.value); setEntryOffset(0); }}><option value="all">本研究与共享</option><option value="run">仅本研究</option><option value="shared">仅共享记忆</option></select></label><label><span>写入来源角色</span><select value={entryRole} onChange={event => { setEntryRole(event.target.value); setEntryOffset(0); }}><option value="">全部来源角色（含文献索引）</option>{Object.entries(memoryRoleNames).map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select></label><div><label htmlFor={queryLabelId}>筛选保存的记忆文字</label><div className="memory-entry-search-controls"><input id={queryLabelId} type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder="输入关键词后按 Enter 筛选" /><button className="btn small" type="submit"><Search size={14} />筛选</button></div></div></form><MemoryReadNotice loading={entries.loading} error={entries.error} label="持久记忆" />{entries.data && <><div className="memory-storage-list">{entries.data.items.map(entry => <StoredMemoryCard key={entry.id} runId={runId} entry={entry} revision={version} />)}</div>{!entries.data.items.length && <p className="memory-empty-note">当前来源角色、范围或关键词下没有保存的记忆。</p>}<RecordPagination label="持久记忆" {...entries.data} count={entries.data.items.length} loading={entries.loading} onOffset={setEntryOffset} /></>}</>}
  </div>;
}

export function MemoryExplorer(props: { runId: string; revision?: string | number }) {
  return <MemoryExplorerSession key={props.runId} {...props} />;
}
