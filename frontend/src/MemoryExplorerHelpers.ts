import { humanizeMessage } from './localization';

export const MEMORY_PAGE_SIZE = 20;
export const MEMORY_TEXT_PAGE_SIZE = 12000;
export const memoryRoleNames: Record<string, string> = { director: '研究总监', engineer: '实验工程师', scout: '文献研究员' };

export const memoryRoleName = (role: string) => memoryRoleNames[role] ?? ({ shared: '共享来源', library: '文献索引来源', system: '系统来源' } as Record<string, string>)[role] ?? role;
export function memoryPurposeName(purpose: string | null | undefined) {
  const base = (purpose ?? '').split(':')[0];
  if (!base) return '未记录任务类型';
  const name = humanizeMessage(base);
  return name === '其他记录' ? base : name;
}
export const inclusionLabel = (value: unknown) => typeof value === 'string' ? ({ retained: '捕获输入中保留', omitted: '编译后排除', reference_only: '仅引用标识', unknown: '是否进入输入未知' } as Record<string, string>)[value] ?? value : value == null ? '是否进入输入未知' : memoryReadableValue(value);
export const inclusionClassName = (value: unknown) => typeof value === 'string' && ['retained', 'omitted', 'reference_only', 'unknown'].includes(value) ? value : 'unknown';
export const dispatchLabel = (value: string | null | undefined) => ({ prepared_only: '已准备 · 尚无提交凭证', submitted: '已记录提交', completed: '已记录完成', unknown: '提交情况未知' } as Record<string, string>)[value ?? ''] ?? value ?? '提交情况未知';
export const captureLabel = (value: string | null | undefined) => ({ native_frozen_instruction: '本机 Codex 冻结任务正文', prepared_before_http: 'HTTP 请求前准备输入', compiled_snapshot: '编译后的上下文快照', runtime_capture: '运行时输入捕获' } as Record<string, string>)[value ?? ''] ?? value ?? '捕获阶段未记录';
export const requestStatusLabel = (value: string | null | undefined) => ({ prepared: '已准备', running: '进行中', submitted: '已提交', completed: '已完成', succeeded: '已完成', failed: '失败', interrupted: '已中断', cancelled: '已取消', paused: '已暂停', pending: '待处理', blocked: '等待处理', response_saved: '回复已保存', unknown: '未知' } as Record<string, string>)[value ?? ''] ?? value ?? '状态未记录';
export const memoryGapLabel = (value: string) => ({ complete_model_input: '缺少完整模型输入正文', frozen_system_text: '缺少冻结的系统提示词正文', exact_capture_binding: '缺少输入与请求的精确绑定', dispatch_confirmation: '缺少派发确认凭证', bound_context_snapshot: '缺少绑定的上下文快照', pre_compilation_memory_candidates: '缺少编译前记忆候选记录', capture_integrity: '输入捕获完整性校验未通过，正文与引用暂不可读', retrieval_results_truncated: '检索结果超过展示限额，仅显示部分记录', detail_preview_truncated: '详情超过展示限额，仅显示部分记录', detail_preview_truncated_use_context_pages: '详情摘录达到展示限额，请按需查看模型输入正文各段' } as Record<string, string>)[value] ?? value;
export function memoryReadableValue(value: unknown): string { return value == null ? '未记录' : typeof value === 'string' ? value : JSON.stringify(value, null, 2) ?? String(value); }

export function memoryReferenceLabel(value: unknown, field: 'type' | 'source') {
  const labels: Record<string, string> = field === 'type' ? { working_commit: '工作记忆提交引用', working_state: '工作记忆状态', scientific_state: '科研状态', document_working_notes: '文档工作笔记', task_material: '任务材料', tool_result: '工具返回结果', memory: '持久记忆', document: '研究文档', history: '角色历史' } : { captured_model_input: '捕获的模型输入', captured_context_reference: '捕获的上下文引用', captured_tool_result: '捕获的工具返回结果', pre_compilation_capture: '编译前捕获', archived_pointer: '历史引用标识' };
  const raw = memoryReadableValue(value);
  if (typeof value === 'string' && labels[value]) return `${labels[value]} · ${raw}`;
  if (value != null && typeof value === 'object' && '$text' in value) return `结构化文本引用 · ${raw}`;
  return raw;
}

export function memoryExplorerPath(runId: string, section: 'overview' | 'tasks' | 'requests' | 'entries' | 'history', filters: Record<string, string | number> = {}) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) if (value !== '') params.set(key, String(value));
  if (section !== 'overview') { if (!params.has('limit')) params.set('limit', String(MEMORY_PAGE_SIZE)); if (!params.has('offset')) params.set('offset', '0'); }
  return `/runs/${encodeURIComponent(runId)}/memory/${section}${params.size ? `?${params}` : ''}`;
}

export function memoryRequestDetailPath(runId: string, id: string) {
  return `/runs/${encodeURIComponent(runId)}/memory/requests/${encodeURIComponent(id)}`;
}

export function memoryEntriesPath(runId: string, scope = 'all', role = '', query = '', offset = 0) {
  return memoryExplorerPath(runId, 'entries', { role, scope, q: query, offset });
}

export function memoryTextPath(runId: string, kind: 'context' | 'entries' | 'history', id: string, offset = 0, part = 'combined') {
  const params = new URLSearchParams({ offset: String(offset), limit: String(MEMORY_TEXT_PAGE_SIZE) });
  const path = kind === 'context' ? `${memoryRequestDetailPath(runId, id)}/context` : `/runs/${encodeURIComponent(runId)}/memory/${kind}/${encodeURIComponent(id)}`;
  if (kind === 'context') params.set('part', part);
  return `${path}?${params}`;
}

export const memoryRunHref = (id: string) => `#/runs/${encodeURIComponent(id)}?tab=memory`;

export function selectMemoryRequestId(selectedId: string, items: readonly { id: string }[]) {
  return items.some(item => item.id === selectedId) ? selectedId : items[0]?.id ?? '';
}

export function readResearchTab(hash: string) {
  return new URLSearchParams(hash.split('?')[1] ?? '').get('tab') === 'memory' ? 'memory' : 'command';
}

