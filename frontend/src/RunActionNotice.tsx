import { LoaderCircle, RefreshCw } from 'lucide-react';
import type { RunActionState } from './RunAction';

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
