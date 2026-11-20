import { LoaderCircle, RefreshCw } from 'lucide-react';
import type { RunActionState } from './RunAction';
import type { Json } from './api';

export function interruptionResumeReason(interruption?: Json | null): string | undefined {
  if (interruption?.status === 'draining') return '正在等待已启动工作和资源清理完成。';
  if (interruption?.status === 'failed') return '停止清理尚未确认完成，请重新停止后再恢复。';
}

export function AutomaticContinuationNotice({ continuation, status }: { continuation?: Json | null; status: string }) {
  if (continuation?.continuation_status === 'invalid_metadata' && ['paused', 'blocked'].includes(status)) {
    return <div className="inline-warning" role="status" aria-label="任务自动接续状态"><strong>自动接续记录异常</strong><p>本任务尚未重新派发。原检查点和已完成结果已保留，其他任务的恢复不受影响。</p></div>;
  }
  if (continuation?.desired !== 'running' || !['paused', 'blocked'].includes(status)) return null;
  const waiting = continuation.continuation_status === 'waiting_for_native_receipt';
  if (!waiting && status !== 'paused') return null;
  return <div className="inline-warning" role="status" aria-label="任务自动接续状态">
    <strong>{waiting ? '正在等待原任务的结果' : '检查点已保存，等待自动接续'}</strong>
    <p>{waiting ? '系统会自动核对原任务的结果并接续，无需重复启动。' : '执行条件满足后，系统会自动从检查点继续。'}</p>
  </div>;
}

export function InterruptionNotice({ interruption, retry, busy }: {
  interruption?: Json | null; retry: () => void; busy: boolean;
}) {
  const reason = interruptionResumeReason(interruption);
  if (!reason) return null;
  return <div className="inline-warning" role="status" aria-label="任务资源清理状态">
    <strong>{interruption?.status === 'failed' ? '停止清理尚未完成' : '正在停止并保存检查点'}</strong>
    <p>{reason} 已保存的实验与调用回执会继续保留，清理完成前不能重新启动研究。</p>
    {interruption?.status === 'failed' && <button type="button" className="btn small" disabled={busy} onClick={retry}>重新停止</button>}
  </div>;
}

export function RunActionNotice({ state, readError, reading, reload }: {
  state: RunActionState; readError: string; reading: boolean; reload: () => void;
}) {
  if (!state.action && !readError) return null;
  const label = state.action ? { start: '启动', resume: '恢复', pause: '暂停', cancel: '取消' }[state.action] : '';
  return <div className="inline-warning" role="status" aria-label="任务操作与状态核对">
    {state.action && <><strong>{state.phase === 'unconfirmed' ? `${label}请求的结果尚未确认`
      : state.phase === 'checking' ? `${label}请求仍在等待服务确认` : `正在提交${label}请求`}</strong>
      <p>{state.phase === 'unconfirmed' ? '服务端可能已接收请求。请读取最新任务状态核对，确认前不会重复提交。'
        : '正在等待操作回执并核对任务状态，请勿重复提交。页面状态读取超时不代表操作失败。'}</p></>}
    {readError && <p>页面状态读取未完成：{readError} {state.action ? '原操作请求仍按实际回执核对。' : '当前显示的是上次成功读取的状态。'}</p>}
    {state.phase === 'unconfirmed' && state.error && <details><summary>原始操作诊断</summary><pre>{state.error}</pre></details>}
    {state.action && <p className="muted small-text">网络恢复后会自动核对同一操作回执；刷新页面也会继续核对。</p>}
    <button type="button" className="btn small" disabled={reading} onClick={reload}>{reading ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />}重新读取状态</button>
    <p className="muted small-text">重新读取状态不会重新启动研究或重复发送操作。</p>
  </div>;
}
