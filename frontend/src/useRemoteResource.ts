import { useEffect, useRef, useState } from 'react';
import { api } from './api';
import { createResourceSession, ProjectionCache, ProjectionSnapshot, RESOURCE_READ_TIMEOUT_MS } from './RemoteResource';

const projections = new ProjectionCache();

export function useProjectionSnapshot(scope: string | null, revision: string, externalRefresh: string | number = 0) {
  const selection = useRef(new ProjectionSnapshot());
  const [manual, setManual] = useState(0);
  const snapshot = selection.current.select(scope, revision, `${externalRefresh}:${manual}`);
  return { ...snapshot, key: `${snapshot.revision}:${externalRefresh}:${manual}`, refresh: () => setManual(value => value + 1) };
}

export function useRemoteResource<T>(path: string | null, revision: string | number = 0, interval = 0, cached = false) {
  const version = String(revision);
  const key = JSON.stringify([path, version]);
  const initial = path && cached ? projections.get<T>(path, version) : undefined;
  const [state, setState] = useState<{ key: string; data: T | null; error: string; loading: boolean }>(
    () => ({ key, data: initial ?? null, error: '', loading: Boolean(path && initial === undefined) }));
  useEffect(() => {
    const saved = path && cached ? projections.get<T>(path, version) : undefined;
    setState({ key, data: saved ?? null, error: '', loading: Boolean(path && saved === undefined) });
    if (!path || saved !== undefined) return;
    const session = createResourceSession<T>({
      request: signal => api<T>(path, { signal }),
      success: value => {
        if (cached) projections.set(path, version, value);
        setState({ key, data: value, error: '', loading: false });
      },
      failure: error => setState(previous => ({ ...previous, key, error: error.message, loading: false })),
      interval,
      timeoutMs: RESOURCE_READ_TIMEOUT_MS,
      visible: () => document.visibilityState !== 'hidden',
    });
    const visibility = () => session.visibilityChanged();
    document.addEventListener('visibilitychange', visibility);
    void session.start();
    return () => { session.stop(); document.removeEventListener('visibilitychange', visibility); };
  }, [path, version, interval, cached, key]);
  // Effects run after paint. Never show the previous run/tab during that gap.
  return state.key === key ? state : { key, data: initial ?? null, error: '', loading: Boolean(path && initial === undefined) };
}
