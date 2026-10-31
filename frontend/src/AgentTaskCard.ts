import { textOf, type ResearchMapAgent } from './api';
import { publicSummary } from './AgentHistoryFocus';
import { humanizeMessage } from './localization';
import { requestRecoveryText } from './RequestRecoveryDiagnostic';
import { reviewFeedback } from './ReviewFeedback';
import { experimentTaskDescription } from './ExperimentTaskDescriptions';

const outputs: Record<string, string> = {
  implementation_spec: '实现文档与保存提交记录',
  implementation_document_section: '本组审核结论、问题与修改建议',
  implementation_document_review: '整份实现文档的独立审核结论',
  spec_review: '实现规范的审核结论与修改建议',
  code_review: '代码审核结论与验收意见',
  code_repair_map: '逐条对应实现文档的修改清单',
  code_repair_audit: '修改依据与验证标准的独立核对结论',
  design_document_section: '本组架构审核意见',
  design_document_review: '架构文档的完整审核结论',
  design_revision: '修订后的架构文档',
  literature_reading: '原文证据与阅读笔记',
  literature_note_extract: '带来源的分析笔记',
  innovation_candidate: '创新候选及其证据',
  innovation_compare: '候选比较与选择依据',
  report: '研究结果报告',
};

function assignmentTaskLabel(task: string, purpose: string): string {
  if (purpose.split(':')[0] !== 'implementation_spec') return task;
  // Older APIs derived the author's version from its architecture input.
  // The exact author purpose retains the implementation's zero-based revision,
  // including save successors; an incomplete identity must not invent a version.
  const parts = task.split(/\s*·\s*/).filter(part => !/^第\s*\d+\s*版$/.test(part));
  const identity = /^implementation_spec:[^:\s]+:(\d+)(?::save:(\d+))?$/.exec(purpose);
  const revision = identity?.[0] === purpose ? Number(identity[1]) : NaN;
  const save = identity?.[2] === undefined ? 0 : Number(identity[2]);
  if (Number.isSafeInteger(revision) && revision >= 0 && revision < Number.MAX_SAFE_INTEGER
    && Number.isSafeInteger(save) && save >= 0) parts.splice(1, 0, `第 ${revision + 1} 版`);
  return parts.join(' · ') || '制定明确实现规范';
}

const suspendedLabels: Record<string, string> = {
  paused: '暂停前动作', interrupted: '中断前动作', blocked: '等待处理',
  failed: '失败时动作', cancelled: '取消前动作', pending: '待执行', queued: '待执行',
};

/** Live detail belongs to the current handoff; history hydration must not change it. */
export function agentCurrentActivity(agent: ResearchMapAgent) {
  const activity = agent.current_activity;
  if (!activity || agent.status === 'completed' || !agent.current_assignment
    || (activity.handoff_id && activity.handoff_id !== agent.current_assignment.id)) return undefined;
  const stopped = suspendedLabels[agent.status];
  return { ...activity,
    label: stopped && !/暂停|中断|等待处理|失败|取消|待执行/.test(activity.label)
      ? `${stopped} · ${activity.label.replace(/^正在/, '')}` : activity.label,
    toolState: activity.tool?.status === 'running' && agent.status === 'recovering'
      ? '待恢复的调用' : activity.tool?.status === 'running' && stopped
      ? ({paused: '调用已暂停', interrupted: '调用已中断', blocked: '调用状态待处理',
        failed: '调用状态待核对', cancelled: '调用已取消'} as Record<string, string>)[agent.status] ?? '等待调用'
      : activity.tool?.status === 'failed' && activity.tool.validation_code ? '保存校验未通过'
      : ({running: '正在调用', completed: '最近完成', failed: '调用失败'} as Record<string, string>)[activity.tool?.status ?? ''],
  };
}

/** A card follows one assignment; historical outputs belong in the history. */
export function agentTaskCard(agent: ResearchMapAgent) {
  const assignment = agent.current_assignment;
  const completed = agent.status === 'completed';
  const task = assignment?.label || (completed ? agent.last_task : agent.current_task) || '等待任务分配';
  const purpose = assignment?.purpose ?? '';
  const experimentTask = experimentTaskDescription(purpose, agent.role);
  const expected = experimentTask?.expected ?? outputs[purpose.split(':')[0]];
  const history = agent.history ?? [];
  const candidates = [...history, ...(agent.current_output ? [agent.current_output] : [])];
  const output = completed && assignment?.status === 'completed'
    ? candidates.filter(entry => entry.kind === 'output' && entry.status === 'completed'
      && entry.id === `${assignment.id}:output` && entry.purpose === assignment.purpose)
      .sort((a, b) => Number(Boolean(a.truncated)) - Number(Boolean(b.truncated)))[0] : undefined;
  const assignmentEntry = assignment ? history.find(entry => entry.kind === 'assignment' && entry.id === `${assignment.id}:assignment`) : undefined;
  const feedback = reviewFeedback(output);
  const activity = agentCurrentActivity(agent);
  const statusLabels: Record<string, string> = {
    completed: '任务输出', paused: '已暂停的任务', interrupted: '已中断的任务', recovering: '正在恢复的任务',
    blocked: '待处理的任务', failed: '失败的任务', cancelled: '已取消的任务',
    queued: '待执行任务', pending: '待执行任务', idle: '当前任务', running: '当前任务',
  };
  let action = humanizeMessage(requestRecoveryText(textOf(agent.current_action)));
  if (!action || ['等待执行记录', '等待下一次任务分派'].includes(action)) {
    action = agent.status === 'recovering' ? '宿主正在恢复并核验此任务。'
      : agent.status === 'running' ? '正在处理此任务，完成后在这里显示输出。'
      : ['pending', 'queued'].includes(agent.status) ? '等待开始执行。' : '';
  }
  return {
    title: statusLabels[agent.status] ?? '当前任务', task: experimentTask?.summary ?? humanizeMessage(assignmentTaskLabel(task, purpose)),
    output, entry: output ?? assignmentEntry,
    summary: completed ? output ? publicSummary(output) : '任务已结束，尚无已保存的公开输出。' : '',
    verdict: feedback.verdict,
    feedback,
    activity,
    action: completed || activity ? '' : action,
    expected: completed ? '' : expected,
    at: output?.at,
    link: output ? feedback.total ? `查看 ${feedback.total} 项处理建议与全文` : '查看本次输出全文' : '查看任务记录',
  };
}
