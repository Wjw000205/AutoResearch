import { useState } from 'react';
import type { Json } from './api';
import { useRemoteResource } from './useRemoteResource';
import { DocumentVersionPreview } from './DocumentVersionPreview';
import { documentVersionKey, implementationDocumentVersions, implementationVersionDirectorySelection } from './documentVersions';

export function ImplementationVersionReader({ runId, workflowId, readingDocument, localCodex }: {
  runId: string; workflowId: string; readingDocument: { document: Json; revision: number; current: boolean }; localCodex?: Json | null;
}) {
  const [refresh, setRefresh] = useState(0);
  const { supplied, revision } = implementationVersionDirectorySelection(localCodex, workflowId, readingDocument, refresh);
  const resource = useRemoteResource<Json>(!supplied ? `/runs/${encodeURIComponent(runId)}/local-codex` : null, revision, 0, true);
  const versions = implementationDocumentVersions(supplied ? localCodex : resource.data, workflowId, readingDocument);
  return <>
    {!readingDocument.current && <p className="inline-info">以下为此前已保存版本；当前版本信息可能仍在同步，或新修订稿尚未保存。</p>}
    {!supplied && resource.loading && <p role="status">正在读取已保存版本目录…</p>}
    {!supplied && resource.error && <p role="alert">历史版本目录读取失败：{resource.error} <button className="btn small" onClick={() => setRefresh(value => value + 1)}>重新读取版本目录</button></p>}
    <DocumentVersionPreview runId={runId} versions={versions} initialVersionKey={documentVersionKey(readingDocument.document)} title="实现文档" />
  </>;
}
