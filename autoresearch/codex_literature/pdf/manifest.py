"""A small immutable PDF repository, backed by existing source receipts."""
import hashlib
import json

from ...storage import now


def build(engine, run_id, source_id):
    from ...pdf_tools import LocalPDF
    from ...deletion import _plain_path
    document, original = LocalPDF(engine).original(run_id, source_id)
    scope = hashlib.sha256(json.dumps([source_id, document['content_sha256'], document['text_sha256'], 1]).encode()).hexdigest()[:24]
    # Short directory names work with Windows' default path-length limit. Full
    # source/version hashes remain authoritative in the manifest and artifacts.
    prefix = f'literature/pdf/{scope}'
    pages = []

    def save(name, text, kind):
        path = engine.run_dir(run_id) / name
        _plain_path(path, engine.data_dir)
        body = text.encode('utf-8')
        sha = hashlib.sha256(body).hexdigest()
        key = f'{run_id}:{name}'
        item = {'id': key, 'run_id': run_id, 'name': name, 'path': str(path),
                'kind': kind, 'sha256': sha, 'created_at': now()}
        with engine.store._db() as db:
            db.execute('BEGIN IMMEDIATE')
            engine.store._guard_write(db, 'artifacts', key, item)
            engine.check_active(run_id)
            old = engine.store.get('artifacts', key)
            if old and (old.get('run_id') != run_id or old.get('sha256') != sha or old.get('kind') != kind):
                raise ValueError('PDF repository artifact identity changed')
            if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != sha:
                raise ValueError('PDF repository file changed; refusing to mix source versions')
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)  # Preserve LF and exact UTF-8 hashes on Windows.
            item = old or item
            db.execute('INSERT OR IGNORE INTO records VALUES (?,?,?,?)',
                       ('artifacts', key, json.dumps(item, ensure_ascii=False), now()))
        return item

    for page in document.get('pages', []):
        number = page.get('page_number', page.get('page'))
        filename = f'pages/{number:04d}.txt'
        artifact = save(prefix + '/' + filename, page['text'], 'literature_pdf_page')
        pages.append({'page': number, 'file': filename, 'artifact_id': artifact['id'],
            'text_sha256': page['text_sha256'], 'start': page['start'], 'end': page['end'],
            'empty': page.get('empty', False), 'truncated': page.get('text_truncated', False),
            'read': {'name': 'pdf.read', 'arguments': {'source_id': source_id, 'page': number,
                'expected_sha256': document['text_sha256']}},
            'image': {'name': 'pdf.render', 'arguments': {'source_id': source_id, 'page': number,
                'expected_sha256': document['text_sha256']}}})
    manifest = {'version': 1, 'source_id': source_id, 'title': document.get('title'),
        'content_sha256': document['content_sha256'], 'text_sha256': document['text_sha256'],
        'original': {'artifact_id': original['id'], 'path': original['path'], 'sha256': original['sha256']},
        'pages_total': document.get('pages_total'), 'pages': pages,
        'text_truncated': document.get('text_truncated', False), 'ocr_performed': document.get('ocr_performed', False),
        'provenance': 'immutable_local_pdf_repository', 'ai_read': False,
        'scope': 'Indexed text pages and original file only. Images are rendered on demand; use read receipts and exact citation handles for evidence.'}
    artifact = save(prefix + '/manifest.json', json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2),
                    'literature_pdf_manifest')
    return {**manifest, 'artifact_id': artifact['id'], 'manifest_path': artifact['path']}
