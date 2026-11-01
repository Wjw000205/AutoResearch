import { BookOpen, Download, FileText } from 'lucide-react';
import { lazy, Suspense, useId, useState } from 'react';
import type { Json } from './api';
import { selectCurrentImplementationDocument } from './implementationReadingSelection';
import { ViewBoundary } from './ViewBoundary';
import './implementation-document.css';
import { DocumentDeliverySummary } from './DocumentDeliverySummary';

const ImplementationVersionReader = lazy(() => import('./ImplementationVersionReader').then(module => ({ default: module.ImplementationVersionReader })));
type ReadingDocument = { document: Json; revision: number; current: boolean };

export function ImplementationDocumentActions({ runId, readingDocument, onPreview, expanded, controls }: {
  runId: string; readingDocument: ReadingDocument; onPreview: () => void; expanded: boolean; controls: string;
}) {
  const { document: doc, revision, current } = readingDocument;
  const downloadUrl = `/api/runs/${encodeURIComponent(runId)}/documents/${encodeURIComponent(doc.document_id)}/file?expected_sha256=${encodeURIComponent(doc.sha256)}`;
  return <section className="implementation-document-access" aria-label="实现文档预览与下载">
    <div className="implementation-document-access-title"><FileText size={21} /><div><h4>实现文档 <span>审核第 {revision + 1} 轮</span></h4>
      <p>{current ? '当前已保存版本' : '此前已保存版本 · 当前版本信息待同步或新稿尚未保存'} · 在线阅读全文、切换版本和查看修订</p></div></div>
    <div className="implementation-document-access-actions">
      <button type="button" className="btn btn-secondary" aria-expanded={expanded} aria-controls={controls} onClick={onPreview}><BookOpen size={15} />在线预览</button>
      <a className="btn" href={downloadUrl} download><Download size={15} />下载实现文档 <small>Markdown</small></a>
    </div>
    <details className="implementation-document-identity"><summary>保存位置、文件版本与校验信息</summary>
      <DocumentDeliverySummary runId={runId} document={doc} reviewRound={revision + 1} /></details>
  </section>;
}

/** Put the current saved text ahead of the workflow's accumulated review history. */
export function CurrentImplementationDocumentPanel({ runId, localCodex }: { runId: string; localCodex?: Json | null }) {
  const readingDocument = selectCurrentImplementationDocument(localCodex, runId);
  const [previewScope, setPreviewScope] = useState('');
  const panelId = useId();
  if (!readingDocument) return null;
  const scope = `${runId}:${readingDocument.workflowId}`;
  const preview = previewScope === scope;
  return <div className="implementation-current-document">
    <ImplementationDocumentActions runId={runId} readingDocument={readingDocument} expanded={preview} controls={panelId}
      onPreview={() => setPreviewScope(preview ? '' : scope)} />
    <div id={panelId}>{preview && <ViewBoundary key={`${scope}:${readingDocument.document.sha256}`} label="实现文档预览" reloadPage><Suspense fallback={<p role="status">正在打开在线预览…</p>}>
      <ImplementationVersionReader key={scope} runId={runId} workflowId={readingDocument.workflowId} readingDocument={readingDocument}
        localCodex={{ ...localCodex, workflows: [localCodex!.current] }} />
    </Suspense></ViewBoundary>}</div>
  </div>;
}
