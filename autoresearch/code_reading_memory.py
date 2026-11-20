"""Derived, receipt-bound code observations for subsequent model requests.

This module never edits reading history or resolves a scientific objection.
Coordinates refer to the exact frozen source string, which may be an excerpt
of a physical file. An observation is the author's judgment, not host approval.
"""
from __future__ import annotations

import ast
import copy
import functools
import hashlib
import json

from .architecture_validation import operation
from .execution_archive import encoded

KEY = 'code_reading_memory'
VERSION = 1


def wire_view(context):
    """Index duplicate repair-note prose only in a newly compiled wire view.

    Frozen v3 requests and authenticate() still reconstruct their original
    packet. The derived view has its own truthful hash; targets, prior audit,
    source packet and every unknown mirror item remain unchanged.
    """
    from .code_repair_contract import sha as view_sha
    if context.get('code_repair_tool_review') != 1 or context.get('audit_view_version') != 3:
        return context
    view = context.get('repair_packet_view', {})
    mirror = view.get('reading_memory', {})
    memory = context.get(KEY, {})
    owner = memory.get('owner', {})
    if (not isinstance(mirror, dict) or not isinstance(mirror.get('notes'), list)
            or not {'run_id', 'role', 'handoff_id'} <= owner.keys()
            or view.get('source_packet_sha256') != context.get('packet_sha256')
            or context.get('_purpose') != 'code_repair_audit:' + str(context.get('packet_sha256'))
            or view_sha(view) != context.get('audit_view_sha256')):
        return context
    directory = memory.get('directory', [])
    indexed, changed = [], 0
    for note in mirror['notes']:
        if not isinstance(note, dict):
            indexed.append(copy.deepcopy(note)); continue
        payload = {k: v for k, v in note.items() if k != 'observation_id'}
        entry = next((row for row in directory if row.get('handoff_id') and row.get('read_with')
            and code_note_id(owner['run_id'], owner['role'], row['handoff_id'], payload) == row['id']
            and note.get('observation_id', row['id']) == row['id']), None)
        if entry is None:
            indexed.append(copy.deepcopy(note)); continue
        indexed.append({'observation_id': entry['id'], 'key': entry['key'],
            'classification': entry['classification'], 'question_status': entry.get('question_status', 'unclassified'),
            'read_with': copy.deepcopy(entry['read_with']), 'scientific_approval': False})
        changed += 1
    if not changed:
        return context
    result = copy.deepcopy(context)
    derived = result['repair_packet_view']
    derived['reading_memory']['notes'] = indexed
    derived['reading_memory']['note_view'] = ('Exact own observations indexed; active questions are in code_reading_memory. '
        'Read note_id for the original conclusion and source ranges. Unknown items remain literal.')
    result['audit_view_sha256'] = view_sha(derived)
    result['code_repair_reading_projection'] = {'version': 1, **owner,
        'source_audit_view_sha256': context['audit_view_sha256'], 'derived_audit_view_sha256': result['audit_view_sha256'],
        'indexed_note_count': changed, 'original_records_preserved': True, 'scientific_approval': False}
    return result


def sha(value):
    return hashlib.sha256(encoded(value).encode('utf-8')).hexdigest()


def text_sha(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def analysis_schema():
    text = {'type': 'string', 'minLength': 1, 'maxLength': 1200, 'pattern': r'\S'}
    return {'type': 'object', 'properties': {'version': {'const': VERSION, 'type': 'integer'},
        'conclusion': text, 'open_questions': {'type': 'array', 'maxItems': 12,
            'uniqueItems': True, 'items': text},
        'supersedes': {'type': 'array', 'maxItems': 12, 'uniqueItems': True,
            'items': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}}},
        'required': ['version', 'conclusion', 'open_questions', 'supersedes'], 'additionalProperties': False}


def code_notes_schema():
    note = {'type': 'object', 'properties': {'key': {'type': 'string', 'minLength': 1, 'maxLength': 160},
        'statement': {'type': 'string', 'minLength': 1, 'maxLength': 1200}, 'code_analysis': analysis_schema(),
        'receipt_sha256s': {'type': 'array', 'minItems': 1, 'maxItems': 20, 'uniqueItems': True,
            'items': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}}},
        'required': ['key', 'statement', 'receipt_sha256s'], 'additionalProperties': False}
    return {'type': 'object', 'properties': {'notes': {'type': 'array', 'minItems': 1, 'maxItems': 6, 'items': note},
        'action': {'enum': ['save', 'read']}, 'note_id': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}},
        'additionalProperties': False, 'oneOf': [
            {'required': ['notes'], 'properties': {'action': {'const': 'save'}}, 'not': {'required': ['note_id']}},
            {'required': ['action', 'note_id'], 'properties': {'action': {'const': 'read'}}, 'not': {'required': ['notes']}}]}


GUIDANCE = ('\n代码多轮记忆：code_reading_memory 按文件、精确版本、函数和已投递范围关联本人原观察。'
    '读完决定性代码后在 record_notes 的每条 note 中保存可选 code_analysis={version:1,'
    'conclusion:简短结论,open_questions:未决问题列表,supersedes:明确替代的本人原笔记ID列表}。'
    '引用实际已投递读取回执；没有新结论也必须保留未决问题。只读到部分范围不能替代完整旧观察。'
    '旧自由文本是 legacy_unclassified，不能猜成问题已解决；分叉意见均保留。'
    '源码版本变化只保留历史索引，不继承判断。观察及其替代均不构成批准、执行许可或科学验收。')
GUIDANCE += ' 旧代码观察可用 code.record_notes({action:read,note_id:目录中的原ID})直接回读，无须重读源码。'


@functools.lru_cache(maxsize=8)
def symbols(path, text):
    """Line-inclusive ranges avoid AST's UTF-8 byte column offsets entirely."""
    if not path.endswith('.py'):
        return []
    try:
        # A leading BOM is one character in the frozen coordinate namespace.
        tree = ast.parse(text.removeprefix('\ufeff'))
    except (SyntaxError, ValueError):
        return []
    starts = [0]
    starts.extend(i + 1 for i, char in enumerate(text) if char == '\n')
    result = []
    def visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            name = prefix
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = '.'.join([*prefix, child.name])
                first = min([child.lineno, *[d.lineno for d in child.decorator_list]])
                start = starts[first - 1]
                end = starts[child.end_lineno] if child.end_lineno < len(starts) else len(text)
                result.append({'qualified_name': name, 'kind': type(child).__name__,
                    'char_start': start, 'char_end': end, 'start_line': first, 'end_line': child.end_lineno})
                name = [*prefix, child.name]
            visit(child, name)
    visit(tree, [])
    return result


def locator(row, text, start, end):
    return {'path': row['path'], 'file_sha256': row.get('file_sha256', row.get('sha256')),
        'source_sha256': text_sha(text), 'source_record_sha256': row.get('record_sha256', sha(row)),
        'coordinate_namespace': 'frozen_source_unicode_characters', 'char_start': start, 'char_end': end,
        'start_line': text.count('\n', 0, start) + 1,
        'end_line': text.count('\n', 0, max(start, end - 1)) + 1,
        'symbols': [copy.deepcopy(s) for s in symbols(row['path'], text) if s['char_start'] < end and start < s['char_end']]}


def _json_offset(value, path, lengths=None):
    """Locate a declared JSON value by structural path, never by text search."""
    lengths = {} if lengths is None else lengths
    def size(item):
        identity = id(item)
        if identity not in lengths: lengths[identity] = len(encoded(item))
        return lengths[identity]
    offset = 0
    for key in path:
        offset += 1  # opening object/array
        if isinstance(value, dict):
            for name in sorted(value):
                offset += size(name) + 1
                if name == key:
                    value = value[name]
                    break
                offset += size(value[name]) + 1
            else:
                raise ValueError('Code source JSON path disappeared')
        elif isinstance(value, list) and type(key) is int and 0 <= key < len(value):
            offset += sum(size(v) + 1 for v in value[:key])
            value = value[key]
        else:
            raise ValueError('Unsupported code source JSON path')
    return offset, value


def _material_origins(context):
    from . import review_material_packet as packets
    from .metric_capability_evidence_recovery import KEY as RECOVERY, target_fragment
    original = packets.source(context)
    marker = original.pop(RECOVERY, None)
    packet = packets.enabled(original)
    if (packet and not packets.supported(original)
            or not {'runtime_capability', 'previous_runtime_capability'} <= original.keys()):
        return {}
    from .metric_review_sources import compile_sources
    current, previous = original['runtime_capability'], original['previous_runtime_capability']
    compiled = compile_sources(current, previous)
    result, lengths, target_bodies = {}, {}, {}
    target_ids = {target['material_id'] for target in marker['targets']} if marker else set()
    def add(mid, value, path, row, source, start, end, plain=False):
        offset, scalar = (0, value) if plain else _json_offset(value, path, lengths)
        if scalar != source[start:end]:
            raise ValueError('Declared code scalar differs from its frozen source')
        result.setdefault(mid, []).append({'row': row, 'source': source, 'source_start': start,
            'source_end': end, 'material_offset': offset + (0 if plain else 1),
            'encoding': 'text' if plain else 'json_string'})
        if mid in target_ids and mid not in target_bodies:
            target_bodies[mid] = value if plain else packets.encoded(value)
    lookup = {(side, r['path'], sha(r)): r for side, rows in [('current', current), ('previous', previous)] for r in rows}
    for fragment in [*compiled['required_current_sources'], *compiled['optional_sources']]:
        row = lookup[(fragment['side'], fragment['path'], fragment['record_sha256'])]
        source = row['source']; a, b = fragment['char_start'], fragment['char_end']
        if fragment in compiled['required_current_sources']:
            mid = packets.sha(packets.encoded(['text', fragment['source']]))
            add(mid, fragment['source'], (), row, source, a, b, True)
        else:
            mid = packets.sha(packets.encoded(['json', fragment]))
            add(mid, fragment, ('source',), row, source, a, b)
    archive = packets.sha(packets.encoded(['json', original])) if packet else 'root'
    for side, rows, name in [('current', current, 'runtime_capability'), ('previous', previous, 'previous_runtime_capability')]:
        for index, row in enumerate(rows):
            mid = packets.sha(packets.encoded(['json', row]))
            add(mid, row, ('source',), row, row['source'], 0, len(row['source']))
            add(archive, original, (name, index, 'source'), row, row['source'], 0, len(row['source']))
    if marker:
        # A target of the directory or any non-code material has no source
        # coordinates. Build slices only from the original code-bearing bodies.
        base = {mid: tuple(result[mid]) for mid in target_bodies}
        for target in marker['targets']:
            mid = target['material_id']; start, end = target['start'], target['end']
            if mid not in target_bodies:
                continue
            fragment = target_fragment(target, target_bodies[mid])
            focused = packets.sha(packets.encoded(['text', fragment]))
            for origin in base[mid]:
                # Preserve the original JSON token namespace even in a slice.
                item = {**origin, 'material_offset': origin['material_offset'] - start,
                    'clip_start': 0, 'clip_end': end - start}
                if item not in result.setdefault(focused, []):
                    result[focused].append(item)
    return result


def _map_page(origins, page):
    result = []
    a, b = page['char_start'], page['char_end']
    for origin in origins:
        source = origin['source']; lo, hi = origin['source_start'], origin['source_end']
        offset = origin['material_offset']
        if origin['encoding'] == 'text':
            start, end = max(lo, lo + a - offset), min(hi, lo + b - offset)
        else:
            # Character token boundaries exclude partial JSON escape tokens.
            def boundary(index):
                return offset + len(encoded(source[lo:lo + index])[1:-1])
            length = hi - lo
            left, right = 0, length
            while left < right:
                middle = (left + right) // 2
                if boundary(middle) < a: left = middle + 1
                else: right = middle
            start = lo + left
            left, right = 0, length
            while left < right:
                middle = (left + right + 1) // 2
                if boundary(middle) <= b: left = middle
                else: right = middle - 1
            end = lo + left
        if start < end:
            item = locator(origin['row'], source, start, end)
            if item not in result: result.append(item)
    return result


@functools.lru_cache(maxsize=16)
def _cached_origins(serial):
    # Pure frozen-byte coordinate calculation only. No read delivery, role,
    # approval or filesystem verdict survives an operation in this cache.
    return _material_origins(json.loads(serial))


def _note_pages(ledger, refs):
    return [row for row in ledger if isinstance(row, dict)
        and isinstance(row.get('call'), dict) and row['call'].get('name') == 'reviews.read_material'
        and isinstance(row.get('result'), dict) and row['result'].get('receipt_sha256') in refs]


def _full_state(engine, rid, role, hid, *, for_save=False, navigation=False, note_id=None):
    from . import full_review_notes as notes, independent_full_review as full, review_material_packet as packets
    from .review_material_reuse import parent_input, science
    context, owners = notes._scope(engine, rid, role, hid)
    parents = context.get(packets.REUSE, {}).get(role, {}).get('parents', [])
    rows = [engine.store.get(notes.COLLECTION, note_id)] if note_id else engine.store.list(notes.COLLECTION, rid)
    saved_notes = [r for r in rows if r and r.get('run_id') == rid and r.get('role') == role]
    candidates = {*owners, *[p['handoff_id'] for p in parents]}
    if not for_save and not any(r.get('handoff_id') in candidates for r in saved_notes):
        return context, {}, [], []
    from collections import defaultdict
    from .evidence_delivery import delivery_spans, covering_receipts
    from .full_review_receipts import acquired_material
    from .harness_activity import checkpoint

    by_owner = defaultdict(list)
    for row in saved_notes:
        if row.get('handoff_id') in candidates:
            by_owner[row['handoff_id']].append(row)
    refs = {ref for rows in by_owner.values() for row in rows for ref in row['note']['receipt_sha256s']}
    available = {}
    for owner in owners:
        frozen = engine.store.get('handoff_inputs', owner)['context']
        ledger = engine.store.get('handoff_progress', owner, {}).get('tool_results', [])
        available[owner] = (frozen, ledger, None)
    science_sha = science(context) if parents else None
    for parent in parents:
        owner = parent['handoff_id']
        # An unused parent need not be authenticated as a reading source.
        # Its pages may still be cited by a note saved on a continuation.
        progress = engine.store.get('handoff_progress', owner, {})
        ledger = progress.get('tool_results', [])
        if (navigation or note_id) and not by_owner[owner] and not _note_pages(ledger, refs):
            continue
        frozen, hand, progress = parent_input(engine.store, rid, owner, reading_only=True)
        if hand['to_role'] != role or hand['input_hash'] != parent['input_hash'] or science(frozen['context']) != science_sha:
            raise ValueError('Code observation inheritance changed role or scientific scope')
        available.setdefault(owner, (frozen['context'], progress.get('tool_results', []), parent['material_sha256']))

    reads, observations, focus, navigable = {}, [], [], {}
    readers = {}
    checkpoint('code_memory_navigation', 0, len(available), '份源码记忆', role)
    for position, (owner, (frozen, ledger, material_sha)) in enumerate(available.items(), 1):
        if navigation and material_sha is not None:
            # This derived directory is only a locator. Before a note can
            # become active we still reconstruct its cited pages and check
            # their submitted delivery using the original packet.
            pages = []
            for receipt in _note_pages(ledger, refs):
                call, page = receipt['call'], receipt['result']
                body = {k: v for k, v in page.items() if k != 'receipt_sha256'}
                if (page.get('policy_sha256') != frozen[full.KEY]['sha256']
                        or page.get('material_sha256') != material_sha
                        or page['receipt_sha256'] != full.sha({'call': call, 'result': body})):
                    raise ValueError('Historical code observation page changed its original receipt identity')
                pages.append((call, page))
        else:
            selected = _note_pages(ledger, refs) if note_id else ledger
            reader, pages = acquired_material(engine.store, rid, role, owner, frozen, selected)
            readers[owner] = reader
        if pages:
            original = packets.source(frozen)
            origins = _cached_origins(packets.encoded(original))
            for call, page in pages:
                mid = call['arguments'].get('material_id', 'root')
                anchors = _map_page(origins.get(mid, []), page)
                if owner == hid and anchors:
                    focus.append(anchors)
                code_only = any(o['encoding'] == 'text' and o['material_offset'] <= page['char_start']
                    and page['char_end'] <= o['material_offset'] + o['source_end'] - o['source_start']
                    for o in origins.get(mid, []))
                navigable[page['receipt_sha256']] = {'anchors': anchors, 'row': {'call': call, 'result': page},
                    'handoff_id': owner, 'code_only': code_only, 'material_id': mid}
        if by_owner[owner]:
            index = (full.material_index(frozen) if not navigation or material_sha is None
                else {'policy_sha256': frozen[full.KEY]['sha256'], 'sha256': material_sha})
            for row in by_owner[owner]:
                if (row.get('id') != full.sha({k: v for k, v in row.items() if k not in {'id', 'created_at'}})
                        or row.get('policy_sha256') != index['policy_sha256'] or row.get('material_sha256') != index['sha256']):
                    raise ValueError('Code observation changed immutable material identity')
                observations.append({'id': row['id'], 'handoff_id': owner, 'note': row['note'],
                    'read_with': {'name': notes.TOOL, 'arguments': {'action': 'read', 'note_id': row['id']}}})
        checkpoint('code_memory_navigation', position, len(available), '份源码记忆', role)
    focus = [a for page in focus[-8:] for a in page]
    wanted = set(navigable)
    if navigation:
        current = packets.source(context)['runtime_capability']
        versions = {(row['path'], row['sha256'], text_sha(row['source']), sha(row),
            'frozen_source_unicode_characters') for row in current}
        wanted = set()
        by_id = {r['id']: r for r in observations}
        for row in observations:
            refs = row['note']['receipt_sha256s']
            anchors = [a for ref in refs if ref in navigable for a in navigable[ref]['anchors']]
            row['navigation_anchors'] = anchors
            current_version = bool(anchors) and all(_identity(a) in versions for a in anchors)
            if current_version and (row['note'].get('code_analysis') or any(_related(a, f) for a in anchors for f in focus)):
                wanted.update(refs)
                # A successor cannot hide an old objection using navigation
                # alone. Authenticate every explicitly superseded obligation.
                stack = list(row['note'].get('code_analysis', {}).get('supersedes', [])); seen = set()
                while stack:
                    identity = stack.pop()
                    if identity in seen: continue
                    seen.add(identity); old = by_id.get(identity)
                    if old:
                        wanted.update(old['note']['receipt_sha256s'])
                        stack.extend(old['note'].get('code_analysis', {}).get('supersedes', []))
    authenticate = defaultdict(list)
    for ref in wanted & navigable.keys():
        item = navigable[ref]
        if item['handoff_id'] not in readers:
            authenticate[item['handoff_id']].append(item['row'])
    for owner, selected in authenticate.items():
        frozen, _, _ = available[owner]
        readers[owner], _ = acquired_material(engine.store, rid, role, owner, frozen, selected)
    coverage = {}
    for ref in sorted(wanted & navigable.keys()):
        item = navigable[ref]; mid = item['material_id']; owner = item['handoff_id']
        reader = readers[owner]
        key = (owner, mid)
        if key not in coverage:
            index, text = reader.index(mid), reader.text(mid)
            coverage[key] = delivery_spans(engine.store, rid, role, index['document_id'], index['sha256'],
                identity_key='document_id', handoff_id=owner, text=text)
        page = item['row']['result']
        if covering_receipts(coverage[key], page['char_start'], page['char_end']):
            reads[ref] = {k:v for k,v in item.items() if k != 'material_id'}
    return context, reads, observations, focus


def code_note_id(rid, role, hid, note):
    return sha({'version': VERSION, 'run_id': rid, 'role': role, 'handoff_id': hid, 'note': note})


def _whole_history(engine, rid, role, context):
    """Only explicit, frozen same-workflow parent votes form historical links."""
    from . import independent_full_review as full
    result, seen = [], set()
    current = context
    while feedback := current.get('prior_code_review_feedback'):
        marker = current.get('whole_code_review', {})
        if (feedback.get('version') != 1 or feedback.get('sha256') != full.sha({k:v for k,v in feedback.items() if k != 'sha256'})
                or marker.get('version') != 1 or not full.members(feedback.get('votes', []))):
            raise ValueError('Historical code observations lack their frozen workflow feedback')
        vote = next((v for v in feedback['votes'] if v['role'] == role), None)
        if not vote or full.KEY not in vote:
            break  # Legacy bare decisions do not authenticate a new note alias.
        prior = full.authenticate(engine.store, rid, vote)['context']
        old = prior.get('whole_code_review', {})
        hid = vote[full.KEY]['handoff_id']
        if (hid in seen or old.get('version') != 1 or old.get('workflow_id') != marker.get('workflow_id')
                or old.get('attempt') != feedback['source_attempt'] or old['attempt'] >= marker['attempt']
                or prior.get('code_sha256') != feedback.get('source_code_sha256')
                or prior.get('delivery_hash') != feedback.get('source_delivery_hash')
                or not current.get('proposal_hash') or prior.get('proposal_hash') != current['proposal_hash']):
            raise ValueError('Historical code observations changed role/run/workflow/attempt provenance')
        result.append(hid); seen.add(hid); current = prior
    return result


def _code_state(engine, rid, role, hid, *, for_save=False):
    from . import code_navigation as nav
    from .independent_full_review import code_delivery_scope
    from .code_whole_review import permits_tool
    from .evidence_delivery import delivery_spans, covering_receipts
    context = nav.frozen(engine, rid, role, hid)
    reads, observations, focus = {}, [], []
    scoped = code_delivery_scope(context)
    if scoped:
        from .implementation_alignment_evidence import _full_handoffs
        owners = _full_handoffs(engine, rid, role, hid, context)
    else:
        owners = nav.memory_owners(engine, rid, role, hid, context)
        owners = list(dict.fromkeys([*owners, *_whole_history(engine, rid, role, context)]))
    for owner in owners:
        raw = engine.store.get('handoff_inputs', owner, {}).get('context', {})
        purpose = engine.store.get('handoffs', owner, {}).get('purpose', '')
        if not nav.enabled(raw, purpose): continue
        prior = nav.frozen(engine, rid, role, owner)
        # The navigation owner adapters authenticate inheritance. Keep judgment
        # inheritance narrower: the exact packet or exact original assignment.
        history_only = False
        if owner != hid:
            from .review_inheritance import _same_version
            if not (prior.get('packet_sha256') and prior.get('packet_sha256') == context.get('packet_sha256')
                    or _same_version(prior, context) and all(prior.get(key) == context.get(key)
                        for key in ('cycle', 'research_cycle', 'experiment_plan_hash', 'design_hash'))):
                history_only = True
        files = {r['artifact_id']: r for r in nav.sources(engine, rid, prior).values()}
        texts, coverage = {}, {}
        ledger = engine.store.get('handoff_progress', owner, {}).get('tool_results', [])
        if not for_save and not any(r.get('call', {}).get('name') == 'code.record_notes'
                and r.get('call', {}).get('arguments', {}).get('notes') for r in ledger):
            continue
        for row in ledger:
            call, page = row.get('call', {}), row.get('result', {})
            if not isinstance(page, dict) or page.get('error'): continue
            name = call.get('name'); args = call.get('arguments', {})
            if scoped and not permits_tool(context, name, args):
                continue
            if name == 'code.record_notes':
                values = args.get('notes', [])
                if values and page.get('notes') != values and ('observation_ids' in page
                        or any('code_analysis' in note for note in values)):
                    raise ValueError('Immutable code observation call differs from its saved note result')
                if values and page.get('notes') == values:
                    identities = [code_note_id(rid, role, owner, note) for note in values]
                    if ('observation_ids' in page or any('code_analysis' in note for note in values)) and (
                            page.get('observation_ids') != identities or page.get('scientific_approval') is not False):
                        raise ValueError('Immutable code observation ID differs from its original note payload')
                    for note in values:
                        observations.append({'id': code_note_id(rid, role, owner, note),
                            'handoff_id': owner, 'note': note, 'history_only': history_only,
                            'read_with': {'name': 'code.record_notes', 'arguments': {'action': 'read',
                                'note_id': code_note_id(rid, role, owner, note)}}})
                continue
            if name in {'documents.read', 'documents.recall'}:
                if name == 'documents.recall':
                    nav.verify_recall(engine, rid, role, owner, args, page)
                    delivered = bool(page.get('entries'))
                else:
                    from .document_index import read as document_read
                    from .implementation_reading import coverage as document_coverage
                    if document_read(engine, rid, args) != page:
                        raise ValueError('Code observation document receipt differs from original source')
                    delivered = document_coverage(engine, rid,
                        {'document_id': page['document_id'], 'sha256': page['sha256']},
                        [{'result': page}], [owner], include_memory=False)['complete']
                    # Legacy untracked ledgers are not submitted-byte proof.
                    from .evidence_delivery import tracking_required
                    delivered = delivered and tracking_required(engine.store, rid, owner)
                if delivered:
                    for ref in nav.read_receipt_ids(row):
                        reads[ref] = {'anchors': [], 'row': row, 'handoff_id': owner, 'code_only': False, 'history_only': history_only}
                continue
            source = files.get(args.get('id'))
            if name not in {'code.read', 'artifacts.read'} or source is None or not isinstance(page.get('text'), str):
                continue
            identity = source['artifact_id']
            if identity not in texts:
                try:
                    texts[identity] = nav.body(engine, rid, prior, source)
                except (OSError, ValueError) as exc:
                    if not history_only: raise
                    for ref in nav.read_receipt_ids(row):
                        reads[ref] = {'anchors': [], 'row': row, 'handoff_id': owner, 'code_only': False,
                            'history_only': True, 'unavailable': {'code': 'historical_source_unavailable', 'cause': type(exc).__name__}}
                    continue
            text = texts[identity]; start = page.get('offset'); end = start + len(page['text']) if type(start) is int else -1
            if (page.get('artifact_id') != identity or page.get('sha256') != source['sha256']
                    or type(start) is not int or not 0 <= start < end <= len(text) or text[start:end] != page['text']
                    or name == 'code.read' and nav.read(engine, rid, role, owner, args) != page):
                raise ValueError('Code observation read differs from its frozen source')
            anchors = [locator(source, text, start, end)]
            if owner == hid: focus = [*focus, *anchors][-12:]
            if identity not in coverage:
                coverage[identity] = delivery_spans(engine.store, rid, role, identity, source['sha256'],
                    identity_key='artifact_id', handoff_id=owner, text=text)
            if covering_receipts(coverage[identity], start, end):
                for ref in nav.read_receipt_ids(row):
                    reads[ref] = {'anchors': anchors, 'row': row, 'handoff_id': owner, 'code_only': True, 'history_only': history_only}
    return context, reads, observations, focus


def _anchors(note, reads):
    return [a for ref in note['receipt_sha256s'] for a in reads[ref]['anchors']]


def _identity(anchor):
    return tuple(anchor[k] for k in ('path', 'file_sha256', 'source_sha256', 'source_record_sha256', 'coordinate_namespace'))


def _covers(new, old):
    from .evidence_delivery import covering_receipts
    return all(covering_receipts([n for n in new if _identity(n) == _identity(o)], o['char_start'], o['char_end']) for o in old)


def validate_notes(state, notes, *, allow_history=False):
    from .prose_schema import validate
    _, reads, previous, _ = state
    known = {row['id']: row for row in previous}
    for note in notes:
        if 'code_analysis' not in note: continue
        validate(note['code_analysis'], analysis_schema())
        if (set(note['receipt_sha256s']) - reads.keys() or not _anchors(note, reads)
                or not allow_history and any(reads[ref].get('history_only') for ref in note['receipt_sha256s'])):
            raise ValueError('Code analysis requires actual submitted same-role code ranges')
        for identity in note['code_analysis']['supersedes']:
            old = known.get(identity)
            if (old is None or not allow_history and old.get('history_only') or set(old['note']['receipt_sha256s']) - reads.keys()
                    or any(ref not in note['receipt_sha256s'] for ref in old['note']['receipt_sha256s']
                        if not reads[ref]['code_only'])
                    or not _anchors(old['note'], reads) or not _covers(_anchors(note, reads), _anchors(old['note'], reads))):
                raise ValueError('Superseding a code observation requires its exact ID and complete same-version source coverage')


@operation
def state(engine, rid, role, hid, *, full_review=False, for_save=False, navigation=False, note_id=None):
    if not getattr(engine.store, 'is_read_snapshot', False):
        with engine.store.read_snapshot() as snapshot:
            owner = copy.copy(engine); owner.store = snapshot
            return state(owner, rid, role, hid, full_review=full_review, for_save=for_save,
                navigation=navigation, note_id=note_id)
    if full_review:
        return _full_state(engine, rid, role, hid, for_save=for_save, navigation=navigation, note_id=note_id)
    return _code_state(engine, rid, role, hid, for_save=for_save)


def _related(left, right):
    if _identity(left) != _identity(right): return False
    if left['char_start'] < right['char_end'] and right['char_start'] < left['char_end']: return True
    def functions(anchor):
        values = [s for s in anchor['symbols'] if s['kind'] != 'ClassDef']
        return {(s['qualified_name'], s['char_start'], s['char_end']) for s in values if not any(s != other
            and s['char_start'] <= other['char_start'] and other['char_end'] <= s['char_end'] for other in values)}
    return bool(functions(left) & functions(right))


def read_note(engine, rid, role, hid, identity):
    _, reads, observations, _ = state(engine, rid, role, hid)
    row = next((r for r in observations if r['id'] == identity), None)
    if row is None or not row.get('history_only') and set(row['note']['receipt_sha256s']) - reads.keys():
        raise ValueError('Code observation ID lacks this role\'s authorized original submitted sources')
    unavailable = [ref for ref in row['note']['receipt_sha256s'] if ref not in reads or reads[ref].get('unavailable')]
    return {'notes': [copy.deepcopy(row['note'])], 'observation_ids': [identity],
        'original_handoff_id': row['handoff_id'], 'scientific_approval': False, 'reading_evidence': False,
        'new_reading_progress': False, 'history_only': row.get('history_only', False),
        'run_id': rid, 'role': role, 'source_ranges': [] if unavailable else _anchors(row['note'], reads),
        'source_evidence_status': 'unavailable' if unavailable else 'authenticated_original_delivery',
        'unavailable_source_receipts': unavailable}


def _scoped_directory(context, directory, rows, owners):
    """Project the authenticated graph; never filter its validation inputs."""
    import re
    from . import review_material_packet as packets
    controls = {k: v for k, v in context.items() if k not in {
        packets.REUSE, 'runtime_capability', 'previous_runtime_capability',
        'changed_runtime_sources', 'capability_check_evidence', 'approved_base_plan',
        'approved_source_design'}}
    explicit = set(re.findall(r'(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])', encoded(controls)))
    selected = {r['id'] for r in directory if r['version_status'] == 'current_exact'
        or r['handoff_id'] in owners or r['id'] in explicit}
    edges = [(r['id'], old) for r in rows for old in r['note'].get('code_analysis', {}).get('supersedes', [])]
    while True:
        expanded = selected | {identity for edge in edges if selected.intersection(edge) for identity in edge}
        if expanded == selected: break
        selected = expanded
    groups = {}
    for row in directory:
        if row['id'] in selected: continue
        # Historical locators are grouped by exact file/source version. The
        # original function/range graph remains host-owned and is returned by
        # read-by-ID, rather than duplicating every old AST range in each turn.
        anchors = row.get('anchors') or [{}]
        for anchor in anchors:
            source = {k: anchor[k] for k in ('path', 'file_sha256', 'source_sha256') if k in anchor}
            group = groups.setdefault(encoded(source), {**source, 'note_ids': set()})
            group['note_ids'].add(row['id'])
    history = {'by_source_version': [{**value, 'note_ids': sorted(value['note_ids'])}
        for _, value in sorted(groups.items())],
        'read_tool': 'reviews.record_notes',
        'policy': 'Read an exact note_id to recover original function/range locators and observations. Historical entries remain unresolved; they confer no current-version judgment.'}
    return [r for r in directory if r['id'] in selected], history


@operation
def project(engine, rid, role, hid, prepared, *, full_review=False):
    if not getattr(engine.store, 'is_read_snapshot', False):
        with engine.store.read_snapshot() as snapshot:
            owner = copy.copy(engine); owner.store = snapshot
            return project(owner, rid, role, hid, prepared, full_review=full_review)
    if full_review:
        from . import review_material_packet as packets
        frozen = engine.store.get('handoff_inputs', hid, {}).get('context', {})
        if not packets.enabled(frozen): return prepared
    context, reads, rows, focus = state(engine, rid, role, hid, full_review=full_review, navigation=True)
    if not rows:
        result = copy.deepcopy(prepared)
        result['context'][KEY] = {'version': VERSION, 'directory': [], 'observations': [],
            'decisive_reads': [], 'focus': focus, 'scientific_approval': False,
            'owner': {'run_id': rid, 'role': role, 'handoff_id': hid}}
        return result
    if full_review:
        current = packets.source(context)['runtime_capability']
        versions = {(row['path'], row['sha256'], text_sha(row['source']), sha(row),
            'frozen_source_unicode_characters') for row in current}
    else:
        from . import code_navigation as nav
        # Metadata-only current-version comparison; body() is authenticated for
        # actual read targets above, never for every file in the delivery.
        files = nav.sources(engine, rid, context)
        versions = {(row['path'], row['sha256'], sha(row)) for row in files.values()}
    authenticated, unavailable_history = [], []
    for row in rows:
        if row.get('navigation_anchors') and set(row['note']['receipt_sha256s']) - reads.keys():
            anchors = row['navigation_anchors']
            current_version = all(_identity(a) in versions for a in anchors)
            unavailable_history.append({'id': row['id'], 'handoff_id': row['handoff_id'], 'key': row['note']['key'],
                'anchors': anchors, 'receipt_sha256s': row['note']['receipt_sha256s'],
                'classification': 'structured_role_observation' if row['note'].get('code_analysis') else 'legacy_unclassified',
                'version_status': 'current_exact' if current_version else 'stale_version',
                'source_evidence_status': 'not_checked_for_navigation', 'reading_evidence': False,
                'active': False, 'body_selected': False, 'read_with': row['read_with']})
            continue
        if row.get('history_only') and any(ref not in reads or reads[ref].get('unavailable') for ref in row['note']['receipt_sha256s']):
            unavailable_history.append({'id': row['id'], 'handoff_id': row['handoff_id'], 'key': row['note']['key'],
                'version_status': 'historical_scope', 'source_evidence_status': 'unavailable',
                'receipt_sha256s': row['note']['receipt_sha256s'], 'active': False, 'body_selected': False,
                'read_with': row['read_with']})
            continue
        if set(row['note']['receipt_sha256s']) - reads.keys(): continue
        anchors = _anchors(row['note'], reads)
        if not anchors: continue
        validate_notes((None, reads, rows, None), [row['note']], allow_history=True)
        authenticated.append({**row, 'anchors': anchors})
    successors = {}
    for row in authenticated:
        for old in row['note'].get('code_analysis', {}).get('supersedes', []):
            successors.setdefault(old, []).append(row['id'])
    directory, active, fragments, seen = unavailable_history, [], [], {}
    from .independent_full_review import code_delivery_scope
    scoped_delivery = code_delivery_scope(context)
    for row in authenticated:
        note = row['note']; analysis = note.get('code_analysis')
        same_version = not row.get('history_only') and all((_identity(a) if full_review else
            (a['path'], a['file_sha256'], a['source_record_sha256'])) in versions for a in row['anchors'])
        related = any(_related(a, f) for a in row['anchors'] for f in focus)
        # A new read of a related function does not reopen a closed observation.
        # Legacy free text has no authenticated resolution status. Index it as
        # unclassified and keep its exact reopen entry, without guessing that
        # it is resolved or automatically replaying all its prose.
        selected = (same_version and row['id'] not in successors and bool(analysis and analysis['open_questions'])
            and (related or not scoped_delivery))
        reason = 'current_focus' if related else 'other_unresolved'
        directory.append({'id': row['id'], 'handoff_id': row['handoff_id'], 'key': note['key'],
            'anchors': row['anchors'], 'receipt_sha256s': note['receipt_sha256s'],
            'classification': 'structured_role_observation' if analysis else 'legacy_unclassified',
            'version_status': 'current_exact' if same_version else 'historical_scope' if row.get('history_only') else 'stale_version',
            'successors': sorted(successors.get(row['id'], [])), 'read_with': row['read_with'],
            'active': same_version and row['id'] not in successors, 'body_selected': selected,
            'question_status': ('unresolved' if analysis['open_questions'] else 'no_listed_open_questions')
                if analysis else 'unclassified', 'scientific_approval': False})
        if not selected: continue
        active.append({'id': row['id'], 'classification': directory[-1]['classification'],
            'selection_reason': reason,
            **({'open_questions': [{'text': q} for q in analysis['open_questions']]}
                if analysis else {'statement': note['statement']}), 'scientific_approval': False})
        for ref in note['receipt_sha256s']:
            read = reads[ref]
            if not read['anchors']: continue
            key = sha(read['row'])
            if key in seen:
                if related: fragments[seen[key]]['selection_reason'] = reason
                continue
            seen[key] = len(fragments)
            fragments.append({'original_handoff_id': read['handoff_id'], 'original_receipt_sha256': ref,
                'selection_reason': reason, 'reopen': copy.deepcopy(read['row']['call']),
                'new_reading_progress': False})
    result = copy.deepcopy(prepared)
    history = None
    if full_review and context.get('review_memory_context_policy') == 'index-v1':
        from .full_review_notes import _scope
        _, owners = _scope(engine, rid, role, hid)
        directory, history = _scoped_directory(context, directory, rows, owners)
    result['context'][KEY] = {'version': VERSION, 'focus': focus, 'directory': directory,
        'owner': {'run_id': rid, 'role': role, 'handoff_id': hid},
        'observations': active, 'decisive_reads': [], 'decisive_read_index': fragments, 'scientific_approval': False,
        'policy': ('Unresolved questions outside the current source focus stay in the directory. '
            if scoped_delivery else 'Only unresolved questions and relevant unclassified observations are active. ') + (
            'Prior conclusions and exact original reads are indexed and available on demand. '
            'No listed open questions is not scientific approval. Forks remain active; supersession is not scientific resolution.'),
        'source_window_overflow': 'Archived fields remain obligations; reopen their exact original calls before deciding.'}
    if history is not None: result['context'][KEY]['history'] = history
    from .full_review_window import bound
    result['context'] = bound(result['context'])
    memory = result['context'][KEY]
    memory['omitted_decisive_reads'] = [{'original_handoff_id': row['original_handoff_id'],
        'original_receipt_sha256': row['original_receipt_sha256'], 'selection_reason': row['selection_reason'],
        'reopen': row['reexhibited_original_read']['call'],
        'archived_fields': row['reexhibited_original_read']['result']['archived_source_fields']}
        for row in memory['decisive_reads'] if row['reexhibited_original_read']['result'].get('archived_source_fields')]
    return result
