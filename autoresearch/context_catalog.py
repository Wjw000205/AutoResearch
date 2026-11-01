"""Task-owned, immutable catalog directories and bounded JSON selection reads."""
from pathlib import Path
import hashlib
import json

from .context_compiler import catalog_view, encoded, fingerprint, profile_for
from .context_artifacts import _object


def load(engine, run_id, artifact_id, expected):
    record = engine.store.get('artifacts', artifact_id)
    if (not isinstance(artifact_id, str) or not artifact_id.startswith(f'{run_id}:literature/index/')
            or not record or record.get('run_id') != run_id):
        raise ValueError('Catalog does not belong to this run')
    path = Path(record['path']).resolve()
    if not path.is_relative_to(engine.run_dir(run_id)):
        raise ValueError('Catalog path outside run directory')
    text = path.read_text(encoding='utf-8')
    sha = hashlib.sha256(text.encode()).hexdigest()
    if not isinstance(expected, str) or expected != sha:
        raise ValueError('Catalog content hash changed; no evidence returned')
    return json.loads(text, object_pairs_hook=_object), sha


def directory(engine, run_id, args):
    document, sha = load(engine, run_id, args['artifact_id'], args['expected_sha256'])
    view = catalog_view(document, args['artifact_id'], sha, str(args.get('query', '')),
                        profile_for({'_purpose': args.get('purpose', '')}))
    return {'catalog': view, 'directory_sha256': fingerprint(view),
            'provenance': 'local_catalog_projection', 'primary_source_reading': False, 'version': 1}


def read(engine, run_id, args):
    document, sha = load(engine, run_id, args.get('artifact_id'), args.get('expected_sha256'))
    pointer = args.get('path')
    if not isinstance(pointer, str) or not pointer.startswith('/'):
        raise ValueError('Evidence path must be a JSON pointer')
    parts = [part.replace('~1', '/').replace('~0', '~') for part in pointer[1:].split('/')]
    if parts[0] not in {'notes', 'papers', 'analysis', 'reading_gate', 'reading_plan', 'coverage', 'targeted_evidence'}:
        raise ValueError('Evidence path is outside the catalog reading scope')
    selected = document
    for part in parts:
        if isinstance(selected, list):
            if not part.isdecimal() or str(int(part)) != part or int(part) >= len(selected):
                raise ValueError('Evidence list index is invalid')
            selected = selected[int(part)]
        elif isinstance(selected, dict) and part in selected:
            selected = selected[part]
        else:
            raise ValueError('Evidence path does not exist')
    text = encoded(selected)
    offset, limit = args.get('offset', 0), args.get('limit', 4000)
    if type(offset) is not int or not 0 <= offset <= len(text) or type(limit) is not int or not 1 <= limit <= 6000:
        raise ValueError('Evidence offset/limit is invalid')
    end = min(len(text), offset+limit)
    return {'artifact_id': args['artifact_id'], 'source_sha256': sha, 'json_pointer': pointer,
            'selection_sha256': hashlib.sha256(text.encode()).hexdigest(), 'text': text[offset:end],
            'offset': offset, 'total_chars': len(text), 'next_offset': end if end < len(text) else None,
            'truncated': end < len(text), 'untrusted': True, 'primary_source_reading': False,
            'provenance': 'saved_catalog_selection'}
