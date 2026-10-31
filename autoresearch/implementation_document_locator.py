"""Bounded navigation of an author's exact editable SPEC; never reading credit."""
from __future__ import annotations

import copy
import json

TOOL = 'implementation.locate_document'
PROTOCOL = 'implementation_document_locate_protocol'
HELP = ('arguments={document_id:string,expected_sha256:string,base_spec_hash:string,'
    'requirement_id?:string,path?:string,query?:string,offset?:integer,limit?:integer} '
    'locates existing values in the current editable implementation SPEC. Choose exactly one '
    'selector: requirement_id returns matching acceptance, implementation-map and traceability rows; '
    'query finds literal text in leaf values; path reads an exact JSON Pointer. Returns exact paths, '
    'current values and version-bound edit arguments. offset/limit page matches (default 0/10, max 20), '
    'or characters for a string path (default 0/6000, max 12000). Oversized containers return child '
    'paths to inspect without rereading the whole document. Only the current assigned engineer may '
    'inspect its current base. No writing, scientific reading credit or approval.')
GUIDANCE = ('\n修订先定位、再修改：用 implementation.locate_document 按 requirement_id、query 或 path '
    '查当前基线，返回具体 JSON Pointer、原值和版本标识。对已有条目使用 replace，value 填审核意见要求的'
    '修正后内容；只在确需新增且不存在时 add。/- 表示末尾追加，不能用它更新已有验收 ID 或重复来源。'
    '定位只恢复本人的当前实现文档，不要求重读架构；定位不替代独立科学阅读或批准。'
    '定位后用 implementation.edit_document 的 edit_arguments 加 patch 保存；保存成功后重新绑定新版本。')
GUIDANCE += ('\n实现文档有分层目录 document_index。优先按 index_id 取回整个索引单元，'
    '以 revisions:[{index_id:目录中的精确ID,value:该单元修正后完整内容}] 修订。'
    '宿主将索引解析到当前版本的真实位置并替换；无需猜数组序号，不会自动追加。'
    '同批不能同时修改父章节和它的子条目。若当前内容已完整保留，可以直接按目录修订，无需重复定位读取。')
GUIDANCE += ('\n长文本的局部修改可用 revisions:[{index_id:精确ID,text_replacements:[{before:当前稿中唯一匹配的完整原文,'
    'after:替换后的正文}]}]，无需重新输出整段。before 必须逐字匹配且不重叠；after 可为空以删除过时或重复段落。'
    '所有匹配均以本次真实基线计算，宿主保留未匹配文字；匹配失败应重新定位，不得追加一套新定义代替修改旧定义。'
    '同一项只能使用 value 或 text_replacements；替换后仍受原有完整文档校验和长度限制。')
HELP += (' Use index:true to list the chapter/item directory, or index_id:string to retrieve one '
    'directory unit; these are alternative selectors. Revise a unit through '
    'implementation.edit_document with revisions:[{index_id:string,value:corrected complete unit}], '
    'or use text_replacements:[{before:unique exact original text,after:replacement text}] instead of value '
    'to edit only selected spans of a string unit.')
_TABLES = {'acceptance_checks': 'id', 'implementation_map': 'requirement_id',
           'architecture_traceability': 'requirement_id'}
VALUE_BUDGET = 12000


def enabled(context):
    return type(context.get(PROTOCOL)) is int and context[PROTOCOL] == 1


def schema():
    sha = {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}
    return {'type': 'object', 'additionalProperties': False,
        'properties': {'document_id': {'type': 'string', 'minLength': 1},
            'expected_sha256': sha, 'base_spec_hash': sha,
            'requirement_id': {'type': 'string', 'minLength': 1, 'maxLength': 256},
            'path': {'type': 'string', 'pattern': '^/', 'maxLength': 2048},
            'query': {'type': 'string', 'minLength': 1, 'maxLength': 256},
            'index': {'const': True},
            'index_id': {'type': 'string', 'minLength': 1, 'maxLength': 2048},
            'offset': {'type': 'integer', 'minimum': 0},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': VALUE_BUDGET}},
        'required': ['document_id', 'expected_sha256', 'base_spec_hash'],
        'oneOf': [{'required': [key]} for key in ('requirement_id', 'path', 'query', 'index', 'index_id')]}


def child_path(path, key):
    return path + '/' + str(key).replace('~', '~0').replace('/', '~1')


def item_index(spec):
    """Small positional index; values are retrieved through the locator."""
    return {field: [{'requirement_id': row[key], 'path': f'/{field}/{index}'}
                    for index, row in enumerate(spec.get(field, []))
                    if isinstance(row, dict) and key in row]
            for field, key in _TABLES.items() if isinstance(spec.get(field), list)}


def catalog(spec):
    from .implementation_document_readability import HEADINGS
    entries = []
    for field, value in spec.items():
        title = HEADINGS.get(field, {'architecture_traceability': '架构要求与实现验收对应'}.get(field, field))
        entries.append({'index_id': field, 'parent_id': None, 'title': title,
                        'path': child_path('', field)})
        if not isinstance(value, (dict, list)):
            continue
        counts = {}
        for key, child in (value.items() if isinstance(value, dict) else enumerate(value)):
            label = str(child.get(_TABLES[field], key)) if field in _TABLES and isinstance(child, dict) else str(key)
            unit_id = child_path(field, label)
            if field == 'implementation_map':
                counts[label] = counts.get(label, 0) + 1
                unit_id += '/' + str(counts[label])
            entries.append({'index_id': unit_id, 'parent_id': field, 'title': title + ' · ' + label,
                            'path': child_path(child_path('', field), key)})
    return entries


def revision_patch(spec, revisions):
    from .implementation_authoring_preservation import SaveConflict
    if not isinstance(revisions, list) or not 1 <= len(revisions) <= 64:
        raise SaveConflict('invalid_index_revision', 'revisions 必须含 1 至 64 项索引修订。')
    entries, operations = catalog(spec), []
    for revision in revisions:
        if not isinstance(revision, dict) or set(revision) not in (
                {'index_id', 'value'}, {'index_id', 'text_replacements'}):
            raise SaveConflict('invalid_index_revision', '每项修订须含 index_id 和完整 value 或 text_replacements，不能混用。')
        matches = [entry for entry in entries if entry['index_id'] == revision['index_id']]
        if len(matches) != 1:
            raise SaveConflict('invalid_index_revision', '索引不存在或不唯一，请先定位当前目录。', index_id=revision['index_id'])
        path = matches[0]['path']
        if any(path == op['path'] or path.startswith(op['path'] + '/') or op['path'].startswith(path + '/')
               for op in operations):
            raise SaveConflict('invalid_index_revision', '同批修订的索引重复或父子范围重叠；请选择独立单元。')
        value = (_replace_text(_node(spec, path), revision['text_replacements'])
                 if 'text_replacements' in revision else copy.deepcopy(revision['value']))
        operations.append({'op': 'replace', 'path': path, 'value': value})
    return operations


def _replace_text(original, replacements):
    """Resolve every unique span against the same base, never against earlier edits."""
    from .implementation_authoring_preservation import SaveConflict
    if not isinstance(original, str) or not isinstance(replacements, list) or not 1 <= len(replacements) <= 32:
        raise SaveConflict('invalid_text_replacement', '仅文本单元可使用 1 至 32 项 text_replacements。')
    spans = []
    for replacement in replacements:
        if (not isinstance(replacement, dict) or set(replacement) != {'before', 'after'}
                or not isinstance(replacement['before'], str) or not replacement['before']
                or not isinstance(replacement['after'], str)):
            raise SaveConflict('invalid_text_replacement', '替换须含非空 before 原文和 after 文本。')
        before, after = replacement['before'], replacement['after']
        start = original.find(before)
        if start < 0 or original.find(before, start + 1) >= 0:
            raise SaveConflict('text_replacement_conflict', 'before 必须在当前单元中唯一匹配；请定位当前原文后重试。')
        end = start + len(before)
        if before == after or any(start < b and a < end for a, b, _ in spans):
            raise SaveConflict('text_replacement_conflict', '替换不得重叠或原样重写；请合并冲突范围。')
        spans.append((start, end, after))
    result = original
    for start, end, after in sorted(spans, reverse=True):
        result = result[:start] + after + result[end:]
    return result


def _node(spec, path):
    from .implementation_authoring_preservation import _tokens, _index, SaveConflict
    value = spec
    try:
        for token in _tokens(path):
            value = value[_index(token, len(value))] if isinstance(value, list) else value[token]
    except (KeyError, TypeError, IndexError) as exc:
        raise SaveConflict('invalid_document_location', '定位路径不存在，请按当前基线的条目索引定位。', path=path) from exc
    return value


def _leaves(value, path=''):
    if isinstance(value, (dict, list)):
        for key, child in (value.items() if isinstance(value, dict) else enumerate(value)):
            yield from _leaves(child, child_path(path, key))
    else:
        yield path, value


def inspect_spec(base, args):
    """Pure, bounded view; a partial value is never returned as a full replacement."""
    from .engine import digest
    from .implementation_authoring_preservation import SaveConflict
    spec = base['spec']
    if args.get('index') is True:
        entries = catalog(spec)
        offset, limit = args.get('offset', 0), args.get('limit', 20)
        if limit > 20:
            raise SaveConflict('invalid_document_location', '目录每页最多 20 项。')
        return entries[offset:offset + limit], len(entries), offset + limit if offset + limit < len(entries) else None
    if 'index_id' in args:
        found = [entry for entry in catalog(spec) if entry['index_id'] == args['index_id']]
        if len(found) != 1:
            raise SaveConflict('invalid_document_location', '索引不存在或不唯一，请定位当前版本的目录。')
        args = {**args, 'path': found[0]['path']}
    offset = args.get('offset', 0)
    if 'path' in args:
        value = _node(spec, args['path'])
        if isinstance(value, str):
            limit = args.get('limit', 6000)
            end = min(len(value), offset + limit)
            complete = offset == 0 and end == len(value)
            match = {'path': args['path'], 'value_sha256': digest(value),
                'complete': complete, 'text': value[offset:end], 'char_start': offset,
                'char_end': max(offset, end), 'total_chars': len(value),
                'next_offset': end if end < len(value) else None}
            if complete:
                match['value'] = value
            match['index_id'] = args.get('index_id')
            return [match], 1, None
        if len(json.dumps(value, ensure_ascii=False)) <= VALUE_BUDGET:
            candidates = [(args['path'], value)]
        else:
            candidates = [(child_path(args['path'], key), child) for key, child in
                          (value.items() if isinstance(value, dict) else enumerate(value))]
    elif 'requirement_id' in args:
        candidates = [(entry['path'], _node(spec, entry['path']))
                      for rows in item_index(spec).values() for entry in rows
                      if entry['requirement_id'] == args['requirement_id']]
    else:
        query = args['query'].casefold()
        candidates = [(path, value) for path, value in _leaves(spec)
                      if query in str(value).casefold()]
    limit = args.get('limit', 10)
    if limit > 20:
        raise SaveConflict('invalid_document_location', '条目定位每页最多 20 项；字符串路径可按字符分页。')
    matches, remaining = [], VALUE_BUDGET
    for path, value in candidates[offset:offset + limit]:
        size = len(json.dumps(value, ensure_ascii=False))
        match = {'path': path, 'value_sha256': digest(value), 'serialized_chars': size,
                 'complete': size <= remaining}
        units = [entry['index_id'] for entry in catalog(spec) if entry['path'] == path]
        if len(units) == 1:
            match['index_id'] = units[0]
        if match['complete']:
            match['value'] = copy.deepcopy(value)
            remaining -= size
        else:
            match['inspect_path'] = path
            match['instruction'] = '按此 path 定位原值；容器返回子字段，长文本按字符分页。未返回完整值，不能按摘要覆盖。'
        matches.append(match)
    end = offset + len(matches)
    return matches, len(candidates), end if end < len(candidates) else None


def locate(engine, run_id, role, handoff_id, args):
    from . import implementation_authoring as author, implementation_authoring_preservation as preservation
    from .engine import ResearchBlocked
    from .tool_contracts import validate_arguments
    validate_arguments(TOOL, args)
    assignment, context, state, binding = author._assignment(engine, run_id, role, handoff_id)
    if (assignment.get('status') == 'completed' or state.get('status') != 'specification'
            or state.get('current_revision') != binding['revision']):
        raise ResearchBlocked('定位仅限当前未提交的实现文档编写阶段。')
    latest = engine.store.get(author.COLLECTION, handoff_id)
    if latest:
        if (latest.get('id') != handoff_id or latest.get('role') != role
                or latest.get('workflow_id') != state['id'] or latest.get('revision') != binding['revision']
                or latest.get('context') != context or latest.get('status') == 'submitted'):
            raise ResearchBlocked('定位的当前草稿不属于本次冻结编写交接。')
        author._checked(engine, run_id, latest)
    try:
        base = preservation.baseline(engine, run_id, state, binding, context, latest)
        preservation.check_edit_base(args, base)
        matches, total, next_offset = inspect_spec(base, args)
    except preservation.SaveConflict as exc:
        return {'located': False, 'validation': {'code': exc.code, 'error': str(exc), **exc.details},
                'reading_credit': False, 'scientific_approval': False}
    return {'located': True, 'base': preservation.descriptor(base, edit=True),
        'edit_arguments': {key: args[key] for key in ('document_id', 'expected_sha256', 'base_spec_hash')},
        'matches': matches, 'total_matches': total, 'next_offset': next_offset,
        'revision_instruction': '将审核意见落实为 revisions: [{index_id: 定位索引ID, value: 修正后完整单元}]；未改子字段须保留。',
        'reading_credit': False, 'scientific_approval': False}


def duplicate_targets(spec, base):
    """Actionable current-base targets, without silently changing JSON Patch semantics."""
    if not base:
        return []
    problems = []
    for field, key in _TABLES.items():
        if field == 'implementation_map':
            continue  # Multiple implementation locations for one requirement are valid.
        rows = spec.get(field, [])
        if not isinstance(rows, list):
            continue
        seen = {}
        for i, row in enumerate(rows):
            if not isinstance(row, dict) or not isinstance(row.get(key), str):
                continue
            identity = row[key]
            if identity in seen:
                paths = [entry['path'] for entry in item_index(base['spec']).get(field, [])
                         if entry['requirement_id'] == identity]
                problems.append({'kind': 'structure', 'field_path': [field],
                    'requirement_id': identity, 'duplicate_path': f'/{field}/{i}',
                    'existing_base_paths': paths, 'reason': '已有 ID 被重复追加；定位已有条目后 replace，不能再 add 同一 ID。'})
            seen[identity] = i
    trace = spec.get('architecture_traceability', [])
    for i, row in enumerate(trace if isinstance(trace, list) else []):
        if not isinstance(row, dict) or not isinstance(row.get('source_refs'), list):
            continue
        seen = set()
        for j, ref in enumerate(row['source_refs']):
            identity = json.dumps(ref, sort_keys=True, ensure_ascii=False)
            if identity in seen:
                paths = [f'/architecture_traceability/{bi}/source_refs/{bj}'
                         for bi, br in enumerate(base['spec'].get('architecture_traceability', []))
                         if br.get('requirement_id') == row.get('requirement_id')
                         for bj, existing in enumerate(br.get('source_refs', [])) if existing == ref]
                problems.append({'kind': 'structure', 'field_path': ['architecture_traceability', i, 'source_refs'],
                    'requirement_id': row.get('requirement_id'),
                    'duplicate_path': f'/architecture_traceability/{i}/source_refs/{j}',
                    'existing_base_paths': paths, 'reason': '同一条目重复引用相同来源范围；保留已有引用，需修正时定位后 replace。'})
            seen.add(identity)
    return problems
