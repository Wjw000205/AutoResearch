"""Read-only scientific presentation of an already audited agent graph.

This is the same renderer used by architecture.draw_diagrams. Re-presenting a
legacy saved graph does not author a graph, invoke an agent, or create a receipt.
"""
from __future__ import annotations

import copy
from functools import lru_cache
import json
import math
import re
import xml.etree.ElementTree as ET

from . import architecture_diagrams as diagrams
from .architecture_diagram_svg import render_svg
from .engine import digest


RENDERER_VERSION = 1


def _transform(value: str):
    """The renderer emits translate/scale only on interactive containers."""
    sx, sy, tx, ty = 1.0, 1.0, 0.0, 0.0
    for kind, body in re.findall(r'(translate|scale)\s*\(([^)]+)\)', value):
        numbers = [float(part) for part in re.split(r'[ ,]+', body.strip())]
        if not 1 <= len(numbers) <= 2 or not all(math.isfinite(n) for n in numbers):
            raise ValueError('绘图坐标变换无效。')
        if kind == 'translate':
            tx += sx * numbers[0]
            ty += sy * (numbers[1] if len(numbers) == 2 else 0)
        else:
            sx *= numbers[0]
            sy *= numbers[1] if len(numbers) == 2 else numbers[0]
    return sx, sy, tx, ty


def _geometry(svg: str, graph: dict):
    root = ET.fromstring(svg)
    units = []
    valid_ids = {node['id'] for node in graph['nodes']}

    def visit(element, parent=(1.0, 1.0, 0.0, 0.0), owner=None):
        local = _transform(element.get('transform', ''))
        psx, psy, ptx, pty = parent
        lsx, lsy, ltx, lty = local
        world = psx * lsx, psy * lsy, ptx + psx * ltx, pty + psy * lty
        sx, sy, tx, ty = world
        module = element.get('data-module-id') or element.get('data-node-id')
        glyph = element.get('data-local-node-id')
        if module or glyph:
            ids = (element.get('data-local-node-ids') if glyph else
                   element.get('data-node-ids')) or module or glyph
            node_ids = ids.split()
            if not node_ids or not set(node_ids) <= valid_ids:
                raise ValueError('图示模块映射不属于已保存的图结构。')
            if module:
                owner = module
                width = element.get('data-width')
                height = element.get('data-height')
                if width is None or height is None:
                    shape = next((child for child in element
                                  if child.tag.rsplit('}', 1)[-1] in {'rect', 'polygon'}), None)
                    if shape is None:
                        raise ValueError('图示缺少可定位的模块形状。')
                    if shape.tag.rsplit('}', 1)[-1] == 'rect':
                        width, height = shape.get('width'), shape.get('height')
                    else:
                        points = [float(n) for n in re.findall(r'-?\d+(?:\.\d+)?', shape.get('points', ''))]
                        width = max(points[::2]) - min(points[::2])
                        height = max(points[1::2]) - min(points[1::2])
                unit_id, role = module, 'module'
            else:
                # Local category illustrations use the renderer's 220x110 proxy.
                width, height = 220, 110
                unit_id, role = f'glyph:{owner}:{glyph}', 'glyph'
            dimensions = float(width) * abs(sx), float(height) * abs(sy)
            if not all(math.isfinite(n) and n > 0 for n in dimensions):
                raise ValueError('图示模块尺寸无效。')
            units.append({'id': unit_id, 'role': role, 'x': tx, 'y': ty,
                          'width': dimensions[0], 'height': dimensions[1], 'node_ids': node_ids})
        for child in element:
            visit(child, world, owner)

    visit(root)
    if set().union(*(set(unit['node_ids']) for unit in units)) != valid_ids:
        raise ValueError('图示没有保留全部原始模块的定位映射。')
    if len({unit['id'] for unit in units}) != len(units):
        raise ValueError('图示交互单元编号重复。')
    return {'width': float(root.get('width')), 'height': float(root.get('height')), 'nodes': units}


@lru_cache(maxsize=16)
def _render(graph_json: str, kind: str, version: int):
    graph = json.loads(graph_json)
    svg = render_svg(graph, publication=kind == 'network', vertical=kind == 'flowchart')
    return {'svg': svg, **_geometry(svg, graph)}


def _saved(engine, run_id, document_id, expected_sha256, *, historical=False):
    review = diagrams.document_review(engine, run_id, document_id, expected_sha256, historical=historical)
    if (not review or review.get('status') != 'approved'
            or review['document']['document_id'] != document_id
            or review['document']['sha256'] != expected_sha256):
        raise ValueError('架构版本已经变化或尚未通过评议，请重新读取详情。')
    row = diagrams.saved_record(engine, run_id, document_id, expected_sha256, historical=historical)
    if (not row or row.get('status') != 'completed'
            or row.get('run_id') != run_id
            or row.get('architecture_document_id') != document_id
            or row.get('document_sha256') != expected_sha256
            or row.get('audit', {}).get('status') != 'validated'):
        raise ValueError('同版本的 agent 图结构尚未完成来源校验与交付。')
    bundle = row.get('bundle', {})
    if (bundle.get('architecture_document_id') != document_id
            or bundle.get('architecture_document_sha256') != expected_sha256
            or row.get('bundle_hash') != digest(bundle)):
        raise ValueError('已保存图结构的版本或内容哈希不一致。')
    return row


def presentation(engine, run_id, document_id, expected_sha256, kind, *, historical=False):
    if kind not in {'network', 'flowchart'}:
        raise ValueError('未知架构图类型。')
    row = _saved(engine, run_id, document_id, expected_sha256, historical=historical)
    graph_json = json.dumps(row['bundle'][kind], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    result = copy.deepcopy(_render(graph_json, kind, RENDERER_VERSION))
    latest = _saved(engine, run_id, document_id, expected_sha256, historical=historical)
    if latest['bundle_hash'] != row['bundle_hash']:
        raise ValueError('图结构在绘制过程中已变化，请重新读取。')
    return {**result, 'document_id': document_id, 'document_sha256': expected_sha256,
            'bundle_hash': row['bundle_hash'], 'kind': kind, 'renderer_version': RENDERER_VERSION,
            'provenance': 'saved_agent_bundle_presentation'}
