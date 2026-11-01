import { ChevronLeft, ChevronRight } from 'lucide-react';

export const RECORD_PAGE_SIZE = 30;

export const memoryPagePath = (query: string, runId: string, offset = 0) =>
  `/memory?q=${encodeURIComponent(query)}&offset=${offset}&limit=${RECORD_PAGE_SIZE}${runId ? `&run_id=${encodeURIComponent(runId)}` : ''}`;

export const memoryDetailPath = (id: string, runId: string, open: boolean) => open
  ? `/memory/${encodeURIComponent(id)}${runId ? `?run_id=${encodeURIComponent(runId)}` : ''}` : null;

export const billingPagePath = (runId: string, offset = 0, nativeOffset = 0, nativeOpen = false) =>
  `/billing?offset=${offset}&limit=${RECORD_PAGE_SIZE}&native_offset=${nativeOffset}&native_limit=${nativeOpen ? RECORD_PAGE_SIZE : 0}${runId ? `&run_id=${encodeURIComponent(runId)}` : ''}`;

export function RecordPagination({ label, offset, count, total, limit = RECORD_PAGE_SIZE, loading = false, onOffset }: {
  label: string; offset: number; count: number; total?: number; limit?: number; loading?: boolean; onOffset: (offset: number) => void;
}) {
  const knownTotal = typeof total === 'number' && Number.isSafeInteger(total) && total >= 0 ? total : undefined;
  const hasNext = knownTotal === undefined ? count === limit : offset + count < knownTotal;
  return <nav className="record-pagination" aria-label={`${label}分页`}>
    <span role="status">{count ? `第 ${offset + 1}–${offset + count} 条` : '本页暂无记录'}{knownTotal !== undefined && `，共 ${knownTotal} 条`} · 每页 {limit} 条</span>
    <div><button type="button" className="btn small" aria-label={`上一页${label}`} disabled={loading || !offset}
      onClick={() => onOffset(Math.max(0, offset - limit))}><ChevronLeft size={14} />上一页</button>
      <span>第 {Math.floor(offset / limit) + 1} 页</span>
      <button type="button" className="btn small" aria-label={`下一页${label}`} disabled={loading || !hasNext}
        onClick={() => onOffset(offset + limit)}>下一页<ChevronRight size={14} /></button></div>
  </nav>;
}
