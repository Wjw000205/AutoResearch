"""Locate the installed native executable without the desktop app's PATH.

Discovery is fresh for each spawn: desktop updates replace version directories.
This module never starts a process, reads credentials, or changes global PATH.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil

OVERRIDE = 'AUTORESEARCH_CODEX_EXECUTABLE'


def _installed_windows(environ):
    # The detached Windows service doesn't inherit the desktop application's
    # PATH. Prefer its registered package over an obsolete extracted CLI.
    if os.name == 'nt' and environ.get('ProgramFiles'):
        import winreg
        registry = r'Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppModel\Repository\Packages'
        packages = []
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, registry) as key:
                for index in range(winreg.QueryInfoKey(key)[0]):
                    name = winreg.EnumKey(key, index)
                    match = re.fullmatch(r'OpenAI\.Codex_(\d+\.\d+\.\d+\.\d+)_[^_]+__2p2nqsd0c76g0', name)
                    if match:
                        packages.append((tuple(map(int, match[1].split('.'))), name))
        except OSError:
            pass
        for _, name in sorted(packages, reverse=True):
            yield Path(environ['ProgramFiles']) / 'WindowsApps' / name / 'app/resources/codex.exe', 'desktop_package'
    local = environ.get('LOCALAPPDATA')
    if local:
        directory = Path(local) / 'OpenAI/Codex/bin'
        try:
            candidates = list(directory.glob('*/codex.exe'))
        except OSError:
            candidates = []
        dated = []
        for candidate in candidates:
            try:
                dated.append((candidate.stat().st_mtime_ns, str(candidate), candidate))
            except OSError:
                continue  # An update may remove a directory during discovery.
        for _, _, candidate in sorted(dated, reverse=True):
            yield candidate, 'desktop_installation'
        yield directory / 'codex.exe', 'desktop_installation'
    home = environ.get('USERPROFILE')
    if home:
        yield Path(home) / '.local/bin/codex.exe', 'user_installation'


def resolve_codex(*, exclude=(), environ=None, platform=None):
    environment = os.environ if environ is None else environ
    windows = (os.name if platform is None else platform) == 'nt'
    excluded = {os.path.normcase(os.path.abspath(str(path))) for path in exclude}

    def result(path, source):
        try:
            path = Path(path).expanduser()
            if not path.is_absolute() or not path.is_file():
                return None
            resolved = str(path.resolve())
            if os.path.normcase(resolved) in excluded:
                return None
            return {'available': True, 'executable': resolved, 'source': source}
        except (OSError, ValueError):
            return None

    override = environment.get(OVERRIDE, '').strip().strip('"')
    if override:
        # An explicit pin is not permission to silently substitute another binary.
        return result(override, 'explicit_configuration') or {
            'available': False, 'error_code': 'codex_executable_invalid', 'source': 'explicit_configuration'}
    for name in (('codex.exe', 'codex') if windows else ('codex',)):
        located = shutil.which(name, path=environment.get('PATH', ''))
        if located and (found := result(located, 'path')):
            return found
    if windows:
        for candidate, source in _installed_windows(environment):
            if found := result(candidate, source):
                return found
    return {'available': False, 'error_code': 'codex_not_installed', 'source': 'not_found'}
