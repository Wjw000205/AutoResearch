export type NameIndex = ReadonlyMap<string, readonly string[]>;
type RecordValue = Record<string, unknown>;

const fingerprintField = /(?:^|_)(?:sha256|hash|fingerprint)$|^sha$/i;
const opaqueToken = /\b[\w./:@-]*[a-f\d]{32,64}[\w./:@-]*\b/gi;
const bareFingerprint = /^(?=[a-f\d]*[a-f])[a-f\d]{12,64}$|^[a-f\d]{32,64}$/i;
const fallbackName = '未关联名称的记录';
const asRecord = (value: unknown): RecordValue | null => value && typeof value === 'object' && !Array.isArray(value) ? value as RecordValue : null;
const basename = (value: string) => value.replace(/\\/g, '/').split('/').filter(Boolean).at(-1) ?? value;
const written = (value: unknown) => typeof value === 'string' && value.trim() ? value.trim() : '';

/** Display aliases only. These never participate in source identity or version checks. */
export function referenceNames(records: unknown, parent?: NameIndex): NameIndex {
  const names = new Map<string, Set<string>>();
  const priorities = new Map<string, number>();
  parent?.forEach((labels, key) => names.set(key, new Set(labels)));
  const bind = (key: unknown, name: string, priority = 20) => {
    if (typeof key !== 'string' || !key || !name) return;
    if (/^s\d+$/.test(key)) return;
    if (/\.(?:md|pdf|py|txt|csv|json|jsonl|yaml|yml|toml|png|jpg|xlsx|docx)$/i.test(name)) name = basename(name);
    name = name.replace(opaqueToken, '未命名文档');
    const previous = priorities.get(key) ?? 0;
    if (priority < previous) return;
    const labels = priority > previous ? new Set<string>() : names.get(key) ?? new Set<string>();
    labels.add(name); names.set(key, labels);
    priorities.set(key, priority);
  };
  const seen = new WeakSet<object>();
  const related: RecordValue[] = [];
  const identities = ['id', 'paper_id', 'source_id', 'document_id', 'artifact_id', 'memory_document_id', 'rag_entry_id', 'canonical_paper_id', 'version_id'];
  const visit = (value: unknown, sourceName = '') => {
    if (!value || typeof value !== 'object' || seen.has(value)) return;
    seen.add(value);
    if (Array.isArray(value)) { value.forEach(item => visit(item, sourceName)); return; }
    const record = value as RecordValue;
    const paper = asRecord(record.paper);
    const proposal = asRecord(record.proposal);
    const document = asRecord(record.document);
    const storedPath = written(record.path) || written(record.relative_path);
    const file = written(record.filename) || (storedPath && /\.[a-z\d]{1,12}$/i.test(basename(storedPath)) ? storedPath : '');
    const isSection = record.section_id != null || /^s\d+$/.test(written(record.id));
    const name = isSection ? '' : written(record.title) || written(record.name) || (file ? basename(file) : '') || written(paper?.title) || sourceName;
    const priority = file || (record.document_id && record.sha256 && Array.isArray(record.sections)) ? 50 : paper || sourceName || record.url || record.doi || record.memory_document_id ? 40 : 20;
    if (name) {
      for (const [key, identity] of Object.entries(record)) {
        if (identities.includes(key) || fingerprintField.test(key)) bind(identity, name, priority);
      }
      if (file) { bind(file, name, priority); bind(basename(file), name, priority); }
      if (written(record.title)) bind(written(record.title), name, priority);
    }
    bind(record.proposal_hash, written(proposal?.title));
    bind(record.design_hash, written(document?.title) || (record.design_hash ? `架构设计${record.version ? ` · 第 ${record.version} 版` : ''}` : ''));
    bind(record.spec_hash, written(document?.title) || (record.spec_hash ? `代码实现文档${typeof record.index === 'number' ? ` · 第 ${record.index + 1} 版` : ''}` : ''));
    if (document?.title) {
      bind(record.document_sha256, written(document.title));
      bind(record.content_sha256, written(document.title));
    }
    related.push(record);
    Object.entries(record).forEach(([key, item]) => visit(item,
      ['source', 'source_versions', 'citations', 'other_analysis_versions'].includes(key) ? name : ''));
  };
  visit(records);
  // Join saved read/note receipts to their exact named source; titles never establish identity.
  for (const record of related) {
    const source = ['document_id', 'paper_id', 'artifact_id', 'source_id'].map(key => names.get(written(record[key])))
      .find(labels => labels?.size === 1);
    if (!source) continue;
    const name = [...source][0];
    for (const [key, identity] of Object.entries(record))
      if (identities.includes(key) || fingerprintField.test(key)) bind(identity, name, 10);
  }
  return new Map([...names].map(([key, labels]) => [key, [...labels]]));
}

function label(identity: string, names: NameIndex): string | undefined {
  const labels = names.get(identity);
  return labels?.length ? labels.join('、') : undefined;
}

function replaceReferences(text: string, names: NameIndex): string {
  const exact = label(text, names);
  if (exact) return exact;
  if (bareFingerprint.test(text)) return fallbackName;
  return text.replace(opaqueToken, identity => label(identity, names) ??
    label(identity.match(/[a-f\d]{32,64}/i)?.[0] ?? '', names) ?? fallbackName);
}

function displayRecord(value: unknown, names: NameIndex): unknown {
  if (typeof value === 'string') return replaceReferences(value, names);
  if (Array.isArray(value)) return value.map(item => displayRecord(item, names));
  const record = asRecord(value);
  if (!record) return value;
  return Object.fromEntries(Object.entries(record).filter(([key]) => !fingerprintField.test(key))
    .map(([key, item]) => [replaceReferences(key, names), displayRecord(item, names)]));
}

export function referenceText(value: unknown, names: NameIndex = new Map()): string {
  if (value == null || typeof value === 'boolean') return '';
  const displayed = displayRecord(value, names);
  return typeof displayed === 'string' ? displayed : JSON.stringify(displayed, null, 2);
}
