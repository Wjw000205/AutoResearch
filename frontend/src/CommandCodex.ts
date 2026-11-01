import type { Json, ResearchMapLink } from './api';

export interface CodexExecution {
  kind: 'local_codex'; status: 'running' | 'preparing' | 'awaiting_confirmation'; action: string; source: string;
  workflow_id: string; phase: string; workspace?: string; thread_id?: string; turn_id?: string;
  request_status?: string; last_event_at?: string; latest_activity_type?: string; updated_at?: string;
}

export interface CodexParticipant {
  status: string; label: string; phase: string; summary: string; workflowId?: string;
  dispatched: boolean; requestStatus?: string; at?: string; files?: number;
}

const phaseNames: Record<string, string> = {
  specification: '等待实现文档', spec_review: '等待文档审查', implementing: '代码实现与修复',
  acceptance: '本地交付验收', code_review: '三方实际代码评审', completed: '实现交付流程已完成', blocked: '等待处理',
};
const receiptNames: Record<string, string> = {
  completed: '已返回', interrupted: '已中断', failed: '失败', unknown: '状态待核对', blocked: '等待处理',
};

// Use the existing lightweight summary. A research role's local_codex provider
// is not evidence that the independent code-writing executor is running.
export function codexParticipant(progress?: Json | null, execution?: CodexExecution | null, runStatus?: string): CodexParticipant {
  const dataset = execution?.source === 'persisted_dataset_codex';
  const workflow = dataset ? undefined : progress?.workflow;
  const phase = workflow?.status ?? execution?.phase;
  const requestStatus = workflow?.request_status ?? execution?.request_status;
  const dispatched = workflow ? workflow.request_dispatched === true || workflow.request_dispatched === 1 : Boolean(execution?.turn_id);
  const activeRun = runStatus === 'running' || (!runStatus && execution?.status === 'running');
  const running = activeRun && dispatched && phase === 'implementing'
    && (requestStatus === 'running' || (!requestStatus && execution?.status === 'running'));
  const result: CodexParticipant = {
    status: 'pending', label: '等待派发', phase: phaseNames[phase] ?? '代码实现与修复',
    summary: '实现文档通过后接收写码任务，按审核意见修复代码。',
    workflowId: workflow?.id ?? execution?.workflow_id, dispatched, requestStatus,
    at: workflow?.last_activity_at ?? execution?.last_event_at,
    files: typeof workflow?.file_count === 'number' && workflow.file_count > 0 ? workflow.file_count : undefined,
  };
  if (dataset) {
    result.phase = '实验配置与接口实现';
    result.summary = execution.action;
    if (!dispatched) result.label = '准备配置修复';
  }
  if (!dispatched) return dataset && runStatus === 'paused'
    ? { ...result, status: 'paused', label: '已暂停' } : result;
  if (['interrupted', 'failed', 'unknown', 'blocked'].includes(requestStatus ?? '')) {
    return { ...result, status: requestStatus === 'failed' ? 'failed' : 'blocked',
      label: `请求${receiptNames[requestStatus!]}`, summary: '最近一次写码请求需要核对，已保存的回执与交付记录可在实现详情中查看。' };
  }
  if (!activeRun && runStatus && runStatus !== 'completed') {
    return { ...result, status: runStatus === 'paused' ? 'paused' : 'blocked', label: runStatus === 'paused' ? '已暂停' : '等待处理',
      summary: '研究当前已停止推进；最近一次写码请求和交付记录已保留。' };
  }
  if (running) return { ...result, status: 'running', label: '正在实现', summary: execution?.action ?? '本机 Codex 正在实现或修复代码，完成后交回产物并进入验收。' };
  if (phase === 'code_review') return { ...result, status: 'idle', label: '等待代码评审', summary: '流程已进入三方代码评审，Codex 等待后续修改任务。' };
  if (phase === 'acceptance') return { ...result, status: 'idle', label: '等待本地验收', summary: '流程正在核验实际交付，验收结果与请求是否返回分别记录。' };
  if (phase === 'completed') return { ...result, status: 'completed', label: '流程已完成', summary: '实现交付流程已保存完成记录，可查看代码、回执及验收结果。' };
  if (requestStatus === 'completed') return { ...result, status: 'idle', label: '请求已返回', summary: '写码请求已返回，等待产物与验收检查。' };
  return { ...result, status: 'pending', label: '等待请求核对', summary: '已记录写码派发，等待可确认的执行状态。' };
}

export function codexExchanges(codex: CodexParticipant): ResearchMapLink[] {
  if (!codex.workflowId || !codex.dispatched) return [];
  const links: ResearchMapLink[] = [{ id: `${codex.workflowId}:dispatch`, from_role: 'harness', to_role: 'codex',
    purpose: 'codex_dispatch', status: codex.status === 'running' ? 'running' : 'completed', active: codex.status === 'running', at: codex.at ?? '' }];
  // A returned or failed receipt is an interaction, not a claim of acceptance.
  if (receiptNames[codex.requestStatus ?? '']) links.push({ id: `${codex.workflowId}:receipt`, from_role: 'codex', to_role: 'harness',
    purpose: 'codex_receipt', status: codex.requestStatus!, active: false, at: codex.at ?? '' });
  return links;
}
