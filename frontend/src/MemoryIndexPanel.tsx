import { ChevronDown, CircleAlert, Database, ExternalLink, FileText, LoaderCircle, RefreshCw } from 'lucide-react';
import { date, safeUrl, textOf, type Json } from './api';
import { HumanMessage } from './localization';
import { ReferenceNames, ReferenceText } from './ReferenceNames';
import { RecordPagination } from './RecordPagination';

export interface MemoryIndexedDocument {
  id: string; memory_document_id: string; run_id?: string | null; source_id?: string;
  title?: string; url?: string; content_sha256?: string; text_sha256?: string;
  chunk_count: number; index_status: string; pages_total?: number | null; pages_extracted?: number | null;
  empty_pages?: number[]; text_truncated?: boolean; created_at?: string;
}
export interface MemoryIndex {
  index_version: string | number; method: string;
  stats: { chunks: number; documents: number; indexed_chunks: number };
  documents: MemoryIndexedDocument[];
  documents_total?: number; documents_offset?: number; documents_limit?: number;
}

export const memoryIndexPath = (runId: string, offset = 0) => `/memory/index?offset=${offset}&limit=20${runId ? `&run_id=${encodeURIComponent(runId)}` : ''}`;
const statuses: Record<string, string> = { indexed: '已建索引', partial: '部分文本已索引', needs_ocr: '需文字识别提取', empty: '未提取到文本' };
const pageCount = (value: unknown) => typeof value === 'number' && Number.isInteger(value) && value >= 0 ? String(value) : '未记录';

export function MemoryIndexPanel({ data, loading = false, error = '', onRefresh, onOffset }: {
  data?: MemoryIndex | null; loading?: boolean; error?: string; onRefresh?: () => void; onOffset?: (offset: number) => void;
}) {
  const documents = Array.isArray(data?.documents) ? data.documents : [];
  return <ReferenceNames records={data}><section className="memory-index-panel" aria-label="当前筛选的持久化文献索引（含共享）"><header><div><Database size={18} /><h2>文献与 PDF 记忆索引</h2></div>{onRefresh && <button type="button" className="btn small" disabled={loading} onClick={onRefresh}>{loading ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />}刷新索引</button>}</header><p>当前筛选包含共享来源。来源文本分块后保存到本地索引，用于检索和溯源；文献真实性与研究结论仍需另行核验。</p>{error && <div className="memory-index-error" role="alert"><CircleAlert size={14} />索引状态暂不可用。<ReferenceText value={error} /></div>}
    {data && <><div className="memory-index-stats"><div><strong>{data.stats.documents}</strong><span>来源文档</span></div><div><strong>{data.stats.chunks}</strong><span>记忆片段</span></div><div><strong>{data.stats.indexed_chunks}</strong><span>已索引片段</span></div></div><div className="memory-index-method"><span>检索方法：</span><HumanMessage text={data.method} /><span>索引版本：{data.index_version}</span></div>{documents.length ? <div className="memory-index-documents"><p className="memory-index-count">当前显示 {documents.length} / {data.documents_total ?? data.stats.documents} 份文档</p>{documents.map(document => <article className={`memory-index-document index-${document.index_status}`} key={document.memory_document_id || document.id}><div className="memory-index-document-heading"><FileText size={16} /><h3><ReferenceText value={document.title || '未命名来源文档'} /></h3><span>{statuses[document.index_status] ?? '索引状态未识别'}</span></div><div className="memory-index-document-meta"><span>{document.chunk_count} 个片段</span><span>有文本页 {pageCount(document.pages_extracted)} / 总页数 {pageCount(document.pages_total)}</span>{document.created_at && <time>{date(document.created_at, true)}</time>}</div>{document.text_truncated && <p className="memory-extraction-note">提取文本已截断，当前索引未覆盖全文。</p>}{document.index_status === 'needs_ocr' && <p className="memory-extraction-note">此来源尚需文字识别；当前没有可用于检索的文字内容。</p>}{Array.isArray(document.empty_pages) && document.empty_pages.length > 0 && <p className="memory-extraction-note">未提取出文字的页码：<ReferenceText value={document.empty_pages.join('、')} /></p>}<div className="memory-index-document-source"><span>来源文档：<ReferenceText value={document.title || '未命名来源文档'} /></span>{safeUrl(document.url) && <a href={safeUrl(document.url)} target="_blank" rel="noreferrer">查看来源<ExternalLink size={12} /></a>}</div><details className="memory-index-provenance"><summary>来源文档<ChevronDown size={12} /></summary><span><ReferenceText value={document.title || '未命名来源文档'} /></span></details></article>)}</div> : <div className="memory-index-empty">{(data.documents_total ?? data.stats.documents) > 0 ? '本页暂无来源文档，可使用上一页查看其他记录。' : '当前筛选尚无来源文档索引。导入参考资料或在研究中获取文献正文后，这里会显示提取与索引记录。'}</div>}{onOffset && <RecordPagination label="来源文档" offset={data.documents_offset ?? 0} count={documents.length} total={data.documents_total ?? data.stats.documents} limit={data.documents_limit ?? 20} loading={loading} onOffset={onOffset} />}</>}
    {!data && !error && <div className="memory-index-empty">{loading ? '正在读取持久化索引…' : '尚未取得索引状态。'}</div>}
  </section></ReferenceNames>;
}

export function MemorySourceCitation({ memory }: { memory: Json }) {
  const source = memory.metadata;
  if (!source || typeof source !== 'object' || !(source.source_id || source.content_sha256 || source.text_sha256 || source.page_number != null)) return null;
  const page = Number.isInteger(source.page_number) && source.page_number > 0 ? `第 ${source.page_number} 页` : '页码未记录';
  const hasRange = Number.isInteger(source.char_start) && Number.isInteger(source.char_end) && source.char_start >= 0 && source.char_end >= source.char_start;
  return <ReferenceNames records={source}><aside className="memory-source-citation" aria-label="记忆片段来源"><strong><FileText size={13} /><ReferenceText value={textOf(source.title) || '来源文档'} /></strong><div><span>{page}</span>{hasRange && <span>提取文本字符 {source.char_start}–{source.char_end}（末位不含）</span>}</div>{source.untrusted === true && <small>外部来源文本，作为证据材料处理。</small>}{source.source_text_truncated === true && <small>来源正文已截断，当前索引未覆盖全文。</small>}{safeUrl(source.url) && <a href={safeUrl(source.url)} target="_blank" rel="noreferrer">查看来源<ExternalLink size={12} /></a>}<details><summary>来源文档与核验记录<ChevronDown size={12} /></summary><strong>已有核验记录</strong><pre><ReferenceText value={source.verification != null ? textOf(source.verification) : '未记录；索引命中不代表真实性已核验。'} /></pre></details></aside></ReferenceNames>;
}
