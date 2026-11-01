"""Bounded consolidation after acknowledged, exact-source full reading.

This is a Harness checkpoint, not a vote or a change to the frozen review
contract.  An already prepared recovery/paid request always finishes first.
"""
from __future__ import annotations

import copy

VERSION = 1
EXTRA_TOOL_TURNS = 2
STATE = 'indexed_review_closeout'
MARKER = '_indexed_review_closeout'


def enabled(context):
    from .implementation_indexed_review import CONTRACT, PROTOCOL
    return (type(context.get(PROTOCOL)) is int and context[PROTOCOL] == VERSION
            and isinstance(context.get(CONTRACT), dict)
            and context.get('indexed_document_review') is True)


def _identity(h, run_id, role, key, context):
    from .engine import ResearchBlocked, digest
    from .implementation_indexed_review import CONTRACT
    contract = context[CONTRACT]
    handoff = h.engine.store.get('handoffs', key, {})
    inputs = h.engine.store.get('handoff_inputs', key, {})
    payload = {name: inputs.get(name) for name in ('instruction', 'context', 'schema')}
    if (contract.get('run_id') != run_id or contract.get('role') != role
            or contract.get('sha256') != digest({k: v for k, v in contract.items() if k != 'sha256'})
            or handoff.get('id') != key or handoff.get('run_id') != run_id or handoff.get('to_role') != role
            or inputs.get('id') != key or inputs.get('run_id') != run_id or handoff.get('input_hash') != digest(payload)
            or digest(inputs.get('context')) != digest(context)
            or h.decision_key(run_id, role, handoff.get('purpose', ''), **payload) != key):
        raise ResearchBlocked('索引审核收尾的冻结文档或角色交接身份不一致；未派发请求。')
    return contract


def _protected(progress):
    """Do not retire any saved answer, pending tool, or frozen continuation."""
    if progress.get('tool_cursor', 0) < len(progress.get('pending_tool_calls', [])):
        return True
    normal = progress.get('normal_request') or {}
    return (normal.get('turn') == progress.get('turn')
            and normal.get('status') in {'dispatching', 'blocked', 'response_saved', 'ready'}
            and (normal.get('prepared') is not None or normal.get('response') is not None))


def _checked(state, run_id, role, key, contract):
    from .engine import ResearchBlocked
    if (not isinstance(state, dict) or type(state.get('version')) is not int or state['version'] != VERSION
            or state.get('phase') not in {'consolidating', 'final'}
            or (state.get('run_id'), state.get('role'), state.get('handoff_id')) != (run_id, role, key)
            or state.get('contract_sha256') != contract['sha256']
            or state.get('scientific_approval') is not False
            or type(state.get('started_turn')) is not int or type(state.get('deadline_turn')) is not int
            or not 0 <= state['started_turn'] <= state['deadline_turn'] <= state['started_turn'] + EXTRA_TOOL_TURNS
            or type(state.get('remaining_tool_turns')) is not int
            or not 0 <= state['remaining_tool_turns'] <= EXTRA_TOOL_TURNS
            or not isinstance(state.get('reading_proof_sha256'), str)
            or len(state['reading_proof_sha256']) != 64
            or any(char not in '0123456789abcdef' for char in state['reading_proof_sha256'])
            or (state['phase'] == 'final') != (state['remaining_tool_turns'] == 0)):
        raise ResearchBlocked('索引审核收尾状态身份或额度损坏；未重置额度或派发请求。')
    return state


def advance(h, run_id, role, key, context, progress):
    """Synchronous preview retains the same plan and commit behavior."""
    pending = plan_advance(h, run_id, role, key, context, progress)
    return commit_advance(h, run_id, role, key, progress, pending)


def plan_advance(h, run_id, role, key, context, progress):
    """Pure source/coverage validation; an async worker never writes closeout."""
    if not enabled(context) or h.engine.store.get('decisions', key):
        return False
    from .engine import ResearchBlocked, digest
    if ((progress.get('id', key), progress.get('run_id', run_id)) != (key, run_id)
            or type(progress.get('turn', 0)) is not int or progress.get('turn', 0) < 0):
        raise ResearchBlocked('索引审核收尾的读取账本身份或轮次损坏；未派发请求。')
    contract = _identity(h, run_id, role, key, context)
    previous = progress.get(STATE)
    if previous is not None:
        _checked(previous, run_id, role, key, contract)
        if progress.get('turn', 0) < previous['started_turn']:
            raise ResearchBlocked('索引审核收尾轮次已回退；未重置复核额度。')
    # Finalization may have been entered by the existing duplicate/hard limit.
    # Its saved prepared payload, including its original marker, is immutable.
    final = progress.get('finalization') or {}
    if progress.get('mode') == 'final_only':
        if final.get('prepared') is not None:
            return False
    elif _protected(progress):
        return False
    from .evidence_delivery import tracking_required
    if not tracking_required(h.engine.store, run_id, key):
        if previous is not None:
            raise ResearchBlocked('索引审核收尾的真实交付记录已缺失；未提交最终审核请求。')
        return False  # Legacy reads do not become invented Delivered credit.
    from .implementation_indexed_review import _reading
    try:
        # Revalidate at each of the at most three unsent closeout boundaries.
        # A cached "100%" must not survive changed source bytes or manifests.
        proof = _reading(h.engine, run_id, context, key)
    except ResearchBlocked as exc:
        if previous is None and hasattr(exc, 'indexed_review_missing_ranges'):
            return False
        raise
    if previous is None:
        started = progress.get('turn', 0)
        deadline = max(started, min(started + EXTRA_TOOL_TURNS,
                                   h._base_tool_turn_limit('', context, progress)))
        state = {'version': VERSION, 'phase': 'consolidating', 'run_id': run_id,
            'role': role, 'handoff_id': key, 'contract_sha256': contract['sha256'],
            'started_turn': started, 'deadline_turn': deadline,
            'remaining_tool_turns': deadline - started, 'reading_proof_sha256': digest(proof),
            'scientific_approval': False}
    else:
        state = copy.deepcopy(previous)
    state['remaining_tool_turns'] = max(0, state['deadline_turn'] - progress.get('turn', 0))
    if not state['remaining_tool_turns'] or progress.get('mode') == 'final_only':
        state.update(phase='final', remaining_tool_turns=0)
    return {'state': state, 'previous': copy.deepcopy(previous), 'expected': copy.deepcopy(progress)}


def commit_advance(h, run_id, role, key, progress, pending, *, guard=None):
    """Commit the checked plan on the caller, retaining the exact ledger CAS."""
    if pending is False:
        return False
    from .engine import ResearchBlocked, now
    if guard is not None:
        guard()
    state, previous, expected = pending['state'], pending['previous'], pending['expected']
    if state != previous:
        def claim(current):
            if guard is not None:
                guard()
            if current is None:
                current = {'id': key, 'run_id': run_id, **copy.deepcopy(expected)}
            # Do not overwrite a concurrently received response or tool receipt.
            if (current.get('id') != key or current.get('run_id') != run_id
                    or {k: v for k, v in current.items() if k not in {'id', 'run_id'}} != {
                    k: v for k, v in expected.items() if k not in {'id', 'run_id'}}):
                raise ResearchBlocked('索引审核收尾期间交接状态已变化；请恢复当前请求。')
            return {**current, STATE: state}
        progress.update(h.engine.store.mutate('handoff_progress', key, claim))
        if previous is None or previous['phase'] != state['phase']:
            h.engine.store.event(run_id, 'handoff.indexed_review_closeout',
                '全文证据已实际交付，开始有界整合审核。' if state['phase'] == 'consolidating'
                else '补充复核额度结束，提交最终审核意见。',
                {'handoff_id': key, 'role': role, **copy.deepcopy(state), 'created_at': now()})
    if state['phase'] == 'final' and progress.get('mode') != 'final_only':
        h._enter_final_only(run_id, key, progress,
            'Indexed full reading delivered; bounded consolidation completed, return the final independent review')
    return True


def prepare(run_id, role, key, progress, prepared):
    """Annotate only a newly compiled request, before notes are projected."""
    if not enabled(prepared['context']) or STATE not in progress:
        return prepared
    from .implementation_indexed_review import CONTRACT
    state = _checked(progress[STATE], run_id, role, key, prepared['context'][CONTRACT])
    prepared['context'][MARKER] = copy.deepcopy(state)
    if state['phase'] == 'consolidating':
        for branch in prepared['schema'].get('anyOf', []):
            names = (branch.get('properties', {}).get('tool_calls', {}).get('items', {})
                     .get('properties', {}).get('name', {}).get('enum'))
            if isinstance(names, list):
                names[:] = [name for name in names if name in {
                    'documents.list', 'documents.search', 'documents.read'}]
        prepared['task'] += ('\nCONTROLLER REVIEW CLOSEOUT: all frozen required documents have been fully '
            'delivered to this exact review handoff. Consolidate all of your saved observations and open issues '
            'into one complete independent review. Return the final stage schema now if possible. '
            f'At most {state["remaining_tool_turns"]} additional tool rounds remain; use existing document '
            'tools only for a decisive unresolved issue or exact anchor. Re-reading completed pages or adding '
            'notes does not extend this allowance. Reading completion is not approval.')
    else:
        prepared['task'] += ('\nCONTROLLER REVIEW CLOSEOUT: consolidate all saved observations and return '
            'one final independent review using the original stage schema. No further tools. '
            'Preserve unresolved objections; full reading is not approval and does not bypass evidence validation.')
    return prepared
