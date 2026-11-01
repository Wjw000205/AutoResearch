"""Exact, reversible presentation of an author's current three-role reviews.

No model, semantic similarity, source write, vote replacement or reading credit
is involved. Frozen handoffs and original SQLite decisions/workflows remain the
authority. Shared prose references only replace identical UTF-8 strings; issue
groups require identical text, fixes, anchors, routing and version scope.
"""
from __future__ import annotations

import copy
from collections import Counter
import hashlib
import json
import re

from .implementation_review_routing import digest

VERSION = 1
FIELD = 'implementation_review_compaction'
REF = '$review_text'
ROLES = ('director', 'engineer', 'scout')
FIELDS = ('implementation_current_review_votes', 'document_revision_objections',
          'previous_implementation_review', 'implementation_issue_response_contract')
GUIDANCE = ('\n当前三方意见使用无损压缩展示。$review_text 是本请求 '
    'implementation_review_compaction.text_table 中的精确正文 SHA256 引用，按键读取完整原文；'
    '同文正文只保存一份，issue_groups.members 保留每个角色、原问题ID、版本和独立来源。'
    '同组不代表评审互相接受或异议关闭；issue_responses 只逐项回应 '
    'implementation_issue_response_contract.actions，严格保留各 action 的 (objection_id, origin_ref_sha256)。'
    'obligations 中未列入 actions 的历史来源仅作审计背景，不追加回复；'
    '相同 objection_id 不能据此复用旧来源哈希。'
    '压缩视图、来源指针和读取该视图不增加原SPEC或批准来源的实际Delivered阅读覆盖，'
    '不授予科学批准、代码执行或实验通过。')


class ReviewCompactionError(ValueError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _text_sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _fields(value):
    return {name: copy.deepcopy(value[name]) for name in FIELDS if name in value}


def _source_records(value, votes, source_votes):
    binding = value.get('implementation_document_authoring') or {}
    previous = value.get('previous_implementation_review') or {}
    document = previous.get('document') or {}
    if (not isinstance(binding.get('workflow_id'), str) or not binding['workflow_id']
            or type(binding.get('revision')) is not int or binding['revision'] < 1
            or previous.get('index') != binding['revision'] - 1
            or not isinstance(document.get('document_id'), str) or not document['document_id']
            or any(not isinstance(item, str) or not re.fullmatch(r'[0-9a-f]{64}', item)
                   for item in (previous.get('spec_hash'), document.get('sha256')))):
        raise ReviewCompactionError('Compaction current version/source identity is incomplete')
    raw = votes if source_votes is None else source_votes
    if (len(raw) != 3 or {vote.get('role') for vote in raw} != set(ROLES)
            or len({vote.get('handoff_id') for vote in raw}) != 3):
        raise ReviewCompactionError('Compaction requires three distinct current review sources')
    sources = []
    for vote in votes:
        original = next(item for item in raw if item['role'] == vote['role'])
        public = {key: item for key, item in original.items() if key != 'reading_proof'}
        current = {key: item for key, item in vote.items() if key != 'reading_proof'}
        if public != current:
            raise ReviewCompactionError('Original review and current public projection differ')
        if (not isinstance(vote.get('handoff_id'), str) or not vote['handoff_id']
                or vote.get('spec_hash') != previous.get('spec_hash')
                or vote.get('document_sha256') != document.get('sha256')):
            raise ReviewCompactionError('Compaction review version/source binding changed')
        record = {'role': vote['role'], 'handoff_id': vote['handoff_id'],
            'workflow_id': binding.get('workflow_id'), 'revision': previous.get('index'),
            'document_id': document.get('document_id'), 'document_sha256': document.get('sha256'),
            'spec_hash': previous.get('spec_hash'), 'vote_projection_sha256': digest(vote),
            'public_review_sha256': digest({key: item for key, item in public.items()
                                          if key not in {'role', 'handoff_id'}}),
            'source_pointer': {'collection': 'decisions', 'id': vote['handoff_id']}}
        if 'reading_proof' in original:
            record['original_vote_sha256'] = digest(original)
        if 'reading_proof' in original:
            record['reading_proof_sha256'] = digest(original['reading_proof'])
        sources.append(record)
    return sources


def _issue_groups(value, votes, sources):
    """Only identity/index metadata is excluded from exact unit comparison."""
    actions = value.get('document_revision_objections', [])
    groups = {}
    for vote in votes:
        role = vote['role']
        for index, issue in enumerate(vote.get('issues', [])):
            fixes = [(number, fix) for number, fix in enumerate(vote.get('fixes', []))
                     if fix.get('issue_index') == index]
            positions = {number: local for local, (number, _) in enumerate(fixes)}
            locations = [{**{key: item for key, item in location.items() if key != 'fix_index'},
                          'fix_index': positions[location['fix_index']]}
                         for location in vote.get('fix_locations', [])
                         if location.get('fix_index') in positions]
            routes = [route for route in vote.get('review_routing', {}).get('issues', [])
                      if route.get('issue_index') == index]
            matching = [action for action in actions if action.get('role') == role
                and action.get('source', {}).get('handoff_id') == vote['handoff_id']
                and action.get('source', {}).get('issue_index') == index]
            unit = {'issue': issue,
                'fixes': [{key: item for key, item in fix.items() if key != 'issue_index'} for _, fix in fixes],
                'fix_locations': locations,
                'routing': [{key: item for key, item in route.items()
                             if key not in {'issue_index', 'objection_id'}} for route in routes],
                'routing_metadata': {key: item for key, item in vote.get('review_routing', {}).items()
                                     if key != 'issues'},
                'actions': [{**{key: item for key, item in action.items()
                             if key not in {'id', 'role', 'source', 'issue', 'fixes'}},
                             'issue': action.get('issue'),
                             'fixes': [{key: item for key, item in fix.items() if key != 'issue_index'}
                                       for fix in action.get('fixes', [])],
                             'source_scope': {key: item for key, item in action.get('source', {}).items()
                                              if key not in {'role', 'handoff_id', 'issue_index'}}}
                            for action in matching],
                # Unknown scientific/version attributes are retained in the
                # equality key, not guessed to be harmless differences.
                'review_scope': {key: item for key, item in vote.items() if key not in {
                    'role', 'handoff_id', 'reading_proof', 'rationale', 'issues', 'fixes',
                    'fix_locations', 'review_routing'}}}
            key = digest(unit)
            source = next(item for item in sources if item['role'] == role)
            member = {'role': role, 'handoff_id': vote['handoff_id'], 'issue_index': index,
                'fix_indices': [number for number, _ in fixes],
                'objection_ids': [action['id'] for action in matching if 'id' in action],
                'routing_objection_ids': [route.get('objection_id') for route in routes],
                'origin_refs': [{**copy.deepcopy(action['source']), 'role': role} for action in matching],
                'source': copy.deepcopy(source)}
            member['origin_ref_sha256'] = [digest(ref) for ref in member['origin_refs']]
            group = groups.setdefault(key, {'body_sha256': key, 'body': unit, 'members': []})
            if group['body'] != unit:
                raise ReviewCompactionError('Compaction digest collision')
            group['members'].append(member)
    # Equal issue prose with different anchors/routes remains separate, even
    # though the identical literal string can still be referenced once.
    return [group for group in groups.values()
            if len({member['role'] for member in group['members']}) > 1]


def _walk(value, function, key=None):
    if isinstance(value, dict):
        if set(value) == {REF}:
            raise ReviewCompactionError('Source contains a reserved review text reference')
        return {name: _walk(item, function, name) for name, item in value.items()}
    if isinstance(value, list):
        return [_walk(item, function, key) for item in value]
    return function(value, key)


def _candidate(text, key):
    # Exact source IDs, hashes, roles and paths remain directly legible. No
    # whitespace/Unicode/number normalization is performed on eligible prose.
    return (isinstance(text, str) and len(text.encode('utf-8')) >= 160
        and key not in {'role', 'id', 'location', 'field', 'source', 'kind', 'disposition'}
        and (not isinstance(key, str) or not key.endswith(('_id', '_ids', '_hash', '_sha256'))))


def project_revision(value, *, source_votes=None, source_certificate=None):
    """Return a new phase view; caller never passes/rewrites a saved prepared request.

    source_votes may carry original reading_proof omitted by the phase builder.
    Without it only the exact public record hash is claimed, never a fabricated
    full-vote hash. Original full records stay in SQLite regardless of this view.
    """
    if FIELD in value:
        assert_projection(value)
        return copy.deepcopy(value)
    original = _fields(value)
    votes = original.get('implementation_current_review_votes', [])
    if not votes:
        return copy.deepcopy(value)
    counts = Counter()
    def count(item, key):
        if _candidate(item, key):
            counts[item] += 1
        return item
    _walk(original, count)
    table = {_text_sha(text): text for text, count in counts.items() if count > 1}
    if not table:
        return copy.deepcopy(value)
    if len(votes) != 3 or {vote.get('role') for vote in votes} != set(ROLES):
        raise ReviewCompactionError('Compaction cannot omit or combine independent review roles')
    sources = _source_records(value, votes, source_votes)
    if source_certificate is not None:
        _certificate_header(source_certificate)
        previous = source_certificate.get('sources', [])
        if len(previous) != len(sources):
            raise ReviewCompactionError('Prior compaction source roles changed')
        for source in sources:
            prior = next((item for item in previous if item.get('role') == source['role']), None)
            if prior is None or any(prior.get(key) != item for key, item in source.items()):
                raise ReviewCompactionError('Prior original vote hash cannot move to another public review')
            for key in ('original_vote_sha256', 'reading_proof_sha256'):
                if key in prior:
                    source[key] = prior[key]
    def reference(item, key):
        if _candidate(item, key) and _text_sha(item) in table:
            return {REF: _text_sha(item)}
        return copy.deepcopy(item)
    projected = _walk(original, reference)
    groups = _issue_groups(value, votes, sources)
    for group in groups:
        group['body'] = _walk(group['body'], reference)
    certificate = {'version': VERSION, 'kind': 'exact_lossless_review_projection',
        'fields': list(original), 'field_sha256': {key: digest(item) for key, item in original.items()},
        'source_sha256': digest(original), 'sources': sources, 'text_table': table,
        'issue_groups': groups, 'reading_credit': False, 'scientific_approval': False,
        'execution_authorized': False, 'model_generated': False}
    certificate['sha256'] = digest(certificate)
    result = {**copy.deepcopy(value), **projected, FIELD: certificate}
    # Small opinions should not grow because of provenance overhead. Leaving
    # them unchanged is safe; this is an optimization, never a validation gate.
    if len(_json({**projected, FIELD: certificate}).encode('utf-8')) >= len(_json(original).encode('utf-8')):
        return copy.deepcopy(value)
    assert_projection(result, value)
    return result


def _decode(value, table, used):
    if isinstance(value, dict):
        if set(value) == {REF}:
            key = value[REF]
            if not isinstance(key, str) or key not in table:
                raise ReviewCompactionError('Review text reference is missing or outside this request')
            used.add(key)
            return table[key]
        return {key: _decode(item, table, used) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode(item, table, used) for item in value]
    return copy.deepcopy(value)


def _certificate_header(certificate):
    if (not isinstance(certificate, dict) or certificate.get('version') != VERSION
            or certificate.get('kind') != 'exact_lossless_review_projection'
            or any(certificate.get(key) is not False for key in (
                'reading_credit', 'scientific_approval', 'execution_authorized', 'model_generated'))
            or certificate.get('sha256') != digest({key: item for key, item in certificate.items() if key != 'sha256'})):
        raise ReviewCompactionError('Review compaction certificate changed')


def assert_projection(value, original=None, *, source_votes=None):
    """Verify exact fields and group provenance; this grants no reading/approval."""
    certificate = value.get(FIELD)
    if certificate is None:
        if original is not None and _fields(value) != _fields(original):
            raise ReviewCompactionError('Uncompacted review fields changed')
        return None
    _certificate_header(certificate)
    table = certificate.get('text_table')
    if (not isinstance(table, dict) or any(not isinstance(text, str) or _text_sha(text) != key
                                          for key, text in table.items())):
        raise ReviewCompactionError('Review text hash changed')
    if (certificate.get('fields') != list(_fields(value))
            or set(certificate.get('field_sha256', {})) != set(certificate['fields'])):
        raise ReviewCompactionError('Review compaction fields missing or foreign')
    used = set()
    restored = _decode(_fields(value), table, used)
    if (used != set(table) or digest(restored) != certificate.get('source_sha256')
            or any(digest(item) != certificate['field_sha256'][key] for key, item in restored.items())):
        raise ReviewCompactionError('Restored review content changed or unused text was injected')
    votes = restored.get('implementation_current_review_votes', [])
    sources = certificate.get('sources', [])
    # The input source certificate is separate from the display record: a
    # stripped proof cannot silently become a hash of the original full vote.
    expected = _source_records({**value, **restored}, votes, source_votes)
    if len(sources) != 3:
        raise ReviewCompactionError('Compaction source roles changed')
    for source, public in zip(sources, expected):
        if any(source.get(key) != item for key, item in public.items()):
            raise ReviewCompactionError('Compaction original role/version/source pointer changed')
    groups = copy.deepcopy(certificate.get('issue_groups', []))
    for group in groups:
        group['body'] = _decode(group['body'], table, set())
    if groups != _issue_groups({**value, **restored}, votes, sources):
        raise ReviewCompactionError('Shared issue group changed or merged different evidence')
    if original is not None and restored != _fields(original):
        raise ReviewCompactionError('Compaction does not restore the original exact review fields')
    return certificate


def restore(value):
    """Restore the exact phase fields, not an inferred original full reading proof."""
    certificate = assert_projection(value)
    if certificate is None:
        return copy.deepcopy(value)
    result = copy.deepcopy(value)
    result.update(_decode(_fields(value), certificate['text_table'], set()))
    result.pop(FIELD)
    return result


# Compatibility for callers which name the whole current phase a context.
project_context = project_revision
