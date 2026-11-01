"""Source-bound host CPU capability observations, never research approval."""
import copy
import hashlib
from pathlib import Path

from .experiment_workflow import sha
from .storage import now

COLLECTION = 'tuning_runtime_audits'


def register(engine, run_id, report, script):
    source = engine.store.get('experiment_sources', report['source_ref']['id']) or {}
    if source.get('run_id') != run_id or sha(source.get('files')) != report['source_ref']['sha256']:
        raise ValueError('Runtime capability audit has no matching frozen source')
    text = Path(script).read_text(encoding='utf-8')
    if hashlib.sha256(Path(script).read_bytes()).hexdigest() != report['probe_source_sha256']:
        raise ValueError('Runtime capability probe script changed')
    body = {'binding': report['binding'], 'source_ref': report['source_ref'], 'report': copy.deepcopy(report),
            'probe_source': text, 'runtime_route_paths': ['experiment.py', 'dataset_runtime/adapter.py',
                'dataset_runtime/contracts.py', 'dataset_runtime/training.py'],
            'provenance': 'host_isolated_cpu_capability_probe', 'execution_authorized': False,
            'scientific_approval': False}
    if not set(body['runtime_route_paths']) <= set(source['files']):
        raise ValueError('Runtime audit route is not in the executed source archive')
    identifier = run_id + ':runtime-capability:' + sha(body)
    record = {'id': identifier, 'run_id': run_id, 'body': body, 'sha256': sha(body), 'created_at': now()}
    engine.store.mutate(COLLECTION, identifier, lambda old: old or record)
    return record


def available(engine, run_id, evidence):
    rows = [r for r in engine.store.list(COLLECTION, run_id)
            if r['body']['binding'] == evidence['binding'] and r['body']['source_ref'] == evidence['code_sources']]
    if not rows:
        return None
    record = max(rows, key=lambda r: r['created_at'])
    if sha(record['body']) != record['sha256']:
        raise ValueError('Saved runtime capability evidence changed')
    return {'id': record['id'], 'sha256': record['sha256'], **copy.deepcopy(record['body'])}


def reading_progress(rows):
    """Small coverage ledger so loss of a raw window is not loss of a read."""
    paths = {}
    for row in rows:
        call, result = row.get('call', {}), row.get('result', {})
        if call.get('name') not in {'experiments.compare', 'experiments.inspect_module'}:
            continue
        if call['name'].endswith('inspect_module') and not result.get('source_read'):
            continue
        path = call.get('arguments', {}).get('path')
        if not path or any(type(result.get(k)) is not int for k in ('offset', 'end', 'total_chars')):
            continue
        paths.setdefault((call['name'], path, result.get('content_sha256', result.get('source_sha256'))), []).append(result)
    answer = []
    for (tool, path, identity), pages in paths.items():
        end = 0
        for page in sorted(pages, key=lambda p: p['offset']):
            if page['offset'] > end:
                break
            end = max(end, page['end'])
        answer.append({'tool': tool, 'path': path, 'source_sha256': identity, 'read_through': end,
                       'total_chars': pages[0]['total_chars'], 'complete': end == pages[0]['total_chars'],
                       'read_receipts': list(dict.fromkeys(p['receipt_sha256'] for p in pages if p.get('receipt_sha256')))})
    return {'items': answer, 'scope': 'Recorded read coverage, not semantic understanding or approval. Archived complete reads remain valid.'}
