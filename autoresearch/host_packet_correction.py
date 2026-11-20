"""Same-reviewer confirmation when only a host review's packet identifier failed."""
import copy
import re

from jsonschema import Draft202012Validator

from .bounded_evidence_review import KEY, HASH
from .experiment_workflow import sha


def applies(purpose, role, context):
    packet = context.get(KEY, {})
    phase = packet.get('phase')
    if (packet.get('version') != 1 or role != packet.get('role') or not packet.get('host_repair_id')
            or phase not in ('read', 'triage', 'code', 'checks')):
        return False
    prefix = 'rapid_review:host-repair:'
    if phase == 'read' and packet.get('payload', {}).get('citation_correction') == 1:
        prefix += 'quote-correction:'
    return purpose == prefix + str(packet.get('packet_sha256'))


def analyze(purpose, role, frozen, prepared, value):
    packet = frozen['context'][KEY]
    expected = packet['packet_sha256']
    if (not applies(purpose, role, frozen['context'])
            or prepared['context'].get(KEY) != packet or prepared['context'].get('_purpose') != purpose
            or expected != sha({k: v for k, v in packet.items() if k != 'packet_sha256'})):
        return None
    raw = value.get('packet_sha256')
    # An intact foreign digest can identify another assignment. It cannot be
    # transferred to this candidate by an identity-only correction.
    if isinstance(raw, str) and re.fullmatch('[0-9a-fA-F]{64}', raw):
        return None
    corrected = {**value, 'packet_sha256': expected}
    for schema in (frozen['schema'], prepared['schema']):
        if schema.get('properties', {}).get('packet_sha256') not in (HASH, {'const': expected}):
            return None
        validator = Draft202012Validator(schema)
        if validator.is_valid(value) or not validator.is_valid(corrected):
            return None
    if packet['phase'] != 'read' and value['approve'] != (value['required_changes'] == []):
        return None
    return {'packet_sha256': expected, 'candidate_sha256': packet['candidate_sha256'],
            'role': role, 'phase': packet['phase'], 'original_packet_value': raw,
            'unchanged_fields_sha256': sha({k: v for k, v in value.items() if k != 'packet_sha256'})}


def constrain(prepared, value, audit):
    expected = {**value, 'packet_sha256': audit['packet_sha256']}
    prepared['schema'] = {'anyOf': [
        {'type': 'object', 'properties': {k: {'const': copy.deepcopy(v)} for k, v in expected.items()},
         'required': list(expected), 'additionalProperties': False},
        {'type': 'object', 'properties': {'cannot_confirm_packet': {'const': True},
                                          'reason': {'type': 'string', 'minLength': 1}},
         'required': ['cannot_confirm_packet', 'reason'], 'additionalProperties': False}]}
    prepared['context']['output_schema_repair']['packet_confirmation'] = copy.deepcopy(audit)
    # The original task and schema are archived with the first request. Keep
    # the entire frozen evidence packet available for disowning a mistaken vote.
    prepared['task'] = (
        '你是原交接的同一评审角色。请核对 bounded_evidence_review 中的原材料、候选版本、阶段和 '
        'output_schema_repair 中你自己的原答复。仅在确认原答复确实针对本批时，逐字保留原判断、'
        '理由、修改项及观察，重填 schema 指定的完整 packet_sha256。若无法确认归属或原判断，'
        '返回 cannot_confirm_packet=true 和真实原因。仅确认原评审范围；不要调用工具或重新执行实验。')
