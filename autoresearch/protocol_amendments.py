"""Route a rejected premeasurement protocol to its author and review the successor."""
from . import protocol_review_recovery as reviews


def revision_hash(plan, acceptance, policy, threshold):
    from .engine import digest
    return digest({'plan': plan, 'acceptance': acceptance, 'iteration_policy': policy,
                   'task_primary_threshold': threshold})


def replay_threshold(engine, rid, record, current):
    """Use the authenticated frozen numeric spelling only at an equal value.

    Historical hashes distinguish 0 from 0.0. Do not rewrite them or infer a
    replacement approval; the original three handoffs own the representation.
    """
    from .engine import ResearchBlocked, digest
    from .implementation_alignment_evidence import _delivery
    handoffs = record.get('protocol_handoffs') or {}
    if not handoffs:
        return current
    scopes = [engine.store.get('handoff_inputs', hid, {}).get('context', {}) for hid in handoffs.values()]
    if not any('task_primary_threshold' in scope for scope in scopes):
        return current  # Retain legacy protocols without an explicit field.
    from .independent_full_review import members, reviewers
    votes = record.get('protocol_votes', [])
    if not members(votes) or set(handoffs) != set(reviewers(votes)):
        raise ResearchBlocked('冻结实验阈值缺少三方原始交接。')
    frozen = []
    for role, hid in handoffs.items():
        decision = engine.store.get('decisions', hid, {})
        scope = _delivery(engine, rid, role, hid, decision.get('result', {}))
        value = scope.get('task_primary_threshold')
        if (type(current) not in (int, float) or type(value) not in (int, float)
                or not 0 <= current <= 1 or not 0 <= value <= 1 or current != value):
            raise ResearchBlocked('当前科学验收阈值与原三方批准数值不同，不能复用原协议。')
        frozen.append(value)
    if len({digest(value) for value in frozen}) != 1:
        raise ResearchBlocked('原三方协议的冻结阈值表示不一致。')
    return frozen[0]


def packet(context, rejected):
    """Pin the current template, the complete job inventory and the approved source."""
    from .engine import digest
    docs = context['indexed_documents']
    template = next(d for d in docs if d['kind'] == 'architecture_design')
    proposal = next(d for d in docs if d['kind'] == 'approved_proposal')
    return {**context, 'protocol_amendment_parent': rejected['revision_hash'],
            'protocol_objections': rejected['votes'], 'protocol_objections_sha256': digest(rejected['votes']),
            'architecture_workflow_protocol': 1, 'document_read_window_protocol': 3,
            'architecture_required_documents': [context['protocol_document']['document_id'],
                                                template['document_id'], proposal['document_id']]}


async def seed(engine, h, rid, hid, ctx):
    if engine.store.get('handoff_progress', hid) or engine.store.get('decisions', hid):
        return
    from .architecture_documents import units
    from .architecture_validation import operation
    def pages():
        docs = {d['document_id']: d for d in ctx['indexed_documents']}
        return [page for did in ctx['architecture_required_documents']
                for group in units(engine, rid, docs[did]) for page in group]
    receipts = await engine._read_consensus_check(rid, operation(pages))
    h._save_progress(rid, hid, {'turn': 0, 'seen': {}, 'tool_results': receipts})


def materialize(engine, rid, rejected, hid, acceptance, policy):
    from .engine import Harness, ResearchBlocked, digest
    reviews.checked_votes(engine, rejected)
    frozen = engine.store.get('handoff_inputs', hid, {})
    assignment = engine.store.get('handoffs', hid, {})
    decision = engine.store.get('decisions', hid, {})
    ctx = frozen.get('context', {})
    parent = rejected['revision_hash']
    if (assignment.get('to_role') != 'engineer' or decision.get('role') != 'engineer'
            or decision.get('run_id') != rid or assignment.get('status') != 'completed'
            or assignment.get('purpose') != 'protocol_amendment:' + parent
            or ctx.get('design_hash') != rejected['design_hash'] or ctx.get('proposal_hash') != rejected['proposal_hash']
            or ctx.get('revision_hash') != parent or ctx.get('protocol_objections') != rejected['votes']
            or ctx.get('protocol_document') != rejected['context']['protocol_document']
            or Harness.decision_key(rid, 'engineer', assignment['purpose'], frozen['instruction'], ctx, frozen['schema']) != hid):
        raise ResearchBlocked('协议修订缺少原工程师、父协议或三方修改意见的绑定。')
    reply = decision['result']
    if reply['parent_revision_hash'] != parent or reply['requires_design_change']:
        raise ResearchBlocked('协议修改需要处理设计范围或版本绑定：' + reply['change_summary'])
    return ({**acceptance, 'complete': reply['criteria_complete'], 'limitations': reply['limitations']},
            {**policy, 'rationale': reply['iteration_rationale']})


async def amend(engine, h, rid, rejected, acceptance, policy):
    from .engine import STR, STRS, obj
    ctx = packet(rejected['context'], rejected)
    parent = rejected['revision_hash']
    purpose = 'protocol_amendment:' + parent
    instruction = ('根据三方 protocol_objections 修订实验协议。本次提供完整模板、作业清单和批准方案原文。'
        '逐项核对修改要求，在 limitations、iteration_rationale 和 change_summary 中给出精确来源位置及处理结果。'
        'criteria_complete 仅表示定量条款是否完整。区分执行故障、负结果、修订和方法族停止。'
        '本次仅可澄清上述条款；如需更改模型、作业、指标、阈值或停止动作，requires_design_change=true 并说明。'
        '区分已有检查证据及待验证条件，不得声称实验通过。返回原 parent_revision_hash。')
    schema = obj({'parent_revision_hash': {'const': parent}, 'criteria_complete': {'type': 'boolean'},
                  'limitations': STRS, 'iteration_rationale': STR, 'change_summary': STR,
                  'requires_design_change': {'type': 'boolean'}})
    hid = rejected.get('amendment_handoff_id')
    if hid:
        frozen = engine.store.get('handoff_inputs', hid)
        instruction, ctx, schema = (frozen[k] for k in ('instruction', 'context', 'schema'))
    else:
        hid = h._handoff(rid, 'engineer', purpose, instruction, ctx, schema)['id']
        rejected.update(status='revising', amendment_handoff_id=hid)
        engine.store.put(reviews.COLLECTION, rejected['id'], rejected)
        engine.store.event(rid, 'experiment.protocol_amendment_dispatched',
            '三方协议意见已派发给实验工程师修改。', {'handoff_id': hid, 'parent_revision_hash': parent})
    await seed(engine, h, rid, hid, ctx)
    await h.ask(rid, 'engineer', purpose, instruction, ctx, schema)
    return materialize(engine, rid, rejected, hid, acceptance, policy)


def replay(engine, rid, record, plan, acceptance, policy, threshold, design_hash, proposal_hash):
    from .engine import ResearchBlocked
    for hid in record.get('protocol_amendments', []):
        parent = revision_hash(plan, acceptance, policy, threshold)
        rejected = engine.store.get(reviews.COLLECTION, reviews.key(rid, parent), {})
        if (rejected.get('amendment_handoff_id') != hid or rejected.get('design_hash') != design_hash
                or rejected.get('proposal_hash') != proposal_hash):
            raise ResearchBlocked('协议修订链与当前设计、方案或原评议不一致。')
        acceptance, policy = materialize(engine, rid, rejected, hid, acceptance, policy)
    return acceptance, policy


async def review(engine, h, rid, design, plan, acceptance, policy, purpose, instruction, ctx, schema):
    from .architecture_revisions import protocol_context_async
    from .engine import ResearchBlocked
    chain, seen = [], set()
    base_purpose, base_instruction = purpose, instruction
    while True:
        parent = ctx['revision_hash']
        if parent in seen:
            raise ResearchBlocked('协议修改未产生新版本；原意见和工程师交付均已保留。')
        seen.add(parent)
        rejected = engine.store.get(reviews.COLLECTION, reviews.key(rid, parent))
        if not rejected or rejected.get('status') not in ('blocked', 'revision_requested', 'revising') or rejected.get('segmented_review'):
            votes, ids = await reviews.review(engine, h, rid, purpose, instruction, ctx, schema)
            if all(reviews.accepted(v, ctx) for v in votes):
                return votes, ids, acceptance, policy, parent, chain
            rejected = reviews.reject(engine, rid, purpose, instruction, ctx, votes, ids, request_revision=True)
        reviews.checked_votes(engine, rejected)
        if any(rejected[k] != ctx[k] for k in ('revision_hash', 'design_hash', 'proposal_hash')):
            raise ResearchBlocked('协议退修的方案或版本已改变。')
        if (any(v.get('revision_hash') != parent or v.get('proposal_hash') != ctx['proposal_hash']
                for v in rejected['votes']) or not any(v.get('required_changes') for v in rejected['votes'])):
            raise ResearchBlocked('协议意见缺少同版本绑定或具体修改要求，需核对评审交付。')
        acceptance, policy = await amend(engine, h, rid, rejected, acceptance, policy)
        chain.append(rejected['amendment_handoff_id'])
        successor = revision_hash(plan, acceptance, policy, ctx['task_primary_threshold'])
        if successor in seen:
            raise ResearchBlocked('工程师交付与已有协议版本相同；修改意见和交付已保存。')
        ctx = await protocol_context_async(engine, h, rid, design, plan, acceptance, policy, ctx['proposal_hash'], successor)
        ctx = packet(ctx, rejected)
        ctx['amendment_summary'] = engine.store.get('decisions', chain[-1])['result']['change_summary']
        purpose = base_purpose + ':amended:' + successor
        instruction = base_instruction + (
            '\n本版协议修订说明：以新 protocol_document 中的验收与停止条款作为原架构相应条款的待批准勘误，'
            '独立核对完整模板、作业清单、批准方案及全部退修意见。仅对本版投票。')
