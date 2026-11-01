import { implementationSectionTitle } from './implementationDocumentContent';

export type DocumentDiffKind = 'equal' | 'add' | 'remove';

/** Consecutive complete source lines; text includes the exact original endings. */
export interface DocumentDiffHunk {
  type: DocumentDiffKind;
  text: string;
  oldStart: number | null;
  newStart: number | null;
  oldLines: number;
  newLines: number;
}

export interface DocumentDiff {
  hunks: DocumentDiffHunk[];
  added: number;
  removed: number;
  unchanged: number;
  oldLineCount: number;
  newLineCount: number;
  simplified: boolean;
  simplificationReason?: string;
}

/** Callers can lower these limits; hard caps also apply to supplied values. */
export interface DocumentDiffOptions {
  maxLines?: number;
  maxWork?: number;
  maxEditDistance?: number;
  maxHunks?: number;
}

const LIMITS = { maxLines: 100_000, maxWork: 1_000_000, maxEditDistance: 256, maxHunks: 10_000 };

function boundedOption(value: number | undefined, maximum: number): number {
  if (value === undefined) return maximum;
  if (!Number.isSafeInteger(value) || value < 0) throw new RangeError('Diff limits must be nonnegative safe integers.');
  return Math.min(value, maximum);
}

/** Like a source file: an ending newline terminates a line, not an extra line. */
export function countDocumentLines(text: string): number {
  if (!text.length) return 0;
  let count = 0;
  for (let index = 0; index < text.length; index++) {
    if (text[index] === '\r') {
      count++;
      if (text[index + 1] === '\n') index++;
    } else if (text[index] === '\n') count++;
  }
  return count + (text.endsWith('\r') || text.endsWith('\n') ? 0 : 1);
}

function sourceLines(text: string): { lines: string[]; offsets: number[] } {
  const lines: string[] = [], offsets = [0];
  let start = 0;
  for (let index = 0; index < text.length; index++) {
    if (text[index] !== '\r' && text[index] !== '\n') continue;
    if (text[index] === '\r' && text[index + 1] === '\n') index++;
    lines.push(text.slice(start, index + 1));
    start = index + 1;
    offsets.push(start);
  }
  if (start < text.length) {
    lines.push(text.slice(start));
    offsets.push(text.length);
  }
  return { lines, offsets };
}

function resultFor(hunks: DocumentDiffHunk[], reasons: Iterable<string> = []): DocumentDiff {
  let added = 0, removed = 0, unchanged = 0;
  for (const hunk of hunks) {
    if (hunk.type === 'add') added += hunk.newLines;
    else if (hunk.type === 'remove') removed += hunk.oldLines;
    else unchanged += hunk.oldLines;
  }
  const reason = Array.from(new Set(reasons)).join(',');
  return { hunks, added, removed, unchanged, oldLineCount: removed + unchanged,
    newLineCount: added + unchanged, simplified: Boolean(reason),
    ...(reason ? { simplificationReason: reason } : {}) };
}

function isLineStart(text: string, index: number): boolean {
  return index === 0 || text[index - 1] === '\n' || (text[index - 1] === '\r' && text[index] !== '\n');
}

/** Bounded fallback still includes every byte of both inputs, with common edges. */
function coarseDiff(oldText: string, newText: string, reason?: string): DocumentDiff {
  let prefix = 0;
  const limit = Math.min(oldText.length, newText.length);
  for (let index = 0; index < limit && oldText[index] === newText[index]; index++) {
    if (oldText[index] === '\n' || (oldText[index] === '\r'
      && oldText[index + 1] !== '\n' && newText[index + 1] !== '\n')) prefix = index + 1;
  }
  let oldEnd = oldText.length, newEnd = newText.length;
  while (oldEnd > prefix && newEnd > prefix && oldText[oldEnd - 1] === newText[newEnd - 1]) {
    oldEnd--; newEnd--;
  }
  // A character suffix inside a changed line must not become an equal line.
  while (oldEnd < oldText.length && (!isLineStart(oldText, oldEnd) || !isLineStart(newText, newEnd))) {
    oldEnd++; newEnd++;
  }
  const hunks: DocumentDiffHunk[] = [];
  let oldLine = 1, newLine = 1;
  const push = (type: DocumentDiffKind, text: string) => {
    if (!text) return;
    const count = countDocumentLines(text);
    const oldLines = type === 'add' ? 0 : count, newLines = type === 'remove' ? 0 : count;
    hunks.push({ type, text, oldStart: oldLines ? oldLine : null, newStart: newLines ? newLine : null,
      oldLines, newLines });
    oldLine += oldLines; newLine += newLines;
  };
  push('equal', oldText.slice(0, prefix));
  push('remove', oldText.slice(prefix, oldEnd));
  push('add', newText.slice(prefix, newEnd));
  push('equal', oldText.slice(oldEnd));
  return resultFor(hunks, reason ? [reason] : []);
}

interface Range { type: DocumentDiffKind; a0: number; a1: number; b0: number; b1: number }
interface Anchor { a: number; b: number }

/**
 * Patience anchors plus bounded Myers for repeated-line gaps. No n*m table,
 * unbounded recursion, omitted tail, numeric parsing, or newline normalization.
 * A simplified diff is a complete coarse replacement, not a minimal edit count.
 */
export function diffDocumentText(oldText: string, newText: string, options: DocumentDiffOptions = {}): DocumentDiff {
  if (typeof oldText !== 'string' || typeof newText !== 'string') throw new TypeError('Diff inputs must be strings.');
  const maxLines = boundedOption(options.maxLines, LIMITS.maxLines);
  const maxWork = boundedOption(options.maxWork, LIMITS.maxWork);
  const maxDistance = boundedOption(options.maxEditDistance, LIMITS.maxEditDistance);
  const maxHunks = boundedOption(options.maxHunks, LIMITS.maxHunks);
  if (oldText === newText) {
    const count = countDocumentLines(oldText);
    return resultFor(count ? [{ type: 'equal', text: oldText, oldStart: 1, newStart: 1, oldLines: count, newLines: count }] : []);
  }
  if (!oldText || !newText) return coarseDiff(oldText, newText);
  if (countDocumentLines(oldText) + countDocumentLines(newText) > maxLines) return coarseDiff(oldText, newText, 'line_limit');
  const old = sourceLines(oldText), next = sourceLines(newText);
  const a = old.lines, b = next.lines, ranges: Range[] = [], reasons = new Set<string>();
  let work = maxWork, tooManyHunks = false;
  const emit = (type: DocumentDiffKind, a0: number, a1: number, b0: number, b1: number) => {
    if (a0 === a1 && b0 === b1) return;
    const previous = ranges[ranges.length - 1];
    if (previous?.type === type && previous.a1 === a0 && previous.b1 === b0) {
      previous.a1 = a1; previous.b1 = b1;
    } else if (ranges.length >= maxHunks) tooManyHunks = true;
    else ranges.push({ type, a0, a1, b0, b1 });
  };
  const replacement = (a0: number, a1: number, b0: number, b1: number, reason: string) => {
    reasons.add(reason);
    emit('remove', a0, a1, b0, b0);
    emit('add', a1, a1, b0, b1);
  };
  const anchors = (a0: number, a1: number, b0: number, b1: number): Anchor[] | null => {
    work -= a1 - a0 + b1 - b0;
    if (work < 0) return null;
    const oldUnique = new Map<string, number>(), newUnique = new Map<string, number>();
    for (let i = a0; i < a1; i++) oldUnique.set(a[i], oldUnique.has(a[i]) ? -1 : i);
    for (let j = b0; j < b1; j++) newUnique.set(b[j], newUnique.has(b[j]) ? -1 : j);
    const matches: Anchor[] = [];
    for (let j = b0; j < b1; j++) {
      const i = oldUnique.get(b[j]);
      if (i !== undefined && i >= 0 && newUnique.get(b[j]) === j) matches.push({ a: i, b: j });
    }
    // Longest increasing old-position subsequence, in new-document order.
    const tails: number[] = [], previous: number[] = [];
    for (let i = 0; i < matches.length; i++) {
      let low = 0, high = tails.length;
      while (low < high) {
        if (--work < 0) return null;
        const middle = (low + high) >>> 1;
        if (matches[tails[middle]].a < matches[i].a) low = middle + 1;
        else high = middle;
      }
      previous[i] = low ? tails[low - 1] : -1;
      tails[low] = i;
    }
    const result: Anchor[] = [];
    for (let i = tails.length ? tails[tails.length - 1] : -1; i >= 0; i = previous[i]) result.push(matches[i]);
    return result.reverse();
  };
  const myers = (a0: number, a1: number, b0: number, b1: number): Range[] | null => {
    const n = a1 - a0, m = b1 - b0, distance = Math.min(maxDistance, n + m);
    if (Math.abs(n - m) > distance) return null;
    const offset = distance + 1, frontier = new Int32Array(distance * 2 + 3);
    frontier.fill(-1); frontier[offset + 1] = 0;
    const trace: Int32Array[] = [];
    for (let d = 0; d <= distance; d++) {
      trace.push(frontier.slice());
      for (let k = -d; k <= d; k += 2) {
        if (--work < 0) return null;
        let x = k === -d || (k !== d && frontier[offset + k - 1] < frontier[offset + k + 1])
          ? frontier[offset + k + 1] : frontier[offset + k - 1] + 1;
        let y = x - k;
        while (x < n && y < m && a[a0 + x] === b[b0 + y]) {
          if (--work < 0) return null;
          x++; y++;
        }
        frontier[offset + k] = x;
        if (x < n || y < m) continue;
        const edits: Range[] = [];
        for (let depth = d; depth >= 0; depth--) {
          const saved = trace[depth], diagonal = x - y;
          const previousDiagonal = diagonal === -depth || (diagonal !== depth
            && saved[offset + diagonal - 1] < saved[offset + diagonal + 1]) ? diagonal + 1 : diagonal - 1;
          const previousX = saved[offset + previousDiagonal], previousY = previousX - previousDiagonal;
          while (x > previousX && y > previousY) {
            edits.push({ type: 'equal', a0: a0 + x - 1, a1: a0 + x, b0: b0 + y - 1, b1: b0 + y });
            x--; y--;
          }
          if (!depth) break;
          if (x === previousX) {
            edits.push({ type: 'add', a0: a0 + x, a1: a0 + x, b0: b0 + y - 1, b1: b0 + y });
            y--;
          } else {
            edits.push({ type: 'remove', a0: a0 + x - 1, a1: a0 + x, b0: b0 + y, b1: b0 + y });
            x--;
          }
        }
        return edits.reverse();
      }
    }
    return null;
  };
  const walk = (a0: number, a1: number, b0: number, b1: number, depth: number): void => {
    if (tooManyHunks) return;
    let prefix = 0, suffix = 0;
    while (a0 + prefix < a1 && b0 + prefix < b1 && a[a0 + prefix] === b[b0 + prefix]) prefix++;
    emit('equal', a0, a0 + prefix, b0, b0 + prefix);
    a0 += prefix; b0 += prefix;
    while (a1 - suffix > a0 && b1 - suffix > b0 && a[a1 - suffix - 1] === b[b1 - suffix - 1]) suffix++;
    a1 -= suffix; b1 -= suffix;
    if (a0 === a1) emit('add', a0, a1, b0, b1);
    else if (b0 === b1) emit('remove', a0, a1, b0, b1);
    else if (depth >= 32 || work <= 0) replacement(a0, a1, b0, b1, depth >= 32 ? 'depth_limit' : 'work_limit');
    else {
      const matches = anchors(a0, a1, b0, b1);
      if (matches === null) replacement(a0, a1, b0, b1, 'work_limit');
      else if (matches.length) {
        let previousA = a0, previousB = b0;
        for (const match of matches) {
          walk(previousA, match.a, previousB, match.b, depth + 1);
          emit('equal', match.a, match.a + 1, match.b, match.b + 1);
          previousA = match.a + 1; previousB = match.b + 1;
          if (tooManyHunks) break;
        }
        walk(previousA, a1, previousB, b1, depth + 1);
      } else {
        const edits = myers(a0, a1, b0, b1);
        if (edits === null) replacement(a0, a1, b0, b1, work < 0 ? 'work_limit' : 'edit_distance_limit');
        else for (const edit of edits) emit(edit.type, edit.a0, edit.a1, edit.b0, edit.b1);
      }
    }
    emit('equal', a1, a1 + suffix, b1, b1 + suffix);
  };
  walk(0, a.length, 0, b.length, 0);
  if (tooManyHunks) return coarseDiff(oldText, newText, 'hunk_limit');
  return resultFor(ranges.map(range => ({ type: range.type,
    text: range.type === 'add' ? newText.slice(next.offsets[range.b0], next.offsets[range.b1])
      : oldText.slice(old.offsets[range.a0], old.offsets[range.a1]),
    oldStart: range.type === 'add' ? null : range.a0 + 1,
    newStart: range.type === 'remove' ? null : range.b0 + 1,
    oldLines: range.a1 - range.a0, newLines: range.b1 - range.b0 })), reasons);
}

export interface DocumentSection {
  title?: unknown;
  field?: unknown;
  text?: unknown;
  [key: string]: unknown;
}

export interface DocumentSectionMatch<T extends DocumentSection = DocumentSection> {
  key: string;
  title: string;
  oldSection: T | null;
  newSection: T | null;
  oldIndex: number | null;
  newIndex: number | null;
  status: 'equal' | 'changed' | 'added' | 'removed';
}

/** Section ids/offsets are version-local; only semantic fields/titles can pair. */
export function matchDocumentSections<T extends DocumentSection>(oldSections: readonly T[], newSections: readonly T[]): DocumentSectionMatch<T>[] {
  const identity = (section: T): string | null => {
    const value = typeof section.field === 'string' && section.field ? section.field : section.title;
    return typeof value === 'string' && value ? implementationSectionTitle(value) : null;
  };
  const groups = new Map<string, { old: number[]; next: number[] }>();
  const oldOccurrences: number[] = [], newOccurrences: number[] = [];
  const collect = (sections: readonly T[], side: 'old' | 'next') => sections.forEach((section, index) => {
    const name = identity(section);
    if (name === null) return;
    const group = groups.get(name) ?? { old: [], next: [] };
    (side === 'old' ? oldOccurrences : newOccurrences)[index] = group[side].length;
    group[side].push(index); groups.set(name, group);
  });
  collect(oldSections, 'old'); collect(newSections, 'next');
  const pairs = new Map<number, number>(), pairedOld = new Set<number>();
  const pair = (oldIndex: number, newIndex: number) => { pairs.set(newIndex, oldIndex); pairedOld.add(oldIndex); };
  for (const group of groups.values()) {
    if (group.old.length === 1 && group.next.length === 1) { pair(group.old[0], group.next[0]); continue; }
    // Duplicate titles are paired by exact body first. Ambiguous changed bodies
    // remain explicit additions/removals instead of silently matching by sN.
    const bodies = new Map<string, { indices: number[]; used: number }>();
    for (const index of group.old) {
      const text = oldSections[index].text;
      if (typeof text !== 'string') continue;
      const body = bodies.get(text) ?? { indices: [], used: 0 };
      body.indices.push(index); bodies.set(text, body);
    }
    for (const index of group.next) {
      const text = newSections[index].text;
      const body = typeof text === 'string' ? bodies.get(text) : undefined;
      if (body && body.used < body.indices.length) pair(body.indices[body.used++], index);
    }
    const oldRest = group.old.filter(index => !pairedOld.has(index));
    const newRest = group.next.filter(index => !pairs.has(index));
    if (oldRest.length === 1 && newRest.length === 1) pair(oldRest[0], newRest[0]);
  }
  const results: DocumentSectionMatch<T>[] = [], emittedOld = new Set<number>();
  const append = (oldIndex: number | null, newIndex: number | null) => {
    const oldSection = oldIndex === null ? null : oldSections[oldIndex];
    const newSection = newIndex === null ? null : newSections[newIndex];
    const section = (newSection ?? oldSection)!;
    const name = identity(section), group = name === null ? undefined : groups.get(name);
    const unique = group && group.old.length <= 1 && group.next.length <= 1;
    const occurrence = oldIndex !== null ? `old:${oldOccurrences[oldIndex] ?? oldIndex}`
      : `new:${newOccurrences[newIndex!] ?? newIndex}`;
    const title = typeof section.title === 'string' && section.title
      ? implementationSectionTitle(section.title) : name ?? '未命名章节';
    results.push({ key: `section:${JSON.stringify(name)}${unique ? '' : `:${occurrence}`}`, title,
      oldSection, newSection, oldIndex, newIndex,
      status: !oldSection ? 'added' : !newSection ? 'removed'
        : typeof oldSection.text === 'string' && oldSection.text === newSection.text ? 'equal' : 'changed' });
    if (oldIndex !== null) emittedOld.add(oldIndex);
  };
  // Follow the new document; place deletions before their next surviving peer.
  let deletionCursor = 0;
  for (let newIndex = 0; newIndex < newSections.length; newIndex++) {
    const oldIndex = pairs.get(newIndex);
    if (oldIndex !== undefined) {
      while (deletionCursor < oldIndex) {
        if (!pairedOld.has(deletionCursor)) append(deletionCursor, null);
        deletionCursor++;
      }
    }
    append(oldIndex ?? null, newIndex);
  }
  for (let oldIndex = 0; oldIndex < oldSections.length; oldIndex++) {
    if (!emittedOld.has(oldIndex)) append(oldIndex, null);
  }
  return results;
}
