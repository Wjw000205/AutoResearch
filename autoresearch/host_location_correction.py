"""One same-reviewer repair of settled invalid location references.

This does not resolve a reference on the model's behalf. All substantive valid
fields and the verdict remain frozen. Explicit empty placeholder tail entries
may be completed or withdrawn by their author; their original receipt remains.
"""
import copy
import difflib

from jsonschema import Draft202012Validator

from .experiment_workflow import sha

KEY = 'bounded_evidence_review'
PLACEHOLDERS = ('占位', '占位符', 'placeholder', 'TODO', 'todo', 'TBD', 'tbd', '...')


def analyze(purpose, context, schema, value):
    if not str(purpose).startswith('rapid_review:host-repair:') or not isinstance(value, dict):
        return None
    packet = context.get(KEY, {})
    from .host_review_compaction import vote_packet_matches
    if (packet.get('version') != 1 or packet.get('phase') not in ('triage', 'code', 'checks')
            or packet.get('packet_sha256') != sha({k:v for k,v in packet.items() if k != 'packet_sha256'})
            or not vote_packet_matches(purpose, packet, value)):
        return None
    payload = packet.get('payload', {})
    catalog = payload.get('review_scope', payload).get('location_catalog')
    if packet.get('compact_review_protocol') == 1:
        from .host_review_compaction import alias_catalog
        try:
            catalog = alias_catalog(context, schema)
        except (KeyError, TypeError, ValueError):
            return None
    if not isinstance(catalog, list) or not catalog:
        return None
    ids = [row.get('location_id') for row in catalog]
    if any(not isinstance(x, str) for x in ids) or len(set(ids)) != len(ids):
        return None
    try:
        declared = schema['properties']['required_changes']['items']['properties']['location_id']['enum']
    except (KeyError, TypeError):
        return None
    if set(declared) != set(ids):
        return None
    errors = list(Draft202012Validator(schema).iter_errors(value))
    if not errors:
        return None
    indices = []
    for error in errors:
        path = tuple(error.path)
        if (error.validator != 'enum' or len(path) != 3 or path[0] != 'required_changes'
                or type(path[1]) is not int or path[2] != 'location_id'):
            return None
        indices.append(path[1])
    changes = value['required_changes']
    # A malformed reference cannot authorize changing an approval or any gate.
    if value.get('approve') is not False or not changes:
        return None
    placeholders = [i for i in indices if all(changes[i].get(k) in PLACEHOLDERS
                                             for k in ('problem', 'change', 'acceptance'))]
    removable = []
    count = len(changes)
    while count > 1 and count-1 in placeholders:
        count -= 1
        removable.append(count)
    index = {r['location_id']: r for r in catalog}
    hints = []
    for i in sorted(set(indices)):
        matches = difflib.get_close_matches(str(changes[i]['location_id']), ids, n=3, cutoff=.5)
        hints.append({'change_index': i, 'invalid_location_id': changes[i]['location_id'],
                      'explicit_placeholder': i in placeholders,
                      'navigation_only_not_a_resolution': [index[x] for x in matches]})
    return {'version': 1, 'packet_sha256': packet['packet_sha256'], 'catalog_sha256': sha(catalog),
            'invalid_indices': sorted(set(indices)), 'placeholder_indices': placeholders,
            'allowed_shorter_lengths': removable, 'navigation': hints,
            'scope': 'Only invalid references and explicitly empty placeholder entries; valid findings/verdict frozen',
            'execution_authorized': False}


def constrain(prepared, value, audit):
    original = copy.deepcopy(prepared['schema'])
    changes = value['required_changes']
    items = []
    for i, change in enumerate(changes):
        if i not in audit['invalid_indices']:
            items.append({'const': copy.deepcopy(change)})
            continue
        item = copy.deepcopy(original['properties']['required_changes']['items'])
        for name, content in change.items():
            if name == 'location_id':
                continue
            if i in audit['placeholder_indices'] and name in ('problem', 'change', 'acceptance'):
                item['properties'][name] = {'allOf': [item['properties'][name], {'not': {'enum': list(PLACEHOLDERS)}}]}
            else:
                item['properties'][name] = {'const': copy.deepcopy(content)}
        items.append(item)
    branches = []
    for count in [len(changes), *audit['allowed_shorter_lengths']]:
        branch = copy.deepcopy(original)
        for name, content in value.items():
            if name != 'required_changes':
                branch['properties'][name] = {'const': copy.deepcopy(content)}
        branch['properties']['required_changes'] = {
            'type': 'array', 'prefixItems': copy.deepcopy(items[:count]), 'items': False,
            'minItems': count, 'maxItems': count}
        branches.append(branch)
    prepared['schema'] = {'anyOf': branches} if len(branches) > 1 else branches[0]
    prepared['context']['output_schema_repair']['host_location_correction'] = copy.deepcopy(audit)
    prepared['task'] += (
        '\n本次仅纠正你原答复中的位置引用错误，不重新做审查。其余实质意见、合法位置、候选身份、rationale和approve已固定。'
        'invalid_indices是从0开始的错误条目。按原location_catalog选择真实位置；相似编号仅供导航，宿主没有代你决定位置。'
        '若某条problem/change/acceptance全部为“占位”等空内容，必须依据本角色已读材料补为真实内容，'
        '或从尾部撤回该空占位条目。不得撤回任何已有实质意见，不能为通过格式校验编造新问题、证据或批准。'
        '无法支持额外问题时撤回空占位，不要换一个编号继续保留占位。无需重读源码、调用工具、修代码或运行训练。'
        '这是原角色的一次有界字段补正；原始输出与回执保持不变。')
