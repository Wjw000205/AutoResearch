"""Resume an independent artifact review with the same role's missing pages."""
import copy

from . import experiment_evidence_handoff as handoff
from .diagnostic_planning_recovery import authenticate
from .experiment_workflow import sha


def assignment(review, role):
    if role in review.get('full_assignments', {}):
        return review['full_assignments'][role]
    requests = review.get('page_assignments', {}).get(role, [])
    return requests[-1] if requests else review['assignment']


def _authenticate(engine, run_id, role, request, answer):
    return authenticate(engine, run_id, role, request['purpose'],
                        {k: request[k] for k in ('instruction', 'context', 'schema')}, answer)


def _continuation(base, role, owners, answer, pages):
    packet = {'role': role, 'source_handoffs': list(owners), 'previous_answer': answer,
              'missing_pages': pages, 'execution_authorized': False}
    context = copy.deepcopy(base['context'])
    context.setdefault('artifact_review_request', {})['page_continuation'] = packet
    if base['purpose'].startswith('experiment_vote:'):
        return {'purpose': base['purpose'], 'context': context, 'schema': base['schema'],
                'instruction': '接续本人对当前提案的独立投票。按 artifact_review_request.page_continuation.missing_pages '
                    '补读对应补充快照的缺页并记录带回执的笔记。结合新页重新核对 previous_answer，'
                    '返回针对原 proposal_sha256 的完整独立投票。已有读取可复用，旧票不自动生效。'}
    return {'purpose': base['purpose'] + ':missing-pages:' + sha(packet),
            'context': context, 'schema': base['schema'],
            'instruction': '接续你本人已完成的产物复核。先按 artifact_review_request.page_continuation.missing_pages'
                ' 给出的 path、offset、limit，用 experiments.compare 读取 artifact_evidence_handoff.evidence 的缺页。'
                '宿主已核验旧回执，已覆盖的页直接复用。每批阅读后用 experiments.record_notes 保存简短观察及真实回执。'
                '检查新页是否影响 previous_answer 的结论，返回完整更新后的独立复核 JSON。保留真实未知。'
                '此次补读不授权训练或参数变更。'}


def inspect(engine, run_id, role, review, first_answer=None):
    """Replay the exact role/answer chain before joining any page receipts."""
    base = review['assignment']
    successors = review.get('page_assignments', {}).get(role, [])
    owners, recent_gaps = [], []
    for index, request in enumerate([base, *successors]):
        key = handoff.assignment_key(engine, run_id, role, **request)
        decision = engine.store.get('decisions', key) or {}
        answer = first_answer if index == 0 and first_answer is not None else decision.get('result')
        if successors:
            _authenticate(engine, run_id, role, request, answer)
        try:
            proof = handoff.require_reads(engine, run_id, role, **request, prior_handoffs=owners)
        except handoff.MissingArtifactPages as gap:
            if not successors:
                # An unowned or uncommitted judgment cannot seed a new request.
                try:
                    _authenticate(engine, run_id, role, request, answer)
                except ValueError as exc:
                    raise ValueError(f'{gap}; {exc}') from exc
            owners.append(key)
            recent_gaps.append(gap.pages)
            if len(recent_gaps) >= 4 and all(p == gap.pages for p in recent_gaps[-4:]):
                raise ValueError(f'{gap}; three same-role continuations produced no reading progress') from gap
            next_request = _continuation(base, role, owners, answer, gap.pages)
            if index == len(successors):
                return None, None, next_request
            if successors[index] != next_request:
                raise ValueError('Artifact continuation no longer matches the original answer and missing pages')
        else:
            if index != len(successors):
                raise ValueError('Artifact read history changed after a continuation was assigned')
            return answer, proof, None


def verified_reads(engine, run_id, role, review):
    if role in review.get('full_assignments', {}):
        from . import independent_full_review as full
        result = next(r for r in review['results'] if r['role']==role)
        full.authenticate(engine.store,run_id,result)
        full.validate_delivery(engine.store,run_id,result,engine=engine)
        req = review['full_assignments'][role]
        ids = full.reading_handoffs(engine.store,run_id,result)
        return handoff.require_reads(engine,run_id,role,**req,prior_handoffs=ids[1:])
    if not review.get('page_assignments', {}).get(role):
        return handoff.require_reads(engine, run_id, role, **review['assignment'])
    answer, proof, pending = inspect(engine, run_id, role, review)
    result = next(r for r in review['results'] if r['role'] == role)
    if pending or result != {'role': role, **(answer or {})}:
        raise ValueError('Saved artifact review does not match the completed page continuation')
    return proof


async def finish_pages(engine, harness, run_id, role, base, answer, saved, save):
    """Resume only missing intervals and persist each assignment before dispatch."""
    assignments = list(saved)
    count = 0
    while True:
        prefix = {'assignment': base, 'page_assignments': {role: assignments[:count]}}
        result, proof, pending = await engine._read_consensus_check(
            run_id, inspect, engine, run_id, role, prefix, answer)
        if not pending:
            if count != len(assignments):
                raise ValueError('Completed artifact reads conflict with saved continuation history')
            return {'role': role, **result}, proof
        if count < len(assignments):
            if pending != assignments[count]:
                raise ValueError('Artifact page assignment changed before resume')
        else:
            assignments.append(pending)
            await save(list(assignments))
        await harness.ask(run_id, role, **pending)
        count += 1
