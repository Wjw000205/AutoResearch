"""Implementation specification blockers do not revise the scientific proposal."""

from .engine import ResearchBlocked


SPECIFICATION_FAILURE_KINDS = frozenset({
    'codex_spec_rejected', 'codex_spec_no_progress', 'codex_spec_review_budget_exhausted',
})
CODE_FAILURE_KINDS = frozenset({'codex_code_no_progress', 'codex_delivery_rejected',
    'codex_protocol_conflict', 'codex_execution_evidence_invalid', 'codex_repair_unverified',
    'codex_definition_required', 'codex_verification_pending'})
IMPLEMENTATION_FAILURE_KINDS = SPECIFICATION_FAILURE_KINDS | CODE_FAILURE_KINDS


class ImplementationSpecificationPending(Exception):
    """One reviewed version is saved; the outer graph dispatches its successor."""


class ImplementationSpecificationBlocked(ResearchBlocked):
    """Retain exact implementation dissent without starting a research cycle."""

    def __init__(self, message, *, kind, details=None):
        if kind not in SPECIFICATION_FAILURE_KINDS:
            raise ValueError('Unknown implementation specification failure kind')
        super().__init__(message)
        self.failure = {'kind': kind, 'message': message, 'details': details or {}}


class ImplementationCodeBlocked(ResearchBlocked):
    """A delivery/resource gate cannot invalidate an approved scientific plan."""

    def __init__(self, message, *, kind, details=None):
        if kind not in CODE_FAILURE_KINDS:
            raise ValueError('Unknown implementation code failure kind')
        super().__init__(message)
        self.failure = {'kind': kind, 'message': message, 'details': details or {}}


class ImplementationDeliveryCorrection(ImplementationCodeBlocked):
    """An authenticated delivery needs correction under its existing scope.

    Identity, source and approval failures must use ImplementationCodeBlocked
    directly; they must never be turned into new code-edit authority.
    """

    def __init__(self, message, *, diagnostic, **details):
        super().__init__('代码交付需要修正：' + message, kind='codex_repair_unverified',
                         details={'diagnostic': diagnostic, **details})


def block(engine, run_id, failure, step):
    """Keep an implementation blocker in this stage without revising science."""
    from .orchestration import STAGES
    from .storage import now, redact
    failure = redact(failure)
    failure = {**failure, 'details': {key: value for key, value in failure.get('details', {}).items()
                                    if key != 'specifications'}}
    current = {key: value for key, value in step.items()
               if key not in {'failure_review', 'completed_at', 'next_node'}}
    code = failure['kind'] in CODE_FAILURE_KINDS
    waiting = 'implementation_code' if code else 'implementation_specification'
    current.update(status='blocked', output=None, implementation_failure=failure,
                   waiting_for=waiting)
    engine.store.batch([
        ('steps', step['id'], current),
        ('graph_status', run_id, {'id': run_id, 'run_id': run_id,
            'node': 'implementation', 'cycle': engine.cycle(run_id), 'status': 'blocked',
            'waiting_for': waiting, 'error': failure['message'], 'updated_at': now()}),
    ])
    engine.store.event(run_id, 'implementation.code_blocked' if code else 'implementation.specification_blocked', failure['message'],
        {'kind': failure['kind'], 'workflow_id': failure.get('details', {}).get('workflow_id')})
    engine.update_run(run_id, stage='implementation',
                     progress=round(STAGES.index('implementation') / len(STAGES) * 100))
    blocker = ImplementationCodeBlocked if code else ImplementationSpecificationBlocked
    raise blocker(failure['message'], kind=failure['kind'],
                                             details=failure.get('details'))
