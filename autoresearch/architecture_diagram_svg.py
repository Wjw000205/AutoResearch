"""Deterministic, offline SVG presentation of validated architecture graphs.

This renderer does not invent scientific operators, approve architecture, or fetch resources.
The caller is responsible for the graph's immutable-source/read-receipt audit.
Only SVG elements created here are emitted; graph strings are always XML text.
"""
from __future__ import annotations

from dataclasses import dataclass
import heapq
import json
import math
import re
import unicodedata
import xml.etree.ElementTree as ET


_NS = 'http://www.w3.org/2000/svg'
_FONT = "Arial, Helvetica, 'Microsoft YaHei', 'Noto Sans CJK SC', sans-serif"
_KINDS = {'input', 'layer', 'operation', 'merge', 'decision', 'output', 'stop'}
_EDGE_COLOR = '#767676'
_BACKGROUND = '#ffffff'
_VISUALS = {'block', 'tensor', 'mlp', 'conv', 'spectral', 'clock', 'normalization', 'parameter', 'merge'}


def _string(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError('绘图文本必须是字符串。')
    # ElementTree escapes markup but does not reject XML 1.0 control characters.
    if any(not (character in '\t\n\r' or 0x20 <= ord(character) <= 0xD7FF
                or 0xE000 <= ord(character) <= 0xFFFD
                or 0x10000 <= ord(character) <= 0x10FFFF) for character in value):
        raise ValueError('绘图文本包含 XML 不支持的字符。')
    return value


def _units(character: str) -> float:
    if unicodedata.combining(character):
        return 0
    if unicodedata.east_asian_width(character) in {'W', 'F'} or character in 'MW@#%':
        return 1
    if character.isascii() and character.isupper():
        return .72
    return .55


def _wrap(text: str, units: float) -> tuple[str, ...]:
    result = []
    for paragraph in _string(text).splitlines() or ['']:
        line, used = '', 0.0
        # Scientific identifiers stay intact; Unicode prose can wrap by glyph.
        for character in re.findall(r'[A-Za-z0-9_]+(?:[.\-][A-Za-z0-9_]+)*|.',paragraph):
            cost = sum(_units(part) for part in character)
            if line and used + cost > units:
                result.append(line)
                line, used = '', 0.0
            line += character
            used += cost
        result.append(line)
    return tuple(result)


def _number(value: float) -> str:
    return f'{value:.2f}'.rstrip('0').rstrip('.')


def _element(parent: ET.Element, name: str, **attributes) -> ET.Element:
    return ET.SubElement(parent, name, {key.replace('_', '-'): str(value)
                                      for key, value in attributes.items()})


@dataclass
class _Node:
    data: dict
    label: tuple[str, ...]
    lines: tuple[str, ...]
    width: float
    height: float
    x: float = 0
    y: float = 0


def _nodes(graph: dict) -> list[_Node]:
    raw_nodes = graph['nodes']
    if not 2 <= len(raw_nodes) <= 32 or not 1 <= len(graph['edges']) <= 64:
        raise ValueError('绘图要求 2–32 个节点及 1–64 条连线。')
    result, identifiers, positions = [], set(), set()
    for node in raw_nodes:
        identifier = _string(node['id'])
        kind = node['kind']
        visual = node.get('visual', 'block')
        if visual not in _VISUALS:
            raise ValueError('绘图模块图案无效。')
        layer, order = node['layer'], node['order']
        if (kind not in _KINDS or type(layer) is not int or not 0 <= layer <= 16
                or type(order) is not int or not 0 <= order <= 8):
            raise ValueError('绘图节点类型或布局位置无效。')
        if identifier in identifiers or (layer, order) in positions:
            raise ValueError('绘图节点编号或布局位置重复。')
        identifiers.add(identifier)
        positions.add((layer, order))
        label = _wrap(node['label'], 14 if kind == 'decision' else 16)
        _string(node['detail'])
        annotations = node['lines']
        if len(annotations) > 3:
            raise ValueError('绘图节点文本超出架构图协议。')
        lines = tuple(part for text in annotations
                      for part in _wrap(text, 14 if kind == 'decision' else 17))
        content_height = len(label) * 20 + len(lines) * 18 + (12 if lines else 0)
        illustration_height = 108 if visual != 'block' and kind != 'decision' else 0
        # Diamonds enclose the same complete text block in their centre.
        width = 420 if kind == 'decision' and graph.get('panels') else 480 if kind == 'decision' else 300
        height = max(190, 2 * (content_height + 24)) if kind == 'decision' else max(140, content_height + illustration_height + 36)
        result.append(_Node(node, label, lines, width, height))
    return result


def _text_lines(parent: ET.Element, lines: tuple[str, ...], x: float, y: float,
                *, size: int = 15, spacing: int = 18, anchor: str = 'middle',
                weight: str = '400', color: str = '#4D4D4D', outlined: bool = False) -> None:
    attributes = {'text_anchor': anchor, 'font_family': _FONT, 'font_size': size,
                  'font_weight': weight, 'fill': color}
    if outlined:
        attributes.update(paint_order='stroke', stroke=_BACKGROUND,
                          stroke_width='4', stroke_linejoin='round')
    element = _element(parent, 'text', **attributes)
    for index, line in enumerate(lines):
        span = _element(element, 'tspan', x=_number(x), y=_number(y + index * spacing))
        span.text = line


def _path(points: list[tuple[float, float]]) -> str:
    return ' '.join(f'{"M" if index == 0 else "L"}{_number(x)} {_number(y)}'
                    for index, (x, y) in enumerate(points))


def _route(source: _Node, target: _Node, ranks: dict[int, int], vertical: bool,
           lane: float | None, gap: float, layer_sizes: dict[int, float],
           horizontal_direction: int = 1) -> tuple[str, float, float, str]:
    delta = ranks[target.data['layer']] - ranks[source.data['layer']]
    direction = (1 if delta >= 0 else -1) * (1 if vertical else horizontal_direction)
    if delta == 0:
        # Same-layer branches use the gap beside this layer, including sibling
        # jumps, so their path cannot pass through an intermediate sibling.
        if vertical:
            corridor = source.y + layer_sizes[source.data['layer']] / 2 + gap / 2
            points = [(source.x, source.y + source.height / 2), (source.x, corridor),
                      (target.x, corridor), (target.x, target.y + target.height / 2 + 7)]
            return _path(points), (source.x + target.x) / 2, corridor - 8, 'middle'
        corridor = source.x + horizontal_direction * (layer_sizes[source.data['layer']] / 2 + gap / 2)
        points = [(source.x + horizontal_direction * source.width / 2, source.y), (corridor, source.y),
                  (corridor, target.y), (target.x + horizontal_direction * (target.width / 2 + 7), target.y)]
        return _path(points), corridor, (source.y + target.y) / 2, 'middle'
    if vertical:
        x1, x2 = source.x, target.x
        y1 = source.y + direction * source.height / 2
        y2 = target.y - direction * (target.height / 2 + 7)
    else:
        x1 = source.x + direction * source.width / 2
        x2 = target.x - direction * (target.width / 2 + 7)
        y1, y2 = source.y, target.y
    if lane is not None:
        if vertical:
            exit_y = source.y + direction * (layer_sizes[source.data['layer']] / 2 + 22)
            entry_y = target.y - direction * (layer_sizes[target.data['layer']] / 2 + 22)
            points = [(x1, y1), (x1, exit_y), (lane, exit_y),
                      (lane, entry_y), (x2, entry_y), (x2, y2)]
            return _path(points), lane + 8, (y1 + y2) / 2, 'start'
        exit_x = source.x + direction * (layer_sizes[source.data['layer']] / 2 + 22)
        entry_x = target.x - direction * (layer_sizes[target.data['layer']] / 2 + 22)
        points = [(x1, y1), (exit_x, y1), (exit_x, lane),
                  (entry_x, lane), (entry_x, y2), (x2, y2)]
        return _path(points), (x1 + x2) / 2, lane - 8, 'middle'
    if vertical:
        middle = (y1 + y2) / 2
        path = f'M{_number(x1)} {_number(y1)} C{_number(x1)} {_number(middle)} {_number(x2)} {_number(middle)} {_number(x2)} {_number(y2)}'
    else:
        middle = (x1 + x2) / 2
        path = f'M{_number(x1)} {_number(y1)} C{_number(middle)} {_number(y1)} {_number(middle)} {_number(y2)} {_number(x2)} {_number(y2)}'
    return path, (x1 + x2) / 2, (y1 + y2) / 2 - 8, 'middle'


def _folded_layout(nodes: list[_Node], edges: list[dict], labels: list[tuple[str, ...]],
                   title: str, layers: list[int], groups: dict[int, list[_Node]],
                   ranks: dict[int, int], layer_sizes: dict[int, float], gap: float):
    """Fold long networks into six-layer rows; edges retain their true direction.

    Alternating rows read left-to-right, then right-to-left. Cross-row edges
    use dedicated exterior gutters and the vacant spaces between columns.
    No semantic node, edge, layer or branch is added or combined.
    """
    row_layers = [layers[start:start + 6] for start in range(0, len(layers), 6)]
    row_of = {layer: index for index, row in enumerate(row_layers) for layer in row}
    by_id = {node.data['id']: node for node in nodes}
    incoming = {row: [] for row in range(len(row_layers))}
    outgoing = {row: [] for row in incoming}
    internal = {row: [] for row in incoming}
    cross = []
    for index, edge in enumerate(edges):
        source, target = by_id[edge['source']], by_id[edge['target']]
        source_row, target_row = row_of[source.data['layer']], row_of[target.data['layer']]
        if source_row != target_row:
            cross.append(index)
            outgoing[source_row].append(index)
            incoming[target_row].append(index)
        else:
            delta = ranks[target.data['layer']] - ranks[source.data['layer']]
            if delta > 1 or delta < 0:
                internal[source_row].append(index)
    left_count = sum(row_of[by_id[edges[index]['source']].data['layer']] % 2 for index in cross)
    right_count = len(cross) - left_count
    left_margin, right_margin = 40 + left_count * 18, 40 + right_count * 18
    row_width = max(sum(layer_sizes[layer] for layer in row) + (len(row) - 1) * gap for row in row_layers)
    width = left_margin + row_width + right_margin
    heading = _wrap(title, max(20, (width - 80) / 20))
    header_height = 40 + len(heading) * 25 + 24
    cursor = header_height + 36
    lanes, incoming_lanes, outgoing_lanes = {}, {}, {}
    for row, row_members in enumerate(row_layers):
        for index in incoming[row]:
            cursor += 18
            incoming_lanes[index] = cursor
        for index in internal[row]:
            cursor += max(22, len(labels[index]) * 17 + 18)
            lanes[index] = cursor
        node_top = cursor + 36
        members = [node for layer in row_members for node in groups[layer]]
        node_height = max(node.height for node in members)
        max_order = max(node.data['order'] for node in members)
        branch_pitch = node_height + 60
        direction = 1 if row % 2 == 0 else -1
        primary = left_margin if direction > 0 else left_margin + row_width
        for layer in row_members:
            centre = primary + direction * layer_sizes[layer] / 2
            for node in groups[layer]:
                node.x = centre
                node.y = node_top + node_height / 2 + node.data['order'] * branch_pitch
            primary += direction * (layer_sizes[layer] + gap)
        cursor = node_top + node_height + max_order * branch_pitch
        for index in outgoing[row]:
            cursor += max(24, len(labels[index]) * 17 + 18)
            outgoing_lanes[index] = cursor
        cursor += 70
    cross_routes, left_slot, right_slot = {}, 0, 0
    for index in cross:
        edge = edges[index]
        source, target = by_id[edge['source']], by_id[edge['target']]
        source_direction = 1 if row_of[source.data['layer']] % 2 == 0 else -1
        target_direction = 1 if row_of[target.data['layer']] % 2 == 0 else -1
        if source_direction > 0:
            spine = left_margin + row_width + 22 + right_slot * 18
            right_slot += 1
        else:
            spine = left_margin - 22 - left_slot * 18
            left_slot += 1
        x1 = source.x + source_direction * source.width / 2
        x2 = target.x - target_direction * (target.width / 2 + 7)
        exit_x = source.x + source_direction * (layer_sizes[source.data['layer']] / 2 + 22)
        entry_x = target.x - target_direction * (layer_sizes[target.data['layer']] / 2 + 22)
        bottom, top = outgoing_lanes[index], incoming_lanes[index]
        points = [(x1, source.y), (exit_x, source.y), (exit_x, bottom), (spine, bottom),
                  (spine, top), (entry_x, top), (entry_x, target.y), (x2, target.y)]
        cross_routes[index] = (_path(points), max(110, min(width - 110, (exit_x + spine) / 2)), bottom - 8, 'middle')
    return width, cursor, heading, lanes, cross_routes, row_of


def _panel_layout(graph: dict, nodes: list[_Node]):
    definitions = graph['panels']
    if not 1 <= len(definitions) <= 8:
        raise ValueError('科研绘图区需要 1–8 个分区。')
    known = {_string(panel['id']): panel for panel in definitions}
    if len(known) != len(definitions):
        raise ValueError('科研绘图分区编号重复。')
    groups = {identifier: [] for identifier in known}
    ungrouped = []
    for node in nodes:
        identifier = node.data.get('panel_id')
        if identifier is None:
            ungrouped.append(node)
        elif identifier not in known:
            raise ValueError('节点绘图区不存在。')
        else:
            groups[identifier].append(node)
    items = [(identifier, _string(definition['title']), groups[identifier])
             for identifier, definition in known.items() if groups[identifier]]
    if ungrouped:
        items.append(('', '其他模块', ungrouped))
    columns = min(3, len(items))
    plans = []
    for identifier, title, members in items:
        members = sorted(members, key=lambda node: (node.data['layer'], node.data['order']))
        count = min(2, len(members))
        cell_width = max(node.width for node in members)
        rows = [members[start:start + count] for start in range(0, len(members), count)]
        heights = [max(node.height for node in row) for row in rows]
        plans.append({'id': identifier, 'title': title, 'nodes': members, 'rows': rows,
                      'heights': heights, 'width': count * cell_width + (count - 1) * 140 + 80,
                      'height': sum(heights) + (len(rows) - 1) * 110 + 90,
                      'cell_width': cell_width, 'red': all(node.data['kind'] == 'stop' for node in members)})
    column_width = max(plan['width'] for plan in plans)
    width = columns * column_width + (columns - 1) * 100 + 100
    heading = _wrap(graph['title'], max(20, (width - 80) / 20))
    header_height = 64 + len(heading) * 25 + 28
    bottoms = [header_height + 36] * columns
    for plan in plans:
        column = min(range(columns), key=lambda index: (bottoms[index], index))
        plan['x'] = 50 + column * (column_width + 100)
        plan['y'] = bottoms[column]
        plan['width'] = column_width
        y = plan['y'] + 60
        for row_index, (members, height) in enumerate(zip(plan['rows'], plan['heights'])):
            direction = 1 if row_index % 2 == 0 else -1
            count = len(plan['rows'][0])
            for index, node in enumerate(members):
                slot = index if direction > 0 else count - 1 - index
                node.x = plan['x'] + plan['width'] / 2 + (slot - (count - 1) / 2) * (plan['cell_width'] + 140)
                node.y = y + height / 2
            y += height + 110
        bottoms[column] += plan['height'] + 80
    return width, max(bottoms) - 30, heading, plans


def _illustration(group: ET.Element, node: _Node, y: float) -> None:
    """Category glyphs are sampled illustrations, never inferred scientific layers."""
    visual = node.data.get('visual', 'block')
    if visual == 'block' or node.data['kind'] == 'decision':
        return
    picture = _element(group, 'g', data_visual=visual, aria_hidden='true', transform=f'translate(0 {_number(y)})')
    _element(picture, 'title').text = '模块类型示意；准确层数、维度及约束以节点文字为准。'
    blue, pale, green = '#3775BA', '#DCEAF8', '#AADCA9'
    if visual == 'tensor':
        for offset in (12, 6, 0):
            _element(picture, 'rect', x=-58 + offset, y=-30 - offset, width='116', height='64', rx='2',
                     fill=pale if offset else '#ffffff', stroke=blue, stroke_width='1.5')
        for x in (-29, 0, 29):
            _element(picture, 'line', x1=x, y1='-30', x2=x, y2='34', stroke=blue, stroke_width='.8')
        for line_y in (-9, 12):
            _element(picture, 'line', x1='-58', y1=line_y, x2='58', y2=line_y, stroke=blue, stroke_width='.8')
    elif visual == 'mlp':
        chains = [match.group(0).replace(' ', '').split('→') for line in node.data['lines']
                  for match in re.finditer(r'\d+(?:\s*→\s*\d+)+', line)]
        dimensions = max(chains, key=len) if chains else []
        dimensions = dimensions[:4]
        alternatives=[chain[-1] for chain in chains if len(chain)==len(dimensions)
                      and chain[:-1]==dimensions[:-1]]
        if dimensions and len(set(alternatives))>1:
            dimensions[-1]='/'.join(dict.fromkeys(alternatives))
        count = len(dimensions) or 3
        xs = [(index - (count - 1) / 2) * (150 / max(1, count - 1)) for index in range(count)]
        circles = [[0] if dimensions and dimensions[index] == '1' else [-22, 0, 22]
                   for index in range(count)]
        for column in range(count - 1):
            for start_y in circles[column]:
                for end_y in circles[column + 1]:
                    _element(picture, 'line', x1=_number(xs[column]), y1=start_y,
                             x2=_number(xs[column + 1]), y2=end_y, stroke='#99B7D6', stroke_width='.8')
        for column, x in enumerate(xs):
            for centre_y in circles[column]:
                _element(picture, 'circle', cx=_number(x), cy=centre_y, r='4.3', fill='#ffffff',
                         stroke=blue, stroke_width='1.4')
            if dimensions:
                if not node.data.get('_hide_dimension_labels'):
                    _text_lines(picture, (dimensions[column],), x, -37, size=12, color='#0F4D92')
                if any(int(value)>3 for value in dimensions[column].split('/')):
                    _text_lines(picture, ('⋮',), x, 41, size=17, color=blue)
        residual = next((match for line in node.data['lines']
                         if (match := re.search(r'残差[^\d]*(\d+)\s*→\s*(\d+)', line))), None)
        if (residual and dimensions and residual.group(1) == dimensions[0]
                and residual.group(2) == dimensions[-1] and len(dimensions) >= 3):
            left, right = _number(xs[0]), _number(xs[-1])
            _element(picture, 'path', data_mlp_residual=f'{dimensions[0]}→{dimensions[-1]}',
                     d=f'M{left} 27 V49 Q{left} 52 {_number(xs[0]+4)} 52 H{_number(xs[-1]-4)} Q{right} 52 {right} 49 V28',
                     fill='none', stroke=blue, stroke_width='1.3', stroke_dasharray='4 3',
                     marker_end='url(#diagram-arrow)')
    elif visual == 'conv':
        for offset in (16, 8, 0):
            _element(picture, 'rect', x=-58 + offset, y=-30 - offset, width='88', height='60', rx='2',
                     fill='#ffffff' if offset == 0 else pale, stroke=blue, stroke_width='1.4')
        for x in (-36, -14, 8):
            _element(picture, 'line', x1=x, y1='-30', x2=x, y2='30', stroke='#99B7D6', stroke_width='.7')
        for line_y in (-10, 10):
            _element(picture, 'line', x1='-58', y1=line_y, x2='30', y2=line_y, stroke='#99B7D6', stroke_width='.7')
        # A stated kernel size may determine the grid; no default forward kernel
        # or activation is invented from initialisation or convention.
        stated = next((re.search(r'(?:核|kernel)\s*[:=：]?\s*(\d+)', line, re.I)
                       for line in node.data['lines'] if re.search(r'(?:核|kernel)\s*[:=：]?\s*(\d+)', line, re.I)), None)
        kernel = min(9, int(stated.group(1))) if stated else 0
        _element(picture, 'rect', x='-9', y='-10', width='28', height='28', fill=green, stroke='#527B54', stroke_width='1.3')
        for cell in range(1, kernel):
            shift = cell * 28 / kernel
            _element(picture, 'line', x1=_number(-9 + shift), y1='-10', x2=_number(-9 + shift), y2='18', stroke='#527B54', stroke_width='.65')
            _element(picture, 'line', x1='-9', y1=_number(-10 + shift), x2='19', y2=_number(-10 + shift), stroke='#527B54', stroke_width='.65')
        _element(picture, 'path', d='M42 1 L69 1', fill='none', stroke=blue, stroke_width='1.5', marker_end='url(#diagram-arrow)')
        _element(picture, 'rect', x='76', y='-21', width='27', height='44', fill=pale, stroke=blue, stroke_width='1.4')
    elif visual == 'spectral':
        _element(picture, 'path', d='M-87 30 H87 M-81 30 V-33', fill='none', stroke='#767676', stroke_width='1.1')
        for index, height in enumerate((18, 35, 56, 42, 27, 16, 24, 10)):
            x = -68 + index * 18
            _element(picture, 'line', x1=x, y1='28', x2=x, y2=28-height, stroke=blue, stroke_width='4')
        _text_lines(picture, ('⋯',), 91, 30, size=20, color=blue)
    elif visual == 'clock':
        _element(picture, 'circle', cx='-30', cy='0', r='31', fill='#ffffff', stroke=blue, stroke_width='1.6')
        for angle in range(0, 360, 45):
            radians = math.radians(angle)
            _element(picture, 'line', x1=_number(-30 + 26*math.cos(radians)), y1=_number(26*math.sin(radians)),
                     x2=_number(-30 + 30*math.cos(radians)), y2=_number(30*math.sin(radians)), stroke=blue, stroke_width='1')
        _element(picture, 'path', d='M-30 0 L-30 -22 M-30 0 L-11 10', fill='none', stroke=blue, stroke_width='1.7')
        wave = [(17 + index * 2, math.sin(index * math.pi / 10) * 17) for index in range(41)]
        _element(picture, 'path', d=_path(wave), fill='none', stroke=blue, stroke_width='1.6')
    elif visual == 'normalization':
        inverse=bool(re.search(r'逆|inverse',node.data['label'],re.I))
        _element(picture, 'rect', x='-78', y='-26', width='48', height='52', rx='4', fill=pale, stroke=blue, stroke_width='1.3')
        _text_lines(picture, ('x̂' if inverse else 'x',), -54, 5, size=20, color=blue)
        _element(picture, 'path', d='M-23 0 H23', stroke=blue, stroke_width='1.5', fill='none', marker_end='url(#diagram-arrow)')
        _element(picture, 'rect', x='30', y='-26', width='48', height='52', rx='4', fill='#ffffff', stroke=blue, stroke_width='1.3')
        _text_lines(picture, ('x' if inverse else 'x̂',), 54, 5, size=20, color=blue)
    elif visual == 'parameter':
        all_text = ' '.join([node.data['label'], *node.data['lines']])
        symbol = 'ϕ' if 'ϕ' in all_text else 'ρ' if 'ρ' in all_text else 'θ'
        text = 'D = 20 或 29' if '20' in all_text and '29' in all_text else symbol
        _element(picture, 'rect', x='-85', y='-28', width='170', height='56', rx='8', fill='#ffffff', stroke=blue, stroke_width='1.5')
        _text_lines(picture, (text,), 0, 7, size=22, color='#0F4D92')
    elif visual == 'merge':
        _element(picture, 'circle', cx='0', cy='0', r='24', fill='#ffffff', stroke=blue, stroke_width='1.5')
        _element(picture, 'path', d='M-78 -24 L-24 -10 M-78 24 L-24 10 M24 0 H79', fill='none', stroke=blue, stroke_width='1.5', marker_end='url(#diagram-arrow)')
        _element(picture, 'circle', cx='0', cy='0', r='4', fill=blue)


def _panel_routes(nodes: list[_Node], edges: list[dict], plans: list[dict], width: float, height: float):
    """Orthogonal routes avoid module rectangles and panel title bands."""
    by_id = {node.data['id']: node for node in nodes}
    obstacles = [(node.x-node.width/2-6, node.y-node.height/2-6,
                  node.x+node.width/2+6, node.y+node.height/2+6) for node in nodes]
    obstacles += [(plan['x']-2, plan['y']-2, plan['x']+plan['width']+2, plan['y']+42) for plan in plans]
    ports = []
    for edge in edges:
        source, target = by_id[edge['source']], by_id[edge['target']]
        dx, dy = target.x-source.x, target.y-source.y
        if abs(dx) >= abs(dy):
            direction = 1 if dx >= 0 else -1
            start = (source.x+direction*(source.width/2+12), source.y)
            end = (target.x-direction*(target.width/2+12), target.y)
            first = (source.x+direction*source.width/2, source.y)
            last = (target.x-direction*(target.width/2+7), target.y)
        else:
            direction = 1 if dy >= 0 else -1
            start = (source.x, source.y+direction*(source.height/2+12))
            end = (target.x, target.y-direction*(target.height/2+12))
            first = (source.x, source.y+direction*source.height/2)
            last = (target.x, target.y-direction*(target.height/2+7))
        ports.append((first, start, end, last))
    xs = sorted({20, width-20, *(x for lo, _, hi, _ in obstacles for x in (lo-2, hi+2)),
                 *(point[0] for port in ports for point in port[1:3])})
    ys = sorted({120, height-20, *(y for _, lo, _, hi in obstacles for y in (lo-2, hi+2)),
                 *(point[1] for port in ports for point in port[1:3])})
    x_index, y_index = {x: index for index, x in enumerate(xs)}, {y: index for index, y in enumerate(ys)}
    blocked, used = {}, {}
    def free(a, b):
        key = tuple(sorted((a, b)))
        if key not in blocked:
            x1, y1, x2, y2 = xs[a[0]], ys[a[1]], xs[b[0]], ys[b[1]]
            blocked[key] = any((left < x1 < right and min(y1, y2) < bottom and max(y1, y2) > top)
                               if x1 == x2 else (top < y1 < bottom and min(x1, x2) < right and max(x1, x2) > left)
                               for left, top, right, bottom in obstacles)
        return not blocked[key]
    result = {}
    for index, (first, start, end, last) in enumerate(ports):
        start_cell, end_cell = (x_index[start[0]], y_index[start[1]]), (x_index[end[0]], y_index[end[1]])
        initial = (*start_cell, 0)
        pending = [(0, 0, initial)]
        costs, previous, serial = {initial: 0}, {}, 0
        goal = None
        while pending:
            _, cost, state = heapq.heappop(pending)
            if costs.get(state) != cost:
                continue
            x, y, heading = state
            if (x, y) == end_cell:
                goal = state
                break
            for nx, ny, axis in ((x-1, y, 1), (x+1, y, 1), (x, y-1, 2), (x, y+1, 2)):
                if not 0 <= nx < len(xs) or not 0 <= ny < len(ys) or not free((x, y), (nx, ny)):
                    continue
                key = tuple(sorted(((x, y), (nx, ny))))
                step = abs(xs[nx]-xs[x]) + abs(ys[ny]-ys[y]) + (22 if heading and axis != heading else 0) + used.get(key, 0)*18
                next_state, next_cost = (nx, ny, axis), cost+step
                if next_cost >= costs.get(next_state, math.inf):
                    continue
                costs[next_state], previous[next_state] = next_cost, state
                serial += 1
                heuristic = abs(xs[nx]-end[0]) + abs(ys[ny]-end[1])
                heapq.heappush(pending, (next_cost+heuristic, next_cost, next_state))
        if goal is None:
            raise ValueError('科研绘图未找到不遮挡模块的连线路径。')
        cells = []
        while goal != initial:
            cells.append(goal[:2])
            goal = previous[goal]
        cells.append(start_cell)
        cells.reverse()
        for a, b in zip(cells, cells[1:]):
            key = tuple(sorted((a, b)))
            used[key] = used.get(key, 0) + 1
        points = [first, *((xs[x], ys[y]) for x, y in cells), last]
        simplified = []
        for point in points:
            if simplified and point == simplified[-1]:
                continue
            while len(simplified) > 1 and ((simplified[-2][0] == simplified[-1][0] == point[0])
                                         or (simplified[-2][1] == simplified[-1][1] == point[1])):
                simplified.pop()
            simplified.append(point)
        # Use the longest free segment for an editable, outlined edge label.
        segments = sorted(zip(simplified, simplified[1:]),
                          key=lambda pair: abs(pair[0][0]-pair[1][0])+abs(pair[0][1]-pair[1][1]), reverse=True)
        a, b = segments[0]
        result[index] = (_path(simplified), (a[0]+b[0])/2, (a[1]+b[1])/2-8, 'middle')
    return result


def validate_publication_scene(graph: dict, scene: dict) -> dict:
    """Validate exact source-node and cross-module edge mappings, without I/O."""
    if not isinstance(scene, dict):
        raise ValueError('publication scene 必须是对象。')
    if type(scene.get('version')) is not int or scene['version'] != 1:
        raise ValueError('publication scene version 无效。')
    modules, connections, insets = scene.get('modules', []), scene.get('connections', []), scene.get('insets', [])
    if not all(isinstance(value, list) for value in (modules, connections, insets)) or not 2 <= len(modules) <= 32 or len(connections) > 64 or len(insets) > 8:
        raise ValueError('publication scene 模块、连接或 inset 数量无效。')
    identifiers, membership = set(), {}
    original_nodes = {node['id'] for node in graph['nodes']}
    for module in modules:
        identifier = _string(module['id'])
        if not identifier or identifier in identifiers or module['visual'] not in _VISUALS:
            raise ValueError('publication scene 模块编号重复或图案无效。')
        _string(module['title'])
        identifiers.add(identifier)
        if not module['node_ids']:
            raise ValueError('publication scene 模块必须映射原节点。')
        for node_id in module['node_ids']:
            if node_id not in original_nodes or node_id in membership:
                raise ValueError('publication scene 节点映射未知、重复或归属不唯一。')
            membership[node_id] = identifier
        for axis in ('row', 'column'):
            if axis in module and (type(module[axis]) is not int or not 0 <= module[axis] <= 8):
                raise ValueError('publication scene 布局坐标无效。')
    if set(membership) != original_nodes:
        raise ValueError('publication scene 未完整覆盖原节点。')
    cross_edges = {index for index, edge in enumerate(graph['edges'])
                   if membership[edge['source']] != membership[edge['target']]}
    covered = set()
    for connection in connections:
        source, target = connection['source'], connection['target']
        if source not in identifiers or target not in identifiers or source == target or connection['display'] not in {'arrow', 'badge'}:
            raise ValueError('publication scene 连接端点、方向或显示方式无效。')
        _string(connection['label'])
        if not connection['edge_indices']:
            raise ValueError('publication scene 连接必须映射原连线。')
        for index in connection['edge_indices']:
            if type(index) is not int or not 0 <= index < len(graph['edges']):
                raise ValueError('publication scene edge index 未知。')
            edge = graph['edges'][index]
            if index not in cross_edges or index in covered:
                raise ValueError('publication scene 内部边或重复边不能作为跨模块连线。')
            if membership[edge['source']] != source or membership[edge['target']] != target:
                raise ValueError('publication scene 连接端点方向与原连线不一致。')
            covered.add(index)
    if covered != cross_edges:
        raise ValueError('publication scene 未完整覆盖跨模块原连线。')
    inset_members = set()
    for inset in insets:
        _string(inset['title'])
        if not inset['module_ids']:
            raise ValueError('publication scene inset 不能为空。')
        for identifier in inset['module_ids']:
            if identifier not in identifiers or identifier in inset_members:
                raise ValueError('publication scene inset 模块引用未知或重复。')
            inset_members.add(identifier)
    return {'original_node_count': len(original_nodes), 'original_edge_count': len(graph['edges']),
            'module_count': len(modules), 'cross_edge_count': len(cross_edges),
            'internal_edges': [{'module_id': module['id'], 'edge_indices': [index for index, edge in enumerate(graph['edges'])
                if membership[edge['source']] == membership[edge['target']] == module['id']]} for module in modules]}


def _visual(node: dict) -> str:
    if 'visual' in node:
        return node['visual']
    text = ' '.join([node['label'], *node['lines']])
    if node['kind'] in {'input', 'output'}:
        return 'tensor'
    if node['kind'] in {'decision', 'stop'}:
        return 'block'
    if '卷积' in node['label'] and node['kind']=='layer' or re.search(r'\bCNN\b|kernel|dilation', text, re.I) and node['kind']=='layer':
        return 'conv'
    if node['kind'] == 'merge':
        return 'merge'
    if re.search(r'fft|频谱|复谱', text, re.I):
        return 'spectral'
    if re.search(r'RevIN|归一化', text, re.I):
        return 'normalization'
    if '谐波' in text:
        return 'clock'
    if 'ρ' in node['label'] or 'ϕ' in node['label']:
        return 'parameter'
    return 'mlp' if node['kind'] == 'layer' and re.search(r'\d+\s*→\s*\d+',text) else 'block'


def compile_publication_scene(graph: dict) -> dict:
    """Deterministically collapse source topology into a generic paper overview.

    Explicit panels are respected. Legacy graphs use source-defined series and
    simple parallel regions; IDs and research-specific module names are never
    hard coded. Every original edge remains in a connection or internal mapping.
    """
    _nodes(graph)
    by_id = {node['id']: node for node in graph['nodes']}
    incoming = {identifier: set() for identifier in by_id}
    outgoing = {identifier: set() for identifier in by_id}
    for edge in graph['edges']:
        outgoing[edge['source']].add(edge['target'])
        incoming[edge['target']].add(edge['source'])
    references = {index for index, edge in enumerate(graph['edges'])
                  if by_id[edge['target']]['layer']-by_id[edge['source']]['layer'] > 2
                  and len(outgoing[edge['source']]) > 1 and len(incoming[edge['target']]) > 1
                  and re.search(r'复用|共享|参数|统计量|shared|parameter|statistics',edge['label'],re.I)}
    groups = {identifier: [identifier] for identifier in by_id}
    panel_titles = {}
    if graph.get('panels'):
        groups = {}
        for panel in graph['panels']:
            members = [node['id'] for node in graph['nodes'] if node.get('panel_id') == panel['id']]
            if members:
                key='panel:'+panel['id']
                groups[key] = members
                panel_titles[key] = panel['title']
        assigned = {identifier for members in groups.values() for identifier in members}
        groups.update({'node:'+identifier: [identifier] for identifier in by_id if identifier not in assigned})
        if len(groups)==1:
            # A single semantic panel is legal. Preserve it as two traceable
            # macro regions instead of violating the scene's minimum size.
            key=next(iter(groups))
            members=groups.pop(key)
            ordered_members=sorted(members,key=lambda node:(by_id[node]['layer'],by_id[node]['order']))
            groups['panel:first']=[ordered_members[0]]
            groups['panel:rest']=ordered_members[1:]
            panel_titles['panel:rest']=panel_titles.get(key,graph['title'])
    else:
        def adjacency():
            membership = {node: group for group, members in groups.items() for node in members}
            ins, outs = {group: set() for group in groups}, {group: set() for group in groups}
            for index, edge in enumerate(graph['edges']):
                source, target = membership[edge['source']], membership[edge['target']]
                if (index not in references and source != target
                        and by_id[edge['target']]['kind']!='stop'):
                    outs[source].add(target); ins[target].add(source)
            return ins, outs
        def protected(group):
            return any(by_id[node]['kind'] in {'decision', 'stop'} for node in groups[group])
        def merge(keys):
            keeper = min(keys, key=lambda key: min(list(by_id).index(node) for node in groups[key]))
            members = [node for key in keys for node in groups[key]]
            groups[keeper] = sorted(members, key=lambda node: (by_id[node]['layer'], by_id[node]['order']))
            for key in keys:
                if key != keeper:
                    del groups[key]
        while len(groups) > 2:
            ins, outs = adjacency()
            candidates = []
            keys = list(groups)
            # Shared real/imaginary (or other parallel) inputs with the same
            # predecessor and successor can become one local module illustration.
            for i, left in enumerate(keys):
                for right in keys[i+1:]:
                    if (not protected(left) and not protected(right) and ins[left] == ins[right]
                            and outs[left] == outs[right] and len(ins[left]) == len(outs[left]) == 1):
                        candidates.append([left, right])
            for source in keys:
                if protected(source):
                    continue
                if len(outs[source]) == 1:
                    target = next(iter(outs[source]))
                    if len(ins[target]) == 1 and not protected(target):
                        candidates.append([source, target])
                if len(outs[source]) == 2:
                    left, right = sorted(outs[source])
                    joins = outs[left] & outs[right]
                    if right in outs[left]:
                        joins.add(right)
                    if left in outs[right]:
                        joins.add(left)
                    for join in sorted(joins):
                        region = list(dict.fromkeys([source, left, right, join]))
                        if (not any(protected(group) for group in region)
                                and all(ins[group] <= set(region) for group in region if group != source)
                                and all(outs[group] <= set(region) for group in region if group != join)):
                            candidates.append(region)
            selected = next((keys for keys in candidates if sum(len(groups[key]) for key in keys) <= 8
                             and len(groups)-len(keys)+1 >= 2), None)
            if selected is None:
                break
            merge(selected)
    modules, membership = [], {}
    ordered = sorted(groups, key=lambda group: min((by_id[node]['layer'], by_id[node]['order']) for node in groups[group]))
    for index, key in enumerate(ordered):
        members = sorted(groups[key], key=lambda node: (by_id[node]['layer'], by_id[node]['order']))
        identifier = f'module{index+1}'
        for node in members:
            membership[node] = identifier
        visuals = [_visual(by_id[node]) for node in members]
        visual = next((name for name in ('conv', 'mlp', 'clock', 'tensor', 'spectral', 'normalization', 'parameter', 'merge') if name in visuals), 'block')
        title = panel_titles.get(key) or (by_id[members[0]]['label'] if len(members)==1 else f'{by_id[members[0]]["label"]} → {by_id[members[-1]]["label"]}')
        modules.append({'id': identifier, 'title': title, 'node_ids': members, 'visual': visual})
    connections_by_pair = {}
    for index, edge in enumerate(graph['edges']):
        source, target = membership[edge['source']], membership[edge['target']]
        if source == target:
            continue
        display = 'badge' if index in references else 'arrow'
        key = source, target, display
        connection = connections_by_pair.setdefault(key, {'source':source,'target':target,'edge_indices':[],'display':display,'label':edge['label']})
        connection['edge_indices'].append(index)
    insets = []
    for module in modules:
        if all(by_id[node]['kind'] == 'stop' for node in module['node_ids']):
            insets.append({'title': module['title'], 'module_ids': [module['id']]})
    scene = {'version':1,'modules':modules,'connections':list(connections_by_pair.values()),'insets':insets}
    validate_publication_scene(graph, scene)
    return scene


def _dimension_chains(node: dict) -> list[list[str]]:
    return [match.group(0).replace(' ','').split('→') for line in node['lines']
            for match in re.finditer(r'\d+(?:\s*→\s*\d+)+',line)]


def _publication_glyphs(originals: list[dict], graph: dict) -> list[dict]:
    """Compile local source operators; merge only compatible connected layers."""
    members={node['id'] for node in originals}
    incoming={identifier:set() for identifier in members}
    outgoing={identifier:set() for identifier in members}
    for edge in graph['edges']:
        if edge['source'] in members and edge['target'] in members:
            outgoing[edge['source']].add(edge['target']); incoming[edge['target']].add(edge['source'])
    by_id={node['id']:node for node in originals}
    full_incoming={identifier:{edge['source'] for edge in graph['edges'] if edge['target']==identifier} for identifier in members}
    full_outgoing={identifier:{edge['target'] for edge in graph['edges'] if edge['source']==identifier} for identifier in members}
    used=set(); glyphs=[]
    residuals=[node for node in originals if re.search(r'残差|residual',node['label'],re.I)
               and _dimension_chains(node) and node['kind']=='layer']
    for original in originals:
        if original['id'] in used or original in residuals or original['kind'] in {'decision','stop'}:
            continue
        visual=_visual(original)
        glyph={**original,'visual':visual,'_glyph_node_ids':[original['id']]}
        used.add(original['id'])
        if visual=='mlp' and _dimension_chains(original):
            chain=max(_dimension_chains(original),key=len)
            combined=list(chain); current=original['id']
            while len(outgoing[current])==1 and len(combined)<4:
                target=next(iter(outgoing[current])); following=by_id[target]
                chains=_dimension_chains(following)
                if (target in used or following in residuals or _visual(following)!='mlp'
                        or len(incoming[target])!=1 or not chains):
                    break
                next_chain=max(chains,key=len)
                if combined[-1]!=next_chain[0]:
                    break
                combined.extend(next_chain[1:]); current=target; used.add(target)
                glyph['_glyph_node_ids'].append(target)
            annotations=['→'.join(combined)]
            for alternative in _dimension_chains(original):
                if len(alternative)==len(combined) and alternative[:-1]==combined[:-1]:
                    annotations.append('→'.join(alternative))
            for node in residuals:
                dimensions=max(_dimension_chains(node),key=len)
                if dimensions[0]==combined[0] and dimensions[-1]==combined[-1]:
                    annotations.append('残差：'+dimensions[0]+'→'+dimensions[-1])
            annotations.extend(line for line in original['lines'] if re.search(r'残差[^\d]*\d+\s*→\s*\d+',line))
            glyph['lines']=list(dict.fromkeys(annotations))
        if visual=='block' and original['kind']=='operation':
            twins=[node for node in originals if node['id'] not in used and node['kind']=='operation'
                   and _visual(node)=='block' and full_incoming[node['id']]==full_incoming[original['id']]
                   and full_outgoing[node['id']]==full_outgoing[original['id']]
                   and full_incoming[node['id']] and full_outgoing[node['id']]]
            if twins:
                for twin in twins:
                    glyph['_glyph_node_ids'].append(twin['id']); used.add(twin['id'])
                expressions=[node['lines'][0].split('：')[0].split(':')[0]
                             for node in [original,*twins] if node['lines']]
                glyph['label']=' / '.join(expressions) if expressions else ' / '.join(node['label'] for node in [original,*twins])
        glyphs.append(glyph)
    return glyphs


def _publication_brief(node: dict) -> tuple[str,...]:
    """Short source-defined dimensions and interface status for the main figure."""
    chains=_dimension_chains(node)
    if chains:
        chain=max(chains,key=len)
        alternatives=[other[-1] for other in chains if len(other)==len(chain) and other[:-1]==chain[:-1]]
        if len(set(alternatives))>1:
            chain=[*chain[:-1],'/'.join(dict.fromkeys(alternatives))]
        brief='→'.join(chain)
        kernel=next((match.group(0) for line in node['lines'] if (match:=re.search(r'核\s*\d+',line))),None)
        return (brief, kernel) if kernel else (brief,)
    if node['visual']=='parameter':
        rhos=list(dict.fromkeys(symbol for line in node['lines'] for symbol in re.findall(r'ρ(?:_m)?',line)))
        if len(rhos)>1:
            return (' / '.join(rhos),)
        return tuple(node['lines'][:1])
    if node['visual']=='clock':
        return tuple(node['lines'][:1])
    inverse=next((line.split('：')[-1].split(':')[-1] for line in node['lines'] if '解析逆' in line),None)
    if inverse:
        shapes=[match.group(0) for line in node['lines'] for match in re.finditer(r'\[[^\]]+\]',line)]
        return (inverse,shapes[-1]) if shapes else (inverse,)
    fft=next((line for line in node['lines'] if re.search(r'(?:i?rfft|fft)',line,re.I)),None)
    if node['visual']=='spectral' and fft:
        return (fft,)
    shape=next((match.group(0) for line in node['lines'] if (match:=re.search(r'\[[^\]]+\]',line))
               and (',' in match.group(0) or re.search(r'[BC]',match.group(0)))),None)
    if shape:
        return (shape,)
    if node['visual']=='block':
        pooling=next((match.group(1) for line in node['lines'] if (match:=re.search(r'pooling\s*=\s*([\w]+)',line))),None)
        if pooling:
            return (pooling,)
        return tuple(node['lines'][:1])
    return ()


def _publication_notes(original: dict) -> tuple[str,...]:
    """Keep science/interface annotations visible, omit repeated tensor outputs."""
    lines=original['lines']
    selected=[line for line in lines if not (len(lines)>1 and
        re.match(r'^(?:输出|形状|频谱|趋势预测|季节预测)\s*[:：]\s*\[',line))]
    return tuple(selected or lines)


def _publication_local_paths(glyphs: list[dict], node_ids: list[str], graph: dict) -> list[dict]:
    """Local glyph links map actual internal paths, never visual adjacency."""
    members=set(node_ids)
    representatives=[set(glyph.get('_glyph_node_ids',[glyph['id']])) for glyph in glyphs]
    selected=set().union(*representatives) if representatives else set()
    outgoing={identifier:[] for identifier in members}
    for index,edge in enumerate(graph['edges']):
        if edge['source'] in members and edge['target'] in members:
            outgoing[edge['source']].append((edge['target'],index))
    paths=[]
    for source_index,source_ids in enumerate(representatives):
        for target_index,target_ids in enumerate(representatives):
            if source_index==target_index:
                continue
            queue=[(identifier,[],[]) for identifier in sorted(source_ids)]
            visited=set(source_ids)
            found=None
            for current,indices,intermediates in queue:
                for target,index in outgoing[current]:
                    if target in target_ids:
                        found={'source':source_index,'target':target_index,
                               'edge_indices':indices+[index],'intermediate_node_ids':intermediates}
                        break
                    if target not in selected and target not in visited:
                        visited.add(target)
                        queue.append((target,indices+[index],intermediates+[target]))
                if found:
                    break
            if found:
                paths.append(found)
    return paths


def _publication_plan(module: dict, graph: dict, width: float) -> dict:
    by_id={node['id']:node for node in graph['nodes']}
    originals=[by_id[node] for node in module['node_ids']]
    glyphs=_publication_glyphs(originals,graph)
    special=any(node['kind']=='decision' for node in originals)
    stop=all(node['kind']=='stop' for node in originals)
    singleton_block=len(originals)==1 and len(glyphs)==1 and glyphs[0]['visual']=='block'
    if singleton_block:
        glyphs=[]
    title=_wrap(module['title'],max(7,(width-86)/17))
    title_height=len(title)*22
    columns=min(6,len(glyphs)) or 1
    glyph_rows=math.ceil(len(glyphs)/columns)
    glyph_height=148*glyph_rows if glyphs else 0
    conditions=[]
    if special or stop:
        # Decisions and failure criteria belong on the main scientific graph.
        conditions=[part for original in originals for line in original['lines']
                    for part in _wrap(line,(width-42)/13)]
    elif singleton_block:
        conditions=[part for line in originals[0]['lines'][:1] for part in _wrap(line,(width-42)/13)]
    interface=any('未展开' in line for node in originals for line in node['lines'])
    height=32+title_height+glyph_height+len(conditions)*17+(30 if interface else 0)+15
    return {'module':module,'originals':originals,'glyphs':glyphs,'title':title,
            'entries':[],'columns':columns,'row_heights':[],'width':width,
            'height':height,'stop':stop,'decision':special,'title_height':title_height,
            'glyph_height':glyph_height,'conditions':conditions,'interface':interface}


def _rounded_route(path: str, radius: float=8) -> str:
    points=[(float(x),float(y)) for x,y in re.findall(r'[ML](-?[\d.]+) (-?[\d.]+)',path)]
    if len(points)<3:
        return path
    result=[f'M{_number(points[0][0])} {_number(points[0][1])}']
    for before,corner,after in zip(points,points[1:],points[2:]):
        incoming=math.hypot(corner[0]-before[0],corner[1]-before[1])
        outgoing=math.hypot(after[0]-corner[0],after[1]-corner[1])
        bend=min(radius,incoming/2,outgoing/2)
        if not bend:
            continue
        start=(corner[0]+(before[0]-corner[0])*bend/incoming,
               corner[1]+(before[1]-corner[1])*bend/incoming)
        end=(corner[0]+(after[0]-corner[0])*bend/outgoing,
             corner[1]+(after[1]-corner[1])*bend/outgoing)
        result.append(f'L{_number(start[0])} {_number(start[1])} Q{_number(corner[0])} {_number(corner[1])} {_number(end[0])} {_number(end[1])}')
    result.append(f'L{_number(points[-1][0])} {_number(points[-1][1])}')
    return ' '.join(result)


def _render_publication(graph: dict, scene: dict) -> str:
    """Draw a paper overview, not a smaller copy of the detailed node diagram."""
    _nodes(graph)
    audit=validate_publication_scene(graph,scene)
    modules=scene['modules']
    by_module={module['id']:module for module in modules}
    by_id={node['id']:node for node in graph['nodes']}
    rank={module['id']:min((by_id[node]['layer'],by_id[node]['order']) for node in module['node_ids']) for module in modules}
    inset_ids={identifier for inset in scene['insets'] for identifier in inset['module_ids']}
    main=[module for module in modules if module['id'] not in inset_ids]
    if not main:
        main=modules
        inset_ids=set()
    incoming={module['id']:set() for module in main}
    outgoing={module['id']:set() for module in main}
    # Layout excludes feedback, references and local insets; drawing retains
    # every source connection, including these paths, with exact mappings.
    for connection in scene['connections']:
        source,target=connection['source'],connection['target']
        if (connection['display']=='arrow' and source in outgoing and target in incoming
                and rank[source]<rank[target]):
            outgoing[source].add(target); incoming[target].add(source)
    inline={module['id'] for module in main if len(module['node_ids'])==1 and
            (by_id[module['node_ids'][0]]['kind']=='decision' or
             by_id[module['node_ids'][0]]['kind'] in {'operation','merge'} and module['visual'] in {'block','merge'})}
    if len(inline)==len(main):
        inline.clear()
    weights={module['id']:0 if module['id'] in inline else 1 for module in main}
    columns,rows={},{}
    for module in sorted(main,key=lambda module:rank[module['id']]):
        identifier=module['id']; predecessors=incoming[identifier]
        columns[identifier]=module.get('column',max((columns[pred]+weights[pred] for pred in predecessors),default=0))
        desired=[]
        for predecessor in predecessors:
            siblings=sorted(outgoing[predecessor],key=lambda child:rank[child])
            desired.append(rows[predecessor]+siblings.index(identifier))
        rows[identifier]=module.get('row',sum(desired)/len(desired) if desired else 0)
    for module in reversed(sorted(main,key=lambda module:rank[module['id']])):
        identifier=module['id']
        if 'row' in module:
            continue
        if len(outgoing[identifier])>1:
            rows[identifier]=sum(rows[target] for target in outgoing[identifier])/len(outgoing[identifier])
        elif len(incoming[identifier])>1:
            rows[identifier]=sum(rows[source] for source in incoming[identifier])/len(incoming[identifier])
    majors=[module for module in main if module['id'] not in inline]
    column_ids=sorted({columns[module['id']] for module in majors}) or [0]
    grouped={column:[module for module in majors if columns[module['id']]==column] for column in column_ids}
    branch_modules=[module for members in grouped.values() if len(members)>1 for module in members]
    center=(min((rows[module['id']] for module in branch_modules),default=0)+
            max((rows[module['id']] for module in branch_modules),default=0))/2
    terminal=set()
    if len(column_ids)>1:
        for column in (column_ids[0],column_ids[-1]):
            if len(grouped[column])==1 and len(grouped[column][0]['node_ids'])>=3:
                identifier=grouped[column][0]['id']; terminal.add(identifier)
                if 'row' not in grouped[column][0]: rows[identifier]=center
    plans={}
    for module in modules:
        identifier=module['id']
        width=160 if identifier in inline else 340 if identifier in terminal else 230 if identifier in inset_ids else max(360,min(810,len(_publication_glyphs([by_id[node] for node in module['node_ids']],graph))*125+48))
        plans[identifier]=_publication_plan(module,graph,width)
        plan=plans[identifier]; plan['inline']=identifier in inline; plan['vertical']=identifier in terminal
        if plan['vertical']:
            plan['columns']=1; plan['glyph_height']=len(plan['glyphs'])*92
            plan['height']=47+plan['title_height']+plan['glyph_height']+(22 if plan['interface'] else 0)
        if plan['inline']:
            plan['glyphs']=[]; plan['glyph_height']=0
            if plan['decision']:
                clauses=[]
                for line in plan['originals'][0]['lines']:
                    if '互斥' in line:
                        match=re.search(r'互斥.*',line); clauses.append(match.group(0))
                    else:
                        clauses.append(line.split('：')[0].split(':')[0])
                plan['conditions']=[part for clause in clauses[:3] for part in _wrap(clause,10)]
                plan['title']=_wrap(module['title'],10); plan['title_height']=len(plan['title'])*19
                plan['height']=24+plan['title_height']+len(plan['conditions'])*17+16
            else:
                source=plan['originals'][0]
                plan['title']=_wrap(source['lines'][0] if source['lines'] else source['label'],10)
                plan['title_height']=len(plan['title'])*19; plan['conditions']=[]
                plan['height']=plan['title_height']+26
    column_widths={column:max(plans[module['id']]['width'] for module in members) for column,members in grouped.items()}
    column_left={}; cursor=40; gutter=220
    for column in column_ids:
        column_left[column]=cursor; cursor+=column_widths[column]+gutter
    for column in {columns[module['id']] for module in main}-set(column_left):
        next_column=next((value for value in column_ids if value>column),None)
        column_left[column]=column_left[next_column] if next_column is not None else cursor
    pitch=max((plans[module['id']]['height'] for module in branch_modules),default=max((plans[module['id']]['height'] for module in main if module['id'] not in terminal),default=220))+40
    origin=112+max((plans[module['id']]['height'] for module in branch_modules),default=220)/2
    # A tall vertical preprocessing/readout stack remains inside the canvas.
    origin=max(origin,max((112+plans[module['id']]['height']/2-rows[module['id']]*pitch for module in main),default=origin))
    placed=[]
    def overlaps(x,y,width,height):
        return any(abs(x-other.x)<(width+other.width)/2+24 and
                   abs(y-other.y)<(height+other.height)/2+24 for other in placed)
    for module in sorted(main,key=lambda module:(module['id'] in inline,columns[module['id']],rows[module['id']])):
        identifier=module['id']; plan=plans[identifier]
        x=column_left[columns[identifier]]+plan['width']/2
        if identifier in inline:
            x=max(40+plan['width']/2,column_left[columns[identifier]]-gutter/2)
        y=origin+rows[identifier]*pitch
        while overlaps(x,y,plan['width'],plan['height']):
            y+=pitch
        data={'id':identifier,'kind':'stop' if plan['stop'] else 'decision' if plan['decision'] else 'operation',
              'layer':columns[identifier],'order':rows[identifier]}
        placed.append(_Node(data,(),(),plan['width'],plan['height'],x,y))
    inset_plans=[]
    for inset in scene['insets']:
        ids=[identifier for identifier in inset['module_ids'] if identifier in inset_ids]
        if not ids:
            continue
        predecessors=[connection['source'] for connection in scene['connections']
                      if connection['target'] in ids and connection['source'] not in ids]
        anchors=[node for node in placed if node.data['id'] in predecessors]
        desired_width=sum(plans[identifier]['width']+28 for identifier in ids)+8
        anchor_left=min((node.x-node.width/2 for node in anchors),default=40)
        left=max(40,anchor_left-desired_width-40)
        top=max((node.y for node in anchors),default=max(node.y+node.height/2 for node in placed))-50
        cursor=left+18
        members=[]
        for identifier in ids:
            plan=plans[identifier]
            x=cursor+plan['width']/2; y=top+68+plan['height']/2
            while overlaps(x,y,plan['width'],plan['height']):
                blocking=[node for node in placed if abs(x-node.x)<(plan['width']+node.width)/2+24
                          and abs(y-node.y)<(plan['height']+node.height)/2+24]
                y=max(node.y+node.height/2 for node in blocking)+24+plan['height']/2
            node=_Node({'id':identifier,'kind':'stop' if plan['stop'] else 'operation','layer':0,'order':0},
                       (),(),plan['width'],plan['height'],x,y)
            placed.append(node); members.append(node); cursor+=plan['width']+28
        min_x=min(node.x-node.width/2 for node in members)-18
        min_y=min(node.y-node.height/2 for node in members)-68
        max_x=max(node.x+node.width/2 for node in members)+18
        max_y=max(node.y+node.height/2 for node in members)+18
        # The entire inset, including its header, is an obstacle. Testing only
        # the inner stop card would allow an input module to cover that title.
        other_nodes=[node for node in placed if node not in members]
        while True:
            blocking=[node for node in other_nodes
                      if min_x<node.x+node.width/2+20 and max_x>node.x-node.width/2-20
                      and min_y<node.y+node.height/2+20 and max_y>node.y-node.height/2-20]
            if not blocking:
                break
            offset=max(node.y+node.height/2 for node in blocking)+20-min_y
            for node in members: node.y+=offset
            min_y+=offset; max_y+=offset
        inset_plans.append({'title':inset['title'],'x':min_x,'y':min_y,'width':max_x-min_x,
                            'height':max_y-min_y,'module_ids':ids})
    width=max(node.x+node.width/2 for node in placed)+45
    height=max(node.y+node.height/2 for node in placed)+85
    if inset_plans:
        width=max(width,max(plan['x']+plan['width'] for plan in inset_plans)+40)
        height=max(height,max(plan['y']+plan['height'] for plan in inset_plans)+65)
    arrow_connections=[connection for connection in scene['connections'] if connection['display']=='arrow']
    route_edges=[{'source':connection['source'],'target':connection['target']} for connection in arrow_connections]
    routes=_panel_routes(placed,route_edges,inset_plans,width,height) if route_edges else {}
    positions={node.data['id']:node for node in placed}
    root=ET.Element('svg',{'xmlns':_NS,'version':'1.1','width':_number(width),'height':_number(height),
        'viewBox':f'0 0 {_number(width)} {_number(height)}','role':'img',
        'aria-labelledby':'diagram-title diagram-description','data-direction':'horizontal',
        'data-layout':'publication','data-original-node-count':str(len(graph['nodes'])),
        'data-original-edge-count':str(len(graph['edges']))})
    _element(root,'title',id='diagram-title').text=_string(graph['title'])
    _element(root,'desc',id='diagram-description').text='\n'.join([
        '架构概览与局部模块示意；所有源节点及连接保留可追溯映射。',
        f"{len(graph['nodes'])} 原节点，{len(graph['edges'])} 原连线，{len(modules)} 宏模块。",
        *[_string(paragraph['text']) for paragraph in graph.get('paragraphs',[])],
        '结构图案为示意，真实维度、互斥路径与未展开接口以源标注为准。'])
    metadata=_element(root,'metadata')
    metadata.text=json.dumps({'publication_scene':scene,'original_graph':graph,'mapping_audit':audit,
        'layout_provenance':'agent_scene' if graph.get('publication_scene') else 'automatic_topology_compiler'},
        ensure_ascii=False,separators=(',',':'))
    definitions=_element(root,'defs')
    marker=_element(definitions,'marker',id='diagram-arrow',markerWidth='7',markerHeight='7',
                    refX='6',refY='3.5',orient='auto',markerUnits='userSpaceOnUse')
    _element(marker,'path',d='M0 0 L7 3.5 L0 7 Z',fill='#65758A')
    _element(root,'rect',x='0',y='0',width=_number(width),height=_number(height),fill='#ffffff')
    _text_lines(root,_wrap(graph['title'],(width-90)/21),40,39,size=21,spacing=25,anchor='start',weight='600',color='#182C47')
    _text_lines(root,('架构概览 / 局部模块示意',),40,69,size=14,anchor='start',color='#64748B')
    inset_group=_element(root,'g',id='diagram-insets')
    for plan in inset_plans:
        group=_element(inset_group,'g',data_module_ids=' '.join(plan['module_ids']))
        _element(group,'title').text=plan['title']
        _element(group,'rect',x=_number(plan['x']),y=_number(plan['y']),width=_number(plan['width']),height=_number(plan['height']),
                 rx='10',fill='#FFFCFB',stroke='#DCC9C5',stroke_width='1',stroke_dasharray='5 4')
        _text_lines(group,_wrap('局部约束 · '+plan['title'],(plan['width']-36)/15),plan['x']+18,plan['y']+26,
                    size=15,anchor='start',weight='600',color='#9E5954')
    edge_group=_element(root,'g',id='diagram-edges')
    arrow_index=0; badge_index=0; badge_groups=[]
    for index,connection in enumerate(scene['connections']):
        group=_element(edge_group,'g',data_edge_index=index,data_edge_indices=' '.join(map(str,connection['edge_indices'])),
                       data_source=connection['source'],data_target=connection['target'],data_display=connection['display'])
        labels=list(dict.fromkeys(graph['edges'][i]['label'] for i in connection['edge_indices']))
        _element(group,'title').text=' / '.join(labels)
        if connection['display']=='badge':
            badge_index+=1; badge_groups.append((badge_index,connection)); continue
        path,x,y,anchor=routes[arrow_index]; arrow_index+=1
        source,target=positions[connection['source']],positions[connection['target']]
        failure=plans[connection['target']]['stop']
        feedback=rank[connection['source']]>=rank[connection['target']]
        color='#A35E57' if failure else '#65758A'
        _element(group,'path',d=_rounded_route(path),fill='none',stroke=color,stroke_width='1.7',
                 stroke_linejoin='round',marker_end='url(#diagram-arrow)',
                 **({'stroke_dasharray':'5 4'} if failure or feedback else {}))
        label=connection['label'] or ' / '.join(labels)
        # Module captions/choices identify business routes. Duplicate edge prose
        # is traceable in title/metadata rather than covering scientific glyphs.
        group.set('data-label-placement','module-caption-or-metadata')
    node_group=_element(root,'g',id='diagram-nodes')
    node_groups={}
    palette={'mlp':('#695A96','#F8F6FC'),'conv':('#398287','#F3FAF9'),
             'clock':('#398287','#F3FAF9'),'tensor':('#416D98','#F5F8FC'),
             'spectral':('#416D98','#F5F8FC'),'parameter':('#695A96','#F8F6FC')}
    for index,module in enumerate(modules):
        identifier=module['id']; plan=plans[identifier]; position=positions[identifier]
        w,h=plan['width'],plan['height']; x,y=-w/2,-h/2
        accent,pale=palette.get(module['visual'],('#586B80','#F8FAFC'))
        if plan['decision']:
            accent,pale='#957A3F','#FCFAF3'
        if plan['stop']:
            accent,pale='#AA5A51','#FFF6F4'
        group=_element(node_group,'g',data_node_id=identifier,data_module_id=identifier,
            data_node_ids=' '.join(module['node_ids']),data_kind=position.data['kind'],data_visual=module['visual'],
            transform=f'translate({_number(position.x)} {_number(position.y)})',
            data_width=_number(w),data_height=_number(h))
        node_groups[identifier]=group
        _element(group,'title').text='\n\n'.join('\n'.join([node['label'],*node['lines'],node['detail']]) for node in plan['originals'])
        _element(group,'rect',x=_number(x),y=_number(y),width=_number(w),height=_number(h),rx='9',
                 fill=pale,stroke='#D6DEE7' if not plan['stop'] else '#D8AAA5',stroke_width='1')
        _element(group,'rect',x=_number(x+17),y=_number(y+18),width='3',height=_number(plan['title_height']),rx='1',fill=accent)
        letter=chr(97+index) if index<26 else str(index+1)
        if plan['inline']:
            _text_lines(group,plan['title'],x+28,y+22,size=13,spacing=19,anchor='start',weight='600',color='#23364C')
            content_y=y+16+plan['title_height']
        else:
            _text_lines(group,('('+letter+')',),x+28,y+33,size=14,anchor='start',weight='600',color=accent)
            _text_lines(group,plan['title'],x+62,y+33,size=17,spacing=22,anchor='start',weight='600',color='#23364C')
            content_y=y+26+plan['title_height']
        if plan['decision']:
            # A visible diamond denotes a business choice; the original mutual
            # exclusion text remains in the summary rather than a vague merge.
            symbol_y=y+h-27 if plan['inline'] else y+21
            _element(group,'path',data_choice_symbol='true',d=f'M{_number(x+w-26)} {_number(symbol_y)} l9 9 -9 9 -9 -9 Z',
                     fill='#ffffff',stroke=accent,stroke_width='1.2')
        if plan['glyphs']:
            count=len(plan['glyphs']); step=(w-46)/plan['columns']
            glyph_xs=[x+23+(local_index%plan['columns']+.5)*step for local_index in range(count)]
            glyph_ys=[content_y+55+(local_index//plan['columns'])*148 for local_index in range(count)]
            if plan['vertical']:
                step=w-46
                glyph_xs=[x+74]*count
                glyph_ys=[content_y+42+local_index*92 for local_index in range(count)]
            local_edges=_element(group,'g',data_local_connections='source-mapped')
            for link in _publication_local_paths(plan['glyphs'],module['node_ids'],graph):
                start,end=link['source'],link['target']
                direction=1 if end>start else -1
                port=min(62,step*.41)
                a,b=glyph_xs[start]+direction*port,glyph_xs[end]-direction*port
                by=glyph_ys[start]
                cross_row=glyph_ys[start]!=glyph_ys[end]
                skip=abs(end-start)>1 or end<start
                if cross_row:
                    local_path=_path([(glyph_xs[start],by+31),(glyph_xs[start],by+75),
                                      (glyph_xs[end],by+75),(glyph_xs[end],glyph_ys[end]-31)])
                elif skip:
                    top=content_y+9
                    local_path=_path([(a,by-25),(a,top),(b,top),(b,by-25)])
                else:
                    local_path=_path([(a,by),(b,by)])
                local=_element(local_edges,'g',data_local_edge_indices=' '.join(map(str,link['edge_indices'])),
                    data_local_source=plan['glyphs'][start]['id'],data_local_target=plan['glyphs'][end]['id'],
                    data_local_intermediate_node_ids=' '.join(link['intermediate_node_ids']))
                _element(local,'title').text=' / '.join(graph['edges'][i]['label'] for i in link['edge_indices'])
                _element(local,'path',d=_rounded_route(local_path,4),fill='none',stroke=accent,
                    stroke_width='1.2',marker_end='url(#diagram-arrow)',
                    **({'stroke_dasharray':'4 3'} if skip else {}))
                if link['intermediate_node_ids']:
                    intermediates=[by_id[node] for node in link['intermediate_node_ids']]
                    if skip:
                        note=next((match.group(0).replace(' ','') for node in intermediates for line in node['lines']
                                   if (match:=re.search(r'\d+\s*→\s*\d+',line))),intermediates[0]['label'])
                        _text_lines(local,(note,),sum((a,b))/2,content_y+5,size=11,color=accent,outlined=True)
                    elif len(intermediates)==1:
                        # An omitted interface (for example source-defined
                        # pooling) is a small operator annotation, not an
                        # invented internal neural network.
                        label=intermediates[0]['label']
                        cx=(glyph_xs[start]+glyph_xs[end])/2
                        _element(local,'rect',x=_number(cx-58),y=_number(content_y-4),width='116',height='20',
                                 rx='4',fill='#ffffff',stroke='#B8CAD6',stroke_width='.8')
                        _text_lines(local,_wrap(label,10),cx,content_y+10,size=10,spacing=12,color=accent)
            for local_index,glyph in enumerate(plan['glyphs']):
                gx=glyph_xs[local_index]
                gy=glyph_ys[local_index]
                scale=.55 if plan['vertical'] else min(.66,step/245)
                local=_element(group,'g',data_local_node_id=glyph['id'],
                    data_local_node_ids=' '.join(glyph.get('_glyph_node_ids',[glyph['id']])),
                    transform=f'translate({_number(gx)} {_number(gy)}) scale({_number(scale)})')
                proxy=_Node({**glyph,'_hide_dimension_labels':True},(),(),220,110)
                if glyph['visual']=='block':
                    _element(local,'rect',x='-80',y='-27',width='160',height='54',rx='6',
                        fill='#ffffff',stroke=accent,stroke_width='1.6')
                    inside=next((match.group(1) for line in glyph['lines']
                                 if (match:=re.search(r'pooling\s*=\s*(\w+)',line))),None)
                    _text_lines(local,_wrap(inside or glyph['label'],8),0,-5 if len(_wrap(inside or glyph['label'],8))>1 else 5,
                                size=16,spacing=19,color=accent)
                else:
                    _illustration(local,proxy,0)
                # Recolor exact generated glyph geometry to the section palette.
                for element in local.iter():
                    for key in ('stroke','fill'):
                        if element.get(key) in {'#3775BA','#0F4D92'}: element.set(key,accent)
                        if element.get(key)=='#DCEAF8': element.set(key,pale)
                local_name=glyph['label']
                caption_width=w-154 if plan['vertical'] else step
                names=_wrap(local_name,max(6,caption_width/13))
                names=names[:1]+(names[1]+'…',) if len(names)>2 else names
                caption_x=x+145 if plan['vertical'] else gx
                caption_y=gy-13 if plan['vertical'] else gy+45
                _text_lines(group,names,caption_x,caption_y,size=13,spacing=16,color=accent,
                            anchor='start' if plan['vertical'] else 'middle')
                brief_lines=tuple(part for line in _publication_brief(glyph) for part in
                    ((line,) if not re.search(r'[\u3400-\u9fff]',line) and len(line)<=24 else _wrap(line,max(6,caption_width/13))))
                brief_y=gy+27 if plan['vertical'] else gy+88
                _text_lines(group,brief_lines[:2],caption_x,brief_y,size=13,spacing=16,color='#42556C',
                            anchor='start' if plan['vertical'] else 'middle')
            content_y+=plan['glyph_height']
        if plan['conditions']:
            _text_lines(group,tuple(plan['conditions']),x+21,content_y+19,size=13,spacing=17,anchor='start',color='#5B6675')
        if plan['interface']:
            _text_lines(group,('局部接口未展开',),x+21,y+h-17,size=12,anchor='start',color='#718096')
        cell=(w-42-(plan['columns']-1)*18)/plan['columns']
        row_y=content_y+20
        for start,row_height in zip(range(0,len(plan['entries']),plan['columns']),plan['row_heights']):
            for local_index,entry in enumerate(plan['entries'][start:start+plan['columns']]):
                tx=x+21+local_index*(cell+18)
                text_group=_element(group,'g',data_source_node_id=entry['node']['id'])
                _text_lines(text_group,entry['label'],tx,row_y,size=14,spacing=17,anchor='start',weight='600',color='#314258')
                _text_lines(text_group,entry['notes'],tx,row_y+len(entry['label'])*17,size=13,spacing=16,anchor='start',color='#5B6675')
            row_y+=row_height
    endpoint_badges={identifier:0 for identifier in node_groups}
    for number,connection in badge_groups:
        for endpoint in (connection['source'],connection['target']):
            group=node_groups[endpoint]; plan=plans[endpoint]
            slot=endpoint_badges[endpoint]; endpoint_badges[endpoint]+=1
            x=plan['width']/2-35-slot*38; y=-plan['height']/2-13
            badge=_element(group,'g',data_relation_badge=number,data_relation_source=connection['source'],data_relation_target=connection['target'])
            _element(badge,'title').text=connection['label']
            _element(badge,'rect',x=_number(x),y=_number(y),width='30',height='25',rx='6',fill='#ffffff',stroke='#99AFC9',stroke_width='1')
            _text_lines(badge,(f'R{number}',),x+15,y+17,size=12,weight='600',color='#416D98')
    caption='结构图案为示意；全部语义节点及原连线保留映射，互斥选择只执行所选支路。'
    _text_lines(root,(caption,),40,height-35,size=13,anchor='start',color='#64748B')
    if badge_groups:
        legend='；'.join(f'R{number} '+connection['label'] for number,connection in badge_groups)
        _text_lines(root,_wrap(legend,(width-80)/13),40,height-57,size=13,anchor='start',color='#64748B')
    xml=ET.tostring(root,encoding='unicode')
    return '<?xml version="1.0" encoding="UTF-8"?>'+xml.replace('\r','&#13;').replace('\n','&#10;')


def render_svg(graph: dict, *, vertical: bool = False, publication: bool = False) -> str:
    """Return a standalone horizontal network or vertical flowchart SVG.

    The graph is the ``network``/``flowchart`` object from architecture_diagrams.
    Output is single-line XML: text CR/LF become numeric entities so Windows
    text-mode artifact saving preserves the exact UTF-8 bytes and digest.
    """
    if publication:
        return _render_publication(graph, graph.get('publication_scene') or compile_publication_scene(graph))
    nodes = _nodes(graph)
    title = _string(graph['title'])
    if not title:
        raise ValueError('绘图标题超出架构图协议。')
    by_id = {node.data['id']: node for node in nodes}
    layers = sorted({node.data['layer'] for node in nodes})
    ranks = {layer: index for index, layer in enumerate(layers)}
    edges = graph['edges']
    labels = []
    outer = []
    pairs = set()
    for index, edge in enumerate(edges):
        source, target = _string(edge['source']), _string(edge['target'])
        label = _string(edge['label'])
        if source not in by_id or target not in by_id or source == target or (source, target) in pairs:
            raise ValueError('绘图连线端点、重复或自环无效。')
        pairs.add((source, target))
        labels.append(_wrap(label, 12) if label else ())
        delta = ranks[by_id[target].data['layer']] - ranks[by_id[source].data['layer']]
        if delta > 1 or delta < 0:
            outer.append(index)
    groups = {layer: sorted((node for node in nodes if node.data['layer'] == layer),
                            key=lambda node: node.data['order']) for layer in layers}
    max_branches = max(len(group) for group in groups.values())
    branch_size = max(node.width if vertical else node.height for node in nodes)
    branch_pitch = branch_size + 60
    branch_span = branch_size + (max_branches - 1) * branch_pitch
    label_height = max((len(lines) * 17 for lines in labels), default=0)
    gap = max(110, 2 * label_height + 60) if vertical else 210
    layer_sizes = {layer: max(node.height if vertical else node.width for node in groups[layer])
                   for layer in layers}
    primary_span = sum(layer_sizes.values()) + (len(layers) - 1) * gap
    base_width = branch_span + 80 if vertical else primary_span + 80 + gap / 2
    heading = _wrap(title, max(20, (base_width - 80) / 20))
    header_height = 40 + len(heading) * 25 + 24
    lanes = {}
    if vertical:
        cursor = base_width + 8
        for index in outer:
            lanes[index] = cursor
            cursor += max(38, max((sum(_units(c) for c in line) * 14 for line in labels[index]), default=0) + 28)
        width = cursor + 20 if outer else base_width
        top = header_height + 24
        height = top + primary_span + 40 + gap / 2
    else:
        cursor = header_height + 12
        for index in outer:
            cursor += max(22, len(labels[index]) * 17 + 14)
            lanes[index] = cursor
        top = cursor + 36
        width = base_width
        height = top + branch_span + 40
    primary = header_height + 24 if vertical else 40
    for layer in layers:
        centre = primary + layer_sizes[layer] / 2
        group = groups[layer]
        for index, node in enumerate(group):
            secondary = (branch_span / 2 + 40 + (index - (len(group) - 1) / 2) * branch_pitch)
            node.x = secondary if vertical else centre
            node.y = centre if vertical else top + branch_span / 2 + (index - (len(group) - 1) / 2) * branch_pitch
        primary += layer_sizes[layer] + gap
    folded = not vertical and len(layers) > 6
    cross_routes, row_of = {}, {layer: 0 for layer in layers}
    if folded:
        width, height, heading, lanes, cross_routes, row_of = _folded_layout(
            nodes, edges, labels, title, layers, groups, ranks, layer_sizes, gap)
    plans = []
    if graph.get('panels'):
        width, height, heading, plans = _panel_layout(graph, nodes)
        cross_routes = _panel_routes(nodes, edges, plans, width, height)
    root = ET.Element('svg', {'xmlns': _NS, 'version': '1.1', 'width': _number(width),
        'height': _number(height), 'viewBox': f'0 0 {_number(width)} {_number(height)}',
        'role': 'img', 'aria-labelledby': 'diagram-title diagram-description',
        'data-direction': 'vertical' if vertical else 'horizontal',
        'data-layout': 'panels' if plans else 'folded' if folded else 'layered'})
    _element(root, 'title', id='diagram-title').text = title
    paragraphs = [_string(paragraph['text']) for paragraph in graph.get('paragraphs', [])]
    _element(root, 'desc', id='diagram-description').text = '\n'.join([
        f'{"流程图" if vertical else "网络结构图"}，{len(nodes)} 个模块，{len(edges)} 条连接。', *paragraphs])
    definitions = _element(root, 'defs')
    marker = _element(definitions, 'marker', id='diagram-arrow', markerWidth='8', markerHeight='8',
                      refX='7', refY='4', orient='auto', markerUnits='userSpaceOnUse')
    _element(marker, 'path', d='M0 0 L8 4 L0 8 Z', fill=_EDGE_COLOR)
    _element(root, 'rect', x='0', y='0', width=_number(width), height=_number(height), fill=_BACKGROUND)
    _text_lines(root, heading, 40, 38, size=20, spacing=25, anchor='start', weight='600', color='#0F4D92')
    _text_lines(root, (f'{"流程图" if vertical else "网络结构图"} · {len(nodes)} 模块 · {len(edges)} 条连接',),
                40, 38 + len(heading) * 25, size=13, anchor='start')
    if plans:
        _text_lines(root, ('模块图案为结构示意；准确层宽、张量形状与未展开接口以节点标注为准。',),
                    40, 61 + len(heading)*25, size=13, anchor='start')
        panel_group = _element(root, 'g', id='diagram-panels')
        for plan in plans:
            group = _element(panel_group, 'g', data_panel_id=plan['id'])
            _element(group, 'title').text = plan['title']
            _element(group, 'rect', x=_number(plan['x']), y=_number(plan['y']), width=_number(plan['width']),
                     height=_number(plan['height']), rx='10', fill='#fff8f7' if plan['red'] else '#f7fafe',
                     stroke='#B64342' if plan['red'] else '#A7BDD5', stroke_width='1.3', stroke_dasharray='6 4')
            _text_lines(group, (plan['title'],), plan['x']+20, plan['y']+29,
                        size=17, anchor='start', weight='600', color='#B64342' if plan['red'] else '#0F4D92')
    edge_group = _element(root, 'g', id='diagram-edges')
    for index, edge in enumerate(edges):
        group = _element(edge_group, 'g', data_edge_index=index, data_source=edge['source'],
                         data_target=edge['target'], data_label=edge['label'])
        _element(group, 'title').text = f'{by_id[edge["source"]].data["label"]} → {by_id[edge["target"]].data["label"]}' + (f'：{edge["label"]}' if edge['label'] else '')
        path, x, y, anchor = cross_routes.get(index) or _route(
            by_id[edge['source']], by_id[edge['target']], ranks,
            vertical, lanes.get(index), gap, layer_sizes,
            1 if row_of[by_id[edge['source']].data['layer']] % 2 == 0 else -1)
        _element(group, 'path', d=path, fill='none', stroke=_EDGE_COLOR, stroke_width='2',
                 stroke_linejoin='round', marker_end='url(#diagram-arrow)',
                 **({'stroke_dasharray': '7 5'} if index in outer else {}))
        if labels[index]:
            # Labels above horizontal routes and centred alongside vertical ones.
            first_y = y - (len(labels[index]) - 1) * 17 if anchor == 'middle' else y - (len(labels[index]) - 1) * 8.5
            _text_lines(group, labels[index], x, first_y, size=14, spacing=17, anchor=anchor, outlined=True)
    node_group = _element(root, 'g', id='diagram-nodes')
    for index, node in enumerate(nodes):
        data, width, height = node.data, node.width, node.height
        title_id = f'diagram-node-title-{index}'
        group = _element(node_group, 'g', id=f'diagram-node-{index}', data_node_id=data['id'],
                         data_kind=data['kind'], data_layer=data['layer'], data_order=data['order'],
                         data_row=row_of[data['layer']],
                         data_visual=data.get('visual', 'block'), data_panel_id=data.get('panel_id', ''),
                         role='group', aria_labelledby=title_id,
                         transform=f'translate({_number(node.x)} {_number(node.y)})')
        _element(group, 'title', id=title_id).text = '\n'.join([data['label'], *data['lines'], data['detail']])
        fill, stroke = ('#EAF2FB', '#3775BA') if data['kind'] == 'layer' else ('#ffffff', '#767676')
        if data['kind'] in {'input', 'output'}:
            fill, stroke = '#DDF3DE', '#739D72'
        elif data['kind'] == 'merge':
            fill, stroke = '#EAF2FB', '#0F4D92'
        if data['kind'] in {'decision', 'stop'}:
            fill, stroke = '#F6CFCB', '#B64342'
        if data['kind'] == 'decision':
            _element(group, 'polygon', points=f'0,{_number(-height/2)} {_number(width/2)},0 0,{_number(height/2)} {_number(-width/2)},0',
                     fill=fill, stroke=stroke, stroke_width='2')
        elif data['kind'] == 'merge':
            _element(group, 'polygon', points=f'{_number(-width/2+18)},{_number(-height/2)} {_number(width/2-18)},{_number(-height/2)} {_number(width/2)},0 {_number(width/2-18)},{_number(height/2)} {_number(-width/2+18)},{_number(height/2)} {_number(-width/2)},0',
                     fill=fill, stroke=stroke, stroke_width='2')
        else:
            _element(group, 'rect', x=_number(-width / 2), y=_number(-height / 2),
                     width=_number(width), height=_number(height),
                     rx='28' if data['kind'] in {'input', 'output', 'stop'} else '12',
                     fill=fill, stroke=stroke, stroke_width='2')
        illustration_height = 108 if data.get('visual', 'block') != 'block' and data['kind'] != 'decision' else 0
        content_height = len(node.label) * 20 + len(node.lines) * 18 + (12 if node.lines else 0) + illustration_height
        first_y = -content_height / 2 + 16
        _text_lines(group, node.label, 0, first_y, size=16, spacing=20, weight='600', color='#272727')
        if illustration_height:
            _illustration(group, node, first_y + len(node.label)*20 + 48)
        if node.lines:
            _text_lines(group, node.lines, 0, first_y + len(node.label) * 20 + illustration_height + 10,
                        size=15, spacing=18)
    xml = ET.tostring(root, encoding='unicode', short_empty_elements=True)
    return '<?xml version="1.0" encoding="UTF-8"?>' + xml.replace('\r', '&#13;').replace('\n', '&#10;')
