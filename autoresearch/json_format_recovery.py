"""Lossless local recovery of narrowly specified JSON structure defects.

This is deliberately not a general JSON repairer. It cannot finish truncated
outputs, invent values, fix research judgments, or authorize another request.
The caller must still validate the original requested output schema.
"""
from __future__ import annotations

import hashlib
import json
import re


def _reject(value):
    raise ValueError('Nonstandard or ambiguous JSON')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _reject(key)
        result[key] = value
    return result


def completed_output(metadata: dict) -> bool:
    """Require an explicit successful terminal, never guess from response text."""
    return (isinstance(metadata, dict) and metadata.get('output_truncated') is False
            and metadata.get('output_limit_reached') is False
            and metadata.get('provider_status') in (None, 'completed')
            and metadata.get('incomplete_reason') is None
            and metadata.get('stop_reason') not in {'refusal', 'pause_turn', 'model_context_window_exceeded', 'max_tokens'}
            and not any(reason in {'content_filter', 'length'} for reason in metadata.get('finish_reasons', []))
            and (metadata.get('provider_status') == 'completed'
                 or metadata.get('stop_reason') in {'end_turn', 'stop_sequence'}
                 or 'stop' in metadata.get('finish_reasons', [])))


def repair_duplicate_review_binding(text: str, schema: dict, metadata: dict) -> tuple[str, dict] | None:
    """Remove an exactly repeated root review hash, never a repeated judgment.

    Only complete final review objects qualify. Both occurrences must contain
    the same literal SHA256 string, and all remaining values and the original
    closed review schema must validate unchanged. Nested or conflicting keys,
    repeated approval/rationale fields and tool calls are always rejected.
    """
    from jsonschema import Draft202012Validator
    if (not completed_output(metadata) or not isinstance(text, str) or not isinstance(schema, dict)
            or len(text) > 128_000 or not text.lstrip().startswith('{')):
        return None
    decoder = json.JSONDecoder(parse_constant=_reject, object_pairs_hook=_unique_object)
    position = len(text) - len(text.lstrip()) + 1
    fields, literals, edits, preceding_comma = {}, {}, [], None

    def whitespace(offset):
        while offset < len(text) and text[offset] in ' \t\r\n':
            offset += 1
        return offset

    try:
        while True:
            position = whitespace(position)
            key, position = decoder.raw_decode(text, position)
            position = whitespace(position)
            if not isinstance(key, str) or text[position:position + 1] != ':':
                return None
            start = whitespace(position + 1)
            value, end = decoder.raw_decode(text, start)
            literal = text[start:end]
            if key in fields:
                if (key not in {'proposal_hash', 'revision_hash'} or preceding_comma is None
                        or len(edits) >= 2 or not isinstance(value, str)
                        or not re.fullmatch('[0-9a-f]{64}', value)
                        or fields[key] != value or literals[key] != literal):
                    return None
                edits.append({'offset': preceding_comma, 'removed': text[preceding_comma:end], 'inserted': ''})
            else:
                fields[key], literals[key] = value, literal
            position = whitespace(end)
            if text[position:position + 1] == '}':
                if text[position + 1:].strip():
                    return None
                break
            if text[position:position + 1] != ',':
                return None
            preceding_comma, position = position, position + 1
        branches = schema.get('anyOf', [schema])
        reviews = [branch for branch in branches if isinstance(branch, dict)
            and branch.get('type') == 'object' and branch.get('additionalProperties') is False
            and {'approve', 'proposal_hash', 'rationale', 'required_changes'}.issubset(branch.get('required', []))
            and branch.get('properties', {}).get('approve', {}).get('type') == 'boolean']
        if (not edits or 'tool_calls' in fields or not any(Draft202012Validator(branch).is_valid(fields) for branch in reviews)):
            return None
        repaired = text
        for edit in reversed(edits):
            offset = edit['offset']
            repaired = repaired[:offset] + repaired[offset + len(edit['removed']):]
        if decoder.decode(repaired) != fields:
            return None
    except (ValueError, RecursionError, TypeError):
        return None
    return repaired, {'version': 1, 'kind': 'duplicate_identical_review_binding',
        'original_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
        'repaired_sha256': hashlib.sha256(repaired.encode('utf-8')).hexdigest(),
        'edits': edits, 'offset_unit': 'unicode_code_point', 'values_preserved': True,
        'policy': 'Only an identical repeated root review SHA256 removed; judgments, original schema and downstream evidence gates unchanged.'}


def _escape_string_whitespace(text, *, max_edits):
    """Escape string-value whitespace only; retain exact character edit offsets."""
    pieces, edits, quoted, escaped, string_edits = [], [], False, False, False
    escapes = {'\n': '\\n', '\r': '\\r', '\t': '\\t'}
    for offset, char in enumerate(text):
        if quoted:
            if escaped:
                if ord(char) < 32:
                    return None
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
                if string_edits and text[offset + 1:].lstrip().startswith(':'):
                    return None  # Never change a key, even in an open nested object.
            elif ord(char) < 32:
                if char not in escapes or len(edits) >= max_edits:
                    return None
                edits.append({'offset': offset, 'removed': char, 'inserted': escapes[char]})
                string_edits = True
                pieces.append(escapes[char])
                continue
        elif char == '"':
            quoted, string_edits = True, False
        pieces.append(char)
    if quoted or escaped or not edits:
        return None
    return ''.join(pieces), edits


def repair_string_whitespace(text: str, schema: dict, metadata: dict) -> tuple[str, dict] | None:
    """Escape literal LF/CR/TAB in values of a completed closed final object.

    No content is removed or invented. The relaxed decoding of the original
    must equal strict decoding of the derived text under the unchanged schema.
    Tool requests, keys, other controls and all other syntax defects are refused.
    """
    from jsonschema import Draft202012Validator
    if (not completed_output(metadata) or not isinstance(schema, dict) or not isinstance(text, str)
            or len(text) > 128_000 or not text.lstrip().startswith('{')):
        return None
    strict = json.JSONDecoder(parse_constant=_reject, object_pairs_hook=_unique_object)
    try:
        strict.decode(text)
        return None
    except json.JSONDecodeError as error:
        if not error.msg.startswith('Invalid control character'):
            return None
    except (ValueError, RecursionError):
        return None
    escaped = _escape_string_whitespace(text, max_edits=128)
    if escaped is None:
        return None
    repaired, edits = escaped
    try:
        value = strict.decode(repaired)
        original = json.loads(text, strict=False, parse_constant=_reject, object_pairs_hook=_unique_object)
        branches = schema.get('anyOf', [schema])
        if (not isinstance(value, dict) or 'tool_calls' in value or value != original
                or not any(isinstance(branch, dict) and branch.get('type') == 'object'
                    and branch.get('additionalProperties') is False
                    and Draft202012Validator(branch).is_valid(value) for branch in branches)):
            return None
    except (ValueError, RecursionError, TypeError):
        return None
    return repaired, {'version': 1, 'kind': 'unescaped_string_whitespace',
        'original_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
        'repaired_sha256': hashlib.sha256(repaired.encode('utf-8')).hexdigest(),
        'edits': edits, 'offset_unit': 'unicode_code_point', 'values_preserved': True,
        'policy': 'Only literal LF/CR/TAB in string values escaped; decoded original values, schema and downstream evidence gates unchanged.'}


def repair_local_document_string_whitespace(text: str, schema: dict, metadata: dict) -> tuple[str, dict] | None:
    """Losslessly escape completed local document write/edit argument values.

    The frozen closed tool branch must accept the complete original value. Only
    implementation.write_document/edit_document qualify; identities, hashes,
    JSON Pointer paths, operation names and keys are never repaired. The normal
    writer still checks role, source bytes, versions, SPEC and review gates.
    """
    from jsonschema import Draft202012Validator
    try:
        if (not completed_output(metadata) or not isinstance(schema, dict) or not isinstance(text, str)
                or len(text) > 128_000 or not text.lstrip().startswith('{')):
            return None
    except (TypeError, ValueError):
        return None
    strict = json.JSONDecoder(parse_constant=_reject, object_pairs_hook=_unique_object)
    try:
        strict.decode(text)
        return None
    except json.JSONDecodeError as error:
        if not error.msg.startswith('Invalid control character'):
            return None
    except (ValueError, RecursionError):
        return None

    def allowed_values(value, *, protected=False):
        if isinstance(value, dict):
            for key, child in value.items():
                if any(ord(char) < 32 for char in key):
                    return False
                identity = (key in {'id', 'ids', 'name', 'tool', 'op', 'path', 'paths', 'hash', 'sha256'}
                    or key.endswith(('_id', '_ids', '_hash', '_sha256', '_path', '_paths')))
                if not allowed_values(child, protected=protected or identity):
                    return False
        elif isinstance(value, list):
            return all(allowed_values(child, protected=protected) for child in value)
        elif isinstance(value, str):
            controls = [char for char in value if ord(char) < 32]
            return not controls or (not protected and all(char in '\n\r\t' for char in controls))
        return True

    try:
        original = json.loads(text, strict=False, parse_constant=_reject, object_pairs_hook=_unique_object)
        calls = original.get('tool_calls') if isinstance(original, dict) else None
        if (not isinstance(original, dict) or set(original) != {'tool_calls'}
                or not isinstance(calls, list) or not calls or len(calls) > 3):
            return None
        for call in calls:
            if not isinstance(call, dict) or set(call) != {'name', 'arguments'}:
                return None
            arguments = call['arguments']
            if not isinstance(arguments, dict):
                return None
            if call['name'] == 'implementation.write_document':
                if (set(arguments) - {'specification', 'patch', 'base_spec_hash', 'expected_sha256'}
                        or ('specification' in arguments) == ('patch' in arguments)):
                    return None
            elif call['name'] == 'implementation.edit_document':
                if set(arguments) != {'document_id', 'expected_sha256', 'base_spec_hash', 'patch'}:
                    return None
            else:
                return None
            if not allowed_values(call):
                return None
        branches = schema.get('anyOf', [schema])
        if not any(isinstance(branch, dict) and branch.get('type') == 'object'
                and branch.get('additionalProperties') is False
                and 'tool_calls' in branch.get('required', [])
                and Draft202012Validator(branch).is_valid(original) for branch in branches):
            return None
        escaped = _escape_string_whitespace(text, max_edits=512)
        if escaped is None:
            return None
        repaired, edits = escaped
        if strict.decode(repaired) != original:
            return None
    except (ValueError, TypeError, RecursionError):
        return None
    return repaired, {'version': 1, 'kind': 'unescaped_local_document_string_whitespace',
        'original_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
        'repaired_sha256': hashlib.sha256(repaired.encode('utf-8')).hexdigest(),
        'edits': edits, 'offset_unit': 'unicode_code_point', 'values_preserved': True,
        'policy': 'Only literal LF/CR/TAB in local document argument values escaped; frozen schema, decoded original values, identity/hash/path/op and downstream source/review gates unchanged.'}


def repair_evidence_condition_quote(text: str, schema: dict, metadata: dict) -> tuple[str, dict] | None:
    """Escape one premature quote in the first conditions string.

    Only the closed evidence-card contract is eligible, with an explicitly
    completed output. A single inserted backslash must make the entire original
    object strict, schema-valid JSON. Source quotes and scientific fields are
    retained; their independent verbatim validation still applies.
    """
    from jsonschema import Draft202012Validator
    from .context_reduction import SCHEMA
    if (schema != SCHEMA or not completed_output(metadata) or not isinstance(text, str)
            or len(text) > 128_000 or not text.lstrip().startswith('{')):
        return None
    decoder = json.JSONDecoder(parse_constant=_reject, object_pairs_hook=_unique_object)
    try:
        decoder.decode(text)
        return None
    except json.JSONDecodeError as error:
        if error.msg != "Expecting ',' delimiter" or text[error.pos:error.pos + 1] != ')':
            return None
        position = error.pos
    except (ValueError, RecursionError):
        return None
    tokens = list(re.finditer(r'"(?:[^"\\]|\\["\\/bfnrt]|\\u[0-9a-fA-F]{4})*"', text[:position]))
    if not tokens:
        return None
    last = tokens[-1]
    if (text[last.end():position].strip()
            or not re.search(r'"conditions"\s*:\s*\[\s*$', text[:last.start()])):
        return None
    offset = last.end() - 1
    repaired = text[:offset] + '\\' + text[offset:]
    try:
        value = decoder.decode(repaired)
        if not Draft202012Validator(schema).is_valid(value):
            return None
    except (ValueError, RecursionError, TypeError):
        return None
    return repaired, {'version': 1, 'kind': 'unescaped_evidence_condition_quote',
        'original_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
        'repaired_sha256': hashlib.sha256(repaired.encode('utf-8')).hexdigest(),
        'edits': [{'offset': offset, 'removed': '', 'inserted': '\\'}],
        'offset_unit': 'unicode_code_point',
        'policy': 'Escape one premature quote in conditions; every supplied character and scientific field retained. Source anchors require ordinary validation.'}


def repair_evidence_background_kind(text: str, schema: dict, metadata: dict) -> tuple[str, dict] | None:
    """Map background to the catch-all category in the exact evidence contract.

    This is a taxonomy compatibility rule, not a scientific judgment repair.
    Claims, quotes, epistemic status and adverse categories remain untouched;
    the evidence compiler must still validate every source anchor.
    """
    from jsonschema import Draft202012Validator
    from .context_reduction import SCHEMA, LEGACY_SCHEMA
    if (schema not in (SCHEMA, LEGACY_SCHEMA) or not completed_output(metadata)
            or not isinstance(text, str) or len(text) > 128_000):
        return None
    try:
        value = json.loads(text, parse_constant=_reject, object_pairs_hook=_unique_object)
        errors = list(Draft202012Validator(schema).iter_errors(value))
        replacements = []
        for error in errors:
            path = list(error.absolute_path)
            if (error.validator != 'enum' or error.instance != 'background' or len(path) != 5
                    or path[0] != 'pages' or type(path[1]) is not int
                    or path[2] != 'findings' or type(path[3]) is not int or path[4] != 'kind'):
                return None
            replacements.append({'path': '/' + '/'.join(map(str, path)), 'from': 'background', 'to': 'other'})
            value['pages'][path[1]]['findings'][path[3]]['kind'] = 'other'
        if not replacements or not Draft202012Validator(schema).is_valid(value):
            return None
        repaired = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    except (ValueError, RecursionError, KeyError, TypeError):
        return None
    return repaired, {'version': 1, 'kind': 'evidence_background_category', 'replacements': replacements,
        'original_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
        'repaired_sha256': hashlib.sha256(repaired.encode('utf-8')).hexdigest(),
        'policy': 'Only background kind mapped to other; claims, quotes, epistemic status and source validation retained.'}


def repair_detached_schema_fields(text: str, schema: dict, metadata: dict) -> tuple[str, dict] | None:
    """Remove one premature root closing brace, retaining all supplied fields.

    Only {known_fields},"other_known_field":...} is accepted. Both pieces must
    be intact strict JSON with disjoint keys from one closed final contract.
    This never manufactures missing fields, drops unknown ones or repairs tools.
    """
    from jsonschema import Draft202012Validator
    if not completed_output(metadata) or not isinstance(text, str) or len(text) > 128_000:
        return None
    branches = schema.get('anyOf', [schema]) if isinstance(schema, dict) else []
    decoder = json.JSONDecoder(parse_constant=_reject, object_pairs_hook=_unique_object)
    try:
        first, end = decoder.raw_decode(text, len(text) - len(text.lstrip()))
        tail = text[end:].lstrip()
        if not isinstance(first, dict) or not first or not tail.startswith(','):
            return None
        remaining = decoder.decode('{' + tail[1:])
        if not isinstance(remaining, dict) or not remaining or set(first) & set(remaining):
            return None
        value = {**first, **remaining}
        if 'tool_calls' in value or not any(
                isinstance(branch, dict) and branch.get('type') == 'object'
                and branch.get('additionalProperties') is False
                and set(value) <= set(branch.get('properties', {}))
                and Draft202012Validator(branch).is_valid(value) for branch in branches):
            return None
        repaired = text[:end - 1] + text[end:]
        if text[end - 1] != '}' or decoder.decode(repaired) != value:
            return None
    except (ValueError, RecursionError, KeyError, TypeError):
        return None
    return repaired, {'version': 1, 'kind': 'detached_schema_fields',
        'fields': list(remaining),
        'original_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
        'repaired_sha256': hashlib.sha256(repaired.encode('utf-8')).hexdigest(),
        'edits': [{'offset': end - 1, 'removed': '}', 'inserted': ''}],
        'offset_unit': 'unicode_code_point',
        'policy': 'Only one premature root closing brace removed; every supplied value retained.'}


def repair_detached_review_fields(text: str, schema: dict, metadata: dict) -> tuple[str, dict] | None:
    """Nest an intact detached progress object, only for the exact core contract.

    Accept only {vote}\n{progress},"problem_review":{...}}. Every original
    value is preserved, duplicate fields/trailing text are rejected, and the
    caller must revalidate the original complete schema. No missing value can
    be supplied by this transformation.
    """
    base = {'approve', 'proposal_hash', 'rationale', 'required_changes'}
    if (not isinstance(schema, dict) or set(schema.get('required', [])) != base | {'progress', 'problem_review'}
            or set(schema.get('properties', {})) != base | {'progress', 'problem_review'}
            or not isinstance(metadata, dict) or metadata.get('output_truncated') is not False
            or metadata.get('output_limit_reached') is not False
            or metadata.get('provider_status') not in (None, 'completed')
            or metadata.get('incomplete_reason') is not None
            or metadata.get('stop_reason') in {'refusal', 'pause_turn', 'model_context_window_exceeded', 'max_tokens'}
            or any(reason in {'content_filter', 'length'} for reason in metadata.get('finish_reasons', []))
            or not (metadata.get('provider_status') == 'completed'
                    or metadata.get('stop_reason') in {'end_turn', 'stop_sequence'}
                    or 'stop' in metadata.get('finish_reasons', []))
            or not isinstance(text, str) or len(text) > 128_000):
        return None
    decoder = json.JSONDecoder(parse_constant=_reject, object_pairs_hook=_unique_object)
    try:
        first, end = decoder.raw_decode(text, len(text) - len(text.lstrip()))
        start = end + len(text[end:]) - len(text[end:].lstrip())
        progress, last = decoder.raw_decode(text, start)
        tail = text[last:].lstrip()
        if not tail.startswith(','):
            return None
        remaining = decoder.decode('{' + tail[1:])
        if (not isinstance(first, dict) or set(first) != base or not isinstance(progress, dict)
                or set(progress) != set(schema['properties']['progress']['required'])
                or not isinstance(remaining, dict) or set(remaining) != {'problem_review'}):
            return None
        repaired = text[:end - 1] + ',"progress":' + text[start:]
        if decoder.decode(repaired) != {**first, 'progress': progress, **remaining}:
            return None
    except (ValueError, RecursionError, KeyError, TypeError):
        return None
    return repaired, {'version': 1, 'kind': 'detached_review_progress_object',
        'original_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
        'repaired_sha256': hashlib.sha256(repaired.encode('utf-8')).hexdigest(),
        'edits': [{'offset': end - 1, 'removed': text[end - 1:start], 'inserted': ',"progress":'}],
        'offset_unit': 'unicode_code_point', 'policy': 'Only nesting repaired; all supplied judgment values retained.'}


def repair_survey_queries(text: str, metadata: dict) -> tuple[str, dict] | None:
    """Insert one ] before an existing }, only for a complete tool envelope.

    Strings (including quotes, escapes and brackets within search queries) and
    all surrounding calls stay byte-for-byte intact. No EOF completion, nested
    answer extraction, wrapper removal or other syntax change is permitted.
    """
    if (not isinstance(metadata, dict) or metadata.get('output_truncated') is not False
            or metadata.get('output_limit_reached') is not False
            or metadata.get('provider_status') not in (None, 'completed')
            or metadata.get('incomplete_reason') is not None
            or metadata.get('stop_reason') in {'refusal', 'pause_turn', 'model_context_window_exceeded', 'max_tokens'}
            or any(reason in {'content_filter', 'length'} for reason in metadata.get('finish_reasons', []))
            or not (metadata.get('provider_status') == 'completed'
                    or metadata.get('stop_reason') in {'end_turn', 'stop_sequence', 'tool_use'}
                    or any(reason in {'stop', 'tool_calls', 'function_call'} for reason in metadata.get('finish_reasons', [])))
            or not text.lstrip().startswith('{') or len(text) > 128_000):
        return None
    decoder = json.JSONDecoder(parse_constant=_reject, object_pairs_hook=_unique_object)
    try:
        decoder.decode(text)
        return None
    except json.JSONDecodeError as error:
        position = error.pos
        if error.msg != "Expecting ',' delimiter" or text[position:position + 1] != '}':
            return None
    except (ValueError, RecursionError):
        return None

    # Find the exact opening array, respecting JSON string escapes. The only
    # accepted nesting is root -> tool_calls -> call -> arguments -> queries.
    stack, quoted, escaped = [], False, False
    for offset, char in enumerate(text[:position]):
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in '[{':
            stack.append((char, offset))
        elif char in ']}':
            if not stack or stack[-1][0] != ('[' if char == ']' else '{'):
                return None
            stack.pop()
    if quoted or [char for char, _ in stack] != ['{', '[', '{', '{', '[']:
        return None
    if not re.search(r'"queries"\s*:\s*$', text[:stack[-1][1]]):
        return None
    repaired = text[:position] + ']' + text[position:]
    try:
        value = decoder.decode(repaired)
        call = decoder.raw_decode(repaired, stack[2][1])[0]
        arguments = decoder.raw_decode(repaired, stack[3][1])[0]
    except (ValueError, RecursionError):
        return None
    if (not isinstance(value, dict) or set(value) != {'tool_calls'}
            or not isinstance(value['tool_calls'], list) or not 1 <= len(value['tool_calls']) <= 3
            or any(not isinstance(item, dict) or set(item) != {'name', 'arguments'}
                   or not isinstance(item['name'], str) or not isinstance(item['arguments'], dict)
                   for item in value['tool_calls'])
            or call not in value['tool_calls'] or call.get('name') != 'literature.survey'
            or call.get('arguments') != arguments or set(arguments) != {'queries'}
            or not isinstance(arguments['queries'], list) or not 1 <= len(arguments['queries']) <= 3
            or any(not isinstance(query, str) or not query.strip() or len(query) > 400
                   for query in arguments['queries'])):
        return None
    return repaired, {
        'version': 1, 'kind': 'missing_survey_queries_closing_bracket',
        'original_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
        'repaired_sha256': hashlib.sha256(repaired.encode('utf-8')).hexdigest(),
        'insertions': [{'offset': position, 'text': ']'}], 'offset_unit': 'unicode_code_point',
    }


def repair_extra_tool_call_closing_brace(text: str, schema: dict,
                                       metadata: dict) -> tuple[str, dict] | None:
    """Remove one unmatched } after a complete final documents.read call.

    This recognizes root -> tool_calls array, after both the call and its
    arguments have already closed, immediately before the array's ]. Matched
    containers, string contents, keys, values and every other character remain
    untouched. Multiple preceding calls are allowed by the original schema.
    This is not a repair for missing commas, quotes, values or incomplete output.
    """
    import math
    from jsonschema import Draft202012Validator, SchemaError, ValidationError

    if (not isinstance(text, str) or not 0 < len(text) <= 128_000
            or not isinstance(schema, dict) or not text.lstrip().startswith('{')):
        return None
    try:
        if not completed_output(metadata):
            return None
    except (TypeError, ValueError):
        return None

    def finite_float(literal):
        value = float(literal)
        if not math.isfinite(value):
            _reject(literal)
        return value

    decoder = json.JSONDecoder(parse_constant=_reject, parse_float=finite_float,
                               object_pairs_hook=_unique_object)
    try:
        decoder.decode(text)
        return None  # A valid response never needs a format audit.
    except json.JSONDecodeError as error:
        position = error.pos
        if error.msg != "Expecting ',' delimiter" or text[position:position + 1] != '}':
            return None
    except (ValueError, RecursionError, OverflowError):
        return None

    stack, quoted, escaped, last_object = [], False, False, None
    for offset, char in enumerate(text[:position]):
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in '[{':
            stack.append((char, offset))
        elif char in ']}':
            if not stack or stack[-1][0] != ('[' if char == ']' else '{'):
                return None
            opening, start = stack.pop()
            if opening == '{':
                last_object = (start, offset + 1)
    if (quoted or [char for char, _ in stack] != ['{', '['] or last_object is None
            or text[last_object[1]:position].strip()
            or not text[position + 1:].lstrip().startswith(']')):
        return None

    # Establish the real root property owning this array with the JSON decoder,
    # rather than matching a suffix that could occur inside an escaped string.
    root_start, array_start = stack[0][1], stack[1][1]
    cursor = root_start + 1
    try:
        while True:
            while cursor < len(text) and text[cursor] in ' \t\r\n':
                cursor += 1
            key, cursor = decoder.raw_decode(text, cursor)
            if not isinstance(key, str):
                return None
            while cursor < len(text) and text[cursor] in ' \t\r\n':
                cursor += 1
            if text[cursor:cursor + 1] != ':':
                return None
            cursor += 1
            while cursor < len(text) and text[cursor] in ' \t\r\n':
                cursor += 1
            if cursor == array_start:
                if key != 'tool_calls':
                    return None
                break
            _, cursor = decoder.raw_decode(text, cursor)
            while cursor < len(text) and text[cursor] in ' \t\r\n':
                cursor += 1
            if text[cursor:cursor + 1] != ',':
                return None
            cursor += 1

        # This object was independently complete before the unmatched closer.
        call, call_end = decoder.raw_decode(text, last_object[0])
        if (call_end != last_object[1] or not isinstance(call, dict)
                or set(call) != {'name', 'arguments'} or call['name'] != 'documents.read'
                or not isinstance(call['arguments'], dict)):
            return None
        repaired = text[:position] + text[position + 1:]
        value = decoder.decode(repaired)
        if (not isinstance(value, dict) or not set(value) <= {'tool_calls', 'working_notes'}
                or not isinstance(value.get('tool_calls'), list) or not value['tool_calls']
                or value['tool_calls'][-1] != call):
            return None
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(value)
        original_sha = hashlib.sha256(text.encode('utf-8')).hexdigest()
        repaired_sha = hashlib.sha256(repaired.encode('utf-8')).hexdigest()
        schema_sha = hashlib.sha256(json.dumps(schema, ensure_ascii=False, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
    except (ValueError, TypeError, RecursionError, OverflowError, SchemaError, ValidationError):
        return None
    return repaired, {
        'version': 1, 'kind': 'extra_tool_call_closing_brace',
        'original_sha256': original_sha, 'repaired_sha256': repaired_sha,
        'schema_sha256': schema_sha,
        'edits': [{'offset': position, 'removed': '}', 'inserted': ''}],
        'offset_unit': 'unicode_code_point', 'values_preserved': True,
        'charges_added': 0, 'scientific_approval': False,
        'policy': 'Only the unmatched closing brace after a complete documents.read call removed; '
            'all supplied characters and values otherwise retained; original schema and evidence gates unchanged.',
    }
