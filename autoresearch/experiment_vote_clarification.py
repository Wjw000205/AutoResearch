"""One same-reviewer clarification of contradictory approval fields.

Original assignments and answers stay immutable. A clarification may reject
the proposal; host code never interprets an issue's wording as approval.
"""
from __future__ import annotations

import copy
import json

from .prose_schema import validate

from .experiment_workflow import sha


def _fail():
    from .engine import ResearchBlocked
    raise ResearchBlocked('实验决策澄清缺少匹配的原评审答复、冻结证据或版本绑定。')


def _original(engine, run_id, record, vote, key=None):
    from .engine import Harness
    role = vote['role']
    purpose = 'experiment_vote:' + record['proposal_sha256']
    answer = {k: v for k, v in vote.items() if k != 'role'}
    if key is None:
        with engine.store._read_db() as db:
            rows = db.execute("SELECT key,data FROM records WHERE collection='decisions' "
                "AND json_extract(data,'$.run_id')=? AND json_extract(data,'$.role')=? "
                "AND json_extract(data,'$.purpose')=?", (run_id, role, purpose)).fetchall()
        matches = [k for k, raw in rows if json.loads(raw).get('result') == answer]
        if len(matches) != 1:
            _fail()
        key = matches[0]
    decision = engine.store.get('decisions', key) or {}
    owner = engine.store.get('handoffs', key) or {}
    frozen = engine.store.get('handoff_inputs', key) or {}
    context = frozen.get('context', {})
    if (decision.get('result') != answer or decision.get('run_id') != run_id
            or decision.get('role') != role or decision.get('purpose') != purpose
            or owner.get('run_id') != run_id or owner.get('to_role') != role
            or frozen.get('run_id') != run_id
            or context.get('validation_evidence') != record['evidence']
            or context.get('decision_proposal') != record['proposal']
            or context.get('proposal_sha256') != record['proposal_sha256']
            or answer.get('proposal_sha256') != record['proposal_sha256']
            or answer.get('evidence_sha256') != record['evidence']['sha256']
            or record['proposal_sha256'] != sha(record['proposal'])
            or key != Harness.decision_key(run_id, role, purpose, frozen.get('instruction'),
                                          context, frozen.get('schema'))):
        _fail()
    return key, context


def verify(engine, run_id, record, vote, assignment):
    from .engine import Harness
    _original(engine, run_id, record, {'role': vote['role'], **assignment['original_vote']},
              assignment['original_handoff'])
    key = Harness.decision_key(run_id, vote['role'], assignment['purpose'],
        assignment['instruction'], assignment['context'], assignment['schema'])
    answer = {k: v for k, v in vote.items() if k != 'role'}
    validate(answer, assignment['schema'])
    if (engine.store.get('decisions', key) or {}).get('result') != answer:
        _fail()


async def clarify(engine, harness, run_id, record, checkpoint):
    from .experiment_decisions import VOTE
    votes = copy.deepcopy(record['votes'])
    assignments = copy.deepcopy(record.get('vote_clarifications', {}))
    for i, vote in enumerate(votes):
        role = vote['role']
        assignment = assignments.get(role)
        if assignment and assignment.get('resolved_vote'):
            if vote != assignment['resolved_vote']:
                _fail()
            verify(engine, run_id, record, vote, assignment)
            continue
        if vote.get('approve') is not True or not vote.get('issues'):
            continue
        if assignment is None:
            key, original_context = _original(engine, run_id, record, vote)
            original = {k: v for k, v in vote.items() if k != 'role'}
            # Reuse the same evidence by reference, not its logs/source/history.
            context = {k: copy.deepcopy(original_context[k]) for k in (
                'training_context_protocol', 'experiment_workflow_protocol', 'research_cycle',
                'validation_evidence', 'evidence_sha256', 'decision_proposal', 'proposal_sha256')}
            context['experiment_decision'] = {k: copy.deepcopy(v) for k, v in
                original_context['experiment_decision'].items() if k in (
                    'binding', 'counts', 'coverage_complete', 'budget', 'configuration_queue',
                    'iteration_policy', 'paths', 'scientific_approval')}
            context['experiment_decision']['approval_clarification'] = {
                'prior_vote': original, 'previous_handoff_id': key, 'approval_inherited': False}
            schema = copy.deepcopy(VOTE)
            for field in ('evidence_sha256', 'proposal_sha256'):
                schema['properties'][field] = {'const': original[field]}
            schema['allOf'] = [{'if': {'properties': {'approve': {'const': True}}},
                'then': {'properties': {'issues': {'maxItems': 0}}},
                'else': {'properties': {'issues': {'minItems': 1}}}}]
            assignment = {'purpose': 'experiment_vote:' + record['proposal_sha256'] + ':approval-clarification',
                'context': context, 'schema': schema, 'original_vote': original, 'original_handoff': key,
                'instruction': '澄清你对同一实验下一步提案的评审。上一答复 approve=true 且 issues 非空。'
                    'issues 只记录阻止当前 action 的具体问题；若有阻塞问题，请 approve=false 并列明问题；'
                    '若同意该 action，请 approve=true、issues=[]，将已知局限、未完成实验和后续门禁保留在 rationale。'
                    '停止探索的批准与科学验收分开判断。保持原证据和提案哈希；不得扩大预算、改提案或声称实验已完成。'
                    '根据自己此前的评审澄清字段；需要复核时按 validation_evidence 定向读取。返回严格 JSON。'}
            assignments[role] = assignment
            await checkpoint(vote_clarifications=assignments)
        response = await harness.ask(run_id, role, assignment['purpose'], assignment['instruction'],
                                     assignment['context'], assignment['schema'])
        resolved = {'role': role, **response}
        verify(engine, run_id, record, resolved, assignment)
        votes[i] = resolved
        assignment['resolved_vote'] = resolved
        # Save each completed role so restart cannot ask it again or lose dissent.
        await checkpoint(votes=votes, vote_clarifications=assignments)
    return votes
