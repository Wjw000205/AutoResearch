"""Bounded, read-only display of controller-accepted whole-document reports.

These metadata checks prevent mixing versions in the UI. They do not replace
the controller's source, reading, report or scientific acceptance validators.
"""


def sql():
    fields = {
        'round': 'ordinal', 'revision': 'revision', 'spec_hash': 'spec_hash',
        'document_id': 'document.document_id', 'document_sha256': 'document.sha256',
        'document_title': 'document.title', 'complete': 'complete', 'outcome': 'outcome',
    }
    columns = ','.join(f"'{key}',json_extract(w.value,'$.{path}')" for key, path in fields.items())
    reports = []
    for role in ('director', 'scout'):
        base = '$.reports.' + role
        values = ','.join(f"'{key}',json_extract(w.value,'{base}.{path}')" for key, path in {
            'role': 'role', 'handoff_id': 'handoff_id', 'verdict': 'result.verdict',
            'document_sha256': 'result.document_sha256',
            'workflow_id': 'context.implementation_whole_document_review.workflow_id',
            'round': 'context.implementation_whole_document_review.round',
            'spec_hash': 'context.implementation_whole_document_review.spec_hash',
            'document_id': 'context.implementation_whole_document_review.document.document_id',
            'marker_document_sha256': 'context.implementation_whole_document_review.document.sha256',
        }.items())
        reports.append(f"'{role}',json_object({values},'summary',"
            f"substr(json_extract(w.value,'{base}.result.review_summary'),1,1200))")
    return ("(SELECT json_object(" + columns + ",'reports',json_object(" + ','.join(reports)
        + "),'source_matches',(SELECT count(*)=1 FROM json_each(records.data,'$.specifications') s "
        "WHERE json_extract(s.value,'$.index')=json_extract(w.value,'$.revision') "
        "AND json_extract(s.value,'$.spec_hash')=json_extract(w.value,'$.spec_hash') "
        "AND json_extract(s.value,'$.document.document_id')=json_extract(w.value,'$.document.document_id') "
        "AND json_extract(s.value,'$.document.sha256')=json_extract(w.value,'$.document.sha256'))) FROM whole_round w)")


def summary(run_id, workflow):
    raw = workflow.pop('_whole_review', None) or {}
    binding = (workflow.get('acceptance') or {}).get('binding') or {}
    valid = bool(raw.get('source_matches') == 1
        and type(raw.get('revision')) is int and type(raw.get('round')) is int
        and raw.get('document_id') and raw.get('document_sha256') and raw.get('spec_hash')
        and all(binding.get(key) == raw[key] for key in ('revision', 'spec_hash', 'document_sha256')))
    roles = []
    for role in ('director', 'scout'):
        packet = (raw.get('reports') or {}).get(role) or {}
        accepted = bool(valid and packet.get('role') == role and packet.get('handoff_id')
            and packet.get('workflow_id') == workflow['id']
            and all(packet.get(key) == raw.get(key) for key in
                ('round', 'spec_hash', 'document_id', 'document_sha256'))
            and packet.get('marker_document_sha256') == raw.get('document_sha256')
            and packet.get('verdict') in ('APPROVE', 'REQUEST_CHANGES'))
        roles.append({'role': role, 'report_saved': accepted,
            'verdict': packet.get('verdict') if accepted else None,
            'summary': packet.get('summary') if accepted else None,
            'handoff_id': packet.get('handoff_id') if accepted else None})
    outcome_consistent = (raw.get('outcome') not in ('APPROVED', 'APPROVED_WITH_NOTES')
        or all(r['verdict'] == 'APPROVE' for r in roles))
    return {'version': 1, 'run_id': run_id, 'workflow_id': workflow['id'],
        'projection_only': True, 'scientific_approval': False, 'binding_valid': valid,
        **{key: raw.get(key) if valid else None for key in
            ('round', 'revision', 'spec_hash', 'document_id', 'document_sha256', 'document_title')},
        'complete': bool(valid and raw.get('complete') == 1 and outcome_consistent and all(r['report_saved'] for r in roles)),
        'outcome': raw.get('outcome') if valid else None, 'roles': roles,
        'counts': {'expected_reviews': 2, 'saved_reviews': sum(r['report_saved'] for r in roles),
            'approved_reviews': sum(r['verdict'] == 'APPROVE' for r in roles),
            'changes_required_reviews': sum(r['verdict'] == 'REQUEST_CHANGES' for r in roles)}}
