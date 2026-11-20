"""Authenticate and project existing evidence, without creating reading credit."""
from __future__ import annotations

import copy

from . import code_repair_contract as repair


def sealed(engine, run_id, role, identity):
    hand = engine.store.get('handoffs', identity, {})
    frozen = engine.store.get('handoff_inputs', identity, {})
    decision = engine.store.get('decisions', identity, {})
    if (hand.get('run_id') != run_id or hand.get('to_role') != role or hand.get('status') != 'completed'
            or decision.get('run_id') != run_id or decision.get('role') != role
            or hand.get('output_hash') != repair.sha(decision.get('result'))
            or hand.get('input_hash') != repair.sha({k: frozen.get(k) for k in ('instruction','context','schema')})):
        raise ValueError('Inherited evidence requires the same role and sealed original assignment')
    return frozen, decision['result']


def entry_memory(engine, run_id, task):
    """Return actual old pages, not a handoff ID mislabeled as a context receipt."""
    from .implementation_alignment_evidence import _code_text, _own_code_spans
    recovery = task['read_binding_recovery']
    prior, plan = sealed(engine, run_id, 'engineer', recovery['handoff_id'])
    current, _ = sealed(engine, run_id, 'engineer', task['handoff_id'])
    for frozen in (prior, current):
        if frozen.get('context', {}).get('implementation_evidence_task', {}).get('binding') != task['binding']:
            raise ValueError('Inherited entry memory belongs to another frozen task')
    if current['context']['implementation_evidence_task'].get('read_binding_recovery') != recovery:
        raise ValueError('Inherited entry recovery reference changed')
    if repair.sha(plan) != recovery['decision_sha256'] or plan.get('entrypoint') != recovery['entrypoint']:
        raise ValueError('Inherited entry decision changed')
    files = {item['path']: item for item in prior['context']['delivery_artifacts']}
    pages = []
    for path, owner in ((recovery['entrypoint'], recovery['handoff_id']), ('implementation.json', task['handoff_id'])):
        item = files[path];aid = item.get('artifact_id') or item.get('id')
        text = _code_text(engine, run_id, aid, item)
        spans = _own_code_spans(engine, owner, aid, item['sha256'], text)
        cursor = 0
        for start, end in spans:
            if start <= cursor: cursor = max(cursor, end)
        if cursor != len(text):
            raise ValueError('Inherited entry memory lacks complete original page receipts')
        if path == 'implementation.json' and item['sha256'] != recovery['file_sha256']:
            raise ValueError('Inherited manifest version changed')
        pages.append({'path':path, 'artifact_id':aid, 'sha256':item['sha256'],
            'source_handoff_id':owner, 'ranges':[list(span) for span in spans], 'text':text,
            'provenance':'same_role_same_version_saved_read', 'new_source_reading':False})
    return {'version':1, 'task_id':task['id'], 'binding':copy.deepcopy(task['binding']),
        'original_decision_sha256':repair.sha(plan), 'original_plan':copy.deepcopy(plan), 'pages':pages,
        'scope':'Handoff IDs identify owners, not context.expand receipts. Use these authenticated saved pages directly.',
        'execution_authorized':False, 'new_source_reading':False}

