"""Persist disagreements and ask for specific revisions, never infer approval."""
import copy
import inspect

from .prose_schema import validate

from .experiment_workflow import sha
from .storage import now
from .training_adjustment import obj

TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 1600}


def issues(votes, error=None):
    result = []
    if error:
        result.append({'role': 'host', 'text': str(error), 'kind': 'contract'})
    for vote in votes:
        if vote.get('approve') is True and not vote.get('issues'):
            continue
        for text in vote.get('issues') or [vote.get('rationale') or 'Reviewer did not approve']:
            result.append({'role': vote['role'], 'text': text, 'kind': 'review'})
    return [{**v, 'id': sha([i, v])} for i, v in enumerate(result)]


def contract(items, source_sha):
    return obj({'source_sha256': {'const': source_sha}, 'objective': TEXT,
        'resolutions': {'type': 'array', 'minItems': len(items), 'maxItems': len(items), 'items': obj({
            'issue_id': {'enum': [v['id'] for v in items]},
            'kind': {'enum': ['clarify', 'revise', 'inspect_evidence']},
            'instruction': TEXT, 'acceptance_check': TEXT})},
        'execution_authorized': {'const': False}})


async def mediate(engine, harness, run_id, row, context, source, items, save):
    """One durable director assignment; engineering and all votes follow it."""
    from .parameter_resolution import owner
    async def persist():
        result = save()
        if inspect.isawaitable(result):
            await result
    if not items:
        raise ValueError('Consensus mediation requires concrete unresolved issues')
    source_sha = sha({'source': source, 'issues': items})
    if 'mediation' not in row:
        ctx = copy.deepcopy(context)
        ctx['experiment_decision']['consensus_reconciliation'] = {
            'source_sha256': source_sha, 'source': source, 'issues': items,
            'execution_authorized': False}
        row['mediation'] = {'purpose': 'experiment_diagnose:tuning-consensus:' + source_sha,
            'instruction': '主持当前分歧的定向收敛。逐项解释异议，提出可验证的澄清、修订或已有证据读取要求，交工程师落实。'
                '已有材料用 experiments.compare / inspect_module 定向读取，不重做无关研究。'
                '不能用多数票、删除反对票、降低验收或宣布共识来关闭异议。你的答复不是批准，修订后仍须三方独立复审。'
                '重复争议须找出前次修订为何无效，明确不同的可检验修订或所需证据。不要仅重复“存在异议，暂停”。'
                '保留未知根因、原数据划分、种子、预算及完成记录，不调用训练工具。返回每个 issue_id 恰好一次。',
            'context': ctx, 'schema': contract(items, source_sha)}
        await persist()
    assignment = row['mediation']
    if assignment['schema']['properties']['source_sha256']['const'] != source_sha:
        raise ValueError('Consensus source changed after mediation dispatch')
    if 'answer' not in assignment:
        assignment['answer'] = await harness.ask(run_id, 'director', assignment['purpose'],
            assignment['instruction'], assignment['context'], assignment['schema'])
        await persist()
    owner(engine, run_id, 'director', assignment)
    answer = assignment['answer']
    validate(answer, assignment['schema'])
    if sorted(v['issue_id'] for v in answer['resolutions']) != sorted(v['id'] for v in items):
        raise ValueError('Consensus resolution must cover each issue exactly once')
    return {'source_sha256': source_sha, 'issues': copy.deepcopy(items), 'resolution': copy.deepcopy(answer)}


def pending(stage='tuning', **details):
    return {'tuning_pending': True, 'optimization_pending': True, 'next_node': stage,
            'activity': 'consensus_reconciliation', 'execution_authorized': False, **details}
