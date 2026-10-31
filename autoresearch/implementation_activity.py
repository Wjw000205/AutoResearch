"""Read-only activity exclusions bound to the current implementation restart.

Paid originals, scientific sources and billing remain untouched. Only compact
receipt metadata enters these projections; never decode the offline archive.
"""
from __future__ import annotations

import json


_PURPOSES = ('implementation', 'spec_review', 'implementation_spec',
    'implementation_document_section', 'implementation_document_review', 'implementation_document_author',
    'code_review')
_ARTIFACT_KINDS = ('implementation_specification', 'implementation_review_response', 'implementation_review_history')


def receipt_metadata(store, run_id, *, include_handoff_scopes=False):
    if not run_id:
        return {}
    with store._read_db() as db:
        row = db.execute("""SELECT json_object(
            'id',receipt.key,'generation',json_extract(receipt.data,'$.generation'),
            'created_at',json_extract(receipt.data,'$.created_at'),
            'handoff_ids',json_extract(receipt.data,'$.archived_implementation_handoff_ids'),
            'artifact_ids',json_extract(receipt.data,'$.archived_implementation_artifact_ids'))
            FROM records run JOIN records receipt
                ON receipt.collection='implementation_restarts'
                AND receipt.key=json_extract(run.data,'$.state.implementation_restart')
            WHERE run.collection='runs' AND run.key=?
                AND json_extract(receipt.data,'$.run_id')=run.key
                AND json_extract(receipt.data,'$.generation')=json_extract(run.data,'$.state.implementation_generation')""",
            (run_id,)).fetchone()
        if not row:
            return {}
        metadata = json.loads(row[0])
        # Existing receipts predate the explicit UI exclusion arrays. Derive
        # their metadata locally, without changing either receipt or archive.
        if metadata['handoff_ids'] is None:
            placeholders = ','.join('?' for _ in _PURPOSES)
            metadata['handoff_ids'] = [item[0] for item in db.execute(
                "SELECT key FROM records WHERE collection='handoffs' AND json_extract(data,'$.run_id')=? "
                "AND coalesce(json_extract(data,'$.created_at'),updated_at)<=? "
                "AND (json_extract(data,'$.stage')='implementation' OR "
                "substr(json_extract(data,'$.purpose'),1,instr(json_extract(data,'$.purpose')||':',':')-1) IN ("
                + placeholders + ')) ORDER BY key', (run_id, metadata['created_at'], *_PURPOSES))]
        if metadata['artifact_ids'] is None:
            placeholders = ','.join('?' for _ in _ARTIFACT_KINDS)
            metadata['artifact_ids'] = [item[0] for item in db.execute(
                "SELECT key FROM records WHERE collection='artifacts' AND json_extract(data,'$.run_id')=? "
                "AND coalesce(json_extract(data,'$.created_at'),updated_at)<=? "
                "AND json_extract(data,'$.kind') IN (" + placeholders + ') ORDER BY key',
                (run_id, metadata['created_at'], *_ARTIFACT_KINDS))]
        if include_handoff_scopes:
            # Read each archived handoff once by its primary key. Canonical JSON
            # keys distinguish unknown native roles from exact role matches.
            # The caller's NOT IN list is independent of every native record.
            scopes = db.execute("""SELECT DISTINCT
                json_array('purpose',json_extract(data,'$.purpose')),
                json_array('role',json_extract(data,'$.purpose'),json_extract(data,'$.to_role'))
                FROM records WHERE collection='handoffs'
                    AND key IN (SELECT value FROM json_each(?))
                    AND json_extract(data,'$.purpose') IS NOT NULL""",
                (json.dumps(metadata['handoff_ids']),))
            metadata['handoff_scope_keys'] = sorted({value for scope in scopes for value in scope})
    for field in ('handoff_ids', 'artifact_ids'):
        metadata[field] = sorted({value for value in metadata[field] if isinstance(value, str)})
    return metadata


def predicate(metadata, scope, *, key='records.key', data='records.data'):
    """Fixed internal expressions; IDs stay in a single parameterized array."""
    if scope == 'codex_workflows':
        return (f"json_type({data},'$.implementation_indexed_review_protocol')='integer' "
            f"AND json_extract({data},'$.implementation_indexed_review_protocol')=1 "
            "AND EXISTS(SELECT 1 FROM records active_run WHERE active_run.collection='runs' "
            f"AND active_run.key=json_extract({data},'$.run_id') AND "
            "(json_extract(active_run.data,'$.state.implementation_generation') IS NULL OR "
            f"json_extract({data},'$.implementation_generation')=json_extract(active_run.data,'$.state.implementation_generation')))", ())
    if not metadata:
        return '1', ()
    field = 'artifact_ids' if scope in {'artifacts', 'memory_documents'} else 'handoff_ids'
    ids = metadata.get(field, [])
    if not ids:
        return '1', ()
    encoded = json.dumps(ids)
    if scope in {'handoffs', 'handoff_inputs', 'decisions', 'handoff_progress'}:
        return f'{key} NOT IN (SELECT value FROM json_each(?))', (encoded,)
    if scope == 'artifacts':
        return f'{key} NOT IN (SELECT value FROM json_each(?))', (encoded,)
    if scope == 'memory_documents':
        # Content-addressed aliases may include both a retained scientific
        # source and an archived implementation file. Keep mixed live aliases.
        return (f"NOT (coalesce(json_extract({data},'$.artifact_id') IN (SELECT value FROM json_each(?)),0) "
            f"AND NOT EXISTS(SELECT 1 FROM json_each({data},'$.artifact_ids') alias "
            "WHERE alias.value NOT IN (SELECT value FROM json_each(?))))", (encoded, encoded))
    if scope == 'memory':
        visible, parameters = predicate(metadata, 'memory_documents', key='manifest.key', data='manifest.data')
        return ("NOT EXISTS(SELECT 1 FROM records manifest WHERE manifest.collection='memory_documents' "
            f"AND manifest.key=json_extract({data},'$.document_id') AND NOT ({visible}))", parameters)
    if scope == 'codex_reasoning':
        # Unary + preserves SQLite values/types while allowing JSON expression
        # index values to replace functions that carry a JSON result subtype.
        purpose, role = f"+json_extract({data},'$.selection.purpose')", f"+json_extract({data},'$.role')"
        scopes = metadata.get('handoff_scope_keys')
        if scopes is None:
            raise ValueError('Native activity filtering requires projected handoff scopes')
        return (f"CASE WHEN {role} IS NULL THEN json_array('purpose',{purpose}) "
            f"ELSE json_array('role',{purpose},{role}) END NOT IN (SELECT value FROM json_each(?))",
            (json.dumps(scopes),))
    elif scope == 'transcripts':
        purpose = f"coalesce(json_extract({data},'$.context._purpose'),json_extract({data},'$.request_scope.purpose'))"
        role = f"json_extract({data},'$.role')"
    elif scope == 'context_snapshots':
        purpose = (f"coalesce(json_extract({data},'$.active_context.context._purpose'),"
            "(SELECT json_extract(transcript.data,'$.context._purpose') FROM records transcript "
            f"WHERE transcript.collection='transcripts' AND transcript.key=json_extract({data},'$.original_transcript_id')))" )
        role = f"json_extract({data},'$.role')"
    else:
        return '1', ()
    return ("NOT EXISTS(SELECT 1 FROM records archived JOIN json_each(?) old ON old.value=archived.key "
        "WHERE archived.collection='handoffs' AND json_extract(archived.data,'$.purpose')=" + purpose
        + " AND (" + role + " IS NULL OR json_extract(archived.data,'$.to_role')=" + role + '))', (encoded,))


def records(store, collection, run_id, *, metadata=None, limit=None):
    metadata = receipt_metadata(store, run_id, include_handoff_scopes=collection == 'codex_reasoning') if metadata is None else metadata
    condition, args = predicate(metadata, collection)
    with store._read_db() as db:
        sql = "SELECT key FROM records WHERE collection=? AND json_extract(data,'$.run_id')=? AND " + condition
        sql += ' ORDER BY updated_at DESC,key DESC' if limit is not None else ' ORDER BY updated_at,key'
        parameters = [collection, run_id, *args]
        if limit is not None:
            sql += ' LIMIT ?'
            parameters.append(limit)
        keys = [row[0] for row in db.execute(sql, parameters)]
    if limit is not None:
        keys.reverse()
    return [value for key in keys if (value := store.get(collection, key)) is not None]


def count(store, collection, run_id, *, metadata=None):
    metadata = receipt_metadata(store, run_id, include_handoff_scopes=collection == 'codex_reasoning') if metadata is None else metadata
    condition, args = predicate(metadata, collection)
    with store._read_db() as db:
        return db.execute("SELECT count(*) FROM records WHERE collection=? AND json_extract(data,'$.run_id')=? AND "
            + condition, (collection, run_id, *args)).fetchone()[0]
