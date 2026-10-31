"""Portable staged-file identity, also shipped to isolated dataset adapters."""
import os


def portable_stat(value):
    # Windows Python versions expose either a 32-bit volume serial or a wider
    # device value. Keep their common volume serial; inode/times/size are also
    # checked, and the host separately verifies full-file SHA256 before launch.
    device = value.st_dev & 0xffffffff if os.name == 'nt' else value.st_dev
    return {'size': value.st_size, 'mtime_ns': value.st_mtime_ns,
            'ctime_ns': value.st_ctime_ns, 'device': device, 'inode': value.st_ino}
