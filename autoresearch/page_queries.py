"""Bounded display queries. These do not authorize research or replace evidence."""
from __future__ import annotations

import json

from .storage import redact


def run_list(store, *, limit=None, offset=0):
    fields = ('id', 'title', 'mode', 'status', 'stage', 'created_at', 'updated_at', 'progress')
    projection = ','.join(f"'{field}',json_extract(data,'$.{field}')" for field in fields)
    query = f"SELECT json_object({projection},'objective_preview',substr(json_extract(data,'$.objective'),1,320)) FROM records WHERE collection='runs' ORDER BY updated_at DESC,key DESC"
    parameters = []
    if limit is not None:
        query += ' LIMIT ? OFFSET ?'
        parameters = [limit, offset]
    with store._read_db() as db:
        return [json.loads(row[0]) for row in db.execute(query, parameters)]


def run_counts(store):
    with store._read_db() as db:
        row = db.execute("""SELECT count(*),coalesce(sum(json_extract(data,'$.status')='running'),0),
            coalesce(sum(json_extract(data,'$.status')='completed'),0)
            FROM records WHERE collection='runs'""").fetchone()
    return dict(zip(('total', 'running', 'completed'), row))


def memory_page(store, run_id=None, *, limit=30, offset=0, key=None):
    # Embedding features and complete document text have no place in list cards.
    fields = ('id', 'run_id', 'role', 'created_at', 'metadata')
    projection = ','.join(f"'{field}',json_extract(data,'$.{field}')" for field in fields)
    text = "json_extract(data,'$.text')" if key is not None else "substr(json_extract(data,'$.text'),1,2000)"
    projection += f",'text',{text},'text_chars',length(json_extract(data,'$.text'))"
    where, parameters = store._record_filter('memory', run_id)
    if key is not None:
        where += ' AND key=?'
        parameters.append(key)
    with store._read_db() as db:
        total = db.execute('SELECT count(*) FROM records WHERE ' + where, parameters).fetchone()[0]
        rows = [json.loads(row[0]) for row in db.execute('SELECT json_object(' + projection +
            ') FROM records WHERE ' + where + ' ORDER BY updated_at DESC,key DESC LIMIT ? OFFSET ?',
            [*parameters, limit, offset])]
    for row in rows:
        row['truncated'] = len(row.get('text') or '') < (row.get('text_chars') or 0)
    return redact(rows), total
