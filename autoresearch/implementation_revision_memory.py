"""Explicit, version-bound author continuity for new revision requests.

This is a disposable context view, never a new tool receipt or reading verdict.
The previous author's notes and output are reusable knowledge; only the current
review findings are new work. Independent review and saved-document gates retain
their own actual-reading requirements.
"""
from __future__ import annotations

import copy
import hashlib
import json

from .implementation_review_routing import digest

KEY = 'implementation_revision_memory'
FIELD_LIMIT = 16000
NOTE_LIMIT = 6000
CURRENT_FIELD_LIMIT = 48000
SOURCE_TEXT_LIMIT = 64000
GUIDANCE = ('\nimplementation_revision_memory 是宿主核验的同一作者上一版上下文。'
    '先读本轮修改意见，沿用其中 previous_output、working_notes 和已读来源记录完成定向修改；'
    '不要为新修订轮次重新通读上一版、架构、定义或计划。未改字段由编辑工具原样保留。'
    'historical_reading 记录过去已交付的精确版本范围，不表示这些原文全部在本次请求中。'
    '只在所需细节未保留、原文换版、笔记冲突或修改依赖无法判断时，按精确 SHA 和章节补读。'
    'previous_output 是上一版基线；已保存新草稿时以 implementation_document_base 的新标识编辑，'
    '不要用上一版字段覆盖本轮已保存修改。旧笔记中的 open_issue 是历史观察，当前修改范围以本轮意见为准。'
    '历史阅读与作者笔记不代替对新保存文档的核对，也不授予独立审核批准。')
GUIDANCE += ('\n记忆 version>=2 时，current_output 是当前编辑基线：fields 覆盖旧值，inherited_fields '
    '明确列出的字段从 previous_output.fields 原样继承；其余字段不可猜测。'
    'restored_reading 的 text 是已经按当前 SHA 恢复到本请求的来源原文，不只是阅读索引。'
    '这些正文可直接用于本轮修订，不必再调用 documents.read。先根据全部修改意见给出修订并保存；'
    '只有 omitted_fields、未恢复的范围或新版本需要定向取回。保存后的当前稿自检仍须真实读取。')
GUIDANCE += ('\nversion=3 在每次新请求前同步真实保存稿和本交接最新读取结果。'
    'freshness 标识这份记忆绑定的文档版本及读取账本；成功保存的部分立即成为新基线，失败部分不会覆盖它。'
    '本轮新取回的正文优先进入 restored_reading，未改且仍在授权范围的内容继续复用。'
    '新保存稿尚未核对的范围仍须读取；已经取回并进入本次记忆的同版本范围可直接复用。'
    '已冻结或正在执行的旧请求保持原快照，下一次请求才切换到更新后的记忆。')


def _current_output(base, previous_fields, fields, *, saved, priority_fields=()):
    from .implementation_document_locator import catalog
    selected, inherited, used = {}, [], 0
    priority = set(priority_fields)
    for name in sorted(fields, key=lambda name: (name not in priority, name != 'summary', name)):
        if name not in base['spec']:
            continue
        value = base['spec'][name]
        size = len(json.dumps(value, ensure_ascii=False))
        if used + size > CURRENT_FIELD_LIMIT:
            continue
        used += size
        if name in previous_fields and digest(value) == digest(previous_fields[name]):
            inherited.append(name)
        else:
            selected[name] = copy.deepcopy(value)
    available = set(selected) | set(inherited)
    return {'document': base['document'], 'spec_hash': base['spec_hash'],
        'fields': selected, 'inherited_fields': inherited,
        'omitted_fields': sorted(set(base['spec']) - available),
        'field_sections': {**{item['title']: item['index_id'] for item in catalog(base['spec'])
                             if item['parent_id'] is None and item['index_id'] in available},
                           **{name: name for name in available}},
        'saved_in_this_handoff': saved}


def _restore_reading(engine, run_id, directories, history, hid, *, current_document=None, priority_ranges=()):
    """Refresh from exact durable reads first, then retain older delivered text.

    Retrieval may precede delivery. Including retrieved text in this new packet
    is how it becomes available; it never manufactures a receipt or credit.
    """
    from . import document_index
    from .document_read_index import _known_read, _in_directory, _merge
    from .engine import ResearchBlocked
    current_document = current_document or {}
    current_identity = (current_document.get('document_id'), current_document.get('sha256'))
    allowed = {did: doc for did, doc in directories.items()
        if doc.get('kind') != 'document_working_notes' and (doc.get('kind') != 'implementation_specification'
            or (did, doc['sha256']) == current_identity)}
    progress = engine.store.get('handoff_progress', hid, {})
    if progress and (progress.get('id') != hid or progress.get('run_id') != run_id):
        raise ResearchBlocked('修订记忆的最新读取账本不属于当前作者交接。')
    cache, candidates = {}, []

    def source(doc):
        key = (doc['document_id'], doc['sha256'])
        if key not in cache:
            cache[key] = document_index.load(engine, run_id, *key)
        return cache[key]

    # Newest reads must not be crowded out by an earlier full source window.
    for row in reversed(progress.get('tool_results', [])):
        if not isinstance(row, dict) or row.get('predecessor_handoff_id') is not None:
            continue
        call, page = row.get('call', {}), row.get('result', {})
        if call.get('name') != 'documents.read' or not isinstance(page, dict) or page.get('error'):
            continue
        args = call.get('arguments', {})
        doc = allowed.get(args.get('document_id'))
        if doc is None or args.get('expected_sha256') != doc['sha256']:
            continue  # Historical versions never populate a current-version entry.
        manifest, text = source(doc)
        if (not _known_read(row) or not _in_directory(row, doc)
                or page['text'] != text[page['char_start']:page['char_end']]):
            raise ResearchBlocked('修订记忆的最新读取正文、版本或范围与保存记录不一致。')
        section = next(s for s in manifest['sections'] if s['id'] == page['section_id'])
        candidates.append((doc, section, page['char_start'], page['char_end'], 'current_handoff_read'))

    for prior in history:
        doc = allowed.get(prior['document_id'])
        if doc is None or doc['sha256'] != prior['sha256'] or doc.get('kind') == 'implementation_specification':
            continue
        manifest, _ = source(doc)
        for section in manifest['sections']:
            spans = _merge([(r['start'], r['end']) for r in prior['covered_ranges'] if r['section_id'] == section['id']])
            for start, end in spans:
                if not section['start'] <= start < end <= section['end']:
                    raise ResearchBlocked('修订记忆的历史正文范围与版本不一致。')
                candidates.append((doc, section, start, end, 'historical_delivery'))

    if priority_ranges:
        from .implementation_revision_worklist import MAX_READS
        # Keep an entire fresh read batch deliverable before restoring pending
        # findings' older evidence. Never turn a locator into a new read receipt.
        fresh = [c for c in candidates if c[4] == 'current_handoff_read'][:MAX_READS]
        preferred = []
        for ref in priority_ranges:
            for doc, section, start, end, origin in candidates:
                if (doc['document_id'], doc['sha256'], section['id']) != (
                        ref['document_id'], ref['sha256'], ref['section_id']):
                    continue
                for left, right in ref['ranges']:
                    a, b = max(start, left), min(end, right)
                    if a < b:
                        preferred.append((doc, section, a, b, origin))
        candidates = [*fresh, *preferred, *candidates]

    restored, selected, used = [], {}, 0
    for doc, section, start, end, origin in candidates:
        key = (doc['document_id'], doc['sha256'], section['id'])
        spans = [(start, end)]
        for left, right in selected.get(key, []):
            spans = [(a, b) for a, b in spans if b <= left or a >= right] + [
                piece for a, b in spans if a < right and b > left
                for piece in ((a, min(b, left)), (max(a, right), b)) if piece[0] < piece[1]]
        _, text = source(doc)
        for start, end in sorted(spans):
            end = min(end, start + SOURCE_TEXT_LIMIT - used)
            if start >= end:
                continue
            value = text[start:end]
            restored.append({'document_id': doc['document_id'], 'sha256': doc['sha256'], 'section_id': section['id'],
                'section_start': section['start'], 'section_end': section['end'],
                'char_start': start, 'char_end': end, 'text': value, 'origin': origin,
                'text_sha256': hashlib.sha256(value.encode()).hexdigest()})
            selected[key] = _merge([*selected.get(key, []), [start, end]])
            used += len(value)
    return restored


def read_from_memory(context, call):
    """Withhold only reads whose exact current content is already in the request."""
    memory = context.get(KEY, {})
    if (context.get('implementation_document_authoring_protocol') != 1
            or call.get('name') != 'documents.read' or memory.get('version') not in (2, 3)):
        return None
    validate(context)
    args = call.get('arguments', {})
    from .tool_contracts import validate_arguments, ToolArgumentsError
    try:
        validate_arguments('documents.read', args, context)
    except ToolArgumentsError:
        return None  # Normal tool validation reports malformed arguments.
    did, sha, sid = (args.get(key) for key in ('document_id', 'expected_sha256', 'section_id'))
    offset, limit = args.get('offset', 0), args.get('limit', 4000)
    parts = [part for part in memory['restored_reading']
             if (part['document_id'], part['sha256'], part['section_id']) == (did, sha, sid)]
    if parts:
        from .document_read_index import _merge
        start = parts[0]['section_start'] + offset
        end = min(start + limit, parts[0]['section_end'])
        if any(a <= start < end <= b for a, b in _merge([(p['char_start'], p['char_end']) for p in parts])):
            return {'path': KEY + '.restored_reading', 'document_id': did, 'section_id': sid}
    current = memory['current_output']
    if current['saved_in_this_handoff'] or (did, sha) != (
            current['document']['document_id'], current['document']['sha256']):
        return None
    section = next((s for s in current['document'].get('sections', []) if s['id'] == sid), None)
    if section and 0 <= offset < section['end'] - section['start']:
        field = current['field_sections'].get(section['title'])
        if field:
            return {'path': KEY + ('.current_output.fields.' if field in current['fields']
                                   else '.previous_output.fields.') + field,
                    'document_id': did, 'section_id': sid, 'index_id': field}
    return None


def _author(engine, run_id, hid, workflow_id, revision):
    from .engine import Harness, ResearchBlocked
    assignment = engine.store.get('handoffs', hid, {})
    inputs = engine.store.get('handoff_inputs', hid, {})
    payload = {key: inputs.get(key) for key in ('instruction', 'context', 'schema')}
    binding = (inputs.get('context') or {}).get('implementation_document_authoring', {})
    if (assignment.get('id') != hid or assignment.get('run_id') != run_id
            or assignment.get('to_role') != 'engineer' or inputs.get('run_id') != run_id
            or binding.get('workflow_id') != workflow_id or binding.get('revision') != revision
            or not assignment.get('purpose', '').startswith('implementation_spec:')
            or assignment.get('input_hash') != digest(payload)
            or Harness.decision_key(run_id, 'engineer', assignment['purpose'], **payload) != hid):
        raise ResearchBlocked('修订记忆的原作者、版本或冻结输入不一致。')
    return inputs


def _sources(engine, run_id, state, previous):
    """Follow only the submitted author and its authenticated save predecessor."""
    from . import implementation_authoring as author
    from .engine import ResearchBlocked
    hid = previous.get('authoring_handoff_id')
    if not hid:
        return []  # Historical rows without an author cannot manufacture memory.
    inputs = _author(engine, run_id, hid, state['id'], previous['index'])
    record = engine.store.get(author.COLLECTION, hid)
    if not record:
        return []
    author._checked(engine, run_id, record)
    if (record.get('status') != 'submitted' or record.get('workflow_id') != state['id']
            or record.get('context') != inputs['context']
            or any(record['row'].get(k) != previous.get(k) for k in ('index', 'spec_hash', 'spec', 'document'))):
        raise ResearchBlocked('修订记忆不属于已提交的紧邻前版。')
    author.validate_submission(engine, run_id, state, previous)
    sources = [hid]
    completion = inputs['context'].get('implementation_document_completion')
    if completion:
        source = completion.get('source_handoff_id')
        original = _author(engine, run_id, source, state['id'], previous['index'])
        audit = engine.store.get('stage_contexts', completion.get('predecessor_audit_id', ''), {})
        payload = audit.get('payload', {})
        if (source == hid or completion.get('phase') != 'save'
                or completion.get('predecessor_audit_id') != source + ':save-predecessor:v1'
                or audit.get('run_id') != run_id or audit.get('successor_id') != hid
                or audit.get('sha256') != completion.get('predecessor_sha256')
                or digest(payload) != audit.get('sha256') or payload.get('input') != original
                or payload.get('progress') != engine.store.get('handoff_progress', source)):
            raise ResearchBlocked('修订记忆的保存前驱归档已改变。')
        sources.insert(0, source)
    return sources


def _notes(engine, run_id, sources, allowed):
    from .document_working_notes import _checked
    entries, archives = [], []
    for hid in sources:
        record = _checked(engine, run_id, 'engineer', hid,
                          engine.store.get('handoff_progress', hid, {}))
        if not record:
            continue
        archives.append({'handoff_id': hid, 'document': record['document'], 'sha256': record['sha256']})
        for entry in record['payload']['entries']:
            # A note anchored to a superseded SPEC or changed source cannot
            # masquerade as knowledge about the current immutable inputs.
            if all(allowed.get(a['document_id']) == a['expected_sha256'] for a in entry['anchors']):
                if entry not in entries:
                    entries.append(copy.deepcopy(entry))
    selected = []
    for entry in reversed(entries):
        candidate = [entry, *selected]
        if len(json.dumps(candidate, ensure_ascii=False)) <= NOTE_LIMIT:
            selected = candidate
    return {'selected': selected, 'eligible_notes': len(entries),
            'omitted_notes': len(entries) - len(selected), 'archives': archives,
            'scientific_claims_verified': False}


def prepare(engine, run_id, role, hid, context):
    from . import implementation_phase_context as phase, implementation_authoring_preservation as preservation
    if not phase.author(context, role) or not context['implementation_document_authoring']['revision']:
        return context
    from . import implementation_authoring as author, document_index, implementation_reading as reading
    from .evidence_delivery import tracking_required
    _, frozen, state, binding = author._assignment(engine, run_id, role, hid)
    latest = engine.store.get(author.COLLECTION, hid)
    if latest:
        author._checked(engine, run_id, latest)
    current = preservation.baseline(engine, run_id, state, binding, frozen, latest)
    previous = preservation.baseline(engine, run_id, state, binding, frozen, None)
    from .implementation_acceptance_author import CONTINUATION
    if CONTINUATION in frozen:
        # The edit baseline is the current partial draft; reusable prior-review
        # knowledge still belongs to the authenticated previous submitted author.
        previous = next(row for row in state['specifications'] if row['index'] == binding['revision'] - 1)
    sources = _sources(engine, run_id, state, previous)
    if not sources:
        return context
    directories = {d['document_id']: d for d in context['indexed_documents']
                   if d.get('kind') != 'document_working_notes'}
    directories[previous['document']['document_id']] = previous['document']
    directories[current['document']['document_id']] = current['document']
    allowed = {did: doc['sha256'] for did, doc in directories.items()}
    tracked = [source for source in sources if tracking_required(engine.store, run_id, source)]
    read_versions = set()
    for source in tracked:
        for receipt in engine.store.get('handoff_progress', source, {}).get('tool_results', []):
            page = receipt.get('result', {})
            if (receipt.get('call', {}).get('name') == 'documents.read' and isinstance(page, dict)
                    and not page.get('error') and receipt.get('predecessor_handoff_id') is None):
                read_versions.add((page.get('document_id'), page.get('sha256')))
    history = []
    for did, doc in directories.items():
        if (did, doc['sha256']) not in read_versions:
            continue
        manifest, _ = document_index.load(engine, run_id, did, doc['sha256'])
        required = [{'result': {'section_id': s['id'], 'char_start': s['start'], 'char_end': s['end']}}
                    for s in manifest['sections'] if s['end'] > s['start']]
        proof = reading.coverage(engine, run_id, doc, required, tracked)
        if proof['covered_chars']:
            history.append({'document_id': did, 'sha256': doc['sha256'],
                            'covered_ranges': proof['covered_ranges'], 'covered_chars': proof['covered_chars']})
    from .implementation_acceptance import criterion_fields
    fields = {'summary'}
    if context.get('implementation_acceptance_protocol') == 1:
        for issue in context.get('acceptance_issues', []):
            fields.update(criterion_fields(context['acceptance_contract'], issue['criterion_id']))
    else:
        fields.update(previous['spec'])
    selected = {}
    for name in sorted(fields, key=lambda name: (name != 'summary', name)):
        if name in previous['spec']:
            candidate = {**selected, name: previous['spec'][name]}
            if len(json.dumps(candidate, ensure_ascii=False)) <= FIELD_LIMIT:
                selected = candidate
    changed = {name for name in current['spec'] if digest(current['spec'][name]) != digest(previous['spec'].get(name))}
    ledger = engine.store.get('handoff_progress', hid, {}).get('tool_results', [])
    from . import implementation_revision_worklist as worklist
    from .implementation_acceptance_author import enabled as batch_author
    from .implementation_revision_batch import status as batch_status
    plan = worklist.dependencies(context, current, batch_status(engine, hid, context, latest)) if batch_author(context) else None
    memory = {'version': 3, 'role': role, 'workflow_id': state['id'], 'revision': binding['revision'],
        'source_handoff_ids': sources, 'previous_output': {
            'revision': previous['index'], 'spec_hash': previous['spec_hash'], 'document': previous['document'],
            'fields': copy.deepcopy(selected),
            'field_sha256': {name: digest(value) for name, value in previous['spec'].items()},
            'omitted_fields': sorted(set(previous['spec']) - set(selected))},
        'working_notes': _notes(engine, run_id, sources, allowed), 'historical_reading': history,
        'current_output': _current_output(current, selected, set(current['spec']), saved=bool(latest), priority_fields=changed),
        'restored_reading': _restore_reading(engine, run_id, directories, history, hid,
            current_document=current['document'], priority_ranges=plan['source_ranges'] if plan else ()),
        'freshness': {'handoff_id': hid, 'document_id': current['document']['document_id'],
            'document_sha256': current['document']['sha256'], 'spec_hash': current['spec_hash'],
            'ledger_receipts': len(ledger), 'ledger_sha256': digest(ledger),
            'saved_versions': len((latest or {}).get('versions', []))},
        'reading_credit': False, 'scientific_approval': False,
        'new_information': '本轮审核修改意见；历史正文按需取回，不要求重复通读。'}
    if plan is not None:
        memory[worklist.KEY] = worklist.complete(plan, memory)
    memory['sha256'] = digest(memory)
    return {**context, KEY: memory, 'document_policy':
        '本轮是实现文档作者修订。优先使用 implementation_revision_memory 中当前稿字段和已恢复来源原文，'
        '不要重新通读已载入内容。仅对未载入范围、变更版本或保存后的自检调用 documents.read；'
        '每次读取仍受原工具范围限制。记忆恢复不产生新的阅读回执或独立审核批准。'}


def validate(context):
    value = context.get(KEY)
    if value is None:
        return
    from .engine import ResearchBlocked
    binding = context.get('implementation_document_authoring', {})
    if (not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] not in (1, 2, 3)
            or value.get('role') != 'engineer'
            or value.get('workflow_id') != binding.get('workflow_id')
            or value.get('revision') != binding.get('revision')
            or value.get('previous_output', {}).get('revision') != binding.get('revision', 0) - 1
            or value.get('reading_credit') is not False or value.get('scientific_approval') is not False
            or value.get('sha256') != digest({k: v for k, v in value.items() if k != 'sha256'})):
        raise ResearchBlocked('修订记忆的当前绑定或内容指纹已改变。')
    if value['version'] >= 2:
        current = value.get('current_output', {})
        inherited = current.get('inherited_fields', [])
        parts = value.get('restored_reading', [])
        if (not isinstance(current.get('fields'), dict) or not isinstance(parts, list)
                or not set(inherited) <= set(value['previous_output']['fields'])
                or set(inherited).intersection(current['fields'])
                or any(not isinstance(part.get('text'), str)
                    or part['char_end'] - part['char_start'] != len(part['text'])
                    or not part['section_start'] <= part['char_start'] < part['char_end'] <= part['section_end']
                    or hashlib.sha256(part['text'].encode()).hexdigest() != part.get('text_sha256')
                    for part in parts)):
            raise ResearchBlocked('修订记忆的已恢复正文或当前稿字段不完整。')
        if value['version'] == 3:
            fresh = value.get('freshness', {})
            if (fresh.get('document_id') != current['document']['document_id']
                    or fresh.get('document_sha256') != current['document']['sha256']
                    or fresh.get('spec_hash') != current['spec_hash']
                    or type(fresh.get('ledger_receipts')) is not int or fresh['ledger_receipts'] < 0
                    or type(fresh.get('saved_versions')) is not int or fresh['saved_versions'] < 0):
                raise ResearchBlocked('修订记忆的最新保存版本或读取账本标识不一致。')
            plan = value.get('revision_worklist')
            if plan is not None and (plan.get('version') != 1
                    or plan.get('document_id') != current['document']['document_id']
                    or plan.get('spec_hash') != current['spec_hash']
                    or plan.get('reading_credit') is not False or plan.get('scientific_approval') is not False):
                raise ResearchBlocked('修订工作清单与当前保存版本不一致。')
