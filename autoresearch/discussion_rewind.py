"""Rewind a rejected discussion tail to a verified completed revision."""
from __future__ import annotations

import argparse
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path
import re

from .deletion import _plain_path
from .delivery_files import io_path
from .storage import now, uid
from .task_restart import _file_manifest, _paths, owned_records

ROUND_PURPOSE = re.compile(r'(?:vote|revise|amendment|literature_reading:discussion|discussion_action:discussion):(\d+)')
ROUND_CONTEXTS = {'post-review-evidence', 'revision-evidence', 'constructive-revision'}
ROUND_EVENTS = {
    'agent.completed', 'agent.started', 'consensus.round', 'context.compiled',
    'context.preparation_started', 'context.preparation_completed', 'discussion.constraints_updated',
    'discussion.records_resolved', 'discussion.stalled', 'discussion.work_delivered',
    'discussion.work_started', 'handoff.completed', 'handoff.queued', 'memory.retrieval',
    'run.blocked', 'tool.completed', 'tool.started', 'tool.failed',
}


def _related_generation(run):
    return run.get('state', {}).get('related_work_restart', {}).get('generation')


def _revision(store, run_id, marker):
    from .engine import digest
    handoff_id, proposal_hash = marker['handoff_id'], marker['proposal_hash']
    handoff = store.get('handoffs', handoff_id, {})
    decision = store.get('decisions', handoff_id, {})
    proposal = decision.get('result')
    key = f'{run_id}:revision-result-evidence:{digest([handoff_id, proposal_hash])}'
    row = store.get('stage_contexts', key, {})
    evidence = row.get('evidence', {})
    binding = evidence.get('revision_delivery', {})
    if (handoff.get('run_id') != run_id or handoff.get('status') != 'completed'
            or handoff.get('to_role') != 'director' or handoff.get('stage') != 'debate'
            or handoff.get('purpose') != f'revise:{marker["first_round"] - 1}'
            or handoff.get('output_hash') != proposal_hash or decision.get('run_id') != run_id
            or not isinstance(proposal, dict) or digest(proposal) != proposal_hash
            or row.get('run_id') != run_id or binding.get('handoff_id') != handoff_id
            or binding.get('output_hash') != proposal_hash
            or evidence.get('snapshot_hash') != marker['snapshot_hash']
            or digest({k: v for k, v in evidence.items() if k != 'snapshot_hash'}) != marker['snapshot_hash']):
        raise ValueError('Rewind predecessor revision or evidence snapshot changed')
    return proposal, evidence


def resume_payload(engine, run, initial_proposal, consensus):
    from .engine import ResearchBlocked, digest
    marker = run.get('state', {}).get('discussion_rewind')
    if (not marker or marker['cycle'] != engine.cycle(run['id'])
            or marker['related_work_generation'] != _related_generation(run)):
        return None
    first_round = marker['first_round']
    history = consensus.get('history', [])[:first_round - 1]
    try:
        if (consensus.get('cycle') != marker['cycle'] or len(history) != first_round - 1
                or digest(history) != marker['history_sha256']
                or digest(initial_proposal) != history[0]['proposal_hash']):
            raise ValueError('Rewind discussion history or initial proposal changed')
        proposal, evidence = _revision(engine.store, run['id'], marker)
    except ValueError as exc:
        raise ResearchBlocked(str(exc)) from exc
    return {'round_number': first_round, 'proposal': proposal, 'history': history,
            'prior_hashes': sorted({r['proposal_hash'] for r in history}),
            'proposal_evidence_snapshot': evidence, 'discussion_generation': marker['generation']}


def rewind(engine, run_id, count, *, expected_proposal_hash, reason):
    """Run with the service stopped. Preserve billing and earlier research evidence."""
    from .engine import digest
    store = engine.store
    run = store.get('runs', run_id, {})
    worker = store.get('runtime', 'worker')
    consensus = store.get('consensus', run_id, {})
    cycle = engine.cycle(run_id) if run else None
    step = store.get('steps', f'{run_id}:debate', {})
    history = consensus.get('history', [])
    if (type(count) is not int or not 1 <= count < len(history) or not reason.strip()
            or run.get('status') != 'blocked' or run.get('stage') != 'debate'
            or (worker or {}).get('held') or step.get('status') != 'running' or step.get('cycle') != cycle
            or consensus.get('cycle') != cycle or consensus.get('round') != len(history)
            or consensus.get('proposal_hash') != expected_proposal_hash
            or any(r.get('cycle') != cycle or r.get('round') != i or r.get('unanimous') is not False
                   or digest(r.get('proposal')) != r.get('proposal_hash') for i, r in enumerate(history, 1))):
        raise ValueError('Expected an inactive blocked debate with a matching rejected history and predecessor')

    retained, removed = history[:-count], history[-count:]
    rounds = {r['round'] for r in removed}
    first = removed[0]
    prefix = f'{run_id}:{cycle}:'
    input_prefixes = tuple(f'{prefix}{n}:' for n in sorted(rounds))
    input_rows = [r for r in store.list('debate_inputs', run_id)
                  if r['id'].startswith(f'{prefix}{first["round"]}:{first["proposal_hash"]}')
                  and r.get('context', {}).get('proposal') == first['proposal']]
    if not input_rows:
        raise ValueError('Missing the exact first removed round input')
    boundary = min(input_rows, key=lambda r: r['created_at'])
    cutoff = boundary['created_at']
    evidence = boundary['context']['evidence']
    generation = uid()
    marker = {'generation': generation, 'cycle': cycle, 'first_round': first['round'],
              'related_work_generation': _related_generation(run), 'history_sha256': digest(retained),
              'handoff_id': evidence.get('revision_delivery', {}).get('handoff_id'),
              'proposal_hash': first['proposal_hash'], 'snapshot_hash': evidence.get('snapshot_hash')}
    _revision(store, run_id, marker)

    handoff_ids = set()
    for row in store.list('handoffs', run_id):
        if row.get('created_at', '') < cutoff:
            continue
        match = ROUND_PURPOSE.fullmatch(row.get('purpose', ''))
        if (row.get('stage') != 'debate' or not match or int(match[1]) not in rounds
                or row.get('status') not in {'completed', 'failed', 'interrupted'}):
            raise ValueError('Work after the rewind boundary has an unrelated or unresolved assignment')
        handoff_ids.add(row['id'])
    owned = owned_records(store, run_id, handoff_ids)
    snapshots = {r['key'] for r in owned if r['collection'] == 'context_snapshots'}
    if any(r['collection'] == 'handoffs' and r['key'] not in handoff_ids for r in owned):
        raise ValueError('Rewind intersects a retained handoff')
    receipt_ids = {rid for r in owned if r['collection'] == 'context_commits'
                   for rid in json.loads(r['data']).get('payload', {}).get('receipt_ids', [])}
    selected = {(r['collection'], r['key']): r for r in owned
                if r['collection'] not in {'context_receipts', 'context_documents'}}
    scoped = ('debate_inputs', 'proposal_reviews', 'stage_contexts', 'artifacts', 'context_commits',
              'context_receipts', 'contexts', 'execution_archives')
    retained_hashes = {r['proposal_hash'] for r in retained}
    artifact_names = {'blocked-results.json', 'blocked-report.md'} | {
        f'ideas/{r["proposal_hash"]}/idea-materials.md' for r in removed if r['proposal_hash'] not in retained_hashes}
    with store._read_db() as db:
        rows = [dict(row) for row in db.execute(
            f"SELECT collection,key,data,updated_at FROM records WHERE collection IN ({','.join('?' for _ in scoped)}) "
            "AND json_extract(data,'$.run_id')=?", (*scoped, run_id))]
        events = [dict(row) for row in db.execute(
            'SELECT * FROM events WHERE run_id=? AND created_at>=? ORDER BY id', (run_id, cutoff))
            if row['kind'] in ROUND_EVENTS]
    retained_paths, retained_events = set(), set()
    for row in rows:
        col, key = row['collection'], row['key']
        if (col, key) in selected:
            continue
        if col == 'context_commits':
            receipt_ids.difference_update(json.loads(row['data']).get('payload', {}).get('receipt_ids', []))
        if col == 'execution_archives':
            value = json.loads(row['data'])
            retained_events.add(value.get('event_id'))
            if value.get('path'):
                retained_paths.add(Path(value['path']).absolute())
    for row in rows:
        col, key = row['collection'], row['key']
        value = json.loads(row['data'])
        parts = key.removeprefix(run_id + ':').split(':')
        name, *scope = parts
        stage_context = col == 'stage_contexts' and (
            (name in ROUND_CONTEXTS and len(scope) >= 3 and scope[0] == str(cycle)
             and scope[1] in {str(n) for n in rounds})
            or (name == 'discussion-actions' and scope and scope[0] == str(cycle) and value.get('round') in rounds)
            or (name in {'discussion-action-recovery', 'platform-evidence-work-recovery'} and scope
                and scope[0] == str(cycle) and value.get('round') in rounds)
            or (name == 'revision-result-evidence' and
                value.get('evidence', {}).get('revision_delivery', {}).get('handoff_id') in handoff_ids))
        if (col == 'debate_inputs' and key.startswith(input_prefixes)
                or col == 'proposal_reviews' and value.get('cycle') == cycle and value.get('round') in rounds
                or stage_context
                or col == 'contexts' and value.get('snapshot_id') in snapshots
                or col == 'context_receipts' and key in receipt_ids and value.get('created_at', '') >= cutoff
                or col == 'artifacts' and value.get('name') in artifact_names and value.get('created_at', '') >= cutoff):
            selected[col, key] = row

    root = engine.data_dir.resolve()
    paths = [p for p in _paths(engine, run_id, selected.values()) if Path(p['path']) not in retained_paths]
    events = [e for e in events if e['id'] not in retained_events]
    run_root = root / 'runs' / run_id
    for (col, _), row in selected.items():
        if col != 'artifacts':
            continue
        value = json.loads(row['data'])
        path = run_root / value['name']
        if not path.is_relative_to(run_root) or path != Path(value['path']).absolute():
            raise ValueError('Discussion artifact is outside its owned path')
        files = _file_manifest(path, root)
        if len(files) != 1 or files[0]['path'] != '.':
            raise ValueError('Discussion artifact is not a file')
        # Engine.artifact hashes the input text before Windows newline conversion.
        raw = io_path(path).read_bytes()
        text_hash = hashlib.sha256(raw.replace(b'\r\n', b'\n')).hexdigest()
        if value['sha256'] not in {files[0]['sha256'], text_hash}:
            raise ValueError('Discussion artifact content changed')
        paths.append({'path': str(path), 'files': files})

    updated = {**run, 'status': 'paused', 'error': None, 'updated_at': now(),
               'state': {**run.get('state', {}), 'discussion_rewind': marker}}
    restored = {**retained[-1], 'id': run_id, 'run_id': run_id, 'history': retained}
    archive = root / 'diagnostics' / 'discussion-rewinds' / run_id / generation
    _plain_path(archive, root)
    archive.mkdir(parents=True)
    snapshot = archive / 'before.json.gz'
    before = {'run': run, 'consensus': consensus, 'records': list(selected.values()), 'events': events}
    with gzip.open(snapshot, 'wt', encoding='utf-8') as stream:
        json.dump(before, stream, ensure_ascii=False)
    with gzip.open(snapshot, 'rt', encoding='utf-8') as stream:
        if json.load(stream) != before:
            raise ValueError('Discussion rewind archive did not verify')
    receipt = {'id': generation, 'run_id': run_id, 'created_at': now(), 'reason': reason,
               **marker, 'removed_rounds': sorted(rounds), 'handoff_count': len(handoff_ids),
               'removed_records': dict(Counter(col for col, _ in selected)), 'removed_events': len(events),
               'removed_paths': paths, 'archive_path': str(snapshot),
               'archive_sha256': hashlib.sha256(snapshot.read_bytes()).hexdigest(), 'status': 'paused'}
    moved = []
    try:
        with store._db() as db:
            db.execute('BEGIN IMMEDIATE')
            store._guard_run(db, run_id)
            for col, key, original in [('runs', run_id, run), ('consensus', run_id, consensus),
                                       ('runtime', 'worker', worker), ('steps', step['id'], step)]:
                live = db.execute('SELECT data FROM records WHERE collection=? AND key=?', (col, key)).fetchone()
                if (json.loads(live['data']) if live else None) != original:
                    raise ValueError('Discussion rewind boundary changed before commit')
            for row in selected.values():
                deleted = db.execute('DELETE FROM records WHERE collection=? AND key=? AND data=? AND updated_at=?',
                                     (row['collection'], row['key'], row['data'], row['updated_at']))
                if deleted.rowcount != 1:
                    raise ValueError('Discussion rewind record changed before commit')
            db.executemany('DELETE FROM events WHERE run_id=? AND id=?', [(run_id, e['id']) for e in events])
            db.execute("DELETE FROM records WHERE collection='execution_intents' AND key=?", (run_id,))
            for col, key, value in [('runs', run_id, updated), ('consensus', run_id, restored),
                                    ('discussion_rewinds', generation, receipt)]:
                db.execute('INSERT OR REPLACE INTO records VALUES (?,?,?,?)',
                           (col, key, json.dumps(value, ensure_ascii=False, allow_nan=False), now()))
            for entry in paths:
                source = Path(entry['path'])
                if _file_manifest(source, root) != entry['files']:
                    raise ValueError('Discussion rewind file changed before commit')
                dest = archive / 'files' / source.relative_to(root)
                _plain_path(dest, root)
                io_path(dest.parent).mkdir(parents=True, exist_ok=True)
                io_path(source).rename(io_path(dest))
                moved.append((source, dest))
    except BaseException:
        for source, dest in reversed(moved):
            io_path(dest).rename(io_path(source))
        raise
    store.event(run_id, 'discussion.rewound', reason,
                {'first_round': first['round'], 'removed_rounds': sorted(rounds), 'receipt_id': generation})
    return receipt


def retire_superseded(engine, run_id, handoff_ids, *, reason):
    """Archive interrupted handoffs created from a superseded discussion cursor."""
    from .engine import Harness, digest
    store = engine.store
    ids = set(handoff_ids)
    run = store.get('runs', run_id, {})
    worker = store.get('runtime', 'worker')
    marker = run.get('state', {}).get('discussion_rewind') or {}
    if (not run_id.isalnum() or not ids or not reason.strip() or not marker
            or run.get('status') not in {'paused', 'blocked'} or (worker or {}).get('held')):
        raise ValueError('Superseded discussion cleanup requires an inactive rewound task')
    key = f'{run_id}:retired-discussion:{digest(sorted(ids))}'
    if existing := store.get('discussion_request_retirements', key):
        return existing
    rewind_receipt = store.get('discussion_rewinds', marker['generation'], {})
    root = engine.data_dir.resolve()
    if rewind_receipt.get('run_id') != run_id or not rewind_receipt.get('archive_path'):
        raise ValueError('Missing discussion rewind archive')
    rewind_archive = Path(rewind_receipt['archive_path']).absolute()
    _plain_path(rewind_archive, root)
    if hashlib.sha256(io_path(rewind_archive).read_bytes()).hexdigest() != rewind_receipt['archive_sha256']:
        raise ValueError('Discussion rewind archive changed')
    with gzip.open(io_path(rewind_archive), 'rt', encoding='utf-8') as stream:
        original = json.load(stream)['consensus']['history']
    removed_rounds = set(rewind_receipt['removed_rounds'])
    removed = {(r['round'], r['proposal_hash']) for r in original if r['round'] in removed_rounds}
    dispatch_windows, input_prefixes = {}, set()
    for handoff_id in ids:
        handoff = store.get('handoffs', handoff_id, {})
        frozen = store.get('handoff_inputs', handoff_id, {})
        context = frozen.get('context', {})
        match = ROUND_PURPOSE.fullmatch(handoff.get('purpose', ''))
        if (handoff.get('run_id') != run_id or handoff.get('stage') != 'debate'
                or handoff.get('status') != 'interrupted' or handoff.get('output_hash')
                or not match or (int(match[1]), context.get('proposal_hash')) not in removed
                or context.get('research_cycle') != marker['cycle']
                or context.get('discussion_generation') == marker['generation']
                or handoff.get('created_at', '') <= rewind_receipt['created_at']
                or frozen.get('run_id') != run_id or store.get('decisions', handoff_id)):
            raise ValueError('Handoff is not an interrupted assignment from the superseded cursor')
        assignment_key = Harness.decision_key(run_id, handoff['to_role'], handoff['purpose'],
            frozen['instruction'], context, frozen['schema'])
        if assignment_key != handoff_id:
            raise ValueError('Superseded handoff input identity changed')
        dispatch_windows.setdefault((handoff['purpose'], handoff['to_role']), []).append(
            (handoff['created_at'], handoff['updated_at']))
        input_prefixes.add(f'{run_id}:{marker["cycle"]}:{match[1]}:{context["proposal_hash"]}:')
    native_ids = set(store.record_keys('codex_reasoning', run_id,
        predicate="json_extract(data,'$.memory_capture_binding.handoff_id') IN (SELECT value FROM json_each(?))",
        parameters=(json.dumps(sorted(ids)),)))
    rows = owned_records(store, run_id, ids, cancelled_native_ids=native_ids)
    if any((r['collection'] == 'handoffs' and r['key'] not in ids)
           or r['collection'] in {'decisions', 'tool_invocations', 'context_receipts', 'context_documents'}
           for r in rows):
        raise ValueError('Superseded assignments intersect retained work or have downstream tool results')
    snapshots = {r['key'] for r in rows if r['collection'] == 'context_snapshots'}
    with store._read_db() as db:
        for record in db.execute("SELECT collection,key,data,updated_at FROM records "
                                 "WHERE collection IN ('debate_inputs','contexts') AND json_extract(data,'$.run_id')=?",
                                 (run_id,)):
            value = json.loads(record['data'])
            if (record['collection'] == 'debate_inputs' and record['key'].startswith(tuple(input_prefixes))
                    and value.get('created_at', '') > rewind_receipt['created_at']
                    and value.get('context', {}).get('discussion_generation') != marker['generation']
                    or record['collection'] == 'contexts' and value.get('snapshot_id') in snapshots):
                rows.append(dict(record))
        events = []
        for event in db.execute("SELECT * FROM events WHERE run_id=? AND created_at>? ORDER BY id",
                                (run_id, rewind_receipt['created_at'])):
            value = json.loads(event['data'])
            windows = dispatch_windows.get((event['message'], value.get('role')), [])
            if (value.get('handoff_id') in ids or value.get('snapshot_id') in snapshots
                    or event['kind'] == 'agent.started'
                    and any(start <= event['created_at'] <= stop for start, stop in windows)):
                events.append(dict(event))

    rows = list({(r['collection'], r['key']): r for r in rows}.values())
    paths = _paths(engine, run_id, rows)
    archive = root / 'diagnostics' / 'discussion-retirements' / run_id / uid()
    _plain_path(archive, root)
    archive.mkdir(parents=True)
    snapshot = archive / 'before.json.gz'
    original = {'records': rows, 'events': events}
    with gzip.open(snapshot, 'wt', encoding='utf-8') as stream:
        json.dump(original, stream, ensure_ascii=False)
    with gzip.open(snapshot, 'rt', encoding='utf-8') as stream:
        if json.load(stream) != original:
            raise ValueError('Superseded request archive did not verify')
    receipt = {'id': key, 'run_id': run_id, 'rewind_generation': marker['generation'],
               'created_at': now(), 'reason': reason, 'handoff_ids': sorted(ids),
               'removed_records': len(rows), 'removed_events': len(events), 'removed_paths': paths,
               'archive_path': str(snapshot), 'archive_sha256': hashlib.sha256(snapshot.read_bytes()).hexdigest()}
    moved = []

    def move():
        for entry in paths:
            source = Path(entry['path'])
            if _file_manifest(source, root) != entry['files']:
                raise ValueError('Superseded request file changed before commit')
            target = archive / 'files' / source.relative_to(root)
            _plain_path(target, root)
            io_path(target.parent).mkdir(parents=True, exist_ok=True)
            io_path(source).rename(io_path(target))
            moved.append((source, target))

    try:
        store.batch(puts=[('discussion_request_retirements', key, receipt)],
            deletes=[(r['collection'], r['key']) for r in rows], delete_events=events, before_commit=move,
            expected=[('runs', run_id, run), ('runtime', 'worker', worker),
                      *[(r['collection'], r['key'], json.loads(r['data'])) for r in rows]])
    except BaseException:
        for source, target in reversed(moved):
            io_path(target).rename(io_path(source))
        raise
    store.event(run_id, 'discussion.requests_retired', reason,
        {'receipt_id': key, 'handoff_count': len(ids), 'rewind_generation': marker['generation']})
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=Path(__file__).resolve().parent.parent / 'data')
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--rounds', type=int, required=True)
    parser.add_argument('--expected-proposal-hash', required=True)
    parser.add_argument('--reason', required=True)
    args = parser.parse_args()
    from .engine import Engine
    from .storage import Store
    data_dir = args.data_dir.resolve()
    try:
        receipt = rewind(Engine(Store(data_dir / 'research.sqlite3'), data_dir), args.run_id,
                         args.rounds, expected_proposal_hash=args.expected_proposal_hash, reason=args.reason)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps({k: v for k, v in receipt.items() if k != 'removed_paths'}, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
