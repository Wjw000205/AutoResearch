export interface ResourceOptions<T> {
  request: (signal: AbortSignal) => Promise<T>;
  success: (value: T) => void;
  failure: (error: Error) => void;
  interval?: number;
  timeoutMs?: number;
  visible: () => boolean;
  schedule?: typeof setTimeout;
  cancel?: typeof clearTimeout;
}

export const RESOURCE_READ_TIMEOUT_MS = 20_000;

// Schedule after settlement. Cancelled reads cannot publish even when a
// transport ignores AbortSignal. Deadlines include response body decoding.
export function createResourceSession<T>(options: ResourceOptions<T>) {
  let stopped = false, pending = false, settled = false;
  let controller: AbortController | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const schedule = options.schedule ?? setTimeout;
  const cancel = options.cancel ?? clearTimeout;
  function clear() { if (timer !== undefined) cancel(timer); timer = undefined; }
  function next() {
    if (!stopped && options.visible() && options.interval) timer = schedule(() => void load(), options.interval);
  }
  async function load() {
    clear();
    if (stopped || pending || !options.visible() || settled && !options.interval) return;
    pending = true;
    const request = new AbortController(); controller = request;
    let timedOut = false;
    let deadline: ReturnType<typeof setTimeout> | undefined;
    let interrupt: () => void = () => {};
    const cancelled = new Promise<never>((_resolve, reject) => {
      interrupt = () => reject(new Error('读取已取消'));
      request.signal.addEventListener('abort', interrupt, { once: true });
    });
    if (options.timeoutMs && options.timeoutMs > 0) {
      deadline = schedule(() => { timedOut = true; request.abort(); }, options.timeoutMs);
    }
    try {
      const value = await Promise.race([options.request(request.signal), cancelled]);
      if (!stopped && !request.signal.aborted) options.success(value);
    } catch (error) {
      if (!stopped && (timedOut || !request.signal.aborted)) {
        options.failure(timedOut ? new Error(`读取超时（${Math.ceil(options.timeoutMs! / 1000)} 秒内未完成），请重新读取。`) : error instanceof Error ? error : new Error(String(error)));
      }
    } finally {
      if (deadline !== undefined) cancel(deadline);
      request.signal.removeEventListener('abort', interrupt);
      pending = false;
      if (!request.signal.aborted || timedOut) settled = true;
      // Visibility can return while cancellation is still settling.
      // A timeout follows the normal polling delay, never an immediate loop.
      if (!stopped && request.signal.aborted && !timedOut && options.visible()) void load();
      else next();
    }
  }
  return {
    start: load,
    visibilityChanged() { clear(); if (options.visible()) void load(); else controller?.abort(); },
    stop() { stopped = true; clear(); controller?.abort(); },
  };
}

// Only selected projections are cached: one revision per path, bounded paths.
export class ProjectionCache {
  private entries = new Map<string, { revision: string; value: unknown }>();
  constructor(private readonly limit = 16) {}
  get<T>(path: string, revision: string): T | undefined {
    const entry = this.entries.get(path);
    if (entry && entry.revision !== revision) { this.entries.delete(path); return undefined; }
    return entry?.value as T | undefined;
  }
  set(path: string, revision: string, value: unknown) {
    this.entries.delete(path); this.entries.set(path, { revision, value });
    while (this.entries.size > this.limit) this.entries.delete(this.entries.keys().next().value!);
  }
}

// Open detail is a snapshot. Background summary ticks can announce new records
// but must not turn into full-history polling or reset an open document reader.
export class ProjectionSnapshot {
  private key = '';
  private revision = '';
  select(scope: string | null, currentRevision: string, refresh: string) {
    const key = JSON.stringify([scope, refresh]);
    if (key !== this.key) { this.key = key; this.revision = currentRevision; }
    return { revision: this.revision, stale: Boolean(scope && this.revision !== currentRevision) };
  }
}
