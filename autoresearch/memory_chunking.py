"""Exact text ranges, preferring structural boundaries for newly indexed text."""
from __future__ import annotations

import re

VERSION = 'structure-v1'
HEADING = re.compile(r'^\s{0,3}(?:#{1,6}\s+|\d+(?:\.\d+)*\.?\s+\S|(?:Abstract|Introduction|Methods?|Results|Discussion|References|方法|实验|结果|参考文献)\s*$)', re.I)


def ranges(text, *, size=2200, overlap=250):
    """Return (start,end,strategy) without rewriting text or historical indexes.

    Ordinary unstructured text retains the old 2200/250 windows. Recognized
    code/formula/table blocks stay atomic, including blocks larger than size.
    """
    if not text:
        return []
    lines = text.splitlines(keepends=True)
    units, position, start, protected = [], 0, 0, None
    structured = False
    for line in lines:
        stripped = line.strip()
        fence = re.match(r'^\s{0,3}(`{3,}|~{3,})', line)
        formula = stripped == '$$' or stripped in {r'\[', r'\]'}
        table = bool(stripped.startswith('|') and '|' in stripped[1:])
        if protected:
            closes = ((protected in {'`', '~'} and fence and fence[1][0] == protected)
                      or (protected == '$$' and formula)
                      or (protected == 'table' and not table and stripped))
            if protected == 'table' and closes:
                units.append((start, position, True)); start = position; protected = None
            elif closes:
                position += len(line)
                units.append((start, position, True)); start = position; protected = None
                continue
            else:
                position += len(line)
                continue
        if fence or formula or table:
            structured = True
            if position > start:
                units.append((start, position, False))
            start = position
            protected = fence[1][0] if fence else ('$$' if formula else 'table')
        elif HEADING.match(line):
            structured = True
            if position > start:
                units.append((start, position, False))
            start = position
        position += len(line)
        if not protected and not stripped:
            structured = True
            units.append((start, position, False)); start = position
    if position > start:
        units.append((start, position, bool(protected)))
    if not structured:
        return [(i, min(i+size, len(text)), 'fixed-window-v1')
                for i in range(0, len(text), size-overlap)]
    result, active = [], None
    for start, end, atomic in units:
        # Long plain paragraphs remain bounded; protected structures do not.
        pieces = [(start, end)] if atomic or end-start <= size else [
            (i, min(i+size, end)) for i in range(start, end, size-overlap)]
        for first, stop in pieces:
            if active is not None and stop-active[0] > size:
                result.append((*active, VERSION)); active = None
            active = (first, stop) if active is None else (active[0], stop)
            if atomic and stop-first > size:
                result.append((*active, VERSION)); active = None
    if active is not None:
        result.append((*active, VERSION))
    return result
