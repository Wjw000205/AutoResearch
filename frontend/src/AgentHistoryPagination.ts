import { api, type AgentHistoryEntry } from './api';

export interface AgentHistoryPage {
  role: string; history: AgentHistoryEntry[]; total: number; returned: number;
  has_older: boolean; next_cursor: string | null;
}
export interface AgentHistoryPages {
  history: AgentHistoryEntry[]; total: number; hasOlder: boolean; nextCursor: string | null;
  coveredIds: string[]; loaded: boolean; loading: 'refresh' | 'older' | null; error: string;
}

export function mergeAgentHistory(recent: AgentHistoryEntry[], full: AgentHistoryEntry[]) {
  const entries = new Map(full.map(entry => [entry.id, entry]));
  recent.forEach(entry => { const previous = entries.get(entry.id); entries.set(entry.id, previous && !previous.truncated && entry.truncated ? { ...entry, text: previous.text, truncated: false } : entry); });
  return [...entries.values()].sort((a, b) => b.at.localeCompare(a.at) || b.id.localeCompare(a.id));
}

export async function fetchAgentHistory(runId: string, role: string, cursor: string | null = null, signal?: AbortSignal): Promise<AgentHistoryPage> {
  const query = new URLSearchParams({ limit: '100' });
  if (cursor !== null) query.set('cursor', cursor);
  const page = await api<AgentHistoryPage>(`/runs/${encodeURIComponent(runId)}/agents/${encodeURIComponent(role)}/history?${query}`, { signal });
  if (page.role !== role) throw new Error('返回记录的角色归属不匹配');
  if (!Array.isArray(page.history) || !Number.isInteger(page.total) || page.total < 0 || page.returned !== page.history.length
      || typeof page.has_older !== 'boolean' || (page.has_older ? typeof page.next_cursor !== 'string' || !page.next_cursor : page.next_cursor !== null)) {
    throw new Error('历史分页信息不完整，请刷新后重试');
  }
  return page;
}

const emptyPages = (): AgentHistoryPages => ({ history: [], total: 0, hasOlder: false, nextCursor: null, coveredIds: [], loaded: false, loading: null, error: '' });

export function mergeAgentHistoryPage(previous: AgentHistoryPages, page: AgentHistoryPage, mode: 'refresh' | 'older'): AgentHistoryPages {
  const pageIds = page.history.map(entry => entry.id);
  const overlapsCovered = previous.loaded && pageIds.some(id => previous.coveredIds.includes(id));
  // A refresh with >100 new entries can leave a gap above older loaded pages.
  // Keep those records visible, but traverse from the new page until the gap is filled.
  const retainContinuation = mode === 'refresh' && overlapsCovered && page.has_older;
  return {
    history: mergeAgentHistory(page.history, previous.history), total: page.total,
    hasOlder: retainContinuation ? previous.hasOlder : page.has_older,
    nextCursor: retainContinuation ? previous.nextCursor : page.next_cursor,
    coveredIds: mode === 'older' || overlapsCovered ? [...new Set([...previous.coveredIds, ...pageIds])] : pageIds,
    loaded: true, loading: null, error: '',
  };
}

// Own one run at a time. Late responses from a prior run (including returning
// to that same run later) cannot replace state, errors, or cursors in this scope.
export function createAgentHistoryLoader(fetchPage = fetchAgentHistory) {
  let runId = '', generation = 0;
  const pages = new Map<string, AgentHistoryPages>();
  const controllers = new Map<string, AbortController>();
  function abort() { controllers.forEach(controller => controller.abort()); controllers.clear(); }
  return {
    setRun(nextRunId: string) { if (runId !== nextRunId) { abort(); runId = nextRunId; generation++; pages.clear(); } },
    get(role: string) { return pages.get(role); },
    cancelPending() { abort(); generation++; for (const [role, state] of pages) pages.set(role, { ...state, loading: null }); },
    async load(role: string, mode: 'refresh' | 'older', onChange: () => void) {
      const previous = pages.get(role) ?? emptyPages();
      if (previous.loading || (mode === 'older' && (!previous.loaded || !previous.hasOlder || !previous.nextCursor))) return;
      const scope = generation, requestRun = runId;
      const controller = new AbortController(); controllers.set(role, controller);
      const cursor = mode === 'older' ? previous.nextCursor : null;
      pages.set(role, { ...previous, loading: mode, error: '' }); onChange();
      try {
        const result = await fetchPage(requestRun, role, cursor, controller.signal);
        if (generation !== scope || runId !== requestRun) return;
        if (cursor && result.has_older && result.next_cursor === cursor) throw new Error('历史游标未推进，请刷新最新记录后重试');
        pages.set(role, mergeAgentHistoryPage(previous, result, mode));
      } catch (error) {
        if (generation !== scope || runId !== requestRun) return;
        pages.set(role, { ...previous, loading: null, error: error instanceof Error ? error.message : String(error) });
      }
      if (controllers.get(role) === controller) controllers.delete(role);
      if (generation === scope && runId === requestRun) onChange();
    },
  };
}
