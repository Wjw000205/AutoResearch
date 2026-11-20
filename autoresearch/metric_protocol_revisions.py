"""Authenticated protocol amendments create a new derived metric source.

Protocol amendment v1 can clarify completeness/limitations and policy rationale;
it cannot change criteria, jobs or stopping actions. Original requests/replies
remain immutable. The successor plan receives fresh three-role protocol review.
"""
from __future__ import annotations

import copy

from .metric_contract import base_plan, compile_contract, signature
from . import independent_full_review as full_review


def verify_source(engine, rid, source, design):
    from .engine import ResearchBlocked
    from .implementation_alignment_evidence import _delivery
    from .harness_delivery import authenticate as authenticate_delivery
    from .metric_contract_recovery import checked
    from .protocol_amendments import replay, revision_hash, replay_threshold
    if (source.get('version') != 1 or source.get('kind') != 'protocol_amendment'
            or source.get('design_sha256') != signature(design)):
        raise ResearchBlocked('Unknown metric protocol source revision')
    parent = source['parent_plan']
    checked(engine, rid, {**parent, 'acceptance': source['parent_acceptance']}, design)
    previous_source = parent['metric_contract'].get('acceptance_source')
    previous_policy = (previous_source['iteration_policy'] if previous_source else design.get('iteration_policy',
        {'on_validation_failure': 'revise', 'rationale': 'Default bounded research iteration'}))
    if source['parent_policy'] != previous_policy:
        raise ResearchBlocked('Protocol source parent policy is not its approved predecessor')
    proposal = engine.store.get('consensus', rid)['proposal']
    if source['proposal_hash'] != signature(proposal):
        raise ResearchBlocked('Protocol amendment proposal identity changed')
    threshold = replay_threshold(engine, rid, source,
        engine.store.get('runs', rid)['config'].get('min_relative_improvement', .01))
    if signature(threshold) != signature(source['task_primary_threshold']):
        raise ResearchBlocked('Protocol amendment threshold representation changed')
    from .protocol_review_recovery import COLLECTION, key
    for hid in source['amendment_handoffs']:
        decision = engine.store.get('decisions', hid) or {}
        author_context = authenticate_delivery(engine, rid, 'engineer', hid, decision.get('result'))
        rejected = engine.store.get(COLLECTION, key(rid, author_context['revision_hash'])) or {}
        for vote in rejected.get('votes', []):
            _delivery(engine, rid, vote['role'], rejected['handoffs'][vote['role']],
                      full_review.answer(vote))
    acceptance, policy = replay(engine, rid, {'protocol_amendments': source['amendment_handoffs']},
        parent, source['parent_acceptance'], source['parent_policy'], threshold,
        signature(design), source['proposal_hash'])
    expected_hash = revision_hash(parent, acceptance, policy, threshold)
    if (source['acceptance'] != acceptance or source['iteration_policy'] != policy
            or source['protocol_revision_hash'] != expected_hash
            or not full_review.members(source['protocol_votes'])
            or set(source['protocol_handoffs']) != set(full_review.reviewers(source['protocol_votes']))):
        raise ResearchBlocked('Protocol source does not replay to its approved acceptance/policy')
    for vote in source['protocol_votes']:
        role = vote['role']
        delivered = full_review.answer(vote)
        if full_review.KEY in vote:
            full_review.authenticate(engine.store, rid, vote)
            full_review.validate_delivery(engine.store, rid, vote, engine=engine)
        context = _delivery(engine, rid, role, source['protocol_handoffs'][role], delivered)
        if (vote.get('approve') is not True or vote.get('required_changes')
                or not engine.store.get('handoffs', source['protocol_handoffs'][role])['purpose'].startswith('protocol_review:')
                or vote.get('proposal_hash') != source['proposal_hash']
                or vote.get('revision_hash') != expected_hash
                or context.get('proposal_hash') != source['proposal_hash']
                or context.get('design_hash') != signature(design)
                or context.get('revision_hash') != expected_hash):
            raise ResearchBlocked('Metric source amendment lacks three exact authentic approvals')
    return acceptance, policy


def derive(engine, rid, design, plan, before_acceptance, before_policy,
           acceptance, policy, votes, handoffs, revision_hash, amendments, proposal_hash, threshold):
    source = {'version': 1, 'kind': 'protocol_amendment', 'design_sha256': signature(design),
        'parent_plan': copy.deepcopy(plan), 'parent_acceptance': copy.deepcopy(before_acceptance),
        'parent_policy': copy.deepcopy(before_policy), 'acceptance': copy.deepcopy(acceptance),
        'iteration_policy': copy.deepcopy(policy), 'protocol_votes': copy.deepcopy(votes),
        'protocol_handoffs': copy.deepcopy(handoffs), 'protocol_revision_hash': revision_hash,
        'amendment_handoffs': list(amendments), 'proposal_hash': proposal_hash,
        'task_primary_threshold': threshold}
    verify_source(engine, rid, source, design)
    compiled = compile_contract(design, base_plan(plan), acceptance,
        plan['metric_contract']['bindings'], acceptance_source=source, host_threshold=threshold,
        host_primary_criterion=plan['metric_contract'].get('host_primary_criterion'))
    return {**plan, 'metric_contract': compiled}


def checked(engine, rid, plan, design):
    source = plan['metric_contract']['acceptance_source']
    acceptance, _ = verify_source(engine, rid, source, design)
    parent = source['parent_plan']
    rebuilt = compile_contract(design, base_plan(parent), acceptance,
        parent['metric_contract']['bindings'], acceptance_source=source,
        host_threshold=source['task_primary_threshold'],
        host_primary_criterion=parent['metric_contract'].get('host_primary_criterion'))
    if (rebuilt != plan['metric_contract']
            or base_plan(plan) != base_plan(parent)
            or plan.get('metric_contract_work_order') != parent.get('metric_contract_work_order')
            or acceptance != plan['acceptance']):
        raise ValueError('Derived metric contract/source/binding differs from authenticated amendment')
    return acceptance, source['iteration_policy']


async def review_successors(engine, harness, rid, design, plan, before_acceptance, before_policy,
                            acceptance, policy, votes, handoffs, rh, amendments,
                            proposal_hash, threshold, instruction, schema):
    from .architecture_revisions import protocol_context_async
    from .engine import ResearchBlocked
    from .protocol_amendments import review, revision_hash
    seen = set()
    while acceptance != before_acceptance:
        engine.check_active(rid)
        semantic = signature({'acceptance': acceptance, 'policy': policy})
        if semantic in seen:
            raise ResearchBlocked('Protocol source revisions made no semantic progress; original approvals retained')
        seen.add(semantic)
        plan = derive(engine, rid, design, plan, before_acceptance, before_policy,
                      acceptance, policy, votes, handoffs, rh, amendments, proposal_hash, threshold)
        before_acceptance, before_policy = copy.deepcopy(acceptance), copy.deepcopy(policy)
        rh = revision_hash(plan, acceptance, policy, threshold)
        context = await protocol_context_async(engine, harness, rid, design, plan,
            acceptance, policy, proposal_hash, rh)
        context['metric_protocol_source'] = copy.deepcopy(plan['metric_contract']['acceptance_source'])
        votes, handoffs, acceptance, policy, rh, amendments = await review(
            engine, harness, rid, design, plan, acceptance, policy,
            f'protocol_review:{engine.cycle(rid)}:metric_source:{rh}',
            instruction + '\n本版仅以真实协议修订与三方批准形成新的验收来源版本；原设计、付费交付和原条款保留。'
            '核对 metric_protocol_source 的来源与当前编译合同；不得更改指标、阈值、作业或停止动作。',
            context, schema)
    return plan, votes, handoffs, acceptance, policy, rh, amendments
