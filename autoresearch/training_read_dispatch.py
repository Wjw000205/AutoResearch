"""Durable, role-owned supplements for a proposal's missing training/source reads.

Reading is orchestration work, not a new proposal or execution authorization.
Each delivery is retained before verification; partial pages create a successor
assignment for the remaining ranges, and completed roles are reused on resume.
"""
from __future__ import annotations

import copy

from .experiment_workflow import sha
from .prose_schema import validate

KEY = 'training_read_dispatch'
ROLES = {'director', 'engineer', 'scout'}


def retained_proposal(record):
    if (record.get('proposal') or record.get('votes')
            or record.get('status') not in {'reading_training', 'reading_optimization', 'proposing'}):
        return None
    read_gates = {'All three agents must read the affected training evidence and relevant module source before this intervention',
                  'All three agents must read comparison/ablation analysis and relevant module source before optimization'}
    rejected = {sha(row['proposal']) for row in record.get('proposal_corrections', [])
                if row.get('proposal') and row.get('error') not in read_gates}
    pending = record.get('training_pending_proposal') or {}
    if (pending.get('proposal_sha256') == sha(pending.get('proposal'))
            and pending.get('proposal_sha256') not in rejected
            and pending.get('evidence') == record.get('evidence')
            and pending.get('proposal', {}).get('evidence_sha256') == record['evidence']['sha256']):
        return copy.deepcopy(pending['proposal'])
    # Legacy records may retain several correction attempts. Resume the latest
    # valid delivery, never an earlier proposal that the host already rejected.
    for audit in reversed(list(record.get(KEY, {}).values())):
        value = audit.get('proposal') or {}
        if (audit.get('status') in {'reading', 'complete'}
                and audit.get('evidence') == record.get('evidence')
                and audit.get('proposal_sha256') == sha(value)
                and audit.get('proposal_sha256') not in rejected
                and value.get('evidence_sha256') == record['evidence']['sha256']):
            return copy.deepcopy(value)
    return None


def _gaps(total, ranges):
    cursor, missing = 0, []
    for start, end in sorted(ranges):
        if not (type(start) is int and type(end) is int and 0 <= start < end <= total):
            raise ValueError('Invalid training supplement page range')
        if start > cursor:
            missing.append([cursor, start])
        cursor = max(cursor, end)
    if cursor < total:
        missing.append([cursor, total])
    return missing


def _targets(engine, run_id, context, proposal, original, required):
    from .experiment_analysis import analyze, inspect_module
    ref = context['validation_evidence']
    base = {'evidence_id': ref['id'], 'expected_sha256': ref['sha256'], 'offset': 0, 'limit': 1}
    targets = []
    from .architecture_experiment_contract import module_paths
    for path in sorted(module_paths(proposal) - set(original.get('source_paths', []))):
        from .source_read_identity import source_files, resolve
        identity = resolve(source_files(engine.store, run_id, context), path)
        args = {**base, 'path': identity['path'], 'view': 'source', 'snapshot': 'current'}
        meta = inspect_module(engine, run_id, context, args)
        targets.append({'name': 'experiments.inspect_module', 'arguments': args,
            'sha256': meta['source_sha256'], 'total_chars': meta['total_chars'], 'ranges': [],
            **({'source_reference': identity} if identity['path'] != path else {})})
    for trial in sorted(required - set(original.get('training_trials_read', []))):
        args = {**base, 'kind': 'training', 'trial_id': trial}
        meta = analyze(engine, run_id, context, args)
        prior = original.get('critical_training_reads', {}).get(trial, {})
        targets.append({'name': 'experiments.analyze', 'arguments': args,
            'sha256': meta['content_sha256'], 'total_chars': meta['total_chars'],
            'ranges': copy.deepcopy(prior.get('ranges', []))
                if prior.get('analysis_sha256') == meta['content_sha256'] else []})
    # Host metadata locates missing pages; it is never recorded as an agent read.
    return targets


def _pending(targets):
    result = []
    for target in targets:
        for start, end in _gaps(target['total_chars'], target['ranges']):
            result.append({'name': target['name'], 'arguments': {
                **target['arguments'], 'offset': start, 'limit': min(6000, end - start)},
                'read_until': end, 'content_sha256': target['sha256']})
    return result


def _verify(engine, run_id, role, assignment):
    """Verify ownership, frozen input/output and the actual archived page bytes."""
    from .engine import Harness, digest, ResearchBlocked
    from .experiment_analysis import analyze, inspect_module
    from .experiment_decisions import _supplementary_analysis_read
    payload = {k: assignment[k] for k in ('instruction', 'context', 'schema')}
    hid = Harness.decision_key(run_id, role, assignment['purpose'], **payload)
    handoff = engine.store.get('handoffs', hid, {})
    inputs = engine.store.get('handoff_inputs', hid, {})
    answer = assignment['answer']
    ref = payload['context']['validation_evidence']
    if (handoff.get('run_id') != run_id or handoff.get('to_role') != role
            or handoff.get('purpose') != assignment['purpose'] or handoff.get('status') != 'completed'
            or handoff.get('input_hash') != digest(payload) or handoff.get('output_hash') != digest(answer)
            or inputs.get('run_id') != run_id or inputs.get('id') != hid
            or {k: inputs.get(k) for k in payload} != payload
            or answer.get('evidence_sha256') != ref['sha256']):
        raise ResearchBlocked('Training supplement lost its frozen role/input/output/evidence binding')
    validate(answer, assignment['schema'])
    cited, valid, pages, kinds = set(answer['read_receipts']), set(), [], set()
    for row in engine.store.get('handoff_progress', hid, {}).get('tool_results', []):
        call, result = row.get('call', {}), row.get('result', {})
        receipt = result.get('receipt_sha256')
        if receipt not in cited:
            continue
        args = call.get('arguments', {})
        body = {k: v for k, v in result.items() if k != 'receipt_sha256'}
        if result.get('error') or receipt != sha({'call': call, 'result': body}):
            raise ResearchBlocked('Training supplement has an invalid tool receipt')
        if call.get('name') in {'experiments.inspect_module', 'experiments.analyze'}:
            if args.get('evidence_id') != ref['id'] or args.get('expected_sha256') != ref['sha256']:
                raise ResearchBlocked('Training supplement refers to another frozen evidence snapshot')
            fn = inspect_module if call['name'] == 'experiments.inspect_module' else analyze
            if body != fn(engine, run_id, payload['context'], args):
                raise ResearchBlocked('Training supplement page differs from the frozen source/report bytes')
            if call['name'] == 'experiments.analyze':
                kinds.add(args['kind'])
            if body.get('source_read') or (call['name'] == 'experiments.analyze' and args['kind'] == 'training'):
                pages.append({'name': call['name'], 'arguments': args,
                    'sha256': body.get('source_sha256', body.get('content_sha256')),
                    'total_chars': body['total_chars'], 'range': [body['offset'], body['end']]})
        elif not _supplementary_analysis_read(engine, run_id, payload['context'], call, result):
            raise ResearchBlocked('Training supplement cites an unsupported or unrelated receipt')
        valid.add(receipt)
    if valid != cited:
        raise ResearchBlocked('Training supplement cites receipts absent from this role assignment')
    return {'role': role, 'handoff_id': hid, 'evidence_sha256': ref['sha256'],
        'receipt_sha256s': sorted(valid), 'analysis_kinds': sorted(kinds), 'pages': pages}


def _merge(reads, targets, proof):
    from .experiment_evidence_handoff import merge_reads
    result = merge_reads(reads, proof)
    row = next(r for r in result if r['role'] == proof['role'])
    for target in targets:
        args = target['arguments']
        for page in proof['pages']:
            match = (page['name'] == target['name'] and page['sha256'] == target['sha256']
                and page['total_chars'] == target['total_chars']
                and all(page['arguments'].get(k, 'current' if k == 'snapshot' else None)
                        == args.get(k, 'current' if k == 'snapshot' else None)
                        for k in ('path', 'trial_id', 'snapshot')))
            if match and page['range'] not in target['ranges']:
                target['ranges'].append(page['range'])
        done = not _gaps(target['total_chars'], target['ranges'])
        if target['name'] == 'experiments.inspect_module':
            if done:
                row['source_paths'] = sorted(set(row.get('source_paths', [])) | {args['path']})
                if target.get('source_reference'):
                    row['source_paths'] = sorted(set(row['source_paths']) | {target['source_reference']['reference']})
                row['module_source_read'] = True
        else:
            row.setdefault('critical_training_reads', {})[args['trial_id']] = {
                'analysis_sha256': target['sha256'], 'total_chars': target['total_chars'],
                'ranges': sorted(target['ranges']), 'complete': done}
            if done:
                row['training_trials_read'] = sorted(set(row.get('training_trials_read', [])) | {args['trial_id']})
    return result


def _satisfied(reads, required, modules):
    return (len(reads) == 3 and {r.get('role') for r in reads} == ROLES
        and all(required.issubset(r.get('training_trials_read', [])) and r.get('source_paths')
            and modules.issubset(r.get('source_paths', [])) for r in reads))


async def ensure_reads(engine, harness, run_id, record, proposal, reads, context, checkpoint):
    from .engine import ResearchBlocked
    from .experiment_decisions import analysis_schema
    from .experiment_evidence_handoff import merge_reads
    from .training_proposal_reads import author_reads
    if (context['validation_evidence'] != record['evidence']
            or proposal.get('evidence_sha256') != record['evidence']['sha256']):
        raise ResearchBlocked('Training supplement requires the exact proposal evidence snapshot')
    evidence = engine.store.get('experiment_evidence', record['evidence']['id'])['summary']
    active = evidence.get('training_investigation') or {}
    required = set(active.get('focus_trial_ids', []) or (active.get('followup') or {}).get('new_trial_ids', []))
    from .architecture_experiment_contract import module_paths
    modules = module_paths(proposal)
    key = sha(proposal)
    pending = {'proposal_sha256': key, 'proposal': copy.deepcopy(proposal),
               'evidence': copy.deepcopy(record['evidence'])}
    if record.get('training_pending_proposal') != pending:
        await checkpoint(training_pending_proposal=pending, status='proposing')
    audits = copy.deepcopy(record.get(KEY, {}))
    saved = audits.get(key)
    if not saved and _satisfied(reads, required, modules):
        return reads
    if len(reads) != 3 or {r.get('role') for r in reads} != ROLES:
        raise ResearchBlocked('Training supplement requires the three original independent role analyses')
    if not saved:
        saved = audits[key] = {'version': 1, 'proposal': copy.deepcopy(proposal), 'proposal_sha256': key,
            'evidence': copy.deepcopy(record['evidence']), 'initial_reads': copy.deepcopy(reads),
            'required_training_trials': sorted(required), 'module_paths': sorted(modules),
            'assignments': {}, 'status': 'reading', 'approval_inherited': False}
    if (saved['proposal'] != proposal or saved['evidence'] != record['evidence']
            or saved['required_training_trials'] != sorted(required) or saved['module_paths'] != sorted(modules)):
        raise ResearchBlocked('Training supplement no longer matches its proposal and frozen evidence')
    effective = copy.deepcopy(saved['initial_reads'])
    from .source_read_identity import credit_existing
    effective = credit_existing(engine.store, run_id, context, effective, modules)
    engineer = next(r for r in effective if r['role'] == 'engineer')
    author_basis = (next(r for r in saved['initial_reads'] if r['role'] == 'engineer')
                    if 'author_read' in saved else engineer)
    additional = author_reads(engine, run_id, proposal, record['evidence'], modules - set(author_basis.get('source_paths', [])))
    if 'author_read' in saved and saved['author_read'] != additional:
        raise ResearchBlocked('Retained proposal author read changed')
    saved['author_read'] = additional
    if additional:
        effective = merge_reads(effective, additional)
    await checkpoint(**{KEY: audits}, status='reading_training')
    for original in list(effective):
        role = original['role']
        targets = _targets(engine, run_id, context, proposal, original, required)
        assignments = saved['assignments'].setdefault(role, [])
        index = 0
        while _pending(targets) or index < len(assignments):
            if index == len(assignments):
                request = {k: copy.deepcopy(context[k]) for k in (
                    'training_context_protocol', 'experiment_workflow_protocol', 'research_cycle',
                    'validation_evidence', 'evidence_sha256') if k in context}
                request['experiment_decision'] = {KEY: {
                    'version': 1, 'role': role, 'proposal_sha256': key, 'action': proposal['action'],
                    'pending_reads': _pending(targets),
                    'retained_training_trials': original.get('training_trials_read', []),
                    'retained_source_paths': original.get('source_paths', []),
                    'previous_observations': [a['answer']['observations'] for a in assignments if 'answer' in a],
                    'execution_authorized': False, 'scientific_approval': False}}
                assignments.append({
                    'purpose': f'experiment_diagnose:{context["evidence_sha256"]}:training-supplement-v1:{key}:{index + 1}',
                    'instruction': '补读本角色 pending_reads 指定的冻结原文，直接使用其中工具名和 arguments。'
                        '每项读到 read_until；有 next_offset 时继续未读页，每页 limit<=6000。'
                        'retained_* 已经由本角色读过，不必重读；本次只补缺口，不重做方案、不训练、不改代码。'
                        '源码补读无需再做训练分析。只在 read_receipts 引用本次实际工具回执，'
                        '原答复和已读页会保留；未读完会自动派发剩余页。'
                        '记录新发现、反证和局限，suggested_action=diagnose。补读不是批准，随后仍须三方独立投票。',
                    'context': request, 'schema': analysis_schema(request)})
                await checkpoint(**{KEY: audits}, status='reading_training')
            assignment = assignments[index]
            if 'answer' not in assignment:
                assignment['answer'] = copy.deepcopy(await harness.ask(run_id, role,
                    assignment['purpose'], assignment['instruction'], assignment['context'], assignment['schema']))
                await checkpoint(**{KEY: audits}, status='reading_training')
            proof = _verify(engine, run_id, role, assignment)
            effective = _merge(effective, targets, proof)
            assignment['read'] = proof
            await checkpoint(**{KEY: audits}, analysis_reads=effective, status='reading_training')
            index += 1
    if not _satisfied(effective, required, modules):
        raise ResearchBlocked('Training supplement has no readable source for the declared intervention')
    saved['status'] = 'complete'
    await checkpoint(**{KEY: audits}, analysis_reads=effective, status='proposing')
    return effective


def complete(record):
    saved = record.get(KEY, {}).get(record.get('proposal_sha256'))
    return not saved or (saved.get('status') == 'complete'
        and saved.get('evidence') == record.get('evidence') and saved.get('proposal') == record.get('proposal')
        and saved.get('proposal_sha256') == sha(record.get('proposal'))
        and _satisfied(record.get('analysis_reads', []), set(saved['required_training_trials']), set(saved['module_paths'])))


def vote_context(record, context):
    saved = record.get(KEY, {}).get(record.get('proposal_sha256'))
    if not saved:
        return context
    result = copy.deepcopy(context)
    result['experiment_decision'][KEY] = {
        'proposal_sha256': saved['proposal_sha256'], 'status': saved['status'],
        'author_read': saved.get('author_read'),
        'analyses': [{'role': role, **copy.deepcopy(a['answer']), 'handoff_id': a['read']['handoff_id']}
            for role, assignments in saved['assignments'].items() for a in assignments if a.get('read')],
        'approval_inherited': False}
    return result
