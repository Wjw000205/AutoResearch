import type { Json } from './api';

const agents: Record<string, string> = { director: '研究总监', engineer: '实验工程师', scout: '文献研究员' };
const statuses: Record<string, string> = { started: '已开始', completed: '已完成', failed: '失败' };

export function ToolEventSummary({ name, role, status }: { name: string; role?: string; status?: string }) {
  return <div className="tool-event-summary"><span>调用工具</span><code>{name}</code><span>调用 agent：{role ? `${agents[role] ?? role} (${role})` : '未记录'}</span>{status && <span>{statuses[status] ?? status}</span>}</div>;
}

export function TimelineToolSummary({ event }: { event: Json }) {
  if (!['tool.started', 'tool.completed', 'tool.failed'].includes(event.kind) || typeof event.message !== 'string') return null;
  return <ToolEventSummary name={event.message} role={event.data?.role} status={event.kind.split('.')[1]} />;
}
