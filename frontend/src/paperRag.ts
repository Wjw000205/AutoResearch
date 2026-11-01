export interface RagFilters {
  q: string; venue: string; domain: string; topic: string; year_from: string; year_to: string; ccf_rank: string;
  sort: 'relevance' | 'publication_recent' | 'analysis_recent' | 'title';
}
export const emptyRagFilters: RagFilters = { q: '', venue: '', domain: '', topic: '', year_from: '', year_to: '', ccf_rank: '', sort: 'relevance' };
export interface RagFacet { value: string; label: string; count: number }
export function ragDomainName(paper: Record<string, any>) {
  return paper.ccf_domain || paper.research_domain || '领域未记录';
}
export function ragDomainBasis(paper: Record<string, any>) {
  return paper.domain_classification?.label || (paper.import_provenance === 'external_agent_analysis' && paper.ccf_domain
    ? '外部提交的分类，待核验' : '分类来源未记录');
}
export function ragCatalogPath(filters: RagFilters, offset = 0, limit = 12) {
  const params = new URLSearchParams();
  Object.entries(filters).forEach(([key, value]) => { if (value.trim()) params.set(key, value.trim()); });
  params.set('offset', String(offset)); params.set('limit', String(limit));
  return `/literature/rag?${params}`;
}
export function ragYearError(filters: Pick<RagFilters, 'year_from' | 'year_to'>) {
  return filters.year_from && filters.year_to && Number(filters.year_from) > Number(filters.year_to) ? '起始年份不能晚于结束年份，请调整年份范围。' : '';
}
export const ragAspectNames: Record<string, string> = { architecture: '架构与方法', contributions: '作者贡献与创新点', baselines: '比较基线' };
export function ragEvidencePreview(evidence: Record<string, { fields?: Record<string, unknown> }> = {}) {
  const architecture = evidence.architecture?.fields;
  const contributions = evidence.contributions?.fields;
  const baselines = evidence.baselines?.fields;
  const value = architecture?.method || contributions?.summary || contributions?.research_question
    || contributions?.method || (Array.isArray(baselines?.baselines) ? baselines.baselines.join('；') : '') || baselines?.method;
  const text = value ? String(value) : '在详情中查看分析与原文引用。';
  return text.length > 220 ? `${text.slice(0, 220)}…` : text;
}
