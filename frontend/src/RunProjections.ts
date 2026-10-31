import type { Detail } from './api';

export type RunProjection = Partial<Detail> & { section?: string; stage?: string };
export const projectionRevision = (summary?: Detail | null) => summary?.revision ?? summary?.run?.updated_at ?? '';

export function workspaceRequestPaths(page: string, runId: string | null) {
  return {
    overview: !runId && page === 'overview' ? '/overview' : null,
    runs: !runId && ['runs', 'memory', 'billing'].includes(page) ? '/runs' : null,
    hardware: !runId && page === 'hardware' ? '/hardware' : null,
  };
}

export function runViewPath(id: string, tab: string, loaded: string[] = ['command']) {
  if (tab === 'command' || tab === 'report' || tab === 'tools' || tab === 'memory' || loaded.includes(tab)) return null;
  return tab === 'design' ? runStagePath(id, tab) : `/runs/${encodeURIComponent(id)}/views/${encodeURIComponent(tab)}`;
}
export const runStagePath = (id: string, stage: string) => `/runs/${encodeURIComponent(id)}/stages/${encodeURIComponent(stage)}`;

export function mergeRunProjection(summary: Detail, projection?: RunProjection | null): Detail {
  // The summary owns current action gates and the run header. A detail fetch
  // may finish after a newer summary and must not restore an older run status.
  if (!projection || projection.run && projection.run.id !== summary.run.id) return summary;
  return { ...summary, ...projection, run: summary.run, implementation_progress: summary.implementation_progress,
    revision: summary.revision, projection: summary.projection };
}
