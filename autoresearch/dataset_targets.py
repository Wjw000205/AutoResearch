"""Task-owned dataset targets, from a catalog or a captured source/download."""
from __future__ import annotations

from .experiment_workflow import sha


def bind(engine, run_id, target, notes=None):
    if not isinstance(target, dict) or not {'note_id', 'dataset'} <= target.keys():
        raise ValueError('Dataset target requires note_id and dataset')
    notes = notes if notes is not None else {n['id']: n for n in engine.datasets._notes(run_id)}
    note = notes.get(target['note_id'])
    if not note or target['dataset'] not in note['datasets']:
        raise ValueError('Dataset target must match an intact reading note in this task')
    if set(target) == {'note_id', 'dataset', 'catalog_key'}:
        from .dataset_catalog import resolve
        catalog = resolve(target['dataset'], target['catalog_key'])
        return {'catalog_sha256': sha(catalog), 'source_note_sha256': sha(note)}
    if set(target) == {'note_id', 'dataset', 'asset_id', 'manifest_sha256'}:
        asset = engine.datasets.inspect(run_id, asset_id=target['asset_id'], expected_sha256=target['manifest_sha256'])
        origin = asset['manifest']['origin']
        if (origin.get('note_id') != note['id'] or origin.get('dataset') != target['dataset']
                or origin.get('note_sha256') != note.get('note_sha256')):
            raise ValueError('Dataset asset does not match its current source note')
        return {'source_note_sha256': sha(note), 'asset_id': target['asset_id'],
            'manifest_sha256': target['manifest_sha256']}
    from .tool_contracts import validate_arguments
    validate_arguments('datasets.download', target)
    if not target.get('url') or not target.get('filename') or target.get('catalog_key') is not None:
        raise ValueError('Source-bound target requires url, filename and its captured source chain')
    origin = engine.datasets._origin(run_id, target['note_id'], target['dataset'], target['url'], target.get('source_chain', []))
    return {'source_note_sha256': sha(note), 'download_sha256': sha(target), 'origin_sha256': sha(origin)}


async def acquire(engine, run_id, target):
    if 'asset_id' in target:
        import asyncio
        return await asyncio.to_thread(engine.datasets.inspect, run_id,
            asset_id=target['asset_id'], expected_sha256=target['manifest_sha256'])
    return await engine.datasets.download(run_id, **target)
