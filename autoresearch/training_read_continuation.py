"""Carry actual same-role reads into a correction without inventing read credit."""
import copy

from .experiment_workflow import sha


def retained_handoff(context):
    decision = context.get('experiment_decision', {})
    return (decision.get('runtime_audit_read_repair', {}).get('source_handoff_id')
            or decision.get('training_read_repair', {}).get('retained_reads', {}).get('handoff_id'))


def origin(engine, run_id, role, context, instruction, schema):
    from .engine import Harness
    purpose = 'experiment_diagnose:' + context['evidence_sha256']
    key = Harness.decision_key(run_id, role, purpose, instruction, context, schema)
    handoff = engine.store.get('handoffs', key) or {}
    frozen = engine.store.get('handoff_inputs', key) or {}
    if handoff.get('run_id') != run_id or handoff.get('to_role') != role or frozen.get('context') != context:
        return None
    rows = _valid_rows(engine, key, context['evidence_sha256'], run_id=run_id, context=context)
    if not rows:
        return None
    return {'handoff_id': key, 'input_sha256': sha(frozen),
            'reads': [{'call': row['call'], 'receipt_sha256': row['result']['receipt_sha256'],
                       **{k: row['result'][k] for k in ('offset', 'end', 'total_chars', 'source_read')
                          if k in row['result']}} for row in rows],
            'scope': 'Actual earlier reads by this role on this exact evidence. Cite only reads used; unread pages remain unread.'}


def _valid_rows(engine, key, evidence_sha, *, run_id, context):
    from .experiment_decisions import _supplementary_analysis_read
    progress = engine.store.get('handoff_progress', key) or {}
    result = []
    for row in progress.get('tool_results', []):
        call, value = row.get('call') or {}, row.get('result') or {}
        if (not value.get('text') or value.get('receipt_sha256') != sha({'call': call,
                'result': {k: v for k, v in value.items() if k != 'receipt_sha256'}})):
            continue
        if call.get('name') in {'experiments.analyze', 'experiments.inspect_module'}:
            bound = value.get('evidence_sha256') == evidence_sha
        else:
            bound = _supplementary_analysis_read(engine, run_id, context, call, value)
        if bound:
            result.append(row)
    return result


def retained_rows(engine, run_id, role, context):
    """Check immutable parent ownership and context before accepting citations."""
    from .engine import ResearchBlocked
    audit_repair = context.get('experiment_decision', {}).get('runtime_audit_read_repair')
    if audit_repair:
        expected = copy.deepcopy(context)
        expected['experiment_decision'].pop('runtime_audit_read_repair')
        hid = audit_repair['source_handoff_id']
        frozen = engine.store.get('handoff_inputs', hid) or {}
        answer = (engine.store.get('decisions', hid) or {}).get('result')
        if (frozen.get('context') != expected or sha(frozen) != audit_repair['source_input_sha256']
                or sha(answer) != audit_repair['source_answer_sha256']):
            raise ResearchBlocked('Retained runtime audit reads lost their exact parent context')
        from .diagnostic_planning_recovery import authenticate
        parent = engine.store.get('handoffs', hid) or {}
        try:
            verified = authenticate(engine, run_id, role, parent.get('purpose'),
                {k: frozen.get(k) for k in ('instruction','context','schema')}, answer)
        except (ValueError, KeyError, TypeError) as exc:
            raise ResearchBlocked('Retained runtime audit reads must belong to the same role and exact completed parent') from exc
        if verified != hid:
            raise ResearchBlocked('Retained runtime audit parent identity changed')
        return retained_rows(engine, run_id, role, expected) + _valid_rows(
            engine, hid, context['evidence_sha256'], run_id=run_id, context=expected)
    repair = context.get('experiment_decision', {}).get('training_read_repair') or {}
    saved = repair.get('retained_reads')
    if not saved:  # Existing frozen corrections retain their original contract.
        return []
    parent = engine.store.get('handoffs', saved['handoff_id']) or {}
    frozen = engine.store.get('handoff_inputs', saved['handoff_id']) or {}
    expected = copy.deepcopy(context)
    expected['experiment_decision'].pop('training_read_repair', None)
    if (parent.get('run_id') != run_id or parent.get('to_role') != role
            or parent.get('purpose') != 'experiment_diagnose:' + context['evidence_sha256']
            or sha(frozen) != saved['input_sha256'] or frozen.get('context') != expected):
        raise ResearchBlocked('Retained training reads must belong to the same role and exact frozen context')
    allowed = {r['receipt_sha256'] for r in saved['reads']}
    # Legacy indexes omitted compare/read pages although the same parent had
    # delivered them. Recover only those supplementary receipts, revalidated
    # against the frozen evidence above. They cannot replace training analysis
    # or source reads, and neither the old index nor the paid answer is edited.
    return [r for r in _valid_rows(engine, saved['handoff_id'], context['evidence_sha256'],
                                  run_id=run_id, context=expected)
            if r['result']['receipt_sha256'] in allowed
            or r['call']['name'] in {'experiments.compare', 'experiments.read'}]
