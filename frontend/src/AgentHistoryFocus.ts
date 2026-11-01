import { textOf, type AgentHistoryEntry } from './api';

export function latestPublicOutput(history: AgentHistoryEntry[]) {
  return [...history].filter(entry => entry.kind === 'output')
    .sort((a, b) => b.at.localeCompare(a.at) || b.id.localeCompare(a.id))[0];
}

export function publicSummary(entry: AgentHistoryEntry | undefined): string {
  if (!entry) return '';
  if (entry.consensus?.rationale) return textOf(entry.consensus.rationale);
  const fields = ['rationale', 'summary', 'explanation', 'hypothesis', 'architecture', 'research_question', 'markdown', 'title'];
  try {
    const output = JSON.parse(entry.text);
    if (output && typeof output === 'object') {
      for (const field of fields) if (typeof output[field] === 'string' && output[field]) return output[field];
      if (typeof output.document_id === 'string' && typeof output.spec_hash === 'string') return '已保存并提交实现规范文档。';
      return Array.isArray(output.fixes) ? `已提交 ${output.fixes.length} 条修改意见，点击查看详细记录。` : '已保存结构化公开记录，点击查看详情。';
    }
  } catch {
    // Recent history may end midway through JSON. Extract only a complete JSON
    // string value; never display raw serialized fields as the speech preview.
    if (entry.truncated && /^\s*[\[{]/.test(entry.text)) {
      for (const field of fields) {
        const match = entry.text.match(new RegExp(`"${field}"\\s*:\\s*("(?:[^"\\\\]|\\\\.)*")`));
        if (match) { try { const value = JSON.parse(match[1]); if (value) return value; } catch { /* Keep the preview conservative. */ } }
      }
      return '已保存公开输出，点击读取全文。';
    }
  }
  return textOf(entry.text);
}

export function historyDisplay(history: AgentHistoryEntry[], filter: string, count: number, target?: AgentHistoryEntry) {
  const entries = [...history].sort((a, b) => b.at.localeCompare(a.at) || b.id.localeCompare(a.id));
  const focused = target && (filter === 'all' || filter === target.kind)
    ? entries.find(entry => entry.id === target.id) ?? target : undefined;
  const remaining = entries.filter(entry => (filter === 'all' || entry.kind === filter) && entry.id !== focused?.id);
  return { focused, visible: [...(focused ? [focused] : []), ...remaining.slice(0, count)], more: Math.max(0, remaining.length - count) };
}
