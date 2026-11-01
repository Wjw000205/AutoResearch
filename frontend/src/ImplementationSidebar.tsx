import { RefreshCw } from 'lucide-react';
import { lazy, Suspense, useId, useState } from 'react';
import { date, type Json } from './api';
import { ImplementationProgress } from './ImplementationProgress';
import { ImplementationFlow, sidebarDetailMatches } from './ImplementationFlow';
import { useRemoteResource } from './useRemoteResource';
import { selectSidebarReadingDocument } from './implementationReadingSelection';
import { ImplementationDocumentActions } from './ImplementationDocumentAccess';
import { ViewBoundary } from './ViewBoundary';
const ImplementationVersionReader = lazy(() => import('./ImplementationVersionReader').then(module => ({ default: module.ImplementationVersionReader })));

export function ImplementationSidebar({ progress, runId, localCodex }: { progress?: Json | null; runId: string; localCodex?: Json | null }) {
  const [refresh, setRefresh] = useState(0);
  const [selection, setSelection] = useState<{ runId: string; reading: boolean } | null>(null);
  const panelId = useId();
  const resource = useRemoteResource<Json>(progress?.run_id === runId ? `/runs/${encodeURIComponent(runId)}/implementation-progress` : null,
    `${runId}:${progress?.workflow?.id ?? ''}:${progress?.workflow?.revision ?? ''}:${progress?.workflow?.document_sha256 ?? ''}:${refresh}`, progress?.run_status === 'running' ? 5000 : 15000);
  if (!progress || progress.run_id !== runId) return null;
  const matches = sidebarDetailMatches(progress, resource.data, runId);
  const reading = selection?.runId === runId && selection.reading;
  const detail = matches ? resource.data?.details : null;
  const readingDocument = selectSidebarReadingDocument(progress, resource.data, runId);
  const documentRevision = detail?.document?.revision ?? progress.workflow?.document_revision
    ?? (['document_authoring', 'document_review'].includes(progress.phase) ? progress.workflow?.revision : null);
  const updatedAt = matches ? resource.data?.updated_at : progress.updated_at;
  const runState = { running: '进行中', blocked: '等待处理', paused: '已暂停', failed: '执行失败', cancelled: '已取消', completed: '已完成' }[progress.run_status as string] ?? '状态待核对';
  return <section className="implementation-sidebar" aria-label="实现进度侧栏">
    <header className="implementation-sidebar-heading"><div><h3>{progress.label}</h3>
      <span className={`implementation-pill state-${progress.run_status}`}>{runState}</span>
      {Number.isInteger(documentRevision) && <span className="implementation-pill">文档审核第 {documentRevision + 1} 轮</span>}</div>
      <div className="implementation-sidebar-status"><span>{resource.error ? '更新失败，保留已保存记录' : updatedAt ? `更新于 ${date(updatedAt, true)}` : '正在同步进度'}</span>
        <button className="btn small" type="button" onClick={() => setRefresh(value => value + 1)} disabled={resource.loading} aria-label="刷新实现进度"><RefreshCw size={13} />刷新</button></div>
      {progress.next_action && <p className="implementation-next-action"><strong>下一步</strong><span>{progress.next_action}</span></p>}
    </header>
    {resource.error && <p role="alert" className="inline-warning">{resource.error}</p>}
    <div className="tabs implementation-reading-tabs" role="tablist" aria-label="代码实现详情内容">
      <button type="button" role="tab" id={`${panelId}-progress-tab`} aria-selected={!reading} aria-controls={`${panelId}-content`} onClick={() => setSelection({ runId, reading: false })}>流程进度</button>
      <button type="button" role="tab" id={`${panelId}-reading-tab`} aria-selected={!!reading} aria-controls={`${panelId}-content`} onClick={() => setSelection({ runId, reading: true })}>阅读实现文档</button>
    </div>
    <div role="tabpanel" id={`${panelId}-content`} aria-labelledby={`${panelId}-${reading ? 'reading' : 'progress'}-tab`}>
      {reading ? readingDocument ? <><ImplementationDocumentActions runId={runId} readingDocument={readingDocument} expanded
          controls={`${panelId}-content`} onPreview={() => setSelection({ runId, reading: true })} />
        <ViewBoundary key={`${runId}:${progress.workflow?.id}:${readingDocument.document.sha256}`} label="实现文档预览" reloadPage><Suspense fallback={<p role="status">正在打开文档阅读器…</p>}><ImplementationVersionReader key={`${runId}:${progress.workflow?.id}`} runId={runId} workflowId={progress.workflow?.id} readingDocument={readingDocument} localCodex={localCodex} /></Suspense></ViewBoundary></>
        : resource.loading ? <p role="status">正在核对文档版本…</p>
          : <div role={resource.error || !matches ? 'alert' : 'status'}><p>{resource.error ? '文档目录读取失败，请重新读取。' : !matches ? '文档版本信息尚未同步，请重新读取目录。' : '实现文档尚未保存，保存后可在这里在线预览和下载。'}</p>
            <button type="button" className="btn small" onClick={() => setRefresh(value => value + 1)}>重新读取文档目录</button></div>
        : <ImplementationFlow progress={progress} detail={detail} runId={runId} />}
    </div>
    {!reading && readingDocument && <details className="implementation-receipts"><summary>实现文档与版本信息</summary>
      <ImplementationDocumentActions runId={runId} readingDocument={readingDocument} expanded={false}
        controls={`${panelId}-content`} onPreview={() => setSelection({ runId, reading: true })} /></details>}
    <details className="implementation-receipts"><summary>读取、保存与流程回执</summary><ImplementationProgress progress={progress} runId={runId} /></details>
  </section>;
}
