"""Preserve unprocessed findings as questions, never as replacement votes."""
from __future__ import annotations

import copy

from . import implementation_acceptance as policy
from . import implementation_acceptance_flow as flow
from .implementation_review_routing import digest


def pending_findings(engine, run_id, state, row):
    if row is None:
        return []
    known = list(state[policy.FIELD]['issues'].values())
    keys = engine.store.record_keys('handoff_inputs', run_id,
        predicate="json_extract(data,'$.context.acceptance_workflow_id')=? AND "
                  "json_extract(data,'$.context.implementation_acceptance.document.document_id')=?",
        parameters=[state['id'], row['document']['document_id']])
    findings = {}
    for hid in keys:
        raw = engine.store.get('decisions', hid)
        if not raw or not isinstance(raw.get('result'), dict):
            continue
        items = [i for i in raw['result'].get('issues', []) if i.get('severity') in {'P0', 'P1'}]
        if not items:
            continue
        context = flow.authenticate(engine, run_id, raw['role'], hid, raw['result'])
        if context[policy.CONTEXT]['document'] != row['document']:
            policy.fail('Unprocessed historical finding has a different document version')
        for item in items:
            identity = [item['criterion_id'], item['claim'], item['close_when']]
            if any(i.get('source_handoff_id') == hid and i.get('local_id') == item['local_id']
                   and [i['criterion_id'], i['claim'], i['close_when']] == identity for i in known):
                continue  # Only this exact original finding was already recorded.
            key = 'H-' + digest(identity)[:20]
            origin = {'handoff_id': hid, 'role': raw['role'], 'result_sha256': digest(raw['result']),
                      'local_id': item['local_id'], 'finding_sha256': digest(item),
                      'evidence': copy.deepcopy(item['evidence']),
                      'new_evidence_reason': item.get('new_evidence_reason', ''),
                      'reopen_issue_id': item.get('reopen_issue_id')}
            if key not in findings:
                findings[key] = {'issue_id': key, 'finding': copy.deepcopy(item), 'origins': [],
                    'close_when': ['Independently resolve the original counterexample at the current version'],
                    'scientific_approval': False}
            findings[key]['origins'].append(origin)
    return [findings[k] for k in sorted(findings)]


def authenticate(engine, run_id, findings):
    for item in findings:
        for origin in item['origins']:
            raw = engine.store.get('decisions', origin['handoff_id'], {})
            if digest(raw.get('result')) != origin['result_sha256'] or raw.get('role') != origin['role']:
                policy.fail('Historical unresolved finding lost its original receipt')
            flow.authenticate(engine, run_id, origin['role'], origin['handoff_id'], raw['result'])
            source = next((i for i in raw['result'].get('issues', []) if i['local_id'] == origin['local_id']), None)
            if (source is None or digest(source) != origin['finding_sha256']
                    or any(source[k] != item['finding'][k] for k in ('criterion_id', 'claim', 'close_when'))):
                policy.fail('Historical unresolved finding differs from its original author report')
