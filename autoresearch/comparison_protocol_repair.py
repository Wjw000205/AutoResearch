"""Explicit, source-bound host compatibility repairs; never rewrite a SPEC vote."""
import copy

from .storage import now
from .architecture_validation import operation

COLLECTION = 'comparison_protocol_repairs'
PROFILE = 'declared_diagnostics_v1'


def extend(contract, declarations):
    from .comparison_transport import validate_contract
    candidate = copy.deepcopy(contract)
    known = {row['path']: row for row in candidate['payload_arrays']}
    if not isinstance(declarations, list) or not declarations:
        raise ValueError('Approved diagnostic array declarations are required')
    for row in declarations:
        if not isinstance(row, dict) or not str(row.get('path', '')).startswith('diagnostics.'):
            raise ValueError('Diagnostic additions must use diagnostic paths')
        if row['path'] in known and known[row['path']] != row:
            raise ValueError('Diagnostic repair conflicts with an existing declaration')
        if row['path'] not in known:
            candidate['payload_arrays'].append(row)
            known[row['path']] = row
    return validate_contract(candidate)


def _definition_inheritance(workflow, bindings, receipt, reviewed_definition):
    """Transfer only an unchanged decoder contract across an approved addition.

    runtime_transport authenticates the complete current release before passing
    reviewed_definition. Original receipt, SPEC and reviews remain immutable.
    """
    from . import implementation_definition_repair as definitions
    from .comparison_transport import frozen_digest
    row = reviewed_definition
    if (not isinstance(row, dict) or row.get(definitions.PROTOCOL) != 1
            or row.get('approved') is not True or row not in workflow.get('specifications', [])
            or row.get('spec_hash') != bindings.get('spec_hash')
            or frozen_digest(row.get('spec')) != row.get('spec_hash')
            or row.get('document', {}).get('sha256') != bindings.get('implementation_document_sha256')):
        return None
    chains = [workflow.get(definitions.FIELD, {}), *workflow.get('definition_repair_history', [])]
    matches = [r for r in chains if r.get('id') == row.get('definition_repair_id')]
    if len(matches) != 1 or matches[0].get('status') != 'approved' or matches[0].get('approved_index') != row.get('index'):
        return None
    record = matches[0]
    state = {**workflow, definitions.FIELD: record}
    base = definitions.base_row(state, record)
    definitions.validate_candidate(row['spec'], state, definitions.binding(state))
    original_bindings = {**bindings, 'spec_hash': base['spec_hash'],
                         'implementation_document_sha256': base['document']['sha256']}
    if (receipt.get('bindings') != original_bindings
            or frozen_digest(base['spec']) != base['spec_hash']):
        return None
    return {'definition_repair_id': record['id'], 'base_index': base['index'],
            'approved_index': row['index'], 'original_bindings': copy.deepcopy(original_bindings),
            'current_bindings': copy.deepcopy(bindings)}


def apply(engine, run_id, workflow, transport, *, reviewed_definition=None):
    from .comparison_transport import digest
    key = workflow['id'] + ':diagnostic-protocol-v1'
    receipt = engine.store.get(COLLECTION, key)
    if not receipt:
        return transport
    body = {k: v for k, v in receipt.items() if k != 'sha256'}
    candidate = extend(transport['contract'], receipt.get('declarations'))
    inheritance = None
    if receipt.get('bindings') != transport['bindings']:
        inheritance = _definition_inheritance(workflow, transport['bindings'], receipt, reviewed_definition)
    if (receipt.get('sha256') != digest(body) or not isinstance(receipt.get('profile'), str)
            or receipt.get('run_id') != run_id or receipt.get('workflow_id') != workflow['id']
            or (receipt.get('bindings') != transport['bindings'] and inheritance is None)
            or receipt.get('original_contract_sha256') != digest(transport['contract'])
            or receipt.get('effective_contract_sha256') != digest(candidate)):
        raise ValueError('Diagnostic protocol repair no longer matches the frozen evidence')
    result = {**transport, 'contract': candidate, 'protocol_repair': {
        'id': key, 'sha256': receipt['sha256'], 'profile': receipt['profile'],
        'original_contract_sha256': receipt['original_contract_sha256'],
        'effective_contract_sha256': receipt['effective_contract_sha256'],
        'scope': 'Host decoding declarations only; original SPEC and votes retained. Diagnostic semantics require independent code review.'}}
    if inheritance is not None:
        result['protocol_repair']['binding_inheritance'] = inheritance
    return result


@operation
def install(engine, run_id, workflow_id, expected_document_sha256):
    """Operator-authorized repair, only on a stopped, independently reviewed SPEC."""
    from .comparison_transport import runtime_transport, digest
    run = engine.store.get('runs', run_id)
    if not run or run.get('status') not in {'paused', 'blocked', 'failed'}:
        raise ValueError('Pause the task before reconciling its host protocol')
    if engine.store.get('runtime', 'worker', {}).get('held'):
        raise ValueError('The execution lease has not been released')
    workflow = engine.store.get('codex_workflows', workflow_id)
    if not workflow or workflow.get('run_id') != run_id:
        raise ValueError('Unknown code workflow')
    rows = [s for s in workflow['specifications'] if s.get('approved') is True]
    if len(rows) != 1 or rows[0].get('document', {}).get('sha256') != expected_document_sha256:
        raise ValueError('The approved implementation document changed')
    spec = rows[0]['spec']
    declarations = spec.get('diagnostic_payload_arrays')
    transport = runtime_transport(engine, run_id, spec=spec)
    key = workflow_id + ':diagnostic-protocol-v1'
    previous = engine.store.get(COLLECTION, key)
    if previous:
        return previous  # runtime_transport already authenticates the receipt.
    candidate = extend(transport['contract'], declarations)
    receipt = {'id': key, 'run_id': run_id, 'workflow_id': workflow_id, 'profile': PROFILE,
        'bindings': transport['bindings'], 'original_contract_sha256': digest(transport['contract']),
        'effective_contract_sha256': digest(candidate), 'declarations': copy.deepcopy(declarations),
        'created_at': now(), 'authorization': 'Explicit operator request to reconcile diagnostic protocol and reuse saved executions',
        'scientific_approval': False, 'original_spec_and_reviews_preserved': True}
    receipt['sha256'] = digest(receipt)
    engine.store.put(COLLECTION, key, receipt)
    engine.store.event(run_id, 'implementation.protocol_repaired',
        '诊断数组传输声明已补齐；原文档与审核保留，代码仍须独立审核',
        {'repair_id': key, 'repair_sha256': receipt['sha256'], 'added_arrays': len(declarations)})
    return receipt
