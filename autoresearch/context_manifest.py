"""Metadata-only audit of compiled input and acknowledged model delivery.

The immutable context snapshot owns the text. This module never grants scientific
verification or infers reading from storage, search, or a dispatch intention.
"""
from __future__ import annotations

import copy
import json

from .memory_capture import read_captured_input, sha_text, render_active_v1
from .storage import now

VERSION = 1


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _sha(value):
    return sha_text(_json(value))


def _resolved(active):
    """Expand only the compiler's lossless tables in this same input.

    Archive/catalog navigation pointers intentionally remain pointers. Missing or
    cyclic references are never interpreted as delivered text.
    """
    tables = {'$record': active.get('shared_context_records', {}),
              '$catalog_record': active.get('shared_context_catalog_records', {}),
              '$text': active.get('shared_context_text', {})}
    def expand(value, visited=frozenset()):
        if isinstance(value, dict):
            if len(value) == 1 and next(iter(value), None) in tables:
                kind, key = next(iter(value.items()))
                marker = (kind, str(key))
                table = tables[kind]
                if marker in visited or not isinstance(table, dict) or str(key) not in table:
                    return {'unresolved_context_reference': str(key)}
                return expand(table[str(key)], visited | {marker})
            return {key: expand(item, visited) for key, item in value.items()
                    if key not in {'shared_context_records', 'shared_context_catalog_records', 'shared_context_text'}}
        if isinstance(value, list):
            return [expand(item, visited) for item in value]
        return value
    result = expand(active)
    # These references also point inside this very input, rather than archives.
    def pointer(path):
        for root in (result, active):
            item = root
            try:
                for key in path.lstrip('/').split('/'):
                    key = key.replace('~1', '/').replace('~0', '~')
                    item = item[int(key)] if isinstance(item, list) else item[key]
                return expand(item)
            except (KeyError, IndexError, ValueError, TypeError):
                continue
        return None
    def metadata(value):
        if isinstance(value, dict):
            ref = value.get('source_metadata_ref')
            parent = pointer(ref) if isinstance(ref, str) and ref.startswith('/') else None
            if isinstance(parent, dict):
                value.update({key: item for key, item in parent.items() if key not in value})
            text_ref = value.get('text_ref')
            text = pointer(text_ref) if isinstance(text_ref, str) and text_ref.startswith('/') else None
            if isinstance(text, str) and 'text' not in value:
                value['text'] = text
            for child in list(value.values()):
                metadata(child)
        elif isinstance(value, list):
            for child in value:
                metadata(child)
    metadata(result)
    return result


IDENTITY_FIELDS = ('id', 'memory_id', 'chunk_id', 'document_id', 'source_id', 'source_alias_id',
                   'canonical_source_id', 'artifact_id', 'paper_id', 'rag_version_id', 'version_id',
                   'version', 'source_version', 'document_version', 'sha256', 'text_sha256',
                   'content_sha256', 'section', 'section_id', 'page_number', 'page_start', 'page_end',
                   'pdf_page', 'page', 'page_offset',
                   'char_start', 'char_end', 'offset', 'end', 'status', 'provenance',
                   'commit_id', 'handoff_id', 'state_id', 'source_status')


def _identity(value):
    metadata = value.get('metadata')
    combined = {**(metadata if isinstance(metadata, dict) else {}), **value}
    identity = {key: copy.deepcopy(combined[key]) for key in IDENTITY_FIELDS
                if key in combined and isinstance(combined[key], (str, int, float, bool, type(None)))}
    if type(value.get('chunk_index')) is int and isinstance(value.get('id'), str):
        # Existing memory records are the chunks themselves. Older records
        # need no rewrite merely to expose this identity in a new manifest.
        identity.setdefault('chunk_id', value['id'])
        identity['memory_id'] = value['id']
    return identity


def _source_sha(value):
    # Document-window fragments use text_sha256 for the displayed fragment,
    # whereas sha256 continues binding the complete immutable document.
    if value.get('document_id'):
        return value.get('sha256') or value.get('content_sha256') or value.get('text_sha256')
    return value.get('text_sha256') or value.get('sha256') or value.get('content_sha256')


def inventory(active):
    """List locators, transformations, and text hashes, never duplicate bodies."""
    active = _resolved(active)
    materials = []
    def add(kind, key, value, path, **extra):
        item = {'material_id': kind + ':' + str(key), 'kind': kind, 'path': path,
                'representation_sha256': _sha(value), **_identity(value if isinstance(value, dict) else {}), **extra}
        materials.append(item)
        return item
    for index, value in enumerate(active.get('retrieved_memories', [])):
        if isinstance(value, dict):
            add('memory', value.get('id', index), value, f'/retrieved_memories/{index}')
    for index, value in enumerate(active.get('recent_turns', [])):
        if isinstance(value, dict):
            add('history', value.get('id', index), value, f'/recent_turns/{index}',
                summary=bool(value.get('summary') or value.get('compacted')))
    context = active.get('context') or {}
    if not isinstance(context, dict):
        context = {}
    for key, value in context.items():
        if key != 'tool_results':
            kind = 'working_state' if key in {'working_state', 'research_state', '_working_set'} else 'task_context'
            add(kind, key, value, '/context/' + key)
    def sources(value, path, tool=None):
        if isinstance(value, list):
            for index, child in enumerate(value):
                sources(child, path + '/' + str(index), tool)
            return
        if not isinstance(value, dict):
            return
        identity = _identity(value)
        source = identity.get('source_id') or identity.get('document_id') or identity.get('artifact_id') or identity.get('rag_version_id')
        if source:
            text = value.get('text')
            exact = isinstance(text, str) and bool(text) and value.get('primary_text_displayed') is not False and not value.get('error')
            start = identity.get('char_start', identity.get('offset'))
            end = identity.get('char_end', identity.get('end'))
            if exact and type(start) is int and end is None:
                end = start + len(text)
            if exact and type(start) is int and type(end) is int and end - start != len(text):
                exact = False  # A range mismatch cannot establish delivery coverage.
            source_hash = _source_sha(identity)
            key = _sha([source, source_hash, identity.get('section_id', identity.get('section')), start, end,
                        identity.get('chunk_id'), identity.get('page_number'), identity.get('pdf_page'),
                        identity.get('page'), identity.get('page_start'), identity.get('page_end')])
            item = add('evidence', key, value, path, tool=tool, text_deliverable=exact,
                       representation='exact_text' if exact else 'metadata_or_summary')
            if exact:
                item.update(delivered_text_sha256=sha_text(text), delivered_chars=len(text),
                            char_start=start, char_end=end)
        if tool == 'memory.search' and isinstance(value.get('id'), str):
            add('memory', value['id'], value, path)
        for key, child in value.items():
            if key not in {'metadata', 'call', 'reasoning', 'internal_reasoning', 'analysis'}:
                sources(child, path + '/' + key, tool)
    for index, row in enumerate(context.get('tool_results', [])):
        if isinstance(row, dict):
            tool = (row.get('call') or {}).get('name')
            add('tool_receipt', _sha(row.get('call') or {'index': index}), row,
                f'/context/tool_results/{index}', tool=tool)
            sources(row.get('result'), f'/context/tool_results/{index}/result', tool)
    # Explicit task-bound text can also be supplied without a read-tool wrapper.
    for key, value in context.items():
        if key != 'tool_results':
            sources(value, '/context/' + key)
    return materials


def compile_manifest(snapshot, original_active, *, task_id=None, compiler=None, token_budget=None):
    capture = snapshot['model_input_capture']
    active = capture.get('redacted_active_context', snapshot['active_context'])
    candidates, compiled = inventory(original_active), inventory(active)
    # The audit view can redact ordinary source text as well as credentials.
    # Credit the exact dispatched bytes, not that display transformation. Keep
    # only hashes/lengths and require the original serialized input binding;
    # never persist an unredacted body or repair historical receipts in place.
    wire_active = snapshot['active_context']
    wire_bound = active != wire_active and capture.get('original_user_sha256') in {
        sha_text(render_active_v1(wire_active)), sha_text(_json(wire_active))}
    if wire_bound and active != wire_active:
        wire_items = {item['material_id']: item for item in inventory(wire_active)}
        for item in compiled:
            raw = wire_items.get(item['material_id'], {})
            if (item['kind'] == 'evidence' and raw.get('text_deliverable')
                    and _identity(item) == _identity(raw)):
                for key in ('delivered_text_sha256', 'delivered_chars', 'char_start', 'char_end'):
                    item[key] = raw[key]
                item.update(text_deliverable=True, representation='exact_text',
                            delivery_basis='bound_original_input', audit_view_redacted=True)
    by_id = {item['material_id']: item for item in compiled}
    original_ids = {item['material_id'] for item in candidates}
    materials = []
    context = original_active.get('context') or {}
    diagnostics = compiler or snapshot.get('compiler') or {}
    superseded = set(diagnostics.get('historical_reviews_replaced_by_current_gate', []))
    for candidate in candidates:
        current = by_id.get(candidate['material_id'])
        item = {**candidate, 'candidate': True, 'selected': current is not None,
                'compiled': current is not None, 'submitted': False}
        if current:
            changed = candidate['representation_sha256'] != current['representation_sha256']
            item.update(current, transformed=changed)
            if changed:
                item.update(original_representation_sha256=candidate['representation_sha256'],
                            transform='projected_or_compressed')
        else:
            reason = ('superseded_review' if candidate.get('id') in superseded else
                      'archived_or_projected_by_context_policy' if candidate['kind'] in {'evidence', 'tool_receipt', 'task_context'}
                      else 'context_policy_or_budget')
            item.update(exclusion_reason=reason, transformed=False)
        materials.append(item)
    for item in compiled:
        if item['material_id'] not in original_ids:
            materials.append({**item, 'candidate': False, 'selected': True, 'compiled': True,
                              'submitted': False, 'transformed': True, 'transform': 'compiler_added_view'})
    manifest = {'id': 'compiled:' + snapshot['id'], 'version': VERSION, 'run_id': snapshot['run_id'],
        'research_id': snapshot['run_id'], 'task_id': task_id or capture['operation_id'], 'role': snapshot['role'],
        'operation_id': capture['operation_id'], 'snapshot_id': snapshot['id'], 'request_id': None,
        'input_transcript_id': snapshot['original_transcript_id'], 'status': 'compiled',
        'created_at': now(), 'context_compiler_version': diagnostics.get('version', snapshot.get('method')),
        'system_sha256': capture['system_sha256'], 'user_sha256': capture['user_sha256'],
        'token_budget': token_budget, 'estimated_input_tokens': snapshot.get('active_tokens_estimate'),
        'original_estimated_input_tokens': snapshot.get('original_tokens_estimate'), 'materials': materials,
        'transforms': {key: copy.deepcopy(diagnostics[key]) for key in
            ('exact_text_table', 'exact_catalog_record_table', 'exact_record_table', 'working_set_actions',
             'source_metadata_projections', 'history_archived', 'historical_reviews_replaced_by_current_gate') if key in diagnostics},
        'delivery_definition': 'Provider-acknowledged input availability; not model understanding, scientific verification, or source approval.'}
    for key in ('handoff_id',):
        if key in capture:
            manifest[key] = capture[key]
    for key in ('proposal_version', 'proposal_hash', 'cycle', 'research_cycle'):
        if key in context:
            manifest[key] = copy.deepcopy(context[key])
    snapshot['context_manifest_id'] = manifest['id']
    snapshot['context_manifest_sha256'] = _sha(manifest)
    return manifest


def record_submission(store, binding, request_id, *, status, wire_sha256=None, thread_id=None, turn_id=None):
    """Bind a request to a frozen compilation; only acknowledgment creates delivery.

    Native pre-dispatch markers and HTTP transport failures use submission_unknown.
    Repeated progress receipts are idempotent and cannot downgrade delivered input.
    """
    if not binding:
        return None  # Legacy inputs retain their explicit historical read protocol.
    if status not in {'prepared', 'submission_unknown', 'submitted', 'completed', 'rejected'}:
        raise ValueError('Unsupported context submission status')
    if (binding.get('original_instruction_sha256') and wire_sha256 is not None
            and wire_sha256 != binding['original_instruction_sha256']):
        raise ValueError('Native submitted input differs from its frozen context binding')
    snapshot = store.get('context_snapshots', binding.get('context_snapshot_id'))
    if not snapshot:
        raise ValueError('Context manifest frozen snapshot unavailable')
    frozen = read_captured_input(snapshot, binding['run_id'], binding['role'])
    if not frozen or any(binding.get(key) != value for key, value in frozen['binding'].items()):
        raise ValueError('Context manifest request binding mismatch')
    compiled_id = binding.get('context_manifest_id', 'compiled:' + snapshot['id'])
    compiled = store.get('context_manifests', compiled_id)
    expected_sha = binding.get('context_manifest_sha256') or snapshot.get('context_manifest_sha256')
    if expected_sha and (not compiled or _sha(compiled) != expected_sha
            or snapshot.get('context_manifest_sha256') != expected_sha):
        raise ValueError('Compiled context manifest integrity mismatch')
    if not compiled:
        # Old prepared requests may resume, but no fabricated old candidate list.
        compiled = compile_manifest(snapshot, frozen['active_context'])
        compiled['legacy_candidate_inventory_unknown'] = True
    previous = store.get('context_manifests', request_id)
    if previous and previous.get('snapshot_id') != snapshot['id']:
        raise ValueError('Request already belongs to a different context compilation')
    acknowledged = status in {'submitted', 'completed'} or bool(previous and previous.get('status') in {'submitted', 'completed'})
    if previous and previous.get('status') == 'completed':
        status = 'completed'
    elif acknowledged and status not in {'submitted', 'completed'}:
        status = previous['status']
    manifest = {**copy.deepcopy(compiled), 'id': request_id, 'request_id': request_id,
                'compiled_manifest_id': compiled_id, 'status': status,
                'compiled_manifest_sha256': _sha(compiled),
                'submission_status': 'submitted' if acknowledged else status, 'updated_at': now()}
    for item in manifest['materials']:
        item['submitted'] = acknowledged and item['compiled']
    for key, value in (('wire_sha256', wire_sha256), ('thread_id', thread_id), ('turn_id', turn_id)):
        if value is not None:
            manifest[key] = value
        elif previous and key in previous:
            manifest[key] = previous[key]
    manifest['receipt_sha256'] = _sha(manifest)
    puts = [('context_manifests', request_id, manifest)]
    if acknowledged:
        emitted = set()
        for material in manifest['materials']:
            if not material['compiled'] or material['kind'] != 'evidence' or not material.get('text_deliverable'):
                continue
            identity = {key: copy.deepcopy(material[key]) for key in IDENTITY_FIELDS if key in material}
            # Full version hash is required; snippet hashes alone are insufficient.
            source_sha = _source_sha(identity)
            if (not isinstance(source_sha, str) or len(source_sha) != 64
                    or any(char not in '0123456789abcdefABCDEF' for char in source_sha)):
                continue
            did = 'delivery:' + _sha([request_id, material['material_id'], material['delivered_text_sha256']])
            if did in emitted:
                continue
            emitted.add(did)
            receipt = {**identity, 'id': did, 'run_id': manifest['run_id'], 'role': manifest['role'],
                'request_id': request_id, 'operation_id': manifest['operation_id'], 'manifest_id': request_id,
                'snapshot_id': snapshot['id'], 'status': 'delivered', 'sha256': source_sha,
                'text_sha256': source_sha, 'char_start': material.get('char_start'), 'char_end': material.get('char_end'),
                'delivered_chars': material['delivered_chars'], 'delivered_text_sha256': material['delivered_text_sha256'],
                'page_sha256': material['delivered_text_sha256'],
                'tool': material.get('tool'), 'path': material['path'], 'created_at': now(),
                'reading_semantics': 'Text available in acknowledged model input, not proof of comprehension.'}
            source_version = next((identity[key] for key in ('source_version', 'document_version',
                'rag_version_id', 'version_id', 'version') if identity.get(key) is not None), None)
            if source_version is not None:
                receipt['version'] = receipt['source_version'] = source_version
            if manifest.get('handoff_id'):
                receipt['handoff_id'] = manifest['handoff_id']
            puts.append(('evidence_deliveries', did, store.get('evidence_deliveries', did) or receipt))
    store.batch(puts=puts)
    return manifest


def get_manifest(store, request_id, *, run_id=None, role=None):
    manifest = store.get('context_manifests', request_id)
    if not manifest or (run_id is not None and manifest.get('run_id') != run_id) or (role is not None and manifest.get('role') != role):
        return None
    return manifest


def assert_http_wire_binding(binding, protocol, body):
    """Check protocol adaptation preserved the frozen text before dispatch."""
    if not binding:
        return
    def text(value):
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return ''.join(item['text'] for item in value if isinstance(item, dict)
                and item.get('type') in {'text', 'input_text'} and isinstance(item.get('text'), str))
        return None
    if protocol == 'responses':
        system, rows = text(body.get('instructions')), body.get('input')
    elif protocol == 'anthropic':
        system, rows = text(body.get('system')), body.get('messages')
    else:
        rows = body.get('messages')
        system = next((text(row.get('content')) for row in rows or []
                       if isinstance(row, dict) and row.get('role') == 'system'), None)
    user = next((text(row.get('content')) for row in rows or []
                 if isinstance(row, dict) and row.get('role') == 'user'), None)
    if (not isinstance(system, str) or not isinstance(user, str)
            or sha_text(system) != binding.get('system_sha256')
            or sha_text(user) != binding.get('original_user_sha256', binding.get('user_sha256'))):
        raise ValueError('HTTP submitted input differs from its frozen context binding')


def validate_manifest(store, manifest):
    """Check a persisted request inventory against its frozen compilation.

    This reads only snapshot metadata, avoiding repeated decoding of large input
    bodies in note/review gates. Legacy rows without this protocol are not an
    authenticated version-1 manifest and return False.
    """
    if not isinstance(manifest, dict) or manifest.get('version') != VERSION:
        return False
    received_sha = manifest.get('receipt_sha256')
    if manifest.get('request_id') and (not received_sha
            or _sha({key: value for key, value in manifest.items() if key != 'receipt_sha256'}) != received_sha):
        return False
    compiled_id = manifest.get('compiled_manifest_id', manifest.get('id'))
    # Inventory validation consumes this exact transaction's fresh decode once;
    # large compiled histories need not evict the compact delivery operands.
    get = getattr(store, '_owned_record', store.get)
    compiled = get('context_manifests', compiled_id)
    if not compiled or compiled.get('status') != 'compiled':
        return False
    snapshot_id = compiled.get('snapshot_id')
    if hasattr(store, '_read_db'):
        fields = ('id', 'run_id', 'role', 'original_transcript_id', 'context_manifest_id', 'context_manifest_sha256')
        selection = ','.join("'" + key + "',json_extract(data,'$." + key + "')" for key in fields)
        with store._read_db() as db:
            row = db.execute("SELECT json_object(" + selection + ") FROM records "
                "WHERE collection='context_snapshots' AND key=?", (snapshot_id,)).fetchone()
        snapshot = json.loads(row[0]) if row else None
    else:
        snapshot = store.get('context_snapshots', snapshot_id)
    compiled_sha = _sha(compiled)
    if (not snapshot or snapshot.get('context_manifest_id') != compiled_id
            or snapshot.get('context_manifest_sha256') != compiled_sha
            or any(snapshot.get(key) != compiled.get(key) for key in ('run_id', 'role'))
            or snapshot.get('original_transcript_id') != compiled.get('input_transcript_id')):
        return False
    if not manifest.get('request_id'):
        return manifest == compiled
    if manifest.get('compiled_manifest_sha256') != compiled_sha:
        return False
    submitted = manifest.get('status') in {'submitted', 'completed'}
    if (manifest.get('submission_status') == 'submitted') != submitted:
        return False
    for key, value in compiled.items():
        if key in {'id', 'request_id', 'status', 'materials'}:
            continue
        if manifest.get(key) != value:
            return False
    # Only this top-level flag changes; nested inventory operands stay read-only.
    expected_materials = [{**item, 'submitted': submitted and item['compiled']}
                          for item in compiled['materials']]
    return manifest.get('materials') == expected_materials
