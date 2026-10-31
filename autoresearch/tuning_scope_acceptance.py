"""Project the effective approvals alongside the immutable pre-approval plan."""
import copy

from .experiment_workflow import sha
from .tuning_scope_reads import active_vote


GUIDANCE = '''
tuning_scope.acceptance 是宿主对当前绑定重新核验后的范围批准证据。
它包含生效的三方独立票及历史CPU检查原额度；plan.strategy保留批准前原文，
其中“待认证”、旧否决及错误资源口径是历史状态，不能覆盖已生效的批准与澄清。
不再重复索取同一范围认证或重审固定控制项；历史检查按其原回执额度核对，不能追溯套用上下文准备时限。
范围批准仅关闭范围/回执归属准入问题；新候选仍须精确YAML版本、同种子控制、
单因素差异、独立投票、资源门禁及真实训练。保留未定训练原因，不假称稳定或科学通过。
'''


def project(engine, run_id, record, plan):
    """Caller has just verified the plan, owners, reads and all effective votes."""
    final = record['rounds'][-1]
    votes = {}
    for role, original in final['votes'].items():
        request = active_vote(original)
        answer = request['answer']
        votes[role] = {'purpose': request['purpose'], 'answer_sha256': sha(answer),
            **{k: copy.deepcopy(answer[k]) for k in ('approve', 'rationale', 'issues')}}
    if (record['status'] != 'accepted' or record['plan_sha256'] != sha(plan)
            or set(votes) != {'director', 'engineer', 'scout'}
            or any(not v['approve'] or v['issues'] for v in votes.values())):
        raise ValueError('Scope acceptance requires the verified effective independent votes')
    from .tuning_scope_context import historical_check
    archive = engine.store.get('experiment_evidence', record['evidence']['id'])
    historical = historical_check(engine, run_id, archive['summary'])
    body = {'version': 1, 'scope_id': record['id'], 'plan_sha256': record['plan_sha256'],
        'binding': copy.deepcopy(record['binding']), 'source_evidence': copy.deepcopy(record['evidence']),
        'accepted_at': record['updated_at'], 'effective_votes': votes,
        'verification': 'Current source identity, author, same-role read coverage and effective votes verified by host',
        'historical_cpu_check': historical, 'scope_approved': True,
        'execution_authorized': False, 'scientific_approval': False}
    return {**body, 'sha256': sha(body)}
