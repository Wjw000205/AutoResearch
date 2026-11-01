import { ReferenceText } from './ReferenceNames';
import { BookOpen, ChevronDown, CircleAlert } from 'lucide-react';

export interface LiteratureCoverageRecord {
  as_of_date: string; recent_window: { year_from: number; year_to: number; date_granularity: 'year' | 'day'; date_from?: string; date_to?: string };
  target_distinct: number; distinct_count: number; verified_count: number; recent_verified_count: number;
  latest_publication_year: number | null; query_count: number; completed_query_count: number;
  sources: Record<string, { queries_attempted: number; queries_succeeded: number; queries_failed: number; returned_count: number; distinct_count: number }>;
  retrieval_errors: { query_index: number; source: string; error: string; status_code?: number | null }[];
  gaps: { code: string; message: string }[]; exhaustive: false; full_text_read_count: number; coverage_note: string;
}
const count = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? String(value) : '未记录';
const sourceName = (source: string) => ({ semantic_scholar: 'Semantic Scholar', crossref: 'Crossref', openalex: 'OpenAlex',
  arxiv: 'arXiv', pubmed: 'PubMed', europe_pmc: 'Europe PMC', paper_rag: '本地论文库' }[source] ?? source);

export function LiteratureCoverage({ coverage }: { coverage?: LiteratureCoverageRecord | null }) {
  if (!coverage) return <section className="literature-coverage coverage-unavailable"><h3><BookOpen size={17} />文献检索覆盖</h3><p>此任务尚未保存检索覆盖记录，不能根据现有文献数量推定检索完整度。</p></section>;
  const recent = coverage.recent_window;
  const sources = Object.entries(coverage.sources ?? {});
  const gaps = Array.isArray(coverage.gaps) ? coverage.gaps : [];
  const errors = Array.isArray(coverage.retrieval_errors) ? coverage.retrieval_errors : [];
  return <section className="literature-coverage" aria-label="文献检索覆盖记录"><header><h3><BookOpen size={17} />文献检索覆盖</h3><span>截至 {coverage.as_of_date || '未记录日期'}</span></header><div className="literature-coverage-stats"><div><strong>{count(coverage.distinct_count)}<small> / {count(coverage.target_distinct)}</small></strong><span>不同论文 / 检索目标</span></div><div><strong>{count(coverage.verified_count)}</strong><span>标识与元数据已核验</span></div><div><strong>{count(coverage.recent_verified_count)}</strong><span>{recent?.date_granularity === 'day' ? '近 12 个月已核验' : recent && recent.year_to - recent.year_from === 1 ? '近两年已核验' : '近期已核验'}</span></div><div><strong>{count(coverage.completed_query_count)}<small> / {count(coverage.query_count)}</small></strong><span>已完成 / 计划检索批次</span></div></div><p className="coverage-window">近期窗口：<ReferenceText value={recent ? recent.date_from ? `${recent.date_from} 至 ${recent.date_to}（按发表日期）` : `${recent.year_from}–${recent.year_to}（按出版年份）` : '未记录'} /> · 最新出版年份：<ReferenceText value={count(coverage.latest_publication_year)} /></p><div className="coverage-limit-note"><CircleAlert size={15} /><span>当前结果非穷尽检索。元数据核验不等于阅读论文全文；本覆盖记录的全文阅读计数：{count(coverage.full_text_read_count)}。</span></div>{sources.length > 0 && <div className="coverage-source-table"><table><caption>检索来源覆盖 · 各来源论文可能重叠，不能直接相加</caption><thead><tr><th>来源</th><th>尝试批次</th><th>成功</th><th>失败</th><th>返回记录</th><th>不同论文</th></tr></thead><tbody>{sources.map(([source, record]) => <tr key={source}><th scope="row">{sourceName(source)}</th><td>{count(record.queries_attempted)}</td><td>{count(record.queries_succeeded)}</td><td>{count(record.queries_failed)}</td><td>{count(record.returned_count)}</td><td>{count(record.distinct_count)}</td></tr>)}</tbody></table></div>}<div className="coverage-gaps"><h4>尚存缺口</h4>{gaps.length ? <ul>{gaps.map((gap, index) => <li key={`${gap.code}-${index}`}><span><ReferenceText value={gap.message} /></span><code><ReferenceText value={gap.code} /></code></li>)}</ul> : <p>本次记录没有列出具体缺口，仍不代表已覆盖全部相关研究。</p>}</div>{errors.length > 0 && <details className="coverage-errors"><summary>检索失败记录 · {errors.length} 条<ChevronDown size={13} /></summary>{errors.map((error, index) => <article key={index}><strong>{sourceName(error.source)}{error.status_code != null && ` · HTTP ${error.status_code}`}</strong><code>query_index={error.query_index}</code><p><ReferenceText value={error.error} /></p></article>)}</details>}{coverage.coverage_note && <details className="coverage-original-note"><summary>原始覆盖说明<ChevronDown size={12} /></summary><p><ReferenceText value={coverage.coverage_note} /></p></details>}</section>;
}
