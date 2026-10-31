import { ScientificDiagramMath } from './ScientificDiagramMath';

/** Presentation over saved agent prose; this parser never derives new formulas. */
export interface ScientificDiagramBlock { kind: 'paragraph' | 'equation'; text: string }

const letters = /[A-Za-z0-9\u0370-\u03ff\u1f00-\u1fff\u2070-\u209f\u00b2\u00b3\u00b9]/u;
const startsVariable = /^(?:[A-Za-z0-9\u0370-\u03ff]|\\[A-Za-z])/u;
const signs = new Set([..."_\\^{}()[]+−-–*/=<>≤≥≠≈≡∝∈∉√ΣΠ∑∏∫∞±×⋅·⊙′″'!%|&:"]);
const closing: Record<string, string> = { ')': '(', ']': '[', '}': '{' };
const opening = new Set(['(', '[', '{']);
const proseWords = /^(?:and|where|with|then|must|only|when|because|therefore|which|that|is|are|was|were|the|but|or|to|for|from|using|after|before)$/i;

function escaped(text: string, at: number) {
  let slashes = 0;
  for (let i = at - 1; i >= 0 && text[i] === '\\'; i--) slashes++;
  return slashes % 2 === 1;
}

function append(blocks: ScientificDiagramBlock[], kind: ScientificDiagramBlock['kind'], text: string) {
  const value = text.trim();
  if (!value) return;
  // A separating comma/period remains in its original order, without a stray
  // punctuation-only paragraph between two standalone formulas.
  if (/^[，。；、,.;:：]+$/u.test(value) && blocks.length) {
    blocks[blocks.length - 1].text += value;
  } else blocks.push({ kind, text: value });
}

function paragraphs(blocks: ScientificDiagramBlock[], text: string) {
  for (const part of text.match(/[^\n。；]+[。；]?|[。；]/gu) ?? []) append(blocks, 'paragraph', part);
}

function formulaStart(text: string, operator: number, floor: number) {
  let end = operator;
  while (end > floor && /\s/u.test(text[end - 1])) end--;
  let start = end;
  const stack: string[] = [];
  while (start > floor) {
    const char = text[start - 1];
    if (closing[char]) stack.push(closing[char]);
    else if (opening.has(char)) {
      if (stack.at(-1) !== char) break;
      stack.pop();
    } else if (/\s/u.test(char)) {
      if (!stack.length) break;
    } else if (!(letters.test(char) || signs.has(char) || (stack.length && /[,;]/u.test(char)))) break;
    start--;
  }
  const lhs = text.slice(start, end);
  if (stack.length || !startsVariable.test(lhs)) return null;
  // Leave URLs, HTML attributes and a suffix of a longer spaced expression
  // intact as prose. Explicit $...$ remains available for complex notation.
  const prefix = text.slice(floor, start);
  const token = prefix.match(/[^\s，。；]*$/u)?.[0] ?? '';
  const tag = prefix.lastIndexOf('<');
  if (/(?:https?:\/\/|www\.|mailto:)/i.test(token)
      || (tag > prefix.lastIndexOf('>') && /^<\/?[A-Za-z]/u.test(prefix.slice(tag)))
      || (text[start - 1] === '<' && /^[A-Za-z]+$/u.test(lhs))
      || /[+*/^−-]\s*$/u.test(prefix)) return null;
  return start;
}

function formulaEnd(text: string, start: number) {
  let end = start;
  const stack: string[] = [];
  while (end < text.length) {
    const char = text[end];
    if (char === '\n' || char === '\r') break;
    if (/\s/u.test(char) && !stack.length) {
      const following = text.slice(end).match(/^\s+([A-Za-z][A-Za-z0-9_]*)/u);
      if (following) {
        const word = following[1], after = text.slice(end + following[0].length);
        const previous = text.slice(start, end).trimEnd().at(-1) ?? '';
        if (proseWords.test(word) || /^(?:\s*)(?:=|<=|>=|≤|≥|≠|≈)/u.test(after)
            || (text.slice(start, end).trim().length > 0 && word.length > 1
              && !/^\s*\(/u.test(after) && !/[+*/^=<>≤≥−-]/u.test(previous))) break;
      }
    }
    if (opening.has(char)) stack.push(char);
    else if (closing[char]) {
      if (stack.at(-1) !== closing[char]) break;
      stack.pop();
    } else if (char === '.') {
      if (!(letters.test(text[end - 1] ?? '') && letters.test(text[end + 1] ?? ''))) break;
    } else if (char === ',' || char === ';') {
      if (!stack.length) break;
    } else if (!(letters.test(char) || signs.has(char) || /\s/u.test(char))) break;
    end++;
  }
  if (stack.length) return start;
  while (end > start && /\s/u.test(text[end - 1])) end--;
  return end;
}

function plain(blocks: ScientificDiagramBlock[], text: string) {
  const relations = /===|==|!=|<=|>=|=|≤|≥|≠|≈|≡|∝|<|>/gu;
  let cursor = 0;
  for (let match = relations.exec(text); match; match = relations.exec(text)) {
    if (text[match.index] === '<' && /^<\/?[A-Za-z][\w:-]*(?:\s|>|\/)/u.test(text.slice(match.index))) continue;
    const start = formulaStart(text, match.index, cursor);
    if (start === null) continue;
    const rhs = match.index + match[0].length, end = formulaEnd(text, rhs);
    if (!letters.test(text.slice(rhs, end))) continue;
    paragraphs(blocks, text.slice(cursor, start));
    let after = end;
    if (/^[，。；,.;]/u.test(text.slice(after))) after++;
    append(blocks, 'equation', text.slice(start, after));
    cursor = after;
    relations.lastIndex = after;
  }
  paragraphs(blocks, text.slice(cursor));
}

/** Extract explicit math and clear existing relations, preserving their bytes. */
export function splitScientificDiagramText(text: string): ScientificDiagramBlock[] {
  const blocks: ScientificDiagramBlock[] = [];
  let cursor = 0, index = 0;
  while (index < text.length) {
    if (text[index] === '`' && !escaped(text, index)) {
      let endMarker = index + 1;
      while (text[endMarker] === '`') endMarker++;
      const marker = text.slice(index, endMarker), closed = text.indexOf(marker, endMarker);
      const end = closed < 0 ? text.length : closed + marker.length;
      plain(blocks, text.slice(cursor, index));
      append(blocks, 'paragraph', text.slice(index, end));
      cursor = index = end;
      continue;
    }
    if (text[index] !== '$' || escaped(text, index)) { index++; continue; }
    const marker = text[index + 1] === '$' ? '$$' : '$';
    let closed = index + marker.length;
    while (closed < text.length && !(text.startsWith(marker, closed) && !escaped(text, closed)
      && (marker.length === 2 || (text[closed - 1] !== '$' && text[closed + 1] !== '$')))) closed++;
    if (closed >= text.length) {
      plain(blocks, text.slice(cursor, index));
      append(blocks, 'paragraph', text.slice(index));
      cursor = index = text.length;
      break;
    }
    const math = text.slice(index + marker.length, closed);
    // Currency/prose between two unrelated dollar signs is not a formula.
    if (marker.length === 1 && (math.includes('\n') || /\b(?:and|dollars?|USD)\b/iu.test(math))) {
      index = closed + marker.length;
      continue;
    }
    plain(blocks, text.slice(cursor, index));
    append(blocks, 'equation', math);
    cursor = index = closed + marker.length;
  }
  plain(blocks, text.slice(cursor));
  return blocks;
}

export function ScientificDiagramText({ text }: { text: string }) {
  const blocks = splitScientificDiagramText(text);
  return <div className="scientific-diagram-text">{blocks.map((block, index) => block.kind === 'equation'
    ? <div className="scientific-diagram-equation" role="math" key={index}><ScientificDiagramMath text={block.text} /></div>
    : <p className="scientific-diagram-paragraph" key={index}>{block.text}</p>)}</div>;
}
