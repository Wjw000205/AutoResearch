import { ReferenceText } from './ReferenceNames';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { ArrowDownToLine, CircleAlert, FileClock, LoaderCircle } from 'lucide-react';
import { api, type Json, type Run } from './api';

type PreviewRun = Pick<Run, 'id' | 'status' | 'updated_at'>;
export interface HistoricalPreview {
  kind: 'historical_preview'; status: string; run_id: string; source_cycle: number;
  approved: false; title: string; markdown: string; snapshot: Json;
}

export function historicalPreviewAvailability(run: PreviewRun, experiments: Json[]) {
  const cycles = [...new Set(experiments.filter(trial => trial.status === 'completed' && trial.params?.phase === 'validation')
    .map(trial => trial.research_cycle ?? 1).filter(cycle => Number.isInteger(cycle) && cycle > 0))].sort((a, b) => b - a);
  const eligibleStatus = ['blocked', 'failed', 'paused'].includes(run.status);
  return { cycles, enabled: eligibleStatus && cycles.length > 0,
    reason: !eligibleStatus ? '仅阻塞、失败或暂停的任务可读取历史验证预览。' : !cycles.length ? '尚无已完成的验证实验记录。' : '' };
}

export const historicalPreviewKey = (run: PreviewRun) => JSON.stringify([run.id, run.status, run.updated_at]);

export async function loadHistoricalReportPreview(run: PreviewRun, experiments: Json[], sourceCycle: number | null = null): Promise<HistoricalPreview> {
  const availability = historicalPreviewAvailability(run, experiments);
  if (!availability.enabled) throw new Error(availability.reason);
  if (sourceCycle !== null && !availability.cycles.includes(sourceCycle)) throw new Error('所选周期没有已完成的验证实验记录。');
  const query = sourceCycle === null ? '' : `?source_cycle=${encodeURIComponent(sourceCycle)}`;
  const result = await api<HistoricalPreview>(`/runs/${encodeURIComponent(run.id)}/historical-report-preview${query}`, { method: 'GET' });
  if (result.kind !== 'historical_preview' || result.approved !== false || result.run_id !== run.id || result.status !== run.status
      || !availability.cycles.includes(result.source_cycle) || sourceCycle !== null && result.source_cycle !== sourceCycle
      || typeof result.markdown !== 'string' || !result.markdown.trim()) {
    throw new Error('预览身份、周期或任务状态不匹配，请刷新任务后重试。');
  }
  return result;
}

export function HistoricalPreviewResult({ preview, renderMarkdown }: { preview: HistoricalPreview; renderMarkdown: (text: string) => ReactNode }) {
  const snapshot = preview.snapshot ?? {};
  const outcome = typeof snapshot.outcome === 'object' ? snapshot.outcome?.research_outcome : snapshot.outcome;
  const outcomeLabel = ({ negative: '历史验证负结果', inconclusive: '历史验证证据不足' } as Record<string, string>)[outcome] ?? '见预览记录';
  function download() {
    const url = URL.createObjectURL(new Blob([preview.markdown], { type: 'text/markdown;charset=utf-8' }));
    const link = document.createElement('a');
    link.href = url; link.download = `research-${preview.run_id.slice(0, 8)}-validation-cycle-${preview.source_cycle}-unreviewed-preview.md`;
    link.click(); URL.revokeObjectURL(url);
  }
  return <div className="historical-preview-result"><div className="historical-preview-warning" role="note"><CircleAlert size={17} /><div><strong>已有验证结果预览（未经三方审议）</strong><p>这是对过去验证记录的只读整理，不会恢复 AI、批准收束或将任务标为完成。</p><small>来源：第 {preview.source_cycle} 轮 · {outcomeLabel}</small></div></div><details className="historical-preview-evidence"><summary>预览依据与覆盖范围</summary>{snapshot.scope && <p><ReferenceText value={snapshot.scope} /></p>}<dl><dt>预览报告</dt><dd><ReferenceText value={preview.title} /></dd><dt>来源周期</dt><dd>第 {preview.source_cycle} 轮研究</dd></dl>{snapshot.coverage_gaps != null && <pre><ReferenceText value={snapshot.coverage_gaps} /></pre>}</details><div className="historical-preview-download"><button type="button" className="btn small" onClick={download}><ArrowDownToLine size={14} />下载未审议预览</button></div>{renderMarkdown(preview.markdown)}</div>;
}

export function HistoricalReportPreview({ run, experiments, renderMarkdown }: { run: PreviewRun; experiments: Json[]; renderMarkdown: (text: string) => ReactNode }) {
  const [sourceCycle, setSourceCycle] = useState('');
  const [preview, setPreview] = useState<HistoricalPreview | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const availability = historicalPreviewAvailability(run, experiments);
  if (!['blocked', 'failed', 'paused'].includes(run.status)) return null;
  async function readPreview() {
    setLoading(true); setError(''); setPreview(null);
    try {
      const result = await loadHistoricalReportPreview(run, experiments, sourceCycle ? Number(sourceCycle) : null);
      if (mounted.current) setPreview(result);
    } catch (failure) { if (mounted.current) setError((failure as Error).message); }
    finally { if (mounted.current) setLoading(false); }
  }
  return <section className="historical-report-preview" aria-label="已有验证结果预览"><header><FileClock size={19} /><div><h3>已有验证结果预览</h3><p>手动读取历史验证证据，作为现有报告的补充。未经三方审议；不会调用 AI 或改变任务状态。</p></div></header><div className="historical-preview-controls"><label>来源周期<select aria-label="选择历史验证来源周期" value={sourceCycle} disabled={loading || !availability.enabled} onChange={event => { setSourceCycle(event.target.value); setPreview(null); setError(''); }}><option value="">自动选择可用历史周期</option>{availability.cycles.map(cycle => <option key={cycle} value={cycle}>第 {cycle} 轮研究</option>)}</select></label><button type="button" className="btn" disabled={loading || !availability.enabled} onClick={() => void readPreview()}>{loading ? <LoaderCircle size={14} className="spin" /> : <FileClock size={14} />}{loading ? '读取预览中…' : '读取已有验证结果预览'}</button></div>{availability.reason && <p className="historical-preview-unavailable"><ReferenceText value={availability.reason} /></p>}{error && <p role="alert" className="historical-preview-error"><CircleAlert size={14} /><ReferenceText value={error} /></p>}{preview && <HistoricalPreviewResult preview={preview} renderMarkdown={renderMarkdown} />}</section>;
}
