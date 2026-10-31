import { unified } from 'unified';
import remarkParse from 'remark-parse';
import remarkGfm from 'remark-gfm';
import type { Nodes, Paragraph, Root } from 'mdast';
import remarkMarkdownMath from './MarkdownMathSyntax';

export type ReadingPart = { kind: 'text' | 'formula' | 'field'; source: string; label?: string; tex?: string };
export type ReadingProseBlock = { type: 'markdown'; text: string }
  | { type: 'prose'; source: string; title: string; parts: ReadingPart[] };

const fields: Record<string, string> = {
  reference: '参考值', candidate: '候选值', relative_improvement: '相对改善',
  direction: '优化方向', comparison: '比较方式', minimum_relative_improvement: '改善门槛',
};
const parser = unified().use(remarkParse).use(remarkGfm).use(remarkMarkdownMath);
const close: Record<string, string> = { ')': '(', ']': '[', '}': '{', '）': '（', '】': '【' };

/** Split only at prose separators outside brackets. Subscript commas, decimal
 * points, function arguments and set members are all part of the expression. */
function clauses(source: string): string[] | null {
  const stack: string[] = [], result: string[] = [];
  let start = 0;
  for (let index = 0; index < source.length; index++) {
    const char = source[index];
    if ('([{（【'.includes(char)) stack.push(char);
    else if (close[char]) {
      if (stack.pop() !== close[char]) return null;
    } else if (!stack.length && (/[，。；、;\n]/u.test(char)
      || char === ',' && /^\s+(?:[A-Za-z\u0370-\u03ff]|[\u3400-\u9fff])/.test(source.slice(index + 1)))) {
      result.push(source.slice(start, index + 1)); start = index + 1;
    }
  }
  if (stack.length) return null;
  if (start < source.length) result.push(source.slice(start));
  return result;
}

function textLabel(source: string): string {
  if (/^(?:用于|仅(?:用于)?validation|仅(?:用于)?test|适用(?:于|阶段|范围))/.test(source.trim())) return '适用范围';
  if (/^(?:门槛|阈值)/.test(source.trim())) return '阈值';
  return '说明';
}

function appendText(parts: ReadingPart[], source: string) {
  if (!source) return;
  const previous = parts.at(-1);
  const label = textLabel(source);
  if (previous?.kind === 'text' && previous.label === label && !/[。；;]\s*$/.test(previous.source)) previous.source += source;
  else parts.push({ kind: 'text', source, label });
}

function plainParts(source: string): ReadingPart[] | null {
  const split = clauses(source);
  if (!split) return null;
  const parts: ReadingPart[] = [];
  for (const clause of split) {
    // Existing assignment notation only: no inferred equations or thresholds.
    // Chinese subscripts such as Δ_F,s,线性 are retained verbatim.
    const assignment = /(?:\[[^\]\r\n]+\]|[A-Za-z\u0370-\u03ff][\p{L}\p{M}\p{N}_^*,.{}()[\]′'−+/-]*)\s*=(?!=)/u.exec(clause);
    if (!assignment || /(?:https?:\/\/|www\.|mailto:|[<>])/.test(clause)
      || !clause.slice(assignment.index + assignment[0].length).replace(/[，。；、;\s]/g, '')) {
      appendText(parts, clause); continue;
    }
    const before = clause.slice(0, assignment.index);
    // A spaced or otherwise ambiguous LHS stays intact instead of extracting
    // a suffix (e.g. x + y = z, \alpha = 1 or an unfinished expression).
    if (/[A-Za-z\u0370-\u03ff\\+*/^−-]\s*$/.test(before)) {
      appendText(parts, clause); continue;
    }
    appendText(parts, before);
    const key = assignment[0].replace(/\s*=$/, '');
    parts.push({ kind: Object.hasOwn(fields, key) ? 'field' : 'formula',
      source: clause.slice(assignment.index), label: fields[key] ?? '公式' });
  }
  return parts;
}

function supported(node: Nodes, source: string): boolean {
  if (node.type === 'text' || node.type === 'markdownMath') return true;
  // Bare Greek subscripts can be parsed as Markdown emphasis. Recover their
  // exact source; intentional emphasis, links, code and HTML use Markdown.
  if (node.type === 'emphasis' && node.position) {
    const start = node.position.start.offset!;
    return (source[start] === '_' || source[start] === '*') && /[\p{L}\p{M}\p{N}*]/u.test(source[start - 1] ?? '')
      && node.children.every(child => child.type === 'text');
  }
  return false;
}

function projectParagraph(node: Paragraph, source: string): ReadingProseBlock | null {
  if (!node.children.every(child => supported(child, source))) return null;
  const start = node.position?.start.offset, end = node.position?.end.offset;
  if (start === undefined || end === undefined) return null;
  const original = source.slice(start, end);
  if (!node.children.some(child => child.type === 'markdownMath') && /\$|\\[([]/.test(original)) return null;
  // Explicit metric keys are labels, never guessed from an arbitrary sentence.
  const title = /^[A-Za-z][A-Za-z0-9_]*_[A-Za-z0-9_]+[ \t]*[：:][ \t]*/.exec(original)?.[0] ?? '';
  const parts: ReadingPart[] = [];
  let cursor = start + title.length;
  for (const child of node.children) {
    if (child.type !== 'markdownMath') continue;
    const from = child.position?.start.offset, to = child.position?.end.offset;
    if (from === undefined || to === undefined || from < cursor) return null;
    const preceding = plainParts(source.slice(cursor, from));
    if (!preceding) return null;
    parts.push(...preceding, { kind: 'formula', source: source.slice(from, to), tex: child.value, label: '公式' });
    cursor = to;
  }
  const remainder = plainParts(source.slice(cursor, end));
  if (!remainder) return null;
  parts.push(...remainder);
  if (!parts.some(part => part.kind !== 'text')) return null;
  return { type: 'prose', source: original, title, parts };
}

/** A reading projection of complete top-level paragraphs. The original bytes
 * and their order remain recoverable, while all other Markdown stays intact. */
export function implementationReadingProse(source: string): ReadingProseBlock[] {
  if (!/[=$\\]/.test(source)) return source ? [{ type: 'markdown', text: source }] : [];
  const tree = parser.parse(source) as Root;
  const result: ReadingProseBlock[] = [];
  let cursor = 0;
  for (const node of tree.children) {
    if (node.type !== 'paragraph') continue;
    const projection = projectParagraph(node, source);
    if (!projection) continue;
    const start = node.position!.start.offset!, end = node.position!.end.offset!;
    if (start > cursor) result.push({ type: 'markdown', text: source.slice(cursor, start) });
    result.push(projection); cursor = end;
  }
  if (cursor < source.length) result.push({ type: 'markdown', text: source.slice(cursor) });
  return result;
}
