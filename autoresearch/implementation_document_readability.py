"""Readable new implementation documents; frozen inputs and schemas stay intact.

This is an authoring and serialization policy, not a scientific approval gate.
The structured SPEC remains the authoritative machine record. No prose, formula,
parameter, source identity or verification outcome is inferred while rendering.
"""
from __future__ import annotations

import html
import json

VERSION = 2
PROTOCOL = 'implementation_document_readability_protocol'
HEADINGS = {
    'summary': '实现目标与范围',
    'algorithm': '模块、接口与算法流程',
    'branches': '基线、提出方法与消融分支',
    'data_contract': '数据来源与划分合同',
    'parameter_contract': '参数与运行合同',
    'metrics': '指标定义',
    'dependencies': '运行依赖',
    'acceptance_checks': '验收要求',
    'implementation_map': '需求与实现位置映射',
    'limitations': '局限与待核实事项',
}
GUIDANCE = ('\n实现文档可读结构要求（用于首次编写和修订，严格保持原 SPEC schema）：'
    '面向读者用简体中文写可直接阅读的 Markdown；不要把 JSON、Python dict 或转义后的整份文档作为正文。'
    '系统按 summary=实现目标与范围、algorithm=模块/接口/算法流程、branches=三种算法分支、'
    'data_contract=数据来源与划分、parameter_contract=参数与运行合同、metrics=指标定义、'
    'dependencies=运行依赖、acceptance_checks=验收要求、implementation_map=需求与实现位置映射、'
    'limitations=局限与待核实事项生成各字段的中文章节。不要在字符串中重复整份 SPEC。'
    'algorithm 内部用 #### 模块职责、#### 接口与张量、#### 执行流程 等小标题：'
    '模块表写模块/计划位置/职责/输入/输出，接口表写接口或函数名/参数与张量轴/返回值/边界条件；'
    '流程用有序步骤说明数据准备、三种分支、训练、验证与独立测试的调用顺序和阶段边界。'
    'parameter_contract 用 #### 参数定义 和表格列参数名/类型/冻结值或约束/单位/适用分支与阶段/来源，'
    '说明模型 seed 与数据划分 seed 的区别；只能写已批准的参数，不补造默认值。'
    '字段内部的小标题用 ####，不要另开 #、##、### 以制造额外顶层阅读单元。'
    '先用短段落解释机制，再列接口、参数和验收；数学公式的 $$ 起止符各占独立一行，'
    '公式后解释符号、张量维度、成立条件和来源，不能把长公式挤进表格。'
    'branches 的 steps 写逐步行为，expected_behavior 写预期行为；验收项保留 id/requirement/verification，'
    '计划映射保留 requirement_id/location/behavior，完整 ID 不截短、不改名。'
    '在对应条目保留来源 document_id、SHA256、section_id 和已核对范围，区分原文、作者解释、计划与待核实项。'
    '来源映射、科学定义、数值、资源边界和全部审计意见不能因排版被省略或改写；'
    '无法核实的具体项记入 limitations。排版要求不是额外科研批准条件；'
    '保存文档、计划位置与验收方法不等于文件已实现、检查已通过或实验成功。')


def enabled(context):
    return context.get(PROTOCOL) in (1, VERSION)


def new_assignment(h, run_id, purpose, instruction, context, schema):
    """Opt in only when this exact legacy assignment has never been frozen."""
    key = h.decision_key(run_id, 'engineer', purpose, instruction, context, schema)
    if h.engine.store.get('handoffs', key):
        return instruction, context
    from . import implementation_module_details as module_details
    if context.get('implementation_document_authoring_protocol') == 1:
        instruction, context = module_details.opt_in(instruction, context)
    if enabled(context):
        return instruction, context
    return instruction + GUIDANCE, {**context, PROTOCOL: VERSION}


def new_save_instruction(instruction, context):
    """Called only when creating a successor, never for a cached child."""
    if enabled(context):
        return instruction, context
    return instruction + GUIDANCE, {**context, PROTOCOL: VERSION}


def _scalar(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, allow_nan=False)


def _cell(value):
    # GFM table syntax must not consume literal pipes, backticks, HTML or slashes.
    text = html.escape(_scalar(value), quote=False)
    return text.replace('\\', '\\\\').replace('|', '\\|').replace('`', '\\`')


def _rich(value):
    return isinstance(value, str) and ('\n' in value or len(value) > 200
        or '$$' in value or '```' in value or '~~~' in value)


def _table(headers, rows, *, section, version=VERSION, identity_columns=(0,)):
    """Keep long prose and block formulas outside table cells, without summarizing."""
    lines = ['| ' + ' | '.join(headers) + ' |', '| ' + ' | '.join('---' for _ in headers) + ' |']
    details = []
    for index, row in enumerate(rows, 1):
        cells = []
        identity = ' · '.join(_scalar(row[column]).replace('\n', ' ').replace('\r', ' ')
                              for column in identity_columns)
        for header, value in zip(headers, row):
            if _rich(value):
                if version == 1:
                    cells.append(f'见本节条目 {index}：{header}')
                    heading = f'条目 {index} · {header}'
                else:
                    # A local row number repeats in every chapter. Bind both
                    # the link text and detail heading to the actual field ID,
                    # so a bounded read cannot confuse e.g. acceptance #16
                    # with limitations #16. The original prose stays verbatim.
                    cells.append(_cell(f'见“{section}”章节内“{identity} · {header}”全文'))
                    heading = _cell(f'{section} · {identity} · {header}')
                details += ['', f'#### {heading}', '', value, '']
            else:
                cells.append(_cell(value))
        lines.append('| ' + ' | '.join(cells) + ' |')
    return '\n'.join([*lines, *details])


def _list(values):
    lines = []
    for number, value in enumerate(values, 1):
        if _rich(value):
            lines += [f'#### 条目 {number}', '', value, '']
        else:
            lines += [f'{number}. {_cell(value)}']
    return '\n'.join(lines)


def render_specification(spec, *, version=VERSION):
    """Serialize the same SPEC values in a stable, readable field order."""
    bodies = {
        'summary': spec['summary'],
        'algorithm': spec['algorithm'],
        'branches': _table(['分支标识', '实现步骤', '预期行为'], [
            (row['variant'], row['steps'], row['expected_behavior']) for row in spec['branches']],
            section=HEADINGS['branches'], version=version),
        'data_contract': _table(['字段标识', '说明', '冻结要求'], [
            (key, label, spec['data_contract'][key]) for key, label in [
                ('dataset', '数据集'), ('source', '来源'), ('split_identity', '划分身份'),
                ('leakage_prevention', '防止数据泄漏')]], section=HEADINGS['data_contract'], version=version),
        'parameter_contract': spec['parameter_contract'],
        'metrics': _list(spec['metrics']),
        'dependencies': _table(['序号', '依赖或版本要求'], [
            (number, value) for number, value in enumerate(spec['dependencies'], 1)],
            section=HEADINGS['dependencies'], version=version),
        'acceptance_checks': _table(['验收项 ID', '要求', '验证方法'], [
            (row['id'], row['requirement'], row['verification']) for row in spec['acceptance_checks']],
            section=HEADINGS['acceptance_checks'], version=version),
        'implementation_map': _table(['验收项 ID', '计划实现位置', '实现职责'], [
            (row['requirement_id'], row['location'], row['behavior']) for row in spec['implementation_map']],
            section=HEADINGS['implementation_map'], version=version),
        'limitations': _list(spec['limitations']),
    }
    chapters = ['## ' + heading + '\n\n' + bodies[key] for key, heading in HEADINGS.items()]
    if 'architecture_traceability' in spec:
        chapters.append('## 架构要求与实现验收对应\n\n' + _table(
            ['验收项 ID', '批准来源精确位置', '落实理由'], [
                (row['requirement_id'], '\n'.join(
                    f"document_id={ref['document_id']}；SHA256={ref['document_sha256']}；"
                    f"section_id={ref['section_id']}；offset={ref['offset']}；limit={ref['limit']}"
                    for ref in row['source_refs']), row['rationale'])
                for row in spec['architecture_traceability']], section='架构要求与实现验收对应', version=version))
    if 'comparison_transport' in spec:
        contract = spec['comparison_transport']
        body = ('未启用比较数据传递。' if contract is None else
                _table(['合同字段', '精确值'], list(_leaves(contract)), section='比较数据传递合同', version=version))
        chapters.append('## 比较数据传递合同\n\n' + body)
    return '\n\n'.join(chapters) + '\n'


def _schema_rows(schema, path='$'):
    constraints = {key: value for key, value in schema.items() if key not in {'type', 'properties', 'items'}}
    yield path, schema.get('type', '未指定'), '; '.join(
        f'{key}={_scalar(value)}' for key, value in sorted(constraints.items()))
    for key, child in sorted(schema.get('properties', {}).items()):
        yield from _schema_rows(child, path + '.' + key)
    if isinstance(schema.get('items'), dict):
        yield from _schema_rows(schema['items'], path + '[]')


def _leaves(value, path='$'):
    if isinstance(value, dict) and value:
        for key in sorted(value):
            yield from _leaves(value[key], path + '/' + key.replace('~', '~0').replace('/', '~1'))
    elif isinstance(value, list) and value:
        for index, item in enumerate(value):
            yield from _leaves(item, path + '/' + str(index))
    else:
        yield path, value


def render_format_contract(contract, *, version=VERSION):
    """Explain stage mapping first; schema details stay in one reference appendix."""
    plan, delivery = contract['planning_document'], contract['delivery_document']
    lines = ['协议版本：' + _cell(contract['version']), '',
        _table(['阶段', '机器记录名称', '用途与证据边界'], [
            ('实现计划', plan['name'], plan['scope']),
            ('真实代码交付', delivery['name'], delivery['scope'])], section='计划映射与真实交付格式', version=version), '',
        '#### 计划到真实交付的转换', '', _list(contract['conversion']), '',
        '#### 结构示例（不代表实际文件或验证通过）', '',
        _table(['示例字段定位', '示例值'], list(_leaves(contract['illustration_only_not_artifact_evidence'])),
               section='结构示例（不代表实际文件或验证通过）', version=version), '',
        '#### 机器字段与约束参考附录', '',
        '以下保留冻结协议的字段、类型和约束；不替代真实写入回执、文件核验或同版本独立审计。', '',
        _table(['阶段', '字段路径', '类型', '约束'], [
            *[('实现计划', *row) for row in _schema_rows(plan['mapping_schema'])],
            *[('真实交付', *row) for row in _schema_rows(delivery['schema'])]],
            section='机器字段与约束参考附录', version=version, identity_columns=(0, 1))]
    return '\n'.join(lines)
