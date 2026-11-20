"""Lossless, versioned materials for authoring and reviewing a frozen experiment plan."""
from __future__ import annotations

import copy
import hashlib
import json
import re

from .task_document_projection import SCALAR_TYPES, encoded, sha

VERSION = 1
FIELD = 'experiment_materials'
GUIDANCE = ('以当前批准架构说明及实现接口为主。架构已明确的内容直接落实；'
    '只在当前实现或验收需要核对具体冻结值、任务差异或冲突时，按完整组合表和原文位置补读。'
    '原文引用保留文档 ID、SHA256、章节和实际读取范围；资料目录不计为原文阅读。'
    '不要遍历实验计划、论文协议库或历史记录。')
PLAN_REQUIREMENTS = {
    'C04': (('metric_contract', 'acceptance'), ('protocol',), ('paper_reference',)),
}
TASK_REQUIREMENTS = {
    'C04': (('task_dimensions',), ('protocol_id',), ('protocol_sha256',)),
    'C05': (('plan', 'validation_seeds'), ('plan', 'final_test_seeds')),
    'C06': (('plan', 'planned_trials'),),
}
_FENCES = re.compile(r'^\x60\x60\x60json\n([^\n]+)\n\x60\x60\x60$', re.MULTILINE)


def _check_json(value):
    if isinstance(value, dict):
        if any(type(key) is not str for key in value):
            raise ValueError('Experiment materials require JSON object keys')
        for child in value.values():
            _check_json(child)
    elif isinstance(value, list):
        for child in value:
            _check_json(child)
    elif type(value) not in SCALAR_TYPES:
        raise ValueError('Experiment materials require JSON values')


def _protocol_binding(row, source):
    identity, version = row.get('protocol_id'), row.get('protocol_sha256')
    if (type(identity) is not str or not identity
            or type(version) is not str or not re.fullmatch('[a-f0-9]{64}', version)
            or not isinstance(source, dict) or source.get('id') != identity
            or source.get('protocol_sha256') != version):
        raise ValueError('Experiment combination source protocol binding changed')


def _shared(rows):
    first = rows[0]
    if all(encoded(value) == encoded(first) for value in rows[1:]):
        return copy.deepcopy(first)
    if all(isinstance(value, dict) for value in rows):
        result = {}
        for key in sorted(set.intersection(*(set(row) for row in rows))):
            value = _shared([row[key] for row in rows])
            if value is not _DIFFERENT:
                result[key] = value
        if result:
            return result
    return _DIFFERENT


_DIFFERENT = object()


def _differences(template, row, path=''):
    for key, value in sorted(row.items()):
        pointer = path + _pointer(key)
        if key not in template:
            yield [pointer, copy.deepcopy(value)]
        elif encoded(template[key]) != encoded(value):
            yield from _differences(template[key], value, pointer)


def _expand(template, differences):
    row = copy.deepcopy(template)
    for pointer, value in differences:
        if not isinstance(pointer, str) or not pointer.startswith('/'):
            raise ValueError('Invalid experiment difference pointer')
        tokens = [part.replace('~1', '/').replace('~0', '~') for part in pointer[1:].split('/')]
        canonical = '/' + '/'.join(p.replace('~', '~0').replace('/', '~1') for p in tokens)
        if canonical != pointer:
            raise ValueError('Invalid experiment difference pointer escape')
        parent = row
        for part in tokens[:-1]:
            parent = parent[part]
        if tokens[-1] in parent:
            raise ValueError('Experiment difference replaces an existing template field')
        parent[tokens[-1]] = copy.deepcopy(value)
    return row


def _pointer(key):
    return '/' + key.replace('~', '~0').replace('/', '~1')


def compile_view(plan):
    """Group identical task data by exact JSON values and retain ordered combinations."""
    from .benchmark_matrix import dimensions, enabled
    if not enabled(plan):
        return None
    _check_json(plan)
    rows = plan['task_plans']
    if not isinstance(rows, list) or not rows:
        raise ValueError('Experiment matrix requires frozen tasks')
    protocols, combinations, bodies, ids = {}, [], [], set()
    for row in rows:
        task_id = row['task_id']
        if not isinstance(task_id, str) or not task_id or task_id in ids:
            raise ValueError('Experiment combination requires a unique task ID')
        ids.add(task_id)
        source = row['source_protocol']
        _protocol_binding(row, source)
        ref = sha(source)
        protocols.setdefault(ref, copy.deepcopy(source))
        dimensions_for_row = dimensions(row)
        combinations.append({'task_id': task_id, 'dimensions': dimensions_for_row,
            'protocol_id': row['protocol_id'], 'protocol_sha256': row['protocol_sha256'],
            'protocol_content_sha256': ref})
        bodies.append({k: v for k, v in row.items() if k not in {'task_id', 'source_protocol'}})
    # The root template remains an object even when the rows share no fields.
    template = _shared(bodies)
    template = {} if template is _DIFFERENT else template
    for combination, body in zip(combinations, bodies, strict=True):
        combination['differences'] = list(_differences(template, body))
    # Flattened inventories are derived only when the exact canonical values agree.
    flat = [key for key in ('grid', 'validation_pairs', 'ablation_candidates')
            if key in plan and all(key in row['plan'] for row in rows)
            and encoded(plan[key]) == encoded([value for row in rows for value in row['plan'][key]])]
    result = {'version': VERSION, 'source_sha256': sha(plan), 'template': template,
        'combinations': combinations, 'protocols': protocols, 'flattened_fields': flat,
        'common': {k: copy.deepcopy(v) for k, v in plan.items() if k not in {'task_plans', *flat}}}
    if encoded(restore(result)) != encoded(plan):
        raise ValueError('Experiment materials changed the original plan')
    return result


def restore(view):
    from .benchmark_matrix import dimensions
    _check_json(view)
    if type(view.get('version')) is not int or view['version'] != VERSION:
        raise ValueError('Unknown experiment materials version')
    protocols, tasks, used, ids = view['protocols'], [], set(), set()
    for combination in view['combinations']:
        identity = combination['protocol_content_sha256']
        source = protocols[identity]
        row = _expand(view['template'], combination['differences'])
        _protocol_binding(row, source)
        _protocol_binding(combination, source)
        task_id = combination['task_id']
        if (sha(source) != identity or not isinstance(task_id, str) or not task_id or task_id in ids
                or {'task_id', 'source_protocol'} & row.keys()
                or combination['protocol_id'] != row['protocol_id'] or source['id'] != row['protocol_id']
                or combination['protocol_sha256'] != row['protocol_sha256']
                or source['protocol_sha256'] != row['protocol_sha256']
                or encoded(dimensions(row)) != encoded(combination['dimensions'])):
            raise ValueError('Experiment combination or protocol reference changed')
        used.add(identity)
        ids.add(task_id)
        tasks.append({'task_id': task_id, **row, 'source_protocol': copy.deepcopy(source)})
    if not tasks or used != protocols.keys():
        raise ValueError('Experiment protocol library does not match its combinations')
    flat = view['flattened_fields']
    if (len(set(flat)) != len(flat) or set(flat) - {'grid', 'validation_pairs', 'ablation_candidates'}
            or {'task_plans', *flat} & view['common'].keys()):
        raise ValueError('Experiment inventory projection is invalid')
    plan = {**copy.deepcopy(view['common']), 'task_plans': tasks,
        **{k: [copy.deepcopy(value) for row in tasks for value in row['plan'][k]] for k in flat}}
    if sha(plan) != view['source_sha256']:
        raise ValueError('Experiment materials do not match the frozen source fingerprint')
    return plan


def _section(title, value):
    return [f'## {title}', '', '```json', encoded(value), '```', '']


def render_protocol(source, identity):
    lines = ['# 论文协议精确定义', '', f'内容 SHA256 `{identity}`', '', GUIDANCE, '']
    for key, value in sorted(source.items()):
        label = ' '.join(key.split())
        pointer = '/' + key.replace('~', '~0').replace('/', '~1')
        if isinstance(value, list) and value:
            lines += _section(f'字段 {label}', [pointer, key, 'array', len(value)])
            for index, item in enumerate(value):
                name = item.get('name') if isinstance(item, dict) else None
                name = ' '.join(name.split()) if isinstance(name, str) else ''
                lines += _section(f'{label} 条目 {index + 1} {name}'.rstrip(), [pointer, index, item])
        else:
            lines += _section(f'字段 {label}', [pointer, key, 'value', value])
    text = '\n'.join(lines)
    if encoded(parse_protocol(text, identity)) != encoded(source):
        raise ValueError('Published protocol is not an exact reconstruction')
    return text


def parse_protocol(text, identity):
    source, arrays = {}, {}
    for match in _FENCES.finditer(text):
        entry = json.loads(match[1])
        if len(entry) == 3:
            pointer, index, item = entry
            key, count = arrays[pointer]
            if type(index) is not int or index != len(source[key]) or index >= count:
                raise ValueError('Published protocol array order changed')
            source[key].append(item)
            continue
        pointer, key, kind, value = entry
        if (type(key) is not str or key in source
                or pointer != '/' + key.replace('~', '~0').replace('/', '~1')):
            raise ValueError('Published protocol key or pointer is invalid')
        if kind == 'array' and type(value) is int and value > 0:
            arrays[pointer] = (key, value)
            source[key] = []
        elif kind == 'value':
            source[key] = value
        else:
            raise ValueError('Published protocol container type is invalid')
    if any(len(source[key]) != count for key, count in arrays.values()) or sha(source) != identity:
        raise ValueError('Published protocol SHA256 differs from source')
    return source


def render(view, original, protocol_documents):
    restore(view)
    lines = ['# 冻结实验计划任务视图', '', GUIDANCE, '']
    lines += _section('冻结计划与原文', {'version': view['version'], 'source_sha256': view['source_sha256'],
        'original_document_id': original['document_id'], 'original_document_sha256': original['sha256'],
        'original_document_title': original['title']})
    table = [{**{k: v for k, v in row.items() if k != 'differences'},
              'difference_section': f'任务差异 {index + 1}',
              'source_protocol_pointer': f'/task_plans/{index}/source_protocol'}
             for index, row in enumerate(view['combinations'])]
    lines += _section('完整组合表', table)
    lines += _section('论文协议定位', {identity: {k: doc[k] for k in ('document_id', 'sha256', 'title')}
                                       for identity, doc in protocol_documents.items()})
    for group, title in (('common', '全局'), ('template', '模板')):
        heading = '共享模板字段' if group == 'template' else '全局字段'
        lines += _section(heading, {'group': group, 'keys': sorted(view[group])})
        for key, value in sorted(view[group].items()):
            lines += _section(title + ' ' + ' '.join(key.split()), [group, _pointer(key), key, value])
    for index, row in enumerate(view['combinations']):
        lines += _section(f'任务差异 {index + 1}', row['differences'])
    lines += _section('派生作业清单', {key: '/task_plans/*/plan/' + key for key in view['flattened_fields']})
    return '\n'.join(lines)


def parse_materials(text, original, protocol_documents):
    """Reconstruct from the same published text and protocol bytes used by readers."""
    blocks = iter(json.loads(match[1]) for match in _FENCES.finditer(text))
    binding, table, references = next(blocks), next(blocks), next(blocks)
    if (binding['original_document_id'] != original['document_id']
            or binding['original_document_sha256'] != original['sha256']
            or set(references) != protocol_documents.keys()):
        raise ValueError('Published experiment materials have foreign source documents')
    view = {k: binding[k] for k in ('version', 'source_sha256')}
    for group in ('common', 'template'):
        manifest = next(blocks)
        if manifest['group'] != group or sorted(set(manifest['keys'])) != manifest['keys']:
            raise ValueError('Published experiment field manifest is invalid')
        view[group] = {}
        for key in manifest['keys']:
            name, pointer, actual_key, value = next(blocks)
            if (name, pointer, actual_key) != (group, _pointer(key), key):
                raise ValueError('Published experiment field or pointer changed')
            view[group][key] = value
    view['combinations'] = []
    for index, row in enumerate(table):
        if (row.pop('difference_section') != f'任务差异 {index + 1}'
                or row.pop('source_protocol_pointer') != f'/task_plans/{index}/source_protocol'):
            raise ValueError('Published experiment combination pointer changed')
        view['combinations'].append({**row, 'differences': next(blocks)})
    flattened = next(blocks)
    if (any(pointer != '/task_plans/*/plan/' + key for key, pointer in flattened.items())
            or next(blocks, None) is not None):
        raise ValueError('Published experiment inventory is invalid')
    view['flattened_fields'] = list(flattened)
    view['protocols'] = {}
    for identity, (doc, body) in protocol_documents.items():
        if (references[identity] != {k: doc[k] for k in ('document_id', 'sha256', 'title')}
                or hashlib.sha256(body.encode()).hexdigest() != doc['sha256']):
            raise ValueError('Published experiment protocol document version changed')
        view['protocols'][identity] = parse_protocol(body, identity)
    return restore(view)


def _member_offsets(text, value, start, end):
    """Locate generated JSON tokens against the verified original byte-for-byte text."""
    decoder = json.JSONDecoder()
    start += len(text[start:end]) - len(text[start:end].lstrip())
    if isinstance(value, list):
        cursor = start + 1
        for child in value:
            while text[cursor] in ' \r\n\t,':
                cursor += 1
            _, stop = decoder.raw_decode(text, cursor)
            yield cursor, stop, child
            cursor = stop
    elif isinstance(value, dict):
        cursor = start + 1
        for key in sorted(value):
            while text[cursor] in ' \r\n\t,':
                cursor += 1
            decoded, key_end = decoder.raw_decode(text, cursor)
            if decoded != key:
                raise ValueError('Frozen JSON field order differs from its original text')
            value_start = re.compile(r'\s*:\s*').match(text, key_end).end()
            _, stop = decoder.raw_decode(text, value_start)
            yield key, cursor, value_start, stop
            cursor = stop


def _source_ranges(original, section, start, end, **locator):
    from .document_read_window import DOCUMENT_LIMIT
    return [[{'document_id': original['document_id'], 'document_sha256': original['sha256'],
              'section_id': section['id'], 'offset': offset - section['start'],
              'limit': min(DOCUMENT_LIMIT, end - offset), **locator}]
            for offset in range(start, end, DOCUMENT_LIMIT)]


def _plan_field_spans(plan, original, text):
    from .implementation_documents import _json
    cursor = 0
    spans = {}
    for field, value in plan.items():
        prefix, body, suffix = '## ' + field + '\n\n```json\n', _json(value), '\n```\n'
        position = text.index(prefix + body + suffix, cursor)
        start = position + len(prefix)
        end = start + len(body)
        section = next((entry for entry in original['sections'] if entry['start'] <= start < end <= entry['end']), None)
        if section is None:
            raise ValueError('Original plan field exceeds its indexed section')
        spans[field] = (section, start, end)
        cursor = end + len(suffix)
    return spans


def _top_level_evidence(original, spans):
    evidence = {}
    for criterion, requirements in PLAN_REQUIREMENTS.items():
        evidence[criterion] = []
        for names in requirements:
            field = next((name for name in names if name in spans), None)
            if field is None:
                continue  # Some supported plans omit these optional global definitions.
            section, start, end = spans[field]
            evidence[criterion].extend(_source_ranges(original, section, start, end, pointer=_pointer(field)))
    return evidence


def _task_evidence(plan, original, original_text, spans):
    section, start, end = spans['task_plans']
    groups = {cid: [] for cid in TASK_REQUIREMENTS}
    items = list(_member_offsets(original_text, plan['task_plans'], start, end))
    for index, (left, right, row) in enumerate(items):
        root = {key: (key_start, value_start, stop) for key, key_start, value_start, stop
                in _member_offsets(original_text, row, left, right)}
        if 'task_dimensions' not in root:
            if not {'input_length', 'output_length'} <= root.keys():
                raise ValueError(f'Original matrix task {row["task_id"]} lacks its dimensions')
            dimension_paths = (('input_length',), ('output_length',))
        else:
            dimension_paths = (('task_dimensions',),)
        for cid, requirements in TASK_REQUIREMENTS.items():
            if cid == 'C04':
                requirements = (*dimension_paths, ('protocol_id',), ('protocol_sha256',))
            for path in requirements:
                item = root
                value = row
                actual_path = []
                for depth, key in enumerate(path):
                    if key not in item:
                        # A plan can be derived without a planned-trials scalar.
                        # In that version the complete grid is the trial source.
                        if path == ('plan', 'planned_trials') and 'grid' in item:
                            key = 'grid'
                        else:
                            raise ValueError(f'Original matrix task {row["task_id"]} lacks {"/".join(path)}')
                    key_start, value_start, stop = item[key]
                    value = value[key]
                    actual_path.append(key)
                    if depth + 1 < len(path):
                        item = {k: (a, b, c) for k, a, b, c in
                                _member_offsets(original_text, value, value_start, stop)}
                pointer = '/task_plans/' + str(index) + ''.join(map(_pointer, actual_path))
                groups[cid].extend(_source_ranges(original, section, key_start, stop,
                                                   task_id=row['task_id'], pointer=pointer))
    return groups


def attach(engine, h, run_id, plan, context, *, _save_document=None):
    from . import document_index
    from .engine import digest
    from .implementation_documents import _save, original_plan_text
    save_document = _save_document or _save
    view = compile_view(plan)
    if view is None:
        return context
    candidates = [doc for doc in context['indexed_documents'] if doc['kind'] == 'frozen_experiment_plan']
    original = context.get('frozen_experiment_plan_document')
    if original is None and len(candidates) == 1:
        original = candidates[0]
    if original not in candidates:
        raise ValueError('Compact materials require the exact frozen plan document')
    actual = engine.experiment_plan(run_id)
    if (encoded(actual) != encoded(plan) or digest(plan) != context.get('experiment_plan_hash')):
        raise ValueError('Compact materials plan differs from the approved frozen structure')
    manifest, original_text = document_index.load(engine, run_id, original['document_id'], original['sha256'])
    if (original_text != original_plan_text(plan) or manifest['sections'] != original['sections']):
        raise ValueError('Compact materials original document differs from the approved frozen plan')
    spans = _plan_field_spans(plan, original, original_text)
    task_sources = _task_evidence(plan, original, original_text, spans)
    for criterion, groups in _top_level_evidence(original, spans).items():
        task_sources[criterion].extend(groups)
    root = f'cycle-{engine.cycle(run_id)}/implementation/experiment-materials/v{VERSION}'
    protocols, bodies = {}, {}
    for identity, source in sorted(view['protocols'].items()):
        body = render_protocol(source, identity)
        protocols[identity] = save_document(engine, h, run_id, f'{root}/protocol-{sha(body)}.md',
                                    body, 'experiment_source_protocol')
        bodies[identity] = (protocols[identity], body)
    text = render(view, original, protocols)
    if encoded(parse_materials(text, original, bodies)) != encoded(plan):
        raise ValueError('Published experiment material differs from the original plan')
    document = save_document(engine, h, run_id, f'{root}/plan-{sha(text)}.md', text, 'experiment_materials')
    return {**context, FIELD: {'version': VERSION, 'guidance': GUIDANCE,
        'document_id': document['document_id'], 'document_sha256': document['sha256'],
        'original_document_id': original['document_id'], 'original_document_sha256': original['sha256'],
        'source_sha256': view['source_sha256'],
        'task_sources': task_sources,
        'protocol_document_ids': [doc['document_id'] for doc in protocols.values()]},
        'indexed_documents': [*context['indexed_documents'], document, *protocols.values()]}


def verify_frozen(engine, run_id, plan, context):
    """Recompile v1 against immutable indexed bytes, without saving or indexing."""
    from . import document_index
    frozen = context[FIELD]
    if type(frozen.get('version')) is not int or frozen['version'] != VERSION:
        raise ValueError('Unsupported frozen experiment materials version')
    if compile_view(plan) is None:
        raise ValueError('Frozen experiment materials require a compilable approved plan')
    documents = context['indexed_documents']
    if len({d['document_id'] for d in documents}) != len(documents):
        raise ValueError('Ambiguous frozen experiment document inventory')
    for doc in documents:
        if document_index.directory(engine, run_id, doc['document_id'], doc['sha256']) != doc:
            raise ValueError('Frozen indexed document identity or ranges changed')

    def existing(engine, h, run_id, name, text, kind):
        matches = [d for d in documents if d['title'] == name and d['kind'] == kind]
        if len(matches) != 1:
            raise ValueError('Missing or ambiguous frozen derived document')
        doc = matches[0]
        _, original = document_index.load(engine, run_id, doc['document_id'], doc['sha256'])
        if original != text:
            raise ValueError('Frozen derived document differs from deterministic compiler')
        return doc

    # A no-op attach must never validate the supplied field against itself.
    # Only the deterministic compiler may supply the rebuilt materials.
    base = {key: value for key, value in context.items() if key != FIELD}
    base['indexed_documents'] = [d for d in documents
        if d['kind'] not in {'experiment_materials', 'experiment_source_protocol'}]
    rebuilt = attach(engine, None, run_id, plan, base, _save_document=existing)
    if (FIELD not in rebuilt or encoded(rebuilt[FIELD]) != encoded(frozen)
            or {d['document_id']: d for d in rebuilt['indexed_documents']}
                != {d['document_id']: d for d in documents}):
        raise ValueError('Frozen experiment materials or original source ranges changed')


def carry(source, context):
    """Carry navigation into a new acceptance assignment; original refs remain authorized."""
    return {**context, FIELD: source[FIELD]} if FIELD in source else context


def acceptance_sources(context, assigned):
    """Freeze alternatives from the exact original plan directory for each criterion."""
    materials = context.get(FIELD)
    if not materials:
        return None
    original = next((doc for doc in context['indexed_documents']
        if doc['document_id'] == materials['original_document_id']
        and doc['sha256'] == materials['original_document_sha256']
        and doc['kind'] == 'frozen_experiment_plan'), None)
    if original is None:
        raise ValueError('Acceptance materials lack their original frozen plan')
    sources = materials.get('task_sources')
    if not isinstance(sources, dict) or any(not sources.get(cid) for cid in TASK_REQUIREMENTS):
        raise ValueError('Original task reading requirements are missing')
    return {cid: sources[cid] for cid in assigned if cid in TASK_REQUIREMENTS}


