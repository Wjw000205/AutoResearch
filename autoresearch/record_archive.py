"""Stream immutable SQLite record bytes to content-addressed compressed files.

Large legacy transport payloads must never become one Python/JSON aggregate.
Fingerprints and archives use at most one MiB of raw payload at a time.
"""
import gzip
import hashlib
import os
from pathlib import Path
import tempfile

CHUNK = 1024 * 1024


def fingerprint(store, collection, key):
    with store._read_db() as db:
        head = db.execute('SELECT rowid,updated_at FROM records WHERE collection=? AND key=?',
                          (collection, key)).fetchone()
        if not head:
            return None
        digest = hashlib.sha256()
        with db.blobopen('records', 'data', head[0], readonly=True) as body:
            size = len(body)
            while chunk := body.read(CHUNK):
                digest.update(chunk)
        return {'collection': collection, 'key': key, 'updated_at': head[1],
                'sha256': digest.hexdigest(), 'bytes': size, 'encoding': 'utf-8-json'}


def verify(path, expected_sha256, expected_bytes):
    digest, size = hashlib.sha256(), 0
    with gzip.open(path, 'rb') as stream:
        while chunk := stream.read(CHUNK):
            size += len(chunk)
            if size > expected_bytes:
                raise ValueError('Archived record exceeds its bound byte count')
            digest.update(chunk)
    if size != expected_bytes or digest.hexdigest() != expected_sha256:
        raise ValueError('Archived record identity changed')


def save(store, run_id, reference):
    if not reference['key'].startswith(run_id + ':') and reference['key'] != run_id:
        raise ValueError('Archive source belongs to another run')
    root = store.path.parent.resolve()
    path = root / 'runs' / run_id / 'record-archive' / (reference['sha256'] + '.json.gz')
    from .deletion import _plain_path
    _plain_path(path, root)
    with store._read_db() as db:
        head = db.execute('SELECT rowid,updated_at FROM records WHERE collection=? AND key=?',
                          (reference['collection'], reference['key'])).fetchone()
        if not head or head[1] != reference['updated_at']:
            raise ValueError('Archive source changed after preview')
        if path.exists():
            verify(path, reference['sha256'], reference['bytes'])
            return {**reference, 'path': str(path), 'compression': 'gzip'}
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, filename = tempfile.mkstemp(dir=path.parent, prefix='.record-', suffix='.tmp')
        temporary = Path(filename)
        try:
            digest = hashlib.sha256()
            with os.fdopen(descriptor, 'wb') as output, db.blobopen('records', 'data', head[0], readonly=True) as body:
                if len(body) != reference['bytes']:
                    raise ValueError('Archive source size changed after preview')
                with gzip.GzipFile(fileobj=output, mode='wb', compresslevel=1, mtime=0) as compressed:
                    while chunk := body.read(CHUNK):
                        digest.update(chunk)
                        compressed.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if digest.hexdigest() != reference['sha256']:
                raise ValueError('Archive source bytes changed after preview')
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return {**reference, 'path': str(path), 'compression': 'gzip'}
