"""Bounded review contracts after a verified output-limit receipt.

No partial provider output becomes a vote. A compact independent judgment and
one remedy per objection are assembled only after all child receipts validate.
"""
from copy import deepcopy

from . import review_fix_repair


CORE_INSTRUCTION = (
    '原评审因输出长度上限截断。现在分段完成同一提案的独立评审；本段只提交判断、理由、全部必要异议和其他指定判断字段，'
    '不要输出 fixes，后续你会逐项补齐建议。不得为了缩短回复撤销异议、跳过审查或给出虚假批准。'
    '理由只写关键判断，每项异议精简明确，保留原任务的资源、证据、进展和共识标准。'
    '仅使用已保存证据，缺乏证据则明确指出；不得把计划说成已完成实验或已验证结论。'
    '不要复制论文正文和笔记，引用已给出的精确索引。只返回符合 schema 的 JSON。'
)


def eligible(inputs):
    return (inputs.get('purpose', '').startswith('vote:')
            and inputs.get('context', {}).get('review_protocol_version') == 1
            and 'fixes' in inputs.get('schema', {}).get('properties', {}))


def core_contract(inputs, source, tools):
    schema = deepcopy(inputs['schema'])
    # Remove only the deferred remedy field, including conditional constraints.
    def remove_fixes(node):
        if isinstance(node, dict):
            node.get('properties', {}).pop('fixes', None)
            if isinstance(node.get('required'), list):
                node['required'] = [key for key in node['required'] if key != 'fixes']
            for value in node.values():
                remove_fixes(value)
        elif isinstance(node, list):
            for value in node:
                remove_fixes(value)
    remove_fixes(schema)
    fields = schema['properties']
    fields['proposal_hash']['const'] = inputs['context']['proposal_hash']
    fields['rationale']['maxLength'] = min(fields['rationale'].get('maxLength', 600), 600)
    fields['required_changes']['items']['maxLength'] = 240
    for name in ('progress', 'problem_review'):
        for field in fields.get(name, {}).get('properties', {}).values():
            if field.get('type') == 'string':
                field['maxLength'] = min(field.get('maxLength', 300), 300)
    context = deepcopy(inputs['context'])
    context.update(review_split_contract={**source, 'part': 'judgment',
        'original_criteria': inputs['instruction'], 'saved_tool_results': deepcopy(tools)})
    return context, schema


def remedy_contract(core, inputs, index, core_hash, source):
    context, schema = review_fix_repair.contract(core, inputs['context'], inputs['purpose'], core_hash)
    name = f'objection_{index}'
    schema['properties'] = {name: schema['properties'][name]}
    schema['required'] = [name]
    fix = schema['properties'][name]['properties']
    for field in ('proposed_change', 'verification_plan'):
        fix[field]['maxLength'] = 600
    context['review_fix_repair']['missing_objections'] = [row for row in
        context['review_fix_repair']['missing_objections'] if row['objection_index'] == index]
    context['review_split_contract'] = {**source, 'part': name, 'core_hash': core_hash}
    return context, schema
