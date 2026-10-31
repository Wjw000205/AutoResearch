import { createElement, useId, useMemo, type CSSProperties, type Ref, type ReactNode } from 'react';

const SVG_NS = 'http://www.w3.org/2000/svg';
const XMLNS_NS = 'http://www.w3.org/2000/xmlns/';
const ELEMENTS = new Set(['svg', 'g', 'defs', 'marker', 'clipPath', 'rect', 'line', 'path',
  'circle', 'ellipse', 'polygon', 'polyline', 'text', 'tspan', 'title', 'desc', 'metadata']);
const ID = /^[A-Za-z_][\w:.-]{0,159}$/u;
const NUMBER = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/iu;
const NUMERIC = new Set(['x', 'y', 'x1', 'y1', 'x2', 'y2', 'cx', 'cy', 'r', 'rx', 'ry',
  'width', 'height', 'refX', 'refY', 'markerWidth', 'markerHeight', 'stroke-width',
  'stroke-dashoffset', 'opacity', 'fill-opacity', 'stroke-opacity', 'font-size', 'textLength']);
const PAINT = new Set(['fill', 'stroke', 'color']);
const REFERENCES = new Set(['clip-path', 'marker-start', 'marker-mid', 'marker-end']);
const ENUMS: Record<string, ReadonlySet<string>> = {
  'font-weight': new Set(['normal', 'bold', 'bolder', 'lighter', ...Array.from({ length: 9 }, (_, i) => String((i + 1) * 100))]),
  'font-style': new Set(['normal', 'italic', 'oblique']),
  'text-anchor': new Set(['start', 'middle', 'end']),
  'dominant-baseline': new Set(['auto', 'alphabetic', 'central', 'middle', 'hanging', 'text-before-edge', 'text-after-edge']),
  'stroke-linecap': new Set(['butt', 'round', 'square']),
  'stroke-linejoin': new Set(['miter', 'round', 'bevel']),
  'fill-rule': new Set(['nonzero', 'evenodd']),
  'clip-rule': new Set(['nonzero', 'evenodd']),
  'vector-effect': new Set(['none', 'non-scaling-stroke']),
  'markerUnits': new Set(['strokeWidth', 'userSpaceOnUse']),
  'clipPathUnits': new Set(['objectBoundingBox', 'userSpaceOnUse']),
  'lengthAdjust': new Set(['spacing', 'spacingAndGlyphs']),
};
const PRESENTATION = new Set([...NUMERIC, ...PAINT, ...REFERENCES, ...Object.keys(ENUMS),
  'stroke-dasharray', 'font-family', 'paint-order']);
const REACT_ATTRIBUTES: Record<string, string> = {
  'font-family': 'fontFamily', 'font-size': 'fontSize', 'font-weight': 'fontWeight', 'font-style': 'fontStyle',
  'text-anchor': 'textAnchor', 'dominant-baseline': 'dominantBaseline', 'paint-order': 'paintOrder',
  'stroke-width': 'strokeWidth', 'stroke-dasharray': 'strokeDasharray', 'stroke-dashoffset': 'strokeDashoffset',
  'stroke-linecap': 'strokeLinecap', 'stroke-linejoin': 'strokeLinejoin', 'fill-opacity': 'fillOpacity',
  'stroke-opacity': 'strokeOpacity', 'fill-rule': 'fillRule', 'clip-rule': 'clipRule', 'clip-path': 'clipPath',
  'marker-start': 'markerStart', 'marker-mid': 'markerMid', 'marker-end': 'markerEnd', 'vector-effect': 'vectorEffect',
};

export type ScientificSvgMatrix = readonly [number, number, number, number, number, number];
export interface ScientificSvgElement {
  tag: string;
  key: string;
  attributes: Record<string, string>;
  style?: Record<string, string>;
  children: (ScientificSvgElement | string)[];
}
export interface ScientificDiagramBounds { left: number; top: number; right: number; bottom: number }
export interface ScientificDiagramGeometry {
  id: string;
  moduleId: string;
  nodeIds: string[];
  role: 'module' | 'glyph';
  label: string;
  x: number;
  y: number;
  width: number;
  height: number;
  bounds: ScientificDiagramBounds;
  localBounds: ScientificDiagramBounds;
  matrix: ScientificSvgMatrix;
  elementKey: string;
}
export interface ScientificDiagramConnection {
  source: string;
  target: string;
  edgeIndices: number[];
  display: 'arrow' | 'badge';
  label: string;
}
export interface ScientificDiagramDocument {
  title: string;
  width: number;
  height: number;
  viewBox: { x: number; y: number; width: number; height: number };
  tree: ScientificSvgElement;
  modules: ScientificDiagramGeometry[];
  localGlyphs: ScientificDiagramGeometry[];
  connections: ScientificDiagramConnection[];
  positions: Map<string, ScientificDiagramGeometry>;
}

const identity: ScientificSvgMatrix = [1, 0, 0, 1, 0, 0];
function multiply(a: ScientificSvgMatrix, b: ScientificSvgMatrix): ScientificSvgMatrix {
  return [a[0] * b[0] + a[2] * b[1], a[1] * b[0] + a[3] * b[1],
    a[0] * b[2] + a[2] * b[3], a[1] * b[2] + a[3] * b[3],
    a[0] * b[4] + a[2] * b[5] + a[4], a[1] * b[4] + a[3] * b[5] + a[5]];
}
function numeric(value: string, positive = false): number {
  if (!NUMBER.test(value.trim())) throw new Error('SVG 数值无效。');
  const result = Number(value);
  if (!Number.isFinite(result) || Math.abs(result) > 10_000_000 || positive && result <= 0) throw new Error('SVG 数值超出安全范围。');
  return result;
}
function numbers(value: string): number[] {
  return value.trim().split(/[\s,]+/u).filter(Boolean).map(part => numeric(part));
}

/** SVG transforms are evaluated in source order; geometry does not use an invented graph layout. */
export function scientificSvgTransform(value: string): ScientificSvgMatrix {
  let result = identity, cursor = 0;
  const token = /([A-Za-z]+)\s*\(([^()]*)\)/gu;
  for (const match of value.matchAll(token)) {
    if (!/^[\s,]*$/u.test(value.slice(cursor, match.index))) throw new Error('SVG transform 无效。');
    cursor = match.index! + match[0].length;
    const v = numbers(match[2]); let next: ScientificSvgMatrix;
    switch (match[1]) {
      case 'translate':
        if (v.length < 1 || v.length > 2) throw new Error('SVG translate 无效。');
        next = [1, 0, 0, 1, v[0], v[1] ?? 0]; break;
      case 'scale':
        if (v.length < 1 || v.length > 2 || !v[0] || v.length === 2 && !v[1]) throw new Error('SVG scale 无效。');
        next = [v[0], 0, 0, v[1] ?? v[0], 0, 0]; break;
      case 'matrix':
        if (v.length !== 6) throw new Error('SVG matrix 无效。');
        next = v as unknown as ScientificSvgMatrix; break;
      case 'rotate': {
        if (![1, 3].includes(v.length)) throw new Error('SVG rotate 无效。');
        const a = v[0] * Math.PI / 180, rotation: ScientificSvgMatrix = [Math.cos(a), Math.sin(a), -Math.sin(a), Math.cos(a), 0, 0];
        next = v.length === 1 ? rotation : multiply(multiply([1, 0, 0, 1, v[1], v[2]], rotation), [1, 0, 0, 1, -v[1], -v[2]]);
        break;
      }
      case 'skewX': case 'skewY': {
        if (v.length !== 1) throw new Error('SVG skew 无效。');
        const tangent = Math.tan(v[0] * Math.PI / 180);
        if (!Number.isFinite(tangent) || Math.abs(tangent) > 1000) throw new Error('SVG skew 超出安全范围。');
        next = match[1] === 'skewX' ? [1, 0, tangent, 1, 0, 0] : [1, tangent, 0, 1, 0, 0]; break;
      }
      default: throw new Error('SVG transform 类型不受支持。');
    }
    result = multiply(result, next);
    if (result.some(number => !Number.isFinite(number) || Math.abs(number) > 10_000_000)) throw new Error('SVG transform 超出安全范围。');
  }
  if (!/^[\s,]*$/u.test(value.slice(cursor))) throw new Error('SVG transform 无效。');
  return result;
}

function localReference(value: string): string | null {
  const match = /^url\(\s*(['"]?)#([A-Za-z_][\w:.-]{0,159})\1\s*\)$/iu.exec(value.trim());
  return match?.[2] ?? null;
}
function attribute(name: string, raw: string): string {
  const value = raw.trim();
  if (raw.length > 24_000 || /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/u.test(raw)) throw new Error('SVG 属性超出安全范围。');
  if (name === 'id') { if (!ID.test(value)) throw new Error('SVG id 无效。'); return value; }
  if (NUMERIC.has(name)) { numeric(value); return value; }
  if (PAINT.has(name)) {
    if (localReference(value) || /^(?:none|currentColor|transparent|[a-z]{1,30}|#[\da-f]{3,8}|(?:rgb|rgba|hsl|hsla)\([\d.%+,\s-]+\))$/iu.test(value)) return value;
    throw new Error('SVG 颜色或填充无效。');
  }
  if (REFERENCES.has(name)) {
    if (value === 'none' || localReference(value)) return value;
    throw new Error('SVG 只允许图内引用。');
  }
  if (ENUMS[name]) { if (!ENUMS[name].has(value)) throw new Error(`SVG ${name} 无效。`); return value; }
  if (name === 'transform') { scientificSvgTransform(value); return value; }
  if (name === 'd') {
    if (!/^[MmLlHhVvCcSsQqTtAaZzEe\d+.,\s-]+$/u.test(value)) throw new Error('SVG path 无效。');
    return value;
  }
  if (name === 'points') { if (numbers(value).length % 2) throw new Error('SVG points 无效。'); return value; }
  if (name === 'viewBox') { if (numbers(value).length !== 4) throw new Error('SVG viewBox 无效。'); return value; }
  if (name === 'stroke-dasharray') { if (value !== 'none' && numbers(value).some(part => part < 0)) throw new Error('SVG dasharray 无效。'); return value; }
  if (name === 'font-family') { if (!/^[\p{L}\p{N}_\s,'"-]+$/u.test(value)) throw new Error('SVG 字体无效。'); return value; }
  if (name === 'paint-order') { if (!/^(?:normal|(?:fill|stroke|markers)(?:\s+(?:fill|stroke|markers)){0,2})$/u.test(value)) throw new Error('SVG paint-order 无效。'); return value; }
  if (name === 'orient') { if (!['auto', 'auto-start-reverse'].includes(value)) numeric(value); return value; }
  if (name === 'preserveAspectRatio') { if (!/^(?:none|x(?:Min|Mid|Max)Y(?:Min|Mid|Max)(?:\s+(?:meet|slice))?)$/u.test(value)) throw new Error('SVG aspect ratio 无效。'); return value; }
  if (name === 'version') { if (value !== '1.1' && value !== '1.0') throw new Error('SVG version 无效。'); return value; }
  if (name === 'role') { if (!['img', 'group', 'presentation'].includes(value)) throw new Error('SVG role 无效。'); return value; }
  if (name === 'aria-hidden') { if (!['true', 'false'].includes(value)) throw new Error('SVG aria-hidden 无效。'); return value; }
  if (name === 'aria-label' || name === 'aria-description' || name === 'aria-labelledby' || name === 'aria-describedby' || /^data-[a-z][a-z\d_-]{0,70}$/u.test(name)) return raw;
  throw new Error(`SVG 属性 ${name} 不受支持。`);
}

function parseInlineStyle(value: string): Record<string, string> {
  const result: Record<string, string> = {};
  for (const clause of value.split(';').filter(part => part.trim())) {
    const separator = clause.indexOf(':');
    if (separator <= 0) throw new Error('SVG style 无效。');
    const name = clause.slice(0, separator).trim(), raw = clause.slice(separator + 1).trim();
    if (!PRESENTATION.has(name)) throw new Error(`SVG style ${name} 不受支持。`);
    result[name] = attribute(name, raw);
  }
  return result;
}
function textContent(node: ScientificSvgElement): string {
  return node.children.map(child => typeof child === 'string' ? child : textContent(child)).join('');
}
function sourceIds(value: string | undefined, fallback: string | undefined): string[] {
  const ids = (value ?? fallback ?? '').trim().split(/\s+/u).filter(Boolean);
  if (!ids.length || ids.some(id => !ID.test(id))) throw new Error('SVG 原节点映射无效。');
  return [...new Set(ids)];
}

/** Only validated SVG elements become React nodes; no raw markup enters the DOM. */
export function parseScientificDiagramSvg(source: string, parser?: Pick<DOMParser, 'parseFromString'>): ScientificDiagramDocument {
  if (!source || source.length > 2_000_000 || /<!\s*(?:DOCTYPE|ENTITY)/iu.test(source)) throw new Error('SVG 文档不受支持。');
  const withoutProlog = source.replace(/^\s*<\?xml\s[^?]*\?>/u, '');
  if (withoutProlog.includes('<?')) throw new Error('SVG 不允许处理指令或样式表。');
  const xml = (parser ?? new DOMParser()).parseFromString(source, 'image/svg+xml');
  if (xml.doctype || xml.getElementsByTagName('parsererror').length || xml.documentElement.localName !== 'svg') throw new Error('SVG XML 无效。');
  let count = 0; const ids = new Set<string>();
  function visit(element: Element, key: string, depth: number): ScientificSvgElement {
    if (++count > 30_000 || depth > 64 || element.namespaceURI !== SVG_NS || !ELEMENTS.has(element.localName)) throw new Error('SVG 包含不受支持的元素。');
    if (element.localName === 'svg' && depth > 0) throw new Error('SVG 不允许嵌套视口。');
    const result: ScientificSvgElement = { tag: element.localName, key, attributes: {}, children: [] };
    for (const item of Array.from(element.attributes)) {
      if (/^on/iu.test(item.name) || item.namespaceURI && item.namespaceURI !== XMLNS_NS) throw new Error('SVG 不允许事件、链接或外部命名空间。');
      if (item.namespaceURI === XMLNS_NS) {
        if (item.name !== 'xmlns' || item.value !== SVG_NS || depth) throw new Error('SVG 命名空间无效。');
        result.attributes.xmlns = SVG_NS; continue;
      }
      if (item.name === 'style') { result.style = parseInlineStyle(item.value); continue; }
      const clean = attribute(item.name, item.value);
      if (item.name === 'id') { if (ids.has(clean)) throw new Error('SVG id 重复。'); ids.add(clean); }
      result.attributes[item.name] = clean;
    }
    let childIndex = 0;
    for (const child of Array.from(element.childNodes)) {
      if (child.nodeType === 1) result.children.push(visit(child as Element, `${key}.${childIndex++}`, depth + 1));
      else if (child.nodeType === 3 || child.nodeType === 4) result.children.push(child.textContent ?? '');
      else if (child.nodeType !== 8) throw new Error('SVG 包含不受支持的 XML 内容。');
    }
    return result;
  }
  const tree = visit(xml.documentElement, 'root', 0);
  function checkReferences(node: ScientificSvgElement) {
    for (const [name, value] of [...Object.entries(node.attributes), ...Object.entries(node.style ?? {})]) {
      const local = localReference(value);
      if (local && !ids.has(local)) throw new Error('SVG 引用了图外元素。');
      if (name === 'aria-labelledby' || name === 'aria-describedby') {
        if (value.trim().split(/\s+/u).some(id => !ids.has(id))) throw new Error('SVG 无障碍引用无效。');
      }
    }
    node.children.forEach(child => { if (typeof child !== 'string') checkReferences(child); });
  }
  checkReferences(tree);
  const width = numeric(tree.attributes.width, true), height = numeric(tree.attributes.height, true);
  const v = numbers(tree.attributes.viewBox ?? `0 0 ${width} ${height}`);
  if (v.length !== 4 || v[2] <= 0 || v[3] <= 0) throw new Error('SVG 视口无效。');
  const title = tree.children.find(child => typeof child !== 'string' && child.tag === 'title');
  const document: ScientificDiagramDocument = { title: typeof title === 'object' ? textContent(title) : '架构图',
    width, height, viewBox: { x: v[0], y: v[1], width: v[2], height: v[3] }, tree,
    modules: [], localGlyphs: [], connections: [], positions: new Map() };
  collectGeometry(document);
  return document;
}

/** Each mounted main/overview/fullscreen SVG receives a separate reference namespace. */
export function rewriteScientificDiagramIds(document: ScientificDiagramDocument, prefix: string): ScientificSvgElement {
  if (!/^[A-Za-z_][\w-]{0,95}$/u.test(prefix)) throw new Error('SVG id 前缀无效。');
  function rewrite(node: ScientificSvgElement): ScientificSvgElement {
    const attributes: Record<string, string> = {};
    function value(name: string, raw: string): string {
      if (name === 'id') return `${prefix}-${raw}`;
      if (name === 'aria-labelledby' || name === 'aria-describedby') return raw.trim().split(/\s+/u).map(id => `${prefix}-${id}`).join(' ');
      const reference = localReference(raw);
      return reference ? `url(#${prefix}-${reference})` : raw;
    }
    for (const [name, raw] of Object.entries(node.attributes)) attributes[name] = value(name, raw);
    return { ...node, attributes, style: node.style ? Object.fromEntries(Object.entries(node.style).map(([name, raw]) => [name, value(name, raw)])) : undefined,
      children: node.children.map(child => typeof child === 'string' ? child : rewrite(child)) };
  }
  return rewrite(document.tree);
}

export function scientificDiagramSelection(document: ScientificDiagramDocument, targetOrNodeId: string): ScientificDiagramGeometry | null {
  return document.localGlyphs.find(item => item.id === targetOrNodeId)
    ?? document.modules.find(item => item.id === targetOrNodeId)
    ?? document.positions.get(targetOrNodeId) ?? null;
}

// Geometry and component rendering are kept below the security boundary.
function rectangle(left: number, top: number, width: number, height: number): ScientificDiagramBounds {
  if (width < 0 || height < 0) throw new Error('SVG 图形尺寸无效。');
  return { left, top, right: left + width, bottom: top + height };
}
function union(a: ScientificDiagramBounds | null, b: ScientificDiagramBounds | null): ScientificDiagramBounds | null {
  if (!a) return b; if (!b) return a;
  return { left: Math.min(a.left, b.left), top: Math.min(a.top, b.top), right: Math.max(a.right, b.right), bottom: Math.max(a.bottom, b.bottom) };
}
function transformed(bounds: ScientificDiagramBounds, matrix: ScientificSvgMatrix): ScientificDiagramBounds {
  const points = [[bounds.left, bounds.top], [bounds.right, bounds.top], [bounds.left, bounds.bottom], [bounds.right, bounds.bottom]];
  const xs = points.map(([x, y]) => matrix[0] * x + matrix[2] * y + matrix[4]);
  const ys = points.map(([x, y]) => matrix[1] * x + matrix[3] * y + matrix[5]);
  return { left: Math.min(...xs), top: Math.min(...ys), right: Math.max(...xs), bottom: Math.max(...ys) };
}
const IGNORED_GEOMETRY = new Set(['defs', 'marker', 'clipPath', 'title', 'desc', 'metadata', 'text', 'tspan']);
function pathBounds(value: string): ScientificDiagramBounds | null {
  const tokens = value.match(/[MmLlHhVvCcSsQqTtAaZz]|[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?/giu) ?? [];
  if (value.replace(/[MmLlHhVvCcSsQqTtAaZz]|[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?/giu, '').replace(/[\s,]/gu, '')) throw new Error('SVG path 数值无效。');
  let i = 0, command = '', x = 0, y = 0, startX = 0, startY = 0, previous = '', controlX = 0, controlY = 0;
  let bounds: ScientificDiagramBounds | null = null;
  const add = (px: number, py: number) => { bounds = union(bounds, rectangle(px, py, 0, 0)); };
  const count: Record<string, number> = { M: 2, L: 2, H: 1, V: 1, C: 6, S: 4, Q: 4, T: 2, A: 7 };
  while (i < tokens.length) {
    if (/^[A-Za-z]$/u.test(tokens[i])) command = tokens[i++];
    const upper = command.toUpperCase(), relative = command !== upper;
    if (upper === 'Z') { add(startX, startY); x = startX; y = startY; previous = upper; command = ''; continue; }
    const size = count[upper];
    if (!size || i + size > tokens.length || tokens.slice(i, i + size).some(token => /^[A-Za-z]$/u.test(token))) throw new Error('SVG path 命令无效。');
    const v = tokens.slice(i, i + size).map(token => numeric(token)); i += size;
    const px = (index: number) => v[index] + (relative ? x : 0), py = (index: number) => v[index] + (relative ? y : 0);
    if (upper === 'H') { x = px(0); add(x, y); }
    else if (upper === 'V') { y = py(0); add(x, y); }
    else if (upper === 'A') {
      if (v[0] < 0 || v[1] < 0 || ![0, 1].includes(v[3]) || ![0, 1].includes(v[4])) throw new Error('SVG arc 无效。');
      const endX = px(5), endY = py(6);
      // Conservative selection bounds include arc endpoints and radii; drawing itself is untouched.
      const radius = Math.max(v[0], v[1]); add(Math.min(x, endX) - 2 * radius, Math.min(y, endY) - 2 * radius);
      add(Math.max(x, endX) + 2 * radius, Math.max(y, endY) + 2 * radius); x = endX; y = endY;
    } else {
      if (upper === 'S' && ['C', 'S'].includes(previous) || upper === 'T' && ['Q', 'T'].includes(previous)) add(2 * x - controlX, 2 * y - controlY);
      for (let j = 0; j < size; j += 2) add(px(j), py(j + 1));
      if (['C', 'S', 'Q'].includes(upper)) { controlX = px(size - 4); controlY = py(size - 3); }
      x = px(size - 2); y = py(size - 1);
      if (upper === 'M') { startX = x; startY = y; command = relative ? 'l' : 'L'; }
    }
    previous = upper;
  }
  return bounds;
}
function shapeBounds(node: ScientificSvgElement): ScientificDiagramBounds | null {
  const a = node.attributes, n = (name: string, fallback = 0) => a[name] === undefined ? fallback : numeric(a[name]);
  switch (node.tag) {
    case 'rect': return rectangle(n('x'), n('y'), n('width'), n('height'));
    case 'circle': return rectangle(n('cx') - n('r'), n('cy') - n('r'), n('r') * 2, n('r') * 2);
    case 'ellipse': return rectangle(n('cx') - n('rx'), n('cy') - n('ry'), n('rx') * 2, n('ry') * 2);
    case 'line': return { left: Math.min(n('x1'), n('x2')), top: Math.min(n('y1'), n('y2')), right: Math.max(n('x1'), n('x2')), bottom: Math.max(n('y1'), n('y2')) };
    case 'path': return pathBounds(a.d ?? '');
    case 'polygon': case 'polyline': {
      const v = numbers(a.points ?? ''); if (!v.length) return null;
      const xs = v.filter((_, i) => i % 2 === 0), ys = v.filter((_, i) => i % 2 === 1);
      return { left: Math.min(...xs), top: Math.min(...ys), right: Math.max(...xs), bottom: Math.max(...ys) };
    }
    default: return null;
  }
}
/** Glyph bounds come from their actual primitive geometry, with source transforms. */
function subtreeBounds(node: ScientificSvgElement, matrix: ScientificSvgMatrix = identity): ScientificDiagramBounds | null {
  if (IGNORED_GEOMETRY.has(node.tag)) return null;
  const own = multiply(matrix, scientificSvgTransform(node.attributes.transform ?? ''));
  const shape = shapeBounds(node);
  let bounds = shape ? transformed(shape, own) : null;
  for (const child of node.children) if (typeof child !== 'string') bounds = union(bounds, subtreeBounds(child, own));
  return bounds;
}
function collectGeometry(document: ScientificDiagramDocument): void {
  const seen = new Set<string>();
  const v = document.viewBox;
  let sx = document.width / v.width, sy = document.height / v.height;
  let tx = -v.x * sx, ty = -v.y * sy;
  const aspect = document.tree.attributes.preserveAspectRatio ?? 'xMidYMid meet';
  if (aspect !== 'none') {
    const scale = aspect.includes('slice') ? Math.max(sx, sy) : Math.min(sx, sy);
    const dx = document.width - v.width * scale, dy = document.height - v.height * scale;
    tx = -v.x * scale + (aspect.includes('xMax') ? dx : aspect.includes('xMin') ? 0 : dx / 2);
    ty = -v.y * scale + (aspect.includes('YMax') ? dy : aspect.includes('YMin') ? 0 : dy / 2);
    sx = sy = scale;
  }
  const viewport: ScientificSvgMatrix = [sx, 0, 0, sy, tx, ty];
  function visit(node: ScientificSvgElement, parentMatrix: ScientificSvgMatrix, parentModule: ScientificDiagramGeometry | null) {
    if (IGNORED_GEOMETRY.has(node.tag)) return;
    const matrix = multiply(parentMatrix, scientificSvgTransform(node.attributes.transform ?? ''));
    const a = node.attributes;
    const moduleId = a['data-module-id'] ?? a['data-node-id'];
    const localId = a['data-local-node-id'];
    let context = parentModule;
    if (moduleId || localId) {
      const role = localId ? 'glyph' : 'module';
      if (localId && !parentModule) throw new Error('SVG 局部节点缺少所属模块。');
      const semanticId = localId ?? moduleId;
      if (!semanticId || !ID.test(semanticId)) throw new Error('SVG 语义节点 id 无效。');
      const nodeIds = sourceIds(a[localId ? 'data-local-node-ids' : 'data-node-ids'], localId ?? a['data-node-id'] ?? moduleId);
      if (localId && nodeIds.some(id => !parentModule!.nodeIds.includes(id))) throw new Error('SVG 局部节点不属于原模块。');
      const id = localId ? `glyph:${parentModule!.id}:${localId}` : moduleId!;
      if (seen.has(id)) throw new Error('SVG 语义节点 id 重复。'); seen.add(id);
      const directRect = role === 'module' ? node.children.find(child => typeof child !== 'string' && child.tag === 'rect') : undefined;
      let localBounds = typeof directRect === 'object' ? shapeBounds(directRect) : null;
      if (!localBounds && a['data-width'] && a['data-height']) {
        const width = numeric(a['data-width'], true), height = numeric(a['data-height'], true);
        localBounds = rectangle(-width / 2, -height / 2, width, height);
      }
      if (!localBounds) {
        // Exclude this group's transform because matrix already contains it.
        const untransformed = { ...node, attributes: { ...a, transform: '' } };
        localBounds = subtreeBounds(untransformed);
      }
      if (!localBounds) {
        // A path-only symbol still has a source-defined anchor; no frontend layout is invented.
        localBounds = rectangle(-1, -1, 2, 2);
      }
      const bounds = transformed(localBounds, matrix);
      const title = node.children.find(child => typeof child !== 'string' && child.tag === 'title');
      const item: ScientificDiagramGeometry = { id, moduleId: localId ? parentModule!.id : id, nodeIds, role,
        label: typeof title === 'object' ? textContent(title) : semanticId,
        x: (bounds.left + bounds.right) / 2, y: (bounds.top + bounds.bottom) / 2,
        width: bounds.right - bounds.left, height: bounds.bottom - bounds.top, bounds, localBounds, matrix, elementKey: node.key };
      if (role === 'module') { document.modules.push(item); context = item; }
      else document.localGlyphs.push(item);
    }
    if (a['data-source'] && a['data-target']) {
      const indices = (a['data-edge-indices'] ?? a['data-edge-index'] ?? '').split(/\s+/u).filter(Boolean).map(value => numeric(value));
      if (indices.some(index => !Number.isInteger(index) || index < 0)) throw new Error('SVG 连线映射无效。');
      const title = node.children.find(child => typeof child !== 'string' && child.tag === 'title');
      document.connections.push({ source: a['data-source'], target: a['data-target'], edgeIndices: indices,
        display: a['data-display'] === 'badge' ? 'badge' : 'arrow', label: typeof title === 'object' ? textContent(title) : '' });
    }
    for (const child of node.children) if (typeof child !== 'string') visit(child, matrix, context);
  }
  visit(document.tree, viewport, null);
  for (const item of document.modules) for (const id of item.nodeIds) {
    if (document.positions.has(id)) throw new Error('SVG 原节点归属重复。');
    document.positions.set(id, item);
  }
  for (const item of document.localGlyphs) for (const id of item.nodeIds) {
    const current = document.positions.get(id);
    if (!current || current.role === 'module' || item.width * item.height < current.width * current.height) document.positions.set(id, item);
  }
}

export interface ScientificDiagramSvgProps {
  document: ScientificDiagramDocument;
  onSelectNodeIds?: (nodeIds: string[], info: ScientificDiagramGeometry) => void;
  highlightNodeIds?: readonly string[];
  svgRef?: Ref<SVGSVGElement>;
  style?: CSSProperties;
  className?: string;
  interactive?: boolean;
}

/** SVG source never becomes HTML: validated attributes and text create React elements. */
export function ScientificDiagramSvg({ document, onSelectNodeIds, highlightNodeIds = [], svgRef, style, className, interactive = true }: ScientificDiagramSvgProps) {
  const instance = useId();
  const prefix = `scientific-${instance.replace(/[^A-Za-z0-9_-]/gu, '_')}`;
  const tree = useMemo(() => rewriteScientificDiagramIds(document, prefix), [document, prefix]);
  const units = useMemo(() => new Map([...document.modules, ...document.localGlyphs].map(item => [item.elementKey, item])), [document]);
  const selected = new Set(highlightNodeIds);
  function render(node: ScientificSvgElement): ReactNode {
    const props: Record<string, unknown> = { key: node.key };
    for (const [name, value] of Object.entries(node.attributes)) props[REACT_ATTRIBUTES[name] ?? name] = value;
    const safeStyle: Record<string, string | number> = {};
    for (const [name, value] of Object.entries(node.style ?? {})) {
      const reactName = REACT_ATTRIBUTES[name] ?? name;
      safeStyle[reactName] = NUMERIC.has(name) ? Number(value) : value;
    }
    if (Object.keys(safeStyle).length) props.style = safeStyle;
    const unit = units.get(node.key);
    const active = unit?.nodeIds.some(id => selected.has(id)) ?? false;
    if (unit && interactive) {
      const shortLabel = unit.label.split(/[\r\n]/u).find(line => line.trim())?.trim().slice(0, 80) ?? unit.id;
      props.role = 'button'; props.tabIndex = 0; props['aria-label'] = `查看${shortLabel}说明`;
      props['aria-pressed'] = active; props['data-scientific-selection-id'] = unit.id;
      props.style = { ...safeStyle, cursor: 'pointer' };
      props.onPointerDown = (event: { button: number; preventDefault(): void; stopPropagation(): void; currentTarget: SVGElement }) => {
        if (event.button !== 0) return;
        // Native focus can scroll an already visible nested SVG target. Keep
        // keyboard focus without moving the reader's current canvas viewport.
        event.preventDefault(); event.stopPropagation(); event.currentTarget.focus({ preventScroll: true });
      };
      props.onClick = (event: { stopPropagation(): void }) => { event.stopPropagation(); onSelectNodeIds?.([...unit.nodeIds], unit); };
      props.onKeyDown = (event: { key: string; preventDefault(): void; stopPropagation(): void }) => {
        if (event.key === 'Enter' || event.key === ' ' || event.key === 'Spacebar') {
          event.preventDefault(); event.stopPropagation(); onSelectNodeIds?.([...unit.nodeIds], unit);
        }
      };
    }
    const children = node.children.map(child => typeof child === 'string' ? child : render(child));
    if (unit && active) {
      const b = unit.localBounds;
      children.push(createElement('rect', { key: 'selection', 'data-diagram-selection': 'true', 'aria-hidden': true,
        x: b.left - 4, y: b.top - 4, width: b.right - b.left + 8, height: b.bottom - b.top + 8,
        rx: 7, fill: 'none', stroke: '#0F4D92', strokeWidth: 2.5,
        vectorEffect: 'non-scaling-stroke', pointerEvents: 'none' }));
    }
    if (node.key === document.tree.key) {
      // Interactive children need their own accessible targets; role=img hides them.
      props.role = interactive ? 'group' : 'img';
      props.ref = svgRef; if (className) props.className = className;
      if (style) props.style = { ...safeStyle, ...style };
    }
    return createElement(node.tag, props, ...children);
  }
  return render(tree);
}
