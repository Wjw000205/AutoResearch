"""Exact-field reads after an unsaved SPEC write; never scientific approval.

The host calls prepare only for a new, unfrozen save request. Existing prepared
requests remain immutable. Author ownership and ordinary document/read-receipt
gates still apply; this module only narrows the save-phase read exception.
"""
from __future__ import annotations

import copy

from . import document_index


KEY = 'implementation_document_repair_reads'
VERSION = 1
_WRITERS = {'implementation.write_document', 'implementation.edit_document'}
_EXTRA_HEADINGS = {'architecture_traceability': '架构要求与实现验收对应',
                   'comparison_transport': '比较数据传递合同'}


def _failure(tool_results):
    from .engine import digest
    for row in reversed(tool_results or []):
        if not isinstance(row, dict) or row.get('predecessor_handoff_id') is not None:
            continue
        call, result = row.get('call') or {}, row.get('result') or {}
        if not isinstance(call, dict) or call.get('name') not in _WRITERS:
            continue
        # A later successful/undetailed writer supersedes an earlier failure.
        if not isinstance(result, dict) or result.get('saved') is not False:
            return None
        validation = result.get('validation')
        if not isinstance(validation, dict) or not isinstance(validation.get('errors'), list):
            return None
        paths = [error.get('field_path') for error in validation['errors'] if isinstance(error, dict)]
        return {'paths': paths, 'sha256': digest({'tool': call['name'],
            'arguments_sha256': digest(call.get('arguments')), 'validation': validation, 'saved': False})}
    return None


def _ranges(manifest, text, fields):
    from .implementation_document_readability import HEADINGS
    labels = {field: {field, HEADINGS.get(field), _EXTRA_HEADINGS.get(field)} - {None}
              for field in fields}
    sections = manifest.get('sections', [])
    headings = []
    for section in sections:
        # Check the actual indexed heading level, not a title substring or an
        # author-created subheading which happens to reuse a SPEC field label.
        first = text[section['start']:section['end']].split('\n', 1)[0].rstrip('\r')
        level = len(first) - len(first.lstrip('#'))
        headings.append((level, first[level + 1:] if first.startswith('#' * level + ' ') else None))
    ranges = []
    for field in fields:
        matches = [i for i, (level, label) in enumerate(headings) if level == 2 and label in labels[field]]
        if len(matches) != 1:
            continue  # Missing/ambiguous chapter: no guessed read authority.
        index = matches[0]
        for i in range(index, len(sections)):
            if i > index and headings[i][0] <= 2:
                break
            section = sections[i]
            if section['end'] > section['start']:
                ranges.append({'field': field, 'section_id': section['id'],
                               'char_start': section['start'], 'char_end': section['end'],
                               'offset_start': 0, 'offset_end': section['end'] - section['start']})
    return ranges


def prepare(engine, run_id, handoff_id, context, tool_results):
    """Return metadata authorization for selected invalid fields, without text."""
    from . import implementation_authoring as author
    from . import implementation_authoring_preservation as preservation
    from .codex_workflow import SPEC
    from .engine import digest
    output = {key: value for key, value in context.items() if key != KEY}
    if (type(context.get('implementation_document_completion_protocol')) is not int
            or context['implementation_document_completion_protocol'] != 1):
        return output
    failure = _failure(tool_results)
    if not failure:
        return output
    # Use actual host receipts as the authority, not a caller-supplied diagnostic.
    progress = engine.store.get('handoff_progress', handoff_id, {})
    if _failure(progress.get('tool_results')) != failure:
        return output
    for slot in ('normal_request', 'finalization'):
        if progress.get(slot, {}).get('prepared', {}).get('context') == context:
            return context  # Never revise an existing frozen/prepared input.
    assignment, frozen, state, binding = author._assignment(engine, run_id, 'engineer', handoff_id)
    if (assignment.get('status') == 'completed' or state.get('status') != 'specification'
            or state.get('current_revision') != binding['revision']):
        return output
    schema_fields = (binding.get('specification_schema') or SPEC).get('properties', {})
    fields = sorted({path[0] for path in failure['paths'] if isinstance(path, list) and path
                     and isinstance(path[0], str) and path[0] in SPEC['properties']
                     and path[0] in schema_fields})
    if not fields:
        return output
    latest = engine.store.get(author.COLLECTION, handoff_id)
    if latest:
        author._checked(engine, run_id, latest)
    try:
        base = preservation.baseline(engine, run_id, state, binding, frozen, latest)
    except preservation.SaveConflict:
        return output
    descriptor = context.get('implementation_document_base')
    if (base is None or not isinstance(descriptor, dict)
            or descriptor.get('spec_hash') != base.get('spec_hash')
            or descriptor.get('document_id') != base['document']['document_id']
            or descriptor.get('document_sha256') != base['document']['sha256']):
        return output
    manifest, text = document_index.load(engine, run_id, descriptor['document_id'], descriptor['document_sha256'])
    ranges = _ranges(manifest, text, fields)
    if not ranges:
        return output
    ids = {row['section_id'] for row in ranges}
    directory = {'document_id': descriptor['document_id'], 'sha256': descriptor['document_sha256'],
                 'title': manifest['title'], 'kind': manifest['document_kind'],
                 'total_chars': manifest['text_chars'],
                 'sections': [copy.deepcopy(section) for section in manifest['sections'] if section['id'] in ids],
                 'primary_source_reading': False, 'scientific_claims_verified': False}
    authorization = {'version': VERSION, 'run_id': run_id, 'handoff_id': handoff_id, 'role': 'engineer',
        'workflow_id': state['id'], 'revision': binding['revision'],
        'document_id': descriptor['document_id'], 'document_sha256': descriptor['document_sha256'],
        'base_spec_hash': descriptor['spec_hash'], 'failure_sha256': failure['sha256'],
        'fields': sorted({row['field'] for row in ranges}), 'directories': [directory], 'ranges': ranges,
        'automatic_text_delivery': False, 'primary_source_reading': False,
        'scientific_approval': False, 'execution_authorization': False}
    authorization['authorization_sha256'] = digest(authorization)
    return {**output, KEY: authorization}


def read_branch(context):
    """A schema branch allowing only explicit, paginated documents.read calls."""
    value = context.get(KEY)
    if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != VERSION:
        return None
    from .engine import digest
    if (value.get('authorization_sha256') != digest({key: item for key, item in value.items()
                                                    if key != 'authorization_sha256'})
            or not isinstance(value.get('document_id'), str)
            or not isinstance(value.get('document_sha256'), str)):
        return None
    ranges = value.get('ranges')
    if not isinstance(ranges, list) or not ranges:
        return None
    alternatives = []
    for row in ranges:
        if (not isinstance(row, dict) or not isinstance(row.get('section_id'), str)
                or type(row.get('offset_start')) is not int or row['offset_start'] != 0
                or type(row.get('offset_end')) is not int or row['offset_end'] <= 0):
            return None
        alternatives.append({'type': 'object', 'additionalProperties': False,
            'required': ['document_id', 'expected_sha256', 'section_id', 'offset', 'limit'],
            'properties': {'document_id': {'const': value['document_id']},
                           'expected_sha256': {'const': value['document_sha256']},
                           'section_id': {'const': row['section_id']},
                           'offset': {'type': 'integer', 'minimum': row['offset_start'], 'maximum': row['offset_end'] - 1},
                           'limit': {'type': 'integer', 'minimum': 1, 'maximum': min(6000, row['offset_end'])}}})
    return {'type': 'object', 'additionalProperties': False, 'required': ['tool_calls'],
        'properties': {'tool_calls': {'type': 'array', 'minItems': 1, 'maxItems': 3,
            'items': {'type': 'object', 'additionalProperties': False, 'required': ['name', 'arguments'],
                'properties': {'name': {'const': 'documents.read'},
                               'arguments': {'oneOf': alternatives}}}}}}


def validate_call(engine, run_id, handoff_id, context, call):
    """True authorizes this narrow exception; absent marker preserves legacy rules."""
    from .engine import digest
    value = context.get(KEY)
    if value is None:
        return False
    if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != VERSION:
        raise ValueError('Invalid implementation repair read authorization')
    sealed = {key: item for key, item in value.items() if key != 'authorization_sha256'}
    if value.get('authorization_sha256') != digest(sealed):
        raise ValueError('Implementation repair read authorization changed')
    progress = engine.store.get('handoff_progress', handoff_id, {})
    # Rebuild from current immutable base and actual failure receipts. Strip the
    # prepared-only request wrapper so prepare does not mistake this check for a
    # request mutation; the existing prepared object itself remains untouched.
    source = {key: item for key, item in context.items() if key != KEY}
    expected = prepare(engine, run_id, handoff_id, source, progress.get('tool_results', [])).get(KEY)
    if expected != value:
        raise ValueError('Implementation repair read base, failure or ownership changed')
    if call.get('name') != 'documents.read':
        return False  # Never authorize search or any other save-phase tool.
    args = call.get('arguments') or {}
    if (not isinstance(args, dict) or set(args) - {'document_id', 'expected_sha256', 'section_id', 'offset', 'limit'}
            or args.get('document_id') != value['document_id']
            or args.get('expected_sha256') != value['document_sha256']
            or type(args.get('offset')) is not int or type(args.get('limit')) is not int
            or not 1 <= args['limit'] <= 6000):
        raise ValueError('Repair read requires the exact base document, version and range')
    if not any(args.get('section_id') == row['section_id']
               and row['offset_start'] <= args['offset'] < args['offset'] + args['limit'] <= row['offset_end']
               for row in value['ranges']):
        raise ValueError('Repair read is outside the failed SPEC field ranges')
    return True
