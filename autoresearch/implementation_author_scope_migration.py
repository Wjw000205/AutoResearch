"""Explicit, guarded upgrade of an unsent legacy author-read scope.

Legacy grants did not archive the full workflow snapshot. This upgrade therefore
re-authenticates their frozen scientific baseline; it does not assert that the
old whole-record hash changed only because of a timestamp. Never called by model
tools, normal admission, or an automatic retry.
"""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

from .implementation_review_routing import digest
from .storage import now


def _require(condition, message):
    if not condition:
        raise ValueError('Author read scope migration: ' + message)


def migrate(engine, run_id, hid, expected_progress_sha256, expected_workflow_sha256, *, apply=False):
    from . import implementation_author_read_recovery as read
    from . import implementation_issue_responses as replies
    from . import implementation_indexed_replies as indexed
    from .implementation_author_recovery_scope import SCOPE_VERSION, workflow_snapshot
    from .implementation_alignment_evidence import READ_PENDING, validate_candidate
    from .engine import Harness, ResearchBlocked

    store = engine.store
    progress = copy.deepcopy(store.get('handoff_progress', hid, {}))
    old = progress.get(read.KEY, {})
    run = store.get('runs', run_id, {})
    lease = store.get('runtime', 'worker', {})
    assignment = store.get('handoffs', hid, {})
    frozen = store.get('handoff_inputs', hid, {})
    context = frozen.get('context', {})
    state = store.get('codex_workflows', context.get('implementation_document_authoring', {}).get('workflow_id', ''), {})
    _require(digest(progress) == expected_progress_sha256 and digest(state) == expected_workflow_sha256,
             'checkpoint or current workflow changed since explicit preflight')
    if old.get('scope_version') == SCOPE_VERSION:
        read.validate_scope(SimpleNamespace(engine=engine), run_id, 'engineer', assignment['purpose'], hid, progress)
        return {'already_applied': True, 'applied': False, 'scientific_approval': False}
    _require(run.get('status') == 'blocked' and run.get('stage') == 'implementation'
        and not lease.get('held') and str(run.get('error', '')).startswith('实现作者新稿补读：')
        and assignment.get('status') == 'blocked' and old.get('scope_version') is None,
        'requires an explicitly stopped legacy read continuation')
    _require(old.get('sha256') == digest({k: v for k, v in old.items() if k != 'sha256'})
        and old.get('version') == read.VERSION and old.get('role') == 'engineer'
        and old.get('run_id') == run_id and old.get('handoff_id') == hid
        and old.get('purpose') == assignment.get('purpose')
        and old.get('allowed_tool') == 'documents.read' and old.get('scientific_approval') is False
        and type(old.get('start_turn')) is int and old['start_turn'] == progress.get('turn')
        and old.get('turn_limit') == old['start_turn'] + read.EXTRA_TURNS
        and 'normal_request' not in progress and 'finalization' not in progress
        and progress.get('mode') != 'final_only' and not progress.get('pending_tool_calls')
        and 'implementation_author_response_correction' not in progress,
        'legacy seal, first unsent turn, or bounded read intent is not intact')
    probe = copy.deepcopy(progress)
    probe.pop(read.KEY)
    scope, record, checked_context = read._scope(engine, run_id, 'engineer', assignment['purpose'], hid, probe)
    _require(set(old.get('scope', {})) == {'input_sha256', 'context_sha256', 'draft_sha256',
        'configuration_sha256', 'workflow_sha256'} and all(scope[name] == old['scope'][name] for name in (
        'input_sha256', 'context_sha256', 'draft_sha256', 'configuration_sha256')),
        'original frozen input, context, saved draft or configuration differs')
    _require(old['document'] == record['row']['document'] and old['revision'] == record['revision']
        and old['spec_hash'] == record['row']['spec_hash'], 'saved document identity differs')

    # These exact frozen projections are available even though the legacy whole
    # workflow snapshot was not archived. Unknown scientific content is not
    # inferred from the old hash, and the original seal is retained below.
    previous = indexed.previous_round(state, record['revision'])
    projection = {name: previous[name] for name in ('index', 'spec_hash', 'document', 'review_votes')}
    baselines = [row for row in context.get('previous_specifications', [])
                 if row.get('index') == record['revision'] - 1]
    _require(len(baselines) == 1 and projection == {name: baselines[0].get(name) for name in projection},
             'prior document or three independent review votes differ from frozen baseline')
    rebuilt = indexed.author_context(state, record['revision'], checked_context)
    _require(rebuilt.get(replies.CONTRACT) == checked_context.get(replies.CONTRACT),
             'original objection identities or scientific response contract differs')
    indexed.validate_previous(engine, run_id, state, record['revision'])
    final = read._source(engine, run_id, hid, old)
    candidate = final['response']['json']
    try:
        validate_candidate(engine, run_id, 'engineer', hid, candidate, checked_context)
    except ResearchBlocked as error:
        _require(str(error).startswith(READ_PENDING), 'candidate fails a gate other than its exact pending reading')
    else:
        _require(False, 'candidate no longer requires the original reading recovery')
    missing = read._missing(engine, run_id, hid, candidate, record, checked_context)
    _require(missing is not None and missing == old.get('missing_ranges'),
             'pending evidence changed or includes another source')
    new = copy.deepcopy(old)
    new.update(scope_version=SCOPE_VERSION, scope=scope)
    new['legacy_scope_migration'] = {'version': 1, 'old_audit_sha256': old['sha256'],
        'old_workflow_sha256': old['scope']['workflow_sha256'],
        'frozen_previous_round_sha256': digest(projection),
        'frozen_response_contract_sha256': digest(checked_context[replies.CONTRACT]),
        'legacy_workflow_snapshot_available': False,
        'scientific_baseline_reauthenticated': True, 'scientific_approval': False}
    new['sha256'] = digest({k: v for k, v in new.items() if k != 'sha256'})
    probe = copy.deepcopy(progress)
    probe[read.KEY] = new
    h = Harness(engine, SimpleNamespace())
    _require(h._pending_author_read_continuation(run_id, 'engineer', assignment['purpose'], hid, probe),
             'new scope does not authenticate its original unsent successor')
    report = {'already_applied': False, 'applied': False, 'run_id': run_id, 'handoff_id': hid,
        'old_audit_sha256': old['sha256'], 'old_progress_sha256': expected_progress_sha256,
        'workflow_sha256': expected_workflow_sha256, 'new_scope_version': SCOPE_VERSION,
        'missing_chars': sum(row['limit'] for row in missing), 'turn_limit': new['turn_limit'],
        'original_paid_request_replayed': False, 'legacy_workflow_snapshot_available': False,
        'scientific_baseline_reauthenticated': True, 'scientific_approval': False}
    if not apply:
        return report
    from .execution_archive import save
    archive = save(store, run_id, 'engineer', hid + ':legacy-read-scope-upgrade:v2',
        {'original_audit': old, 'current_workflow_snapshot': workflow_snapshot(state),
         'frozen_previous_round_sha256': digest(projection),
         'frozen_response_contract_sha256': digest(checked_context[replies.CONTRACT])}, handoff_id=hid)
    _require(not archive.get('redacted'), 'snapshot contains content that cannot be faithfully archived')
    new['legacy_scope_migration'].update(archive_id=archive['id'], archive_sha256=archive['sha256'], applied_at=now())
    new['sha256'] = digest({k: v for k, v in new.items() if k != 'sha256'})
    with store._db() as db:
        if not db.in_transaction:
            db.execute('BEGIN IMMEDIATE')
        def current(collection, key):
            row = db.execute('SELECT data FROM records WHERE collection=? AND key=?', (collection, key)).fetchone()
            return json.loads(row['data']) if row else {}
        _require(digest(current('handoff_progress', hid)) == expected_progress_sha256
            and digest(current('codex_workflows', state['id'])) == expected_workflow_sha256
            and current('runs', run_id) == run and current('runtime', 'worker') == lease
            and current('handoffs', hid) == assignment and not current('decisions', hid)
            and current('handoff_inputs', hid) == frozen
            and current('implementation_document_drafts', hid) == record,
            'compare-and-swap inputs changed; archive retained, checkpoint not changed')
        pending = db.execute("SELECT 1 FROM records WHERE collection='tool_invocations' "
            "AND json_extract(data,'$.run_id')=? AND json_extract(data,'$.handoff_id')=? "
            "AND json_extract(data,'$.status') IN ('pending','running','unknown') LIMIT 1", (run_id, hid)).fetchone()
        _require(not pending, 'unresolved tool intent appeared; checkpoint not changed')
        progress[read.KEY] = new
        db.execute('UPDATE records SET data=?,updated_at=? WHERE collection=? AND key=?', (
            json.dumps(progress, ensure_ascii=False), now(), 'handoff_progress', hid))
    store.event(run_id, 'implementation.author_read_scope_migrated', 'Legacy author-read scope explicitly reauthenticated',
        {**report, 'applied': True, 'archive_id': archive['id'], 'archive_sha256': archive['sha256']})
    report.update(applied=True, archive_id=archive['id'], archive_sha256=archive['sha256'])
    return report
