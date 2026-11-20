"""Sample live directories without racing ordinary temporary-file cleanup."""
import stat
import os
import time
from contextlib import contextmanager, ExitStack
from pathlib import Path


def _linked(info):
    return (stat.S_ISLNK(info.st_mode) or
            bool(getattr(info, 'st_file_attributes', 0) & 0x400))


@contextmanager
def _directory(path, *, parent_fd=None):
    """Pin each ancestor; never follow a reparse replacement during enumeration."""
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            yield fd
        finally:
            os.close(fd)
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    # OPEN_REPARSE_POINT prevents following the final component. Excluding
    # FILE_SHARE_DELETE pins its name while scandir uses it. Parent pins remain
    # open throughout recursion. A competing deletion retries the whole sample.
    handle = kernel.CreateFileW(str(path), 0x80, 0x1 | 0x2, None, 3, 0x02000000 | 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if _linked(Path(path).lstat()):
            yield None
        else:
            yield path
    finally:
        kernel.CloseHandle(handle)


def _sample(directories):
    def walk(path, parent_fd=None, *, root=False):
        try:
            with _directory(path, parent_fd=parent_fd) as pinned:
                if pinned is None:
                    if root:
                        raise ValueError('Disk accounting root cannot be a reparse point')
                    return 0
                total = 0
                with os.scandir(pinned) as entries:
                    for entry in entries:
                        try:
                            info = entry.stat(follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        if _linked(info):
                            continue
                        if stat.S_ISREG(info.st_mode):
                            total += info.st_size
                        elif stat.S_ISDIR(info.st_mode):
                            total += walk(entry.path if os.name == 'nt' else entry.name,
                                          None if os.name == 'nt' else pinned)
                return total
        except FileNotFoundError:
            return 0  # Only vanished paths are omitted from a sample.
    total = 0
    for directory in directories:
        path = Path(directory).absolute()
        try:
            with ExitStack() as stack:
                pinned = None
                # Pin ancestors too, so a root hidden below a junction is rejected.
                for ancestor in reversed(path.parents):
                    pinned = stack.enter_context(_directory(
                        ancestor if os.name == 'nt' or pinned is None else ancestor.name,
                        parent_fd=None if os.name == 'nt' else pinned))
                    if pinned is None:
                        raise ValueError('Disk accounting root has a reparse ancestor')
                total += walk(path if os.name == 'nt' or pinned is None else path.name,
                              None if os.name == 'nt' else pinned, root=True)
        except FileNotFoundError:
            continue
    return total


def tree_bytes(*directories):
    # Never return an incomplete total after an I/O error. Restart from zero,
    # including every supplied root, on the observed Windows sharing race only.
    for attempt in range(3):
        try:
            return _sample(directories)
        except OSError as exc:
            if getattr(exc, 'winerror', None) != 32 or attempt == 2:
                raise
            time.sleep(.01 * (attempt + 1))
