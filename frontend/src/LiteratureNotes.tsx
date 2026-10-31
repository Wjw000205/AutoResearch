import { ReferenceText, ReferenceNames } from './ReferenceNames';
import { BookOpen, ChevronDown, CircleAlert, Database, ExternalLink, FileText } from 'lucide-react';
import { safeUrl, textOf, type Json } from './api';
import { HumanMessage } from './localization';

export interface LiteratureReadingNote {
  paper_id: string; source_id: string; text_sha256: string; summary: string; research_question: string;
  method: string; datasets: string[]; baselines: string[];
  results: { dataset: string; metric: string; value: string; setting: string; locator: string; quote: string }[];
  limitations: string[]; relevance: string; excerpts: { locator: string; quote: string }[];
  missing_information: string[]; reading_scope: 'inspected_passages'; generation: 'ai_extraction' | 'reused_ai_extraction';
  reused_from?: { rag_version_id: string; run_id: string; analysis_created_at: string };
  scientific_claims_verified: false; document_identity_verified?: false;
}
export interface LiteratureReadingPlan {
  core_paper_ids?: string[]; pending_paper_ids?: string[]; selection_rationale?: string;
}
export interface LiteratureReadingGate {
  approved?: boolean; issues?: string[]; required_core_count?: number; pending_core_ids?: string[]; scope?: string;
  noted_core_ids?: string[];
  declared_core_ids?: string[]; selected_core_ids?: string[]; retained_skipped_core_ids?: string[];
  required_readable_core_count?: number; skipped_core_ids?: string[];
  skipped_papers?: { paper_id: string; reason?: string; arxiv_status?: string }[];
}
interface ReadingRow {
  paperId: string; paper?: Json; readings: Json[]; notes: LiteratureReadingNote[]; unmatchedNotes: Json[];
  status: 'noted' | 'indexed' | 'unread'; core: boolean; pending: boolean;
}
const values = (value: unknown): string[] => Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string' && !!item.trim()) : [];
const records = (value: unknown): Json[] => Array.isArray(value) ? value.filter((item): item is Json => !!item && typeof item === 'object' && !Array.isArray(item)) : [];
const indexed = (reading: Json) => ['pdf_text_indexed', 'web_text_indexed'].includes(reading.retrieval_status)
  && typeof reading.source_id === 'string' && !!reading.source_id && typeof reading.text_sha256 === 'string' && !!reading.text_sha256;

/** Join exact retained source identities only; no title/DOI guessing or index-as-reading inference. */
export function literatureReadingRows(papers: Json[], readings: Json[], notes: Json[], plan?: LiteratureReadingPlan | null): ReadingRow[] {
  const ids = new Set([...papers.map(paper => paper.id), ...readings.map(reading => reading.paper_id),
    ...notes.map(note => note.paper_id), ...values(plan?.core_paper_ids), ...values(plan?.pending_paper_ids)]
    .filter((id): id is string => typeof id === 'string' && !!id.trim()));
  return [...ids].map(paperId => {
    const sourceReadings = readings.filter(reading => reading.paper_id === paperId);
    const allNotes = notes.filter(note => note.paper_id === paperId);
    const matched = allNotes.filter(note => note.reading_scope === 'inspected_passages'
      && note.scientific_claims_verified === false
      && sourceReadings.some(reading => indexed(reading) && reading.source_id === note.source_id && reading.text_sha256 === note.text_sha256
        && (note.generation === 'ai_extraction' || (note.generation === 'reused_ai_extraction' && note.reused_from?.rag_version_id
          && reading.rag_version_id === note.reused_from.rag_version_id))));
    return { paperId, paper: papers.find(paper => paper.id === paperId), readings: sourceReadings,
      notes: matched as LiteratureReadingNote[], unmatchedNotes: allNotes.filter(note => !matched.includes(note)),
      status: matched.length ? 'noted' : sourceReadings.some(indexed) ? 'indexed' : 'unread',
      core: values(plan?.core_paper_ids).includes(paperId), pending: values(plan?.pending_paper_ids).includes(paperId) };
  });
}

export function literatureReadingProgress({ papers = [], readings = [], notes = [], readingPlan, readingGate }: {
  papers?: Json[]; readings?: Json[]; notes?: Json[]; readingPlan?: LiteratureReadingPlan | null; readingGate?: LiteratureReadingGate | null;
}) {
  const rows = literatureReadingRows(records(papers), records(readings), records(notes), readingPlan);
  const hasSkipped = Array.isArray(readingGate?.skipped_core_ids) || Array.isArray(readingGate?.skipped_papers);
  const skippedIds = [...new Set([...values(readingGate?.skipped_core_ids), ...values(records(readingGate?.skipped_papers).map(item => item.paper_id))])];
  const required = readingGate?.required_core_count;
  const declaredIds = Array.isArray(readingGate?.declared_core_ids) ? readingGate.declared_core_ids : readingPlan?.core_paper_ids;
  const hasEffectiveSelection = Array.isArray(readingGate?.selected_core_ids);
  const selectedIds = hasEffectiveSelection ? readingGate?.selected_core_ids : declaredIds;
  return {
    declaredCount: Array.isArray(declaredIds) ? new Set(values(declaredIds)).size : null,
    selectedCount: Array.isArray(selectedIds) ? new Set(values(selectedIds)).size : null,
    hasEffectiveSelection,
    targetCount: typeof required === 'number' && Number.isInteger(required) && required >= 0 ? required : null,
    notedCoreCount: Array.isArray(readingGate?.noted_core_ids) ? new Set(values(readingGate.noted_core_ids)).size : null,
    skippedCount: hasSkipped ? skippedIds.length : null,
    indexedWithoutNotes: rows.filter(row => row.status === 'indexed' && !skippedIds.includes(row.paperId)),
    savedNoteCount: rows.reduce((count, row) => count + row.notes.length, 0),
    notedPaperCount: rows.filter(row => row.status === 'noted').length,
  };
}

export function LiteratureReadingProgress({ readingPlan, readingGate }: { readingPlan?: LiteratureReadingPlan | null; readingGate?: LiteratureReadingGate | null }) {
  const progress = literatureReadingProgress({ readingPlan, readingGate });
  return <div className="reading-notes-counts"><span>本轮声明 {progress.declaredCount ?? '未记录'} 篇</span><span>{progress.hasEffectiveSelection ? '有效核心选择' : '当前核心选择'} {progress.selectedCount ?? '未记录'} / 目标 {progress.targetCount ?? '未记录'} 篇</span><span>核心合格笔记 {progress.notedCoreCount ?? '未记录'} 篇</span><span>已记录跳过 {progress.skippedCount ?? '未记录'} 篇</span></div>;
}

function TextSection({ title, value }: { title: string; value: unknown }) {
  return <section className="reading-note-section"><h5><ReferenceText value={title} /></h5><p><ReferenceText value={textOf(value) || '未记录'} /></p></section>;
}
function ListSection({ title, value }: { title: string; value: unknown }) {
  const items = values(value);
  return <section className="reading-note-section"><h5><ReferenceText value={title} /></h5>{items.length ? <ul>{items.map((item, index) => <li key={index}><ReferenceText value={item} /></li>)}</ul> : <p>未记录</p>}</section>;
}
function ArxivLookupStatus({ status }: { status?: string }) {
  const known: Record<string, string> = {
    none: '未执行 arXiv 公开版本检索', not_attempted: '尚未尝试 arXiv 公开版本检索',
    not_supported: '当前来源适配器不支持替代版本检索',
    no_match: '本次有限检索未找到可靠的同篇匹配', ambiguous: '存在多个候选，尚不能唯一确认同篇论文',
    search_failed: '公开版本检索失败，不能据此判定没有公开版本',
    fetch_failed: '已匹配 arXiv 版本，但正文获取失败', no_text: '已匹配 arXiv 版本，但未取得可读文字',
    source_retrieved: '替代来源正文已获取并索引，尚不代表已读完或已保存合格笔记',
  };
  if (!status) return <p>未记录</p>;
  return known[status] ? <><p><ReferenceText value={known[status]} /></p><details><summary>原始检索状态</summary><code><ReferenceText value={status} /></code></details></> : <HumanMessage text={status} />;
}
function NoteContent({ note }: { note: LiteratureReadingNote }) {
  const results = records(note.results), excerpts = records(note.excerpts);
  return <div className="reading-note-content"><p className="reading-note-provenance"><ReferenceText value={note.reused_from ? `共享 RAG 复用 · 原分析 ${note.reused_from.analysis_created_at} · 原任务 ${note.reused_from.run_id} · 本次未重新阅读全文` : '智能体提取 · 已读所列片段'} /> · 科学主张尚未核验</p>{note.document_identity_verified === false && <p className="reading-note-provenance">来源快照与论文身份的对应关系尚未核验。</p>}
    <TextSection title="阅读摘要" value={note.summary} /><TextSection title="研究问题" value={note.research_question} /><TextSection title="方法" value={note.method} />
    <div className="reading-note-two-column"><ListSection title="论文使用的数据集" value={note.datasets} /><ListSection title="论文比较的基线" value={note.baselines} /></div>
    <div className="reading-note-results"><h5>原论文报告的指标</h5><p>以下为笔记记录的原论文报告值，未由本机实验复现；不同数据、设置和指标不可直接混为本研究结果。</p>{results.length ? <div className="table-scroll"><table><caption>原论文报告值（非本机实验）</caption><thead><tr><th>数据集</th><th>实验设置</th><th>指标</th><th>原文报告值</th><th>原文定位与摘录</th></tr></thead><tbody>{results.map((result, index) => <tr key={index}><td><ReferenceText value={textOf(result.dataset) || '未记录'} /></td><td><ReferenceText value={textOf(result.setting) || '未记录'} /></td><td><ReferenceText value={textOf(result.metric) || '未记录'} /></td><td><ReferenceText value={textOf(result.value) || '未记录'} /></td><td><code><ReferenceText value={textOf(result.locator) || '定位未记录'} /></code>{result.quote ? <details className="reading-note-quote"><summary>查看原文摘录<ChevronDown size={12} /></summary><blockquote><ReferenceText value={result.quote} /></blockquote></details> : <small>摘录未记录</small>}</td></tr>)}</tbody></table></div> : <p>所读片段未提取到可列出的指标值，不补造测量。</p>}</div>
    <ListSection title="论文局限" value={note.limitations} /><TextSection title="与当前研究想法的关系" value={note.relevance} /><ListSection title="尚缺的信息" value={note.missing_information} />
    {excerpts.length > 0 && <details className="reading-note-excerpts"><summary>已检查的原文片段 · {excerpts.length} 条<ChevronDown size={13} /></summary>{excerpts.map((excerpt, index) => <div key={index}><code><ReferenceText value={textOf(excerpt.locator) || '定位未记录'} /></code><blockquote><ReferenceText value={textOf(excerpt.quote) || '摘录未记录'} /></blockquote></div>)}</details>}
    <details className="reading-note-source"><summary>笔记来源文档<ChevronDown size={12} /></summary><dl><div><dt>论文标识</dt><dd><code><ReferenceText value={note.paper_id} /></code></dd></div><div><dt>来源快照</dt><dd><code><ReferenceText value={note.source_id} /></code></dd></div></dl></details>
  </div>;
}

export function LiteratureNotes({ papers = [], readings = [], notes = [], readingPlan, readingGate }: {
  papers?: Json[]; readings?: Json[]; notes?: Json[]; readingPlan?: LiteratureReadingPlan | null; readingGate?: LiteratureReadingGate | null;
}) {
  const rows = literatureReadingRows(records(papers), records(readings), records(notes), readingPlan);
  const progress = literatureReadingProgress({ papers, readings, notes, readingPlan, readingGate });
  const labels = { noted: '已沉淀阅读笔记 · 已读所列片段', indexed: '仅索引 · 尚无阅读笔记', unread: '未读 · 尚无阅读笔记' };
  const gateLabel = readingGate?.approved === true ? '阅读门禁已通过（片段笔记检查）' : readingGate?.approved === false ? '阅读门禁未通过' : '阅读门禁结果未记录';
  const required = typeof readingGate?.required_core_count === 'number' && Number.isInteger(readingGate.required_core_count) && readingGate.required_core_count >= 0 ? `${readingGate.required_core_count} 篇` : '未记录';
  const readableRequired = typeof readingGate?.required_readable_core_count === 'number' && Number.isInteger(readingGate.required_readable_core_count) && readingGate.required_readable_core_count >= 0 ? `${readingGate.required_readable_core_count} 篇` : null;
  const hasSkipRecords = Array.isArray(readingGate?.skipped_core_ids) || Array.isArray(readingGate?.skipped_papers);
  const skipDetails = records(readingGate?.skipped_papers);
  const skippedIds = [...new Set([...values(readingGate?.skipped_core_ids), ...values(skipDetails.map(item => item.paper_id))])];
  const issues = values(readingGate?.issues), pendingCore = values(readingGate?.pending_core_ids);
  return <ReferenceNames records={[papers, readings, notes]}><section className="literature-notes" aria-label="文献阅读笔记与来源"><header><div><BookOpen size={18} /><h3>文献阅读笔记</h3></div><a className="btn small" href="#/memory"><Database size={14} />检索持久记忆</a></header>
    <p className="reading-notes-boundary">笔记由智能体从所列片段提取，表示已读这些片段。获取和索引不等于阅读全文；论文报告值与本机实验结果分别记录，笔记不证明论文主张成立。</p>
    {readingPlan && <div className="reading-notes-plan"><strong>阅读计划</strong><span>核心论文 {values(readingPlan.core_paper_ids).length} 篇 · 计划待处理 {values(readingPlan.pending_paper_ids).length} 篇</span><p><ReferenceText value={textOf(readingPlan.selection_rationale) || '筛选理由未记录'} /></p></div>}
    <LiteratureReadingProgress readingPlan={readingPlan} readingGate={readingGate} />
    <p>已保存合格片段笔记 {progress.savedNoteCount} 条，覆盖 {progress.notedPaperCount} 篇不同论文；同一论文的多条笔记不增加论文篇数。</p>
    {progress.indexedWithoutNotes.length > 0 && <section className="reading-note-section" aria-label="已有正文索引但缺少匹配笔记"><h4>已有正文索引，待读取或补笔记 · {progress.indexedWithoutNotes.length} 篇</h4><p>以下论文尚无与已记录来源匹配的合格笔记；仅有索引不能认定已读片段，也不自动计入核心选择。</p><ul>{progress.indexedWithoutNotes.map(row => <li key={row.paperId}>{textOf(row.paper?.title) && <span><ReferenceText value={row.paper?.title} /> </span>}<code><ReferenceText value={row.paperId} /></code></li>)}</ul></section>}
    <section className="reading-note-section reading-gate" aria-label="文献阅读门禁"><h4><ReferenceText value={gateLabel} /></h4><p><ReferenceText value={readableRequired !== null || hasSkipRecords ? '原选择的核心论文目标' : '门禁要求的核心论文数量'} />：<ReferenceText value={required} /></p>{readableRequired !== null && <p>实际必读核心论文：<ReferenceText value={readableRequired} /></p>}<h5>具体缺口</h5>{issues.length ? <ul>{issues.map((issue, index) => <li key={index}><HumanMessage text={issue} /></li>)}</ul> : <p>未列出具体缺口。</p>}<h5>待补读的核心论文</h5>{pendingCore.length ? <ul>{pendingCore.map((id, index) => <li key={index}><code><ReferenceText value={id} /></code></li>)}</ul> : <p><ReferenceText value={Array.isArray(readingGate?.pending_core_ids) ? '未列出待补读论文。' : '未记录'} /></p>}<p>门禁仅检查片段阅读笔记，不代表已读完全文或科学结论已获核验。</p>{typeof readingGate?.scope === 'string' && readingGate.scope.trim() && <HumanMessage text={readingGate.scope} />}</section>
    {hasSkipRecords && <section className="reading-note-section reading-gate" aria-label="已跳过的核心论文"><h4>已跳过的核心论文 · {skippedIds.length} 篇</h4><p>跳过不计为已读，也不能作为论点支持或新颖性证明；仍需至少一篇有合格阅读笔记的论文才能推进。</p>{skippedIds.length ? skippedIds.map(paperId => {
      const detail = skipDetails.find(item => item.paper_id === paperId);
      const paper = records(papers).find(item => item.id === paperId);
      return <article className="reading-paper" key={paperId}><h5><ReferenceText value={textOf(paper?.title) || paperId} /></h5><code><ReferenceText value={paperId} /></code><strong>跳过原因</strong>{detail?.reason ? <HumanMessage text={textOf(detail.reason)} /> : <p>未记录</p>}<strong>arXiv 公开版本检索状态</strong><ArxivLookupStatus status={typeof detail?.arxiv_status === 'string' ? detail.arxiv_status : undefined} /></article>;
    }) : <p>当前未列出跳过的论文。</p>}</section>}
    <div className="reading-notes-counts"><span>已沉淀笔记 {rows.filter(row => row.status === 'noted').length} 篇</span><span>仅索引 {rows.filter(row => row.status === 'indexed').length} 篇</span><span>未读 {rows.filter(row => row.status === 'unread').length} 篇</span></div>
    {rows.length ? <div className="reading-notes-papers">{rows.map(row => {
      const firstNote = row.notes[0];
      const reading = (firstNote && row.readings.find(item => item.source_id === firstNote.source_id && item.text_sha256 === firstNote.text_sha256))
        || row.readings.find(indexed) || row.readings[0];
      const title = textOf(row.paper?.title || reading?.title) || row.paperId;
      const url = safeUrl(row.paper?.url) || safeUrl(reading?.url);
      return <article className={`reading-paper status-${row.status}`} key={row.paperId}><header><FileText size={16} /><div><h4><ReferenceText value={title} /></h4><code><ReferenceText value={row.paperId} /></code></div><span className={`reading-status status-${row.status}`}>{labels[row.status]}</span></header>
        <div className="reading-paper-meta">{row.core && <span>核心论文</span>}{skippedIds.includes(row.paperId) && <span>已列入跳过清单</span>}{row.pending && <span>阅读计划待处理</span>}{url && <a href={url} target="_blank" rel="noreferrer">查看已记录来源<ExternalLink size={12} /></a>}</div>
        {row.status === 'indexed' && <p className="reading-paper-pending">文本已获取并建立索引，尚未保存与该来源快照匹配的阅读笔记。</p>}
        {typeof reading?.local_pdf_url === 'string' && reading.local_pdf_url.startsWith('/api/runs/') && <p className="reading-paper-pending"><a href={reading.local_pdf_url} target="_blank" rel="noreferrer">打开已下载的本地 PDF<ExternalLink size={12} /></a> · <a href={`${reading.local_pdf_url}&download=true`} download>下载 PDF</a>（预览不可用时用本机阅读器打开）。agent 使用 PDF 工具按页读取，并以片段 ID 保存原文引用。</p>}
        {row.status === 'unread' && <p className="reading-paper-pending">尚无与该论文关联的有效阅读笔记。仅有检索元数据、获取失败或无可提取文本均不计为已读。</p>}
        {row.notes.map((note, index) => <details className="reading-note" key={`${note.source_id}-${note.text_sha256}-${index}`}><summary>查看阅读笔记{row.notes.length > 1 && ` ${index + 1}`}<ChevronDown size={13} /></summary><>{row.readings.some(reading => reading.historical_snapshot === true && reading.source_id === note.source_id && reading.text_sha256 === note.text_sha256) && <p>已核对的历史来源快照笔记；内容对应该保留快照，不代表新获取来源已被阅读。</p>}<NoteContent note={note} /></></details>)}
        {row.unmatchedNotes.length > 0 && <div className="reading-note-mismatch"><p><CircleAlert size={14} />有 {row.unmatchedNotes.length} 条已保存笔记未匹配当前阅读来源或读取范围，暂不计为已读。</p><details><summary>查看待核对笔记原始记录<ChevronDown size={12} /></summary><pre><ReferenceText value={row.unmatchedNotes} /></pre></details></div>}
      </article>;
    })}</div> : <p className="reading-notes-empty">尚无文献阅读记录。检索、获取正文并保存阅读笔记后，这里将显示其方法、结果与来源。</p>}
  </section></ReferenceNames>;
}
