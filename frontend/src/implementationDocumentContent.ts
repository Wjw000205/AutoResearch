import type { Json } from './api';

const sectionTitles: Record<string, string> = {
  summary: '实现概述', algorithm: '算法与实现流程', branches: '基线、候选与消融分支',
  data_contract: '数据与划分约束', parameter_contract: '参数契约', metrics: '评价指标',
  dependencies: '运行依赖', acceptance_checks: '验收要求', implementation_map: '功能实现映射',
  limitations: '限制与未决事项',
};

const fieldLabels: Record<string, string> = {
  ...sectionTitles,
  id: '编号', title: '标题', name: '名称', description: '说明', variant: '算法分支',
  steps: '实现步骤', expected_behavior: '预期行为', dataset: '数据集', source: '数据来源',
  split_identity: '固定划分标识', leakage_prevention: '数据泄漏防护',
  requirement: '验收要求', verification: '验证方法', requirement_id: '对应验收项',
  location: '计划实现位置', locations: '交付实现位置', behavior: '实现行为',
  explanation: '实现说明', requirements: '交付要求', params: '参数', parameters: '参数',
  seed: '随机种子', seeds: '随机种子列表', phase: '阶段', config: '配置',
  document_id: '文档标识', expected_sha256: '期望文档 SHA-256', sha256: 'SHA-256',
  spec_hash: '规范版本标识', document_sha256: '文档 SHA-256', proposal_hash: '共识方案版本标识',
  code_sha256: '代码 SHA-256', delivery_hash: '交付版本标识', section_id: '章节标识',
  char_start: '起始字符位置', char_end: '结束字符位置',
  rationale: '评议理由', issues: '问题', fixes: '修改建议', issue_index: '对应问题序号',
  change: '修改办法', required_changes: '必需修改', suggestions: '补充建议', approve: '评议判断',
  type: '数据类型', properties: '字段定义', required: '必需字段', additionalProperties: '允许额外字段',
  items: '条目定义', enum: '允许取值', const: '固定值', minItems: '最少条目数', maxItems: '最多条目数',
  minLength: '最短长度', maxLength: '最长长度', minimum: '最小值', maximum: '最大值',
  pattern: '格式约束', format: '格式', anyOf: '允许的结构', allOf: '共同约束', oneOf: '互斥结构',
};

/** Display labels only; no saved heading, field, identifier or source is rewritten. */
export function implementationSectionTitle(title: string): string {
  return Object.hasOwn(sectionTitles, title) ? sectionTitles[title] : title;
}

export function implementationFieldLabel(key: string): string {
  return Object.hasOwn(fieldLabels, key) ? fieldLabels[key] : key;
}

export type ImplementationContent = { type: 'markdown'; text: string }
  | { type: 'structured'; value: unknown; source: string };

// A decimal spelling's exact coefficient and exponent, independent of notation.
// Comparing with Number.toString() catches rounding/underflow that JSON.parse
// would otherwise hide. The original spelling remains in the source block.
function decimalIdentity(literal: string): string | null {
  const match = /^(-?)(\d+)(?:\.(\d+))?(?:[eE]([+-]?\d+))?$/.exec(literal);
  if (!match) return null;
  const fraction = match[3] ?? '';
  const digits = (match[2] + fraction).replace(/^0+/, '');
  if (!digits) return '0';
  const coefficient = digits.replace(/0+$/, '');
  const exponent = Number(match[4] ?? 0) - fraction.length + digits.length - coefficient.length;
  return Number.isSafeInteger(exponent) ? `${match[1]}${coefficient}@${exponent}` : null;
}

/** JSON.parse supplies the value; a lexical walk rejects overwritten keys. */
function parseUnambiguousJSON(source: string): unknown {
  const value: unknown = JSON.parse(source);
  let cursor = 0;
  const number = /-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/y;
  const whitespace = () => { while (/[ \t\r\n]/.test(source[cursor] ?? '') && cursor < source.length) cursor++; };
  const string = () => {
    const start = cursor++;
    if (source[start] !== '"') throw new Error('Expected JSON string');
    while (cursor < source.length) {
      const char = source[cursor++];
      if (char === '\\') cursor++;
      else if (char === '"') return source.slice(start, cursor);
    }
    throw new Error('Unclosed JSON string');
  };
  const walk = (depth: number): void => {
    if (depth > 128) throw new Error('Structure too deep for reading view');
    whitespace();
    if (source[cursor] === '{') {
      cursor++; whitespace();
      const keys = new Set<string>();
      if (source[cursor] === '}') { cursor++; return; }
      while (cursor < source.length) {
        whitespace();
        const key = JSON.parse(string()) as string;
        if (keys.has(key)) throw new Error('Duplicate JSON key');
        keys.add(key); whitespace();
        if (source[cursor++] !== ':') throw new Error('Expected JSON colon');
        walk(depth + 1); whitespace();
        if (source[cursor] === '}') { cursor++; return; }
        if (source[cursor++] !== ',') throw new Error('Expected JSON comma');
      }
      throw new Error('Unclosed JSON object');
    }
    if (source[cursor] === '[') {
      cursor++; whitespace();
      if (source[cursor] === ']') { cursor++; return; }
      while (cursor < source.length) {
        walk(depth + 1); whitespace();
        if (source[cursor] === ']') { cursor++; return; }
        if (source[cursor++] !== ',') throw new Error('Expected JSON comma');
      }
      throw new Error('Unclosed JSON array');
    }
    if (source[cursor] === '"') { string(); return; }
    for (const literal of ['true', 'false', 'null']) {
      if (source.startsWith(literal, cursor)) { cursor += literal.length; return; }
    }
    number.lastIndex = cursor;
    const match = number.exec(source);
    if (!match) throw new Error('Expected JSON value');
    cursor = number.lastIndex;
    const decoded = Number(match[0]);
    if (!Number.isFinite(decoded) || (Number.isInteger(decoded) && !Number.isSafeInteger(decoded))
        || decimalIdentity(match[0]) !== decimalIdentity(decoded.toString())) {
      throw new Error('Numeric source would lose precision');
    }
  };
  walk(0); whitespace();
  if (cursor !== source.length) throw new Error('Trailing JSON source');
  return value;
}

/** Only complete top-level Markdown JSON fences become structured reading blocks. */
export function splitImplementationContent(text: string): ImplementationContent[] {
  if (!text) return [];
  const result: ImplementationContent[] = [];
  let markdownStart = 0;
  let cursor = 0;
  let fence: { char: string; length: number; start: number; body: number; json: boolean } | null = null;
  while (cursor < text.length) {
    const newline = text.indexOf('\n', cursor);
    const end = newline < 0 ? text.length : newline + 1;
    const line = text.slice(cursor, newline < 0 ? end : newline).replace(/\r$/, '');
    if (!fence) {
      const open = /^ {0,3}(`{3,}|~{3,})([^\r\n]*)$/.exec(line);
      if (open && !(open[1][0] === '`' && open[2].includes('`'))) {
        fence = { char: open[1][0], length: open[1].length, start: cursor, body: end,
          json: open[2].trim().toLowerCase() === 'json' };
      }
    } else {
      const close = /^ {0,3}(`{3,}|~{3,})[ \t]*$/.exec(line);
      if (close && close[1][0] === fence.char && close[1].length >= fence.length) {
        if (fence.json) {
          try {
            const value = parseUnambiguousJSON(text.slice(fence.body, cursor));
            if (fence.start > markdownStart) result.push({ type: 'markdown', text: text.slice(markdownStart, fence.start) });
            result.push({ type: 'structured', value, source: text.slice(fence.start, end) });
            markdownStart = end;
          } catch { /* Keep the entire original fence in Markdown; never repair it here. */ }
        }
        fence = null;
      }
    }
    cursor = end;
  }
  if (markdownStart < text.length) result.push({ type: 'markdown', text: text.slice(markdownStart) });
  return result;
}

/** Python indexes Unicode code points, whereas JS slice indexes UTF-16 units. */
export function validateImplementationText(text: string, doc: Json): Array<Json & { text: string }> {
  if (typeof text !== 'string' || !doc || typeof doc !== 'object' || !Array.isArray(doc.sections)) {
    throw new Error('实现文档目录缺失或格式不正确');
  }
  const characters = Array.from(text);
  if (!Number.isSafeInteger(doc.total_chars) || doc.total_chars !== characters.length) {
    throw new Error('实现文档总字符数与目录不一致');
  }
  const seen = new Set<string>();
  let end = 0;
  const result: Array<Json & { text: string }> = [];
  for (const section of doc.sections) {
    if (!section || typeof section !== 'object' || Array.isArray(section)
        || typeof section.id !== 'string' || !section.id.trim() || seen.has(section.id)
        || typeof section.title !== 'string' || !section.title.trim()
        || !Number.isSafeInteger(section.start) || !Number.isSafeInteger(section.end)
        || !Number.isSafeInteger(section.chars) || section.start !== end
        || section.end < section.start || section.end > characters.length
        || (section.end === section.start && characters.length !== 0)
        || section.chars !== section.end - section.start) {
      throw new Error('实现文档章节范围、标题或标识校验失败');
    }
    seen.add(section.id); end = section.end;
    result.push({ ...section, text: characters.slice(section.start, section.end).join('') });
  }
  if (end !== characters.length || (!result.length && characters.length > 0)) {
    throw new Error('实现文档目录没有连续覆盖全部正文');
  }
  return result;
}

const record = (value: unknown): Json | null => value && typeof value === 'object' && !Array.isArray(value) ? value as Json : null;
const identity = (doc: Json) => typeof doc.document_id === 'string' && !!doc.document_id.trim()
  && typeof doc.sha256 === 'string' && /^[a-f0-9]{64}$/.test(doc.sha256);
const revisionNumber = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0;

/** A recorded directory or current unsaved revision is not a saved document. */
export function selectImplementationReadingDocument(detail?: Json | null): { document: Json; revision: number; current: boolean } | null {
  const document = record(detail?.document);
  if (!document) return null;
  if (['draft', 'submitted'].includes(document.status) && identity(document) && revisionNumber(document.revision)) {
    return { document, revision: document.revision, current: true };
  }
  const previous = record(document.previous_document);
  return previous && identity(previous) && revisionNumber(previous.prior_revision)
    ? { document: previous, revision: previous.prior_revision, current: false } : null;
}
