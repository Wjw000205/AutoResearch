"""Compare declared revision units with the actual immutable SPEC difference."""
from __future__ import annotations

from .implementation_review_routing import digest
from .implementation_authoring_preservation import _tokens, _index, SaveConflict


def pointer(tokens):
    return ''.join('/' + str(token).replace('~', '~0').replace('/', '~1') for token in tokens)


def changed_paths(before, after, prefix=()):
    if digest(before) == digest(after):
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for key in sorted(set(before) | set(after)):
            path = (*prefix, key)
            result.extend(changed_paths(before[key], after[key], path)
                          if key in before and key in after else [path])
        return result
    if isinstance(before, list) and isinstance(after, list):
        result = []
        for index in range(max(len(before), len(after))):
            path = (*prefix, str(index))
            result.extend(changed_paths(before[index], after[index], path)
                          if index < min(len(before), len(after)) else [path])
        return result
    return [prefix]


def _exists(spec, tokens):
    value = spec
    try:
        for token in tokens:
            if isinstance(value, list):
                value = value[_index(token, len(value))]
            elif isinstance(value, dict):
                value = value[token]
            else:
                return False
    except (KeyError, SaveConflict):
        return False
    return True


def diagnostic(before, after, reports, allowed):
    """Accept chapter names or exact paths, without widening their scope.

    Every declared unit must really change and every changed leaf must be covered.
    Parent declarations cover descendants; siblings cannot cover each other.
    Added/removed units are located in either immutable version.
    Numeric locator paths may omit the leading slash. Resolve literal chapter
    names first, and apply all ordinary pointer/existence/scope checks unchanged.
    """
    actual = changed_paths(before, after)
    claims, invalid = [], []
    for report in reports:
        for field in report['changed_fields']:
            try:
                if field.startswith('/'):
                    tokens = tuple(_tokens(field))
                elif field in before or field in after or '/' not in field:
                    tokens = (field,)
                else:
                    tokens = tuple(_tokens('/' + field))
                if not (_exists(before, tokens) or _exists(after, tokens)):
                    raise ValueError('missing revision unit')
            except (SaveConflict, ValueError):
                invalid.append(field)
                continue
            claims.append((field, tokens))
    def covers(parent, child):
        return child[:len(parent)] == parent
    unreported = [pointer(path) for path in actual if not any(covers(tokens, path) for _, tokens in claims)]
    unchanged = [field for field, tokens in claims if not any(covers(tokens, path) for path in actual)]
    forbidden = sorted({path[0] for path in actual if path[0] not in allowed}
                       | {tokens[0] for _, tokens in claims if tokens[0] not in allowed})
    return {'actual_fields': sorted({path[0] for path in actual}),
        'actual_paths': sorted(pointer(path) for path in actual),
        'invalid_paths': sorted(set(invalid)), 'unchanged_claims': sorted(set(unchanged)),
        'unreported_paths': sorted(unreported), 'forbidden_fields': forbidden,
        'valid': not (invalid or unchanged or unreported or forbidden)}
