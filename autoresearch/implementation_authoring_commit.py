"""Publish a validated draft only while its frozen assignment and base still match.

Document files/indexes are immutable and prepared before this short transaction.
An interruption may leave an unbound candidate, which an exact retry can reuse;
it must never advance the current draft or emit a successful save on its own.
"""
from __future__ import annotations

import json

from .storage import now


def run_guard(run):
    return {**{key: run.get(key) for key in ('id', 'status', 'stage', 'mode', 'config')},
            'state': {key: value for key, value in run.get('state', {}).items()
                      if key in {'research_cycle', 'resume_generation', 'implementation_generation',
                                 'implementation_restart', 'implementation_graph_thread_id'}}}


def same_request(record, tool_name, args):
    from .engine import digest
    # Python equality treats True == 1; a tool retry must preserve JSON types.
    return (record.get('write_tool', 'implementation.write_document') == tool_name
            and 'write_arguments' in record
            and digest(record['write_arguments']) == digest(args))


def publish(engine, run_id, handoff_id, record, *, latest, assignment, context, state, control):
    from .engine import digest
    from .implementation_authoring import COLLECTION, pointer
    from .implementation_authoring_preservation import SaveConflict, descriptor

    with engine.store._db() as db:
        db.execute('BEGIN IMMEDIATE')

        def get(collection, key):
            row = db.execute('SELECT data FROM records WHERE collection=? AND key=?',
                             (collection, key)).fetchone()
            return json.loads(row['data']) if row else None

        engine.store._guard_write(db, COLLECTION, handoff_id, record)
        run = get('runs', run_id) or {}
        inputs = get('handoff_inputs', handoff_id) or {}
        if (digest(run_guard(run)) != digest(control)
                or digest(get('handoffs', handoff_id)) != digest(assignment)
                or digest(inputs.get('context')) != digest(context)
                or digest(get('codex_workflows', state['id'])) != digest(state)):
            raise SaveConflict('authoring_state_conflict',
                '保存期间任务状态、配置或冻结编写交接已改变；未提交修改，请重新核对当前任务与文档版本。')

        current = get(COLLECTION, handoff_id)
        if digest(current) != digest(latest):
            # Two identical concurrent requests may prepare the same candidate.
            # Only the winner publishes a version/event; the other returns it.
            if (current and current.get('status') == 'draft'
                    and current.get('workflow_id') == state['id']
                    and current.get('revision') == record['revision']
                    and current.get('role') == record['role']
                    and digest(current.get('context')) == digest(context)
                    and same_request(current, record['write_tool'], record['write_arguments'])
                    and pointer(current) == pointer(record)):
                return current
            raise SaveConflict('authoring_base_conflict',
                '保存期间当前草稿已被另一请求修改；本批修改未提交，请基于最新版本重新核对整批 patch。',
                base=descriptor(current['row'], edit=True) if current else None)

        db.execute('INSERT INTO records VALUES (?,?,?,?) ON CONFLICT(collection,key) '
                   'DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at',
                   (COLLECTION, handoff_id, json.dumps(record, ensure_ascii=False, allow_nan=False), now()))
        data = {'handoff_id': handoff_id, 'workflow_id': state['id'], **pointer(record),
                'previous_sha256': record['previous_sha256'], 'audit_status': 'pending'}
        db.execute('INSERT INTO events(run_id,kind,message,data,created_at) VALUES(?,?,?,?,?)',
                   (run_id, 'implementation.document_authored',
                    '实验工程师已保存实现文档版本；尚未通过独立审计。',
                    json.dumps(data, ensure_ascii=False, allow_nan=False), now()))
    return record
