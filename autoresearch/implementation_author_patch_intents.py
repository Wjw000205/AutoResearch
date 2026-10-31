"""Retain unsaved author field targets without applying or storing their prose.

The caller supplies the authenticated current-handoff tool ledger and real SPECs.
This is a completeness check, never a document save or scientific approval.
"""
from __future__ import annotations

import copy
import re

from .implementation_authoring_preservation import SaveConflict, _patch, _tokens
from .implementation_review_routing import digest

KEY = 'implementation_author_revision_contract'
_WRITERS = {'implementation.write_document', 'implementation.edit_document'}
_SHA = re.compile(r'[0-9a-f]{64}')


def _changed(before, after):
    return {field for field in set(before) | set(after)
            if (field in before) != (field in after)
            or digest(before.get(field)) != digest(after.get(field))}


def _base_document(context, spec_hash):
    descriptor = context.get('implementation_document_base')
    if isinstance(descriptor, dict) and descriptor.get('spec_hash') == spec_hash:
        return {'document_id': descriptor.get('document_id'),
                'sha256': descriptor.get('document_sha256')}
    for row in context.get('previous_specifications', []):
        if isinstance(row, dict) and row.get('spec_hash') == spec_hash:
            document = row.get('document')
            if isinstance(document, dict):
                return document
    return None


def _candidate(call, base, document, fields):
    args = call.get('arguments')
    if not isinstance(args, dict):
        return None
    supplied = args.get('base_spec_hash')
    if supplied is not None and supplied != digest(base):
        return None
    if call['name'] == 'implementation.edit_document':
        required = {'document_id', 'expected_sha256', 'base_spec_hash'}
        if (set(args) not in (required | {'patch'}, required | {'revisions'})
                or not document or args['document_id'] != document.get('document_id')
                or args['expected_sha256'] != document.get('sha256')):
            return None
        if 'revisions' in args:
            from .implementation_document_locator import revision_patch
            try:
                args = {**args, 'patch': revision_patch(base, args['revisions'])}
            except SaveConflict:
                return None
    elif set(args) - {'specification', 'patch', 'base_spec_hash', 'expected_sha256'}:
        return None
    if 'patch' in args:
        if ('specification' in args or supplied is None or not isinstance(args['patch'], list)):
            return None
        try:
            if any(not isinstance(op, dict) or _tokens(op.get('path'))[0] not in fields
                   for op in args['patch']):
                return None
            candidate = _patch(base, args['patch'])
        except (SaveConflict, TypeError, ValueError):
            return None
    else:
        candidate = args.get('specification')
    if not isinstance(candidate, dict) or any(not isinstance(field, str) or field not in fields
                                               for field in candidate):
        return None
    try:
        digest(candidate)
    except (TypeError, ValueError):
        return None
    return candidate


def _validation(value):
    """Keep locations/format diagnostics, never an invalid instance or its text."""
    output = {}
    keys = {'code', 'kind', 'field_path', 'validator', 'expected_type', 'actual_length',
            'actual_count', 'limit', 'missing_fields', 'extra_fields', 'operation_index', 'path'}
    for key in keys & value.keys():
        output[key] = copy.deepcopy(value[key])
    if isinstance(value.get('errors'), list):
        output['errors'] = [_validation(error) for error in value['errors'] if isinstance(error, dict)]
    return output


def _saved_document(result, candidate):
    submission, document = result.get('submission'), result.get('document')
    if (not isinstance(submission, dict) or not isinstance(document, dict)
            or submission.get('spec_hash') != digest(candidate)
            or not isinstance(submission.get('document_id'), str) or not submission['document_id']
            or not isinstance(submission.get('document_sha256'), str)
            or not _SHA.fullmatch(submission['document_sha256'])
            or document.get('document_id') != submission['document_id']
            or document.get('sha256') != submission['document_sha256']):
        return None
    return document


def _changes_receipt(result, before, after):
    receipt = result.get('save_changes')
    if receipt is None:
        return None
    if not isinstance(receipt, dict):
        return False
    fields = [{'field': field, 'before_sha256': digest(before.get(field)),
               'after_sha256': digest(after.get(field)), 'before_present': field in before,
               'after_present': field in after} for field in sorted(_changed(before, after))]
    body = {'version': 1, 'base_spec_hash': digest(before),
            'saved_spec_hash': digest(after), 'fields': fields}
    # Exact comparison catches resealed forged field lists and no-op claims.
    return receipt == {**body, 'sha256': digest(body)}


def derive(context, rows, baseline_spec, current_spec):
    """Derive pending failed-batch roots using only exact, successful field writes.

    Legacy assignments are untouched. Invalid/unbound ledger entries grant no
    completion credit. A retry can use corrected text; it must actually save a
    change in the target root, and that root must remain changed in the real draft.
    """
    empty = {'failures': [], 'pending_fields': []}
    marker = context.get(KEY) if isinstance(context, dict) else None
    if (not isinstance(marker, dict) or type(marker.get('version')) is not int
            or marker['version'] != 1 or not isinstance(baseline_spec, dict)
            or not isinstance(current_spec, dict)):
        return empty
    schema = context.get('implementation_document_authoring', {}).get('specification_schema', {})
    allowed = set(schema.get('properties', {})) or set(baseline_spec) | set(current_spec)
    known = copy.deepcopy(baseline_spec)
    document = _base_document(context, digest(known))
    pending, targets, failures, seen = set(), set(), [], set()
    for row in rows or []:
        if not isinstance(row, dict) or row.get('predecessor_handoff_id') is not None:
            continue
        call, result = row.get('call'), row.get('result')
        if (not isinstance(call, dict) or call.get('name') not in _WRITERS
                or not isinstance(result, dict)):
            continue
        candidate = _candidate(call, known, document, allowed)
        if candidate is None:
            continue
        changed = _changed(known, candidate)
        if result.get('saved') is False and changed:
            validation = result.get('validation')
            if not isinstance(validation, dict):
                continue
            failure = {'tool': call['name'], 'arguments_sha256': digest(call['arguments']),
                       'validation': validation, 'saved': False}
            failure_hash = digest(failure)
            if failure_hash not in seen:
                failures.append({'failure_sha256': failure_hash, 'fields': sorted(changed),
                                 'validation': _validation(validation)})
                seen.add(failure_hash)
            pending.update(changed)
            targets.update(changed)
        elif result.get('saved') is True:
            saved_document = _saved_document(result, candidate)
            receipt = _changes_receipt(result, known, candidate)
            if not saved_document or receipt is False:
                continue
            # Old successes lacking a change receipt only receive credit when
            # their complete reconstructed SPEC is the actual current draft.
            if receipt is True or digest(candidate) == digest(current_spec):
                pending.difference_update(changed)
            known, document = copy.deepcopy(candidate), saved_document
    if digest(known) != digest(current_spec):
        # A divergent/unknown successor cannot erase an earlier failed target.
        pending.update(targets)
    pending.update(targets - _changed(baseline_spec, current_spec))
    return {'failures': failures, 'pending_fields': sorted(pending)}
