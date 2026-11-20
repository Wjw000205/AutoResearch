"""Shared resolution of frozen task catalog receipts for any role or stage.

Navigation and archive selections are records, never primary-source reading.
Callers authenticate the task and its frozen prepared input before resolving.
"""
from __future__ import annotations

import hashlib


def catalog_reference(engine, run_id, identifier, prepared):
    from .context_catalog import read, directory
    artifact = engine.store.get('artifacts', identifier, {})
    if (artifact.get('id') != identifier or artifact.get('run_id') != run_id
            or artifact.get('kind') != 'literature_index'):
        return None
    for index, tool in enumerate(prepared.get('context', {}).get('tool_results', [])):
        call, result = tool.get('call', {}), tool.get('result')
        args = call.get('arguments', {})
        if (call.get('name') not in {'evidence.read', 'evidence.catalog'} or not isinstance(result, dict)
                or result.get('error') or args.get('artifact_id') != identifier
                or args.get('expected_sha256') != artifact.get('sha256')):
            continue
        try:
            expected = (directory if call['name'] == 'evidence.catalog' else read)(engine, run_id, args)
        except (ValueError, TypeError, KeyError, OSError):
            continue
        if expected != result:
            continue
        if call['name'] == 'evidence.catalog':
            return {'id': identifier, 'kind': 'task_catalog_directory',
                'sha256': artifact['sha256'], 'directory_sha256': expected['directory_sha256'],
                'primary_source_reading': False, 'tool_result_index': index,
                'scope': 'Exact returned catalog navigation only; claims about coverage or passages require their own bounded reads.'}
        return {'id': identifier, 'kind': 'task_catalog_selection',
            'sha256': expected['source_sha256'], 'selection_sha256': expected['selection_sha256'],
            'page_sha256': hashlib.sha256(expected['text'].encode()).hexdigest(),
            'json_pointer': expected['json_pointer'], 'offset': expected['offset'],
            'end': expected['offset'] + len(expected['text']), 'total_chars': expected['total_chars'],
            'primary_source_reading': False, 'tool_result_index': index}
    return None
