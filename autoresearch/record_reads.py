"""Scoped exact reads that never retain an entire collection of decoded bodies."""
from __future__ import annotations

import hashlib
import json


def iter_records(store, collection, run_id=None, *, where='', args=(), order_by='updated_at,key'):
    # Expressions come only from internal callers; all identities are bound.
    predicate, parameters = store._record_filter(collection, run_id)
    if where:
        predicate += ' AND (' + where + ')'
        parameters.extend(args)
    with store._read_db() as db:
        for row in db.execute('SELECT data FROM records WHERE ' + predicate + ' ORDER BY ' + order_by, parameters):
            yield json.loads(row['data'])


def hash_records(records):
    """Exactly hash json.dumps(list(records), sort_keys=True), one row at a time."""
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, allow_nan=False)
    result = hashlib.sha256(b'[')
    first = True
    for record in records:
        if not first:
            result.update(b', ')
        first = False
        for chunk in encoder.iterencode(record):
            result.update(chunk.encode())
    result.update(b']')
    return result.hexdigest()


def output_exists(store, run_id, operation_id, *, request_ids=(), controller_operation_id=None):
    conditions = ["json_extract(data,'$.operation_id') IS ?"]
    values = [operation_id]
    if request_ids:
        conditions.append("json_extract(data,'$.request_id') IN (" + ','.join('?' for _ in request_ids) + ')')
        values.extend(request_ids)
    if controller_operation_id is not None:
        conditions.append("json_extract(data,'$.controller_operation_id') IS ?")
        values.append(controller_operation_id)
    scope = " AND json_extract(data,'$.run_id')=?" if run_id is not None else ''
    with store._read_db() as db:
        return db.execute("SELECT 1 FROM records WHERE collection='transcripts' "
            "AND json_extract(data,'$.kind')='output'" + scope + ' AND (' + ' OR '.join(conditions) + ') LIMIT 1',
            ([run_id] if run_id is not None else []) + values).fetchone() is not None


def receipt_headers(store, collection, run_id=None, *, where='', args=()):
    predicate, parameters = store._record_filter(collection, run_id)
    if collection == 'transcripts':
        predicate += " AND json_extract(data,'$.kind')='output'"
    if where:
        predicate += ' AND (' + where + ')'
        parameters.extend(args)
    fields = ('id', 'run_id', 'kind', 'role', 'operation_id', 'controller_operation_id', 'request_id')
    columns = ','.join(f"'{field}',json_extract(data,'$.{field}')" for field in fields)
    with store._read_db() as db:
        for row in db.execute('SELECT json_object(' + columns + ') FROM records WHERE ' + predicate + ' ORDER BY updated_at,key', parameters):
            yield json.loads(row[0])


class ReceiptSourceStore:
    """Only output association predicates need transcript headers in retry proofs."""
    def __init__(self, store, *, operations=(), controllers=(), request_ids=()):
        self.store = store
        predicates, arguments = [], []
        for field, values in (('operation_id', operations), ('controller_operation_id', controllers),
                              ('id', request_ids), ('request_id', request_ids)):
            if values:
                predicates.append(f"json_extract(data,'$.{field}') IN (" + ','.join('?' for _ in values) + ')')
                arguments.extend(values)
        self.predicate = ' OR '.join(predicates)
        self.arguments = arguments

    def __getattr__(self, name):
        return getattr(self.store, name)

    def list(self, collection, run_id=None):
        if collection == 'transcripts':
            return receipt_headers(self.store, collection, run_id, where=self.predicate, args=self.arguments)
        # These proofs filter associated rows before retaining their full bytes.
        if collection == 'request_audits':
            return iter_records(self.store, collection, run_id, where=self.predicate, args=self.arguments)
        return iter_records(self.store, collection, run_id)


def receipt_source_store(store, intelligence, run_id, progress, records):
    """Select the complete current/archived lineage before original-byte proofs."""
    operations, controllers = set(), set()
    slots = [progress.get('normal_request'), progress.get('finalization'),
             *progress.get('finalization_history', [])]
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        if isinstance(slot.get('operation_id'), str):
            controllers.add(slot['operation_id'])
        prepared = slot.get('prepared')
        if not isinstance(prepared, dict) or not isinstance(prepared.get('context'), dict):
            continue
        if set(('role', 'task', 'context', 'schema')) <= prepared.keys():
            operations.add(intelligence._operation_identity(prepared['role'], prepared['task'],
                prepared['context'], prepared['schema']))
        control = prepared['context'].get('_harness_control')
        if isinstance(control, dict) and isinstance(control.get('operation_id'), str):
            controllers.add(control['operation_id'])
    request_ids = {row['id'] for row in records if isinstance(row, dict) and isinstance(row.get('id'), str)
        and ((isinstance(row.get('operation_id'), str) and row['operation_id'] in operations)
             or (isinstance(row.get('controller_operation_id'), str)
                 and row['controller_operation_id'] in controllers))}
    return ReceiptSourceStore(store, operations=sorted(operations), controllers=sorted(controllers),
        request_ids=sorted(request_ids))
