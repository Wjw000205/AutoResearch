import { api, ApiResponseError, type Json } from './api';
import type { RunAction } from './RunAction';

type Intent = { request_id: string; action: RunAction };
const actions = ['start', 'resume', 'pause', 'cancel'];

/** Reconnect by receipt ID. Replaying a lost POST uses the same durable ID. */
export function createDurableRunRequest(options: {
  runId: string;
  storage?: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;
  fetch?: (path: string, init: RequestInit) => Promise<Json>;
  identify?: () => string;
  retryMs?: number;
  timeoutMs?: number;
}) {
  const key = `autoresearch:run-control:${options.runId}`;
  const path = `/runs/${encodeURIComponent(options.runId)}/control-requests`;
  const fetch = options.fetch ?? api<Json>;
  let pending: Intent | null = null, stopped = false;
  let active: AbortController | undefined, wake: (() => void) | undefined;
  try {
    const saved = JSON.parse(options.storage?.getItem(key) ?? 'null');
    if (saved && /^[a-zA-Z0-9-]{16,80}$/.test(saved.request_id) && actions.includes(saved.action)) pending = saved;
  } catch { /* Storage can be disabled; the in-memory request still deduplicates. */ }
  function remember(value: Intent | null) {
    pending = value;
    try {
      if (value) options.storage?.setItem(key, JSON.stringify(value));
      else options.storage?.removeItem(key);
    } catch { /* Keep the active in-memory intent. */ }
  }
  async function call(url: string, init: RequestInit) {
    const controller = new AbortController(); active = controller;
    const timeout = setTimeout(() => controller.abort(), options.timeoutMs ?? 10_000);
    try { return await fetch(url, { ...init, cache: 'no-store', signal: controller.signal }); }
    finally { clearTimeout(timeout); if (active === controller) active = undefined; }
  }
  async function wait() {
    if (stopped) return;
    await new Promise<void>(resolve => {
      const timer = setTimeout(() => { wake = undefined; resolve(); }, options.retryMs ?? 2000);
      wake = () => { clearTimeout(timer); wake = undefined; resolve(); };
    });
  }
  return {
    pending: () => pending,
    async request(action: RunAction): Promise<Json> {
      if (pending && pending.action !== action) throw new Error('请先核对上一操作的回执。');
      const intent = pending ?? { action, request_id: (options.identify ?? (() => crypto.randomUUID()))() };
      remember(intent);
      // Always read first: remounting/reconnecting must not create a new operation.
      let submit = false;
      while (!stopped) {
        let receipt: Json;
        try {
          receipt = submit
            ? await call(path, { method: 'POST', body: JSON.stringify(intent) })
            : await call(`${path}/${encodeURIComponent(intent.request_id)}`, { method: 'GET' });
        } catch (error) {
          if (stopped) break;
          if (error instanceof ApiResponseError && error.status === 404 && !submit) {
            submit = true; continue;
          }
          if (error instanceof ApiResponseError && [400, 401, 403, 404, 405, 409, 422, 429].includes(error.status)) {
            remember(null); throw error;
          }
          // A timeout/5xx can occur after commit; query before attempting the same ID.
          submit = false; await wait(); continue;
        }
        submit = false;
        if (receipt.request_id !== intent.request_id || receipt.run_id !== options.runId || receipt.action !== action) {
          await wait(); continue;
        }
        if (receipt.status === 'completed' && receipt.result?.id === options.runId
            && ['draft', 'running', 'paused', 'blocked', 'failed', 'cancelled', 'completed'].includes(receipt.result.status)) {
          remember(null); return receipt.result;
        }
        if (['rejected', 'interrupted'].includes(receipt.status)) {
          remember(null); throw new ApiResponseError(receipt.error || '操作未完成，请核对任务状态后重试。', 409);
        }
        await wait();
      }
      throw new DOMException('页面已离开，操作回执将于返回后核对。', 'AbortError');
    },
    stop() { stopped = true; active?.abort(); wake?.(); },
  };
}
