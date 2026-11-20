"""Explicit, archived restart of related work after a completed innovation stage."""
from __future__ import annotations

import copy
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import uuid

from .deletion import _plain_path
from .storage import now


def state(store, run_id):
    return (store.get('runs', run_id) or {}).get('state', {}).get('related_work_restart') or {}


def excluded_versions(store, run_id):
    return set(state(store, run_id).get('excluded_rag_versions', []))


def graph_entry(engine, run_id):
    restart = state(engine.store, run_id)
    if not restart:
        return None
    selected = engine.store.get('innovation_sessions', f'{run_id}:{engine.cycle(run_id)}') or {}
    if selected.get('proposal_hash') != restart.get('proposal_hash'):
        return None
    return restart['graph_thread_id'], 'related_work'


def restart(engine, run_id, *, expected_proposal_hash, reason):
    """Run only while the owning service is stopped and research is inactive.

    Prior paid requests and derived files move to an audit archive. Accounting,
    first-round evidence, innovation outputs and shared RAG snapshots survive.
    Old second-round RAG analyses are excluded only for this task's new attempt.
    """
    store = engine.store
    run = store.get('runs', run_id)
    if not run or run['status'] not in {'blocked', 'paused'}:
        raise ValueError('Related-work restart requires an inactive blocked or paused run')
    worker = store.get('runtime', 'worker') or {}
    if worker.get('held') and worker.get('run_id') == run_id:
        raise ValueError('Research worker must release its lease before reset')
    innovation = store.get('steps', f'{run_id}:innovation') or {}
    current = store.get('steps', f'{run_id}:related_work') or {}
    selected = store.get('innovation_sessions', f'{run_id}:{engine.cycle(run_id)}') or {}
    if (innovation.get('status') != 'completed' or selected.get('status') != 'completed'
            or selected.get('proposal_hash') != expected_proposal_hash
            or innovation.get('output', {}).get('proposal_hash') != expected_proposal_hash
            or not current.get('started_at')):
        raise ValueError('Completed innovation and exact selected proposal are required')
    if store.list('experiments', run_id) or (store.get('consensus', run_id) or {}).get('unanimous'):
        raise ValueError('Cannot reset related work after experiment execution or consensus approval')
    cutoff = current['started_at']
    generation = uuid.uuid4().hex
    archive = engine.data_dir / 'diagnostics' / 'related-work-reset' / run_id / generation
    _plain_path(archive, engine.data_dir)
    archive.mkdir(parents=True)
    with store._read_db() as db:
        rows = [dict(row) for row in db.execute(
            "SELECT collection,key,data,updated_at FROM records WHERE "
            "(json_extract(data,'$.run_id')=? OR key=? OR key LIKE ?)", (run_id, run_id, run_id + ':%'))]
        rag_versions = [json.loads(row[0]) for row in db.execute(
            "SELECT data FROM records WHERE collection='paper_rag_versions' "
            "AND json_extract(data,'$.origin_run_id')=? AND updated_at>=?", (run_id, cutoff))]
    first_papers = {}
    for row in rows:
        value = json.loads(row['data'])
        if row['collection'] == 'literature_inputs' and row['updated_at'] < cutoff:
            first_papers.update({p['id']: p for p in value.get('papers', []) if isinstance(p, dict) and p.get('id')})
    protected = {'runs', 'budgets', 'usage', 'codex_usage', 'operator_resumes', 'run_control_requests',
                 'execution_intents', 'runtime', 'related_work_restarts',
                 'innovation_sessions', 'innovation_attempts', 'innovation_readings'}
    deletes, restores, originals = [], [], []
    for row in rows:
        col, key = row['collection'], row['key']
        if col in protected:
            continue
        value = json.loads(row['data'])
        born = value.get('created_at') or value.get('searched_at') or row['updated_at']
        if col == 'run_evidence' and row['updated_at'] >= cutoff:
            paper_id = value.get('paper', {}).get('id')
            if paper_id in first_papers:
                old = copy.deepcopy(value)
                history = [h for h in old.get('reading_history', []) if h.get('recorded_at', '') < cutoff]
                old['paper'] = copy.deepcopy(first_papers[paper_id])
                for field in ('reading', 'reading_history', 'reading_recorded_at'):
                    old.pop(field, None)
                if history:
                    old.update(reading=history[-1]['result'], reading_history=history,
                               reading_recorded_at=history[-1]['recorded_at'])
                restores.append((col, key, old))
                originals.append(row)
                continue
        if col == 'steps' and key != f'{run_id}:related_work':
            continue
        if born >= cutoff or col in {'contexts', 'memory_trees'} or (col.startswith('context_') and row['updated_at'] >= cutoff):
            deletes.append((col, key))
            originals.append(row)
    # Restore the exact first-round source records rather than old second-round
    # histories, and preserve every replaced/deleted JSON record in the archive.
    originals.extend(row for row in rows if row['collection'] in {'runs', 'execution_intents'} and row['key'] == run_id)
    with gzip.open(archive / 'records-before.json.gz', 'wt', encoding='utf-8') as stream:
        json.dump(originals, stream, ensure_ascii=False)
    deleting = set(deletes)
    retained_paths = {json.loads(row['data']).get('path') for row in rows
                      if row['collection'] == 'artifacts' and (row['collection'], row['key']) not in deleting}
    files = []
    for row in originals:
        if row['collection'] != 'artifacts' or (row['collection'], row['key']) not in deleting:
            continue
        value = json.loads(row['data'])
        source = Path(value['path']).absolute()
        if str(source) in retained_paths or not source.is_relative_to(engine.run_dir(run_id)):
            continue
        _plain_path(source, engine.data_dir)
        if source.is_file():
            target = archive / 'files' / source.relative_to(engine.run_dir(run_id))
            _plain_path(target, engine.data_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            sha = hashlib.sha256(source.read_bytes()).hexdigest()
            if hashlib.sha256(target.read_bytes()).hexdigest() != sha:
                raise ValueError('Reset archive file copy failed integrity check')
            files.append({'source': str(source), 'archive': str(target), 'sha256': sha})
    restart_state = {'generation': generation, 'graph_thread_id': f'{run_id}:related-work-reset:{generation}',
        'proposal_hash': expected_proposal_hash, 'cutoff': cutoff, 'archive_path': str(archive),
        'excluded_rag_versions': sorted({r['id'] for r in rag_versions} | excluded_versions(store, run_id))}
    updated = {**run, 'status': 'paused', 'stage': 'related_work', 'error': None,
        'state': {**run.get('state', {}), 'related_work_restart': restart_state}, 'updated_at': now()}
    with store._db() as db:
        db.execute('BEGIN IMMEDIATE')
        live = json.loads(db.execute("SELECT data FROM records WHERE collection='runs' AND key=?", (run_id,)).fetchone()[0])
        if live != run:
            raise ValueError('Research changed during restart preparation; nothing reset')
        db.executemany('DELETE FROM records WHERE collection=? AND key=?', deletes)
        for col, key, value in [*restores, ('runs', run_id, updated)]:
            db.execute('INSERT OR REPLACE INTO records VALUES (?,?,?,?)', (col, key, json.dumps(value, ensure_ascii=False), now()))
        db.execute("DELETE FROM records WHERE collection='execution_intents' AND key=?", (run_id,))
    for item in files:
        source = Path(item['source'])
        _plain_path(source, engine.data_dir)
        if hashlib.sha256(source.read_bytes()).hexdigest() != item['sha256']:
            raise ValueError('Artifact changed after archival; reset remains paused')
        source.unlink()
    receipt = {'id': f'{run_id}:related-work-reset:{generation}', 'run_id': run_id,
        **restart_state, 'reason': reason, 'created_at': now(), 'deleted_record_count': len(deletes),
        'restored_first_round_records': len(restores), 'archived_files': files,
        'preserved_stages': ['intake', 'literature', 'innovation'], 'experiment_authorized': False}
    (archive / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    store.put('related_work_restarts', receipt['id'], receipt)
    store.event(run_id, 'related_work.reset', reason, {'generation': generation,
        'proposal_hash': expected_proposal_hash, 'archive_path': str(archive), 'deleted_records': len(deletes)})
    return receipt
