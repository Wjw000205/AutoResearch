"""Independent release gate for the document's implementation/review boundaries."""
from __future__ import annotations

from .code_review_partition import source_plan, sha

FIELD = 'implementation_scope_review'
VERSION = 1
AUTHOR_GUIDANCE = (
    '\n当前代码审查要求每个验收 ID 都有相关 Python 行为证据；README/YAML/配置位置可共同映射，'
    '不能单独代替行为源码。释放和写码前须澄清无 Python 行为映射的原义务，保留完整原要求，'
    '不得添加无关锚点或豁免不能实现的要求。'
    '\n先阅读冻结分工中 architecture_sections 指向的已批准架构。按该分工组织实现文档，'
    '每个验收项写清归口及涉及的其他模块。按验收项填写 implementation_map。每行写一个计划相对路径或路径:符号；'
    'behavior 说明该文件的职责，以及对其他模块依赖的入口、输入输出、错误与恢复约束。'
    '保持同一模块和紧耦合调用链完整，写明共享接口及对应的验证。'
    'behavior 开头用 [review-role:director]、[review-role:engineer] 或 [review-role:scout] '
    '标明该路径归属，保持同一模块各处标记一致；按冻结架构职责给出理由。'
    '用 [interface:consumer.py:read_key -> provider.json:output_key] 标记需双端核对的接口，'
    '端点必须对应 implementation_map 中的计划文件，标记使用实际符号或字段名。'
    '段落与表格行保持稳定、唯一的路径或验收项锚点；混合职责拆成可分别定位的断言，'
    'algorithm 的“#### 模块：名称”必须与模块表首列名称逐字相同；模块表单独列出精确计划路径。'
    'implementation_map.behavior 可用 [module-title:名称] 绑定同名正文，不能用近义名称代替。'
    '正文可用 [requirement:验收项ID] 精确引用 implementation_map 的 requirement_id；'
    '若该项涉及多个模块，引用保留所有模块职责，不能用单个 review-role 标记替代。'
    '接口必须写完整 path:symbol 对；来源位置和验收项引用应在对应断言旁明确列出。'
    '共享定义只写一次并由相关断言引用。Harness 按冻结职责派发对应范围，跨角色接口由两端核对，'
    '工程师负责部分另派独立同行，再汇总既定全局一致性检查。'
)
ARCHITECTURE_ROLES = {
    'director': {'sections': ['architecture', 'baseline_params', 'proposed_params', 'ablation_params'],
                 'responsibility': '模型、算法、计算分支与相互依赖'},
    'engineer': {'sections': ['resource_plan', 'iteration_policy', 'search_space'],
                 'responsibility': '运行入口、配置、训练执行、资源和恢复'},
    'scout': {'sections': ['dataset', 'evaluation', 'primary_metric', 'direction', 'acceptance',
                           'benchmark_tasks', 'baseline_mode', 'paper_reference', 'limitations'],
              'responsibility': '数据、指标、证据与验收来源'}
}
ARCHITECTURE_FIELD = 'implementation_architecture_scope'


def before_author(state, common, *, engine=None, run_id=None):
    design = state['common']['design']
    document = common['architecture_document']
    fields = set().union(*(row['sections'] for row in ARCHITECTURE_ROLES.values()))
    sections = {item['title']: item['id'] for item in document['sections']}
    mapping = None
    if engine is not None:
        from .document_index import load
        from .architecture_field_sources import compile_sources
        from .implementation_review_routing import digest
        if digest(design) != common['design_hash']:
            raise ValueError('Architecture field source task design changed')
        _, text = load(engine, run_id, document['document_id'], document['sha256'])
        mapping = compile_sources(design, document, text)
        sections = {field: item['section_id'] for field, item in mapping['fields'].items()}
    body = {'version': VERSION, 'design_sha256': common['design_hash'],
            'architecture_document_sha256': document['sha256'],
            'proposal_sha256': common['proposal_hash'],
            'experiment_plan_sha256': common['experiment_plan_hash'],
            'responsibilities': {role: {**row, 'sections': [field for field in row['sections'] if field in design],
                'architecture_sections': [{'field': field, 'section_id': sections[field]}
                                          for field in row['sections'] if field in design and field in sections]}
                                 for role, row in ARCHITECTURE_ROLES.items()},
            'shared_architecture_sections': sorted(set(design) - fields)}
    if mapping is not None:
        body = apply_field_mapping(body, mapping, design)
    return {**body, 'sha256': sha(body)}


def apply_field_mapping(body, mapping, design):
    """Derived view only; preserve the complete declared responsibility list."""
    import copy
    body = copy.deepcopy({k: v for k, v in body.items() if k != 'sha256'})
    owned = set().union(*(row['sections'] for row in ARCHITECTURE_ROLES.values()))
    body.update(field_sources=mapping, source_mapping_protocol=1, shared_fields=sorted(set(design) - owned))
    body['shared_architecture_sections'] = sorted({title for field in body['shared_fields']
        if field in mapping['fields'] for title in mapping['fields'][field].get('section_titles',
            [mapping['fields'][field]['section_title']])})
    for row in body['responsibilities'].values():
        row['architecture_sections'] = [{'field': field, 'section_id': sid}
            for field in [*row['sections'], *body['shared_fields']] if field in mapping['fields']
            for sid in mapping['fields'][field].get('section_ids', [mapping['fields'][field]['section_id']])]
    return body


def release_contract(state, row):
    """Select the original release envelope; never manufacture legacy ballots.

    Callers must authenticate_release before using this envelope. A completed
    scoped round owns its source contract, including mapping revision provenance.
    Historical rounds continue to use the independently approved legacy record.
    """
    from . import implementation_whole_review as whole
    if whole.enabled(state) and row.get(whole.PROTOCOL) == 1:
        return whole.code_scope(state, row)
    from .implementation_document_review_scope import completed, SLOT
    if completed(state, row):
        from .implementation_acceptance import FIELD as ACCEPTANCE
        rounds = [s for s in state[ACCEPTANCE]['rounds']
                  if s.get(SLOT) and s.get('complete') and s['revision'] == row['index']]
        if len(rounds) != 1:
            raise ValueError('Ambiguous scoped document release')
        return rounds[0][SLOT]['source']
    return state[FIELD]['contract']


def authenticate_release(engine, run_id, state, row, roles):
    from . import implementation_whole_review as whole
    if whole.enabled(state) and row.get(whole.PROTOCOL) == 1:
        return whole.release_scope(engine, run_id, state, row)['plan']
    from .implementation_document_review_scope import completed, authenticate_release as scoped_release
    if completed(state, row):
        return scoped_release(engine, run_id, state, row)
    saved = state.get(FIELD, {})
    frozen = saved.get('contract', {})
    if (not frozen or frozen.get('sha256') != sha({key: val for key, val in frozen.items() if key != 'sha256'})
            or (frozen.get('workflow_id'), frozen.get('spec_hash'), frozen.get('document_sha256')) != (
                state['id'], row['spec_hash'], row['document']['sha256'])
            or frozen.get('architecture_scope') != state.get(ARCHITECTURE_FIELD)):
        raise ValueError('写码的分工批准没有绑定原实现文档和架构')
    if frozen != contract(state, row, roles):
        raise ValueError('分工批准的范围与当前规则不同')
    revision = frozen.get('mapping_revision')
    if revision:
        from .harness_delivery import authenticate
        result = engine.store.get('decisions', revision['handoff_id'], {}).get('result')
        context = authenticate(engine, run_id, 'engineer', revision['handoff_id'], result)
        if (context[FIELD]['sha256'] != revision['source_scope_sha256']
                or result['scope_sha256'] != revision['source_scope_sha256']
                or result['implementation_map'] != revision['implementation_map']):
            raise ValueError('分工修订与工程师的原交付不一致')
    inherited = frozen.get('inherited_scope_document')
    if inherited and not revision:
        from .document_index import load
        load(engine, run_id, inherited['document_id'], inherited['sha256'])
    ballots = saved.get('ballots', [])
    if len(ballots) != len(roles) or {ballot['role'] for ballot in ballots} != set(roles):
        raise ValueError('写码前缺少三方独立分工审核')
    for ballot in ballots:
        if not ballot['result']['approve']:
            raise ValueError('分工仍有未解决的否决')
        validate(engine, run_id, ballot['role'], ballot['handoff_id'], ballot['result'], frozen, row['document'])
    return frozen['plan']


def enabled_for(engine, run_id, state):
    from .implementation_restart import COLLECTION, MARKER
    run = engine.store.get('runs', run_id, {})
    receipt = engine.store.get(COLLECTION, run.get('state', {}).get(MARKER), {})
    return bool(state.get(ARCHITECTURE_FIELD) or receipt.get('mode') == 'before_document'
                or state.get('document_scope_protocol') == VERSION)
TASK = (
    '独立核对本次实现规范和 Harness 生成的三方分工。确认验收项归口明确、源码归属无重叠、'
    '模块调用链没有断点，跨组双方的输入输出、错误处理、恢复及验证责任清楚。'
    '引用实际读取的当前实现规范和分工对应的已批准架构章节。若需要修改，指出验收项、路径或段落、理由和建议改法；'
    '规范含糊或分工不可行必须否决。不要写代码、修改研究要求或接受其他角色的判断。'
)


def contract(state, row, roles):
    revisions = [entry for entry in state.get('implementation_scope_revisions', [])
                 if entry['spec_hash'] == row['spec_hash']]
    effective = row if not revisions else {**row, 'spec': {**row['spec'],
        'implementation_map': revisions[-1]['implementation_map']}}
    if not revisions and state.get('inherited_scope_mapping'):
        effective = {**row, 'spec': {**row['spec'], 'implementation_map': state['inherited_scope_mapping']}}
    try:
        mapping = state[ARCHITECTURE_FIELD].get('field_sources', {})
        if mapping.get('unresolved'):
            raise ValueError('Architecture source clarification required: ' + str(mapping['unresolved']))
        plan = source_plan(state['id'], 0, effective, roles, architecture_scope=state[ARCHITECTURE_FIELD])
        diagnostic = None
    except ValueError as exc:
        plan, diagnostic = None, str(exc)
    body = {'version': VERSION, 'workflow_id': state['id'], 'spec_hash': row['spec_hash'],
            'document_sha256': row['document']['sha256'], 'plan': plan,
            'effective_implementation_map': effective['spec']['implementation_map'],
            'diagnostic': diagnostic,
            'mapping_revision': revisions[-1] if revisions else None,
            'inherited_scope_document': state.get('inherited_scope_document'),
            'architecture_scope': state[ARCHITECTURE_FIELD]}
    return {**body, 'sha256': sha(body)}


def schema(frozen):
    from .codex_workflow import obj, TEXT
    from .implementation_alignment import SOURCE_REF
    return obj({'approve': {'type': 'boolean'}, 'scope_sha256': {'const': frozen['sha256']},
                'rationale': TEXT, 'evidence': {'type': 'array', 'minItems': 1, 'items': SOURCE_REF},
                'issues': {'type': 'array', 'items': obj(
                    {'location': TEXT, 'reason': TEXT, 'change': TEXT, 'verification': TEXT})}})


def validate(engine, run_id, role, key, result, frozen, document):
    from .harness_delivery import authenticate
    from .implementation_alignment_evidence import _required_ranges
    from .implementation_reading import coverage
    from .intelligence import parse_json
    import json

    context = authenticate(engine, run_id, role, key, result)
    if context.get(FIELD) != frozen or context.get('implementation_document') != document:
        raise ValueError('分工评审交接与实现文档或角色规则不一致')
    parse_json(json.dumps(result, ensure_ascii=False), schema(frozen))
    if result['approve'] == bool(result['issues']):
        raise ValueError('分工评审结论与具体修改项不一致')
    if result['approve'] and frozen['diagnostic']:
        raise ValueError('分工存在结构冲突，不能批准')
    documents = [document]
    architecture = context.get('architecture_document')
    if architecture:
        if architecture['sha256'] != frozen['architecture_scope']['architecture_document_sha256']:
            raise ValueError('分工审核引用了不同版本的架构')
        assigned = {item['section_id'] for item in frozen['architecture_scope']['responsibilities'][role]['architecture_sections']}
        if assigned and not any(ref['document_id'] == architecture['document_id'] and ref['section_id'] in assigned
                                for ref in result['evidence']):
            raise ValueError('分工审核缺少对应职责的架构章节依据')
        documents.append(architecture)
    supplement = (frozen.get('mapping_revision') or {}).get('document') or frozen.get('inherited_scope_document')
    if supplement:
        documents.append(supplement)
    ranges = _required_ranges(engine, run_id, result['evidence'], {doc['document_id']: doc['sha256'] for doc in documents})
    for doc in documents:
        if not ranges.get(doc['document_id']) or not coverage(
                engine, run_id, doc, ranges[doc['document_id']], [key])['complete']:
            raise ValueError('分工意见缺少本角色实际读取的架构、实现文档或修订依据')
    return {'role': role, 'handoff_id': key, 'result': result}


