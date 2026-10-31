"""Phase-bound data identity; an unmaterialized test set is never a fake SHA."""
from __future__ import annotations

import re

PROTOCOL = 2
SPLITS = ('train', 'validation', 'test')


def enabled(evidence):
    return type(evidence.get('split_identity_protocol')) is int and evidence['split_identity_protocol'] == PROTOCOL


def validate(evidence, phase, *, host_version):
    """Return errors without changing evidence or inferring missing identities."""
    version = evidence.get('split_identity_protocol', 1)
    if type(version) is not int or version not in (1, PROTOCOL):
        return ['Unsupported split_identity_protocol']
    if version == 1:
        return []  # Existing callers retain their legacy validation.
    if host_version < PROTOCOL:
        return ['split_identity_protocol=2 is not enabled by the host']
    if phase not in ('validation', 'test') or evidence.get('phase') != phase:
        return ['Split identity phase does not match the actual invocation']
    expected = ('train', 'validation') if phase == 'validation' else SPLITS
    hashes = evidence.get('split_fingerprints')
    states = evidence.get('split_materialization')
    expected_states = {key: 'materialized' if key in expected else 'not_materialized' for key in SPLITS}
    if not isinstance(hashes, dict) or set(hashes) != set(expected):
        return ['Phase-bound split_fingerprints must contain exactly the materialized splits']
    if states != expected_states:
        return ['split_materialization does not match the phase; held-out identity cannot be fabricated']
    if any(not isinstance(value, str) or re.fullmatch('[0-9a-f]{64}', value) is None for value in hashes.values()):
        return ['Materialized split fingerprints must be lowercase SHA256 values']
    if len(set(hashes.values())) != len(expected):
        return ['Materialized split membership fingerprints must be distinct']
    return []


def compatible(first, second):
    """Compare available identities across phases, preserving every known hash."""
    if first.get('dataset_id') != second.get('dataset_id'):
        return False
    if not enabled(first) and not enabled(second):
        return first.get('split_fingerprints') == second.get('split_fingerprints')
    for item in (first, second):
        if enabled(item):
            if validate(item, item.get('phase'), host_version=PROTOCOL):
                return False
        else:
            # A legacy record may supply additional known test identity, but not
            # an unvalidated missing split or a made-up phase protocol.
            values = item.get('split_fingerprints')
            if (type(item.get('split_identity_protocol', 1)) is not int
                    or item.get('split_identity_protocol', 1) != 1 or not isinstance(values, dict)
                    or any(not isinstance(values.get(k), str) or re.fullmatch('[a-fA-F0-9]{64}', values[k]) is None for k in SPLITS)
                    or len({values[k].lower() for k in SPLITS}) != 3):
                return False
    left, right = first['split_fingerprints'], second['split_fingerprints']
    return all(left[k].lower() == right[k].lower() for k in left.keys() & right.keys())


def capability():
    return {'split_identity_protocol': PROTOCOL, 'host_opt_in': 'evaluation_contract_version >= 2',
        'validation': {'fingerprint_keys': ['train', 'validation'],
            'split_materialization': {'train':'materialized','validation':'materialized','test':'not_materialized'}},
        'test': {'fingerprint_keys': list(SPLITS),
            'split_materialization': {k:'materialized' for k in SPLITS}},
        'unknown_test_is_not_a_fingerprint': True, 'scientific_approval': False,
        'legacy_protocol': 'Unchanged: all three actual SHA256 values required.',
        'cross_phase': 'All known shared identities must match; test phase requires a real test SHA.'}
