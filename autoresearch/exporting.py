"""Bounded, portable research exports with explicit omissions and hash semantics."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

from .storage import now, redact

COLLECTIONS = ['experiment_protocols', 'dataset_assets', 'dataset_downloads', 'context_evidence', 'paper_dossiers', 'paper_ocr', 'paper_visual', 'semantic_vectors',
               'context_receipts', 'context_commits', 'context_documents', 'context_manifests', 'evidence_deliveries',
               'execution_archives', 'experiment_evidence', 'experiment_notes', 'experiment_cleanups', 'performance_policies', 'performance_policy_archives', 'experiment_decisions', 'experiment_decision_sessions', 'experiment_sources', 'experiment_analyses', 'rapid_experiment_revisions', 'experiment_memories', 'rapid_continuations', 'rapid_continuation_archives', 'tool_invocations', 'tool_tasks', 'training_adjustments', 'training_review_recoveries',
               'innovation_sessions', 'steps', 'events', 'experiments', 'experiment_attempts', 'usage', 'codex_usage', 'artifacts', 'consensus',
               'decisions', 'selections', 'tuning_plans', 'programs', 'stage_contexts', 'context_snapshots',
               'transcripts', 'handoffs', 'handoff_inputs', 'handoff_progress', 'operator_resumes', 'run_evidence', 'literature_inputs',
               'research_cycles', 'cycle_steps', 'revision_plans', 'graph_status', 'experiment_restarts', 'debate_inputs', 'proposal_reviews', 'experiment_plans', 'implementation_routing_recoveries',
               'moderation_reviews', 'request_audits', 'memory_documents', 'memory', 'literature_notes', 'pdf_citations', 'codex_workflows', 'codex_checks', 'codex_check_attempts', 'architecture_reviews', 'architecture_revisions', 'architecture_definitions']
SUFFIXES = {'.py', '.json', '.jsonl', '.ndjson', '.md', '.txt', '.csv', '.tsv', '.arff', '.h5', '.hdf5', '.mat', '.log', '.npy', '.npz', '.pt', '.pth', '.bin', '.parquet',
            '.png', '.jpg', '.jpeg', '.tif', '.tiff', '.webp', '.gz', '.zip', '.yaml', '.yml', '.toml', '.pdf'}


def build_export(store, data_dir, run, *, secrets=(), max_file_bytes=20 * 1024**2, max_total_bytes=150 * 1024**2):
    run_id = run['id']
    root = Path(data_dir).resolve()
    buffer, written, hashes, omitted = io.BytesIO(), set(), {}, []
    total = 0
    secrets = [value.encode() if isinstance(value, str) else value for value in secrets if value]
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        def write(name, content, *, metadata=False):
            nonlocal total
            if metadata:
                for secret in secrets:
                    content = content.replace(secret, b'[redacted]')
            elif any(secret in content for secret in secrets):
                omitted.append({'path': name, 'reason': 'credential_content', 'required_for_reproduction': True})
                return False
            allowance = max_total_bytes if metadata else max_total_bytes - min(2 * 1024**2, max_total_bytes // 4)
            if total + len(content) > allowance:
                if metadata:
                    raise ValueError('Required export metadata exceeds the archive size limit')
                omitted.append({'path': name, 'reason': 'archive_size_limit', 'required_for_reproduction': True})
                return False
            archive.writestr(name, content)
            total += len(content)
            written.add(name)
            hashes[name] = hashlib.sha256(content).hexdigest()
            return True

        def json_file(name, value):
            write(name, json.dumps(redact(value), ensure_ascii=False, indent=2, allow_nan=False).encode(), metadata=True)

        execution_archives = store.list('execution_archives', run_id)
        for collection in COLLECTIONS:
            values = (execution_archives if collection == 'execution_archives' else
                      store.events(run_id, limit=5000) if collection == 'events' else store.list(collection, run_id))
            if collection == 'events':
                # The bounded timeline must still include every immutable
                # result referenced by the execution archive, even after 5000
                # events. Otherwise an apparently complete export loses raw
                # results that remain recoverable in the live database.
                required = {row['event_id'] for row in execution_archives if type(row.get('event_id')) is int}
                missing = sorted(required - {row['id'] for row in values})
                with store._read_db() as db:
                    for start in range(0, len(missing), 200):
                        batch = missing[start:start + 200]
                        rows = db.execute('SELECT * FROM events WHERE run_id=? AND id IN (' +
                            ','.join('?' for _ in batch) + ')', [run_id, *batch]).fetchall()
                        values.extend({**dict(row), 'data': json.loads(row['data'])} for row in rows)
                for event_id in required - {row['id'] for row in values}:
                    omitted.append({'path': f'records/events.json#id={event_id}',
                        'reason': 'execution_archive_event_missing', 'required_for_reproduction': True})
                values.sort(key=lambda row: row['id'])
            json_file(f'records/{collection}.json', values)
        json_file('run.json', run)
        for ref_id in run['config'].get('reference_ids', []):
            reference = store.get('references', ref_id)
            if reference:
                json_file(f'references/{ref_id}.json', reference)
        for directory, prefix in [(root / 'runs' / run_id, 'artifacts'), (root / 'experiments' / run_id, 'experiments')]:
            if not directory.exists():
                continue
            for path in sorted(directory.rglob('*')):
                name = prefix + '/' + path.relative_to(directory).as_posix()
                if path.name == '.start' or '__pycache__' in path.parts or path.suffix == '.pyc':
                    continue  # Runtime markers/bytecode are not research inputs.
                if path.is_symlink() or not path.resolve().is_relative_to(directory):
                    omitted.append({'path': name, 'reason': 'external_path_or_symlink', 'required_for_reproduction': True})
                    continue
                if not path.is_file():
                    continue
                reason = 'file_size_limit' if path.stat().st_size > max_file_bytes else ('unsupported_file_type' if path.suffix.lower() not in SUFFIXES or path.name.startswith('.env') else None)
                if reason:
                    omitted.append({'path': name, 'reason': reason, 'required_for_reproduction': True})
                    continue
                write(name, path.read_bytes())
        trials = {}
        for result in [*store.list('experiment_attempts', run_id), *store.list('experiments', run_id)]:
            trials[(result['id'], result.get('attempt', 1))] = result
        manifests = []
        runtime = {'experiment.py', 'params.json', 'environment.json', 'bootstrap.py', 'bootstrap.json', 'result.json'}
        for (trial_id, attempt), result in sorted(trials.items()):
            prefix = f'experiments/{trial_id}/attempt-{attempt:03d}/'
            code_path, params_path, environment_path = (prefix + name for name in ['experiment.py', 'params.json', 'environment.json'])
            missing = [item['path'] for item in omitted if item['path'].startswith(prefix)]
            missing += [name for name in [code_path, params_path, environment_path] if name not in written and name not in missing]
            params_hash = None
            if params_path in written:
                parameters = json.loads(archive.read(params_path))
                params_hash = hashlib.sha256(json.dumps(parameters, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
            dependencies = result.get('required_packages', result.get('limits', {}).get('required_packages'))
            manifests.append({'trial_id': trial_id, 'attempt': attempt, 'record_id': result.get('storage_key'),
                'code_revision': result.get('code_revision'), 'status': result.get('status'), 'superseded': result.get('superseded', False),
                'code_path': code_path, 'params_path': params_path, 'environment_path': environment_path if environment_path in written else None,
                'code_sha256': hashes.get(code_path), 'code_hash_format': 'sha256-bytes',
                'params_sha256': params_hash, 'params_hash_format': 'sha256-canonical-json-v1',
                'required_packages': dependencies or [], 'requirements_status': 'declared' if dependencies is not None else 'unknown',
                'input_paths': sorted(name for name in written if name.startswith(prefix) and name.rsplit('/', 1)[-1] not in runtime and not name.endswith('.log')),
                'omitted_inputs': missing})
        manifest = {'schema_version': 1, 'run_id': run_id, 'created_at': now(), 'input_inventory': 'attempt_directory_only',
                    'complete': not omitted and all(not trial['omitted_inputs'] for trial in manifests),
                    'trials': manifests, 'omitted_files': omitted,
                    'note': 'Completeness refers to the captured attempt directories, not undeclared external inputs or offline dataset availability. Generated code remains trusted local code.'}
        json_file('manifest.json', manifest)
    return buffer.getvalue()
