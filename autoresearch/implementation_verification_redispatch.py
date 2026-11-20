"""Authenticate failed verification handoffs before a new contract dispatch."""
import copy

from . import implementation_acceptance as policy
from . import implementation_acceptance_flow as flow
from .implementation_review_routing import digest
from .implementation_verification_contract import validate, SCOPE


def predecessors(engine, run_id, saved, legacy_scope):
    from .codex_workflow import _frozen_spec_assignment
    from .implementation_delivery_correction import REPAIR
    from .engine import ResearchBlocked
    purpose = 'implementation_acceptance_verification_clarification:' + digest(legacy_scope)
    original = _frozen_spec_assignment(engine,run_id,purpose,'engineer')
    if original is None:
        return [], None
    if original['context'].get(SCOPE) != legacy_scope:
        policy.fail('Verification predecessor differs from its exact frozen scope')
    queue, visited, references, valid = [original['id']], set(), [], []
    while queue:
        hid = queue.pop(0)
        if hid in visited:
            policy.fail('Verification correction lineage contains a cycle')
        visited.add(hid)
        hand = engine.store.get('handoffs',hid,{})
        inputs = engine.store.get('handoff_inputs',hid,{})
        decision = engine.store.get('decisions',hid,{})
        if hand.get('status') != 'completed' or decision.get('result') is None:
            raise ResearchBlocked('原核验范围请求尚无确定终态；须先经普通恢复核实原回执，未重复派发。')
        packet = {'handoff_id':hid,'role':'engineer','context':inputs.get('context',{}),
                  'result':decision['result']}
        flow.check_packet(engine,run_id,packet)
        reference = {'handoff_id':hid,'input_hash':hand['input_hash'],
            'output_hash':hand['output_hash'],'result_sha256':digest(decision['result']),
            'original_result':copy.deepcopy(decision['result'])}
        try:
            validate(saved,packet)
            valid.append(packet)
            reference['validation_status'] = 'valid'
        except ResearchBlocked as exc:
            diagnostic = getattr(exc,'acceptance_delivery_error',None)
            if diagnostic is None:
                raise
            reference.update(validation_status='invalid',diagnostic=copy.deepcopy(diagnostic))
        references.append(reference)
        children = engine.store.record_keys('handoff_inputs',run_id,
            predicate="json_extract(data,'$.context." + REPAIR + ".source_handoff_id')=?",
            parameters=[hid])
        queue.extend(sorted(children))
    # Reuse one unambiguous valid historical assignment. Conflicting valid
    # answers require the new ordinary clarification, never a convenient vote.
    return references, valid[0] if len(valid) == 1 else None


def authenticate(engine, run_id, references):
    from .harness_delivery import authenticate as authenticate_delivery
    for ref in references:
        hand = engine.store.get('handoffs',ref['handoff_id'],{})
        result = engine.store.get('decisions',ref['handoff_id'],{}).get('result')
        if (hand.get('input_hash') != ref['input_hash'] or hand.get('output_hash') != ref['output_hash']
                or digest(result) != ref['result_sha256'] or result != ref['original_result']):
            policy.fail('Verification predecessor input, reply or terminal receipt changed')
        authenticate_delivery(engine,run_id,'engineer',ref['handoff_id'],result)
