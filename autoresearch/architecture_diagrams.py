"""Agent-authored presentation diagrams, pinned to immutable architecture sources.

These jobs write only diagrams and receipts. They never approve a design, change
the research cursor, or authorize experiments.
"""
from __future__ import annotations

import asyncio
import copy
import json

from .prose_schema import validate as validate_schema

from .storage import now, redact

COLLECTION = 'architecture_diagrams'
VERSION = 1
AUTHORING_VERSION = 2
TOOL = 'architecture.draw_diagrams'
HELP = ('arguments={architecture_document_id:string,architecture_document_sha256:string,'
    'network:{title,nodes,edges,paragraphs},flowchart:{title,nodes,edges,paragraphs},limitations:string[]} '
    'validates the engineer-authored graphs against the current approved architecture and this handoff\'s '
    'actual documents.read receipts, renders two standalone SVGs, and saves immutable SVG/JSON artifacts '
    'plus a separate explanations.html document. Long explanations stay outside the scientific figure. '
    'Only this engineer\'s version-bound diagram authoring handoff may call it. Each node has '
    'id,label,detail,lines,kind,layer,order,sources; each edge has source,target,label; '
    'each paragraph has text,sources; sources contain document_id,section_id,char_start,char_end. '
    'Optional node visual (block,tensor,mlp,conv,spectral,clock,normalization,parameter,merge) '
    'and panel_id select scientific module illustrations; graph panels contain id,title,sources. '
    'Network publication layout is automatic: modules, internal illustrations and reference badges '
    'are compiled from the semantic graph, preserving every original node/edge in SVG metadata. '
    'Optional network publication_scene can control source-mapped modules, connections and insets. '
    'Returns saved, submission IDs/hashes, artifact paths/hashes and presentation-only provenance. '
    'Never approves science, writes implementation code, or executes experiments.')


def schema():
    from .engine import obj, STR
    anchor = obj({'document_id': STR, 'section_id': STR,
        'char_start': {'type': 'integer', 'minimum': 0}, 'char_end': {'type': 'integer', 'minimum': 1}})
    sources = {'type': 'array', 'items': anchor, 'minItems': 1, 'maxItems': 4}
    identifier = {'type': 'string', 'pattern': '^[a-zA-Z][a-zA-Z0-9_-]{0,47}$'}
    node = obj({'id': identifier,
        'label': {'type': 'string', 'minLength': 1, 'maxLength': 60},
        'detail': {'type': 'string', 'minLength': 1, 'maxLength': 800},
        'lines': {'type': 'array', 'items': {'type': 'string', 'maxLength': 70}, 'maxItems': 3},
        'kind': {'type': 'string', 'enum': ['input', 'layer', 'operation', 'merge', 'decision', 'output', 'stop']},
        'layer': {'type': 'integer', 'minimum': 0, 'maximum': 16},
        'order': {'type': 'integer', 'minimum': 0, 'maximum': 8}, 'sources': sources,
        'visual': {'type': 'string', 'enum': ['block', 'tensor', 'mlp', 'conv', 'spectral',
            'clock', 'normalization', 'parameter', 'merge']}, 'panel_id': identifier},
        required=['id', 'label', 'detail', 'lines', 'kind', 'layer', 'order', 'sources'])
    edge = obj({'source': STR, 'target': STR, 'label': {'type': 'string', 'maxLength': 60}})
    paragraph = obj({'text': {'type': 'string', 'minLength': 1, 'maxLength': 1800}, 'sources': sources})
    panel = obj({'id': identifier, 'title': {'type': 'string', 'minLength': 1, 'maxLength': 100},
        'sources': sources})
    module = obj({'id': identifier, 'title': {'type': 'string', 'minLength': 1, 'maxLength': 100},
        'node_ids': {'type': 'array', 'items': identifier, 'minItems': 1, 'maxItems': 32},
        'visual': node['properties']['visual'],
        'row': {'type': 'integer', 'minimum': 0, 'maximum': 8},
        'column': {'type': 'integer', 'minimum': 0, 'maximum': 8}},
        required=['id', 'title', 'node_ids', 'visual'])
    connection = obj({'source': identifier, 'target': identifier,
        'edge_indices': {'type': 'array', 'items': {'type': 'integer', 'minimum': 0, 'maximum': 63},
            'minItems': 1, 'maxItems': 64},
        'display': {'type': 'string', 'enum': ['arrow', 'badge']},
        'label': {'type': 'string', 'maxLength': 100}})
    inset = obj({'title': {'type': 'string', 'minLength': 1, 'maxLength': 100},
        'module_ids': {'type': 'array', 'items': identifier, 'minItems': 1, 'maxItems': 32}})
    scene = obj({'version': {'type': 'integer', 'const': 1},
        'modules': {'type': 'array', 'items': module, 'minItems': 2, 'maxItems': 32},
        'connections': {'type': 'array', 'items': connection, 'minItems': 1, 'maxItems': 64},
        'insets': {'type': 'array', 'items': inset, 'maxItems': 8}})
    graph = obj({'title': {'type': 'string', 'minLength': 1, 'maxLength': 100},
        'nodes': {'type': 'array', 'items': node, 'minItems': 2, 'maxItems': 32},
        'edges': {'type': 'array', 'items': edge, 'minItems': 1, 'maxItems': 64},
        'paragraphs': {'type': 'array', 'items': paragraph, 'minItems': 1, 'maxItems': 6},
        'panels': {'type': 'array', 'items': panel, 'minItems': 1, 'maxItems': 8},
        'publication_scene': scene},
        required=['title', 'nodes', 'edges', 'paragraphs'])
    flowchart = copy.deepcopy(graph)
    flowchart['properties'].pop('publication_scene')
    return obj({'architecture_document_id': STR,
        'architecture_document_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
        'network': graph, 'flowchart': flowchart,
        'limitations': {'type': 'array', 'items': {'type': 'string', 'maxLength': 600}, 'maxItems': 8}})


INSTRUCTION = ('依据当前架构 Markdown 和明确绑定的继承规范，自动绘制两张图并写段落解释。'
    'network 是模型本身的神经网络结构图：表达输入、张量轴、层宽、分支、共享参数、跳连、合并及输出；'
    'flowchart 是数据、模型前向、训练验证、冻结与独立测试的流程图，标出文档规定的条件和失败路径。'
    '返回语义节点和连线，由页面绘制矢量图；不要返回 SVG、HTML、图片网址或可执行代码。'
    '每张图附 1–6 个自然中文段落，解释模块作用、数据流和约束，不把未执行计划写成实验结果。'
    '使用 indexed_documents 的目录定位，再 documents.read/search 定向阅读实际相关正文。'
    '每个节点、每段解释的 sources 必须指向本次交接真实读到的同 SHA 章节绝对字符区间。'
    'layer 表示布局的逻辑层级，order 表示同层分支序号，均从 0 开始；同层节点不得重叠。'
    '不要按网络风格凭空增加全连接层、激活、跳连或其他文档未定义模块；图中层宽必须与规范一致。'
    '互斥候选不得混成并行支路。未定义连接或轴须放在 limitations，不能补造默认值。'
    '这只是架构展示，不修改科学方案、不重新审批、不写实现代码，也不运行实验。')

TOOL_INSTRUCTION = (INSTRUCTION +
    '\n采用科研架构图的分区与模块图示：依据已读来源声明 panels，各节点用 panel_id 归属功能区域，'
    '用 visual 指定张量、MLP 多层网络、卷积特征块、频谱或时钟等内部示意。'
    'MLP 层宽、卷积设置和张量轴以来源文字标注为准；示意圆点及方块不代表实际单元个数。'
    '把互斥候选放在有清晰选择关系的独立小区，未实现或未定义接口仅画接口，不展开假想内部结构。'
    '工具会自动把真实结构编译为科研主图，合并连续细节、展开对应模块图案并整理跨区连接。'
    '图内仅保留模块名称、必要维度和结构连接；长段解释由工具保存到独立说明文档。'
    '需要精细版式时可提供 publication_scene：modules 用 node_ids 精确映射原节点，'
    'connections 用 edge_indices 精确映射原连线，insets 表示局部区域；不能遗漏、复制或改变原连接。'
    '主图中的共享引用可显示为配对 badge；互斥选择仍须有明确的选择模块与路径标注。'
    '\n实际绘图必须调用 architecture.draw_diagrams：把两图语义结构和来源锚点作为完整 arguments 提交，'
    '由工具校验并生成、保存两张 SVG 图片及图结构 JSON。不要只在最终答案返回图结构。'
    '先完成必要的 documents.read；工具拒绝时根据实际错误修正或补读。'
    '工具 saved=true 后，最终仅提交它实际返回的 diagram_id、document_id、document_sha256、bundle_hash。'
    '不得捏造保存回执、哈希、图片路径或已调用工具。')


def legacy_schema():
    """Keep protocol-1 output identities unchanged as tool scenes evolve."""
    result = copy.deepcopy(schema())
    result['properties'] = {name: result['properties'][name] for name in (
        'architecture_document_id', 'architecture_document_sha256', 'network', 'flowchart', 'limitations')}
    result['required'] = list(result['properties'])
    for name in ('network', 'flowchart'):
        graph = result['properties'][name]
        graph['properties'] = {field: graph['properties'][field] for field in (
            'title', 'nodes', 'edges', 'paragraphs')}
        graph['required'] = list(graph['properties'])
        node = graph['properties']['nodes']['items']
        node['properties'] = {field: node['properties'][field] for field in (
            'id', 'label', 'detail', 'lines', 'kind', 'layer', 'order', 'sources')}
        node['required'] = list(node['properties'])
    return result


def submission_schema():
    from .engine import obj, STR
    sha = {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}
    return obj({'diagram_id': STR, 'document_id': STR, 'document_sha256': sha, 'bundle_hash': sha})


def submission(row):
    return {'diagram_id': row['id'], 'document_id': row['architecture_document_id'],
        'document_sha256': row['document_sha256'], 'bundle_hash': row['bundle_hash']}


def _key(run_id, sha):
    return f'{run_id}:architecture-diagrams:{VERSION}:{sha}'


def _job_key(engine, run_id, sha):
    key = _key(run_id, sha)
    old = engine.store.get(COLLECTION, key)
    if old and old.get('cycle') != engine.cycle(run_id) and old.get('status') != 'completed':
        # Retain old paid inputs and unfinished receipts under their original
        # identity. A new cycle receives its own assignment for the same SHA.
        return f'{key}:cycle:{engine.cycle(run_id)}'
    return key


def current_review(engine, run_id):
    rows = [row for row in engine.store.list('architecture_reviews', run_id)
            if row.get('cycle') == engine.cycle(run_id)]
    return max(rows, key=lambda row: row.get('created_at', ''), default=None)


def document_review(engine, run_id, document_id, expected_sha256, *, historical=False):
    rows = engine.store.list('architecture_reviews', run_id) if historical else [current_review(engine, run_id)]
    matches = [row for row in rows if row and row.get('run_id') == run_id
               and row.get('document', {}).get('document_id') == document_id
               and row['document'].get('sha256') == expected_sha256]
    if not matches:
        raise ValueError('未找到本任务中与指定版本一致的架构文档。')
    return max(matches, key=lambda row: row.get('created_at', ''))


def saved_record(engine, run_id, document_id, expected_sha256, *, historical=False):
    if not historical:
        return engine.store.get(COLLECTION, _job_key(engine, run_id, expected_sha256))
    with engine.store._read_db() as db:
        row = db.execute("SELECT key FROM records WHERE collection=? AND json_extract(data,'$.run_id')=? "
            "AND json_extract(data,'$.architecture_document_id')=? AND json_extract(data,'$.document_sha256')=? "
            "ORDER BY (json_extract(data,'$.status')='completed') DESC,updated_at DESC LIMIT 1",
            (COLLECTION, run_id, document_id, expected_sha256)).fetchone()
    return engine.store.get(COLLECTION, row[0]) if row else None


def view(engine, run_id, document, *, historical=False):
    row = saved_record(engine, run_id, document['document_id'], document['sha256'], historical=historical)
    if not row or row.get('architecture_document_id') != document['document_id']:
        return {'status': 'pending', 'document_sha256': document['sha256']}
    result = {key: copy.deepcopy(row[key]) for key in (
        'id', 'status', 'architecture_document_id', 'document_sha256', 'bundle',
        'bundle_hash', 'provenance', 'handoff_id', 'audit', 'rendering', 'artifacts', 'tool_receipt',
        'error', 'created_at', 'updated_at') if key in row}
    if result['status'] == 'drawn':
        result['status'] = 'generating'  # Images saved; the agent is still finalizing its submission.
    result['documents'] = copy.deepcopy(row['context']['indexed_documents'])
    if historical:
        result['historical'] = True
        result['source_cycle'] = row.get('cycle')
    return result


def queue(engine, run_id, document_id=None, expected_sha256=None, *, retry=False):
    from . import document_index
    if engine.store.get('runs', run_id)['mode'] != 'live':
        raise ValueError('演示任务不调用真实 agent 绘图。')
    review = current_review(engine, run_id)
    if not review:
        raise ValueError('当前轮次尚无架构文档，不能绘图。')
    doc = review['document']
    if (document_id is not None and document_id != doc['document_id']
            or expected_sha256 is not None and expected_sha256 != doc['sha256']):
        raise ValueError('架构版本已经变化，请重新读取详情。')
    key = _job_key(engine, run_id, doc['sha256'])
    old = engine.store.get(COLLECTION, key)
    if old and (not retry or old['status'] != 'failed'):
        return view(engine, run_id, doc)
    if review.get('status') != 'approved':
        raise ValueError('当前架构尚未批准；通过后才安排 agent 调用绘图工具。')
    documents = {doc['document_id']: doc}
    # Bind only the effective implementation contract and its explicit parents.
    design = engine.outputs(run_id).get('design', {})
    binding = design.get('resource_plan', {}).get('approved_definition_binding', {})
    references = ([binding] if binding.get('document_id') else []) + binding.get('inheritance', [])
    for reference in references:
        directory = document_index.directory(engine, run_id, reference['document_id'], reference['expected_sha256'])
        documents[directory['document_id']] = directory
    context = {'architecture_diagram_authoring_protocol': AUTHORING_VERSION, 'research_cycle': engine.cycle(run_id),
        'document_index_protocol': 1, 'document_read_window_protocol': 1,
        'indexed_document_context': True, 'architecture_workflow_protocol': 1,
        'architecture_document': copy.deepcopy(doc), 'indexed_documents': list(documents.values()),
        'diagram_contract': {'architecture_document_id': doc['document_id'],
            'architecture_document_sha256': doc['sha256'], 'scientific_state_changes_allowed': False},
        'diagram_tool_arguments_schema': schema()}
    attempt = (old or {}).get('attempt', 0) + 1
    row = {'id': key, 'run_id': run_id, 'cycle': engine.cycle(run_id), 'status': 'queued',
        'attempt': attempt, 'architecture_document_id': doc['document_id'], 'document_sha256': doc['sha256'],
        'context': context, 'created_at': (old or {}).get('created_at', now()), 'updated_at': now(),
        'authoring_instruction': TOOL_INSTRUCTION, 'output_schema': submission_schema(),
        'provenance': 'agent_architecture_diagrams', 'error': None}
    created = False
    def claim(existing):
        nonlocal created
        # The workflow and a detail panel may queue the same version together.
        # Freeze only one author assignment; never overwrite its active receipt.
        if existing and (not retry or existing['status'] != 'failed'):
            return existing
        created = True
        return {**row, 'attempt': (existing or {}).get('attempt', 0) + 1,
            'created_at': (existing or {}).get('created_at', row['created_at'])}
    engine.store.mutate(COLLECTION, key, claim)
    if created:
        engine.store.event(run_id, 'architecture.diagrams_queued', '已安排实验工程师绘制网络图、流程图及段落解释。',
            {'diagram_id': key, 'document_id': doc['document_id'], 'document_sha256': doc['sha256']})
    return view(engine, run_id, doc)


def validate_bundle(engine, run_id, bundle, context, receipts):
    validate_schema(bundle, context.get('diagram_tool_arguments_schema') or schema())
    json.dumps(bundle, ensure_ascii=False, allow_nan=False)
    contract = context['diagram_contract']
    if any(bundle[key] != contract[key] for key in contract if key in bundle):
        raise ValueError('绘图提交未绑定当前架构文档版本。')
    docs = {doc['document_id']: doc for doc in context['indexed_documents']}
    from .document_index import load
    originals = {identifier: load(engine, run_id, identifier, doc['sha256'])[1] for identifier, doc in docs.items()}
    def check(anchor):
        doc = docs.get(anchor['document_id'])
        start, end = anchor['char_start'], anchor['char_end']
        section = next((section for section in (doc or {}).get('sections', []) if section['id'] == anchor['section_id']), None)
        if not section or not section['start'] <= start < end <= section['end']:
            raise ValueError('绘图来源范围不属于绑定的架构章节。')
        spans = []
        for receipt in receipts:
            page = receipt.get('result', {})
            if (receipt.get('call', {}).get('name') == 'documents.read'
                    and page.get('document_id') == doc['document_id'] and page.get('sha256') == doc['sha256']
                    and page.get('section_id') == section['id'] and isinstance(page.get('text'), str)
                    and originals[doc['document_id']][page.get('char_start', 0):page.get('char_end', 0)] == page['text']):
                spans.append((page['char_start'], page['char_end']))
        covered = start
        for lo, hi in sorted(spans):
            if lo > covered:
                break
            covered = max(covered, hi)
        if covered < end:
            raise ValueError('绘图节点或解释引用了本次 agent 未实际读取的范围。')
    for name in ('network', 'flowchart'):
        graph = bundle[name]
        panels = graph.get('panels', [])
        panel_ids = [panel['id'] for panel in panels]
        if len(set(panel_ids)) != len(panel_ids):
            raise ValueError('绘图功能区域编号重复。')
        if any(node.get('panel_id') is not None and node['panel_id'] not in panel_ids for node in graph['nodes']):
            raise ValueError('绘图节点引用了未定义的功能区域。')
        ids = [node['id'] for node in graph['nodes']]
        if len(set(ids)) != len(ids) or len({(node['layer'], node['order']) for node in graph['nodes']}) != len(ids):
            raise ValueError('绘图节点编号或布局位置重复。')
        degrees = {identifier: 0 for identifier in ids}
        adjacency = {identifier: [] for identifier in ids}
        edge_ids = set()
        for edge in graph['edges']:
            source, target = edge['source'], edge['target']
            if source not in degrees or target not in degrees or source == target or (source, target) in edge_ids:
                raise ValueError('绘图连线端点、重复或自环不合法。')
            edge_ids.add((source, target)); degrees[source] += 1; degrees[target] += 1
            adjacency[source].append(target)
        if any(value == 0 for value in degrees.values()):
            raise ValueError('绘图包含未连接的模块。')
        if name == 'network':
            visiting, done = set(), set()
            def walk(identifier):
                if identifier in visiting:
                    raise ValueError('网络前向连接形成未解释的循环；递推应封装为模块。')
                if identifier in done:
                    return
                visiting.add(identifier)
                for target in adjacency[identifier]:
                    walk(target)
                visiting.remove(identifier); done.add(identifier)
            for identifier in ids:
                walk(identifier)
        if 'publication_scene' in graph:
            from .architecture_diagram_svg import validate_publication_scene
            validate_publication_scene(graph, graph['publication_scene'])
        for item in [*graph['nodes'], *graph['paragraphs'], *panels]:
            for anchor in item['sources']:
                check(anchor)
    return {'status': 'validated', 'scope': '版本、连接、布局与实际阅读来源校验；不代表科学审批或实现验收。',
        'scientific_approval': False, 'source_document_count': len(docs)}


def _assignment(engine, run_id, role, handoff_id):
    from .engine import ResearchBlocked
    assignment = engine.store.get('handoffs', handoff_id or '', {})
    inputs = engine.store.get('handoff_inputs', handoff_id or '', {})
    context = inputs.get('context', {})
    contract = context.get('diagram_contract', {})
    row = engine.store.get(COLLECTION, _job_key(engine, run_id, contract.get('architecture_document_sha256', '')), {})
    current = current_review(engine, run_id)
    if (role != 'engineer' or assignment.get('to_role') != role
            or assignment.get('run_id') != run_id or inputs.get('run_id') != run_id
            or assignment.get('purpose') != f'architecture_diagram_author:{row.get("document_sha256")}:{row.get("attempt")}'
            or context.get('architecture_diagram_authoring_protocol') != AUTHORING_VERSION
            or context.get('research_cycle') != engine.cycle(run_id)
            or engine.store.get('runs', run_id, {}).get('mode') != 'live'
            or row.get('run_id') != run_id or row.get('cycle') != engine.cycle(run_id)
            or row.get('handoff_id') != handoff_id or row.get('context') != context
            or not current or current.get('status') != 'approved'
            or current['document']['document_id'] != contract.get('architecture_document_id')
            or current['document']['sha256'] != contract.get('architecture_document_sha256')):
        raise ResearchBlocked('绘图工具仅限当前工程师同周期、同版本的已批准架构绘图交接。')
    engine.check_active(run_id)
    return row, context


def _checked_saved(engine, run_id, row):
    """Verify the committed local result before idempotent reuse or publication."""
    import hashlib
    from pathlib import Path
    from .engine import digest
    receipt = row.get('tool_receipt', {})
    if (not receipt.get('saved') or receipt.get('submission') != submission(row)
            or digest(row['bundle']) != row['bundle_hash']
            or receipt.get('artifacts') != row.get('artifacts')
            or receipt.get('rendering') != row.get('rendering')):
        raise ValueError('绘图保存回执或图结构哈希不一致。')
    for item in row['artifacts'].values():
        artifact = engine.store.get('artifacts', item['artifact_id'], {})
        path = Path(item['path']).resolve()
        if (artifact.get('run_id') != run_id or artifact.get('sha256') != item['sha256']
                or Path(artifact.get('path', '')).resolve() != path
                or not path.is_relative_to(engine.run_dir(run_id).resolve())
                or hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']):
            raise ValueError('绘图产物文件、归属或哈希不一致。')
    return copy.deepcopy(receipt)


def _explanations_html(bundle):
    """Static, escaped companion notes; no scripts, resources or model calls."""
    from html import escape
    def plain(value):
        return escape(value, quote=True).replace('\r', '').replace('\n', '<br>')
    parts = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8">',
        '<title>架构图与流程图说明</title><style>body{font:16px/1.7 Arial,"Microsoft YaHei",sans-serif;'
        'max-width:960px;margin:40px auto;padding:0 24px;color:#25344a;background:#fff}'
        'h2{margin-top:40px}h3{margin-bottom:6px}small{color:#65748a}li{margin:4px 0}</style><body>',
        '<h1>架构图与流程图说明</h1><p>图内展示结构与关键维度；完整解释、模块标注与来源列在此处。</p>',
        '<small>架构文档：' + plain(bundle['architecture_document_id']) +
        '<br>SHA256：' + plain(bundle['architecture_document_sha256']) + '</small>']
    for kind, heading in (('network', '网络结构图'), ('flowchart', '流程图')):
        graph = bundle[kind]
        parts.append(f'<h2>{heading}：{plain(graph["title"])}</h2>')
        for paragraph in graph['paragraphs']:
            parts.append(f'<p>{plain(paragraph["text"])}</p>')
        for node in graph['nodes']:
            parts.append(f'<h3>{plain(node["label"])}</h3><p>{plain(node["detail"])}</p><ul>')
            parts.extend(f'<li>{plain(line)}</li>' for line in node['lines'])
            parts.append('</ul><small>来源：' + '；'.join(
                plain(f'{source["document_id"]} / {source["section_id"]} / '
                      f'[{source["char_start"]}, {source["char_end"]})') for source in node['sources']) + '</small>')
    if bundle['limitations']:
        parts.append('<h2>图示范围</h2><ul>')
        parts.extend(f'<li>{plain(item)}</li>' for item in bundle['limitations'])
        parts.append('</ul>')
    return ''.join(parts) + '</body></html>'


def render_bundle(bundle):
    """The tool's shared, deterministic publication rendering pipeline.

    Source approval/read gates belong to ``draw``. This function compiles layout
    from the semantic graph without changing any scientific node or connection.
    """
    from .architecture_diagram_svg import compile_publication_scene, render_svg, validate_publication_scene
    network = copy.deepcopy(bundle['network'])
    explicit = 'publication_scene' in network
    scene = network.get('publication_scene') if explicit else compile_publication_scene(network)
    validate_publication_scene(network, scene)
    rendering = {'style': 'scientific-publication', 'renderer_version': 1,
        'network_scene_source': 'agent_declared' if explicit else 'tool_compiled',
        'network_scene': scene, 'flowchart_layout': 'full',
        'original_node_count': len(network['nodes']), 'original_edge_count': len(network['edges']),
        'symbolic_glyphs': True, 'explanations_location': 'separate_document'}
    return {'network': render_svg(network, publication=True),
        'flowchart': render_svg(bundle['flowchart'], vertical=True),
        'bundle': json.dumps(bundle, ensure_ascii=False, separators=(',', ':'), allow_nan=False),
        'explanations': _explanations_html(bundle)}, rendering


async def draw(engine, run_id, role, handoff_id, bundle):
    """Render and save through a real Harness tool invocation, with source gates."""
    from .engine import digest
    from pathlib import Path
    row, context = _assignment(engine, run_id, role, handoff_id)
    receipts = engine.store.get('handoff_progress', handoff_id, {}).get('tool_results', [])
    audit = await asyncio.to_thread(validate_bundle, engine, run_id, bundle, context, receipts)
    if row.get('tool_receipt'):
        if row['bundle_hash'] != digest(bundle):
            raise ValueError('本次交接已保存其他图结构；不能覆盖已保存图片。')
        return _checked_saved(engine, run_id, row)

    content, rendering = await asyncio.to_thread(render_bundle, bundle)
    row, _ = _assignment(engine, run_id, role, handoff_id)  # Pause/version guards after CPU work.
    if row.get('tool_receipt'):
        if row['bundle_hash'] != digest(bundle):
            raise ValueError('本次交接已保存其他图结构；不能覆盖已保存图片。')
        return _checked_saved(engine, run_id, row)
    fingerprint = digest(bundle)
    artifacts = {}
    for name, text in content.items():
        extension = {'bundle': 'json', 'explanations': 'html'}.get(name, 'svg')
        # Content-addressed local output makes interrupted partial writes replay-safe.
        # The complete fingerprint remains in the receipt; short directories
        # avoid Windows MAX_PATH failures. Existing content is always verified.
        path = f'architecture-diagrams/{fingerprint[:24]}/{name}.{extension}'
        existing = engine.store.get('artifacts', run_id + ':' + path)
        if existing:
            actual = Path(existing['path']).resolve()
            if (existing.get('run_id') != run_id
                    or not actual.is_relative_to(engine.run_dir(run_id).resolve())
                    or actual.read_bytes() != text.encode('utf-8')):
                raise ValueError('不可覆盖已有绘图产物。')
            artifact = existing
        else:
            artifact = engine.artifact(run_id, path, text, 'architecture_diagram_' + extension)
        artifacts[name] = {'artifact_id': artifact['id'], 'path': artifact['path'],
            'sha256': artifact['sha256'], 'format': extension}
    saved = {**row, 'status': 'drawn', 'bundle': copy.deepcopy(bundle), 'bundle_hash': fingerprint,
        'artifact_id': artifacts['bundle']['artifact_id'], 'artifacts': artifacts,
        'audit': audit, 'rendering': rendering, 'updated_at': now(), 'error': None}
    receipt = {'saved': True, **submission(saved), 'submission': submission(saved),
        'artifacts': artifacts, 'audit': audit, 'rendering': rendering, 'scientific_approval': False,
        'provenance': 'agent_architecture_diagrams'}
    saved['tool_receipt'] = receipt
    _checked_saved(engine, run_id, saved)
    def commit(previous):
        if (not previous or previous.get('handoff_id') != handoff_id
                or previous.get('context') != context or previous.get('attempt') != row['attempt']):
            raise ValueError('绘图交接在保存时发生变化。')
        if previous.get('tool_receipt') and previous.get('bundle_hash') != fingerprint:
            raise ValueError('本次交接已保存其他图结构；不能覆盖已保存图片。')
        return saved if not previous.get('tool_receipt') else previous
    persisted = engine.store.mutate(COLLECTION, row['id'], commit)
    return _checked_saved(engine, run_id, persisted)


async def generate(engine, key):
    from .engine import Harness, digest
    from .config import get_settings
    row = engine.store.get(COLLECTION, key)
    if not row or row['status'] == 'completed':
        return
    run_id, context = row['run_id'], row['context']
    current = current_review(engine, run_id)
    if (row.get('cycle') != engine.cycle(run_id)
            or not current or current.get('status') != 'approved'
            or current['document']['document_id'] != row['architecture_document_id']
            or current['document']['sha256'] != row['document_sha256']):
        engine.store.put(COLLECTION, key, {**row, 'status': 'superseded', 'updated_at': now()})
        return
    try:
        intelligence = engine.intelligence_factory(engine.store, get_settings(engine.store))
        h = Harness(engine, intelligence)
        purpose = f'architecture_diagram_author:{row["document_sha256"]}:{row["attempt"]}'
        tool_authoring = context.get('architecture_diagram_authoring_protocol') == AUTHORING_VERSION
        instruction = row.get('authoring_instruction', TOOL_INSTRUCTION if tool_authoring else INSTRUCTION)
        output_schema = row.get('output_schema', submission_schema() if tool_authoring else legacy_schema())
        handoff_id = row.get('handoff_id')
        if handoff_id:
            frozen = engine.store.get('handoff_inputs', handoff_id, {})
            assignment = engine.store.get('handoffs', handoff_id, {})
            if (frozen.get('run_id') != run_id or frozen.get('context') != context
                    or not isinstance(frozen.get('instruction'), str) or not isinstance(frozen.get('schema'), dict)
                    or assignment.get('run_id') != run_id or assignment.get('to_role') != 'engineer'
                    or assignment.get('purpose') != purpose
                    or h.decision_key(run_id, 'engineer', purpose, frozen['instruction'],
                                      frozen['context'], frozen['schema']) != handoff_id):
                raise ValueError('既有绘图交接缺少匹配的冻结输入；不能重新派发其他版本的请求。')
            instruction, context, output_schema = frozen['instruction'], frozen['context'], frozen['schema']
        else:
            handoff_id = h.decision_key(run_id, 'engineer', purpose, instruction, context, output_schema)
        row = {**row, 'status': 'generating', 'handoff_id': handoff_id, 'updated_at': now(), 'error': None}
        engine.store.put(COLLECTION, key, row)
        result = await h.ask(run_id, 'engineer', purpose, instruction, context, output_schema)
        engine.check_active(run_id)
        receipts = engine.store.get('handoff_progress', handoff_id, {}).get('tool_results', [])
        decision = engine.store.get('decisions', handoff_id, {})
        if decision.get('role') != 'engineer' or decision.get('result') != result:
            raise ValueError('绘图缺少实验工程师的实际交付回执。')
        latest = current_review(engine, run_id)
        if (not latest or latest.get('status') != 'approved'
                or latest['document']['document_id'] != row['architecture_document_id']
                or latest['document']['sha256'] != row['document_sha256']):
            saved = engine.store.get(COLLECTION, key, row)
            engine.store.put(COLLECTION, key, {**saved, 'status': 'superseded', 'updated_at': now()})
            return
        if tool_authoring:
            saved = engine.store.get(COLLECTION, key)
            receipt = _checked_saved(engine, run_id, saved)
            if result != submission(saved) or not any(
                    page.get('call', {}).get('name') == TOOL and page.get('result') == receipt
                    and digest(page['call']['arguments']) == saved['bundle_hash'] for page in receipts):
                raise ValueError('绘图提交必须绑定本次实际工具调用的保存回执。')
            invocations = engine.store.list('tool_invocations', run_id)
            if not any(item.get('handoff_id') == handoff_id and item.get('role') == 'engineer'
                    and item.get('tool') == TOOL and item.get('status') == 'succeeded'
                    and item.get('call', {}).get('arguments') == saved['bundle'] for item in invocations):
                raise ValueError('绘图缺少已成功执行的正式工具调用记录。')
            completed = {**saved, 'status': 'completed', 'updated_at': now()}
        else:
            # Existing paid protocol-1 assignments retain their immutable contracts.
            bundle = result
            audit = await asyncio.to_thread(validate_bundle, engine, run_id, bundle, context, receipts)
            artifact = engine.artifact(run_id, f'architecture-diagrams/{row["document_sha256"]}/{digest(bundle)}.json',
                json.dumps(bundle, ensure_ascii=False, indent=2, allow_nan=False), 'architecture_diagrams')
            completed = {**row, 'status': 'completed', 'bundle': bundle,
                'bundle_hash': digest(bundle), 'artifact_id': artifact['id'], 'audit': audit, 'updated_at': now()}
        engine.store.put(COLLECTION, key, completed)
        engine.store.event(run_id, 'architecture.diagrams_completed', '实验工程师已自动绘制两张图并附段落解释，来源校验通过。',
            {'diagram_id': key, 'handoff_id': handoff_id, 'document_sha256': row['document_sha256']})
    except asyncio.CancelledError:
        if engine.store.get('runs', run_id):
            saved = engine.store.get(COLLECTION, key, row)
            engine.store.put(COLLECTION, key, {**saved, 'status': 'queued', 'updated_at': now()})
        raise
    except Exception as exc:
        run = engine.store.get('runs', run_id)
        if not run:
            return
        if run.get('status') in {'paused', 'blocked'}:
            saved = engine.store.get(COLLECTION, key, row)
            engine.store.put(COLLECTION, key, {**saved, 'status': 'queued', 'updated_at': now()})
            return
        saved = engine.store.get(COLLECTION, key, row)
        engine.store.put(COLLECTION, key, {**saved, 'status': 'failed', 'error': redact(str(exc))[:1000], 'updated_at': now()})
        engine.store.event(run_id, 'architecture.diagrams_failed', '架构图生成或来源校验未完成，原研究流程继续保留。', {'diagram_id': key})


def start_pending(engine):
    tasks = getattr(engine, '_architecture_diagram_tasks', None)
    if tasks is None:
        tasks = engine._architecture_diagram_tasks = {}
    for row in engine.store.list(COLLECTION):
        run = engine.store.get('runs', row['run_id'], {})
        if row['status'] not in {'queued', 'generating', 'drawn'} or run.get('status') != 'running':
            continue
        key = row['id']
        if key not in tasks or tasks[key].done():
            tasks[key] = asyncio.create_task(generate(engine, key))


async def shutdown(engine):
    tasks = list(getattr(engine, '_architecture_diagram_tasks', {}).values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
