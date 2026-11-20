"""Lossless, content-addressed views of structured task inputs.

Original records are never edited. Tagged references cannot collide with a
user field, and every view is checked by reconstructing the complete source.
"""
from __future__ import annotations

import copy
import hashlib
import json

VERSION = 1
NODE_LIMIT = 1800
SCALAR_TYPES = {type(None), bool, int, float, str}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def sha(value):
    return hashlib.sha256(encoded(value).encode('utf-8')).hexdigest()


def compile_view(source):
    nodes = {}

    def visit(value):
        if isinstance(value, dict):
            if any(type(k) is not str for k in value):
                raise ValueError('Task document objects require string keys')
            body = ['object', [[k, visit(value[k])] for k in sorted(value)]]
        elif isinstance(value, list):
            body = ['array', [visit(v) for v in value]]
        else:
            if type(value) not in SCALAR_TYPES:
                raise ValueError('Task document literals require JSON scalar types')
            # Encode first to reject NaN, unsupported objects and infinities.
            text = encoded(value)
            if len(text) <= NODE_LIMIT:
                return ['literal', copy.deepcopy(value)]
            if not isinstance(value, str):
                raise ValueError('Unsupported oversized scalar')
            body = ['string', [value[i:i + NODE_LIMIT] for i in range(0, len(value), NODE_LIMIT)]]
        identity = sha(value)
        if identity in nodes and nodes[identity] != body:
            raise ValueError('Task document content identity collision')
        nodes[identity] = body
        return ['reference', identity]

    root = visit(source)
    result = {'version': VERSION, 'source_sha256': sha(source), 'root': root, 'nodes': nodes}
    if restore(result) != source:
        raise ValueError('Task document projection changed its source')
    return result


def restore(view):
    if not isinstance(view, dict) or type(view.get('version')) is not int or view['version'] != VERSION:
        raise ValueError('Unsupported task document projection version')
    if not isinstance(view.get('nodes'), dict):
        raise ValueError('Invalid task document node index')
    nodes, cache, active = view['nodes'], {}, set()

    def value(ref):
        if not isinstance(ref, list) or len(ref) != 2:
            raise ValueError('Invalid task document reference')
        if ref[0] == 'literal':
            if type(ref[1]) not in SCALAR_TYPES or len(encoded(ref[1])) > NODE_LIMIT:
                raise ValueError('Invalid task document literal')
            return copy.deepcopy(ref[1])
        if ref[0] != 'reference' or type(ref[1]) is not str or ref[1] not in nodes or ref[1] in active:
            raise ValueError('Missing or cyclic task document reference')
        key = ref[1]
        if key in cache:
            return copy.deepcopy(cache[key])
        active.add(key)
        node = nodes[key]
        if not isinstance(node, list) or len(node) != 2 or not isinstance(node[1], list):
            raise ValueError('Invalid task document node')
        kind, body = node
        if kind == 'object':
            if any(not isinstance(pair, list) or len(pair) != 2 or type(pair[0]) is not str for pair in body):
                raise ValueError('Task document objects require string keys')
            if len({k for k, _ in body}) != len(body):
                raise ValueError('Duplicate task document key')
            result = {k: value(v) for k, v in body}
        elif kind == 'array':
            result = [value(v) for v in body]
        elif kind == 'string':
            if any(type(piece) is not str or len(piece) > NODE_LIMIT for piece in body):
                raise ValueError('Invalid task document string chunk')
            result = ''.join(body)
        else:
            raise ValueError('Unknown task document node type')
        active.remove(key)
        if sha(result) != key:
            raise ValueError('Task document node fingerprint changed')
        cache[key] = result
        return copy.deepcopy(result)

    result = value(view['root'])
    if sha(result) != view['source_sha256']:
        raise ValueError('Task document source fingerprint changed')
    if set(cache) != set(nodes):
        raise ValueError('Task document contains unreachable material')
    return result


def render(view, title):
    restore(view)
    lines = ['# ' + title, '', '按任务定位的无损资料视图；来源对象和原始证据保持不变。',
        '先读根节点，再按 reference 指向的完整哈希定位节点。无需通读来源附录。',
        '节点是原字段的精确表示，不是模型摘要，不授予科学批准或实验权限。',
        'source_sha256: `' + view['source_sha256'] + '`', '',
        '## 根节点', '', '```json', encoded(view['root']), '```', '']
    for key, (kind, body) in view['nodes'].items():
        # Small sections make every logical item independently addressable.
        pieces = body if kind == 'string' else [body[i:i + 8] for i in range(0, len(body), 8)]
        if not pieces:
            pieces = [[]]
        for index, piece in enumerate(pieces):
            lines += [f'## {key} {kind} {index + 1}/{len(pieces)}', '', '```json', encoded(piece), '```', '']
    return '\n'.join(lines)


def brief(source, title):
    """One root overview plus separately indexed deduplicated definitions."""
    view = compile_view(source)
    root = view['root']
    content = (['# ' + title, '', '来源 SHA256：`' + view['source_sha256'] + '`',
        '原对象逐字段保留；reference 是定义文档的内容地址。按当前问题定位，不要求全文重读。',
        '本视图不改变字段、作业、种子、验收标准或审批。', '', '## 当前任务字段', '', '```json']
        + [encoded(view['nodes'][root[1]]) if root[0] == 'reference' else encoded(root), '```', ''])
    return view, '\n'.join(content)
