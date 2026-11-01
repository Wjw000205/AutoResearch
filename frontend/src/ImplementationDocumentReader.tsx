import { useEffect, useState } from 'react';
import { BookOpen, Download, FileText } from 'lucide-react';
import { type Json } from './api';
import MarkdownReport from './MarkdownReport';
import { implementationFieldLabel, implementationSectionTitle, splitImplementationContent, validateImplementationText } from './implementationDocumentContent';
import { implementationReadingBlocks } from './implementationReadingTables';
import { ImplementationReadingProse } from './ImplementationReadingProse';
import { ViewBoundary } from './ViewBoundary';
import './implementation-document.css';

const LIMIT = 4 * 1024 * 1024;
const fileUrl = (runId: string, doc: Json) => `/api/runs/${encodeURIComponent(runId)}/documents/${encodeURIComponent(doc.document_id)}/file?expected_sha256=${encodeURIComponent(doc.sha256)}`;

/** Human reading uses the existing complete-file export; agent read windows stay bounded. */
export async function readVerifiedImplementationDocument(runId: string, doc: Json, signal: AbortSignal,
  request: typeof fetch = fetch): Promise<Json[]> {
  if (typeof doc.document_id !== 'string' || !doc.document_id || !/^[a-f0-9]{64}$/.test(doc.sha256)) throw new Error('文档版本标识不完整，请刷新目录。');
  const response = await request(fileUrl(runId, doc), { signal, cache: 'no-store' });
  if (!response.ok) throw new Error('文档读取失败，请刷新目录后重试。');
  const buffer = await response.arrayBuffer();
  if (signal.aborted) throw new DOMException('Aborted', 'AbortError');
  if (buffer.byteLength > LIMIT) throw new Error('文档超过阅读大小上限，请下载原文查看。');
  const hash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', buffer)), byte => byte.toString(16).padStart(2, '0')).join('');
  if (signal.aborted) throw new DOMException('Aborted', 'AbortError');
  if (hash !== doc.sha256) throw new Error('正文与所选文档版本不一致，请重新读取目录。');
  let text: string;
  try { text = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(buffer); }
  catch { throw new Error('文档不是完整的 UTF-8 正文，请下载原文核对。'); }
  return validateImplementationText(text, doc);
}

const literalFields = new Set(['id', 'requirement_id', 'document_id', 'section_id', 'sha256', 'expected_sha256',
  'spec_hash', 'proposal_hash', 'design_hash', 'document_sha256', 'code_sha256', 'delivery_hash', 'location', 'locations']);
function ReadingMarkdown({ text }: { text: string }) {
  return <>{implementationReadingBlocks(text).map((block, index) => block.type === 'markdown'
    ? <ImplementationReadingProse key={index} text={block.text} />
    : <div className="implementation-readable-table implementation-projected-table" key={index} tabIndex={0} role="region" aria-label="原文表格，可横向滚动"><table>
      <thead><tr>{block.headers.map((header, column) => <th key={column} scope="col"><MarkdownReport text={header} preserveSourceText /></th>)}</tr></thead>
      <tbody>{block.rows.map((row, n) => <tr key={n}>{row.map((cell, column) => <td key={column}><MarkdownReport text={cell} preserveSourceText /></td>)}</tr>)}</tbody>
    </table></div>)}</>;
}
function Value({ value, depth = 0, field }: { value: unknown; depth?: number; field?: string }) {
  if (value === null) return <code>null</code>;
  if (typeof value === 'boolean' || typeof value === 'number') return <code>{Object.is(value, -0) ? '-0' : String(value)}</code>;
  if (typeof value === 'string') return field && literalFields.has(field) ? <code>{value}</code> : <ReadingMarkdown text={value} />;
  if (depth > 8) return <pre>{JSON.stringify(value, null, 2)}</pre>;
  if (Array.isArray(value)) {
    if (!value.length) return <span className="muted">空列表</span>;
    const columns = ['id', 'requirement', 'verification'];
    const mapping = ['requirement_id', 'location', 'behavior'];
    const keys = value.every(row => row && typeof row === 'object' && !Array.isArray(row)
      && columns.every(key => Object.hasOwn(row, key)) && Object.keys(row).every(key => columns.includes(key))) ? columns
      : value.every(row => row && typeof row === 'object' && !Array.isArray(row)
        && mapping.every(key => Object.hasOwn(row, key)) && Object.keys(row).every(key => mapping.includes(key))) ? mapping : null;
    if (keys) return <div className="implementation-readable-table"><table><thead><tr>{keys.map(key => <th key={key}>{implementationFieldLabel(key)}</th>)}</tr></thead><tbody>{value.map((row, n) => <tr key={n}>{keys.map(key => <td key={key}>{Object.hasOwn(row, key) ? <Value value={row[key]} depth={depth + 1} field={key} /> : '—'}</td>)}</tr>)}</tbody></table></div>;
    return <ol className="implementation-readable-items">{value.map((item, index) => <li key={index}><Value value={item} depth={depth + 1} field={field} /></li>)}</ol>;
  }
  if (value && typeof value === 'object') {
    const entries = Object.entries(value);
    if (!entries.length) return <span className="muted">空对象</span>;
    return <dl className="implementation-readable-fields">{entries.map(([key, item]) => <div key={key}><dt>{implementationFieldLabel(key)}{implementationFieldLabel(key) !== key && <code>{key}</code>}</dt><dd><Value value={item} depth={depth + 1} field={key} /></dd></div>)}</dl>;
  }
  return <span>{String(value)}</span>;
}

export function ImplementationDocumentBody({ text }: { text: string }) {
  return <div className="implementation-document-body">{splitImplementationContent(text).map((block, index) => block.type === 'structured'
    ? <section className="implementation-readable-block" key={index}><Value value={block.value} /><details className="implementation-raw-block"><summary>查看对应原始 JSON</summary><pre>{block.source}</pre></details></section>
    : <ReadingMarkdown key={index} text={block.text} />)}</div>;
}

export function ImplementationDocumentReader({ runId, document: doc, revision, current }: {
  runId: string; document: Json; revision: number; current: boolean;
}) {
  const scope = JSON.stringify([runId, doc.document_id, doc.sha256]);
  const [state, setState] = useState<{ scope: string; sections: Json[]; error: string; loading: boolean }>({ scope, sections: [], error: '', loading: true });
  const [choice, setChoice] = useState<{ scope: string; id: string } | null>(null);
  const [raw, setRaw] = useState(false);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let active = true, timedOut = false;
    const controller = new AbortController();
    const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, 20000);
    setState({ scope, sections: [], error: '', loading: true });
    void readVerifiedImplementationDocument(runId, doc, controller.signal).then(sections => {
      if (active && !controller.signal.aborted) setState({ scope, sections, error: '', loading: false });
    }).catch(error => {
      if (active && (!controller.signal.aborted || timedOut)) setState({ scope, sections: [], error: timedOut ? '读取超时，请重试。' : (error as Error).message, loading: false });
    }).finally(() => clearTimeout(timeout));
    return () => { active = false; clearTimeout(timeout); controller.abort(); };
  }, [scope, retry]); // The immutable document identity controls cancellation and reuse.
  const sections = state.scope === scope ? state.sections : [];
  const selected = sections.find(section => choice?.scope === scope && section.id === choice.id)
    ?? sections.find(section => ['summary', '实现概览', '实现目标与范围'].includes(section.title)) ?? sections[0];
  const originalTitle = selected?.title ?? '';
  const title = implementationSectionTitle(originalTitle);
  const displayText = selected ? selected.text.replace(/^(#{1,3}) ([^\r\n]+)/, (_whole: string, level: string) => `${level} ${title}`) : '';
  return <section className="implementation-document-reader" aria-label="实现文档阅读">
    <header className="implementation-document-heading"><div><h4><BookOpen size={18} />实现文档</h4><p>第 {revision + 1} 版 · {current ? '当前已保存版本' : '最新已保存版本；当前修订稿尚未保存'}</p></div>
      <a className="btn small" href={fileUrl(runId, doc)} download><Download size={14} />下载原文</a></header>
    {(state.scope !== scope || state.loading) && <p role="status">正在读取完整文档…</p>}
    {state.scope === scope && state.error && <div role="alert"><p>{state.error}</p><button className="btn small" onClick={() => setRetry(value => value + 1)}>重新读取文档</button></div>}
    {!!sections.length && <div className="implementation-reading-layout"><nav aria-label="实现文档目录"><h5>文档目录</h5>{sections.map((section, index) => <button type="button" key={section.id} aria-current={selected?.id === section.id ? 'page' : undefined}
      onClick={() => { setChoice({ scope, id: section.id }); setRaw(false); }}><span>{String(index + 1).padStart(2, '0')}</span>{implementationSectionTitle(section.title)}</button>)}</nav>
      <article className="implementation-reading-page" aria-label="实现文档正文"><header><h4>{title}</h4><button className="btn small" aria-pressed={raw} onClick={() => setRaw(value => !value)}><FileText size={13} />{raw ? '阅读排版' : '原始 Markdown'}</button></header>
        {raw ? <pre className="implementation-source-text">{selected.text}</pre> : <ViewBoundary key={`${scope}:${selected.id}`} label="实现文档阅读排版"><ImplementationDocumentBody text={displayText} /></ViewBoundary>}
        <details className="implementation-document-source"><summary>版本与原文定位</summary><dl><dt>原章节</dt><dd>{originalTitle} · <code>{selected.id}</code></dd><dt>正文范围</dt><dd>字符 {selected.start}–{selected.end}</dd><dt>文档标识</dt><dd><code>{doc.document_id}</code></dd><dt>SHA256</dt><dd><code>{doc.sha256}</code></dd></dl></details>
        <div className="implementation-reading-navigation">{sections.indexOf(selected) > 0 && <button className="btn small" onClick={() => { setChoice({ scope, id: sections[sections.indexOf(selected) - 1].id }); setRaw(false); }}>上一章</button>}
          {sections.indexOf(selected) < sections.length - 1 && <button className="btn small" onClick={() => { setChoice({ scope, id: sections[sections.indexOf(selected) + 1].id }); setRaw(false); }}>下一章</button>}</div>
      </article></div>}
  </section>;
}
