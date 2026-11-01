import { useEffect, useMemo, useState } from 'react';
import { BookOpen, Download, FileDiff, FileText } from 'lucide-react';
import type { Json } from './api';
import { ImplementationDocumentBody, readVerifiedImplementationDocument } from './ImplementationDocumentReader';
import { implementationSectionTitle } from './implementationDocumentContent';
import { diffDocumentText, matchDocumentSections } from './documentDiff';
import type { DocumentVersion } from './documentVersions';
import { ViewBoundary } from './ViewBoundary';
import './document-version-preview.css';
import { DocumentDeliverySummary } from './DocumentDeliverySummary';

const fileUrl = (runId: string, doc: Json) => `/api/runs/${encodeURIComponent(runId)}/documents/${encodeURIComponent(doc.document_id)}/file?expected_sha256=${encodeURIComponent(doc.sha256)}`;
const changeNames = { equal: '未修改', changed: '已修改', added: '新增', removed: '已删除' };

function useDocument(runId: string, version?: DocumentVersion) {
  const scope = JSON.stringify([runId, version?.document.document_id, version?.document.sha256]);
  const [retry, setRetry] = useState(0);
  const [state, setState] = useState<{ scope: string; sections: Json[]; loading: boolean; error: string }>({ scope: '', sections: [], loading: false, error: '' });
  useEffect(() => {
    if (!version) return;
    const controller = new AbortController();
    let active = true, timedOut = false;
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, 20000);
    setState({ scope, sections: [], loading: true, error: '' });
    void readVerifiedImplementationDocument(runId, version.document, controller.signal).then(sections => {
      if (active && !controller.signal.aborted) setState({ scope, sections, loading: false, error: '' });
    }).catch(error => {
      if (active && (!controller.signal.aborted || timedOut)) setState({ scope, sections: [], loading: false,
        error: timedOut ? '文档读取超时，请重试。' : (error as Error).message });
    }).finally(() => clearTimeout(timer));
    return () => { active = false; clearTimeout(timer); controller.abort(); };
  }, [scope, retry]);
  return { sections: state.scope === scope ? state.sections : [],
    loading: !!version && (state.scope !== scope || state.loading),
    error: state.scope === scope ? state.error : '', retry: () => setRetry(value => value + 1) };
}

type Line = { text: string; number: number; changed: boolean };
type DiffRow = { old?: Line; next?: Line };
type DiffHunk = ReturnType<typeof diffDocumentText>['hunks'][number];

/** Count aligned rows from diff metadata without splitting any source text. */
export function documentDiffRowCount(hunks: readonly DiffHunk[]): number {
  let count = 0, removed = 0, added = 0;
  for (const hunk of hunks) {
    if (hunk.type === 'equal') {
      count += Math.max(removed, added) + hunk.oldLines;
      removed = 0; added = 0;
    } else if (hunk.type === 'remove') removed += hunk.oldLines;
    else added += hunk.newLines;
  }
  return count + Math.max(removed, added);
}

/** Scan skipped lines without allocating strings/objects; slice visible lines only. */
function fillDiffLines(hunk: DiffHunk, first: number, end: number, rows: DiffRow[], offset: number,
  side: 'old' | 'next' | 'equal') {
  const text = hunk.text;
  let line = 0, start = 0;
  const append = (until: number) => {
    if (line >= first) {
      const source = text.slice(start, until), row = rows[offset + line - first];
      if (side !== 'next') row.old = { text: source, number: hunk.oldStart! + line, changed: side !== 'equal' };
      if (side !== 'old') row.next = { text: source, number: hunk.newStart! + line, changed: side !== 'equal' };
    }
    line++; start = until;
  };
  for (let index = 0; index < text.length && line < end; index++) {
    if (text[index] !== '\r' && text[index] !== '\n') continue;
    if (text[index] === '\r' && text[index + 1] === '\n') index++;
    append(index + 1);
  }
  if (line < end && start < text.length) append(text.length);
}

/** Only the requested (at most 300) aligned rows become display objects. */
export function documentDiffPage(hunks: readonly DiffHunk[], start: number, limit = 300): DiffRow[] {
  if (!Number.isSafeInteger(start) || start < 0 || !Number.isSafeInteger(limit) || limit < 1 || limit > 300) {
    throw new RangeError('Diff pages require a nonnegative start and between 1 and 300 rows.');
  }
  const rows: DiffRow[] = [], end = start + limit;
  let cursor = 0, removed: DiffHunk[] = [], added: DiffHunk[] = [], removedCount = 0, addedCount = 0;
  const fillSide = (sideHunks: DiffHunk[], side: 'old' | 'next', first: number, until: number, offset: number) => {
    let line = 0;
    for (const hunk of sideHunks) {
      const count = side === 'old' ? hunk.oldLines : hunk.newLines;
      const from = Math.max(first, line), to = Math.min(until, line + count);
      if (from < to) fillDiffLines(hunk, from - line, to - line, rows, offset + from - first, side);
      line += count;
      if (line >= until) break;
    }
  };
  const flush = () => {
    const count = Math.max(removedCount, addedCount);
    const from = Math.max(start, cursor), to = Math.min(end, cursor + count), offset = rows.length;
    if (from < to) {
      for (let i = from; i < to; i++) rows.push({});
      fillSide(removed, 'old', from - cursor, to - cursor, offset);
      fillSide(added, 'next', from - cursor, to - cursor, offset);
    }
    cursor += count; removed = []; added = []; removedCount = 0; addedCount = 0;
  };
  for (const hunk of hunks) {
    if (hunk.type === 'equal') {
      flush();
      if (cursor >= end) break;
      const from = Math.max(start, cursor), to = Math.min(end, cursor + hunk.oldLines), offset = rows.length;
      if (from < to) {
        for (let i = from; i < to; i++) rows.push({});
        fillDiffLines(hunk, from - cursor, to - cursor, rows, offset, 'equal');
      }
      cursor += hunk.oldLines;
      if (cursor >= end) break;
    } else if (hunk.type === 'remove') { removed.push(hunk); removedCount += hunk.oldLines; }
    else { added.push(hunk); addedCount += hunk.newLines; }
  }
  flush();
  return rows;
}

function LineText({ line }: { line: Line }) {
  const ending = line.text.endsWith('\r\n') ? 'CRLF' : line.text.endsWith('\n') ? 'LF' : line.text.endsWith('\r') ? 'CR' : '无换行';
  return <code>{line.text.replace(/(?:\r\n|\r|\n)$/, '') || ' '}{line.changed && ending !== 'LF' && <small className="document-line-ending">{ending}</small>}</code>;
}

export function DocumentDiffView({ oldText, newText, sideBySide }: { oldText: string; newText: string; sideBySide: boolean }) {
  const diff = useMemo(() => diffDocumentText(oldText, newText), [oldText, newText]);
  const rowCount = useMemo(() => documentDiffRowCount(diff.hunks), [diff]);
  const [page, setPage] = useState(0);
  const pageCount = Math.max(1, Math.ceil(rowCount / 300)), selectedPage = Math.min(page, pageCount - 1);
  const visible = useMemo(() => documentDiffPage(diff.hunks, selectedPage * 300), [diff, selectedPage]);
  return <div className="document-diff-view">
    <div className="document-diff-stats"><span className="document-added">+{diff.added} 行新增</span><span className="document-removed">−{diff.removed} 行删除</span><span>行号对应本章原文</span></div>
    {diff.simplified && <p className="muted">本章变更较多，按完整文本块对比；所有增删内容均可翻页查看。</p>}
    {!diff.added && !diff.removed && <p className="document-no-change">本章内容没有修改。</p>}
    <div className={`document-diff-lines ${sideBySide ? 'is-split' : 'is-unified'}`} aria-label={sideBySide ? '旧版新版并排差异' : '合并差异'}>
      {sideBySide && <div className="document-diff-columns"><span>旧版 · 删除</span><span>新版 · 新增</span></div>}
      {visible.map((row, index) => sideBySide ? <div className="document-diff-pair" key={index}>
        {(['old', 'next'] as const).map(side => { const line = row[side]; return <div key={side} className={`document-diff-line ${!line ? 'is-empty' : line.changed ? side === 'old' ? 'is-removed' : 'is-added' : ''}`}>
          <span className="document-line-number">{line?.number ?? ''}</span><span className="document-line-sign">{line?.changed ? side === 'old' ? '−' : '+' : ' '}</span>{line && <LineText line={line} />}</div>; })}
      </div> : row.old?.changed || row.next?.changed ? <div key={index}>
        {row.old && <div className="document-diff-line is-removed"><span className="document-line-number">{row.old.number}</span><span className="document-line-number" /><span className="document-line-sign">−</span><LineText line={row.old} /></div>}
        {row.next && <div className="document-diff-line is-added"><span className="document-line-number" /><span className="document-line-number">{row.next.number}</span><span className="document-line-sign">+</span><LineText line={row.next} /></div>}
      </div> : <div className="document-diff-line" key={index}><span className="document-line-number">{row.old?.number}</span><span className="document-line-number">{row.next?.number}</span><span className="document-line-sign"> </span>{row.next && <LineText line={row.next} />}</div>)}
    </div>
    {pageCount > 1 && <div className="document-diff-pagination"><button className="btn small" disabled={selectedPage === 0} onClick={() => setPage(selectedPage - 1)}>上一页差异</button><span>第 {selectedPage + 1} / {pageCount} 页 · 全部 {rowCount} 行</span><button className="btn small" disabled={selectedPage + 1 === pageCount} onClick={() => setPage(selectedPage + 1)}>下一页差异</button></div>}
  </div>;
}

function PreviewPage({ section, label, raw }: { section?: Json | null; label: string; raw: boolean }) {
  return <section className="document-preview-page" aria-label={label}><header>{label}</header>{!section
    ? <p className="document-empty-section">此版本没有这一章。</p>
    : raw ? <pre className="implementation-source-text">{section.text}</pre> : <ViewBoundary key={`${section.id}:${section.start}:${section.end}:${section.text}`} label="文档阅读排版"><ImplementationDocumentBody text={section.text} /></ViewBoundary>}</section>;
}

export function DocumentVersionPreview({ runId, versions, initialVersionKey, title = '文档' }: {
  runId: string; versions: DocumentVersion[]; initialVersionKey?: string; title?: string;
}) {
  const [selection, setSelection] = useState({ current: initialVersionKey ?? versions.at(-1)?.key ?? '', previous: '' });
  const [mode, setMode] = useState<'preview' | 'diff'>('preview');
  const [sideBySide, setSideBySide] = useState(false);
  const [raw, setRaw] = useState(false);
  const [onlyChanges, setOnlyChanges] = useState(false);
  const [chapter, setChapter] = useState('');
  const current = versions.find(item => item.key === selection.current) ?? versions.at(-1);
  const predecessors = current ? versions.slice(0, versions.indexOf(current)) : [];
  const previous = predecessors.find(item => item.key === selection.previous) ?? predecessors.at(-1);
  const effectiveMode = previous ? mode : 'preview';
  const currentRead = useDocument(runId, current), previousRead = useDocument(runId, previous);
  const ready = !!previous && !previousRead.loading && !previousRead.error && !currentRead.loading && !currentRead.error;
  const pairs = useMemo(() => matchDocumentSections(ready ? previousRead.sections : [], currentRead.sections), [ready, previousRead.sections, currentRead.sections]);
  const changes = pairs.filter(pair => pair.status !== 'equal');
  const listed = onlyChanges && ready ? changes : pairs;
  const selected = listed.find(pair => pair.key === chapter) ?? listed.find(pair => ['summary', '实现概述', '实现概览', '实现目标与范围'].includes(pair.title)) ?? listed[0];
  const comparisonKey = `${current?.key}:${previous?.key}:${selected?.key}`;
  if (!current) return <p role="status">尚无已保存文档，保存后可在这里预览。</p>;
  return <section className="document-version-preview" aria-label={`${title}预览与修订`}>
    <header className="document-version-heading"><div><h4><BookOpen size={18} />{title}</h4><p>阅读已保存原文，或按章节核对每次修订。</p></div><a className="btn small" href={fileUrl(runId, current.document)} download><Download size={14} />下载所选版本</a></header>
    <DocumentDeliverySummary runId={runId} document={current.document}
      reviewRound={current.document.kind === 'implementation_specification' ? current.revision + 1 : undefined} />
    <div className="document-version-controls"><label>查看版本<select aria-label="查看版本" value={current.key} onChange={event => { setSelection({ current: event.target.value, previous: '' }); setChapter(''); }}>
      {versions.map(version => <option value={version.key} key={version.key}>{version.label}</option>)}</select></label>
      <label>对照版本<select aria-label="对照版本" value={previous?.key ?? ''} disabled={!predecessors.length} onChange={event => { setSelection({ current: current.key, previous: event.target.value }); setChapter(''); }}>
        {!predecessors.length && <option value="">没有更早版本</option>}{predecessors.map(version => <option value={version.key} key={version.key}>{version.label}</option>)}</select></label>
      <label className="document-compare-switch"><input type="checkbox" checked={!!previous && sideBySide} disabled={!previous} onChange={event => setSideBySide(event.target.checked)} />同时显示旧版</label>
    </div>
    <div className="document-view-options"><div className="document-mode-switch" role="group" aria-label="文档显示方式">
      <button type="button" aria-pressed={effectiveMode === 'preview'} onClick={() => setMode('preview')}><BookOpen size={14} />文档预览</button>
      <button type="button" aria-pressed={effectiveMode === 'diff'} disabled={!previous} onClick={() => setMode('diff')}><FileDiff size={14} />修订对比</button></div>
      {effectiveMode === 'preview' && <button className="btn small" type="button" aria-pressed={raw} onClick={() => setRaw(value => !value)}><FileText size={13} />{raw ? '阅读排版' : '原始 Markdown'}</button>}
      <label><input type="checkbox" checked={ready && onlyChanges} disabled={!ready} onChange={event => setOnlyChanges(event.target.checked)} />只看修改章节</label></div>
    {!previous && <p className="document-comparison-summary">此版本没有更早的已保存版本，可直接预览原文。</p>}
    {ready && <p className="document-comparison-summary">{previous.label} → {current.label}<span>{changes.length ? `${changes.length} 个章节有修订` : '正文没有变化'}</span><small>绿 + 新增 · 红 − 删除</small></p>}
    {(currentRead.loading || previousRead.loading) && <p role="status">{currentRead.loading ? '正在读取所选版本…' : '正在读取对照版本…'}</p>}
    {[{ read: currentRead, label: '所选版本' }, { read: previousRead, label: '对照版本' }].map(({ read, label }) => read.error && <div role="alert" key={label}><p>{label}：{read.error}</p><button className="btn small" onClick={read.retry}>重新读取{label}</button></div>)}
    {!currentRead.loading && !currentRead.error && <div className="document-review-layout"><nav aria-label={`${title}章节`}>
      <h5>章节目录 <span>{listed.length}</span></h5>{listed.map((pair, index) => <button type="button" key={pair.key} aria-current={selected?.key === pair.key ? 'page' : undefined} onClick={() => setChapter(pair.key)}>
        <span className="document-chapter-number">{String(index + 1).padStart(2, '0')}</span><span>{implementationSectionTitle(pair.title)}</span>{ready && pair.status !== 'equal' && <small className={`document-change-badge is-${pair.status}`}>{changeNames[pair.status]}</small>}</button>)}
    </nav><div className="document-review-content">
      {!selected ? <p>{ready && onlyChanges ? '所选版本之间没有修改章节。' : '此版本正文为空。'}</p> : <><header className="document-chapter-heading"><h4>{implementationSectionTitle(selected.title)}</h4>{ready && <span className={`document-change-badge is-${selected.status}`}>{changeNames[selected.status]}</span>}</header>
        {effectiveMode === 'diff' && previous ? ready ? <DocumentDiffView key={comparisonKey} oldText={selected.oldSection?.text ?? ''} newText={selected.newSection?.text ?? ''} sideBySide={sideBySide} /> : <p>对照版本读取完成后显示差异。</p>
          : <div className={`document-preview-pages ${sideBySide && ready ? 'is-split' : ''}`}>{sideBySide && ready && <PreviewPage section={selected.oldSection} label={`旧版 · ${previous.label}`} raw={raw} />}<PreviewPage section={selected.newSection} label={`所选版本 · ${current.label}`} raw={raw} /></div>}
        <details className="implementation-document-source"><summary>版本与原文定位</summary><p>文档 <code>{current.document.document_id}</code></p><p>SHA256 <code>{current.document.sha256}</code></p>{selected.newSection && <p>原章节 {selected.newSection.id} · 字符 {selected.newSection.start}–{selected.newSection.end}</p>}{previous && <p>对照 SHA256 <code>{previous.document.sha256}</code></p>}</details>
      </>}
    </div></div>}
  </section>;
}
