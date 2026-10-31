"""Bounded public dataset acquisition from task-owned, read paper evidence.

Downloaded bytes are untrusted data, never executed. A manifest proves the
captured files and their origin; it does not approve a scientific protocol.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
import threading
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin, urlsplit

import httpx

from .deletion import _plain_path, validate_tree
from .storage import RunDeletedError, now


MIB = 1024 * 1024
MAX_DOWNLOAD = 512 * MIB
MAX_EXTRACTED = 1024 * MIB
MAX_STORAGE = 2 * 1024 * MIB
DATA_SUFFIXES = {'.csv', '.tsv', '.txt', '.json', '.jsonl', '.ndjson', '.arff',
                 '.npy', '.npz', '.parquet', '.h5', '.hdf5', '.mat', '.zip', '.gz',
                 '.jpg', '.jpeg', '.png', '.webp', '.wav', '.flac', '.mp3', '.mp4'}
TOOLS = ('datasets.discover', 'datasets.inspect', 'datasets.download')
GUIDANCE = '''
公共数据准备：用 datasets.discover 从本任务已保存且来源校验有效的论文笔记定位数据集。
匹配目录条目时可使用 discover 返回的作者/基准维护者公开仓库链接下载。
只需传 note_id、dataset，歧义名称另传 discover 返回的 catalog_key。保存固定仓库版本，
内置版本不自动等于所读论文使用的版本，仍须比较数据说明与实验协议。
先读取论文/官方仓库的数据说明，沿 sources.links、sources.fetch 保留实际下载链接的来源链，
再 datasets.download 保存可直接公开获取的文件；需要登录、申请或点击确认的来源记录缺口。
不猜下载地址，不下载所有导航链接，不执行下载脚本，不把缓存/下载成功当作论文复现通过。
下载返回不可变 manifest_sha256、文件 SHA256 和 trial_path。批准设计中各方法共同冻结
dataset_assets=[{"asset_id":"返回的 asset_id","manifest_sha256":"返回的哈希"}]，
代码通过相对 trial_path 读取；宿主在每次实验前复核并复制文件到独立试验目录，Docker 同样可读。
数据下载不会启动实验或替代原有资源、预算及一致同意门禁。实际实验仍须按论文核对版本、
划分、预处理拟合范围、输入与目标规格及指标；manifest 不证明这些语义正确。
'''


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def file_hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _hash(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _bound(value, name, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f'{name} must be an integer in [1, {maximum}]')
    return value


def _filename(name, *, nested=False):
    if (not isinstance(name, str) or not name or len(name) > 240 or '\\' in name
            or any(ord(c) < 32 for c in name)):
        raise ValueError('Invalid dataset file name')
    parts = name.split('/')
    if (not nested and len(parts) != 1) or any(
        part in ('', '.', '..') or re.search(r'[<>:"|?*]', part) or part.endswith((' ', '.'))
        or re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part, re.I)
        for part in parts):
        raise ValueError('Dataset path must be a safe relative file name')
    if PurePosixPath(name).suffix.lower() not in DATA_SUFFIXES:
        raise ValueError('Unsupported dataset format; executable/code files are not dataset assets')
    return name


def _url(value):
    from .tool_contracts import _validate_url
    if not isinstance(value, str):
        raise ValueError('Dataset URL must be public HTTP(S)')
    _validate_url('datasets.download', value, '$.url')
    parsed = urlsplit(value.strip())
    if parsed.port not in (None, 80, 443):
        raise ValueError('Dataset URL must use port 80 or 443')
    return parsed._replace(fragment='').geturl()


def _links(document):
    # Reuse captured HTML links and text URLs. No guessed mirrors, API URLs or
    # guessed raw GitHub transformations are evidence of a download location.
    from .lab import LiteratureService
    candidates = list(document.get('source_links', []))
    candidates += LiteratureService._source_links(b'', document.get('url') or '',
                                                  'text/plain', document.get('text', ''))
    values = []
    for row in candidates:
        try:
            url = _url(row.get('url'))
        except (ValueError, AttributeError):
            continue
        if url not in values:
            values.append(url)
    return values


def bindings(params):
    """Exact immutable asset references, shared by planner and runner."""
    if 'dataset_assets' not in params:
        return []
    rows = params['dataset_assets']
    if not isinstance(rows, list) or not 1 <= len(rows) <= 16:
        raise ValueError('dataset_assets must contain 1..16 immutable asset bindings')
    seen = set()
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {'asset_id', 'manifest_sha256'}
                or not isinstance(row['asset_id'], str)
                or not re.fullmatch(r'[A-Za-z0-9]+:dataset:[0-9a-f]{32}', row['asset_id'])
                or not _hash(row['manifest_sha256']) or row['asset_id'] in seen):
            raise ValueError('Each dataset binding requires a distinct asset_id and exact manifest_sha256')
        seen.add(row['asset_id'])
    return rows


def checked_asset(store, data_dir, run_id, asset_id, expected=None):
    """Rehash the manifest and every retained file before reuse or staging."""
    root = Path(data_dir).absolute()
    if store.get('run_deletions', run_id):
        raise RunDeletedError('Research is being deleted or has been deleted')
    record = store.get('dataset_assets', asset_id)
    if not record or record.get('run_id') != run_id or record.get('status') != 'downloaded':
        raise ValueError('Downloaded dataset does not belong to this research')
    manifest = record['manifest']
    sha = digest(manifest)
    if sha != record['manifest_sha256'] or (expected is not None and sha != expected):
        raise ValueError('Dataset manifest SHA256 mismatch')
    key = asset_id.rsplit(':', 1)[-1]
    if not re.fullmatch(r'[0-9a-f]{32}', key) or asset_id != f'{run_id}:dataset:{key}':
        raise ValueError('Invalid dataset identity')
    directory = root / 'runs' / run_id / 'datasets' / key / sha
    for row in [manifest['download'], *manifest['files']]:
        relative = row['path']
        if (not isinstance(relative, str) or not relative.startswith(('original/', 'files/'))
                or type(row['bytes']) is not int or not 0 < row['bytes'] <= MAX_EXTRACTED):
            raise ValueError('Invalid dataset manifest file entry')
        _filename(relative.split('/', 1)[1], nested=True)
        path = directory / relative
        _plain_path(path, root)
        if not path.is_file() or path.stat().st_size != row['bytes'] or file_hash(path) != row['sha256']:
            raise ValueError('Dataset file is missing or its size/SHA256 changed')
    manifest_path = directory / 'manifest.json'
    _plain_path(manifest_path, root)
    if manifest_path.stat().st_size > 256 * 1024 or json.loads(manifest_path.read_text('utf-8')) != manifest:
        raise ValueError('Dataset manifest file changed')
    return record, directory


def stage_assets(store, data_dir, run_id, params, target, disk_bytes):
    """Copy only explicitly frozen assets into a trial; never alter params."""
    rows = bindings(params)
    if not rows:
        return []
    selected = [checked_asset(store, data_dir, run_id, row['asset_id'], row['manifest_sha256']) for row in rows]
    target = Path(target).absolute()
    validate_tree(target, target)
    existing = sum(path.stat().st_size for path in target.rglob('*') if path.is_file())
    needed = sum(item['bytes'] for record, _ in selected for item in record['manifest']['files'])
    if existing + needed > disk_bytes or needed > shutil.disk_usage(target).free:
        raise ValueError('Dataset staging exceeds the admitted trial disk limit')
    evidence = []
    for record, directory in selected:
        staged_files = []
        for item in record['manifest']['files']:
            path = target / item['trial_path']
            expected_path = f"datasets/{record['id'].rsplit(':', 1)[-1]}/{item['path']}"
            if item['trial_path'] != expected_path:
                raise ValueError('Dataset trial path does not match its manifest identity')
            _plain_path(path, target)
            if path.exists():
                raise ValueError('Dataset staging would overwrite a trial or code-bundle file')
            path.parent.mkdir(parents=True, exist_ok=True)
            with (directory / item['path']).open('rb') as source, path.open('xb') as output:
                shutil.copyfileobj(source, output, length=MIB)
            if path.stat().st_size != item['bytes'] or file_hash(path) != item['sha256']:
                raise ValueError('Dataset changed during staging')
            from .dataset_file_identity import portable_stat
            staged_files.append({**item, 'staged_stat': portable_stat(path.stat())})
        evidence.append({'asset_id': record['id'], 'manifest_sha256': record['manifest_sha256'],
                         'files': staged_files, 'provenance': record['provenance']})
    return evidence


class DatasetService:
    def __init__(self, engine):
        self.engine = engine
        self.store = engine.store
        self.root = Path(engine.data_dir).absolute()
        self._locks = {}

    def _require_run(self, run_id, *, active=False):
        self.engine.sources._require_run(run_id)
        if active:
            self.engine.check_active(run_id)

    def _notes(self, run_id):
        from .literature_notes import notes_view
        self._require_run(run_id)
        return notes_view(self.store, run_id, document_loader=self.engine.sources.document)

    def discover(self, run_id, *, paper_id=None, offset=0, limit=10):
        if type(offset) is not int or offset < 0:
            raise ValueError('Dataset offset must be a nonnegative integer')
        _bound(limit, 'limit', 20)
        rows = []
        for note in self._notes(run_id):
            if paper_id is not None and note['paper_id'] != paper_id:
                continue
            for name in note['datasets']:
                rows.append((note, name))
        selected = []
        for note, name in rows[offset:offset + limit]:
            from .dataset_catalog import candidates
            catalog = candidates(name)
            doc = self.engine.sources.document(run_id, note['source_id'])
            selected.append({'note_id': note['id'], 'paper_id': note['paper_id'], 'dataset': name,
                'source_id': note['source_id'], 'text_sha256': note['text_sha256'],
                'navigation_urls': _links(doc), 'missing_information': note['missing_information'],
                'catalog_candidates': [{**row, 'download_arguments': {'note_id': note['id'],
                    'dataset': name, 'catalog_key': row['key']}} for row in catalog],
                'status': 'catalog_available' if len(catalog) == 1 else 'select_dataset_variant' if catalog else 'requires_source_resolution',
                'scope': 'Paper-note dataset mention; links may be repository or landing pages, not downloadable data.'})
        return {'items': selected, 'total': len(rows), 'offset': offset,
                'next_offset': offset + limit if offset + limit < len(rows) else None,
                'scientific_claims_verified': False}

    def inspect(self, run_id, *, asset_id=None, expected_sha256=None, offset=0, limit=10):
        self._require_run(run_id)
        if type(offset) is not int or offset < 0:
            raise ValueError('Dataset offset must be a nonnegative integer')
        _bound(limit, 'limit', 20)
        if expected_sha256 is not None and (asset_id is None or not _hash(expected_sha256)):
            raise ValueError('Expected manifest hash requires an asset_id and lowercase SHA256')
        if asset_id:
            record, _ = checked_asset(self.store, self.root, run_id, asset_id, expected_sha256)
            self._require_run(run_id)
            return {**record, 'integrity_verified': True}
        records = self.store.list('dataset_assets', run_id)
        result = []
        for record in records[offset:offset + limit]:
            try:
                checked_asset(self.store, self.root, run_id, record['id'])
                result.append({**record, 'integrity_verified': True})
            except (OSError, ValueError, KeyError):
                result.append({'id': record['id'], 'asset_id': record['id'], 'status': 'invalid',
                               'integrity_verified': False})
        self._require_run(run_id)
        return {'items': result, 'total': len(records), 'offset': offset,
                'next_offset': offset + limit if offset + limit < len(records) else None}

    def _origin(self, run_id, note_id, dataset, url, chain, catalog=None):
        note = next((row for row in self._notes(run_id) if row['id'] == note_id), None)
        if not note or dataset not in note['datasets']:
            raise ValueError('Dataset requires an intact saved reading note and its exact dataset name')
        if catalog:
            from .dataset_catalog import VERSION
            return {'paper_id': note['paper_id'], 'note_id': note_id, 'note_sha256': note['note_sha256'],
                'dataset': dataset, 'sources': [{'source_id': note['source_id'], 'text_sha256': note['text_sha256']}],
                'association': 'read_paper_dataset_name_to_public_catalog',
                'catalog_version': VERSION, 'catalog': catalog,
                'paper_protocol_equivalence_verified': False}
        document = self.engine.sources.document(run_id, note['source_id'])
        evidence = [{'source_id': note['source_id'], 'text_sha256': note['text_sha256']}]
        if not isinstance(chain, list) or len(chain) > 4:
            raise ValueError('source_chain must contain at most four captured source snapshots')
        for link in chain:
            if (not isinstance(link, dict) or set(link) != {'source_id', 'expected_sha256'}
                    or not _hash(link['expected_sha256'])):
                raise ValueError('Each source_chain item requires source_id and expected_sha256')
            following = self.engine.sources.document(run_id, link['source_id'])
            if following['text_sha256'] != link['expected_sha256']:
                raise ValueError('Dataset source snapshot SHA256 mismatch')
            parent_links = _links(document)
            if not any(following.get(field) in parent_links for field in ('original_url', 'url')):
                raise ValueError('Dataset source chain is not linked from the paper/previous snapshot')
            evidence.append({'source_id': link['source_id'], 'text_sha256': link['expected_sha256']})
            document = following
        if url not in _links(document):
            raise ValueError('Download URL is not present in the bound source; fetch the linked dataset instructions first')
        return {'paper_id': note['paper_id'], 'note_id': note_id, 'note_sha256': note['note_sha256'],
                'dataset': dataset, 'sources': evidence, 'association': 'linked_from_read_paper',
                'official_ownership_verified': False}

    def _storage_limit(self, run_id):
        value = self.store.get('runs', run_id)['config'].get('disk_limit_gb')
        if value is None:
            return MAX_STORAGE
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError('Invalid configured dataset disk limit')
        return min(MAX_STORAGE, int(value * 1024**3))

    async def _fetch(self, run_id, url, path, maximum):
        current, total = url, 0
        literature = self.engine.literature
        async with httpx.AsyncClient(timeout=30, transport=literature.transport, trust_env=False,
                follow_redirects=False, headers={'User-Agent': 'AutoResearch/0.1 (public dataset retrieval)',
                                                'Accept-Encoding': 'identity'}) as client:
            for hop in range(6):
                self._require_run(run_id, active=True)
                addresses = await literature._validate_public_url(current)
                parsed = httpx.URL(current)
                async with client.stream('GET', parsed.copy_with(host=addresses[0]),
                        headers={'Host': parsed.netloc.decode('ascii')},
                        extensions={'sni_hostname': parsed.host}) as response:
                    if response.is_redirect:
                        if not response.headers.get('location'):
                            raise ValueError('Dataset redirect has no Location')
                        current = _url(urljoin(current, response.headers['location']))
                        continue
                    if response.status_code != 200:
                        raise ValueError(f'Dataset unavailable: HTTP {response.status_code}; no authentication or access bypass attempted')
                    content_type = response.headers.get('content-type', '').lower()
                    if any(t in content_type for t in ('text/html', 'application/xhtml', 'application/pdf')):
                        raise ValueError('Dataset URL returned a landing/login/document page, not data')
                    size = response.headers.get('content-length')
                    if size is not None and (not size.isdecimal() or int(size) > maximum):
                        raise ValueError('Dataset Content-Length exceeds download limit or is invalid')
                    sha, prefix = hashlib.sha256(), bytearray()
                    with path.open('xb') as output:
                        async for block in response.aiter_bytes(chunk_size=64 * 1024):
                            self._require_run(run_id, active=True)
                            total += len(block)
                            if total > maximum:
                                raise ValueError('Dataset exceeds streamed download limit')
                            if len(prefix) < 1024:
                                prefix.extend(block[:1024 - len(prefix)])
                            if re.search(rb'<(?:!doctype\s+html|html|head|body|form)\b', bytes(prefix), re.I):
                                raise ValueError('Dataset URL returned HTML/login content disguised as data')
                            output.write(block)
                            sha.update(block)
                        output.flush()
                        os.fsync(output.fileno())
                    if not total or (size is not None and response.headers.get('content-encoding', 'identity') == 'identity'
                                     and total != int(size)):
                        raise ValueError('Dataset response was empty or truncated')
                    return {'url': current, 'redirect_hops': hop, 'bytes': total, 'sha256': sha.hexdigest(),
                            'content_type': content_type, 'etag': response.headers.get('etag'),
                            'last_modified': response.headers.get('last-modified')}
        raise ValueError('Dataset redirect limit exceeded')

    async def _prepare_files(self, *args):
        # Keep the event loop responsive to pause/delete while copying large
        # files, and join the writer before the caller may remove its directory.
        stopped = threading.Event()
        work = asyncio.create_task(asyncio.to_thread(self._files, *args, stopped))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            stopped.set()
            while not work.done():
                try:
                    await asyncio.shield(work)
                except (asyncio.CancelledError, Exception):
                    pass
            try:
                work.result()
            except (asyncio.CancelledError, Exception):
                pass
            raise

    def _files(self, run_id, temporary, original, key, filename, members, maximum, stopped):
        output_rows = []
        def copy_file(source, name, expected_size):
            self._require_run(run_id, active=True)
            path = temporary / 'files' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            sha, size = hashlib.sha256(), 0
            with path.open('xb') as output:
                while block := source.read(MIB):
                    if stopped.is_set():
                        raise asyncio.CancelledError('Dataset preparation cancelled')
                    self._require_run(run_id, active=True)
                    size += len(block)
                    if size + sum(row['bytes'] for row in output_rows) > maximum:
                        raise ValueError('Dataset extraction exceeds byte limit')
                    output.write(block)
                    sha.update(block)
            if not size or size != expected_size:
                raise ValueError('Dataset member is empty or has an inconsistent size')
            output_rows.append({'path': 'files/' + name, 'bytes': size, 'sha256': sha.hexdigest(),
                                'trial_path': f'datasets/{key}/files/{name}'})

        if not members:
            with original.open('rb') as source:
                copy_file(source, filename, original.stat().st_size)
            return output_rows
        if not zipfile.is_zipfile(original):
            raise ValueError('extract_members requires a ZIP file')
        with zipfile.ZipFile(original) as archive:
            infos = archive.infolist()
            if len(infos) > 10000:
                raise ValueError('ZIP contains too many entries')
            selected = []
            for name in members:
                matches = [info for info in infos if info.filename == name]
                if len(matches) != 1:
                    raise ValueError('Requested ZIP member is missing or ambiguous')
                info = matches[0]
                mode = info.external_attr >> 16
                if (info.is_dir() or info.flag_bits & 1 or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
                        or info.file_size > maximum or info.file_size > max(info.compress_size, 1) * 200):
                    raise ValueError('ZIP member is encrypted, linked, special, or exceeds extraction limits')
                selected.append(info)
            if sum(info.file_size for info in selected) > maximum:
                raise ValueError('ZIP selected members exceed extraction limit')
            for info in selected:
                with archive.open(info) as source:
                    copy_file(source, info.filename, info.file_size)
        return output_rows

    async def download(self, run_id, *, note_id, dataset, url=None, filename=None, source_chain=None,
                       catalog_key=None, expected_content_sha256=None, max_bytes=None,
                       extract_members=None, max_extracted_bytes=256 * MIB):
        self._require_run(run_id, active=True)
        catalog = None
        if url is None and filename is None:
            from .dataset_catalog import resolve
            catalog = resolve(dataset, catalog_key)
            if source_chain or extract_members:
                raise ValueError('Catalog datasets are direct files and cannot use a custom source chain or ZIP members')
            url, filename = catalog['url'], catalog['filename']
            catalog_sha = catalog.get('expected_content_sha256')
            if catalog_sha and expected_content_sha256 not in (None, catalog_sha):
                raise ValueError('Expected checksum conflicts with the pinned dataset catalog')
            expected_content_sha256 = expected_content_sha256 or catalog_sha
        elif url is None or filename is None or catalog_key is not None:
            raise ValueError('Provide both url and filename for a source-bound download, or use a catalog dataset')
        if max_bytes is None:
            max_bytes = catalog['max_bytes'] if catalog else 64 * MIB
        url, filename = _url(url), _filename(filename)
        _bound(max_bytes, 'max_bytes', MAX_DOWNLOAD)
        _bound(max_extracted_bytes, 'max_extracted_bytes', MAX_EXTRACTED)
        members = [] if extract_members is None else extract_members
        if (not isinstance(members, list) or len(members) > 64
                or any(not isinstance(item, str) for item in members)
                or len({item.casefold() for item in members}) != len(members)):
            raise ValueError('extract_members must contain at most 64 distinct file paths')
        for member in members:
            _filename(member, nested=True)
        if members and Path(filename).suffix.lower() != '.zip':
            raise ValueError('Selected ZIP extraction requires a .zip filename')
        if expected_content_sha256 is not None and not _hash(expected_content_sha256):
            raise ValueError('Expected dataset content hash must be lowercase SHA256')
        origin = await asyncio.to_thread(self._origin, run_id, note_id, dataset, url, source_chain or [], catalog)
        identity = {'origin': origin, 'url': url, 'filename': filename, 'extract_members': members,
                    'expected_content_sha256': expected_content_sha256}
        key = digest(identity)[:32]
        asset_id = f'{run_id}:dataset:{key}'
        # Serialize acquisitions in a run so concurrent files share one quota.
        async with self._locks.setdefault(run_id, asyncio.Lock()):
            self._require_run(run_id, active=True)
            if self.store.get('dataset_assets', asset_id):
                result = await asyncio.to_thread(self.inspect, run_id, asset_id=asset_id)
                if result['manifest']['download']['bytes'] > max_bytes or sum(
                        row['bytes'] for row in result['manifest']['files']) > (max_extracted_bytes if members else max_bytes):
                    raise ValueError('Cached dataset exceeds requested limits')
                return {**result, 'cache_hit': True}
            root = self.root / 'runs' / run_id / 'datasets'
            _plain_path(root, self.root)
            root.mkdir(parents=True, exist_ok=True)
            validate_tree(root, self.root)
            used = sum(path.stat().st_size for path in root.rglob('*') if path.is_file())
            reserve = max_bytes + (max_extracted_bytes if members else max_bytes) + 256 * 1024
            if used + reserve > self._storage_limit(run_id) or reserve > shutil.disk_usage(root).free:
                raise ValueError('Dataset acquisition exceeds available/configured disk quota; request smaller explicit bounds')
            temporary = Path(tempfile.mkdtemp(prefix='.download-', dir=root))
            attempt_id = asset_id + ':' + temporary.name
            attempt = {'id': attempt_id, 'run_id': run_id, 'asset_id': asset_id,
                       'status': 'downloading', 'started_at': now(), 'request': identity}
            try:
                self.store.put('dataset_downloads', attempt_id, attempt)
                original = temporary / 'original' / filename
                original.parent.mkdir()
                async with asyncio.timeout(300):
                    fetched = await self._fetch(run_id, url, original, max_bytes)
                if expected_content_sha256 is not None and fetched['sha256'] != expected_content_sha256:
                    raise ValueError('Downloaded dataset checksum does not match expected_content_sha256')
                if catalog and catalog.get('expected_bytes') is not None and fetched['bytes'] != catalog['expected_bytes']:
                    raise ValueError('Downloaded dataset size differs from the pinned catalog version')
                files = await self._prepare_files(run_id, temporary, original, key, filename, members,
                                                  max_extracted_bytes if members else max_bytes)
                provenance = 'test_fixture' if self.engine.literature.transport is not None else 'live_public_download'
                manifest = {'version': 1, 'asset_id': asset_id, 'origin': origin,
                    'requested_url': url, 'download': {**fetched, 'path': 'original/' + filename},
                    'files': files, 'provenance': provenance,
                    'expected_checksum_verified': expected_content_sha256 is not None,
                    'scientific_claims_verified': False}
                sha = digest(manifest)
                (temporary / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
                # Recheck paper evidence before committing a long acquisition.
                if await asyncio.to_thread(self._origin, run_id, note_id, dataset, url, source_chain or [], catalog) != origin:
                    raise ValueError('Paper reading evidence changed during dataset download')
                self._require_run(run_id, active=True)
                target = root / key / sha
                _plain_path(target, self.root)
                target.parent.mkdir(exist_ok=True)
                if target.exists():
                    # An interrupted previous commit may have left the identical
                    # immutable files. Verify them below before registering.
                    validate_tree(target, self.root)
                    if (target / 'manifest.json').read_bytes() != (temporary / 'manifest.json').read_bytes():
                        raise ValueError('Dataset immutable directory already contains different content')
                    for row in [manifest['download'], *files]:
                        if file_hash(target / row['path']) != row['sha256']:
                            raise ValueError('Interrupted dataset files failed hash verification')
                else:
                    temporary.rename(target)
                record = {'id': asset_id, 'asset_id': asset_id, 'run_id': run_id,
                    'status': 'downloaded', 'created_at': now(), 'manifest_sha256': sha,
                    'manifest': manifest, 'provenance': provenance, 'scientific_claims_verified': False}
                def commit(old):
                    if old and old['manifest_sha256'] != sha:
                        raise ValueError('Concurrent dataset download produced a different immutable version')
                    return old or record
                record = self.store.mutate('dataset_assets', asset_id, commit)
                self.store.put('dataset_downloads', attempt_id,
                    {**attempt, 'status': 'downloaded', 'finished_at': now(), 'manifest_sha256': sha})
                return {**record, 'cache_hit': False, 'integrity_verified': True}
            except BaseException as exc:
                if not self.store.get('run_deletions', run_id):
                    self.store.put('dataset_downloads', attempt_id, {**attempt,
                        'status': 'interrupted' if isinstance(exc, asyncio.CancelledError) else 'failed',
                        'finished_at': now(), 'error_type': type(exc).__name__})
                raise
            finally:
                if temporary.exists():
                    validate_tree(temporary, self.root)
                    shutil.rmtree(temporary)
