"""One author correction of an omitted digest in an authenticated code review.

A different valid hash is never a typo. Legacy clarifications accept a 62/63
character candidate-hash subsequence; bounded diagnostics have their own strict
source and read authentication. Neither recovery rewrites a vote.
"""
from __future__ import annotations

import copy
import re
from types import SimpleNamespace

from jsonschema import Draft202012Validator


def omission(store, run_id, key, value):
    from .engine import Harness, ResearchBlocked, digest
    from . import rapid_experiment_revision as rapid

    from .bounded_review_hash_recovery import omission as bounded_omission
    bounded = bounded_omission(store, run_id, key, value)
    if bounded is not None:
        return bounded
    inputs = store.get('handoff_inputs', key, {})
    owner = store.get('handoffs', key, {})
    context, schema = inputs.get('context', {}), inputs.get('schema', {})
    scope = context.get('rapid_revision', {})
    record = store.get(rapid.COLLECTION, scope.get('id', ''), {})
    role = owner.get('to_role')
    assignment = record.get('review_clarifications', {}).get(role, {})
    if (not owner.get('purpose', '').startswith('rapid_review:')
            or not owner.get('purpose', '').endswith(':approval-clarification')
            or record.get('run_id') != run_id or record.get('status') != 'reviewing'
            or not isinstance(value, dict) or 'tool_calls' in value
            or any(assignment.get(field) != inputs.get(field)
                   for field in ('instruction', 'context', 'schema'))
            or assignment.get('purpose') != owner.get('purpose')):
        return None
    bad = value.get('candidate_sha256')
    expected = schema.get('properties', {}).get('candidate_sha256', {}).get('const')
    if (not isinstance(bad, str) or not re.fullmatch('[a-f0-9]{62,63}', bad)
            or not isinstance(expected, str) or not re.fullmatch('[a-f0-9]{64}', expected)):
        return None
    remaining = iter(expected)
    if not all(char in remaining for char in bad):
        return None
    errors = list(Draft202012Validator(schema).iter_errors(value))
    if (len(errors) != 1 or errors[0].validator != 'const'
            or list(errors[0].path) != ['candidate_sha256']):
        return None
    prior = assignment.get('original_handoff')
    vote = assignment.get('original_vote') or {}
    prior_input = store.get('handoff_inputs', prior, {})
    prior_owner = store.get('handoffs', prior, {})
    decision = store.get('decisions', prior, {})
    candidate = record.get('candidate', {})
    expected_hashes = {'candidate_sha256': candidate.get('bundle', {}).get('sha256'),
        'checks_sha256': candidate.get('checks_sha256'),
        'amendment_sha256': candidate.get('amendment_sha256')}
    clarification = scope.get('approval_clarification', {})
    if (prior_owner.get('status') != 'completed' or prior_owner.get('run_id') != run_id
            or prior_owner.get('to_role') != role or decision.get('result') != vote
            or prior_owner.get('output_hash') != digest(vote)
            or prior != Harness.decision_key(run_id, role, prior_owner.get('purpose'),
                prior_input.get('instruction'), prior_input.get('context'), prior_input.get('schema'))
            or clarification.get('prior_vote') != vote
            or clarification.get('previous_handoff_id') != prior
            or clarification.get('approval_inherited') is not False
            or value.get('read_receipts') != vote.get('read_receipts')
            or any(vote.get(field) != expected_hash or scope.get(field) != expected_hash
                   or schema.get('properties', {}).get(field) != {'const': expected_hash}
                   for field, expected_hash in expected_hashes.items())):
        return None
    # Validate the immutable files and original author's real source coverage.
    # These reads are authentication, not a substitute for an author response.
    try:
        rapid._candidate_unchanged(record)
        rapid.review_receipts(SimpleNamespace(store=store), run_id, record,
            {'role': role, **vote}, prior_input['context'], purpose=prior_owner['purpose'],
            instruction=prior_input['instruction'], schema=prior_input['schema'])
    except (ResearchBlocked, ValueError, KeyError, TypeError, OSError):
        return None
    return {'version': 1, 'field': 'candidate_sha256', 'original_value': bad,
        'expected_value': expected, 'revision_id': record['id'], 'original_review_id': prior,
        'original_vote_sha256': digest(vote), 'read_receipts_sha256': digest(vote['read_receipts'])}


def constrain(prepared, value, source):
    """Ask the original author; lock every other field including dissent."""
    audit = source['rapid_review_hash_omission']
    for field, content in value.items():
        if field != audit['field']:
            prepared['schema']['properties'][field] = {'const': copy.deepcopy(content)}
    prepared['schema']['properties'][audit['field']] = {'const': audit['expected_value']}
    prepared['context']['rapid_review_hash_omission'] = copy.deepcopy(audit)
    prepared['task'] += (
        '\n候选版本 SHA 抄写时遗漏了十六进制字符。宿主已核对原同角色复审、源码读取回执和未变候选文件；'
        f'本次仅由你补正 {audit["field"]}，逐字复制 output schema 中该字段的 const。'
        '其余字段已锁定为你的原澄清答复，包括 approve、issues、rationale、其他版本和 read_receipts。'
        '不要重新评审、改变结论或调用工具。原错误答复保留；此次补正本身不是 CPU 通过、训练完成或科学验收。')
    if audit.get('kind') == 'bounded_diagnostic_review':
        prepared['task'] += ('\n本次为有界诊断代码评审。checks_sha256 绑定原候选入口代码，'
            '不表示检查已执行或通过。原 approve 与全部 issues 已锁定，否决必须保持否决；'
            '实现缺口、资源不相容和未执行状态均不能因哈希补正而关闭。')
