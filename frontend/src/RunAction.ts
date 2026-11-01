import { ApiResponseError, type Json } from './api';

export type RunAction = 'start' | 'resume' | 'pause' | 'cancel';
export type RunActionState = { action: RunAction | null; phase: 'idle' | 'pending' | 'checking' | 'unconfirmed'; error: string };
export const RUN_ACTION_WAIT_NOTICE_MS = 20_000;
export const initialRunActionState = (): RunActionState => ({ action: null, phase: 'idle', error: '' });

/** A slow read does not cancel a mutation. Never retry a POST to discover its outcome. */
export function createRunActionController(options: {
  runId: string;
  request: (action: RunAction) => Promise<Json>;
  changed: (state: RunActionState) => void;
  accepted: (action: RunAction, status: string) => void;
  refresh: () => void;
  receiptRequired?: boolean;
  schedule?: typeof setTimeout;
  cancel?: typeof clearTimeout;
}) {
  let state = initialRunActionState(), generation = 0, stopped = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const schedule = options.schedule ?? setTimeout, cancel = options.cancel ?? clearTimeout;
  const publish = (value: RunActionState) => { state = value; if (!stopped) options.changed(value); };
  const clear = () => { if (timer !== undefined) cancel(timer); timer = undefined; };
  function accepted(status: string) {
    const action = state.action;
    if (!action || stopped) return;
    generation++; clear(); publish(initialRunActionState()); options.accepted(action, status);
  }
  return {
    get: () => state,
    observe(run?: Json | null) {
      // start/resume admission is persisted as running. A paused stop request
      // still has cleanup in flight, so status alone cannot settle pause/cancel.
      if (!options.receiptRequired && !stopped && run?.id === options.runId && run.status === 'running'
        && ['start', 'resume'].includes(state.action ?? '')) accepted(run.status);
    },
    async send(action: RunAction) {
      if (stopped || state.action) return false;
      const current = ++generation;
      publish({ action, phase: 'pending', error: '' });
      timer = schedule(() => {
        if (stopped || current !== generation) return;
        timer = undefined; publish({ ...state, phase: 'checking' }); options.refresh();
      }, RUN_ACTION_WAIT_NOTICE_MS);
      try {
        const result = await options.request(action);
        if (stopped || current !== generation) return true;
        if (result?.id !== options.runId || !['draft', 'running', 'paused', 'blocked', 'failed', 'cancelled', 'completed'].includes(result.status)) throw new Error('操作响应未包含当前任务的有效状态。');
        accepted(result.status);
      } catch (error) {
        if (stopped || current !== generation) return true;
        clear();
        const message = error instanceof Error ? error.message : String(error);
        // An explicit client error rejects admission. Network/5xx failures may
        // follow a committed mutation, so keep the duplicate-submission guard.
        if (error instanceof ApiResponseError && [400, 401, 403, 404, 405, 409, 422, 429].includes(error.status)) {
          publish({ ...initialRunActionState(), error: message });
        } else publish({ action, phase: 'unconfirmed', error: message });
        options.refresh();
      }
      return true;
    },
    stop() { stopped = true; generation++; clear(); },
  };
}
