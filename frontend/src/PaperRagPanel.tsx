import { ReferenceText, ReferenceNames } from './ReferenceNames';
import { useEffect, useId, useRef, useState, type FormEvent } from 'react';
import { createPortal } from 'react-dom';
import { ArrowDownToLine, BookOpen, ChevronDown, ChevronLeft, ChevronRight, Database, ExternalLink, FileText, LoaderCircle, RefreshCw, Search, X } from 'lucide-react';
import { api, safeUrl, type Json } from './api';
import { emptyRagFilters, ragAspectNames, ragCatalogPath, ragDomainBasis, ragDomainName, ragEvidencePreview, ragYearError, type RagFacet, type RagFilters } from './paperRag';

export function PaperRagPage({ revision = 0 }: { revision?: number }) {
  return <><div className="page-heading"><div><div className="eyebrow">共享文献库</div><h1>论文 RAG</h1><p>浏览已入库的文献，从原文与历史分析中检索架构、创新点和比较基线。</p></div><span className="pill"><Database size={14} />本地文献检索</span></div><PaperRagPanel revision={revision} /></>;
}

export function PaperRagPanel({ data, error: initialError = '', revision = 0 }: { data?: Json | null; error?: string; revision?: number }) {
  const id = useId();
  const [filters, setFilters] = useState<RagFilters>(emptyRagFilters);
  const [draft, setDraft] = useState('');
  const [offset, setOffset] = useState(0);
  const [limit, setLimit] = useState(12);
  const [refresh, setRefresh] = useState(0);
  const path = ragCatalogPath(filters, offset, limit);
  const [result, setResult] = useState<{ path: string; data?: Json | null }>({ path, data });
  const [error, setError] = useState(initialError);
  const [loading, setLoading] = useState(!data);
  const [selected, setSelected] = useState<Json | null>(null);
  const yearError = ragYearError(filters);
  useEffect(() => {
    if (yearError) { setLoading(false); return; }
    const controller = new AbortController();
    let pending = false;
    async function load() {
      if (pending) return;
      pending = true; setLoading(true); setError('');
      try {
        const value = await api<Json>(path, { signal: controller.signal });
        if (controller.signal.aborted) return;
        if (value.matched_count > 0 && offset >= value.matched_count) { setOffset(Math.floor((value.matched_count - 1) / limit) * limit); return; }
        if (offset && value.matched_count === 0) { setOffset(0); return; }
        setResult({ path, data: value });
      } catch (err) {
        if (!controller.signal.aborted) { setError((err as Error).message); setResult({ path, data: null }); }
      }
      finally { pending = false; if (!controller.signal.aborted) setLoading(false); }
    }
    void load();
    const timer = window.setInterval(load, 30000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [path, offset, limit, refresh, revision, yearError]);
  const catalog = error ? null : result.data;
  const current = result.path === path && !yearError ? catalog : null;
  const papers: Json[] = current?.papers ?? [];
  const matched = current?.matched_count ?? papers.length;
  const facets = catalog?.facets ?? {};
  const years: RagFacet[] = (facets.years ?? []).filter((item: RagFacet) => item.value !== '__unknown__');
  const active = Object.entries(filters).filter(([key, value]) => key !== 'sort' && value);
  const update = (key: keyof RagFilters, value: string) => { setFilters(old => ({ ...old, [key]: value })); setOffset(0); };
  const search = (event: FormEvent) => { event.preventDefault(); update('q', draft.trim()); };
  const reset = () => { setFilters(emptyRagFilters); setDraft(''); setOffset(0); };
  return <ReferenceNames records={catalog}><section className="memory-index-panel paper-rag-panel" aria-label="跨任务共享论文 RAG">
    <header><div><Database size={18} /><h2>跨任务共享论文 RAG</h2></div><button className="btn small" onClick={() => setRefresh(value => value + 1)} disabled={loading}><RefreshCw size={14} className={loading ? 'spin' : ''} />刷新文献库</button></header>
    <p>文献研究员先检索 RAG，缺少的文献才联网补查。同一篇论文只有一个主条目，不同来源自动绑定到该论文；原文与分析版本保留在条目下，任务按需复用。共享内容独立保存，删除原任务后仍可复用。</p>
    {catalog?.library && <p className="rag-library-source">当前文献库：<strong>{catalog.library.label}</strong><ReferenceText value={catalog.library.kind === 'custom' && ' · 独立目录，不是工作空间 data 文献库'} /></p>}
    {catalog && <><div className="memory-index-stats"><div><strong>{catalog.paper_count}</strong><span>共享论文</span></div><div><strong>{catalog.version_count}</strong><span>可追溯分析版本</span></div><div><strong>{catalog.reuse_count}</strong><span>跨任务引用</span></div></div>
      {catalog.manual_supplement_count > 0 && <p>另有 <ReferenceText value={catalog.manual_supplement_count} /> 份逐篇核查补录，原笔记保留。</p>}
      {catalog.pending_count > 0 && <p className="rag-notice"><ReferenceText value={catalog.pending_count} /> 条分析待重试入库；原任务笔记已保存。</p>}</>}
    <form className="rag-search" role="search" aria-label="检索共享论文 RAG" onSubmit={search}>
      <label className="rag-search-input"><Search size={18} /><span className="sr-only">文献关键词或架构问题</span><input type="search" value={draft} maxLength={500} onChange={event => setDraft(event.target.value)} placeholder="搜索论文、研究主题，或输入“Transformer 创新点”" /></label>
      <button className="btn rag-search-submit" type="submit">搜索 RAG</button>
    </form>
    <div className="rag-query-hints"><span>从已有分析中查：</span>{['架构', '创新点', '基线'].map(term => <button key={term} type="button" onClick={() => { const q = `${draft.trim()} ${term}`.trim().slice(0, 500); setDraft(q); update('q', q); }}>{term}<ChevronRight size={12} /></button>)}</div>
    <div className="rag-filters">
      <FacetSelect label="会议 / 期刊" value={filters.venue} options={facets.venues ?? []} onChange={value => update('venue', value)} />
      <FacetSelect label="领域" value={filters.domain} options={facets.domains ?? []} onChange={value => update('domain', value)} />
      <FacetSelect label="研究方向" value={filters.topic} options={facets.topics ?? []} onChange={value => update('topic', value)} />
      <label className="rag-filter"><span>起始年份</span><select value={filters.year_from} onChange={event => update('year_from', event.target.value)}><option value="">不限</option>{years.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
      <label className="rag-filter"><span>结束年份</span><select value={filters.year_to} onChange={event => update('year_to', event.target.value)}><option value="">不限</option>{years.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
      <FacetSelect label="CCF 等级（已核验）" value={filters.ccf_rank} options={facets.ranks ?? []} onChange={value => update('ccf_rank', value)} />
    </div>
    <p className="rag-classification-note">领域优先使用已核验出处的 CCF 目录分类，未覆盖时使用有来源的内容分类；研究方向按论文内容标注。分类依据可在详情中查看，外部提交的标注保留待核验状态。</p>
    {!!active.length && <div className="rag-active-filters" aria-label="当前筛选">{active.map(([key, value]) => <button key={key} onClick={() => { update(key as keyof RagFilters, ''); if (key === 'q') setDraft(''); }} aria-label={`移除${filterNames[key]}筛选`}><span>{filterNames[key]}：{value === '__unknown__' ? '未记录 / 待核验' : value}</span><X size={12} /></button>)}<button className="rag-reset" onClick={reset}>清除全部</button></div>}
    {(error || yearError) && <div className="rag-error" role="alert"><p><ReferenceText value={yearError || `文献库读取失败：${error}`} /></p>{!yearError && <button className="btn small" onClick={() => setRefresh(value => value + 1)}>重新读取</button>}</div>}
    <div className="rag-results-heading"><div><h3 id={`${id}-results`}>文献列表</h3><span role="status" aria-live="polite">{error ? '文献库连接已中断，列表已清空' : loading && !current ? '正在读取…' : current ? `匹配 ${matched} 篇 / 库内 ${catalog?.paper_count ?? 0} 篇` : '等待检索'}</span></div><label className="rag-sort"><span className="sr-only">文献排序</span><select aria-label="文献排序" value={filters.sort} onChange={event => update('sort', event.target.value)}><option value="relevance">相关性 / 最近分析</option><option value="publication_recent">发表年份：从新到旧</option><option value="analysis_recent">分析时间：从新到旧</option><option value="title">标题：A → Z</option></select></label></div>
    <div className="rag-results" aria-busy={loading} aria-labelledby={`${id}-results`}>
      {loading && !current && <div className="rag-empty"><LoaderCircle size={24} className="spin" /><h3>正在检索文献库</h3></div>}
      {!loading && current && !papers.length && <div className="rag-empty"><BookOpen size={30} strokeWidth={1.3} /><h3><ReferenceText value={active.length ? '没有匹配的文献' : '文献库还没有论文'} /></h3><p><ReferenceText value={active.length ? '试试其他关键词，或减少会议、领域和年份筛选。' : '完成文献阅读并保存分析后，论文会自动出现在这里。也可通过 RAG 导入接口添加已有分析。'} /></p>{!!active.length && <button className="btn small" onClick={reset}>查看全部文献</button>}</div>}
      {!!papers.length && <div className="rag-paper-grid">{papers.map(paper => <PaperCard key={paper.rag_entry_id ?? paper.rag_version_id} paper={paper} open={() => setSelected(paper)} />)}</div>}
    </div>
    {current && matched > 0 && <div className="rag-pagination"><span>第 {offset + 1}–{Math.min(offset + papers.length, matched)} 篇，共 {matched} 篇</span><div><label><span className="sr-only">每页文献数</span><select aria-label="每页文献数" value={limit} onChange={event => { setLimit(Number(event.target.value)); setOffset(0); }}>{[12, 24, 48].map(value => <option key={value} value={value}>{value} 篇 / 页</option>)}</select></label><button className="btn small" aria-label="上一页文献" disabled={!offset || loading} onClick={() => setOffset(Math.max(0, offset - limit))}><ChevronLeft size={16} /></button><span>{Math.floor(offset / limit) + 1} / {Math.ceil(matched / limit)}</span><button className="btn small" aria-label="下一页文献" disabled={offset + papers.length >= matched || loading} onClick={() => setOffset(offset + limit)}><ChevronRight size={16} /></button></div></div>}
    <p className="rag-library-note">{catalog?.method ?? '本地检索，无额外模型费用'}。历史分析保留原日期、来源和缺失信息；作者创新主张仍须结合原文与检索范围判断。</p>
    {selected && <PaperDetails paper={selected} close={() => setSelected(null)} />}
  </section></ReferenceNames>;
}

const filterNames: Record<string, string> = { q: '搜索', venue: '会议 / 期刊', domain: '领域', topic: '研究方向', year_from: '起始年份', year_to: '结束年份', ccf_rank: 'CCF' };
function FacetSelect({ label, value, options, onChange }: { label: string; value: string; options: RagFacet[]; onChange: (value: string) => void }) {
  return <label className="rag-filter"><span>{label}</span><select value={value} onChange={event => onChange(event.target.value)}><option value="">全部</option>{options.map(item => <option key={item.value} value={item.value}>{item.label} ({item.count})</option>)}{value && !options.some(item => item.value === value) && <option value={value}><ReferenceText value={value} /></option>}</select></label>;
}
function venueName(paper: Json) { return paper.rag_catalog?.venues?.join('、') || paper.venue?.names?.join('、') || paper.ccf_abbr || '会议 / 期刊未记录'; }
function PaperCard({ paper, open }: { paper: Json; open: () => void }) {
  const external = paper.import_provenance === 'external_agent_analysis';
  const aspects: string[] = paper.rag_matched_aspects ?? [];
  return <article className="rag-paper-card">
    <div className="rag-paper-top"><span><BookOpen size={14} />{venueName(paper)}</span><span>{paper.rag_catalog?.year ?? paper.year ?? '年份未记录'}</span></div>
    <h3><button onClick={open}><ReferenceText value={paper.title} /></button></h3>
    {!!paper.authors?.length && <p className="rag-paper-authors">{paper.authors.slice(0, 4).join(' · ')}<ReferenceText value={paper.authors.length > 4 ? ' 等' : ''} /></p>}
    <div className="rag-paper-tags"><span title={ragDomainBasis(paper)}>{ragDomainName(paper)}</span>{paper.rag_catalog?.rank && <span>CCF {paper.rag_catalog.rank}</span>}{paper.research_topics?.slice(0, 2).map((topic: string) => <span key={topic}>{topic}</span>)}</div>
    {!!aspects.length && <div className="rag-evidence-preview"><strong>{aspects.map(aspect => ragAspectNames[aspect]).join(' · ')}</strong><p><ReferenceText value={ragEvidencePreview(paper.rag_evidence)} /></p><small>历史分析中的作者报告与解释，创新性尚未核验。</small></div>}
    <div className="rag-paper-meta"><span className={external ? 'rag-external' : ''}>{external ? '外部分析 · 身份待核验' : '历史任务分析'}</span><span>原分析 {paper.rag_analysis_at?.slice(0, 10) ?? '未记录'}</span><span>{paper.rag_analysis_version_count ?? 1} 份分析{paper.rag_manual_supplement_count ? ` · ${paper.rag_manual_supplement_count} 份补录` : ''}</span></div>
    <div className="rag-paper-actions"><button className="btn small" onClick={open}>读取分析与来源<ChevronRight size={14} /></button>{safeUrl(paper.url) && <a href={safeUrl(paper.url)} target="_blank" rel="noreferrer" aria-label={`打开 ${paper.title} 的论文来源`}>论文来源<ExternalLink size={13} /></a>}</div>
  </article>;
}

function PaperDetails({ paper, close }: { paper: Json; close: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const id = useId();
  const [version, setVersion] = useState<string>(paper.rag_version_id);
  const [detail, setDetail] = useState<Json | null>(null);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const [tab, setTab] = useState('analysis');
  useEffect(() => {
    const element = dialog.current;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    element?.showModal();
    return () => { element?.close(); document.body.style.overflow = overflow; };
  }, []);
  useEffect(() => {
    const controller = new AbortController(); setDetail(null); setError('');
    api<Json>(`/literature/rag/${encodeURIComponent(version)}/summary`, { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setDetail(value); })
      .catch(err => { if (!controller.signal.aborted) setError((err as Error).message); });
    return () => controller.abort();
  }, [version, revision]);
  const currentPaper = detail?.paper ?? paper;
  const versions: Json[] = detail ? [{ version_id: version, analysis_created_at: detail.analysis_created_at }, ...(detail.other_analysis_versions ?? [])] : [];
  if (typeof document === 'undefined') return null;
  return <ReferenceNames records={[paper, detail]}>{createPortal(<dialog className="paper-rag-dialog" ref={dialog} role="dialog" aria-modal="true" aria-labelledby={`${id}-title`} onCancel={event => { event.preventDefault(); close(); }} onClick={event => { if (event.target === event.currentTarget) close(); }}>
    <div className="rag-dialog-frame"><header className="rag-dialog-heading"><div><span className="rag-dialog-eyebrow">文献详情 · {venueName(currentPaper)}</span><h2 id={`${id}-title`}><ReferenceText value={currentPaper.title} /></h2><p>{currentPaper.authors?.join(' · ') || '作者未记录'} · {currentPaper.year ?? '年份未记录'}</p></div><button className="rag-dialog-close" onClick={close} aria-label="关闭文献详情" autoFocus><X size={22} /></button></header>
      <div className="rag-dialog-tabs" aria-label="文献详情内容">{[['analysis', '分析详情'], ['source', '已保存原文'], ['metadata', '来源与版本']].map(([key, label]) => <button key={key} aria-pressed={tab === key} onClick={() => setTab(key)}>{label}</button>)}{detail?.original_pdf_available && <a className="btn small" href={`/api/literature/rag/${encodeURIComponent(version)}/pdf`} target="_blank" rel="noreferrer"><FileText size={14} />查看 PDF<ExternalLink size={12} /></a>}</div>
      <div className="rag-dialog-body">
        {error && <div className="rag-error" role="alert"><p>详情读取失败：<ReferenceText value={error} /></p><button className="btn small" onClick={() => setRevision(value => value + 1)}>重试</button></div>}
        {!detail && !error && <div className="rag-empty" role="status"><LoaderCircle className="spin" size={24} /><p>正在读取分析与来源…</p></div>}
        {detail && tab === 'analysis' && <PaperRagAnalysis detail={detail} />}
        {detail && tab === 'source' && <><p className="rag-source-notice">以下是已保存的提取文本，可能只覆盖原文部分内容。引文定位与此文本对应；科学主张尚未独立核验。<ReferenceText value={detail.source?.reported_coverage === 'partial' || detail.source?.text_truncated ? ' 当前版本保存的文本不完整。' : ''} /></p><SourceReader key={version} version={version} /></>}
        {detail && tab === 'metadata' && <div className="rag-metadata"><p>所有已绑定来源共同使用这个论文条目。不同原文版本分别保存，分析版本保留各自的来源与日期。</p><dl>{[['统一论文身份', detail.canonical_paper_id ?? paper.rag_entry_id], ['已绑定标识', detail.identifiers?.join('、')], ['原文版本数', detail.source_versions?.length], ['论文标识', currentPaper.id], ['DOI', currentPaper.doi], ['会议 / 期刊', venueName(currentPaper)], ['领域', ragDomainName(currentPaper)], ['领域分类依据', ragDomainBasis(currentPaper)], ['内容领域', currentPaper.research_domain], ['研究方向', currentPaper.research_topics?.join('、')], ['研究方向依据', currentPaper.research_classification ? '依据已保存的逐篇补录标注，仅用于检索' : null], ['分类参考片段', currentPaper.research_classification?.basis?.quote], ['关键词', currentPaper.keywords?.join('、')], ['原分析时间', detail.analysis_created_at], ['原任务', detail.origin_run_id], ['原笔记', detail.origin_note_id], ['分析版本', version], ['来源文档', detail.source?.title || detail.paper?.title || paper.title], ['已保存文本', `${detail.source?.text_chars ?? 0} 字符`], ['原文覆盖', detail.source?.reported_coverage ?? (detail.source?.text_truncated ? '提取文本截断' : '仅已保存文本，全文覆盖未确认')]].map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value || '未记录'}</dd></div>)}</dl>
          <div className="rag-source-links">{safeUrl(currentPaper.url) && <a className="btn small" href={safeUrl(currentPaper.url)} target="_blank" rel="noreferrer">论文来源<ExternalLink size={13} /></a>}{safeUrl(currentPaper.domain_classification?.source_url ?? currentPaper.ccf_domain_source_url) && <a className="btn small" href={safeUrl(currentPaper.domain_classification?.source_url ?? currentPaper.ccf_domain_source_url)} target="_blank" rel="noreferrer">领域分类来源<ExternalLink size={13} /></a>}{safeUrl(currentPaper.research_classification?.source_url) && <a className="btn small" href={safeUrl(currentPaper.research_classification.source_url)} target="_blank" rel="noreferrer">研究方向来源<ExternalLink size={13} /></a>}{detail.original_pdf_available && <a className="btn small" href={`/api/literature/rag/${encodeURIComponent(version)}/pdf?download=true`}><ArrowDownToLine size={14} />下载已保存 PDF</a>}</div>
          {versions.length > 1 && <label className="rag-filter"><span>查看其他分析版本</span><select value={version} onChange={event => { setVersion(event.target.value); setTab('analysis'); }}>{versions.map((item, index) => <option key={item.version_id} value={item.version_id}>{item.analysis_created_at?.slice(0, 10) ?? '时间未记录'} · 分析版本 {index + 1}</option>)}</select></label>}
          <p className="rag-source-notice">{detail.scope_notice || '历史任务保存的片段分析，保留原分析日期与来源；不代表本次新阅读、独立复现或创新性已验证。'}</p>
        </div>}
      </div>
    </div>
  </dialog>, document.body)}</ReferenceNames>;
}

function SourceReader({ version }: { version: string }) {
  const [cursor, setCursor] = useState({ offset: 0, sha: '' });
  const [page, setPage] = useState<Json | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController(); setLoading(true); setError('');
    const params = new URLSearchParams({ section: 'source', offset: String(cursor.offset), limit: '6000' });
    if (cursor.sha) params.set('expected_sha256', cursor.sha);
    api<Json>(`/literature/rag/${encodeURIComponent(version)}?${params}`, { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setPage(value); })
      .catch(err => { if (!controller.signal.aborted) setError((err as Error).message); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [version, cursor, revision]);
  const current = page?.offset === cursor.offset ? page : null;
  return <div className="rag-source-reader">
    {error && <div className="rag-error" role="alert"><p>原文读取失败：<ReferenceText value={error} /></p><button className="btn small" onClick={() => setRevision(value => value + 1)}>重试读取原文</button></div>}
    {loading && <p role="status">正在读取原文片段…</p>}
    {current && <><div className="rag-source-pagination"><span>字符 {current.offset}–<ReferenceText value={current.offset + Array.from(current.text).length} /> / {current.total_chars}</span><div><button className="btn small" disabled={loading || !current.offset} onClick={() => setCursor({ offset: Math.max(0, current.offset - 6000), sha: current.sha256 })}><ChevronLeft size={14} />上一段</button><button className="btn small" disabled={loading || current.next_offset === null} onClick={() => setCursor({ offset: current.next_offset, sha: current.sha256 })}>下一段<ChevronRight size={14} /></button></div></div><pre className="rag-source-text"><ReferenceText value={current.text} /></pre></>}
  </div>;
}

export function PaperRagAnalysis({ detail }: { detail: Json }) {
  const note = detail.analysis;
  return <ReferenceNames records={detail}><div className="reading-note-content">
    {detail.provenance === 'external_agent_analysis' && <section className="reading-note-section">
      <h4>外部 Agent 分析</h4><p><ReferenceText value={detail.scope_notice} /></p>
      <p>执行者：<ReferenceText value={note.producer?.agent} /> · 模型：<ReferenceText value={note.producer?.model} /> · 分析时间：<ReferenceText value={note.producer?.analyzed_at} /></p>
      <p>提交文本范围：<ReferenceText value={detail.external_source?.reported_coverage} /></p>
      {note.sections?.map((section: Json, i: number) => <section key={i}><h4><ReferenceText value={section.topic} /> · <ReferenceText value={section.epistemic_status} /></h4><p><ReferenceText value={section.text} /></p>
        <details><summary>查看原文引用</summary>{section.citations?.map((ref: Json, j: number) => <blockquote key={j}><code>字符 <ReferenceText value={ref.start} />–<ReferenceText value={ref.end} /></code><p><ReferenceText value={ref.quote} /></p></blockquote>)}</details>
      </section>)}
    </section>}
    {detail.manual_supplements?.map((review: Json) => <section className="reading-note-section" key={review.curation_id}>
      <h4>逐篇核查补录 · {review.reviewed_at?.slice(0, 10)}</h4>
      <p>Codex 根据用户要求核对原文关键段落。结果为论文报告值，尚未独立复现；不代替本任务阅读或新颖性审查。</p>
      <p>数据集：{review.datasets?.join('、') || '不适用（理论论文）'}</p>
      <p>比较对象：{review.baselines?.join('、')}</p>
      {review.sections?.map((section: Json, i: number) => <section key={i}><h4><ReferenceText value={section.label} /></h4><p><ReferenceText value={section.text} /></p>
        <details><summary>查看原文定位</summary>{section.citations?.map((ref: Json, j: number) => <p key={j}>页码：{ref.pages?.join('、') || '网页文本'} · 字符 <ReferenceText value={ref.start} />–<ReferenceText value={ref.end} /></p>)}</details>
      </section>)}
      <h4>仍待核实</h4><ul>{review.remaining_gaps?.map((gap: string, i: number) => <li key={i}>{gap}</li>)}</ul>
      {review.sources?.map((source: Json) => safeUrl(source.url) && <p key={source.text_sha256}><a href={safeUrl(source.url)} target="_blank" rel="noreferrer">补录所据原文<ExternalLink size={12} /></a></p>)}
    </section>)}
    {detail.other_analysis_versions?.length > 0 && <details><summary>其他历史笔记 · {detail.other_analysis_versions.length} 份</summary>
      {detail.other_analysis_versions.map((version: Json) => <section key={version.version_id}><h4>{version.analysis_created_at?.slice(0, 10)} · 历史任务 <ReferenceText value={version.origin_run_id} /></h4><p><ReferenceText value={version.analysis.summary} /></p><p><ReferenceText value={version.analysis.method} /></p><p>数据集：{version.analysis.datasets?.join('、') || '此版本未记录'}</p><p>基线：{version.analysis.baselines?.join('、') || '此版本未记录'}</p></section>)}
    </details>}
    {detail.provenance === 'external_agent_analysis'
      ? <p>以下为外部提交的结构化分析；不代表本任务已阅读、独立复现或批准其科学主张。</p>
      : <p>以下为原历史笔记（其缺失项反映当时阅读范围）：任务 <ReferenceText value={detail.origin_run_id} /> · 原分析日期 {detail.analysis_created_at?.slice(0, 10)} · 已读所列片段，科学主张尚未核验。</p>}
    {[['summary', '阅读摘要'], ['research_question', '研究问题'], ['method', '架构与方法'], ['relevance', '原任务相关性（仅供参考）']].map(([key, label]) => <section className="reading-note-section" key={key}><h4><ReferenceText value={label} /></h4><p><ReferenceText value={note[key]} /></p></section>)}
    {[['datasets', '数据集'], ['baselines', '比较基线'], ['limitations', '局限'], ['missing_information', '缺失信息']].map(([key, label]) => <section key={key}><h4><ReferenceText value={label} /></h4>{note[key]?.length ? <ul>{note[key].map((value: string, i: number) => <li key={i}><ReferenceText value={value} /></li>)}</ul> : <p>未记录</p>}</section>)}
    <details><summary>原文摘录与定位<ChevronDown size={12} /></summary>{note.excerpts?.map((item: Json, i: number) => <blockquote key={i}><code><ReferenceText value={item.locator} /></code><p><ReferenceText value={item.quote} /></p></blockquote>)}</details>
    <details><summary>原论文报告值（非本机实验）<ChevronDown size={12} /></summary>{note.results?.length ? note.results.map((item: Json, i: number) => <p key={i}><ReferenceText value={item.dataset} /> · <ReferenceText value={item.metric} /> · <ReferenceText value={item.value} /><br /><ReferenceText value={item.setting} /><br /><code><ReferenceText value={item.locator} /></code></p>) : <p>未提取到可报告值。</p>}</details>
  </div></ReferenceNames>;
}
