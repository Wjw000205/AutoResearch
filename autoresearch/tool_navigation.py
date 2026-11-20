"""Actionable read/navigation failures without guessing evidence or authority."""
from __future__ import annotations


class NavigationError(ValueError):
    def __init__(self, message, *, code, next_calls=(), details=None):
        super().__init__(message)
        self.recovery = {'code': code, 'next_calls': list(next_calls),
                         'reading_credit': False, 'execution_authorized': False,
                         **(details or {})}


def pointer(value, path):
    """Strict JSON Pointer traversal; a failure retains its last valid parent."""
    import re
    if not isinstance(path, str) or path and not path.startswith('/'):
        raise NavigationError('Comparison path must be a JSON pointer', code='invalid_pointer',
                              details={'parent_path': ''})
    parent = ''
    for raw in path[1:].split('/') if path else ():
        invalid = re.search(r'~(?![01])', raw)
        token = raw.replace('~1', '/').replace('~0', '~')
        if invalid:
            error = 'Invalid JSON pointer escape'
        elif isinstance(value, dict) and token in value:
            value = value[token]
            parent += '/' + raw
            continue
        elif (isinstance(value, list) and re.fullmatch(r'0|[1-9][0-9]*', token)
                and len(token) <= len(str(len(value))) and int(token) < len(value)):
            value = value[int(token)]
            parent += '/' + raw
            continue
        else:
            error = 'Comparison path does not exist'
        keys = list(value)[:25] if isinstance(value, dict) else list(range(min(len(value), 25))) if isinstance(value, list) else []
        raise NavigationError(error, code='comparison_path_unavailable', details={
            'parent_path': parent, 'available_children': keys,
            'children_truncated': isinstance(value, (list, dict)) and len(value) > 25})
    return value
