export const REFERENCE_LIMIT = 10;
export const REFERENCE_MAX_BYTES = 12 * 1024 * 1024;

export interface ReferenceUploadProgress { completed: number; total: number; fileName: string }
export interface ReferenceUploadSummary { added: number; reused: number; skipped: number; errors: string[] }

export function mergeReference<T extends { id?: unknown }>(references: readonly T[], incoming: T): T[] {
  const existing = references.findIndex(reference => reference.id === incoming.id);
  if (existing < 0) return [...references, incoming];
  return references.map((reference, index) => index === existing ? { ...reference, ...incoming } : reference);
}

export function referenceRagLabel(reference: { rag_status?: unknown }): string {
  if (reference.rag_status === 'already_present') return '已在 RAG · 复用分析与笔记';
  if (reference.rag_status === 'pending_analysis') return '待精读与入库';
  return '';
}

// Upload separately so one rejected document cannot discard previously accepted papers.
export async function uploadReferenceBatch<F extends { name: string; size: number }, T extends { id?: unknown }>({
  files, references, upload, accepted, progress,
}: {
  files: readonly F[]; references: readonly T[]; upload: (file: F) => Promise<T>;
  accepted: (reference: T) => void; progress: (value: ReferenceUploadProgress) => void;
}): Promise<ReferenceUploadSummary> {
  const ids = new Set(references.map(reference => reference.id));
  const summary: ReferenceUploadSummary = { added: 0, reused: 0, skipped: 0, errors: [] };
  for (let index = 0; index < files.length; index++) {
    const file = files[index];
    progress({ completed: index, total: files.length, fileName: file.name });
    if (file.size > REFERENCE_MAX_BYTES) {
      summary.errors.push(`${file.name}：单份参考资料不能超过 12 MB。`);
    } else if (ids.size >= REFERENCE_LIMIT) {
      summary.skipped += files.length - index;
      break;
    } else {
      try {
        const reference = await upload(file);
        if (typeof reference.id !== 'string' || !reference.id) throw new Error('上传响应缺少资料标识，请重试。');
        if (ids.has(reference.id)) summary.reused++;
        else { ids.add(reference.id); summary.added++; }
        accepted(reference);
      } catch (error) {
        summary.errors.push(`${file.name}：${error instanceof Error ? error.message : '参考资料上传失败'}`);
      }
    }
  }
  progress({ completed: files.length, total: files.length, fileName: '' });
  return summary;
}
