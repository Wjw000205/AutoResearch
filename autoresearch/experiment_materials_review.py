"""Use the ordinary Harness to review a capability while research is paused."""
from __future__ import annotations

import copy


async def execute(engine, run_id, workflow, paused_run, harness_type, settings):
    from .document_efficiency_recovery import adopt
    from .storage import Store
    from .engine import ResearchBlocked

    class ReviewHarness(harness_type):
        async def ask(self, key, role, purpose, instruction, context, schema):
            if (key != run_id or role not in {'director', 'scout'}
                    or not purpose.startswith('document_efficiency_review:')
                    or not context.get('work_order_id', '').startswith(run_id + ':document_efficiency:')):
                raise ResearchBlocked('Paused capability review cannot dispatch a research assignment')
            return await super().ask(key, role, purpose, instruction, context, schema)

    class ReviewStore(Store):
        def get(self, collection, key, default=None):
            value = super().get(collection, key, default)
            if collection == 'runs' and key == run_id:
                if value != paused_run:
                    raise ValueError('Paused research changed during capability review')
                return {**value, 'status': 'running'}
            return value

    store = ReviewStore.__new__(ReviewStore)
    store.path, store._lock = engine.store.path, engine.store._lock
    reviewer = copy.copy(engine)
    reviewer.store = store
    reviewer.check_active = lambda key: store.get('runs', key)
    intelligence = engine.intelligence_factory(store, settings)
    work = await adopt(reviewer, ReviewHarness(reviewer, intelligence), run_id, workflow)
    if engine.store.get('runs', run_id) != paused_run:
        raise ValueError('Research pause changed during independent capability review')
    return {'work_order_id': work['id'], 'status': work['status'], 'research_status': 'paused',
            'review_roles': sorted(work['reviews'])}
