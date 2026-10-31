"""Bounded memory navigation from frozen inputs, never an agent retrieval call.

The four display levels describe containment and saved source relationships.
They are not invented compression ancestry or a claim of model attention.
"""
from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from threading import Lock

from .memory_views import ROLES, _resolve, _scalar, _snippet, _text_page

TOOLS = {'memory.search', 'evidence.search', 'literature.rag_search', 'documents.search',
         'context.expand', 'documents.read', 'literature.rag_read', 'sources.read', 'evidence.read',
         'code.read', 'code.locate'}
LABELS = {'director': '研究总监', 'engineer': '实验工程师', 'scout': '文献研究员'}
FIELD_NAMES = {'task': '当前任务', 'objective': '研究目标', 'scientific_state': '科学状态',
               'document_working_notes': '文档工作笔记', 'source_document': '来源文档',
               'available_context_references': '归档记忆目录', 'research_state': '研究状态',
               'implementation_revision_memory': '修订记忆', 'reading_state': '阅读进度',
               'repair_packet_view': '代码修复记忆', 'amendment_source': '修订来源',
               'indexed_documents': '文档索引', 'working_state': '工作记忆状态',
               'document_index': '文档目录', 'sources': '来源目录', 'summary': '摘要',
               'units': '任务单元', 'documents': '关联文档', 'required_changes': '修改要求',
               'read': '读取定位', 'source': '来源', 'findings': '已保存发现',
               'version': '版本', 'research_goal': '研究目标', 'stage': '阶段', 'research_cycle': '研究轮次',
               'document_id': '文档标识', 'sections': '章节目录', 'sha256': '版本摘要',
               'total_chars': '正文长度', 'offset': '读取位置', 'limit': '读取长度',
               'expected_sha256': '指定版本', 'section_id': '章节', 'id': '标识', 'query': '检索式'}
MAX_NODES = 160
PROJECTION_VERSION = 4
_CACHE = OrderedDict()
_CACHE_LOCK = Lock()


def pointer(path, key):
    return path + '/' + str(key).replace('~', '~0').replace('/', '~1')


def node_id(path):
    return 'n-' + hashlib.sha256(path.encode()).hexdigest()[:20]


def build_tree(snapshot, *, clean):
    """Project a single role/snapshot. Audit data never enters the model prompt."""
    active = (snapshot.get('model_input_capture') or {}).get('redacted_active_context', snapshot.get('active_context')) or {}
    context = _resolve(active.get('context', {}), active)
    context = context if isinstance(context, dict) else {}
    role = snapshot['role']
    nodes, paths = [], []
    omitted = 0

    def add(path, parent, level, title, value=None, state='retained', *, source_id=None,
            detail_path=None, metadata=None):
        nonlocal omitted
        if len(nodes) >= MAX_NODES:
            omitted += 1
            return None
        value = _resolve(value, active)
        # Preview only a few scalar fields; never stringify a huge context.
        if isinstance(value, dict):
            preview = next((_resolve(value[k], active) for k in ('summary', 'query', 'title', 'text', 'quote', 'content', 'reason', 'task', 'path')
                            if isinstance(_resolve(value.get(k), active), str)), None)
            if preview is None:
                preview = ' · '.join(FIELD_NAMES.get(k, str(k)) for k in list(value)[:8])
        elif isinstance(value, list):
            preview = f'{len(value)} 条已保存记录'
        else:
            preview = value
        identifier = node_id(path)
        summary = _snippet(preview, clean, 240) if preview is not None else ''
        nodes.append({'id': identifier, 'parent_id': parent, 'level': level, 'title': str(clean(title))[:90],
                      'summary': summary[:239] + '…' if len(summary) > 240 else summary,
                      'state': state, 'source_id': source_id,
                      'detail_path': detail_path, 'metadata': clean(metadata or {})})
        return identifier

    root = add('agent', None, 0, LABELS.get(role, role), state='context')
    current = add('context', root, 1, '当前 Agent 上下文', active.get('task'), 'context',
                  detail_path='/task', metadata={'snapshot_id': snapshot['id'],
                  'operation_id': snapshot.get('operation_id'), 'tokens': snapshot.get('active_tokens_estimate'),
                  'method': snapshot.get('method')})

    def group(key, title, value):
        return add(key, current, 2, title, value, 'context')

    # L2 directories -> L3 saved records -> L4 exact content or archive locator.
    working = [(k, v, pointer('/context', k)) for k, v in context.items()
               if not k.startswith('_') and k not in {'tool_results', 'available_context_references'}
               and (k in FIELD_NAMES or isinstance(_resolve(v, active), (dict, list)))]
    working.insert(0, ('objective', active.get('objective', ''), '/objective'))
    branch = group('working', '工作记忆', working)
    for key, value, path in working[:6]:
        parent = add(path, branch, 3, FIELD_NAMES.get(key, key), value, detail_path=path)
        resolved = _resolve(value, active)
        if isinstance(resolved, dict):
            for name in list(resolved)[:3]:
                child_path = pointer(path, name)
                add(child_path, parent, 4, FIELD_NAMES.get(name, name), resolved[name], detail_path=child_path)
        elif isinstance(resolved, list):
            for i, child in enumerate(resolved[:3]):
                child = _resolve(child, active)
                title = _scalar(child.get('title', child.get('name')), active) if isinstance(child, dict) else None
                add(pointer(path, i), parent, 4, title or f'记录 {i + 1}', child, detail_path=pointer(path, i))
    omitted += max(0, len(working) - 6)

    turns = _resolve(active.get('recent_turns', []), active)
    if isinstance(turns, list) and turns:
        branch = group('history', '角色历史', turns)
        # The most recent saved history stays visible, in its original order.
        for i in range(max(0, len(turns) - 6), len(turns)):
            row = _resolve(turns[i], active)
            if not isinstance(row, dict):
                continue
            path = pointer('/recent_turns', i)
            content = _resolve(row.get('content', row), active)
            directory = isinstance(content, dict) and content.get('historical_response_directory') is True
            parent = add(path, branch, 3, f'历史记忆 {i + 1}', content,
                         'reference' if directory else 'retained', source_id=_scalar(row.get('id'), active),
                         detail_path=path)
            if directory:
                for j, field in enumerate(content.get('fields', [])[:4]):
                    if isinstance(field, dict):
                        add(path + '/field/' + str(j), parent, 4, str(field.get('path', '归档字段')),
                            '正文保存在归档中，当前输入仅含目录', 'reference',
                            detail_path=path + '/content/fields/' + str(j), metadata=field)
            else:
                add(path + '/body', parent, 4, '历史输出正文', content, detail_path=path + '/content')
        omitted += max(0, len(turns) - 6)

    memories = _resolve(active.get('retrieved_memories', []), active)
    if isinstance(memories, list) and memories:
        branch = group('memories', '检索记忆', memories)
        documents = {}
        for i, row in enumerate(memories[:24]):
            row = _resolve(row, active)
            if not isinstance(row, dict):
                continue
            path = pointer('/retrieved_memories', i)
            document = _scalar(row.get('document_id'), active) or _scalar(row.get('id'), active) or str(i)
            if document not in documents:
                documents[document] = add('document:' + str(document), branch, 3,
                    '记忆来源 ' + str(len(documents) + 1), row.get('metadata'), 'retrieved', source_id=document)
            hit = add(path, documents[document], 4, f'记忆片段 {i + 1}', row, 'retrieved',
                      source_id=_scalar(row.get('id'), active), detail_path=path,
                      metadata={'score': row.get('score'), 'sha256': (row.get('metadata') or {}).get('text_sha256')})
            paths.append({'kind': 'implicit_retrieval', 'nodes': [root, current, branch, documents[document], hit]})
        omitted += max(0, len(memories) - 24)

    rows = _resolve(context.get('tool_results', []), active)
    calls = []
    for i, raw in enumerate(rows if isinstance(rows, list) else []):
        row = _resolve(raw, active)
        call = _resolve(row.get('call', {}), active) if isinstance(row, dict) else {}
        if isinstance(call, dict) and _scalar(call.get('name'), active) in TOOLS:
            calls.append((i, row, call))
    if calls:
        branch = group('tools', '检索与展开', calls)
        # Keep the latest operations visible when the UI folds older siblings.
        # Original receipt indices retain the saved order for provenance.
        for i, row, call in reversed(calls[-10:]):
            path = f'/context/tool_results/{i}'
            name = _scalar(call.get('name'), active)
            result = _resolve(row.get('result'), active)
            failed = isinstance(result, dict) and (result.get('error') or result.get('status') in {'failed', 'error'})
            parent = add(path, branch, 3, name, call.get('arguments'), 'unavailable' if failed else 'retrieved', detail_path=path,
                         metadata={'tool': name, 'arguments': _snippet(call.get('arguments'), clean, 900), 'order': i})
            if failed:
                nodes[-1]['summary'] = '工具返回失败，未取得可用记忆内容'
                continue
            result_path = path + '/result'
            hits = result
            if isinstance(result, dict):
                field = next((key for key in ('passages', 'results', 'matches', 'memories', 'chunks', 'items')
                              if isinstance(result.get(key), list)), None)
                if field:
                    hits, result_path = result[field], pointer(result_path, field)
            if not isinstance(hits, list):
                hits = [result]
                hit_paths = [result_path]
            else:
                hit_paths = [pointer(result_path, j) for j in range(min(6, len(hits)))]
            if not hits:
                nodes[-1]['summary'] = '本次检索没有命中'
            for j, (hit, hit_path) in enumerate(zip(hits[:6], hit_paths)):
                hit = _resolve(hit, active)
                source = next((_scalar(hit.get(k), active) for k in ('id', 'source_id', 'document_id', 'receipt_id')
                               if hit.get(k)), None) if isinstance(hit, dict) else None
                is_ref = isinstance(hit, dict) and (hit.get('primary_text_displayed') is False
                    or hit.get('quote_displayed') is False or hit.get('historical_details_displayed') is False
                    or bool(hit.get('consumed_read_reference')) or hit.get('text_returned') is False
                    or bool(hit) and set(hit) <= {'id', 'source_id', 'document_id', 'receipt_id', 'sha256', 'score', 'title', 'locator'})
                leaf = add(hit_path, parent, 4, ('归档引用' if is_ref else '命中内容') + f' {j + 1}', hit,
                           'reference' if is_ref else 'retrieved', source_id=source, detail_path=hit_path)
                paths.append({'kind': name, 'nodes': [root, current, branch, parent, leaf], 'order': i})
            omitted += max(0, len(hits) - 6)
        omitted += max(0, len(calls) - 10)

    refs = _resolve(context.get('available_context_references', []), active)
    capture = snapshot.get('model_input_capture') or {}
    excluded = [key for key in capture.get('retrieved_memory_ids', [])
                if key not in set(capture.get('effective_memory_ids', []))]
    if (isinstance(refs, list) and refs) or excluded:
        branch = group('archive', '归档与未纳入', None)
        for i, row in enumerate(refs[:8] if isinstance(refs, list) else []):
            path = f'/context/available_context_references/{i}'
            row = _resolve(row, active)
            add(path, branch, 3, '归档引用 ' + str(i + 1), row, 'reference', detail_path=path)
        for i, key in enumerate(excluded[:8]):
            add('excluded:' + str(key), branch, 3, '检索后未纳入 ' + str(i + 1),
                '该记忆曾被召回，但未保留在此请求的输入中。', 'omitted', source_id=key)
        omitted += max(0, len(refs) - 8) if isinstance(refs, list) else 0
        omitted += max(0, len(excluded) - 8)

    return clean({'schema_version': 1, 'projection_version': PROJECTION_VERSION, 'id': snapshot['id'], 'run_id': snapshot['run_id'], 'role': role,
                  'snapshot_id': snapshot['id'], 'operation_id': snapshot.get('operation_id'),
                  'created_at': snapshot.get('created_at'), 'context_sha256': capture.get('user_sha256'),
                  'provenance': 'captured_context', 'levels': ['Agent', '当前上下文', '记忆目录', '记忆记录', '内容与来源'],
                  'nodes': nodes, 'paths': [p for p in paths if all(p['nodes'])], 'omitted_count': omitted,
                  'notice': '层级表示已保存输入的目录与来源关系；光效回放已记录路径，不代表模型内部注意力或逐层检索耗时。'})


def tree_view(store, run_id, *, role, clean, snapshot_id=None):
    from .memory_views import _input_parts, _owned, _request_summary
    if role not in ROLES:
        raise ValueError('Unknown memory role')
    if not snapshot_id:
        with store._read_db() as db:
            row = db.execute("SELECT json_extract(data,'$.snapshot_id') FROM records "
                             "WHERE collection='contexts' AND key=? AND json_extract(data,'$.run_id')=?",
                             (run_id + ':' + role, run_id)).fetchone()
        snapshot_id = row[0] if row else None
    if not snapshot_id:
        return {'schema_version': 1, 'run_id': run_id, 'role': role, 'snapshot_id': None,
                'nodes': [], 'paths': [], 'omitted_count': 0}
    # New requests have a small atomically saved projection, with no full input read.
    with store._read_db() as db:
        row = db.execute("SELECT data FROM records WHERE collection='memory_trees' AND key=? "
                         "AND json_extract(data,'$.run_id')=? AND json_extract(data,'$.role')=?",
                         (snapshot_id, run_id, role)).fetchone()
    if row:
        tree = json.loads(row[0])
        if tree.get('projection_version') == PROJECTION_VERSION:
            return clean(tree)
    with store._read_db() as db:
        stamp = db.execute("SELECT updated_at FROM records WHERE collection='context_snapshots' AND key=? "
                           "AND json_extract(data,'$.run_id')=? AND json_extract(data,'$.role')=?",
                           (snapshot_id, run_id, role)).fetchone()
    if not stamp:
        raise KeyError('Snapshot not found for this role')
    cache_key = (str(store.path), run_id, role, snapshot_id, stamp[0], PROJECTION_VERSION)
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
    if cached:
        return clean(cached)
    snapshot = _owned(store, 'context_snapshots', snapshot_id, run_id)
    if snapshot.get('role') != role:
        raise KeyError('Snapshot belongs to another role')
    # Validate capture bytes before projecting legacy inputs; no guessed events.
    summary = _request_summary('context_snapshots', snapshot_id, snapshot)
    _, _, _, active = _input_parts(summary, snapshot)
    tree = build_tree({**snapshot, 'active_context': active or {}}, clean=clean)
    tree['provenance'] = 'legacy_snapshot'
    with _CACHE_LOCK:
        _CACHE[cache_key] = tree
        while len(_CACHE) > 8:
            _CACHE.popitem(last=False)
    return tree


def node_view(store, run_id, snapshot_id, node, *, role, clean, offset=0, limit=12000):
    from .memory_views import _input_parts, _owned, _request_summary
    tree = tree_view(store, run_id, role=role, clean=clean, snapshot_id=snapshot_id)
    selected = next((item for item in tree['nodes'] if item['id'] == node), None)
    if not selected:
        raise KeyError('Memory node not found')
    path = selected.get('detail_path')
    if not path:
        return _text_page(selected.get('summary', ''), offset=offset, limit=limit, node=selected, source='tree_metadata')
    snapshot = _owned(store, 'context_snapshots', snapshot_id, run_id)
    if snapshot.get('role') != role:
        raise KeyError('Snapshot belongs to another role')
    _, _, _, active = _input_parts(_request_summary('context_snapshots', snapshot_id, snapshot), snapshot)
    value = active
    for key in path[1:].split('/'):
        value = _resolve(value, active)
        key = key.replace('~1', '/').replace('~0', '~')
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif isinstance(value, list) and key.isdecimal() and int(key) < len(value):
            value = value[int(key)]
        else:
            raise KeyError('Captured memory path unavailable')
    value = _resolve(value, active)
    if isinstance(value, dict):
        # Resolve the selected record's shared text/record fields, not the whole
        # context. This exposes its saved body and exact identifiers on demand.
        value = {key: _resolve(child, active) for key, child in value.items()}
    value = clean(value)
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    return _text_page(text, offset=offset, limit=limit, node=selected, source='captured_context', redacted=True)
