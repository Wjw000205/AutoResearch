import { type Json, safeUrl } from './api';

export function LiteratureVenueControl({ value, onChange }: { value?: boolean; onChange: (value: boolean) => void }) {
  return <div className="field"><label><input type="checkbox" checked={value === true} onChange={event => onChange(event.target.checked)} /> 文献仅限 CCF A / B</label><small>依据 CCF 2026 第七版核验发表出处；两轮分析及 RAG 复用均适用。C 类、未确认出处和非正式长文不纳入。</small></div>;
}

export function LiteratureVenue({ paper }: { paper: Json }) {
  const ccf = paper.ccf;
  if (!ccf) return <span className="muted">CCF 出处待核验</span>;
  return <span className={ccf.eligible ? 'verified' : 'unverified'} title={ccf.venue_names?.join(' · ')}>
    {ccf.rank ? `CCF ${ccf.rank}` : 'CCF 未确认'} · {ccf.venue_name || ccf.venue_names?.join(' · ') || '发表出处待核验'}
    {safeUrl(ccf.source_url) && <a href={safeUrl(ccf.source_url)} target="_blank" rel="noreferrer">〔2026 目录〕</a>}
  </span>;
}
