import { lazy, Suspense, useState } from 'react';
import { BookOpen, Download } from 'lucide-react';
import { textOf, type Json } from './api';
import { ReferenceNames, ReferenceText } from './ReferenceNames';
import { useRemoteResource } from './useRemoteResource';
import { documentVersionKey, type DocumentVersion } from './documentVersions';
const MarkdownReport = lazy(() => import('./MarkdownReport'));
const DocumentVersionPreview = lazy(() => import('./DocumentVersionPreview').then(module => ({ default: module.DocumentVersionPreview })));

export function IndexedDocument({ runId, document: doc, label = '研究文档', versions }: { runId: string; document: Json; label?: string; versions?: DocumentVersion[] }) {
  const scope = JSON.stringify([runId, doc.document_id, doc.sha256]);
  const [preview, setPreview] = useState(false);
  const [selection, setSelection] = useState<{ scope: string; section: string; offset: number; retry: number } | null>(null);
  const selected = selection?.scope === scope ? selection : null;
  const params = selected ? new URLSearchParams({ expected_sha256: doc.sha256, section_id: selected.section, offset: String(selected.offset), limit: '4000' }) : null;
  const resource = useRemoteResource<Json>(params ? `/runs/${encodeURIComponent(runId)}/documents/${encodeURIComponent(doc.document_id)}?${params}` : null, `${scope}:${selected?.retry ?? 0}`, 0, true);
  const page = resource.data;
  const loading = resource.loading;
  const error = resource.error || (page && (page.sha256 !== doc.sha256 || page.document_id !== doc.document_id) ? '返回章节与所选文档版本不一致' : '');
  function read(section: string, offset = 0) { setSelection(previous => ({ scope, section, offset, retry: previous?.section === section && previous.offset === offset ? previous.retry + 1 : 0 })); }
  const visiblePage = page?.document_id === doc.document_id && page?.sha256 === doc.sha256 ? page : null;
  return <ReferenceNames records={doc}><div className="indexed-document"><p className="muted"><ReferenceText value={doc.title || `${label}文档`} /></p><div className="document-preview-actions"><button type="button" className="btn small" aria-expanded={preview} onClick={() => setPreview(value => !value)}><BookOpen size={14} />{preview ? '收起文档预览' : '预览文档与修订'}</button><a className="btn small" href={`/api/runs/${encodeURIComponent(runId)}/documents/${encodeURIComponent(doc.document_id)}/file?expected_sha256=${encodeURIComponent(doc.sha256)}`} download><Download size={14} />下载{label} Markdown</a></div>
    {preview && <Suspense fallback={<p role="status">正在打开文档预览…</p>}><DocumentVersionPreview runId={runId} title={`${label}文档`} initialVersionKey={documentVersionKey(doc)} versions={versions?.length ? versions : [{ key: documentVersionKey(doc), document: doc, revision: 0, label: '当前已保存文档' }]} /></Suspense>}
    <details><summary>按文档索引查看章节 · {doc.sections.length} 个条目</summary><div className="architecture-section-list">{doc.sections.map((s: Json) => <button className="btn small" key={s.id} disabled={loading} onClick={() => void read(s.id)}><ReferenceText value={s.title} /> · {s.chars} 字符</button>)}</div>
      {error && <p role="alert"><ReferenceText value={error} /></p>}{loading && <p role="status">正在读取章节…</p>}
      {visiblePage && <article aria-label={`${label}文档章节`}><small><ReferenceText value={doc.title || `${label}文档`} /> · <ReferenceText value={visiblePage.title} /> · 字符 {visiblePage.char_start}–{visiblePage.char_end}</small><Suspense fallback={<p>正在显示章节…</p>}><MarkdownReport text={textOf(visiblePage.text)} /></Suspense>{visiblePage.next_offset != null && <button className="btn small" onClick={() => void read(visiblePage.section_id, visiblePage.next_offset)}>读取本章节下一段</button>}</article>}
    </details>
  </div></ReferenceNames>;
}
