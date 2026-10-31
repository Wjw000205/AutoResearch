"""Stream immutable local deliveries; file size is not a model context limit."""
import codecs
import hashlib
import os
from pathlib import Path

CHUNK_BYTES = 1024 * 1024


def io_path(path):
    """Use Win32 extended paths only for I/O; saved/UI paths stay readable."""
    path = Path(path).absolute()
    value = str(path)
    if os.name == 'nt' and not value.startswith('\\\\?\\'):
        value = '\\\\?\\UNC\\' + value[2:] if value.startswith('\\\\') else '\\\\?\\' + value
    return Path(value)


def regular_files(directory, *, skip_git=True):
    """List regular files without traversing links, junctions, or Git internals."""
    directory = Path(directory).absolute()
    for part in (directory, *directory.parents):
        part = io_path(part)
        if part.is_symlink() or getattr(part, 'is_junction', lambda: False)():
            raise ValueError('交付包含符号链接或目录联接')
    paths = []
    root = io_path(directory)
    def walk_error(error):
        raise error
    for parent, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        dirs[:] = [name for name in dirs if not (skip_git and name == '.git')]
        for name in [*dirs, *files]:
            if skip_git and name == '.git':
                continue
            path = Path(parent) / name
            if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
                raise ValueError('交付包含符号链接或目录联接')
            if not path.resolve().is_relative_to(root.resolve()):
                raise ValueError('交付路径逃逸')
            if name in files:
                if not path.is_file():
                    raise ValueError('交付包含非普通文件')
                paths.append(directory / path.relative_to(root))
    return sorted(paths, key=lambda path: path.relative_to(directory).as_posix())


def identity(path):
    digest, size = hashlib.sha256(), 0
    with io_path(path).open('rb') as source:
        for block in iter(lambda: source.read(CHUNK_BYTES), b''):
            digest.update(block)
            size += len(block)
    return {'sha256': digest.hexdigest(), 'bytes': size}


def copy_verified(source, target, expected):
    """Copy one file without retaining its body, and never overwrite a snapshot."""
    target = io_path(target)
    for parent in target.parents:
        if parent.is_symlink() or getattr(parent, 'is_junction', lambda: False)():
            raise ValueError('交付快照路径包含链接或目录联接')
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_file() or identity(target) != expected:
            raise ValueError('已冻结Codex交付快照发生变化，拒绝覆盖')
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    digest, size = hashlib.sha256(), 0
    with target.open('xb') as destination:
        try:
            with io_path(source).open('rb') as incoming:
                for block in iter(lambda: incoming.read(CHUNK_BYTES), b''):
                    destination.write(block)
                    digest.update(block)
                    size += len(block)
            if {'sha256': digest.hexdigest(), 'bytes': size} != expected:
                raise ValueError('复制期间交付文件发生变化，拒绝冻结')
        except BaseException:
            destination.close()
            target.unlink()  # Only this call's newly created, incomplete file.
            raise


def read_page(path, binding, *, offset=0, limit=16000, expected_sha256=None):
    """Hash/decode incrementally, retaining only the requested Unicode window."""
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 16000:
        raise ValueError('Artifact offset must be nonnegative and limit from 1 to 16000')
    if expected_sha256 is not None and expected_sha256.lower() != binding.get('sha256'):
        raise ValueError('Artifact content changed: SHA256 does not match expected_sha256; no page returned')
    decoder, digest = codecs.getincrementaldecoder('utf-8')(), hashlib.sha256()
    parts, total, size = [], 0, 0

    def take(text):
        nonlocal total
        start, end = max(offset - total, 0), min(offset + limit - total, len(text))
        if end > start:
            parts.append(text[start:end])
        total += len(text)

    try:
        with io_path(path).open('rb') as source:
            for block in iter(lambda: source.read(CHUNK_BYTES), b''):
                digest.update(block)
                size += len(block)
                take(decoder.decode(block))
            take(decoder.decode(b'', final=True))
    except UnicodeError as exc:
        raise ValueError('Artifact is binary, not UTF-8 text; use its directory metadata and related text reports') from exc
    if digest.hexdigest() != binding.get('sha256') or type(binding.get('bytes')) is not int or size != binding['bytes']:
        raise ValueError('Frozen delivery artifact content or SHA256 changed; no page returned')
    if offset > total:
        raise ValueError('Artifact offset exceeds total_chars')
    end = min(offset + limit, total)
    return {'text': ''.join(parts), 'offset': offset, 'total_chars': total,
            'next_offset': end if end < total else None, 'truncated': end < total, 'sha256': digest.hexdigest()}
