import { ReferenceText } from './ReferenceNames';
import { useRef, useState } from 'react';
import { ArrowDownToLine, CircleAlert, LoaderCircle, Trash2, X } from 'lucide-react';
import { api, type Run } from './api';
import { useDialogKeyboard } from './useDialogKeyboard';

export interface DeleteRunResult { deleted: true; run_id: string; billing_retained: true }
type DeleteTarget = Pick<Run, 'id' | 'title' | 'status'>;
type DeleteRequest = (path: string, options: RequestInit) => Promise<DeleteRunResult>;

export function createRunDeletionRequest(runId: string, request: DeleteRequest = (path, options) => api<DeleteRunResult>(path, options)) {
  let pending: Promise<DeleteRunResult> | null = null;
  let completed: DeleteRunResult | null = null;
  return () => {
    if (completed) return Promise.resolve(completed);
    if (pending) return pending;
    pending = request(`/runs/${encodeURIComponent(runId)}`, { method: 'DELETE' }).then(result => {
      if (result?.deleted !== true || result.run_id !== runId || result.billing_retained !== true) throw new Error('删除响应不完整或任务身份不匹配，请刷新任务列表确认。');
      completed = result;
      return result;
    }).finally(() => { pending = null; });
    return pending;
  };
}

export const deletionNavigationTarget = (currentRunId: string | null, deletedRunId: string) => currentRunId === deletedRunId ? 'runs' : null;

export function DeleteResearchDialog({ run, close, onDeleted }: { run: DeleteTarget; close: () => void; onDeleted: (result: DeleteRunResult) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const busyRef = useRef(false);
  const request = useRef(createRunDeletionRequest(run.id));
  const cancel = () => { if (!busyRef.current) close(); };
  useDialogKeyboard(cancel, busy);
  async function confirmDelete() {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(true); setError('');
    let result: DeleteRunResult | undefined;
    try { result = await request.current(); }
    catch (failure) { setError((failure as Error).message); }
    finally { busyRef.current = false; setBusy(false); }
    if (result) onDeleted(result);
  }
  return <div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) cancel(); }}><section className="modal delete-research-modal" role="dialog" aria-modal="true" aria-labelledby="delete-research-title" aria-describedby="delete-research-description" aria-busy={busy}><div className="modal-header"><div><div className="eyebrow">删除研究</div><h2 id="delete-research-title">删除这项研究？</h2><p id="delete-research-description">此操作会永久删除下方研究的任务数据，无法撤销。</p></div><button type="button" className="icon-btn" aria-label="关闭删除确认" disabled={busy} onClick={cancel}><X size={20} /></button></div><div className="modal-body"><div className="delete-target"><Trash2 size={20} /><div><strong><ReferenceText value={run.title} /></strong><code><ReferenceText value={run.id} /></code></div></div><ul className="delete-scope"><li>删除任务、私有记忆、实验记录、代码、报告与检查点。</li><li>保留共享资料、供应商设置和已经产生的计费记录。</li><li>{run.status === 'running' ? '此任务正在运行，会先安全停止，再执行删除。' : '运行中的任务会先安全停止，再执行删除。'}</li></ul><p className="delete-export-note">需要保留研究成果，可先下载完整研究归档。</p>{busy ? <span className="btn small delete-export-disabled" aria-disabled="true"><ArrowDownToLine size={14} />正在删除，导出已暂停</span> : <a className="btn small" href={`/api/runs/${encodeURIComponent(run.id)}/export`} download><ArrowDownToLine size={14} />先导出研究归档</a>}{error && <div className="error-box delete-research-error" role="alert"><CircleAlert size={18} /><span>删除未完成：<ReferenceText value={error} /></span></div>}</div><div className="modal-footer"><button type="button" className="btn" autoFocus disabled={busy} onClick={cancel}>保留研究</button><button type="button" className="btn btn-danger" disabled={busy} onClick={() => void confirmDelete()}>{busy ? <LoaderCircle size={15} className="spin" /> : <Trash2 size={15} />}{busy ? '正在停止并删除…' : '确认删除研究'}</button></div></section></div>;
}
