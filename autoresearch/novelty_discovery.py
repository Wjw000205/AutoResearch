"""One focused RAG-first discovery for an unnamed complete mechanism neighbor."""
from __future__ import annotations

import copy
import re

from .storage import now


async def complete(engine, harness, run_id, state, state_key, gate):
    from .engine import ResearchBlocked
    from .innovation_evidence import supplement
    questions = list(dict.fromkeys(q for vote in gate.get('votes', [])
        for q in vote.get('required_changes', []) if re.search(
            r'时变谐波|振荡状态|时间重参数化|最接近.{0,8}完整', q)))
    if not questions:
        return False
    job = state.get('mechanism_discovery')
    if job and job['status'] != 'pending':
        return False
    if job is None:
        job = {'status': 'pending', 'questions': questions, 'created_at': now(), 'version': 1}
        state['mechanism_discovery'] = job
        engine.store.put('stage_contexts', state_key, state)
    # Frozen handoffs, acquisition journals and accepted-note baselines in
    # supplement make a resumed pending discovery reuse its paid work.
    try:
        updated = await supplement(engine, harness, run_id, state['result'], gate, 'mechanism-1')
    except ResearchBlocked as exc:
        # Known terminal no-progress is not a license to buy another search.
        if str(exc).startswith('定向补读未新增有效原文摘录，'):
            job.update(status='no_new_evidence', completed_at=now(), reason=str(exc))
            engine.store.put('stage_contexts', state_key, state)
        raise
    job.update(status='completed', completed_at=now())
    state['history'].append({'round': 'mechanism-1', 'result': copy.deepcopy(state['result']),
                             'gate': copy.deepcopy(gate), 'completed_at': now()})
    state.update(result=updated, gate=None, review_supplement='mechanism-1')
    engine.store.put('stage_contexts', state_key, state)
    return True
