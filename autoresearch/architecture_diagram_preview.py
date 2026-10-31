"""Execute the formal drawing tool against an isolated, read-only source snapshot.

No model is requested and no source run record/file is written. The old agent's
semantic graph is reused; approval is copied from its actual current review,
and all graph anchors are read again through real documents.read invocations.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from . import architecture_diagrams as diagrams
from .engine import Engine, Harness, digest
from .storage import Store, now, redact


def _snapshot(source_dir, run_id, destination):
    # Apply the same path constraint as Engine.run_dir before any snapshot write.
    if not isinstance(run_id, str) or not run_id.isalnum():
        raise ValueError('Invalid run id')
    source_dir, destination = Path(source_dir).resolve(), Path(destination).resolve()
    if destination == source_dir or destination.is_relative_to(source_dir):
        raise ValueError('Preview output must be separate from the source data directory.')
    database = (source_dir / 'research.sqlite3').as_uri() + '?mode=ro'
    with closing(sqlite3.connect(database, uri=True)) as connection:
        connection.execute('BEGIN')
        def get(collection, key):
            row = connection.execute('SELECT data FROM records WHERE collection=? AND key=?',
                                     (collection, key)).fetchone()
            if not row:
                raise ValueError(f'Missing source {collection} record: {key}')
            return json.loads(row[0])
        run = get('runs', run_id)
        if run['mode'] != 'live':
            raise ValueError('The source graph must come from a live-provenance run.')
        cycle = run.get('state', {}).get('research_cycle', 1)
        reviews = [json.loads(row[0]) for row in connection.execute(
            "SELECT data FROM records WHERE collection='architecture_reviews' AND json_extract(data,'$.run_id')=?",
            (run_id,))]
        review = max((item for item in reviews if item.get('cycle') == cycle),
                     key=lambda item: item.get('created_at', ''), default=None)
        if not review or review.get('status') != 'approved':
            raise ValueError('The actual current architecture review is not approved.')
        original = get(diagrams.COLLECTION, diagrams._key(run_id, review['document']['sha256']))
        if (original.get('status') != 'completed'
                or original['architecture_document_id'] != review['document']['document_id']
                or digest(original['bundle']) != original['bundle_hash']):
            raise ValueError('The saved agent graph is incomplete or does not match the approved source.')
        documents = []
        for directory in original['context']['indexed_documents']:
            manifest = get('memory_documents', directory['document_id'])
            artifact = get('artifacts', manifest['artifact_id'])
            source_path = Path(artifact['path']).resolve()
            if (manifest.get('run_id') != run_id or artifact.get('run_id') != run_id
                    or manifest['text_sha256'] != directory['sha256']
                    or not source_path.is_relative_to(source_dir / 'runs' / run_id)):
                raise ValueError('Source document ownership/version is inconsistent.')
            content = source_path.read_bytes()
            # Validate the bytes that will actually be copied, using the same
            # universal-newline text identity as document_index.artifact_text.
            text = content.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')
            text_sha256 = hashlib.sha256(text.encode('utf-8')).hexdigest()
            if (text_sha256 != artifact['sha256'] or text_sha256 != manifest['text_sha256']
                    or text_sha256 != directory['sha256']):
                raise ValueError('Source Markdown content SHA256 has changed.')
            documents.append((manifest, artifact, content))
    destination.mkdir(parents=True, exist_ok=False)
    store = Store(destination / 'research.sqlite3')
    # Only the isolated preview worker runs. The source run can remain paused.
    store.put('runs', run_id, {**redact(run), 'status': 'running',
        'preview_execution': {'source_status': run['status'], 'model_called': False}})
    store.put('architecture_reviews', review['id'], review)
    for manifest, artifact, content in documents:
        target = destination / 'runs' / run_id / 'source-documents' / (manifest['text_sha256'][:24] + '.md')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        store.put('artifacts', artifact['id'], {**artifact, 'path': str(target)})
        store.put('memory_documents', manifest['id'], manifest)
    provenance = {'kind': 'isolated_formal_tool_preview', 'source_run_id': run_id,
        'source_document_id': review['document']['document_id'],
        'source_document_sha256': review['document']['sha256'],
        'source_review_id': review['id'], 'source_review_hash': digest(review),
        'source_bundle_hash': original['bundle_hash'],
        'source_job_cycle': original['cycle'],
        'source_run_status': run['status'], 'isolated_worker_status': 'running',
        'graph_authoring': 'existing_agent_bundle_reused', 'model_called': False,
        'approval': 'copied_actual_current_review', 'source_data_written': False,
        'created_at': now()}
    (destination / 'origin.json').write_text(json.dumps(provenance, ensure_ascii=False, indent=2), encoding='utf-8')
    return store, original, provenance


async def preview(source_dir, run_id, destination):
    """Return the real drawing invocation/receipt and isolated artifact paths."""
    store, original, provenance = _snapshot(source_dir, run_id, destination)
    engine = Engine(store, Path(destination), intelligence_factory=None)
    engine.cancellations[run_id] = asyncio.Event()
    context = copy.deepcopy(original['context'])
    context.update(architecture_diagram_authoring_protocol=diagrams.AUTHORING_VERSION,
        research_cycle=engine.cycle(run_id), diagram_tool_arguments_schema=diagrams.schema(),
        preview_provenance=provenance)
    row = {key: original[key] for key in ('id', 'run_id', 'cycle', 'architecture_document_id', 'document_sha256')}
    row.update(status='generating', cycle=engine.cycle(run_id), attempt=1, context=context, created_at=now(), updated_at=now(),
        provenance='isolated_formal_tool_preview', authoring_instruction=diagrams.TOOL_INSTRUCTION,
        output_schema=diagrams.submission_schema())
    harness = Harness(engine, None)
    purpose = f'architecture_diagram_author:{row["document_sha256"]}:{row["attempt"]}'
    handoff = harness._handoff(run_id, 'engineer', purpose, row['authoring_instruction'], context, row['output_schema'])
    row['handoff_id'] = handoff['id']
    store.put(diagrams.COLLECTION, row['id'], row)
    prepared = harness.prepare('engineer', purpose, row['authoring_instruction'], context, row['output_schema'])
    progress = {'id': handoff['id'], 'run_id': run_id, 'turn': 0, 'tool_results': [],
                'tool_request': {'turn': 0, 'prepared': prepared, 'prepared_sha256': digest(prepared)}}
    store.put('handoff_progress', handoff['id'], progress)
    directories = {item['document_id']: item for item in context['indexed_documents']}
    spans = {}
    for graph in (original['bundle']['network'], original['bundle']['flowchart']):
        for item in [*graph['nodes'], *graph['paragraphs'], *graph.get('panels', [])]:
            for anchor in item['sources']:
                spans.setdefault((anchor['document_id'], anchor['section_id']), []).append(
                    (anchor['char_start'], anchor['char_end']))
    for (identifier, section_id), ranges in sorted(spans.items()):
        directory = directories[identifier]
        section = next(item for item in directory['sections'] if item['id'] == section_id)
        merged = []
        for start, end in sorted(ranges):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        for start, end in merged:
            while start < end:
                arguments = {'document_id': identifier, 'expected_sha256': directory['sha256'],
                    'section_id': section_id, 'offset': start - section['start'], 'limit': min(4000, end - start)}
                result = await harness.execute_tool(run_id, 'engineer', 'documents.read', arguments,
                                                    handoff_id=handoff['id'])
                progress['tool_results'].append({'call': {'name': 'documents.read', 'arguments': arguments}, 'result': result})
                store.put('handoff_progress', handoff['id'], progress)
                start = result['char_end']
    receipt = await harness.execute_tool(run_id, 'engineer', diagrams.TOOL, original['bundle'], handoff_id=handoff['id'])
    calls = [record for record in store.list('tool_invocations', run_id) if record['tool'] == diagrams.TOOL]
    report = {'provenance': provenance, 'tool': diagrams.TOOL, 'invocation': calls[-1], 'receipt': receipt}
    path = Path(destination) / 'tool-call.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-data-dir', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    options = parser.parse_args()
    result = asyncio.run(preview(options.source_data_dir, options.run_id, options.output_dir))
    print(json.dumps({'tool': result['tool'], 'status': result['invocation']['status'],
        'model_called': False, 'artifacts': result['receipt']['artifacts']}, ensure_ascii=True))
