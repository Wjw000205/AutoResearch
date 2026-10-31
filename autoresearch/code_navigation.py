"""Read-only code navigation, exact pages and disposable model working sets.

Tools only see this role's immutable delivery. Locating a symbol never grants
reading credit. Raw receipts stay in the journal after their prompt copy leaves.
"""
from __future__ import annotations

import ast
import copy
import hashlib

from . import code_repair_contract as repair

TOOLS = ('code.locate', 'code.read', 'code.record_notes', 'documents.recall')
HELP = {
    'code.locate': 'arguments={path?:exact relative file path,query?:literal symbol or text,offset?:nonnegative integer,limit?:integer 1..30}. Searches only this frozen delivery. With no path/query lists files; with path returns matching symbols/line locations. Returns artifact_id,sha256,offset,limit,start_line,end_line; navigation only, not reading evidence. Use code.read for decisive code.',
    'code.read': 'arguments={id:artifact_id,expected_sha256:64-hex,offset?:nonnegative character offset,limit?:integer 1..6000}. Reads only the requested exact frozen UTF-8 source range, preserving newlines. Returns receipt_sha256 and line/character coordinates. Read only relevant functions/callers; do not request whole repositories. Reopen with the same arguments if needed after eviction.',
    'code.record_notes': 'arguments={notes:[{key:string,statement:string up to 1200 chars,receipt_sha256s:string[]}]}. Save concise observations or unresolved questions bound to successful code.read/documents.read receipts from this same role and handoff. No source body, new instructions, code approval or execution authorization. Save conclusions before requesting the next pages; consumed source text leaves subsequent context.',
    'documents.recall': 'arguments={document_id:string,expected_sha256:string,section_id?:string,query?:literal text,offset?:integer,limit?:integer 1..4}. Retrieve this role\'s already journaled same-version document passages and anchored notes. Prefer this before documents.read. Returns exact cached ranges with original handoff/receipt identity; no new source-reading claim. A miss means only missing memory; use documents.search/read for decisive gaps.',
}
GUIDANCE = ('\n代码按需阅读：先 code.locate 定位函数、调用处或关键词，再 code.read 读取必要范围。'
    '目录和搜索命中不算已读。只能以批准实现文档的具体条款判定代码偏差；'
    '不能自创算法、参数、阈值、实验或测试门槛。文档未定义的行为保留待核实，不能下发修改。'
    '每批原文看完后，用 code.record_notes 保存带读取回执的简短发现和未决问题；'
    '继续读取时旧源码自动退出上下文，仅保留哈希、位置、笔记和回查入口。'
    '文档先 documents.recall 检索本角色同版本已读记忆；只在缺失、歧义或版本变化时 documents.read。'
    '补读不是重做已完成阶段，已保存同版本结论不因正文移出而作废。')


def enabled(context, purpose):
    return (context.get('code_repair_tool_review') == 1 or
        context.get('implementation_definition_repair_protocol') == 1 and bool(context.get('definition_code_sources'))
        and purpose.split(':',1)[0] in {'implementation_spec','implementation_definition_review'} or
        context.get('implementation_alignment_protocol') == 1 and purpose.split(':', 1)[0] == 'code_review')


def frozen(engine, run_id, role, hid):
    hand = engine.store.get('handoffs', hid, {})
    inputs = engine.store.get('handoff_inputs', hid, {})
    context = inputs.get('context', {})
    if (hand.get('id') != hid or inputs.get('id') != hid
            or hand.get('run_id') != run_id or inputs.get('run_id') != run_id
            or hand.get('to_role') != role or not enabled(context, hand.get('purpose', ''))
            or hand.get('input_hash') != repair.sha({k: inputs.get(k) for k in ('instruction', 'context', 'schema')})):
        raise ValueError('Code tools require this role and run\'s exact frozen handoff')
    return context


def packet_for(engine, run_id, context):
    identity = context['packet_sha256']
    saved = engine.store.get('code_repair_packets', identity, {})
    packet = saved.get('packet')
    if saved.get('run_id') != run_id or not packet or repair.sha(packet) != identity:
        raise ValueError('Code tools require the unchanged frozen repair packet')
    return packet


def sources(engine, run_id, context):
    if context.get('code_repair_tool_review') == 1:
        packet = packet_for(engine, run_id, context)
        return {p: {**row, 'path': p} for p, row in packet['audit_sources']['code_files'].items()}
    return {row['path']: row for row in context.get('definition_code_sources',context.get('delivery_artifacts',[]))}


def body(engine, run_id, context, source):
    if context.get('code_repair_tool_review') == 1:
        from .code_repair_audit_view import full_text
        text = full_text(packet_for(engine, run_id, context), source)
        if hashlib.sha256(text.encode('utf-8')).hexdigest() != source['sha256']:
            raise ValueError('Frozen code bytes changed')
        return text
    from .implementation_alignment_evidence import _code_text
    return _code_text(engine, run_id, source['artifact_id'], source)


def coordinates(text, start, end):
    return {'offset': start, 'limit': end - start, 'start_line': text.count('\n', 0, start) + 1,
            'end_line': text.count('\n', 0, max(start, end - 1)) + 1}


def locate(engine, run_id, role, hid, args):
    context = frozen(engine, run_id, role, hid)
    files = sources(engine, run_id, context)
    path, query = args.get('path'), args.get('query', '')
    offset, limit = args.get('offset', 0), args.get('limit', 20)
    if (set(args) - {'path', 'query', 'offset', 'limit'} or not isinstance(query, str)
            or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 30
            or path is not None and path not in files):
        raise ValueError('Code location must stay within the frozen delivery with bounded literal search')
    entries = []
    for name, source in sorted(files.items()):
        if path is not None and path != name:
            continue
        meta = {k: source[k] for k in ('artifact_id', 'sha256', 'path')}
        if path is None and not query:
            entries.append(meta)
            continue
        try:
            text = body(engine, run_id, context, source)
        except UnicodeDecodeError:
            continue  # Binary delivery outputs are never decoded into a prompt.
        lines = text.splitlines(keepends=True)
        starts, cursor = [], 0
        for line in lines:
            starts.append(cursor)
            cursor += len(line)
        spans = []
        try:
            tree = ast.parse(text) if name.endswith('.py') else None
        except (SyntaxError, ValueError):
            tree = None
        for node in ast.walk(tree) if tree else ():
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and (not query or query.casefold() in node.name.casefold()):
                start = starts[node.lineno - 1]
                end = starts[node.end_lineno] if node.end_lineno < len(starts) else len(text)
                spans.append({**meta, 'symbol': node.name, **coordinates(text, start, end)})
        if query:
            for number, line in enumerate(lines):
                if query.casefold() in line.casefold() and not any(s['start_line'] == number + 1 for s in spans):
                    spans.append({**meta, **coordinates(text, starts[number], starts[number] + len(line))})
        elif not spans:
            spans.append({**meta, **coordinates(text, 0, min(len(text), 6000))})
        entries.extend(sorted(spans, key=lambda row: row['offset']))
    if offset > len(entries):
        raise ValueError('Code location offset exceeds matches')
    end = min(offset + limit, len(entries))
    return {'entries': entries[offset:end], 'total_entries': len(entries),
            'next_offset': end if end < len(entries) else None, 'reading_evidence': False}


def read(engine, run_id, role, hid, args):
    context = frozen(engine, run_id, role, hid)
    matches = [s for s in sources(engine, run_id, context).values() if s['artifact_id'] == args.get('id')]
    offset, limit = args.get('offset', 0), args.get('limit', 4000)
    if (set(args) - {'id', 'expected_sha256', 'offset', 'limit'} or len(matches) != 1
            or args.get('expected_sha256') != matches[0]['sha256']
            or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 6000):
        raise ValueError('Code read requires the exact frozen file, SHA256 and bounded range')
    source = matches[0]
    text = body(engine, run_id, context, source)
    if offset >= len(text):
        raise ValueError('Code read range exceeds source')
    end = min(len(text), offset + limit)
    page = {'artifact_id': source['artifact_id'], 'sha256': source['sha256'], 'path': source['path'],
        'text': text[offset:end], 'total_chars': len(text), 'next_offset': end if end < len(text) else None,
        'truncated': end < len(text), 'untrusted': True, **coordinates(text, offset, end)}
    page['receipt_sha256'] = repair.sha({'call': {'name': 'code.read', 'arguments': args}, 'result': page})
    return page


def read_receipt_id(row):
    page = row.get('result', {})
    return page.get('receipt_sha256') or repair.sha(row)


def read_receipt_ids(row):
    """The exposed tool, journal and recalled-source IDs identify the same read.

    Recalled-source aliases are valid only via this delivered recall result;
    callers still authenticate the entire recall against the original journal.
    """
    values = {read_receipt_id(row), repair.sha(row)}
    if row.get('call', {}).get('name') == 'documents.recall':
        values.update(e['original_receipt_sha256'] for e in row['result'].get('entries', []))
    return values


def audit_predecessors(engine, run_id, role, context):
    """Only this auditor's exact same-packet sealed predecessors are reusable."""
    result, seen = [], set()
    previous = context.get('repair_packet_view', {}).get('prior_audit')
    while previous and previous.get('tool_review'):
        owner = previous['handoff_id']
        if owner in seen:
            raise ValueError('Audit reading memory contains a cycle')
        seen.add(owner)
        hand = engine.store.get('handoffs', owner, {})
        inputs = engine.store.get('handoff_inputs', owner, {})
        decision = engine.store.get('decisions', owner, {})
        source = inputs.get('context', {})
        verdict = decision.get('result', {})
        if (previous.get('role') != role or hand.get('run_id') != run_id or hand.get('to_role') != role
                or hand.get('status') != 'completed' or inputs.get('run_id') != run_id
                or source.get('code_repair_tool_review') != 1 or source.get('packet_sha256') != context.get('packet_sha256')
                or decision.get('run_id') != run_id or decision.get('role') != role
                or previous.get('result') != {k: verdict.get(k) for k in repair.AUDIT_SCHEMA['properties']}
                or hand.get('output_hash') != repair.sha(verdict)
                or hand.get('input_hash') != repair.sha({k: inputs.get(k) for k in ('instruction', 'context', 'schema')})):
            raise ValueError('Audit reading memory is not this role\'s unchanged sealed packet')
        result.append(owner)
        previous = source.get('repair_packet_view', {}).get('prior_audit')
    return result


def memory_owners(engine, run_id, role, hid, context):
    owners = [hid, *audit_predecessors(engine, run_id, role, context)]
    refs = [*context.get(repair.FIELD, {}).get('document_memory_handoffs', []),
            *context.get('definition_code_followup', {}).get('document_memory_handoffs', [])]
    if context.get('code_repair_tool_review') == 1:
        packet = packet_for(engine, run_id, context)
        refs = [*refs, *packet.get('clarification', {}).get('votes', [])]
    for ref in refs:
        if ref['role'] != role or ref['handoff_id'] in owners:
            continue
        old_id = ref['handoff_id']
        old = engine.store.get('handoffs', old_id, {})
        inputs = engine.store.get('handoff_inputs', old_id, {})
        decision = engine.store.get('decisions', old_id, {})
        if (old.get('run_id') != run_id or old.get('to_role') != role or old.get('status') != 'completed'
                or inputs.get('run_id') != run_id or decision.get('role') != role
                or decision.get('run_id') != run_id or old.get('output_hash') != repair.sha(decision.get('result'))
                or old.get('input_hash') != repair.sha({k: inputs.get(k) for k in ('instruction', 'context', 'schema')})):
            raise ValueError('Prior document memory is not this role\'s sealed evidence')
        owners.append(old_id)
    return owners


def recall(engine, run_id, role, hid, args):
    """Retrieve exact old pages, not an unbound LLM recollection or fresh read."""
    context = frozen(engine, run_id, role, hid)
    sources_to_read = memory_owners(engine, run_id, role, hid, context)
    docs = context.get('indexed_documents', [])
    if context.get('code_repair_tool_review') == 1:
        packet = packet_for(engine, run_id, context)
        docs = packet['audit_sources']['documents']
    if not any(d['document_id'] == args.get('document_id') and d['sha256'] == args.get('expected_sha256') for d in docs):
        raise ValueError('Document memory must match the current approved document version')
    offset, limit, query = args.get('offset', 0), args.get('limit', 2), args.get('query', '')
    if (set(args) - {'document_id', 'expected_sha256', 'section_id', 'query', 'offset', 'limit'}
            or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 4
            or not isinstance(query, str)):
        raise ValueError('Document memory query is outside its bounded contract')
    entries = {}
    from .document_index import load
    _, original = load(engine, run_id, args['document_id'], args['expected_sha256'])
    for owner in sources_to_read:
        progress = engine.store.get('handoff_progress', owner, {})
        memory_coverage = None
        for row in progress.get('tool_results', []):
            page = row.get('result', {})
            if (row.get('call', {}).get('name') != 'documents.read' or not isinstance(page, dict)
                    or page.get('error') or not isinstance(page.get('text'), str)
                    or page.get('document_id') != args['document_id'] or page.get('sha256') != args['expected_sha256']
                    or args.get('section_id') and page.get('section_id') != args['section_id']
                    or query and query.casefold() not in page['text'].casefold()):
                continue
            # Authenticate the cached bytes against the immutable frozen source,
            # without a new model/source-read event or a new reading credit.
            start, end = page.get('char_start'), page.get('char_end')
            if type(start) is not int or type(end) is not int or original[start:end] != page['text']:
                raise ValueError('Persisted document memory bytes changed')
            from .implementation_reading import coverage
            if memory_coverage is None:
                required = [{'result': r['result']} for r in progress.get('tool_results', [])
                    if r.get('call', {}).get('name') == 'documents.read' and isinstance(r.get('result'), dict)
                    and not r['result'].get('error') and r['result'].get('document_id') == args['document_id']
                    and r['result'].get('sha256') == args['expected_sha256']]
                memory_coverage = coverage(engine, run_id,
                    {'document_id': args['document_id'], 'sha256': args['expected_sha256']},
                    required, [owner], include_memory=False)['covered_ranges']
            cursor = start
            for span in sorted((s for s in memory_coverage if s['section_id'] == page['section_id']), key=lambda s: s['start']):
                if span['start'] <= cursor:
                    cursor = max(cursor, span['end'])
            if cursor < end:
                continue  # Retrieved but never delivered text is not prior reading memory.
            inner = max(0, page['text'].casefold().find(query.casefold()) - 200) if query else 0
            text = page['text'][inner:inner + 1800]
            entry = {k: page[k] for k in ('document_id', 'sha256', 'section_id')}
            entry.update(text=text, char_start=start + inner, char_end=start + inner + len(text),
                original_handoff_id=owner, original_receipt_sha256=repair.sha(row),
                provenance='same_role_same_version_saved_read', new_source_reading=False)
            notes = progress.get('document_working_notes', {}).get('payload', {}).get('entries', [])
            entry['notes'] = [{'kind': n['kind'], 'text': n['text'], 'anchors': n['anchors']} for n in notes
                if any(a.get('document_id') == args['document_id'] and a.get('expected_sha256') == args['expected_sha256']
                       and a.get('section_id') == page['section_id'] for a in n.get('anchors', []))][:3]
            entries[(entry['char_start'], entry['char_end'])] = entry
    values = list(entries.values())
    end = min(offset + limit, len(values))
    result = {'entries': values[offset:end], 'total_entries': len(values),
        'next_offset': end if end < len(values) else None, 'new_source_reading': False,
        'scientific_approval': False}
    result['receipt_sha256'] = repair.sha({'call': {'name': 'documents.recall', 'arguments': args}, 'result': result})
    return result


def notes(engine, run_id, role, hid, args):
    context = frozen(engine, run_id, role, hid)
    owners = [hid, *audit_predecessors(engine, run_id, role, context)]
    rows = [r for owner in owners for r in engine.store.get('handoff_progress', owner, {}).get('tool_results', [])]
    known = {ref for r in rows if r.get('call', {}).get('name') in {'code.read', 'artifacts.read', 'documents.read', 'documents.recall'}
             and isinstance(r.get('result'), dict) and (r['result'].get('text') or r['result'].get('entries')) and not r['result'].get('error')
             for ref in read_receipt_ids(r)}
    values = args.get('notes')
    if set(args) != {'notes'} or not isinstance(values, list) or not 1 <= len(values) <= 6:
        raise ValueError('Code notes require one to six concise observations')
    for note in values:
        if (not isinstance(note, dict) or set(note) != {'key', 'statement', 'receipt_sha256s'}
                or not isinstance(note['key'], str) or not note['key']
                or not isinstance(note['statement'], str) or not note['statement']
                or not isinstance(note['receipt_sha256s'], list) or not 1 <= len(note['receipt_sha256s']) <= 20
                or any(ref not in known for ref in note['receipt_sha256s'])):
            raise ValueError('Code notes must cite successful actual reads from this same handoff')
    return {'notes': copy.deepcopy(values), 'scientific_approval': False,
            'provenance': 'role_observation_over_journaled_reads'}


def verify_recall(engine, run_id, role, hid, args, result):
    """Check saved hits individually; later reads must not change old receipts."""
    context = frozen(engine, run_id, role, hid)
    allowed = set(memory_owners(engine, run_id, role, hid, context))
    docs = context.get('indexed_documents', [])
    if context.get('code_repair_tool_review') == 1:
        packet = packet_for(engine, run_id, context)
        docs = packet['audit_sources']['documents']
    if not any(d['document_id'] == args.get('document_id') and d['sha256'] == args.get('expected_sha256') for d in docs):
        raise ValueError('Recalled document version is not approved in this assignment')
    payload = {k: v for k, v in result.items() if k != 'receipt_sha256'}
    if result.get('receipt_sha256') != repair.sha({'call': {'name': 'documents.recall', 'arguments': args}, 'result': payload}):
        raise ValueError('Document memory result hash changed')
    for entry in result.get('entries', []):
        owner = entry['original_handoff_id']
        if owner not in allowed:
            raise ValueError('Document memory belongs to a different role or workflow')
        hand = engine.store.get('handoffs', owner, {})
        frozen_input = engine.store.get('handoff_inputs', owner, {})
        if (hand.get('run_id') != run_id or hand.get('to_role') != role or frozen_input.get('run_id') != run_id
                or hand.get('input_hash') != repair.sha({k: frozen_input.get(k) for k in ('instruction', 'context', 'schema')})):
            raise ValueError('Document memory owner cannot be authenticated')
        ledger = engine.store.get('handoff_progress', owner, {}).get('tool_results', [])
        matches = [r for r in ledger if repair.sha(r) == entry['original_receipt_sha256']]
        # Repeating the exact same read can journal the same content-addressed
        # receipt more than once. Authenticate its bytes, not its occurrence
        # count; never rewrite the original ledger or grant extra reading.
        if (not matches or any(row != matches[0] for row in matches[1:])
                or matches[0].get('call', {}).get('name') != 'documents.read'):
            raise ValueError('Document memory lacks its original read receipt')
        page = matches[0]['result']
        a, b = entry['char_start'] - page['char_start'], entry['char_end'] - page['char_start']
        if (any(entry[k] != page[k] for k in ('document_id', 'sha256', 'section_id'))
                or entry['document_id'] != args['document_id'] or entry['sha256'] != args['expected_sha256']
                or not 0 <= a < b <= len(page['text']) or page['text'][a:b] != entry['text']):
            raise ValueError('Document memory range differs from the exact saved reading')
        from .document_index import load
        _, text = load(engine, run_id, entry['document_id'], entry['sha256'])
        if text[entry['char_start']:entry['char_end']] != entry['text']:
            raise ValueError('Document memory does not match the frozen source')
        from .implementation_reading import coverage
        if not coverage(engine, run_id, {'document_id': entry['document_id'], 'sha256': entry['sha256']},
                [{'result': entry}], [owner], include_memory=False)['complete']:
            raise ValueError('Document memory was retrieved but not delivered to the original role')


def project(engine, run_id, role, hid, progress, prepared):
    context = prepared['context']
    if not enabled(context, context.get('_purpose', '')):
        return prepared
    from .code_review_context import _consumed
    from .working_context import sha
    consumed = _consumed(engine, run_id, role, hid, progress)
    kinds = {'code.read', 'artifacts.read'}
    if context.get('code_repair_tool_review') == 1:
        kinds.add('documents.read')
    result = copy.deepcopy(prepared)
    for row in result['context'].get('tool_results', []):
        page = row.get('result', {})
        name = row.get('call', {}).get('name')
        if name == 'documents.recall' and sha(row) in consumed:
            original_sha = sha(row)
            for entry in page.get('entries', []):
                entry.pop('text', None)
                entry['text_returned'] = False
            page['reopen'] = copy.deepcopy(row['call'])
            page['consumed_read_reference'] = {'receipt_sha256': original_sha, 'reopen': copy.deepcopy(row['call'])}
            continue
        if name not in kinds or not isinstance(page, dict) or page.get('error') or not isinstance(page.get('text'), str):
            continue
        receipt = read_receipt_id(row)
        delivered = copy.deepcopy(row)
        delivered['result'].setdefault('receipt_sha256', receipt)
        original_sha = sha(row)
        if original_sha in consumed or sha(delivered) in consumed:
            row['result'] = {k: v for k, v in page.items() if k != 'text'}
            row['result'].update(text_returned=False, receipt_sha256=receipt,
                consumed_read_reference={'receipt_sha256': original_sha, 'reopen': copy.deepcopy(row['call']),
                    'scope': 'Already delivered; exact original remains in handoff_progress. Not new reading.'})
        else:
            page.setdefault('receipt_sha256', receipt)
    return result


def tool_limit(progress):
    reads = {repair.sha(r['call']) for r in progress.get('tool_results', [])
        if r.get('call', {}).get('name') in {'code.read', 'documents.read', 'documents.recall'}
        and (r.get('result', {}).get('text') or r.get('result', {}).get('entries'))}
    return 8 + len(reads)  # Real new evidence earns continuation; normal run budgets still apply.
