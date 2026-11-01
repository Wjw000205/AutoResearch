"""Small author policy for future SPECs; it grants no scientific approval.

The SPEC field structure, immutable sources and saved legacy documents stay
unchanged. Algorithm prose has a separate bounded capacity. Diagnostics inspect
presentation and declared module explanations, not scientific correctness or
whether a proposed algorithm has been implemented.
"""
from __future__ import annotations

import copy
import re

VERSION = 1
PROTOCOL = 'implementation_module_details_protocol'
LEGACY_ALGORITHM_MAX_LENGTH = 6000
ALGORITHM_MAX_LENGTH = 24000
VALIDATION_POLICY = 'implementation_document_validation_policy'
VALIDATION_GUIDANCE = ('\n有效保存校验策略：implementation_document_validation_policy 是宿主为本次新请求'
    '生成的容量说明。冻结 specification_schema 保持原样；所有答复 JSON 字符串的旧字符上限'
    '由统一校验策略取消，正文保存也不再应用字符上限。其他结构与语义约束仍遵守冻结 schema。'
    '此策略只允许更完整的模块说明，不放宽来源、版本、公式、冻结参数、排版、三方审核或执行门禁；'
    '不要修改 schema 或通过删减必要模块来满足旧上限。')
GUIDANCE = ('\n逐模块实现说明（只适用于本次新编写合同，保持 SPEC 字段结构和全部冻结科研参数）：'
    'algorithm 不可仅列模块名称、职责表或 implementation_map。按实际模块用 #### 模块：名称 分节，'
    '每个模块用独立段落、列表或表格说明：输入与输出；函数/接口和参数、返回值及张量形状/轴；'
    '可执行的实现步骤或伪代码；边界条件与错误处理；对应验收 ID 和验证方法；已批准设计来源。'
    '可以用“输入：”“输出：”“接口与形状：”“实现步骤：”“边界与错误：”“验证方法：”“批准来源：”'
    '作段落标签，不必给每条验收映射复制模板。implementation_map 继续只做 requirement_id/location/behavior '
    '映射，并指向 algorithm 对应模块；多个验收项可复用同一完整模块说明。'
    '非张量模块明确说明形状不适用及原因；伪代码保留阶段边界、三分支行为和已批准值，不能补造参数。'
    '来源引用复用 architecture_traceability 的精确文档 ID/SHA/章节/范围，区分批准原文与作者实现解释；'
    '若依据不足，明确标为未决并写入 limitations，不能把未决设计或拟议验证宣称为已批准、已实现或已通过。'
    'Markdown 章节、段落和步骤分行；表格必须有 GFM 表头分隔行（| --- | ... |）。'
    '长公式用独立 display 数学块，$$ 或 \\[、\\] 起止符各占独立行，块前后留空行，'
    '公式后保留变量解释、形状、成立条件和来源，不将长公式挤进表格。'
    'algorithm 和其他答复字段不设字符上限。使用精确分段读取，不因增加说明容量累积全文。'
    '这些要求补充可读实现说明，不改变既有文档读取、来源绑定、科学审核、三方投票或执行授权门禁。')


def enabled(context):
    return type(context.get(PROTOCOL)) is int and context[PROTOCOL] == VERSION


def validation_schema(schema, context):
    """Apply the global response policy to a copy, retaining frozen bindings."""
    from .prose_schema import relax
    return relax(schema)


def validation_context(context):
    """Describe the host's effective capacity only for a new request view.

    The harness calls this after returning any frozen paid packet. The policy
    does not authorize saves: validation_schema independently checks the actual
    marker and frozen schema, ignoring any caller-supplied policy metadata.
    """
    output = {key: value for key, value in context.items() if key != VALIDATION_POLICY}
    binding = context.get('implementation_document_authoring')
    if (type(context.get('implementation_document_authoring_protocol')) is not int
            or context['implementation_document_authoring_protocol'] != 1
            or not isinstance(binding, dict)):
        return output
    schema = binding.get('specification_schema')
    if validation_schema(schema, context) == schema:
        return output
    from .prose_schema import POLICY
    return {**output, VALIDATION_POLICY: {
        'version': 2, 'algorithm_max_length': None, 'response_length_policy': POLICY,
        'frozen_algorithm_max_length':(schema.get('properties',{}).get('algorithm') or {}).get('maxLength'),
        'frozen_schema_preserved': True,
        'compatibility_reason': 'response_character_caps_removed',
        'module_details_protocol': VERSION, 'scientific_approval': False}}


def opt_in(instruction, context):
    """Caller must first guard the original assignment key against freezing."""
    if enabled(context):
        return instruction, context
    return instruction + GUIDANCE, {**context, PROTOCOL: VERSION}


_MODULE = re.compile(r'^####\s+(?:模块\s*[：:]\s*|模块\s+)(\S[^\n]*?)\s*#*\s*$', re.M)
_FIELDS = {
    'inputs_outputs': ('输入', '输出'),
    'interface_shapes': ('接口|函数', '形状|张量|维度|轴|不适用'),
    'implementation_steps': ('实现步骤|执行步骤|伪代码|实现流程',),
    'boundaries_errors': ('边界|异常|错误|失败',),
    'verification': ('验证|验收',),
    'approved_sources': ('批准来源|设计来源|来源|未决',),
}


def _missing_explanations(body):
    """Recognize declared interfaces and numbered prose, not just optional labels.

    This is a structural check, not an assessment of the algorithm's correctness.
    Source-backed completeness still belongs to the independent review.
    """
    declared = {name: all(re.search(expression, body) for expression in expressions)
                for name, expressions in _FIELDS.items()}
    signature = re.search(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\([^\n)]*\)\s*(?:→|->)\s*\S', body)
    if signature:
        declared['inputs_outputs'] = True
    declared['interface_shapes'] = bool(
        (signature or re.search(r'接口|函数', body))
        and re.search(r'形状|张量|维度|轴|不适用|(?:float|int|uint)\d+\s*\[|\[[A-Z]\w*(?:\s*,[^]\n]+)+\]', body))
    declared['implementation_steps'] |= bool(re.search(r'^\s*\d+[.、)]\s*\S', body, re.M))
    declared['boundaries_errors'] |= bool(re.search(r'停止|拒绝|禁止|不得', body))
    return [name for name, present in declared.items() if not present]


def _detail(path, validator, reason, **extra):
    return {'kind': 'structure', 'field_path': path, 'validator': validator,
            'reason': reason, **extra}


def _cells(line):
    # Escaped pipes are text, not column separators. Leading/trailing pipes are
    # optional GFM syntax; prose with a lone inline pipe is not a table row.
    parts = re.split(r'(?<!\\)\|', line.strip())
    if parts and not parts[0].strip():
        parts.pop(0)
    if parts and not parts[-1].strip():
        parts.pop()
    return parts


def _markdown_details(path, value):
    # Raw HTML is not rendered as Markdown in the reader. Its block boundaries
    # vary across tags; without a parser, conservatively avoid format diagnoses
    # anywhere in this field rather than misinterpret literal HTML contents.
    if re.search(r'</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?/?>', value):
        return []
    details, visible = [], []
    fence = None
    math = None
    # Inline code may contain literal Markdown/math characters, including new
    # lines. Blank it without moving any original diagnostic line numbers.
    value = re.sub(r'(?<!`)(`{1,2})(?!`)(.*?)\1(?!`)',
        lambda match: '\n' * match[0].count('\n'), value, flags=re.S)
    lines = value.splitlines()
    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        opening = re.match(r'^(`{3,}|~{3,})', stripped)
        if fence:
            if opening and opening[1][0] == fence[0] and len(opening[1]) >= len(fence):
                fence = None
            visible.append('')
            continue
        if opening:
            fence = opening[1]
            visible.append('')
            continue
        if '$$' in line or '\\[' in line or '\\]' in line:
            if stripped not in {'$$', '\\[', '\\]'}:
                details.append(_detail(path, 'display_math_lines',
                    'display 数学块的起止符必须各占独立行；保留原公式并换行。', line=number))
            elif stripped == '$$':
                if math not in {None, '$$'}:
                    details.append(_detail(path, 'display_math_pair', '数学块起止符不匹配。', line=number))
                math = None if math == '$$' else '$$'
            elif stripped == '\\[':
                if math is not None:
                    details.append(_detail(path, 'display_math_pair', '数学块不能交叉嵌套。', line=number))
                math = '\\['
            elif math != '\\[':
                details.append(_detail(path, 'display_math_pair', '数学块缺少对应起始符。', line=number))
            else:
                math = None
            visible.append('')
            continue
        visible.append('' if math else line)
    if math:
        details.append(_detail(path, 'display_math_pair', '数学块缺少结束符。'))
    index = 0
    while index < len(visible):
        line = visible[index].strip()
        # Diagnose only clear author tables with known header labels. Literal
        # absolute-value formulas, IDs/paths, optional-pipe prose and HTML are
        # not reliable table evidence and must never provoke scientific edits.
        cells = _cells(line)
        next_cells = _cells(visible[index + 1]) if index + 1 < len(visible) else []
        known_header = any(re.fullmatch(r'\s*(?:模块|名称|参数名|参数|类型|值|冻结值|冻结值或约束|来源|'
            r'职责|输入|输出|接口|函数|接口或函数名|形状|维度|单位|模块名|验证方法)\s*', cell) for cell in cells)
        table = (line.startswith('|') and line.endswith('|') and len(cells) > 1
            and known_header and re.search(r'\s\|\s', line) and len(next_cells) > 1)
        if not table:
            index += 1
            continue
        delimiter = (len(next_cells) == len(cells)
            and all(re.fullmatch(r'\s*:?-+:?\s*', cell) for cell in next_cells))
        if not delimiter:
            details.append(_detail(path, 'gfm_table_header',
                '表格必须在表头后紧跟同列数的 GFM 分隔行（| --- | ... |）。', line=index + 1,
                header_columns=len(cells), expected_separator='| ' + ' | '.join('---' for _ in cells) + ' |'))
        index += 2 if delimiter else 1
        while index < len(visible) and len(_cells(visible[index])) > 1:
            index += 1
    return details


def validation_details(spec, context):
    """Repairable diagnostics only for the new, explicitly opted-in author."""
    if not enabled(context) or not isinstance(spec, dict):
        return []
    details = []
    algorithm = spec.get('algorithm')
    if isinstance(algorithm, str):
        headings = list(_MODULE.finditer(algorithm))
        if not headings:
            details.append(_detail(['algorithm'], 'module_explanations',
                'algorithm 需要逐模块说明如何实现；使用 #### 模块：名称 分节，不能只列名称或职责映射。'))
        for index, heading in enumerate(headings):
            body = algorithm[heading.end():headings[index + 1].start() if index + 1 < len(headings) else len(algorithm)]
            missing = _missing_explanations(body)
            explanatory_text = re.sub(r'输入与输出|接口与形状|实现步骤|边界与错误|验证方法|批准来源|'
                r'输入|输出|接口|形状', '', body)
            if not re.search(r'[\w\u4e00-\u9fff]', explanatory_text):
                missing = list(_FIELDS)
            if missing:
                details.append(_detail(['algorithm'], 'module_explanation_fields',
                    '模块说明需要输入输出、接口与形状、实现步骤、边界错误、验证方法和批准来源；缺科学依据应明确未决。',
                    module=heading[1], missing_fields=missing))
    # These are the two raw Markdown prose fields. Mapping identities, paths,
    # literals and frozen machine metadata are deliberately never parsed.
    for name in ('algorithm', 'parameter_contract'):
        if isinstance(spec.get(name), str):
            details.extend(_markdown_details([name], spec[name]))
    return details
