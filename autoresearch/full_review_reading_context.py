"""Disposable full-review reading views; original journals remain immutable."""
from __future__ import annotations

import copy
from collections import Counter

from . import code_review_reading as reading, full_review_notes as notes
from . import independent_full_review as full, review_material_packet as packets
from .full_review_window import requested_indices
from .working_context import marker, validate_commit, receipt_id


def schema_view(context, schema):
    """Factor repeated exact material-ID enums with standard JSON Schema refs.

    The admitted IDs and validation semantics do not change. Literal data and
    schemas with their own reference scope are deliberately left untouched.
    """
    if reading.FULL_PROJECTION not in context:
        return schema
    reading._owner(context)
    if any(key in str(schema) for key in ("'$id'", "'$anchor'", "'$dynamicAnchor'")):
        return schema
    result = copy.deepcopy(schema)
    definitions, references = {}, {}
    def walk(node):
        if not isinstance(node, dict):
            return
        props = node.get('properties', {})
        material = props.get('material_id') if isinstance(props, dict) else None
        if (isinstance(material, dict) and set(material) == {'enum'}
                and isinstance(material['enum'], list) and len(material['enum']) >= 32
                and material['enum'][0] == 'root'):
            name = 'full_review_material_ids_' + full.sha(material)
            references.setdefault(name, []).append((props, material))
        for key in ('properties', 'patternProperties', '$defs', 'definitions', 'dependentSchemas'):
            value = node.get(key)
            if isinstance(value, dict):
                for child in value.values(): walk(child)
        for key in ('allOf', 'anyOf', 'oneOf', 'prefixItems'):
            for child in node.get(key, []) if isinstance(node.get(key), list) else []: walk(child)
        for key in ('items', 'additionalProperties', 'contains', 'propertyNames', 'if', 'then', 'else', 'not'):
            walk(node.get(key))
    walk(result)
    for name, entries in references.items():
        if len(entries) < 2 or name in result.get('$defs', {}):
            continue
        definitions[name] = entries[0][1]
        for props, _ in entries:
            props['material_id'] = {'$ref': '#/$defs/' + name}
    if definitions:
        result.setdefault('$defs', {}).update(definitions)
    return result


def reuse_view(context):
    """Remove old automatic bodies before they can consume the source window."""
    if reading.FULL_PROJECTION not in context:
        return context
    reading._owner(context)
    result = copy.deepcopy(context)
    reused = result.get('_review_evidence_reuse')
    if isinstance(reused, dict) and isinstance(reused.get('fragments'), list):
        # full_review_window's final full.project(frozen) can restore these
        # after an earlier transform. Enforce the rule at the last owning view.
        result['_review_evidence_reuse'] = {k: v for k, v in reused.items() if k != 'fragments'}
        result['_review_evidence_reuse']['bodies_indexed'] = True
        result['review_guidance'] = ('Previously delivered evidence remains in its authenticated range/note index. '
            'No old source bodies are automatically expanded in this request. '
            'Continue only from unresolved objections and the exact missing required ranges; '
            'explicitly reopen a note or source only to resolve a concrete question. '
            'Original scientific and execution gates remain unchanged.')
    return result


def directory_view(context):
    """Required current materials only; the exact root reopens optional indexes.

    Applied after schema construction and note projection. The frozen catalog,
    allowed material IDs, original root receipts and all source gates stay exact.
    """
    from .code_whole_review import restricted_view
    if full.code_delivery_scope(context) and restricted_view(context):
        from .host_execution_artifacts import FIELD as host_artifacts
        from .code_whole_review import SOURCE_BOUNDARY_GUIDANCE
        result = copy.deepcopy(context)
        result.pop(host_artifacts, None)
        result.pop('review_material', None)
        result.pop('review_navigation_indexes', None)
        result.pop(notes.KEY, None)
        result['review_guidance'] = SOURCE_BOUNDARY_GUIDANCE.strip()
        return result
    if reading.FULL_PROJECTION not in context:
        return context
    result = reuse_view(context)
    index = result['review_material']
    rows = index.get('materials')
    if not isinstance(rows, list):
        return result
    retained = [row for row in rows if not isinstance(row, dict) or row.get('required') is not False]
    if len(retained) != len(rows):
        index['materials'] = retained
        index['optional_material_count'] = len(rows) - len(retained)
        index['catalog_view'] = 'Required current material directory; optional originals remain available on demand.'
        index['complete_catalog_read_with'] = {'name': full.TOOL, 'arguments': {
            'policy_sha256': index['policy_sha256'], 'material_sha256': index['material_sha256'],
            'material_id': 'root', 'offset': 0, 'limit': min(full.PAGE_LIMIT, index['total_chars'])}}
    memory = result.get('code_reading_memory', {})
    if memory.get('version') == 1 and 'directory' in memory:
        sources, symbols = {}, {}
        source_ids, symbol_ids = {}, {}
        def location(anchor):
            identity = {k: anchor[k] for k in (
                'path', 'file_sha256', 'source_sha256', 'source_record_sha256', 'coordinate_namespace') if k in anchor}
            signature = full.sha(identity)
            if signature not in source_ids:
                source_ids[signature] = 's' + str(len(sources))
                sources[source_ids[signature]] = identity
            source_id = source_ids[signature]
            refs = []
            for symbol in anchor.get('symbols', []):
                key = full.sha([source_id, symbol])
                if key not in symbol_ids:
                    symbol_ids[key] = 'f' + str(len(symbols))
                    symbols[symbol_ids[key]] = {'source': source_id, **copy.deepcopy(symbol)}
                refs.append(symbol_ids[key])
            return {'source': source_id, **{k: copy.deepcopy(v) for k, v in anchor.items()
                if k not in identity and k != 'symbols'}, 'symbols': refs}
        memory['directory'] = [{**{k: v for k, v in row.items() if k != 'anchors'},
            **({'anchors': [location(a) for a in row['anchors']]} if 'anchors' in row else {})}
            for row in memory['directory']]
        memory['focus'] = [location(a) for a in memory.get('focus', [])]
        memory['sources'] = sources
        fields = ['source', 'qualified_name', 'kind', 'char_start', 'char_end', 'start_line', 'end_line']
        memory['symbol_fields'] = fields
        memory['symbols'] = {key: [row[name] for name in fields] if set(row) == set(fields) else row
            for key, row in symbols.items()}
        history = memory.get('history', {})
        if isinstance(history.get('by_source_version'), list):
            groups = history['by_source_version']
            # Some legacy recovery certificates deliberately omit observations.
            # Their ordinary pager is empty although authenticated parent IDs
            # remain readable. Keep that exact discovery graph, not a pager
            # which cannot enumerate those notes.
            history['source_version_groups'] = len(groups)
            history['note_count'] = len({identity for group in groups for identity in group['note_ids']})
        # The note/source indexes already recover these exact original reads.
        # Do not repeat one proof/reopen record for every note/receipt pair.
        memory.pop('decisive_read_index', None)
        memory.pop('omitted_decisive_reads', None)
        memory['version'] = 2
        memory['index_encoding'] = ('source-symbol-table-v1: each anchor.source resolves in sources; '
            'anchor.symbols resolves in symbols (array values use symbol_fields columns; objects are literal). '
            'Coordinates and all source versions are exact. '
            'read_with recovers the original note, conclusion and complete source ranges. '
            'Reading receipt proofs stay in the host journal; this index grants no reading credit or approval.')
    confirmations = result.get('full_review_note_receipts')
    if isinstance(confirmations, dict) and isinstance(confirmations.get('receipts'), list):
        confirmations['indexed_receipt_count'] = len(confirmations.pop('receipts'))
        confirmations['original_handoff_id'] = context[reading.FULL_PROJECTION]['handoff_id']
    return result


def _saved_note_ids(row, known, hid):
    """Only the supported, exact durable success shape is replaceable."""
    if not isinstance(row, dict) or set(row) != {'call', 'result'}:
        return None
    call, result = row['call'], row['result']
    if (not isinstance(call, dict) or set(call) != {'name', 'arguments'}
            or call['name'] != notes.TOOL or not isinstance(call['arguments'], dict)
            or not isinstance(result, dict) or set(result) != {
                'notes', 'offset', 'total', 'next_offset', 'scientific_approval', 'reading_evidence'}
            or result['scientific_approval'] is not False or result['reading_evidence'] is not False
            or not isinstance(result['notes'], list)
            or any(type(result[k]) is not int or result[k] < 0 for k in ('offset', 'total'))
            or result['next_offset'] is not None and type(result['next_offset']) is not int):
        return None
    from .prose_schema import validate
    from jsonschema.exceptions import ValidationError
    try:
        validate(call['arguments'], notes.schema())
    except ValidationError:
        return None
    if any(not isinstance(note, dict) or known.get(note.get('id')) != note for note in result['notes']):
        return None
    args = call['arguments']
    if args['action'] == 'save':
        total = result['total']
        if (not 1 <= total <= len(known) or result['offset'] != max(0, total - 12)
                or result['next_offset'] is not None or len(result['notes']) != min(12, total)):
            return None
        saved = [note for note in known.values() if note['handoff_id'] == hid]
        if any(not any(note['note'] == candidate for note in saved) for candidate in args['notes']):
            return None
    return [note['id'] for note in result['notes']]


_SCOPED_DIRECTORY_FIELDS = {
    'documents.list': {'artifact_id', 'chunk_count', 'document_id', 'kind', 'primary_source_reading',
        'provenance', 'read', 'scientific_claims_verified', 'search', 'sections', 'sha256', 'title', 'total_chars'},
    'artifacts.list': {'delivery_hash', 'directory', 'entries', 'next_offset', 'offset', 'reading_evidence', 'total_entries'},
    'code.locate': {'entries', 'next_offset', 'reading_evidence', 'total_entries'},
}
_SCOPED_DIRECTORY_ARGUMENTS = {
    'documents.list': {'document_id', 'expected_sha256'},
    'artifacts.list': {'directory', 'offset', 'limit'},
    'code.locate': {'path', 'query', 'offset', 'limit'},
}


def _plain_fields(value, allowed):
    return isinstance(value, dict) and set(value) <= allowed and all(
        not isinstance(v, (dict, list)) for v in value.values())


def _document_directory(row):
    if (not isinstance(row, dict) or set(row) != _SCOPED_DIRECTORY_FIELDS['documents.list']
            or row['primary_source_reading'] is not False or row['scientific_claims_verified'] is not False
            or not isinstance(row['sections'], list)
            or not all(_plain_fields(s, {'id', 'title', 'start', 'end', 'chars'}) for s in row['sections'])):
        return False
    for field, args in (('read', {'section_id', 'offset', 'limit'}), ('search', {'query'})):
        action = row[field]
        if (not isinstance(action, dict) or set(action) != {'tool', 'arguments'}
                or action['tool'] != 'documents.' + field
                or not _plain_fields(action['arguments'], args | {'document_id', 'expected_sha256'})):
            return False
    return _plain_fields({k: v for k, v in row.items() if k not in {'sections', 'read', 'search'}},
        _SCOPED_DIRECTORY_FIELDS['documents.list'])


def _scoped_directory_receipt(row):
    if (not isinstance(row, dict) or set(row) != {'call', 'result'} or not isinstance(row['call'], dict)
            or set(row['call']) != {'name', 'arguments'}):
        return False
    tool, result = row['call']['name'], row['result']
    fields = _SCOPED_DIRECTORY_FIELDS.get(tool)
    if not fields or not _plain_fields(row['call']['arguments'], _SCOPED_DIRECTORY_ARGUMENTS[tool]):
        return False
    if tool == 'documents.list':
        return isinstance(result, list) and all(_document_directory(doc) for doc in result)
    entries = ({'artifact_id', 'bytes', 'format', 'path', 'sha256', 'type', 'file_count'} if tool == 'artifacts.list'
        else {'artifact_id', 'path', 'sha256', 'offset', 'limit', 'start_line', 'end_line', 'symbol', 'qualified_symbol'})
    return (isinstance(result, dict) and set(result) == fields and result['reading_evidence'] is False
        and isinstance(result['entries'], list) and all(_plain_fields(entry, entries) for entry in result['entries'])
        and _plain_fields({k: v for k, v in result.items() if k != 'entries'}, fields - {'entries'}))


def _scoped_projection(engine, rid, role, hid, frozen, prepared, progress, tool_results):
    """Keep live replies and exact source coverage. Regenerate old directories on demand."""
    requested = set(requested_indices(progress, tool_results))
    from .code_whole_review import permits_tool, restricted_view, scope_directory
    restricted = restricted_view(prepared['context'])
    if not requested and not restricted:
        return prepared, tool_results, None
    rows, selected, omitted = [], [], 0
    scope_changed = False
    known = None
    for position, row in enumerate(tool_results or []):
        call = row.get('call', {}) if isinstance(row, dict) else {}
        result = row.get('result', {}) if isinstance(row, dict) else {}
        args = call.get('arguments') if isinstance(call, dict) else None
        if (restricted and isinstance(args, dict) and not (isinstance(result, dict) and result.get('error'))
                and not permits_tool(frozen, call.get('name'), args)):
            omitted += 1
            continue
        if restricted and isinstance(call, dict) and call.get('name') == 'documents.list' and isinstance(result, list):
            entries = scope_directory(frozen, result)
            if len(entries) != len(result):
                row = {**row, 'result': entries}
                scope_changed = True
        if requested and position not in requested:
            if _scoped_directory_receipt(row):
                omitted += 1
                continue
            if isinstance(call, dict) and call.get('name') == notes.TOOL:
                args = call.get('arguments', {})
                if isinstance(args, dict) and args.get('action') == 'save':
                    if known is None:
                        known = {r['id']: r for r in notes.records(engine, rid, role, hid, for_projection=True)}
                    if _saved_note_ids(row, known, hid) is not None:
                        omitted += 1
                        continue
        if position in requested:
            selected.append(len(rows))
        rows.append(row)
    if not omitted and not scope_changed:
        return prepared, tool_results, None
    result = copy.deepcopy(prepared)
    result['context']['prior_navigation_receipts'] = {'count': omitted,
        'directories': 'Use the current document and code catalogs, or issue the exact list or locate tool again.',
        'observations': 'Read saved conclusions and their source ranges by observation ID.',
        'reading_credit_granted': False}
    return result, rows, selected


def project(engine, rid, role, hid, frozen, prepared, progress, tool_results):
    """Authenticate full packet ownership, then prepare the existing compiler plan.

    No receipt, note, source or delivery is created here. New explicit note
    reads stay exact; verified saves and consumed old note reads become indexes.
    Unknown/error/extended results remain verbatim. Raw source replacement is
    deferred to the compiler, after the combined source window projection.
    """
    if full.code_delivery_scope(frozen):
        return _scoped_projection(engine, rid, role, hid, frozen, prepared, progress, tool_results)
    if (not full.tool_available(frozen) or type(frozen.get(packets.PROTOCOL)) is not int
            or frozen[packets.PROTOCOL] not in (2, 3)
            or not reading.compiler_enabled(engine, rid, prepared['context'])
            or 'code_review_context_projection' in prepared['context']):
        return prepared, tool_results, None
    plan = reading.prepare(engine, rid, role, hid, frozen, progress)
    if plan is None:
        return prepared, tool_results, None
    result = copy.deepcopy(prepared)
    context = result['context']
    result['task'] += ('\nContinue from the open questions and exact source-version/range indexes. '
        'Previously delivered source bodies and saved conclusions leave the active window; '
        'retrieve an exact note_id or source range only when a specific unresolved question requires it. '
        'Unclassified legacy notes remain unknown, not resolved. A closed question is not scientific approval. '
        'Keep required reading coverage and original scientific/execution gates unchanged.')
    context[reading.PLAN] = plan
    context[reading.FULL_PROJECTION] = {'version': 1, 'run_id': rid, 'role': role, 'handoff_id': hid,
        'policy_sha256': frozen[full.KEY]['sha256'], 'material_protocol': frozen[packets.PROTOCOL],
        reading.REUSE_POLICY: reading.REUSE_POLICY_VERSION}
    with engine.store.read_snapshot() as store:
        reader = copy.copy(engine)
        reader.store = store
        has_note_receipts = any(isinstance(row, dict) and isinstance(row.get('call'), dict)
            and row['call'].get('name') == notes.TOOL for row in tool_results or [])
        known = {note['id']: note for note in notes.records(reader, rid, role, hid, for_projection=True)} if has_note_receipts else {}
        commit = validate_commit(store, rid, role, marker(rid, role, hid, progress))
        consumed = Counter(commit['payload']['receipt_ids']) if commit else Counter()
        verified_reads = {}
        for row in tool_results or []:
            call = row.get('call', {}) if isinstance(row, dict) else {}
            args = call.get('arguments', {})
            if (call.get('name') != notes.TOOL or not isinstance(args, dict)
                    or args.get('action') != 'read' or not args.get('note_id')
                    or not consumed[receipt_id(rid, row)]):
                continue
            # Reauthenticate the original reader's complete locator response.
            # Unknown qualifications and failed reads never silently disappear.
            from jsonschema.exceptions import ValidationError
            try:
                exact = notes.execute(reader, rid, role, hid, args)
            except (ValueError, KeyError, TypeError, ValidationError):
                continue
            if exact == row.get('result'):
                verified_reads[full.sha(row)] = [r['id'] for r in exact['notes']]
    requested = set(requested_indices(progress, tool_results))
    rows, selected, indexed = [], [], []
    for position, row in enumerate(tool_results or []):
        identity = receipt_id(rid, row)
        old = consumed[identity] > 0
        if old:
            consumed[identity] -= 1
        note_ids = _saved_note_ids(row, known, hid)
        if note_ids is None:
            note_ids = verified_reads.get(full.sha(row))
        if note_ids is not None and (row['call']['arguments']['action'] == 'save'
                or old and position not in requested):
            indexed.append({'original_receipt_id': identity, 'note_ids': note_ids,
                'action': row['call']['arguments']['action'], 'new_reading_progress': False})
        else:
            if position in requested:
                selected.append(len(rows))
            rows.append(copy.deepcopy(row))
    context['full_review_note_receipts'] = {'version': 1, 'receipts': indexed,
        'read_with': {'name': notes.TOOL, 'arguments': {'action': 'read', 'offset': 0, 'limit': 12}},
        'scientific_approval': False,
        'scope': 'Exact durable own-note confirmations; original calls/results remain in their journal. '
            'Active questions are in code_reading_memory; retrieve conclusions by exact note_id.'}
    return result, rows, selected
