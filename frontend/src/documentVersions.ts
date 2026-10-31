import { type Json } from './api';

export type DocumentVersion = {
  key: string;
  document: Json;
  revision: number;
  label: string;
  status?: string;
  current?: boolean;
};

type ReadingDocument = { document: Json; revision: number; current: boolean };
const record = (value: unknown): Json | null => value && typeof value === 'object' && !Array.isArray(value) ? value as Json : null;
const rows = (value: unknown): Json[] => Array.isArray(value) ? value.map(record).filter((row): row is Json => !!row) : [];
const revisionNumber = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0;
const kindMatches = (doc: Json, kind: string) => [doc.kind, doc.document_kind].every(value => value === undefined || value === kind);

/** Exact saved identity; an incomplete pointer is not a selectable version. */
export function documentVersionKey(value: unknown): string {
  const doc = record(value);
  return doc && typeof doc.document_id === 'string' && !!doc.document_id.trim()
    && typeof doc.sha256 === 'string' && /^[a-f0-9]{64}$/.test(doc.sha256)
    ? `${doc.document_id}:${doc.sha256}` : '';
}

function orderedVersions(candidates: DocumentVersion[]): DocumentVersion[] {
  const unique = new Map<string, DocumentVersion>();
  for (const version of candidates) {
    const previous = unique.get(version.key);
    // Repeated reviews of unchanged bytes are one document. Keep their latest
    // recorded revision, while a matching reading selection supplies its status.
    if (!previous || version.revision >= previous.revision) unique.set(version.key, { ...version,
      current: Boolean(previous?.current || version.current),
      status: previous?.status === 'approved' && version.revision === previous.revision ? previous.status : version.status });
  }
  const result = [...unique.values()].sort((a, b) => a.revision - b.revision);
  const counts = new Map<number, number>();
  const numbers = new Map<number, number>();
  for (const version of result) counts.set(version.revision, (counts.get(version.revision) ?? 0) + 1);
  return result.map(version => {
    const number = (numbers.get(version.revision) ?? 0) + 1;
    numbers.set(version.revision, number);
    return { ...version, label: `第 ${version.revision + 1} 版${(counts.get(version.revision) ?? 0) > 1 ? ` · 保存版本 ${number}` : ''}` };
  });
}

/** The reading selection must come from the same workflow's matched detail. */
export function implementationDocumentVersions(localCodex: Json | null | undefined, workflowId: string,
  readingDocument?: ReadingDocument | null): DocumentVersion[] {
  if (typeof workflowId !== 'string' || !workflowId.trim()) return [];
  const matches = rows(localCodex?.workflows).filter(workflow => workflow.id === workflowId);
  // A duplicate workflow identity is ambiguous; never pick one arbitrarily.
  const workflow = matches.length === 1 ? matches[0] : null;
  const candidates: DocumentVersion[] = [];
  if (workflow) for (const spec of rows(workflow.specifications)) {
    const doc = record(spec.document), key = documentVersionKey(doc);
    if (!doc || !key || !revisionNumber(spec.index) || !kindMatches(doc, 'implementation_specification')) continue;
    if ((doc.workflow_id !== undefined && doc.workflow_id !== workflowId)
      || (doc.run_id !== undefined && doc.run_id !== workflow.run_id)) continue;
    candidates.push({ key, document: doc, revision: spec.index, label: '',
      status: spec.approved === true ? 'approved' : 'submitted', current: false });
  }
  const reading = record(readingDocument), doc = record(reading?.document), key = documentVersionKey(doc);
  if (reading && doc && key && revisionNumber(reading.revision) && typeof reading.current === 'boolean'
    && kindMatches(doc, 'implementation_specification')
    && (doc.workflow_id === undefined || doc.workflow_id === workflowId)
    && (!workflow || doc.run_id === undefined || doc.run_id === workflow.run_id)
    && (doc.status === undefined || ['draft', 'submitted'].includes(doc.status))) {
    candidates.push({ key, document: doc, revision: reading.revision, label: '',
      status: doc.status ?? (reading.current ? 'submitted' : 'historical_reference'), current: reading.current });
  }
  const result = orderedVersions(candidates);
  const selectedKey = result.find(version => version.current)?.key
    ?? result.filter(version => version.revision === workflow?.current_revision).at(-1)?.key;
  return result.map(version => ({ ...version, current: version.key === selectedKey,
    label: `审核第 ${version.revision + 1} 轮 · ${typeof version.document.title === 'string' && version.document.title
      ? version.document.title.split(/[\\/]/).at(-1) : '文档 ' + version.document.document_id}` }));
}

/** Reuse a supplied catalogue only when it contains the exact reading version. */
export function implementationVersionDirectorySelection(localCodex: Json | null | undefined, workflowId: string,
  readingDocument: ReadingDocument, refresh = 0): { supplied: boolean; revision: string } {
  const key = documentVersionKey(readingDocument.document);
  return {
    supplied: Boolean(key && implementationDocumentVersions(localCodex, workflowId)
      .some(version => version.key === key && version.revision === readingDocument.revision)),
    // Polling can replace the detail object or update review status without
    // saving new text. Those changes must not fetch the entire catalogue again.
    revision: JSON.stringify([workflowId, readingDocument.revision, key, refresh]),
  };
}

/** Architecture review history is a lineage; definition and issue documents are separate. */
export function architectureDocumentVersions(value?: Json | null): DocumentVersion[] {
  const current = record(value?.current), currentDoc = record(current?.document);
  if (!current || !currentDoc || !documentVersionKey(currentDoc) || !kindMatches(currentDoc, 'architecture_design')) return [];
  const sameLineage = (row: Json) => ['run_id', 'cycle', 'proposal_hash'].every(field => row[field] === current[field]);
  const source = [...rows(value?.history), current].filter(row => sameLineage(row));
  const candidates: DocumentVersion[] = [];
  const positions = new Map<string, number>();
  for (const row of source) {
    const doc = record(row.document), key = documentVersionKey(doc);
    if (!doc || !key || !kindMatches(doc, 'architecture_design')) continue;
    if (doc.run_id !== undefined && doc.run_id !== current.run_id) continue;
    if (!positions.has(key)) positions.set(key, positions.size);
    candidates.push({ key, document: doc, revision: positions.get(key)!, label: '',
      status: typeof row.status === 'string' ? row.status : undefined,
      current: key === documentVersionKey(currentDoc) });
  }
  // `row.version` is the indexing protocol version, not a document revision.
  return orderedVersions(candidates);
}
