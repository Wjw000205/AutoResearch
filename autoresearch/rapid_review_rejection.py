"""Collect a real reviewer veto without treating incomplete reads as approval.

Original paid assignments, decisions and receipts are never rewritten. Full
source coverage remains mandatory on every path that can authorize execution.
"""
from __future__ import annotations


def authenticate_vote(engine, run_id, record, vote, context):
    from . import independent_full_review as full
    from .engine import Harness, digest
    from .prose_schema import validate
    from .rapid_experiment_revision import review_instruction, VOTE, _fail, review_receipts

    candidate = record['candidate']
    expected = {'amendment_sha256': candidate['amendment_sha256'],
        'candidate_sha256': candidate['bundle']['sha256'],
        'checks_sha256': candidate['checks_sha256']}
    purpose = record.get('review_purpose') or 'rapid_review:' + candidate['amendment_sha256']
    schema = record.get('review_schema') or VOTE
    answer = full.answer(vote)
    validate(answer, schema)
    instruction = review_instruction(record)
    key = full.decision_key(engine, run_id, vote, purpose, instruction, context, schema)
    owner = engine.store.get('handoffs', key) or {}
    frozen = engine.store.get('handoff_inputs', key) or {}
    saved = (engine.store.get('decisions', key) or {}).get('result')
    if (any(answer.get(k) != value for k, value in expected.items())
            or saved != answer or owner.get('run_id') != run_id
            or owner.get('to_role') != vote['role'] or (owner.get('purpose') != purpose and full.KEY not in vote)
            or owner.get('status') != 'completed' or owner.get('output_hash') != digest(answer)
            or full.source_context(frozen.get('context',{})) != full.source_context(context)
            or (frozen.get('instruction') != instruction and full.KEY not in vote)
            or frozen.get('schema') != schema):
        _fail('候选拒绝缺少同版本、同角色的真实冻结评审与最终答复。')
    return review_receipts(engine, run_id, record, vote, context, require_complete=False)


def rejection_reads(engine, run_id, record, votes, context):
    from . import independent_full_review as full
    from .rapid_experiment_revision import _fail
    if not any(v.get('approve') is False and v.get('issues') for v in votes):
        return None
    roles = {v.get('role') for v in votes}
    if any(full.KEY in v for v in votes):
        if not full.members(votes):
            _fail('候选拒绝必须保留同版本两位全文审核者的真实原始答复。')
    elif record.get('structured_review'):
        from .rapid_review_plan import checked
        checked(record)
        if not roles or len(roles)!=len(votes) or not roles<={'director','engineer','scout'}:
            _fail('拒绝记录的独立角色无效。')
    elif len(votes)!=3 or roles!={'director','engineer','scout'}:
        _fail('候选拒绝缺少三个角色的原始独立答复。')
    return [authenticate_vote(engine,run_id,record,vote,
        record.get('review_contexts',{}).get(vote['role'],context)) for vote in votes]
