import type { Json } from './api';
import { selectImplementationReadingDocument } from './implementationDocumentContent';
import { documentVersionKey, implementationDocumentVersions } from './documentVersions';

type ReadingDocument = { document: Json; revision: number; current: boolean };
export type CurrentImplementationDocument = ReadingDocument & { workflowId: string };
const record = (value: unknown): Json | null => value && typeof value === 'object' && !Array.isArray(value) ? value as Json : null;
const revisionNumber = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0;
const identifier = (value: unknown): value is string => typeof value === 'string' && !!value.trim();
const documentScopeMatches = (doc: Json, runId: string, workflowId: string) =>
  !!documentVersionKey(doc)
  && [doc.kind, doc.document_kind].every(kind => kind === undefined || kind === 'implementation_specification')
  && (doc.status === undefined || ['draft', 'submitted', 'approved'].includes(doc.status))
  && (doc.run_id === undefined || doc.run_id === runId)
  && (doc.workflow_id === undefined || doc.workflow_id === workflowId)
  && (doc.scope === undefined || ['implementation_document', 'implementation_document_only', 'historical_reference'].includes(doc.scope));

/** Reading an immutable saved version does not require its audit phase to stay unchanged. */
export function selectSidebarReadingDocument(progress: Json | null | undefined, response: Json | null | undefined,
  runId: string): ReadingDocument | null {
  const workflowId = progress?.workflow?.id;
  if (!identifier(runId) || progress?.run_id !== runId || response?.run_id !== runId
    || !identifier(workflowId) || response.workflow?.id !== workflowId
    || (progress.workflow.run_id !== undefined && progress.workflow.run_id !== runId)
    || (response.workflow.run_id !== undefined && response.workflow.run_id !== runId)) return null;
  const detail = record(response.details), identity = record(detail?.identity), doc = record(detail?.document);
  if (!identity || identity.workflow_id !== workflowId || (identity.run_id !== undefined && identity.run_id !== runId)
    || !doc || !revisionNumber(doc.revision)
    || identity.document_revision !== doc.revision
    || (identity.document_sha256 ?? null) !== (doc.sha256 ?? null)) return null;
  const selected = selectImplementationReadingDocument(detail);
  if (!selected || !documentScopeMatches(selected.document, runId, workflowId)) return null;
  if (!selected.current && selected.revision >= doc.revision) return null;
  const workflow = progress.workflow;
  // The document counter is distinct from the code-attempt counter after approval.
  const expectedRevision = ['document_authoring', 'document_review'].includes(progress.phase)
    ? workflow.revision ?? workflow.document_revision : workflow.document_revision;
  const current = selected.current && selected.document.scope !== 'historical_reference'
    && selected.revision === expectedRevision
    && (!workflow.document_id || workflow.document_id === selected.document.document_id)
    && (!workflow.document_sha256 || workflow.document_sha256 === selected.document.sha256);
  return { ...selected, current };
}

/** Choose a saved file from this run's current workflow, never from a different history entry. */
export function selectCurrentImplementationDocument(localCodex: Json | null | undefined,
  runId: string): CurrentImplementationDocument | null {
  const current = record(localCodex?.current);
  if (!identifier(runId) || !current || current.run_id !== runId || !identifier(current.id)) return null;
  const workflowId = current.id;
  const directory = Array.isArray(localCodex?.workflows) ? localCodex.workflows.filter((item: unknown) => record(item)?.id === workflowId) : [];
  if (directory.length > 1) return null;
  const versions = implementationDocumentVersions({ workflows: [current] }, workflowId)
    .filter(version => documentScopeMatches(version.document, runId, workflowId));
  if (!versions.length) return null;
  const documentPhase = ['specification', 'spec_review'].includes(current.status)
    || current.status === 'blocked' && (!Array.isArray(current.attempts) || current.attempts.length === 0);
  let candidates = versions;
  let isCurrent = false;
  if (documentPhase) {
    if (!revisionNumber(current.current_revision)) return null;
    const atRevision = versions.filter(version => version.revision === current.current_revision);
    candidates = atRevision.length ? atRevision : versions.filter(version => version.revision < current.current_revision);
    isCurrent = atRevision.length > 0;
  } else {
    // Once writing code begins, current_revision counts code attempts. Use the frozen spec binding.
    const specifications = Array.isArray(current.specifications) ? current.specifications : [];
    if (identifier(current.spec_hash)) {
      const bound = specifications.filter((item: unknown) => record(item)?.spec_hash === current.spec_hash);
      const keys = new Set(bound.map((item: Json) => documentVersionKey(item.document)).filter(Boolean));
      if (keys.size !== 1) return null;
      candidates = versions.filter(version => keys.has(version.key));
      isCurrent = true;
    } else if (documentVersionKey(current.document)) {
      candidates = versions.filter(version => version.key === documentVersionKey(current.document));
      isCurrent = true;
    }
  }
  if (!candidates.length) return null;
  const latestRevision = Math.max(...candidates.map(version => version.revision));
  const latest = candidates.filter(version => version.revision === latestRevision);
  if (latest.length !== 1) return null;
  const selected = latest[0];
  return { document: selected.document, revision: selected.revision,
    current: isCurrent && selected.document.scope !== 'historical_reference', workflowId };
}
