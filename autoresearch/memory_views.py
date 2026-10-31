"""Bounded, read-only projections of memory and exact model-input evidence.

These views never retrieve for an agent, rebuild an index, or claim that a
prepared context was sent. Legacy records retain explicit evidence gaps.
"""
from __future__ import annotations

import hashlib
import json

ROLES = ('director', 'engineer', 'scout')
SEARCH_TOOLS = {'memory.search', 'evidence.search', 'literature.rag_search',
                'documents.search', 'context.expand'}


def _page(items, total, limit, offset, **extra):
    more = offset + len(items) < total
    return {'schema_version': 1, 'items': items, 'total': total, 'limit': limit,
            'offset': offset, 'has_more': more,
            'next_offset': offset + len(items) if more else None, **extra}


def _projection(fields, *, alias=''):
    data = alias + 'data'
    return ','.join(f"'{field}',json_extract({data},'$.{path}')" for field, path in fields.items())


def _list(store, collection, run_id, fields, *, predicate='', parameters=(), limit=20, offset=0):
    where = "collection=? AND json_extract(data,'$.run_id')=?"
    args = [collection, run_id]
    if predicate:
        where += ' AND (' + predicate + ')'
        args.extend(parameters)
    with store._read_db() as db:
        total = db.execute('SELECT count(*) FROM records WHERE ' + where, args).fetchone()[0]
        rows = db.execute('SELECT json_object(' + _projection(fields) + ') FROM records WHERE ' + where +
                          ' ORDER BY updated_at DESC,key DESC LIMIT ? OFFSET ?', [*args, limit, offset])
        items = [json.loads(row[0]) for row in rows]
    return items, total


def _owned(store, collection, key, run_id):
    # Apply ownership in SQL before a possibly large body crosses the boundary.
    with store._read_db() as db:
        row = db.execute("SELECT data FROM records WHERE collection=? AND key=? "
                         "AND json_extract(data,'$.run_id')=?", (collection, key, run_id)).fetchone()
    if not row:
        raise KeyError('Record not found for this research task')
    return json.loads(row[0])


def _snippet(value, clean, cap=1000):
    value = clean(value)
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    return text[:cap] + ('… [truncated]' if len(text) > cap else '')


def _task_fields():
    return {'id': 'id', 'role': 'to_role', 'purpose': 'purpose', 'summary': 'summary',
            'status': 'status', 'stage': 'stage', 'created_at': 'created_at', 'updated_at': 'updated_at',
            'attempts': 'attempts', 'reused_count': 'reused_count'}


def tasks_view(store, run_id, *, clean, role=None, limit=20, offset=0):
    rows, total = _list(store, 'handoffs', run_id, _task_fields(),
                        predicate="json_extract(data,'$.to_role')=?" if role else '',
                        parameters=(role,) if role else (), limit=limit, offset=offset)
    return clean(_page(rows, total, limit, offset, run_id=run_id))


def history_view(store, run_id, *, clean, role=None, limit=20, offset=0):
    where = "collection='transcripts' AND json_extract(data,'$.run_id')=? AND json_extract(data,'$.kind')='output'"
    args = [run_id]
    if role:
        where += " AND json_extract(data,'$.role')=?"
        args.append(role)
    fields = {'id': 'id', 'role': 'role', 'created_at': 'created_at', 'provider': 'provider',
              'model': 'model', 'operation_id': 'operation_id', 'request_id': 'request_id',
              'purpose': 'request_scope.purpose'}
    projection = _projection(fields) + ", 'text_preview', substr(_memory_clean_text(json_extract(data,'$.text')),1,1200), 'text_chars',length(_memory_clean_text(json_extract(data,'$.text')))"
    with store._read_db() as db:
        db.create_function('_memory_clean_text', 1, lambda value: clean(value or ''))
        total = db.execute('SELECT count(*) FROM records INDEXED BY transcript_output_operation WHERE ' + where, args).fetchone()[0]
        rows = [json.loads(row[0]) for row in db.execute('WITH page AS MATERIALIZED (SELECT key FROM records INDEXED BY transcript_output_operation WHERE ' + where +
            ' ORDER BY updated_at DESC,key DESC LIMIT ? OFFSET ?) SELECT json_object(' + projection +
            ") FROM page JOIN records ON records.collection='transcripts' AND records.key=page.key ORDER BY records.updated_at DESC,records.key DESC", [*args, limit, offset])]
    for row in rows:
        row['truncated'] = (row.get('text_chars') or 0) > 1200
        row['provenance'] = 'archived_agent_output_not_scientific_verification'
    return clean(_page(rows, total, limit, offset, run_id=run_id))


def _visible_memory(run_id, scope='all'):
    own = "json_extract(data,'$.run_id')=?"
    shared = "(json_extract(data,'$.run_id')='global' OR json_extract(data,'$.run_id') IS NULL OR json_extract(data,'$.metadata.shared')=1)"
    if scope == 'run':
        return own, [run_id]
    if scope == 'shared':
        return shared, []
    return '(' + own + ' OR ' + shared + ')', [run_id]


def _memory_keys(run_id, scope='all'):
    # Express exclusions as indexed ranges. A JSON OR/NOT predicate over the
    # 100k-chunk corpus otherwise reads and parses every embedding payload.
    low, high = sorted({run_id, 'global'}) if run_id != 'global' else ('global', 'global')
    args = {'run': run_id, 'low': low, 'high': high}
    foreign = ("SELECT key FROM records WHERE collection='memory' AND json_extract(data,'$.run_id')<:low "
        "UNION ALL SELECT key FROM records WHERE collection='memory' AND json_extract(data,'$.run_id')>:low AND json_extract(data,'$.run_id')<:high "
        "UNION ALL SELECT key FROM records WHERE collection='memory' AND json_extract(data,'$.run_id')>:high")
    prelude = 'WITH foreign_keys AS MATERIALIZED (' + foreign + '), visible_keys AS MATERIALIZED ('
    own = "SELECT key,updated_at FROM records WHERE collection='memory' AND json_extract(data,'$.run_id')=:run"
    global_rows = "SELECT key,updated_at FROM records WHERE collection='memory' AND json_extract(data,'$.run_id') IN ('global')"
    null_rows = "SELECT key,updated_at FROM records WHERE collection='memory' AND json_extract(data,'$.run_id') IS NULL"
    foreign_shared = ("SELECT records.key,records.updated_at FROM foreign_keys JOIN records ON records.collection='memory' AND records.key=foreign_keys.key "
                      "WHERE json_extract(records.data,'$.metadata.shared')=1")
    if scope == 'run':
        branches = [own]
    elif scope == 'shared':
        branches = [global_rows, null_rows, foreign_shared]
        if run_id != 'global':
            branches.append(own + " AND json_extract(data,'$.metadata.shared')=1")
    else:
        branches = [own, null_rows, foreign_shared]
        if run_id != 'global':
            branches.append(global_rows)
    return prelude + ' UNION ALL '.join(branches) + ') ', args


def entries_view(store, run_id, *, clean, role=None, scope='all', q='', limit=20, offset=0):
    cte, args = _memory_keys(run_id, scope)
    where = "collection='memory' AND key IN (SELECT key FROM visible_keys)"
    if role:
        where += " AND json_extract(data,'$.role')=:role"
        args['role'] = role
    if q:
        # Read-only UI text filter; deliberately not the agent retrieval engine.
        where += " AND instr(lower(json_extract(data,'$.text')),lower(:q))>0"
        args['q'] = q
    fields = {'id': 'id', 'run_id': 'run_id', 'role': 'role', 'created_at': 'created_at',
              'metadata': 'metadata', 'document_id': 'document_id', 'provenance': 'provenance'}
    projection = _projection(fields) + ", 'text_preview',substr(_memory_clean_text(json_extract(data,'$.text')),1,1000),'text_chars',length(_memory_clean_text(json_extract(data,'$.text')))"
    with store._read_db() as db:
        db.create_function('_memory_clean_text', 1, lambda value: clean(value or ''))
        if not role and not q:
            page_cte = cte.rstrip() + ', page AS MATERIALIZED (SELECT key FROM visible_keys ORDER BY updated_at DESC,key DESC LIMIT :limit OFFSET :offset) '
            raw = db.execute(page_cte + 'SELECT total.n, CASE WHEN records.key IS NULL THEN NULL ELSE json_object(' + projection +
                ") END FROM (SELECT count(*) AS n FROM visible_keys) total LEFT JOIN page ON 1 LEFT JOIN records ON records.collection='memory' AND records.key=page.key ORDER BY records.updated_at DESC,records.key DESC",
                {**args, 'limit': limit, 'offset': offset}).fetchall()
            total, rows = raw[0][0], [json.loads(row[1]) for row in raw if row[1] is not None]
        else:
            filtered_cte = cte.rstrip() + ', filtered AS MATERIALIZED (SELECT key,updated_at FROM records WHERE ' + where + '), page AS MATERIALIZED (SELECT key FROM filtered ORDER BY updated_at DESC,key DESC LIMIT :limit OFFSET :offset) '
            raw = db.execute(filtered_cte + 'SELECT total.n, CASE WHEN records.key IS NULL THEN NULL ELSE json_object(' + projection +
                ") END FROM (SELECT count(*) AS n FROM filtered) total LEFT JOIN page ON 1 LEFT JOIN records ON records.collection='memory' AND records.key=page.key ORDER BY records.updated_at DESC,records.key DESC",
                {**args, 'limit': limit, 'offset': offset}).fetchall()
            total, rows = raw[0][0], [json.loads(row[1]) for row in raw if row[1] is not None]
    for row in rows:
        row['scope'] = 'run' if row.get('run_id') == run_id else 'shared'
        row['truncated'] = (row.get('text_chars') or 0) > 1000
        # Metadata is displayed as a bounded card, never a large source payload.
        from .tool_views import _bounded
        row['metadata'], _ = _bounded(clean(row.get('metadata') or {}), budget=1800)
    return clean(_page(rows, total, limit, offset, run_id=run_id,
                       filter_method='read_only_text_filter', filters={'role': role, 'scope': scope, 'q': q}))


def _native_key(run_id, role, operation):
    return hashlib.sha256(json.dumps([run_id, role, operation, 'codex-reasoning-v1']).encode()).hexdigest()


def _request_summary(collection, key, data):
    binding = data.get('memory_capture_binding') or data.get('model_input_capture') or {}
    selection = data.get('selection') or {}
    checkpoint = data.get('checkpoint') or {}
    result = data.get('result') or {}
    request = {'id': {'codex_reasoning': 'codex:', 'request_audits': 'audit:', 'context_snapshots': 'snapshot:'}[collection] + key,
        'source_key': key, 'source_collection': collection, 'source': collection,
        'role': data.get('role'), 'purpose': selection.get('purpose') or data.get('purpose'),
        'created_at': data.get('created_at'), 'updated_at': data.get('updated_at'),
        'operation_id': data.get('operation_id') or binding.get('operation_id'),
        'request_id': result.get('request_id') or (data.get('reservation') or {}).get('id') or data.get('request_id'),
        'handoff_id': binding.get('handoff_id'), 'status': checkpoint.get('status') or 'prepared',
        'provider': data.get('provider') or ('local_codex' if collection == 'codex_reasoning' else binding.get('provider')),
        'model': selection.get('model') or data.get('model') or binding.get('model'),
        'snapshot_id': binding.get('context_snapshot_id') or (key if collection == 'context_snapshots' else None),
        'legacy': not bool(binding), 'capture_stage': {'codex_reasoning': 'native_frozen_instruction',
            'request_audits': data.get('capture_stage') or 'prepared_before_http', 'context_snapshots': 'compiled_snapshot'}[collection]}
    if collection == 'codex_reasoning':
        from .memory_capture import native_dispatch_status
        request['dispatch_status'] = native_dispatch_status(data)
        request['context_available'] = data.get('has_instruction', isinstance(data.get('instruction'), str))
    elif collection == 'context_snapshots':
        request['dispatch_status'] = 'prepared_only'
        request['context_available'] = data.get('has_active_context', isinstance(data.get('active_context'), dict))
    else:
        request['dispatch_status'] = 'unknown'
        request['context_available'] = data.get('has_body', isinstance(data.get('body'), dict))
    return request


def _handoff_links(db, run_id):
    rows = db.execute("SELECT key,json_extract(data,'$.to_role'),json_extract(data,'$.model_calls') "
                     "FROM records WHERE collection='handoffs' AND json_extract(data,'$.run_id')=?", (run_id,))
    links = {}
    for key, role, calls in rows:
        for call in json.loads(calls or '[]'):
            if isinstance(call, dict) and isinstance(call.get('request_id'), str):
                links.setdefault((role, call['request_id']), key)
    return links


def _pending_keys(store, run_id, handoff):
    """The durable prepared tuple proves identity even before a response exists."""
    try:
        progress = _owned(store, 'handoff_progress', handoff['id'], run_id)
    except KeyError:
        return set()
    from .intelligence import _hash
    keys = set()
    for slot in ('normal_request', 'finalization'):
        request = progress.get(slot)
        prepared = request.get('prepared') if isinstance(request, dict) else None
        if (not isinstance(prepared, dict) or prepared.get('role') != handoff.get('to_role') or
                not isinstance(prepared.get('task'), str) or not isinstance(prepared.get('context'), dict)):
            continue
        if slot == 'normal_request' and request.get('turn') != progress.get('turn'):
            continue
        operation = _hash([prepared['role'], prepared['task'], prepared['context'], prepared.get('schema')])
        keys.add(('codex_reasoning', _native_key(run_id, prepared['role'], operation)))
    return keys


def _http_dispatch_evidence(db, run_id, request):
    if request['source_collection'] != 'request_audits' or not request.get('request_id'):
        return
    fields = _projection({name: name for name in ('role', 'provider', 'model', 'operation_id', 'response_metadata')})
    rows = db.execute('SELECT json_object(' + fields + ") FROM records INDEXED BY transcript_output_request WHERE collection='transcripts' AND json_extract(data,'$.kind')='output' AND json_extract(data,'$.request_id')=? AND json_extract(data,'$.run_id')=?", (request['request_id'], run_id))
    for raw in rows:
        output = json.loads(raw[0])
        if any(request.get(field) is not None and output.get(field) != request[field]
               for field in ('role', 'provider', 'model', 'operation_id')):
            continue
        metadata = output.get('response_metadata') or {}
        request['dispatch_status'] = 'completed' if metadata.get('provider_status') == 'completed' else 'submitted'
        request['status'] = metadata.get('provider_status') or 'response_archived'
        request['dispatch_evidence'] = 'exact_request_output_transcript'
        break


def _pending_handoff(store, run_id, role, collection, key):
    if role not in ROLES or collection != 'codex_reasoning':
        return None
    from .run_views import agent_assignment_summary
    run = store.get('runs', run_id)
    if not run:
        return None
    current = agent_assignment_summary(store, run, role).get('current_assignment')
    if not current or current.get('status') == 'completed':
        return None
    assignment = _owned(store, 'handoffs', current['id'], run_id)
    return current['id'] if (collection, key) in _pending_keys(store, run_id, assignment) else None


def _request_page(store, run_id, *, role=None, handoff_id=None, request_id=None, limit=20, offset=0):
    if request_id:
        prefix, separator, key = request_id.partition(':')
        collection = {'codex': 'codex_reasoning', 'audit': 'request_audits', 'snapshot': 'context_snapshots'}.get(prefix)
        if not separator or not collection:
            return [], 0
        data = _owned(store, collection, key, run_id)
        summary = _request_summary(collection, key, data)
        with store._read_db() as db:
            links = _handoff_links(db, run_id)
        summary['handoff_id'] = summary.get('handoff_id') or links.get((summary['role'], summary['request_id']))
        return [summary], 1
    # Key-only page first: the existing native partial index covers these
    # metadata fields, so thousands of frozen instructions are never decoded.
    with store._read_db() as db:
        args = {'run': run_id, 'limit': limit, 'offset': offset}
        condition = ''
        if role:
            args['role'] = role
            condition = " AND +json_extract(data,'$.role')=:role"
        native = "SELECT 'codex_reasoning' AS collection,key,updated_at FROM records INDEXED BY codex_reasoning_list_metadata_v2 WHERE collection='codex_reasoning' AND json_extract(data,'$.run_id')=:run" + condition
        audits = "SELECT 'request_audits' AS collection,key,updated_at FROM records WHERE collection='request_audits' AND json_extract(data,'$.run_id')=:run" + condition
        # The current prepared pointer adds at most one snapshot per role. Older
        # snapshots remain readable by exact ID, rather than duplicating every
        # frozen native request and scanning gigabytes of historical contexts.
        current = []
        for context_role in ([role] if role else ROLES):
            pointer = db.execute("SELECT json_extract(data,'$.snapshot_id'),json_extract(data,'$.transcript_id') FROM records WHERE collection='contexts' AND key=? AND json_extract(data,'$.run_id')=?", (run_id + ':' + context_role, run_id)).fetchone()
            if not pointer or not pointer[0]:
                continue
            snap = db.execute("SELECT json_extract(data,'$.model_input_capture.operation_id'),updated_at FROM records WHERE collection='context_snapshots' AND key=? AND json_extract(data,'$.run_id')=? AND json_extract(data,'$.role')=?", (pointer[0], run_id, context_role)).fetchone()
            if not snap:
                continue
            operation = snap[0]
            if not operation and pointer[1]:
                original = db.execute("SELECT json_extract(data,'$.operation_id') FROM records WHERE collection='transcripts' AND key=? AND json_extract(data,'$.run_id')=? AND json_extract(data,'$.role')=?", (pointer[1], run_id, context_role)).fetchone()
                operation = original[0] if original else None
            native_exists = operation and db.execute("SELECT 1 FROM records WHERE collection='codex_reasoning' AND key=? AND json_extract(data,'$.run_id')=?", (_native_key(run_id, context_role, operation), run_id)).fetchone()
            audit_exists = operation and db.execute("SELECT 1 FROM records WHERE collection='request_audits' AND json_extract(data,'$.run_id')=? AND json_extract(data,'$.operation_id')=? LIMIT 1", (run_id, operation)).fetchone()
            if not native_exists and not audit_exists:
                current.append((pointer[0], snap[1]))
        snapshot_queries = []
        for index, (key, updated_at) in enumerate(current):
            args['snapshot_' + str(index)] = key
            args['snapshot_time_' + str(index)] = updated_at
            snapshot_queries.append("SELECT 'context_snapshots' AS collection,:snapshot_" + str(index) + " AS key,:snapshot_time_" + str(index) + ' AS updated_at')
        keys = native + ' UNION ALL ' + audits + ''.join(' UNION ALL ' + part for part in snapshot_queries)
        links = _handoff_links(db, run_id)
        if handoff_id:
            # A completed request is bound by its host receipt, never by dates.
            handoff = _owned(store, 'handoffs', handoff_id, run_id)
            calls = handoff.get('model_calls') or []
            expected = set()
            for call in calls:
                transcript = call.get('transcript_id') if isinstance(call, dict) else None
                if isinstance(transcript, str) and transcript.endswith(':output'):
                    expected.add(('codex_reasoning', transcript[:-7]))
                if isinstance(call, dict) and call.get('request_id'):
                    expected.add(('request_audits', call['request_id']))
            expected.update(_pending_keys(store, run_id, handoff))
            for snapshot_key, _ in current:
                bound = db.execute("SELECT json_extract(data,'$.model_input_capture.handoff_id') FROM records WHERE collection='context_snapshots' AND key=? AND json_extract(data,'$.run_id')=?", (snapshot_key, run_id)).fetchone()
                if bound and bound[0] == handoff_id:
                    expected.add(('context_snapshots', snapshot_key))
            if not expected:
                return [], 0
            args['handoff_role'] = handoff['to_role']
            branches = []
            for index, (collection, key) in enumerate(sorted(expected)):
                args['collection_' + str(index)] = collection
                args['key_' + str(index)] = key
                # Primary-key lookups avoid pushing contradictory collection
                # predicates through a forced partial-index UNION branch.
                branches.append('SELECT collection,key,updated_at FROM records WHERE collection=:collection_' + str(index) +
                    ' AND key=:key_' + str(index) + " AND json_extract(data,'$.run_id')=:run AND json_extract(data,'$.role')=:handoff_role" +
                    (" AND json_extract(data,'$.role')=:role" if role else ''))
            keys = ' UNION ALL '.join(branches)
        cte = 'WITH request_keys AS (' + keys + ') '
        total = db.execute(cte + 'SELECT count(*) FROM request_keys', args).fetchone()[0]
        page = db.execute(cte + 'SELECT collection,key FROM request_keys ORDER BY updated_at DESC,key DESC LIMIT :limit OFFSET :offset', args).fetchall()
        summaries = []
        fields = {'id': 'id', 'run_id': 'run_id', 'role': 'role', 'operation_id': 'operation_id',
                  'created_at': 'created_at', 'updated_at': 'updated_at', 'provider': 'provider',
                  'model': 'model', 'selection': 'selection', 'memory_capture_binding': 'memory_capture_binding',
                  'model_input_capture': 'model_input_capture', 'request_id': 'request_id'}
        # capture contains frozen system text; do not project it in list cards.
        fields.pop('model_input_capture')
        projection = _projection(fields) + ", 'checkpoint',CASE WHEN json_type(data,'$.checkpoint')='object' THEN json_object('run_id',json_extract(data,'$.checkpoint.run_id'),'operation_id',json_extract(data,'$.checkpoint.operation_id'),'thread_id',json_extract(data,'$.checkpoint.thread_id'),'turn_id',json_extract(data,'$.checkpoint.turn_id'),'status',json_extract(data,'$.checkpoint.status'),'turn_dispatched_at',json_extract(data,'$.checkpoint.turn_dispatched_at')) ELSE NULL END, 'reservation',json_object('id',json_extract(data,'$.reservation.id')), 'result',json_object('request_id',json_extract(data,'$.result.request_id')), 'has_instruction',json_type(data,'$.instruction')='text','has_active_context',json_type(data,'$.active_context')='object','has_body',json_type(data,'$.body')='object','capture_stage',json_extract(data,'$.capture_stage')"
        for collection, key in page:
            raw = db.execute('SELECT json_object(' + projection + ") FROM records WHERE collection=? AND key=? AND json_extract(data,'$.run_id')=?", (collection, key, run_id)).fetchone()
            data = json.loads(raw[0])
            summary = _request_summary(collection, key, data)
            _http_dispatch_evidence(db, run_id, summary)
            summary['handoff_id'] = summary.get('handoff_id') or links.get((summary['role'], summary['request_id']))
            if handoff_id:
                summary['handoff_id'] = handoff_id
            summaries.append(summary)
    return summaries, total


def requests_view(store, run_id, *, clean, role=None, handoff_id=None, limit=20, offset=0):
    if handoff_id:
        _owned(store, 'handoffs', handoff_id, run_id)
    rows, total = _request_page(store, run_id, role=role, handoff_id=handoff_id, limit=limit, offset=offset)
    return clean(_page(rows, total, limit, offset, run_id=run_id))


def overview_view(store, run_id, *, clean):
    from .run_views import agent_assignment_summary
    from .implementation_activity import receipt_metadata
    run = store.get('runs', run_id)
    if not run:
        raise KeyError('Research task not found')
    activity = receipt_metadata(store, run_id)
    agents = []
    for role in ROLES:
        assignment = agent_assignment_summary(store, run, role, activity=activity)
        current = assignment.get('current_assignment')
        if current and current.get('status') == 'completed':
            current = None
        if current:
            current = {**current, 'role': role, 'summary': current.get('label')}
        with store._read_db() as db:
            history_count = db.execute("SELECT count(*) FROM records INDEXED BY transcript_output_operation WHERE collection='transcripts' "
                "AND json_extract(data,'$.run_id')=? AND json_extract(data,'$.role')=? AND json_extract(data,'$.kind')='output'",
                (run_id, role)).fetchone()[0]
            task_count = db.execute("SELECT count(*) FROM records WHERE collection='handoffs' "
                "AND json_extract(data,'$.run_id')=? AND json_extract(data,'$.to_role')=?", (run_id, role)).fetchone()[0]
        _, request_count = _request_page(store, run_id, role=role, limit=1)
        agents.append({'role': role, 'current_task': current,
                       'history_count': history_count, 'task_count': task_count, 'request_count': request_count})
    counts = {}
    with store._read_db() as db:
        for scope in ('run', 'shared', 'all'):
            cte, args = _memory_keys(run_id, scope)
            counts['total' if scope == 'all' else scope] = db.execute(cte + 'SELECT count(*) FROM visible_keys', args).fetchone()[0]
    return clean({'schema_version': 1, 'run_id': run_id, 'roles': agents, 'memory_counts': counts,
                  'notice': '记忆与请求均为持久化只读投影；准备、派发、实际保留与科学验证分别记录。'})


def _parse_native(instruction):
    """Read the frozen user JSON in legacy native input; never rebuild prompts."""
    decoder = json.JSONDecoder()
    from .codex_reasoning import ENVELOPE_INSTRUCTION
    matches = []
    for marker in ('\n\n{"objective":', '\n\n{"task":'):
        offset = 0
        while (at := instruction.find(marker, offset)) >= 0:
            offset = at + len(marker)
            start = at + 2
            try:
                active, end = decoder.raw_decode(instruction[start:])
                if (isinstance(active, dict) and 'context' in active and
                        instruction[start + end:] in (ENVELOPE_INSTRUCTION, '\n\n' + ENVELOPE_INSTRUCTION)):
                    user = instruction[start:start + end]
                    matches.append((instruction[:at], user, active))
            except (ValueError, TypeError):
                pass
    return matches[0] if len(matches) == 1 else (None, None, None)


def _request_source(store, run_id, request_id):
    prefix, separator, key = request_id.partition(':')
    collection = {'codex': 'codex_reasoning', 'audit': 'request_audits', 'snapshot': 'context_snapshots'}.get(prefix)
    if not separator or not collection:
        raise KeyError('Model request not found for this research task')
    source = _owned(store, collection, key, run_id)
    summary = _request_summary(collection, key, source)
    with store._read_db() as db:
        links = _handoff_links(db, run_id)
        _http_dispatch_evidence(db, run_id, summary)
    summary['handoff_id'] = summary.get('handoff_id') or links.get((summary['role'], summary['request_id']))
    summary['handoff_id'] = summary.get('handoff_id') or _pending_handoff(store, run_id, summary['role'], collection, key)
    return summary, source


def _input_parts(summary, source):
    if summary['source_collection'] == 'codex_reasoning':
        combined = source.get('instruction')
        if not isinstance(combined, str):
            return None, None, None, None
        system, user, active = _parse_native(combined)
        return system, user, combined, active
    if summary['source_collection'] == 'request_audits':
        body = source.get('body')
        if not isinstance(body, dict):
            return None, None, None, None
        # The complete adapted JSON is the transport evidence. Provider-specific
        # message projections are conveniences and do not replace that body.
        combined = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        messages = body.get('messages', body.get('input', []))
        system, user = [], []
        if isinstance(body.get('system'), str):
            system.append(body['system'])
        if isinstance(body.get('instructions'), str):
            system.append(body['instructions'])
        if isinstance(messages, str):
            user.append(messages)
        elif isinstance(messages, list):
            for message in messages:
                if not isinstance(message, dict):
                    continue
                content = message.get('content', '')
                if isinstance(content, list):
                    content = '\n'.join(str(part.get('text', '')) for part in content if isinstance(part, dict) and part.get('type') in ('text', 'input_text'))
                if isinstance(content, str):
                    (system if message.get('role') in ('system', 'developer') else user).append(content)
        system_text, user_text = '\n'.join(system) or None, '\n'.join(user) or None
        try:
            active = json.loads(user_text) if user_text else None
        except (ValueError, TypeError):
            active = None
        return system_text, user_text, combined, active
    capture = source.get('model_input_capture')
    if isinstance(capture, dict):
        try:
            from .memory_capture import read_captured_input
            restored = read_captured_input(source, summary.get('run_id', source['run_id']), summary['role'])
            if isinstance(restored, dict):
                system = restored.get('system_text', restored.get('system'))
                user = restored.get('user_text', restored.get('user'))
            else:
                system, user = restored
            active = json.loads(user)
            return system, user, system + '\n\n' + user, active
        except ImportError:
            pass
    active = source.get('active_context')
    if isinstance(active, dict):
        # Old snapshots preserve prepared structured data but not exact system
        # bytes. Display the saved projection and explicitly label the gap.
        user = json.dumps(active, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return None, user, user, active
    return None, None, None, None


def _result_rows(result):
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in ('matches', 'results', 'items', 'memories', 'chunks', 'passages'):
            if isinstance(result.get(key), list):
                return result[key]
    return []


def _resolve(value, active):
    """Dereference lossless compiler tables without expanding the whole input."""
    tables = {'$record': active.get('shared_context_records', {}),
              '$text': active.get('shared_context_text', {}),
              '$shared_text': active.get('shared_text', {})}
    seen = set()
    for _ in range(40):
        if not isinstance(value, dict) or len(value) != 1:
            return value
        kind = next(iter(value))
        identifier = value[kind]
        if kind not in tables or not isinstance(identifier, str):
            return value
        marker = (kind, identifier)
        if marker in seen or identifier not in tables[kind]:
            return value
        seen.add(marker)
        value = tables[kind][identifier]
    return value


def _expand_preview(value, active, depth=0):
    value = _resolve(value, active)
    if depth > 5:
        return '[preview depth limit]'
    if isinstance(value, dict):
        return {key: _expand_preview(child, active, depth + 1) for key, child in list(value.items())[:15]}
    if isinstance(value, list):
        return [_expand_preview(child, active, depth + 1) for child in value[:8]]
    if isinstance(value, str):
        # Credentials are removed by _snippet before its final character cap.
        return value
    return value


def _scalar(value, active):
    value = _resolve(value, active)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))[:300]


def _context_sources(context, active, clean):
    """Show direct task material as well as retrieval tool results."""
    found, seen_refs, visited, truncated = {}, set(), 0, False

    def visit(value, path, depth=0):
        nonlocal visited, truncated
        visited += 1
        if visited > 4000 or depth > 12:
            truncated = True
            return
        if isinstance(value, dict) and len(value) == 1 and '$record' in value:
            marker = value['$record']
            if marker in seen_refs:
                return
            seen_refs.add(marker)
        value = _resolve(value, active)
        if isinstance(value, dict):
            identifier = _resolve(value.get('document_id', value.get('artifact_id', value.get('source_id'))), active)
            if isinstance(identifier, str):
                body = next((_resolve(value[name], active) for name in ('text', 'content', 'quote', 'markdown')
                             if name in value), None)
                retained = isinstance(body, str) and bool(body)
                item = {'id': identifier, 'type': _scalar(value.get('kind', 'task_material'), active),
                        'sha256': _scalar(value.get('document_sha256', value.get('sha256')), active),
                        'source': 'captured_task_context', 'path': path,
                        'inclusion': 'retained' if retained else 'reference_only',
                        'preview': _snippet(body if retained else _expand_preview(value, active), clean, 700)}
                previous = found.get(identifier)
                if previous is None or (retained and previous['inclusion'] != 'retained'):
                    found[identifier] = item
            for name, child in value.items():
                if name not in ('tool_results', 'text', 'content', 'quote', 'markdown', 'sections'):
                    visit(child, path + '/' + str(name), depth + 1)
        elif isinstance(value, list):
            for index, child in enumerate(value[:100]):
                visit(child, path + '/' + str(index), depth + 1)
            truncated = truncated or len(value) > 100

    visit(context, '/context')
    return list(found.values())[:120], truncated or len(found) > 120


def _bound_capture(store, run_id, request, source, clean):
    binding = source.get('memory_capture_binding')
    if not isinstance(binding, dict):
        return source.get('model_input_capture') or {}, None
    if (binding.get('run_id') != run_id or binding.get('role') != request['role'] or
            binding.get('operation_id') != source.get('operation_id')):
        raise ValueError('Request capture ownership mismatch')
    snapshot = _owned(store, 'context_snapshots', binding.get('context_snapshot_id'), run_id)
    from .memory_capture import read_captured_input, capture_binding
    restored = read_captured_input(snapshot, run_id, request['role'])
    if restored is None or capture_binding(restored['capture']) != binding:
        raise ValueError('Request capture identity mismatch')
    instruction_hash = binding.get('original_instruction_sha256')
    if instruction_hash and isinstance(source.get('instruction'), str) and hashlib.sha256(source['instruction'].encode()).hexdigest() != instruction_hash:
        raise ValueError('Frozen native input does not match capture')
    return restored['capture'], snapshot


def request_detail_view(store, run_id, request_id, *, clean):
    request, source = _request_source(store, run_id, request_id)
    try:
        capture, snapshot = _bound_capture(store, run_id, request, source, clean)
        system, user, combined, active = _input_parts(request, source)
    except (ValueError, KeyError, TypeError):
        request['context_available'] = False
        return clean({'schema_version': 1, 'run_id': run_id, 'request': request, 'history': [],
            'retrievals': [], 'references': [], 'missing': ['capture_integrity'], 'truncated': False,
            'notice': '捕获身份或完整性不匹配，未用其他材料重建输入。'})
    missing, history, retrievals, references = [], [], [], []
    if combined is None:
        missing.append('complete_model_input')
    if system is None:
        missing.append('frozen_system_text')
    if request['legacy']:
        missing.append('exact_capture_binding')
    if request['dispatch_status'] in ('unknown', 'prepared_only'):
        missing.append('dispatch_confirmation')
    active = active if isinstance(active, dict) else {}
    request['context_sha256'] = hashlib.sha256(clean(combined).encode()).hexdigest() if isinstance(combined, str) else None
    request['context_chars'] = len(clean(combined)) if isinstance(combined, str) else None
    request['source_ids'] = {'snapshot_id': request.get('snapshot_id'),
                             'input_transcript_id': source.get('memory_capture_binding', {}).get('input_transcript_id')}
    turns = _resolve(active.get('recent_turns', []), active)
    for turn in turns[:60] if isinstance(turns, list) else []:
        turn = _resolve(turn, active)
        if not isinstance(turn, dict):
            continue
        history.append({'id': _scalar(turn.get('id'), active), 'preview': _snippet(_expand_preview(turn.get('content', ''), active), clean),
                        'inclusion': 'retained' if 'content' in turn else 'reference_only',
                        'provenance': _scalar(turn.get('provenance', 'captured_model_input'), active),
                        'provider': _scalar(turn.get('provider'), active), 'model': _scalar(turn.get('model'), active)})
    implicit = _resolve(active.get('retrieved_memories', []), active)
    if isinstance(implicit, list) and implicit:
        results = [{'id': _scalar(row.get('id'), active), 'preview': _snippet(_expand_preview(row.get('text', ''), active), clean),
                    'score': _scalar(row.get('score'), active), 'inclusion': 'retained' if 'text' in row else 'reference_only',
                    'provenance': _scalar(row.get('provenance'), active)} for raw in implicit[:60] if isinstance(row := _resolve(raw, active), dict)]
        retrievals.append({'tool_name': 'implicit_memory_retrieval', 'query': active.get('objective'),
                           'results': results, 'inclusion': 'retained', 'source': 'captured_model_input'})
    context = _resolve(active.get('context', {}), active)
    context = context if isinstance(context, dict) else {}
    direct_sources, source_truncated = _context_sources(context, active, clean)
    references.extend(direct_sources)
    marker = _resolve(context.get('_working_set'), active)
    if isinstance(marker, dict) and marker.get('role') == request['role'] and isinstance(marker.get('handoff_id'), str):
        try:
            assignment = _owned(store, 'handoffs', marker['handoff_id'], run_id)
            if assignment.get('to_role') == request['role']:
                request['handoff_id'] = request.get('handoff_id') or marker['handoff_id']
        except KeyError:
            missing.append('unbound_handoff_reference')
        commit_id = _resolve(marker.get('commit_id'), active)
        if isinstance(commit_id, str):
            try:
                commit = _owned(store, 'context_commits', commit_id, run_id)
                from .working_context import sha
                if (commit.get('role') != request['role'] or commit.get('handoff_id') != marker.get('handoff_id')
                        or commit.get('sha256') != sha(commit.get('payload'))):
                    raise ValueError('Working context commit binding mismatch')
                references.append({'id': commit_id, 'type': 'working_commit', 'sha256': commit.get('sha256'),
                    'source': 'captured_working_set_marker', 'inclusion': 'reference_only', 'preview': ''})
            except (KeyError, ValueError, TypeError):
                missing.append('unbound_working_commit')
    for name, kind in [('working_state', 'working_state'), ('scientific_state', 'scientific_state'),
                       ('document_working_notes', 'document_working_notes')]:
        value = _resolve(context.get(name), active)
        if isinstance(value, (dict, list)) and value:
            references.append({'id': 'context:' + name, 'type': kind, 'sha256': _scalar(value.get('sha256'), active) if isinstance(value, dict) else None,
                'source': 'captured_task_context', 'path': '/context/' + name, 'inclusion': 'retained',
                'preview': _snippet(_expand_preview(value, active), clean, 900)})
    tool_rows = _resolve(context.get('tool_results', []), active)
    total_results, result_count, total_preview_chars, detail_truncated = 0, len(implicit) if isinstance(implicit, list) else 0, sum(len(row['preview']) for row in history), False
    for index, row in enumerate(tool_rows[:60] if isinstance(tool_rows, list) else []):
        row = _resolve(row, active)
        if not isinstance(row, dict):
            continue
        call = _resolve(row.get('call') or {}, active)
        if not isinstance(call, dict):
            continue
        name, result = _scalar(call.get('name'), active), _resolve(row.get('result'), active)
        if name in SEARCH_TOOLS:
            results = []
            result_count += len(_result_rows(result))
            detail_truncated = detail_truncated or len(_result_rows(result)) > 60
            for hit in _result_rows(result)[:60]:
                hit = _resolve(hit, active)
                if not isinstance(hit, dict):
                    continue
                if total_results >= 120 or total_preview_chars >= 100000:
                    detail_truncated = True
                    break
                preview = _snippet(_expand_preview(hit.get('text', hit.get('content', hit.get('snippet', hit.get('quote', hit)))), active), clean)
                total_results += 1
                total_preview_chars += len(preview)
                results.append({'id': _scalar(hit.get('id', hit.get('memory_id', hit.get('chunk_id', hit.get('document_id', hit.get('source_id'))))), active),
                    'preview': preview,
                    'score': _scalar(hit.get('score'), active), 'inclusion': 'retained' if any(k in hit for k in ('text', 'content', 'snippet', 'quote')) else 'reference_only',
                    'provenance': _scalar(hit.get('provenance'), active)})
            retrievals.append({'tool_name': name, 'query': _expand_preview(clean(call.get('arguments', {})), active),
                'results': results, 'inclusion': 'retained', 'source': 'captured_tool_result', 'index': index})
        elif isinstance(result, dict):
            identifier = _scalar(result.get('document_id', result.get('artifact_id', result.get('source_id', result.get('receipt_id')))), active)
            if identifier or name in ('documents.read', 'literature.rag_read', 'sources.read', 'evidence.read', 'context.expand'):
                refs_id = identifier or ('tool-result:' + str(index))
                references.append({'id': refs_id, 'type': 'tool_result', 'tool_name': name,
                    'sha256': _scalar(result.get('sha256'), active), 'source': 'captured_tool_result',
                    'inclusion': 'retained' if any(key in result for key in ('text', 'content', 'quote', 'receipt')) else 'reference_only',
                    'preview': _snippet(_expand_preview(result, active), clean, 700)})
    if snapshot:
        request['context_tokens_estimate'] = snapshot.get('active_tokens_estimate')
    for candidates, effective, destination in [('history_ids', 'effective_history_ids', history),
                                               ('retrieved_memory_ids', 'effective_memory_ids', references)]:
        present = {row.get('id') for row in destination}
        if candidates == 'retrieved_memory_ids':
            present.update(row.get('id') for retrieval in retrievals for row in retrieval.get('results', []))
        kept = set(capture.get(effective) or []) if isinstance(capture, dict) else set()
        for identifier in capture.get(candidates, [])[:200] if isinstance(capture, dict) else []:
            if isinstance(identifier, str) and identifier not in present:
                destination.append({'id': identifier, 'type': 'history' if candidates == 'history_ids' else 'memory',
                    'preview': '', 'sha256': None, 'source': 'pre_compilation_capture',
                    'inclusion': 'reference_only' if identifier in kept else 'omitted'})
    if not isinstance(capture, dict) or not capture:
        missing.append('pre_compilation_memory_candidates')
    available_references = _resolve(context.get('available_context_references', []), active)
    for row in available_references[:60] if isinstance(available_references, list) else []:
        row = _resolve(row, active)
        if isinstance(row, dict):
            references.append({'id': _scalar(row.get('id', row.get('receipt_id', row.get('artifact_id'))), active),
                'type': _scalar(row.get('kind', 'context_reference'), active), 'sha256': _scalar(row.get('sha256'), active),
                'source': 'captured_context_reference', 'inclusion': 'reference_only',
                'preview': _snippet(_expand_preview(row, active), clean, 500)})
    detail_truncated = detail_truncated or source_truncated or (isinstance(turns, list) and len(turns) > 60) or (isinstance(tool_rows, list) and len(tool_rows) > 60)
    if detail_truncated:
        missing.append('detail_preview_truncated_use_context_pages')
    return clean({'schema_version': 1, 'run_id': run_id, 'request': request, 'history': history,
                  'retrievals': retrievals, 'references': references, 'truncated': detail_truncated,
                  'counts': {'history_total': max(len(turns) if isinstance(turns, list) else 0, len(history), len(capture.get('history_ids') or [])), 'history_displayed': len(history),
                             'tool_results_total': len(tool_rows) if isinstance(tool_rows, list) else 0,
                             'retrieval_results_total': result_count,
                             'retrieval_results_displayed': sum(len(row.get('results', [])) for row in retrievals),
                             'references_displayed': len(references)},
                  'missing': list(dict.fromkeys(missing)),
                  'notice': '保留表示正文出现在捕获输入中；准备快照不证明派发，引用不证明已读，历史输出不等于科学验证。'})


def _text_page(text, *, offset, limit, **extra):
    digest = hashlib.sha256(text.encode()).hexdigest()
    part = text[offset:offset + limit]
    more = offset + len(part) < len(text)
    return {'schema_version': 1, 'text': part, 'total_chars': len(text), 'offset': offset,
            'limit': limit, 'has_more': more, 'next_offset': offset + len(part) if more else None,
            'sha256': digest, 'redacted': True, **extra}


def request_context_view(store, run_id, request_id, *, clean, part='combined', offset=0, limit=12000):
    request, source = _request_source(store, run_id, request_id)
    try:
        _bound_capture(store, run_id, request, source, clean)
        system, user, combined, _ = _input_parts(request, source)
    except (ValueError, KeyError, TypeError):
        return {'schema_version': 1, 'text': '', 'total_chars': 0, 'offset': offset, 'limit': limit,
                'has_more': False, 'next_offset': None, 'sha256': None, 'redacted': True,
                'available': False, 'source': request['source'], 'capture_stage': request['capture_stage'],
                'dispatch_status': request['dispatch_status'], 'missing': ['capture_integrity']}
    text = {'system': system, 'user': user, 'combined': combined}[part]
    if text is None:
        return {'schema_version': 1, 'text': '', 'total_chars': 0, 'offset': offset, 'limit': limit,
                'has_more': False, 'next_offset': None, 'sha256': None, 'redacted': True,
                'available': False, 'source': request['source'], 'capture_stage': request['capture_stage'],
                'dispatch_status': request['dispatch_status'], 'missing': ['captured_' + part + '_text']}
    return _text_page(clean(text), offset=offset, limit=limit, available=True, source=request['source'],
                      capture_stage=request['capture_stage'], dispatch_status=request['dispatch_status'],
                      part=part, request_id=request_id)


def stored_text_view(store, run_id, collection, key, *, clean, offset=0, limit=12000):
    if collection == 'memory':
        visible, args = _visible_memory(run_id)
        where = "collection='memory' AND key=? AND " + visible
        parameters = [key, *args]
    else:
        where = "collection='transcripts' AND key=? AND json_extract(data,'$.run_id')=? AND json_extract(data,'$.kind')='output'"
        parameters = [key, run_id]
    # Clean the whole stored text before slicing, including credentials that
    # straddle a page boundary. Only the bounded page crosses out of SQLite.
    with store._read_db() as db:
        db.create_function('_memory_clean_text', 1, lambda value: clean(value or ''))
        db.create_function('_memory_text_sha', 1, lambda value: hashlib.sha256(value.encode()).hexdigest())
        row = db.execute("WITH content AS MATERIALIZED (SELECT _memory_clean_text(json_extract(data,'$.text')) AS text "
            'FROM records WHERE ' + where + ") SELECT substr(text,?,?),length(text),_memory_text_sha(text) FROM content",
            [*parameters, offset + 1, limit]).fetchone()
    if not row:
        raise KeyError('Memory or history not found for this research task')
    text, total, digest = row
    more = offset + len(text) < total
    return {'schema_version': 1, 'id': key, 'run_id': run_id, 'text': text, 'total_chars': total,
            'offset': offset, 'limit': limit, 'has_more': more, 'next_offset': offset + len(text) if more else None,
            'sha256': digest, 'redacted': True, 'source': collection}


def register_memory_routes(app, store, require_run, clean):
    """Register GET-only views; reusable by a strictly read-only preview host."""
    from fastapi import HTTPException, Query
    from fastapi.responses import JSONResponse

    def respond(run_id, function, *args, **kwargs):
        require_run(run_id)
        try:
            result = function(store, run_id, *args, clean=clean, **kwargs)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except (ValueError, TypeError) as exc:
            raise HTTPException(409, '记忆捕获的身份或完整性校验失败') from exc
        require_run(run_id)
        return JSONResponse(clean(result), headers={'Cache-Control': 'no-store'})

    @app.get('/api/runs/{run_id}/memory/overview')
    def memory_overview(run_id: str):
        return respond(run_id, overview_view)

    @app.get('/api/runs/{run_id}/memory/tree')
    def memory_tree(run_id: str, role: str = Query(..., pattern='^(director|engineer|scout)$'),
                    snapshot_id: str | None = Query(None, max_length=2000)):
        from .memory_tree import tree_view
        return respond(run_id, tree_view, role=role, snapshot_id=snapshot_id)

    @app.get('/api/runs/{run_id}/memory/tree/{snapshot_id}/{node_id}')
    def memory_tree_node(run_id: str, snapshot_id: str, node_id: str,
                         role: str = Query(..., pattern='^(director|engineer|scout)$'),
                         limit: int = Query(12000, ge=1, le=30000), offset: int = Query(0, ge=0)):
        from .memory_tree import node_view
        return respond(run_id, node_view, snapshot_id, node_id, role=role, offset=offset, limit=limit)

    @app.get('/api/runs/{run_id}/memory/tasks')
    def memory_tasks(run_id: str, role: str | None = Query(None, pattern='^(director|engineer|scout)$'),
                     limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
        return respond(run_id, tasks_view, role=role, limit=limit, offset=offset)

    @app.get('/api/runs/{run_id}/memory/history')
    def memory_history(run_id: str, role: str | None = Query(None, pattern='^(director|engineer|scout)$'),
                       limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
        return respond(run_id, history_view, role=role, limit=limit, offset=offset)

    @app.get('/api/runs/{run_id}/memory/history/{history_id}')
    def memory_history_text(run_id: str, history_id: str,
                            limit: int = Query(12000, ge=1, le=30000), offset: int = Query(0, ge=0)):
        return respond(run_id, stored_text_view, 'transcripts', history_id, limit=limit, offset=offset)

    @app.get('/api/runs/{run_id}/memory/entries')
    def memory_entries(run_id: str, role: str | None = Query(None, pattern='^(director|engineer|scout)$'),
                       scope: str = Query('all', pattern='^(all|shared|run)$'),
                       q: str = Query('', max_length=500),
                       limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
        return respond(run_id, entries_view, role=role, scope=scope, q=q, limit=limit, offset=offset)

    @app.get('/api/runs/{run_id}/memory/entries/{memory_id}')
    def memory_entry_text(run_id: str, memory_id: str,
                          limit: int = Query(12000, ge=1, le=30000), offset: int = Query(0, ge=0)):
        return respond(run_id, stored_text_view, 'memory', memory_id, limit=limit, offset=offset)

    @app.get('/api/runs/{run_id}/memory/requests')
    def memory_requests(run_id: str, role: str | None = Query(None, pattern='^(director|engineer|scout)$'),
                        handoff_id: str | None = Query(None, max_length=2000),
                        limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
        return respond(run_id, requests_view, role=role, handoff_id=handoff_id, limit=limit, offset=offset)

    @app.get('/api/runs/{run_id}/memory/requests/{request_id}')
    def memory_request(run_id: str, request_id: str):
        return respond(run_id, request_detail_view, request_id)

    @app.get('/api/runs/{run_id}/memory/requests/{request_id}/context')
    def memory_request_context(run_id: str, request_id: str,
                               part: str = Query('combined', pattern='^(combined|system|user)$'),
                               limit: int = Query(12000, ge=1, le=30000), offset: int = Query(0, ge=0)):
        return respond(run_id, request_context_view, request_id, part=part, limit=limit, offset=offset)
