"""Immutable public-source snapshots scoped to one research task.

Retrieval establishes what was downloaded, never scientific truth or official
ownership. Downloaded code is treated as text and is never executed here.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from .deletion import _plain_path
from .lab import LiteratureService, SourceUnavailableError, source_text_issue
from .storage import RunDeletedError, Store, now


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class SourceEvidenceService:
    def __init__(self, store: Store, data_dir: str | Path, *, literature=None):
        self.store = store
        self.data_dir = Path(data_dir).absolute()
        if self.data_dir.resolve() != self.data_dir:
            raise ValueError('Source evidence data directory must not contain links')
        self.literature = literature or LiteratureService(store)
        self._fetch_locks: dict[tuple[str, str], asyncio.Lock] = {}

    def _require_run(self, run_id: str):
        if not isinstance(run_id, str) or not run_id.isascii() or not run_id.isalnum():
            raise ValueError('Invalid research identifier')
        if self.store.get('run_deletions', run_id):
            raise RunDeletedError('Research is being deleted or has been deleted')
        if not self.store.get('runs', run_id):
            raise ValueError('Research not found')

    @staticmethod
    def _url(url: str) -> str:
        if not isinstance(url, str) or not url.strip() or any(ord(c) < 32 for c in url):
            raise ValueError('Source URL must be nonempty public HTTP(S) text')
        value = url.strip()
        parsed = urlparse(value)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Source URL must be public HTTP(S), without credentials')
        return parsed._replace(fragment='').geturl()

    @staticmethod
    def _page_args(offset, limit, expected_sha256):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 16000:
            raise ValueError('Offset must be nonnegative and page limit between 1 and 16000 characters')
        if expected_sha256 is not None and (not isinstance(expected_sha256, str) or len(expected_sha256) != 64
                or any(c not in '0123456789abcdefABCDEF' for c in expected_sha256)):
            raise ValueError('Expected text SHA256 must contain 64 hexadecimal characters')
        if offset and expected_sha256 is None:
            raise ValueError('Subsequent pages require expected_sha256 from the first page')

    @staticmethod
    def _metadata(record):
        evidence = record['evidence']
        public = {key: value for key, value in evidence.items() if key not in {'pages', 'text'}}
        return {**public, 'id': record['id'], 'source_id': record['id'], 'artifact_id': record['id'],
                'run_id': record['run_id'], 'name': record['name'], 'kind': record['kind'],
                'created_at': record['created_at'], 'sha256': record['sha256'],
                'source_version': evidence.get('source_version', evidence.get('document_version',
                    evidence.get('rag_version_id', record['sha256']))),
                'evidence_state': 'stored',
                **{key: record[key] for key in ('superseded_by', 'invalidated_at', 'source_status') if key in record},
                'source_scope': 'this_research_only', 'untrusted': True, 'verified': False}

    def list(self, run_id: str) -> list[dict]:
        self._require_run(run_id)
        return [self._bound_metadata(record) for record in self.store.list('artifacts', run_id)
                if record.get('kind') == 'source_evidence']

    def _bound_metadata(self, record):
        public = self._metadata(record)
        from .paper_bindings import valid_binding, canonical_source
        bound = valid_binding(self.store, record['run_id'], record['id'])
        if bound:
            key = canonical_source(self.store, bound['rag_source_id'])
            public.update(canonical_paper_id=bound['canonical_paper_id'], paper_id=bound['paper_id'],
                          paper_binding_derived=True)
            if self.store.get('paper_rag_sources', key):
                public['canonical_source_id'] = 'rag-source:' + key
        return public

    def links(self, run_id, source_id, *, expected_sha256, expected_links_sha256=None, offset=0, limit=10):
        from .context_navigation import links_hash
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError('Source link offset must be nonnegative and limit between 1 and 20')
        document = self.document(run_id, source_id)
        if expected_sha256 != document['text_sha256']:
            raise ValueError('Source text hash changed; no navigation returned')
        links = document.get('source_links', [])
        sha = links_hash(links)
        if (expected_links_sha256 is not None and expected_links_sha256 != sha) or (offset and not expected_links_sha256):
            raise ValueError('Subsequent source links require the unchanged links hash')
        if offset > len(links):
            raise ValueError('Source link offset exceeds retained navigation')
        end = min(offset + limit, len(links))
        return {'source_id': source_id, 'text_sha256': expected_sha256, 'links_sha256': sha,
            'links': links[offset:end], 'offset': offset, 'total_links': len(links),
            'next_offset': end if end < len(links) else None, 'untrusted': True,
            'primary_source_reading': False, 'scope': 'Retained navigation only; targets have not been read or verified.'}

    def document(self, run_id: str, source_id: str) -> dict:
        """Scoped, hash-checked retained text and pages for local memory indexing."""
        self._require_run(run_id)
        if isinstance(source_id, str) and source_id.startswith('rag-source:'):
            from .paper_bindings import shared_alias
            alias = shared_alias(self.store, run_id, source_id)
            if not alias:
                raise ValueError('Shared source has no paper binding in this research')
            return {**self.document(run_id, alias), 'source_id': source_id,
                    'source_alias_id': alias, 'canonical_source_id': source_id}
        record = self.store.get('artifacts', source_id)
        if not record or record.get('run_id') != run_id or record.get('kind') != 'source_evidence':
            raise ValueError('Source evidence does not belong to this research')
        path = Path(record['path']).absolute()
        root = self.data_dir / 'runs' / run_id / 'sources'
        if not path.is_relative_to(root):
            raise ValueError('Source evidence path is outside this research')
        _plain_path(path, self.data_dir)
        # Stored extraction is bounded by the same ingestion primitive. Refuse
        # tampered huge files before loading or returning any text.
        if not path.is_file() or path.stat().st_size > 600000:
            raise ValueError('Source evidence file is missing or exceeds the extraction bound')
        text = path.read_bytes().decode('utf-8')
        if _sha(text) != record['sha256'] or _sha(text) != record['evidence']['text_sha256']:
            raise ValueError('Source evidence text SHA256 changed; the immutable snapshot is invalid')
        original_id = record['evidence'].get('original_artifact_id')
        if record['evidence'].get('ocr_performed') and not record['evidence'].get('rag_version_id'):
            parent_id = record['evidence'].get('parent_source_id')
            parent_record = self.store.get('artifacts', parent_id) if parent_id else None
            if (not parent_record or parent_record.get('run_id') != run_id
                    or parent_record.get('evidence', {}).get('format') != 'pdf'
                    or parent_record.get('evidence', {}).get('ocr_performed')):
                raise ValueError('OCR source has no task-owned original PDF parent')
            parent = self.document(run_id, parent_id)
            if (parent['text_sha256'] != record['evidence'].get('parent_text_sha256')
                    or parent['content_sha256'] != record['evidence'].get('content_sha256')):
                raise ValueError('OCR parent source hash changed')
            from .image_inputs import resolve
            resolve(self.store, run_id, {'_image_inputs': [record['evidence']['ocr_image']['image']]})
        if original_id:
            original = self.store.get('artifacts', original_id)
            if not original or original.get('run_id') != run_id or original.get('kind') != 'source_original':
                raise ValueError('Original source artifact is missing or belongs to another research')
            original_path = Path(original['path']).absolute()
            if not original_path.is_relative_to(root):
                raise ValueError('Original source path is outside this research')
            _plain_path(original_path, self.data_dir)
            if not original_path.is_file() or original_path.stat().st_size > 12 * 1024 * 1024:
                raise ValueError('Original source file is missing or exceeds the retrieval bound')
            original_hash = hashlib.sha256(original_path.read_bytes()).hexdigest()
            if original_hash != original['sha256'] or original_hash != record['evidence']['content_sha256']:
                raise ValueError('Original source content SHA256 changed')
        pages = record['evidence'].get('pages', [])
        for page in pages:
            start, end = page.get('start'), page.get('end')
            if (type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(text)
                    or text[start:end] != page.get('text') or _sha(page['text']) != page.get('text_sha256')):
                raise ValueError('Source page does not match the stored text snapshot')
        self._require_run(run_id)
        return {**self._bound_metadata(record), 'text': text, 'pages': pages}

    def index_document(self, run_id: str, source_id: str) -> dict:
        document = self.document(run_id, source_id)
        issue = source_text_issue(document)
        if issue:
            raise SourceUnavailableError(issue)
        return document

    def read(self, run_id: str, source_id: str, *, offset: int = 0, limit: int = 8000,
             expected_sha256: str | None = None) -> dict:
        self._page_args(offset, limit, expected_sha256)
        document = self.document(run_id, source_id)
        text = document.pop('text')
        document.pop('pages', None)
        if expected_sha256 is not None and expected_sha256.lower() != document['text_sha256']:
            raise ValueError('Expected text SHA256 does not match this source snapshot')
        if offset > len(text):
            raise ValueError('Offset exceeds the retained source text length')
        end = min(len(text), offset + limit)
        page = text[offset:end]
        if document.get('ocr_lines'):
            lines = document.pop('ocr_lines')
            matching = [line for line in lines if line['start'] < end and line['end'] > offset]
            document['ocr_line_preview'] = matching[:40]
            document['ocr_line_preview_truncated'] = len(matching) > 40
        return {**document, 'text': page, 'offset': offset, 'total_chars': len(text),
                'next_offset': end if end < len(text) else None, 'page_sha256': _sha(page),
                'page_truncated': end < len(text), 'evidence_state': 'retrieved'}

    async def fetch(self, run_id: str, url: str, *, limit: int = 8000, prefer_pdf: bool = False) -> dict:
        self._require_run(run_id)
        self._page_args(0, limit, None)
        url = self._url(url)
        async with self._fetch_locks.setdefault((run_id, url), asyncio.Lock()):
            self._require_run(run_id)
            candidates = sorted((item for item in self.list(run_id) if item.get('original_url') == url),
                                key=lambda item: (item.get('created_at', ''), item['source_id']), reverse=True)
            for existing in candidates:
                if prefer_pdf and existing.get('format') != 'pdf':
                    continue  # An old landing-page snapshot cannot shadow its PDF.
                try:
                    self.index_document(run_id, existing['source_id'])
                except SourceUnavailableError:
                    # Keep the immutable failed snapshot, but never let an old
                    # intermediary page permanently shadow a later real source.
                    continue
                return {**self.read(run_id, existing['source_id'], limit=limit), 'cache_hit': True}
            # This is the existing pinned-DNS, redirect-validated, bounded HTTP
            # reader. Disable all global reference/cache/identifier writes.
            document = await self.literature.ingest_url(url, persist=False, cache=False,
                link_metadata=False, allow_empty_pdf=True, include_content=True,
                **({'prefer_pdf': True} if prefer_pdf else {}))
            issue = source_text_issue(document)
            if issue:
                raise SourceUnavailableError(issue)
            source_id = self._persist(run_id, document, origin={'original_url': url})
            return {**self.read(run_id, source_id, limit=limit), 'cache_hit': False}

    def from_reference(self, run_id: str, reference_id: str, *, limit: int = 8000) -> dict:
        """Copy only a user-selected reference into this task's source scope."""
        self._require_run(run_id)
        self._page_args(0, limit, None)
        run = self.store.get('runs', run_id)
        if reference_id not in run.get('config', {}).get('reference_ids', []):
            raise ValueError('Reference is not selected for this research')
        existing = next((item for item in self.list(run_id) if item.get('source_reference_id') == reference_id), None)
        if existing:
            return {**self.read(run_id, existing['source_id'], limit=limit), 'cache_hit': True}
        document = self.store.get('references', reference_id)
        if not document or not isinstance(document.get('text'), str):
            raise ValueError('Selected reference has no extracted text snapshot')
        if document.get('original_path'):
            original_path = Path(document['original_path']).absolute()
            if not original_path.is_relative_to(self.data_dir / 'references'):
                raise ValueError('Selected reference original is outside the managed reference directory')
            _plain_path(original_path, self.data_dir)
            if not original_path.is_file() or original_path.stat().st_size > 12 * 1024 * 1024:
                raise ValueError('Selected reference original is missing or exceeds the retrieval bound')
            document['_content_bytes'] = original_path.read_bytes()
        source_id = self._persist(run_id, document, origin={
            'source_reference_id': reference_id, 'original_url': document.get('original_url', document.get('url'))})
        return {**self.read(run_id, source_id, limit=limit), 'cache_hit': False}

    def _persist(self, run_id: str, document: dict, *, origin: dict) -> str:
        self._require_run(run_id)
        text = document['text']
        if not isinstance(text, str) or len(text) > 150000:
            raise ValueError('Extracted source text exceeds the 150000 character bound')
        text_hash = _sha(text)
        content = document.get('_content_bytes')
        if content is not None and (not isinstance(content, bytes) or len(content) > 12 * 1024 * 1024
                or hashlib.sha256(content).hexdigest() != document.get('content_sha256')):
            raise ValueError('Original source bytes do not match the captured content SHA256 or size bound')
        identity = json.dumps([run_id, origin, document.get('url'), document.get('content_sha256'), text_hash],
                              sort_keys=True, ensure_ascii=False, separators=(',', ':'))
        source_key = _sha(identity)[:32]
        source_id = f'{run_id}:source:{source_key}'
        path = self.data_dir / 'runs' / run_id / 'sources' / f'{source_key}.txt'
        original_id = f'{run_id}:source-original:{source_key}' if content is not None else None
        evidence = {key: value for key, value in document.items() if key not in {'id', 'text', 'cache_hit', '_content_bytes', 'original_path'}}
        evidence.update(**origin, text_sha256=text_hash, fetched_at=document.get('checked_at'),
            untrusted=True, verified=False, source_links=document.get('source_links', []),
            original_artifact_id=original_id, original_bytes_available=content is not None,
            hash_note='content_sha256 covers original bytes when captured; text_sha256 covers retained extracted text',
            extraction={'status': document.get('extraction_status', 'legacy_text_snapshot'),
                        'pages_total': document.get('pages_total'), 'pages_extracted': document.get('pages_extracted'),
                        'unextracted_pages': max(0, (document.get('pages_total') or 0) - (document.get('pages_extracted') or 0)),
                        'empty_text_pages': document.get('empty_text_pages', []),
                        'ocr_performed': document.get('ocr_performed', False), 'retained_chars': len(text),
                        'text_truncated': document.get('text_truncated', False)})
        evidence.setdefault('url', None)
        evidence.setdefault('provenance', 'user_supplied_untrusted')
        evidence.setdefault('content_sha256', None)
        evidence.setdefault('text_truncated', False)
        record = {'id': source_id, 'run_id': run_id, 'name': f'sources/{source_key}.txt',
                  'path': str(path), 'kind': 'source_evidence', 'created_at': now(),
                  'sha256': text_hash, 'evidence': evidence}

        records = [(record, text.encode('utf-8'))]
        if content is not None:
            suffix = '.pdf' if document.get('format') == 'pdf' or content.startswith(b'%PDF-') else '.bin'
            original = {'id': original_id, 'run_id': run_id, 'name': f'sources/{source_key}{suffix}',
                        'path': str(path.with_suffix(suffix)), 'kind': 'source_original', 'created_at': record['created_at'],
                        'sha256': document['content_sha256'], 'source_id': source_id,
                        'untrusted': True, 'verified': False, 'provenance': evidence['provenance']}
            records.insert(0, (original, content))

        def write_file(item, body):
            path = Path(item['path'])
            _plain_path(path, self.data_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            _plain_path(path, self.data_dir)
            if path.exists():
                if hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
                    raise ValueError('Existing source snapshot differs; refusing replacement')
                return False
            else:
                descriptor, temporary_name = tempfile.mkstemp(prefix='.source-', suffix='.tmp', dir=path.parent)
                temporary = Path(temporary_name)
                try:
                    with os.fdopen(descriptor, 'wb') as output:
                        output.write(body)
                        output.flush()
                        os.fsync(output.fileno())
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
                return True

        # Deletion admission and this immutable filesystem commit share the
        # SQLite write transaction; deletion cannot race past a stale precheck.
        written = []
        with self.store._db() as db:
            db.execute('BEGIN IMMEDIATE')
            for item, _ in records:
                self.store._guard_write(db, 'artifacts', item['id'], item)
            try:
                for item, body in records:
                    row = db.execute("SELECT data FROM records WHERE collection='artifacts' AND key=?", (item['id'],)).fetchone()
                    existing = json.loads(row['data']) if row else None
                    if existing is not None:
                        if any(existing.get(field) != item.get(field) for field in ('run_id', 'kind', 'path', 'sha256')):
                            raise ValueError('Source evidence identity collision')
                        if item['kind'] == 'source_evidence' and any(existing.get('evidence', {}).get(k) != v for k, v in origin.items()):
                            raise ValueError('Source evidence provenance collision')
                    if write_file(item, body):
                        written.append(Path(item['path']))
                    if existing is None:
                        db.execute('INSERT INTO records VALUES (?,?,?,?)',
                            ('artifacts', item['id'], json.dumps(item, ensure_ascii=False, allow_nan=False), now()))
            except BaseException:
                for created in written:
                    _plain_path(created, self.data_dir)
                    created.unlink(missing_ok=True)
                raise
        return source_id
