"""Read-only admission and UI facts for a frozen negative-validation hold.

This never repairs a saved vote, reclassifies a scientific outcome, or writes a
checkpoint. The original review remains available for evidence reconciliation.
"""
import json

from .research_cycle import reported_negative_stop_hold


REASON = ('验证结果与停止规则待核对：已保存的验收报告负结果，已批准的停止规则禁止自动修订或重新训练。'
          '请查看验证评审，核对失败指标、验收完整性与三方意见后收尾；直接恢复或增加轮数不能解除此门禁。')


def current(store, run):
    if (run.get('stage') not in {'validation_review', 'revision', 'final_tests'}
            or run.get('status') not in {'running', 'paused', 'blocked', 'failed'}):
        return None
    cycle = run.get('state', {}).get('research_cycle', 1)
    # Select only operands, not the large frozen design and execution histories.
    with store._read_db() as db:
        row = db.execute("""SELECT json_object(
            'id',json_extract(data,'$.id'),
            'decision',json_extract(data,'$.decision'),
            'proposal_hash',json_extract(data,'$.proposal_hash'),
            'revision_hash',json_extract(data,'$.revision_hash'),
            'policy',json_extract(data,'$.iteration_policy'),
            'plan_policy',json_extract(data,'$.experiment_plan.iteration_policy'),
            'acceptance',json_extract(data,'$.validation_summary.acceptance_checks'),
            'votes',json_extract(data,'$.review_votes'))
            FROM records WHERE collection='research_cycles' AND key=?
            AND json_extract(data,'$.run_id')=? AND json_extract(data,'$.cycle')=?""",
            (f"{run['id']}:{cycle}", run['id'], cycle)).fetchone()
    if not row:
        return None
    review = json.loads(row[0])
    # A recorded stop has its own report-only route and must remain resumable.
    if review['decision'] == 'stop':
        return None
    summary = {'acceptance_checks': review['acceptance']}
    if not any(reported_negative_stop_hold(summary, policy)
               for policy in (review['policy'], review['plan_policy'])):
        return None
    from .rapid_continuation import validated
    continuation = validated(store, run['id'])
    if continuation and continuation['policy_amendment']['cycle'] == cycle:
        return None  # New forward-only policy; original negative review remains archived.
    acceptance = review['acceptance']
    checks = acceptance.get('checks')
    failed = [{key: check.get(key) for key in ('metric', 'relative_improvement',
        'minimum_relative_improvement', 'comparison', 'complete_seed_coverage', 'n_pairs', 'source')}
        for check in (checks if isinstance(checks, list) else [])
        if isinstance(check, dict) and check.get('passed') is False]
    votes = review['votes'] if isinstance(review['votes'], list) else []
    bound = bool(review['proposal_hash'] and review['revision_hash'])
    rejected = sorted({vote['role'] for vote in votes if isinstance(vote, dict)
        and vote.get('role') in {'director', 'engineer', 'scout'} and vote.get('approve') is False
        and bound and vote.get('proposal_hash') == review['proposal_hash']
        and vote.get('revision_hash') == review['revision_hash']})
    return {'kind': 'reported_negative_under_stop_policy', 'resume_allowed': False,
        'reason': REASON, 'source': 'saved_validation_review', 'review_id': review['id'],
        'cycle': cycle, 'saved_decision': review['decision'], 'failed_checks': failed,
        'criteria_complete': acceptance.get('criteria_complete'), 'rejected_roles': rejected,
        'proposal_hash': review['proposal_hash'], 'revision_hash': review['revision_hash']}


def assert_resumable(store, run):
    if current(store, run):
        raise ValueError(REASON)
