"""Recover a copied section ID only from the reviewer's own frozen source packet."""
import copy
import json
import re

from jsonschema import Draft202012Validator

from . import bounded_evidence_review as packets
from .experiment_workflow import sha

KIND = 'host_section_id_from_exact_quote'
PACKET_KIND = 'host_packet_id_from_verified_delivery'


def is_read(purpose, role, context):
    packet = context.get(packets.KEY, {})
    return (packet.get('version') == 1 and packet.get('phase') == 'read'
            and packet.get('role') == role and bool(packet.get('host_repair_id'))
            and purpose == 'rapid_review:host-repair:' + str(packet.get('packet_sha256')))


def one_digit_copy(raw, expected):
    if not re.fullmatch('[0-9a-f]{63}|[0-9a-f]{65}', raw):
        return False
    shorter, longer = sorted((raw, expected), key=len)
    return any(longer[:i] + longer[i + 1:] == shorter for i in range(len(longer)))


def analyze(purpose, role, context, schema, value):
    """Separate uniquely evidenced copy errors from rows requiring a fresh reading."""
    if not is_read(purpose, role, context) or not isinstance(value, dict):
        return None
    packet = context[packets.KEY]
    if (sha({k: v for k, v in packet.items() if k != 'packet_sha256'}) != packet['packet_sha256']
            or value.get('packet_sha256', packet['packet_sha256'] if packet.get('receipt_bound_read') == 1 else None)
            != packet['packet_sha256']):
        return None
    errors = list(Draft202012Validator(schema).iter_errors(value))
    if not errors:
        return None
    for error in errors:
        path = tuple(error.path)
        if (len(path) != 3 or path[0] != 'observations' or type(path[1]) is not int
                or path[2] != 'section_id' or error.validator not in ('pattern', 'enum')):
            return None
    sections = packet['payload']['sections']
    sources = {s['id']: s['text'] for s in sections}
    rows = value['observations']
    if len(sources) != len(sections) or len(rows) != len(sections):
        return None
    for s in sections:
        if (s['id'] != sha([s['path'], s['sha256'], s['offset'], s['end']])
                or len(s['text']) != s['end'] - s['offset']):
            return None
    used, unresolved, changes = set(), [], []
    for index, row in enumerate(rows):
        raw = row['section_id']
        quote = row['quote']
        if raw in sources:
            if raw in used or not quote.strip() or quote not in sources[raw]:
                return None
            used.add(raw)
            continue
        if re.fullmatch(r'[a-f0-9]{64}', raw):
            return None  # A different intact digest is a substantive conflict.
        matches = [sid for sid, text in sources.items() if quote.strip() and quote in text]
        if len(matches) == 1 and one_digit_copy(raw, matches[0]):
            if matches[0] in used:
                return None
            used.add(matches[0])
            changes.append({'index': index, 'original_id': raw, 'section_id': matches[0],
                            'quote_sha256': sha(quote)})
        else:
            unresolved.append(index)
    targets = [sid for sid in sources if sid not in used]
    if len(unresolved) != len(targets):
        return None
    return {'kind': KIND, 'packet_sha256': packet['packet_sha256'], 'changes': changes,
            'unresolved_indices': unresolved, 'remaining_section_ids': targets}


def normalized(value, audit):
    result = copy.deepcopy(value)
    for change in audit['changes']:
        result['observations'][change['index']]['section_id'] = change['section_id']
    return result


def delivery_matches(store, saved, frozen):
    from .host_review_compaction import assert_preserved
    from .native_retained_response import captured_delivery
    wire = captured_delivery(store, saved, frozen)
    if not wire:
        return False
    assert_preserved(frozen['context'], wire['active_context'], require_exact=True)
    return True


def packet_omission(purpose, role, frozen, prepared, value, verify_delivery):
    packet = frozen['context'][packets.KEY]
    expected = packet['packet_sha256']
    raw = value.get('packet_sha256')
    if (not is_read(purpose, role, frozen['context']) or packet.get('compact_review_protocol') != 1
            or not isinstance(raw, str) or not re.fullmatch('[a-f0-9]{48,63}', raw)
            or not verify_delivery or sha({k: v for k, v in packet.items() if k != 'packet_sha256'}) != expected):
        return None
    # Both ends must identify the same digest with one contiguous internal omission.
    missing = len(expected) - len(raw)
    if not any(raw == expected[:i] + expected[i + missing:] for i in range(6, len(raw) - 5)):
        return None
    result = {**value, 'packet_sha256': expected}
    for schema in (frozen['schema'], prepared['schema']):
        if (schema.get('properties', {}).get('packet_sha256') != {'const': expected}
                or not Draft202012Validator(schema).is_valid(result)):
            return None
    sections = packet['payload']['sections']
    rows = result['observations']
    ids = {s['id'] for s in sections}
    if (len(rows) != len(sections) or len(ids) != len(sections)
            or {row['section_id'] for row in rows} != ids):
        return None
    for row in rows:
        quote = row['quote']
        if not quote.strip() or [s['id'] for s in sections if quote in s['text']] != [row['section_id']]:
            return None
    if not verify_delivery():
        return None
    return result, {'kind': PACKET_KIND, 'original_packet_sha256': raw, 'packet_sha256': expected,
                    'observations_sha256': sha(rows), 'verified_sections': len(rows)}


def local(purpose, role, frozen, prepared, text, *, verify_delivery=None):
    if prepared['context'].get(packets.KEY) != frozen['context'].get(packets.KEY):
        return None
    value = json.loads(text)
    if 'packet_sha256' in value and value['packet_sha256'] != frozen['context'][packets.KEY]['packet_sha256']:
        return packet_omission(purpose, role, frozen, prepared, value, verify_delivery)
    audit = analyze(purpose, role, frozen['context'], frozen['schema'], value)
    if not audit or audit['unresolved_indices']:
        return None
    result = normalized(value, audit)
    if not Draft202012Validator(prepared['schema']).is_valid(result):
        return None
    packets.validate_answer({**frozen, 'answer': {'packet_sha256': audit['packet_sha256'], **result}})
    return result, audit


def constrain(prepared, value, audit):
    """Only the original author can re-read unresolved sections of this packet."""
    schema = prepared['schema']
    fixed = normalized(value, audit)
    items = [{'const': row} for row in fixed['observations']]
    array = schema['properties']['observations']
    for index, sid in zip(audit['unresolved_indices'], audit['remaining_section_ids'], strict=True):
        item = copy.deepcopy(array['items'])
        item['properties']['section_id'] = {'const': sid}
        items[index] = item
    schema['properties']['packet_sha256'] = {'const': audit['packet_sha256']}
    array.update(prefixItems=items, items=False, minItems=len(items), maxItems=len(items))
    context = prepared['context']
    context['output_schema_repair']['host_section_read'] = audit
    packet = context[packets.KEY]
    packet['parent_packet_sha256'] = packet.pop('packet_sha256')
    needed = set(audit['remaining_section_ids'])
    packet['payload']['sections'] = [s for s in packet['payload']['sections'] if s['id'] in needed]
    packet['packet_sha256'] = sha(packet)
    prepared['task'] += ('\n补读包只含待核对原文。若答复包含 packet_sha256，使用 schema 指定的父包标识。'
                         '仅重新核对 unresolved_indices 对应的 remaining_section_ids。'
                         '逐段给出你的观察和逐字引文，其余条目已固定。证据不足如实记录。')
