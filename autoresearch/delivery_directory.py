"""Assignment-bound navigation of a frozen code delivery, without file bodies."""
from pathlib import PurePosixPath


def enabled(context, purpose):
    return (context.get('delivery_directory_protocol') == 1
            and context.get('implementation_alignment_protocol') == 1
            and purpose.split(':', 1)[0] == 'code_review'
            and isinstance(context.get('delivery_artifacts'), list))


def project(context):
    result = dict(context)
    files = result.pop('delivery_artifacts')
    result.pop('delivery_files', None)
    result['delivery_directory'] = {'directory': '', 'file_count': len(files),
        'total_bytes': sum(row['bytes'] for row in files),
        'list_tool': 'artifacts.list', 'read_tool': 'artifacts.read',
        'scope': 'Only the current frozen delivery. Directory entries are navigation, not reading evidence.'}
    for key in ('implementation', 'local_acceptance'):
        if isinstance(result.get(key), dict):
            result[key] = {name: value for name, value in result[key].items() if name not in {'code', 'files'}}
    return result


def listing(engine, run_id, role, handoff_id, args):
    from .engine import digest
    frozen = engine.store.get('handoff_inputs', handoff_id, {}) if handoff_id else {}
    assignment = engine.store.get('handoffs', handoff_id, {}) if handoff_id else {}
    context = frozen.get('context', {})
    if (not enabled(context, assignment.get('purpose', ''))
            or assignment.get('run_id') != run_id or assignment.get('to_role') != role
            or frozen.get('run_id') != run_id
            or assignment.get('input_hash') != digest({key: frozen.get(key) for key in ('instruction', 'context', 'schema')})):
        raise ValueError('Delivery directory requires the exact frozen review assignment')
    directory = args.get('directory', '')
    offset, limit = args.get('offset', 0), args.get('limit', 30)
    if (not isinstance(directory, str) or '\\' in directory or ':' in directory
            or directory.startswith('/') or any(part in {'.', '..'} for part in directory.split('/'))):
        raise ValueError('Delivery directory must be a relative path within this snapshot')
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('Directory offset must be nonnegative and limit from 1 to 50')
    prefix = directory.rstrip('/') + '/' if directory else ''
    entries = {}
    for row in context['delivery_artifacts']:
        path = row['path']
        if not path.startswith(prefix):
            continue
        relative = path[len(prefix):]
        if '/' in relative:
            name = prefix + relative.split('/', 1)[0]
            item = entries.setdefault(name, {'path': name, 'type': 'directory', 'file_count': 0, 'bytes': 0})
            item['file_count'] += 1
            item['bytes'] += row['bytes']
        else:
            suffix = PurePosixPath(path).suffix.lower()
            entries[path] = {**row, 'type': 'file', 'format': 'binary' if suffix in {
                '.npy', '.npz', '.pt', '.pth', '.bin', '.pyc', '.png', '.jpg', '.pdf', '.zip'} else 'text_or_unknown'}
    values = [entries[name] for name in sorted(entries)]
    if prefix and not values:
        raise ValueError('Directory is outside the current frozen code-review delivery')
    if offset > len(values):
        raise ValueError('Directory offset exceeds entry count')
    end = min(offset + limit, len(values))
    return {'directory': directory, 'entries': values[offset:end], 'total_entries': len(values),
        'offset': offset, 'next_offset': end if end < len(values) else None,
        'delivery_hash': context.get('delivery_hash'), 'reading_evidence': False}
