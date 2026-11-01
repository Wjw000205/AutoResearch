"""Credit authentic same-author source reads made while forming a proposal."""
from __future__ import annotations

import copy

from .prose_schema import validate

from .experiment_workflow import sha


ERROR = 'All three agents must read the affected training evidence and relevant module source before this intervention'
KEY = 'training_proposal_reads'


def retained_proposal(record):
    from .training_read_dispatch import retained_proposal as pending_read_proposal
    pending = pending_read_proposal(record)
    if pending:
        return pending
    proposal = record.get('rejected_proposal') or {}
    if (record.get('status') == 'invalid' and record.get('rejection') == ERROR
            and not record.get('proposal') and not record.get('votes')
            and proposal.get('evidence_sha256') == record.get('evidence', {}).get('sha256')):
        return copy.deepcopy(proposal)
    return None


def author_reads(engine, run_id, proposal, ref, missing):
    """Revalidate saved tool results against the exact frozen source bytes.

    Only the completed engineer assignment that produced this exact answer can
    supply credit. No analysis, other role's reads, or approval is synthesized.
    """
    from .engine import Harness, digest
    from .experiment_analysis import inspect_module

    prefix = 'experiment_decide:' + ref['sha256']
    keys = engine.store.record_keys('decisions', run_id,
        predicate="json_extract(data,'$.role')=? AND json_extract(data,'$.purpose') LIKE ?",
        parameters=('engineer', prefix + '%'))
    for hid in reversed(keys):
        answer = engine.store.get('decisions', hid)
        if answer.get('result') != proposal:
            continue
        assignment = engine.store.get('handoffs', hid, {})
        inputs = engine.store.get('handoff_inputs', hid, {})
        payload = {k: inputs.get(k) for k in ('instruction', 'context', 'schema')}
        purpose = answer.get('purpose', '')
        if (purpose != prefix and not purpose.startswith(prefix + ':')
                or assignment.get('id') != hid or assignment.get('run_id') != run_id
                or assignment.get('to_role') != 'engineer' or assignment.get('purpose') != purpose
                or assignment.get('status') != 'completed' or assignment.get('output_hash') != digest(proposal)
                or inputs.get('id') != hid or inputs.get('run_id') != run_id
                or assignment.get('input_hash') != digest(payload)
                or Harness.decision_key(run_id, 'engineer', purpose, **payload) != hid
                or (payload['context'] or {}).get('validation_evidence') != ref):
            raise ValueError('Proposal source reads lost their frozen author/evidence binding')
        validate(proposal, payload['schema'])
        pages = []
        for row in engine.store.get('handoff_progress', hid, {}).get('tool_results', []):
            call, result = row.get('call', {}), row.get('result', {})
            args = call.get('arguments', {})
            if (call.get('name') != 'experiments.inspect_module' or args.get('view') != 'source'
                    or args.get('path') not in missing or result.get('error')):
                continue
            body = {k: v for k, v in result.items() if k != 'receipt_sha256'}
            if (args.get('evidence_id') != ref['id'] or args.get('expected_sha256') != ref['sha256']
                    or result.get('receipt_sha256') != sha({'call': call, 'result': body})
                    or body != inspect_module(engine, run_id, payload['context'], args)
                    or not body.get('source_read')):
                raise ValueError('Proposal source read receipt does not match the frozen module bytes')
            pages.append({'path': args['path'], 'snapshot': args.get('snapshot', 'current'),
                'source_sha256': body['source_sha256'], 'offset': body['offset'], 'end': body['end'],
                'receipt_sha256': result['receipt_sha256']})
        if pages:
            return {'role': 'engineer', 'handoff_id': hid, 'evidence_sha256': ref['sha256'],
                'source_paths': sorted({p['path'] for p in pages}), 'pages': pages,
                'receipt_sha256s': sorted({p['receipt_sha256'] for p in pages})}
    return None


async def ensure_reads(engine, run_id, record, proposal, reads, context, checkpoint, *, harness=None):
    from .experiment_evidence_handoff import merge_reads
    from .training_investigation import validate_reads

    if not (context.get('experiment_decision', {}).get('training_investigation', {}).get('required')
            or proposal.get('architecture_review')):
        return reads
    if harness is not None:
        from .training_read_dispatch import ensure_reads as dispatch
        return await dispatch(engine, harness, run_id, record, proposal, reads, context, checkpoint)
    saved = record.get(KEY, {}).get(sha(proposal))
    original = saved['initial_reads'] if saved else reads
    missing = set(proposal['training_response']['module_paths']) - {
        path for r in original for path in r.get('source_paths', [])}
    if not missing:
        return reads
    additional = author_reads(engine, run_id, proposal, record['evidence'], missing)
    if not additional:
        return reads  # The unchanged training gate reports any genuine missing evidence.
    effective = merge_reads(original, additional)
    evidence = engine.store.get('experiment_evidence', record['evidence']['id'])['summary']
    validate_reads(effective, proposal, evidence)
    audit = {'proposal_sha256': sha(proposal), 'initial_reads': copy.deepcopy(original),
             'author_read': additional, 'approval_inherited': False}
    if saved and saved != audit:
        raise ValueError('Saved proposal source-read audit changed')
    if not saved:
        await checkpoint(**{KEY: {**record.get(KEY, {}), sha(proposal): audit}}, analysis_reads=effective)
    return effective


def vote_context(record, context):
    from .training_read_dispatch import vote_context as supplements
    context = supplements(record, context)
    saved = record.get(KEY, {}).get(record.get('proposal_sha256'))
    if not saved:
        return context
    result = copy.deepcopy(context)
    result['experiment_decision'][KEY] = {
        k: copy.deepcopy(saved[k]) for k in ('proposal_sha256', 'author_read', 'approval_inherited')}
    return result
