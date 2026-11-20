import type { Json } from './api';

export function sidebarDetailMatches(progress: Json, response: Json | null | undefined, runId: string): boolean {
  if (!response || response.run_id !== runId || progress.run_id !== runId || response.phase !== progress.phase) return false;
  const expected = progress.workflow?.id;
  if (!expected || response.workflow?.id !== expected || response.details?.identity?.workflow_id !== expected) return false;
  if (progress.workflow?.document_sha256 && response.details?.identity?.document_sha256 !== progress.workflow.document_sha256) return false;
  return !['document_authoring', 'document_review'].includes(progress.phase)
    || response.details?.identity?.document_revision === progress.workflow?.revision;
}
