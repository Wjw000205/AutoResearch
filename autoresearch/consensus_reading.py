"""Route a mediated evidence inspection to the reviewer who requested it."""
import copy
import re

from . import experiment_evidence_handoff as handoff
from .experiment_workflow import sha
from .training_read_dispatch import _gaps, _pending, _verify

KEY = 'directed_review_reading'


def mentions(text, path):
    return re.search(r'(?<![\w./~-])' + re.escape(path) + r'(?![\w/~.-])', text, re.ASCII) is not None


def saved_notes(engine, run_id, role, owner):
    saved = engine.store.get('experiment_notes', run_id + ':' + owner) or {}
    if not saved:
        return []
    verified = {}
    for row in engine.store.get('handoff_progress', owner, {}).get('tool_results', []):
        call, result = row.get('call', {}), row.get('result', {})
        if call.get('name') != 'experiments.record_notes' or not isinstance(result, dict) or not result.get('saved'):
            continue
        for note in call['arguments']['notes']:
            if note in result['notes']:
                verified[note['key']] = note
    if (saved.get('run_id') != run_id or saved.get('role') != role or saved.get('handoff_id') != owner
            or saved.get('notes') != list(verified.values())):
        raise ValueError('Reviewer observations differ from the role-owned saved tool results')
    return saved['notes']


def reviewer_steps(plan):
    issues = {i['id']: i for i in plan['issues']}
    for step in plan['resolution']['resolutions']:
        if step['kind'] == 'inspect_evidence':
            issue = issues[step['issue_id']]
            for role in step.get('reader_roles', [issue['role']]):
                if role in ('director', 'scout'):
                    yield issue, step, role


def requires_notes(step):
    if 'save_notes' in step:
        return step['save_notes']
    text = (step['instruction'] + ' ' + step.get('acceptance_check', '')).casefold()
    return any(word in text for word in ('record_notes', '备注', '笔记', '回执记录', 'note'))


def note_targets(plan, material):
    """Resolve the mediator's explicit requests for notes against that seat's own delivered pages."""
    seats = material.get('prior_inspections', {}).get('roles', {})
    targets = {}
    for issue, step, role in reviewer_steps(plan):
        text = step['instruction'] + '\n' + step['acceptance_check']
        if step.get('save_notes') is False:
            continue
        asks_for_note = requires_notes(step)
        for page in seats.get(role, {}).get('note_sources', []):
            if mentions(text, page['receipt_sha256']) or (asks_for_note and mentions(text, page['path'])):
                targets.setdefault(role, {})[page['receipt_sha256']] = issue['id']
    return targets


def targets(engine, run_id, record, context):
    from .experiment_analysis import inspect_module
    from .experiment_tools import compare
    from .source_read_identity import source_files
    plan = record['consensus_resolution']
    primary = context['validation_evidence']
    supplement = context['experiment_decision'].get(handoff.KEY, {}).get('evidence')
    inspections = record.get('reviewed_revision_material', {}).get('prior_inspections', {}).get('roles', {})
    by_role = {}
    archives = {}
    for issue, step, role in reviewer_steps(plan):
        text = step['instruction']
        typed = 'reader_roles' in step
        own = inspections.get(role, {})
        calls = []
        hashes = re.findall(r'expected_sha256[\s\"\x27:=为是]+([a-fA-F0-9]{64})(?![a-fA-F0-9])', text)
        identified = [ref for ref in (primary, supplement) if ref and mentions(text, ref['id'])]
        if not identified and typed and ':experiment-decision:' not in text:
            identified = [r for r in (primary, supplement) if r]
        if not identified or hashes and (len(identified) != 1 or any(value != identified[0]['sha256'] for value in hashes)):
            continue
        if 'experiments.inspect_module' in text and primary in identified and 'snapshot=candidate' not in text:
            files = source_files(engine.store, run_id, context)
            calls.extend(('experiments.inspect_module', primary, path, {'view': 'source', 'snapshot': 'current'})
                         for path in files if mentions(text, path) and (typed or mentions(issue['text'], path)))
        if 'experiments.compare' in text:
            for path in re.findall(r'(?<![\w./~-])/(?:[\w~.-]+/)*[\w~-]+', text, re.ASCII):
                if not typed and not mentions(issue['text'], path):
                    continue
                candidates = identified
                if typed:
                    from .tool_navigation import pointer, NavigationError
                    candidates = []
                    for candidate in identified:
                        if candidate['id'] not in archives:
                            raw = engine.store.get('experiment_evidence', candidate['id']) or {}
                            if raw.get('run_id') != run_id or sha(raw.get('summary')) != candidate['sha256']:
                                raise ValueError('Directed read requires its frozen source archive')
                            archives[candidate['id']] = raw['summary']
                        try:
                            pointer(archives[candidate['id']], path)
                        except NavigationError:
                            continue
                        candidates.append(candidate)
                if len(candidates) != 1:
                    continue
                ref = candidates[0]
                prior = own.get('supplemental_read', {})
                if ref != supplement or prior.get('evidence') != ref or path not in prior.get('delivered_paths', []):
                    calls.append(('experiments.compare', ref, path, {}))
        for name, ref, path, extra in calls:
            args = {'evidence_id': ref['id'], 'expected_sha256': ref['sha256'], 'path': path, **extra}
            page = (compare if name == 'experiments.compare' else inspect_module)(
                engine, run_id, context, {**args, 'offset': 0, 'limit': 1})
            ranges = []
            digest = page.get('source_sha256') or page['content_sha256']
            if ref == primary:
                kind = 'source' if name == 'experiments.inspect_module' else 'evidence'
                old = next((i for i in own.get('inspections', []) if i['kind'] == kind
                            and i['path_or_trial'] == path and i['sha256'] == digest
                            and i['chars'] == page['total_chars']), None)
                if old and 'undelivered_ranges' in old:
                    if not old['undelivered_ranges']:
                        continue
                    ranges = _gaps(old['chars'], old['undelivered_ranges'])
            target = {'name': name, 'arguments': args, 'sha256': digest,
                      'total_chars': page['total_chars'], 'ranges': ranges, 'issue_id': issue['id'],
                      'notes_required': requires_notes(step)}
            if target not in by_role.setdefault(role, []):
                by_role[role].append(target)
    return by_role


async def ensure(engine, harness, run_id, record, context, checkpoint):
    from .engine import ResearchBlocked
    from .experiment_decisions import analysis_schema
    plan = record.get('consensus_resolution') or {}
    material = record.get('reviewed_revision_material') or {}
    if not material.get('previous_proposal') or not any(
            step['kind'] == 'inspect_evidence' for step in plan.get('resolution', {}).get('resolutions', [])):
        return None
    packet = {k: copy.deepcopy(context[k]) for k in ('validation_evidence', 'evidence_sha256', 'research_cycle',
              'training_context_protocol', 'experiment_workflow_protocol') if k in context}
    packet['experiment_decision'] = {k: context['experiment_decision'][k] for k in ('code_sources',)
                                     if k in context['experiment_decision']}
    saved = record.get(handoff.KEY)
    if saved:
        packet['experiment_decision'][handoff.KEY] = handoff.packet(saved, for_review=True)
    work = record.get(KEY)
    if work is None:
        if record.get('proposal'):
            return None
        assigned = await engine._read_consensus_check(run_id, targets, engine, run_id, record, packet)
        notes_required = note_targets(plan, material)
        if not assigned and not notes_required:
            return None
        for role in notes_required:
            assigned.setdefault(role, [])
        work = {'version': 2, 'targets': assigned, 'note_targets': notes_required, 'assignments': {}, 'approval_inherited': False}
        await checkpoint(**{KEY: work})
    proofs = {}
    for role, frozen in work['targets'].items():
        pending = copy.deepcopy(frozen)
        rounds = work['assignments'].setdefault(role, [])
        proofs[role] = []
        index = 0
        missing_notes = set(work.get('note_targets', {}).get(role, {}))
        while _pending(pending) or missing_notes or index < len(rounds):
            missing = _pending(pending)
            previous_notes = sorted(missing_notes)
            if index == len(rounds):
                ctx = copy.deepcopy(packet)
                prior = material.get('prior_inspections', {}).get('roles', {}).get(role)
                if work.get('version') == 2 and prior:
                    ctx['experiment_decision']['prior_inspections'] = {'delivery_protocol': 1, 'roles': {role: prior}}
                if work.get('version') == 2 and material.get('selected_validation_runs'):
                    ctx['experiment_decision']['selected_validation_runs'] = material['selected_validation_runs']
                    if context['experiment_decision'].get('hyperparameter_yaml'):
                        ctx['experiment_decision']['hyperparameter_yaml'] = context['experiment_decision']['hyperparameter_yaml']
                ctx['experiment_decision'][KEY] = {'pending_reads': missing,
                    'previous_decision': record['consensus_parent'], 'proposal_sha256': sha(record['reviewed_revision_material']['previous_proposal']),
                    'resolution': record['consensus_resolution']['resolution'], 'execution_authorized': False}
                schema = analysis_schema(ctx)
                schema['properties']['suggested_action'] = {'const': 'diagnose'}
                instruction = ('落实本角色提出并经协调指定的只读复核。用 pending_reads 给出的原工具与 arguments 读到 read_until，'
                    '用 experiments.record_notes 记录同版回执和判断。只在 read_receipts 引用本次实际返回的页；'
                    '报告 observations 和仍未知的问题，suggested_action=diagnose。本次不投票、不修改方案、不运行训练。')
                if missing_notes:
                    ctx['experiment_decision'][KEY]['note_receipts'] = previous_notes
                    schema['properties']['read_receipts']['minItems'] = 0
                    instruction += (' note_receipts 为本人已经收到的原页。用 experiments.record_notes 引用它们，按协调要求'
                        '记录具体核对结论。已确认送达的页可直接引用。没有新读页时返回 read_receipts=[]。')
                rounds.append({'purpose': 'experiment_diagnose:consensus-reading:' + sha([record['id'], role, index, missing]),
                    'context': ctx, 'schema': schema,
                    'instruction': instruction})
                await checkpoint(**{KEY: work})
            a = rounds[index]
            if 'answer' not in a:
                a['answer'] = await harness.ask(run_id, role, a['purpose'], a['instruction'], a['context'], a['schema'])
                await checkpoint(**{KEY: work})
            proof = await engine._read_consensus_check(run_id, _verify, engine, run_id, role, a)
            notes = saved_notes(engine, run_id, role, proof['handoff_id'])
            noted = {receipt for note in notes for receipt in note['receipt_sha256s']}
            if missing_notes:
                from .experiment_review_reads import note_origins
                origins = await engine._read_consensus_check(run_id, note_origins, engine, run_id, role,
                    a['context'], noted & missing_notes)
                missing_notes.difference_update(origins)
            if work.get('version') == 2:
                from .experiment_review_reads import submitted_pages
                submitted = await engine._read_consensus_check(run_id, submitted_pages, engine.store, run_id,
                    role, proof['handoff_id'], proof['receipt_sha256s'])
                proof['pages'] = [p for p in proof['pages'] if p['receipt_sha256'] in submitted]
            for target in pending:
                for p in proof['pages']:
                    if (target['name'] == p['name'] and target['sha256'] == p['sha256']
                            and (not target.get('notes_required') or p['receipt_sha256'] in noted)
                            and all(p['arguments'].get(k, 'current' if k == 'snapshot' else None) == v
                                    for k, v in target['arguments'].items())):
                        target['ranges'].append(p['range'])
            prev = rounds[index - 1]['context']['experiment_decision'][KEY] if index else {}
            if (_pending(pending) == missing and sorted(missing_notes) == previous_notes
                    and prev.get('pending_reads') == missing and prev.get('note_receipts', []) == previous_notes):
                raise ResearchBlocked('指定评审尚未交付协调要求的同版缺页，原答复和读取回执已保留。')
            report = {'handoff_id': proof['handoff_id'], 'read_receipts': proof['receipt_sha256s'],
                      'pages': proof['pages'], 'notes': notes, 'observations': a['answer']['observations']}
            if work.get('version') == 2:
                report.update(hypotheses=a['answer']['hypotheses'], missing_evidence=a['answer']['missing_evidence'])
            proofs[role].append(report)
            index += 1
    return proofs or None


async def prepare(engine, harness, run_id, record, context, checkpoint):
    proofs = await ensure(engine, harness, run_id, record, context, checkpoint)
    if not proofs:
        return
    parent = engine.store.get('experiment_decisions', record['consensus_parent'])
    proposal = record['reviewed_revision_material']['previous_proposal']
    if (parent['run_id'] != run_id or parent['cycle'] != record['cycle'] or parent['evidence'] != record['evidence']
            or parent['status'] != 'disputed' or parent['proposal'] != proposal or parent['proposal_sha256'] != sha(proposal)):
        raise ValueError('Reviewer reading lost its original disputed proposal')
    context['experiment_decision'][KEY] = {'proposal_sha256': parent['proposal_sha256'], 'roles': proofs,
                                           'execution_authorized': False}
    if record.get('proposal') or record[handoff.KEY]['independent_review'].get('synthesis_assignment'):
        return
    steps = record['consensus_resolution']['resolution']['resolutions']
    issues = {i['id']: i for i in record['consensus_resolution']['issues']}
    inspected = {(target['issue_id'], role) for role, rows in record[KEY]['targets'].items() for target in rows}
    inspected.update((issue, role) for role, notes in record[KEY].get('note_targets', {}).items() for issue in notes.values())
    if all(step['kind'] == 'inspect_evidence' and all((step['issue_id'], role) in inspected for role in
           step.get('reader_roles', [issues[step['issue_id']]['role']])) for step in steps):
        fields = {k: copy.deepcopy(parent[k]) for k in ('experiment_vote_notes_protocol', 'fit_histories',
                  'vote_results_protocol', 'reuse_fact_review_pages') if k in parent}
        await checkpoint(proposal=copy.deepcopy(proposal), proposal_sha256=parent['proposal_sha256'],
                         status='reviewing', consensus_reading_protocol=1, experiment_vote_focus_protocol=2,
                         bind_review_notes_to_vote=True, **fields)
