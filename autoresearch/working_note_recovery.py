"""Read-only recovery of one malformed working-note metadata field.

Original replies, requests, reading receipts and settlements remain untouched.
This returns derived JSON, never saves notes or grants scientific approval.
"""
from __future__ import annotations
from .prose_schema import validator as response_validator

import copy
import hashlib
import json
import re

from jsonschema import Draft202012Validator, SchemaError, ValidationError

from . import document_index
from .document_read_window import enabled as window_enabled
from .document_working_notes import NOTES, enabled, sha
from .json_format_recovery import _unique_object, completed_output


HASH = re.compile(r'^[0-9a-f]{64}$')


def _directories(context):
    rows = context.get('indexed_documents')
    if not isinstance(rows, list):
        raise ValueError('Missing frozen document index')
    result = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('document_id'), str):
            raise ValueError('Invalid frozen document directory')
        did = row['document_id']
        if did in result and result[did] != row:
            raise ValueError('Ambiguous document identity')
        result[did] = row
    return result


def _read_anchor(engine, run_id, anchor, directory, receipts, sources):
    """One original, same-assignment read must contain this Unicode interval."""
    did, start, end = anchor['document_id'], anchor['char_start'], anchor['char_end']
    if (type(start) is not int or type(end) is not int
            or type(directory.get('total_chars')) is not int
            or not 0 <= start < end <= directory['total_chars']):
        raise ValueError('Anchor interval outside its frozen source')
    section = next((s for s in directory['sections'] if s['id'] == anchor['section_id']), None)
    if not section or not section['start'] <= start < end <= section['end']:
        raise ValueError('Anchor crosses a frozen section')
    expected = directory['sha256']
    if did not in sources:
        manifest, original = document_index.load(engine, run_id, did, expected)
        if (manifest.get('document_kind') == 'document_working_notes'
                or directory.get('kind') == 'document_working_notes'
                or manifest.get('sections') != directory['sections']
                or len(original) != directory['total_chars']):
            raise ValueError('Original document identity changed or is a summary')
        sources[did] = original
    original = sources[did]
    for receipt in receipts:
        if not isinstance(receipt, dict) or receipt.get('predecessor_handoff_id') is not None:
            continue
        call, page = receipt.get('call'), receipt.get('result')
        if (not isinstance(call, dict) or call.get('name') != 'documents.read'
                or not isinstance(page, dict) or page.get('error')
                or page.get('provenance') != 'exact_document_section'
                or not isinstance(page.get('text'), str) or not page['text']):
            continue
        args = call.get('arguments')
        if (not isinstance(args, dict) or args.get('document_id') != did
                or args.get('expected_sha256') != expected or args.get('section_id') != section['id']
                or page.get('document_id') != did or page.get('sha256') != expected
                or page.get('section_id') != section['id']):
            continue
        offset, limit = args.get('offset', 0), args.get('limit', 4000)
        a, b = page.get('char_start'), page.get('char_end')
        if (type(offset) is not int or not 0 <= offset <= section['chars']
                or type(limit) is not int or not 1 <= limit <= 6000
                or type(a) is not int or type(b) is not int or a != section['start'] + offset
                or not a <= start < end <= b <= min(section['end'], a + limit)
                or original[a:b] != page['text']):
            continue
        source_text = original[start:end]
        if page['text'][start-a:end-a] != source_text:
            continue
        return {'receipt_sha256': sha(receipt),
            'source_text_sha256': hashlib.sha256(source_text.encode('utf-8')).hexdigest()}
    raise ValueError('No exact original read covers the anchor')


def _normalize(h, run_id, role, key, progress, prepared, text, metadata):
    from .engine import Harness, digest
    from .intelligence import parse_json

    if (not completed_output(metadata) or not isinstance(text, str) or not 0 < len(text) <= 128_000
            or not isinstance(prepared, dict) or prepared.get('role') != role
            or not isinstance(progress, dict) or progress.get('id') != key or progress.get('run_id') != run_id
            or progress.get('mode') == 'final_only' or progress.get('pending_tool_calls')):
        return None
    store = h.engine.store
    inputs, handoff = store.get('handoff_inputs', key, {}), store.get('handoffs', key, {})
    saved = store.get('handoff_progress', key, {})
    context = prepared.get('context', {})
    frozen = inputs.get('context', {})
    normal = progress.get('normal_request', {})
    if (store.get('decisions', key) is not None or store.get('run_deletions', run_id)
            or any(row.get('id') != key or row.get('run_id') != run_id for row in (inputs, handoff, saved))
            or handoff.get('to_role') != role or handoff.get('status') == 'completed'
            or handoff.get('document_working_notes_protocol') != 1
            or frozen.get('document_working_notes_protocol', 1) != 1
            or not window_enabled(frozen) or not enabled(context)
            or key != Harness.decision_key(run_id, role, handoff.get('purpose'), inputs.get('instruction'),
                frozen, inputs.get('schema'))
            or handoff.get('input_hash') != digest({'instruction': inputs.get('instruction'),
                'context': frozen, 'schema': inputs.get('schema')})
            or context.get('_purpose') != handoff.get('purpose')
            or normal.get('status') not in {'blocked', 'dispatching', 'response_saved'}
            or type(progress.get('turn')) is not int or normal.get('turn') != progress['turn']
            or normal.get('prepared') != prepared or saved.get('normal_request') != normal
            or saved.get('turn') != progress['turn'] or saved.get('mode') == 'final_only'
            or saved.get('pending_tool_calls') or saved.get('tool_results') != progress.get('tool_results')):
        return None
    marker = context.get('_working_set')
    if marker is not None and (not isinstance(marker, dict) or marker.get('handoff_id') != key
            or marker.get('role') != role):
        return None
    # parse_json rejects duplicate keys/nonfinite values; the second strict
    # decode also forbids prose wrappers, detached fields and extra JSON roots.
    value = parse_json(text)
    strict = json.loads(text, object_pairs_hook=_unique_object,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))
    if value != strict or not isinstance(value, dict) or 'tool_calls' not in value or 'working_notes' not in value:
        return None
    schema = prepared.get('schema')
    Draft202012Validator.check_schema(schema)
    validator = response_validator(schema)
    if validator.is_valid(value) or not any(branch.get('properties', {}).get('working_notes') == NOTES
            for branch in schema.get('anyOf', [])):
        return None
    notes = value['working_notes']
    errors = list(response_validator(NOTES).iter_errors(notes))
    if not errors or any(error.validator != 'pattern' or len(error.absolute_path) != 4
            or list(error.absolute_path)[1] != 'anchors' or list(error.absolute_path)[3] != 'expected_sha256'
            or not isinstance(error.instance, str) or not 1 <= len(error.instance) <= 128 for error in errors):
        return None
    frozen_docs, prepared_docs = _directories(frozen), _directories(context)
    receipts = progress.get('tool_results')
    if not isinstance(receipts, list):
        return None
    result, edits, sources = copy.deepcopy(value), [], {}
    for ni, note in enumerate(result['working_notes']):
        for ai, anchor in enumerate(note['anchors']):
            did = anchor['document_id']
            directory, requested = frozen_docs.get(did), prepared_docs.get(did)
            if (not directory or not requested or not isinstance(directory.get('sha256'), str)
                    or not HASH.fullmatch(directory['sha256'])
                    or any(directory.get(field) != requested.get(field)
                        for field in ('document_id', 'sha256', 'sections', 'total_chars'))):
                return None
            old = anchor['expected_sha256']
            if HASH.fullmatch(old) and old != directory['sha256']:
                return None
            evidence = _read_anchor(h.engine, run_id, anchor, directory, receipts, sources)
            if old != directory['sha256']:
                anchor['expected_sha256'] = directory['sha256']
                edits.append({'path': f'/working_notes/{ni}/anchors/{ai}/expected_sha256',
                    'old': old, 'new': directory['sha256'], **evidence})
    if not edits:
        return None
    validator.validate(result)  # The entire original request schema is unchanged.
    repaired = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return result, {'kind': 'working_note_anchor_hash_from_verified_read', 'version': 1,
        'original_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
        'repaired_sha256': hashlib.sha256(repaired.encode('utf-8')).hexdigest(),
        'schema_sha256': sha(schema), 'source_handoff_id': key, 'role': role, 'edits': edits,
        'charges_added': 0, 'scientific_claims_verified': False, 'scientific_approval': False}


def normalize(h, run_id, role, key, progress, prepared, text, metadata):
    """Return a fully verified derived value/audit, or decline without mutation."""
    from .intelligence import ProviderError
    try:
        return _normalize(h, run_id, role, key, progress, prepared, text, metadata)
    except (ProviderError, ValueError, TypeError, KeyError, AttributeError, OSError,
            RecursionError, SchemaError, ValidationError):
        return None
