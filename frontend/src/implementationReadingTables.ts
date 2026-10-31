import { unified } from 'unified';
import remarkParse from 'remark-parse';
import remarkMarkdownMath from './MarkdownMathSyntax';
import type { Nodes, Root } from 'mdast';

export type ReadingMarkdownBlock = { type: 'markdown'; text: string }
  | { type: 'table'; headers: string[]; rows: string[][]; source: string };

// Only explicit, whitespace-padded column boundaries are interpreted.
// |γ|, escaped pipes, inline-code pipes and formula pipes remain cell data.
function cells(line: string): string[] | null {
  if (!/^ {0,3}\|[ \t]/.test(line) || !/[ \t]\|[ \t]*$/.test(line)) return null;
  const body = line.trim().slice(1, -1);
  const values: string[] = [];
  let start = 0, code = '', math = '', escaped = false;
  for (let index = 0; index < body.length; index++) {
    const char = body[index];
    if (escaped) { escaped = false; continue; }
    if (char === '\\' && !code && !math && (body[index + 1] === '(' || body[index + 1] === '[')) {
      math = body[index + 1] === '(' ? '\\)' : '\\]'; index++; continue;
    }
    if (char === '\\' && !code && (math === '\\)' || math === '\\]') && body.slice(index, index + 2) === math) {
      math = ''; index++; continue;
    }
    if (char === '\\') { escaped = true; continue; }
    if (char === '`' && !math) {
      const marker = /^`+/.exec(body.slice(index))![0];
      if (!code) code = marker; else if (code === marker) code = '';
      index += marker.length - 1; continue;
    }
    if (char === '$' && !code) {
      const marker = body[index + 1] === '$' ? '$$' : '$';
      if (!math) math = marker; else if (math === marker) math = '';
      index += marker.length - 1; continue;
    }
    if (char === '|' && !code && !math && /[ \t]/.test(body[index - 1] ?? '') && /[ \t]/.test(body[index + 1] ?? '')) {
      values.push(body.slice(start, index).trim()); start = index + 1;
    }
  }
  if (code || math) return null;
  values.push(body.slice(start).trim());
  return values.length >= 2 && values.length <= 12 ? values : null;
}

const headings = new Set(['参数名', '参数', '类型', '冻结值或约束', '单位', '适用分支与阶段', '来源',
  '字段', '字段名', '含义', '说明', '约束', '要求', '验收要求', '验证方法', '编号']);
const knownHeader = (values: string[]) => new Set(values).size === values.length && values.filter(value => headings.has(value)).length >= 2;
const delimiter = (values: string[]) => values.every(value => /^:?-{3,}:?$/.test(value));

/** Reading projection only; saved bytes, character ranges and raw text stay intact.
 * Missing-delimiter tables require known headings and equally sized physical
 * rows. Collapsed or ambiguous rows are intentionally left as literal Markdown.
 */
export function implementationReadingBlocks(text: string): ReadingMarkdownBlock[] {
  if (!/^ {0,3}\|/m.test(text)) return text ? [{ type: 'markdown', text }] : [];
  // Reuse the same Markdown/math syntax as the renderer to protect complete
  // code, HTML and formula ranges, including content spanning physical lines.
  const parser = unified().use(remarkParse).use(remarkMarkdownMath);
  const tree = parser.parse(text) as Root;
  const protectedRanges: [number, number][] = [];
  const protect = (node: Nodes) => {
    if (node.type === 'code' || node.type === 'html' || node.type === 'markdownMath' && node.display
      || node.type === 'inlineCode' && node.position?.start.line !== node.position?.end.line) {
      const start = node.position?.start.offset, end = node.position?.end.offset;
      if (start !== undefined && end !== undefined) protectedRanges.push([start, end]);
    }
    if ('children' in node) for (const child of node.children) protect(child);
  };
  protect(tree);
  const lines = text.match(/[^\r\n]*(?:\r\n|\r|\n|$)/g)?.filter(Boolean) ?? [];
  let offset = 0;
  const starts = lines.map(line => { const start = offset; offset += line.length; return start; });
  for (let index = 0; index < lines.length; index++) {
    const opening = /^ {0,3}(?:\$\$|\\\[)/.exec(lines[index]);
    const start = starts[index];
    // An incomplete display opener has no math AST node. Keep its remainder
    // literal rather than extracting rows from a possibly unfinished formula.
    if (opening && !protectedRanges.some(([from, to]) => from <= start && to > start)) {
      protectedRanges.push([start, text.length]); break;
    }
  }
  const result: ReadingMarkdownBlock[] = [];
  let plain = '', fence = '';
  const flush = () => { if (plain) { result.push({ type: 'markdown', text: plain }); plain = ''; } };
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index], content = line.replace(/(?:\r\n|\r|\n)$/, '');
    const marker = /^ {0,3}(`{3,}|~{3,})/.exec(content)?.[1];
    if (marker) {
      if (!fence) fence = marker;
      else if (marker[0] === fence[0] && marker.length >= fence.length && /^ {0,3}(?:`+|~+)\s*$/.test(content)) fence = '';
      plain += line; continue;
    }
    const headers = !fence ? cells(content) : null;
    if (!headers || !knownHeader(headers)) { plain += line; continue; }
    const rows: string[][] = [];
    let until = index + 1, valid = true;
    const first = cells((lines[until] ?? '').replace(/(?:\r\n|\r|\n)$/, ''));
    // Existing valid GFM stays with the existing Markdown renderer.
    if (first && delimiter(first)) {
      while (until < lines.length && /^ {0,3}\|/.test(lines[until])) until++;
      plain += lines.slice(index, until).join(''); index = until - 1; continue;
    }
    for (; until < lines.length && /^ {0,3}\|/.test(lines[until]); until++) {
      const row = cells(lines[until].replace(/(?:\r\n|\r|\n)$/, ''));
      if (!row || row.length !== headers.length) valid = false;
      else rows.push(row);
    }
    const start = starts[index], end = starts[until] ?? text.length;
    if (protectedRanges.some(([from, to]) => from < end && to > start)) valid = false;
    if (!valid || !rows.length) {
      plain += lines.slice(index, until).join(''); index = until - 1; continue;
    }
    flush();
    result.push({ type: 'table', headers, rows, source: lines.slice(index, until).join('') });
    index = until - 1;
  }
  flush();
  return result;
}

