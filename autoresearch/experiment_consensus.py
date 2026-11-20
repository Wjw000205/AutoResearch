"""A rejected proposal leads to a separately bound successor, not execution."""
import copy

from .engine import ResearchBlocked
from .experiment_workflow import sha
from .storage import now
from .tuning_consensus import issues, mediate

COLLECTION = 'experiment_consensus_tasks'


class ConsensusPending(ResearchBlocked):
    pass


class DecisionDissent(ResearchBlocked):
    """Only actual review dissent can enter mediation; failed gates cannot."""
    pass


def source(record):
    return {k: copy.deepcopy(record.get(k)) for k in
            ('id', 'evidence', 'proposal', 'proposal_sha256', 'votes', 'analyses', 'analysis_reads')}


def validation_runs(engine, run_id, record):
    from . import experiment_evidence_handoff as handoff
    from .saved_validation_configuration import collect
    saved = record.get(handoff.KEY) or {}
    review = saved.get('independent_review') or {}
    if not review:
        return None
    selected = saved.get('request', {}).get('artifact_trial_ids')
    reviewed_packet = review['assignment']['context']['experiment_decision'][handoff.KEY]
    if selected != reviewed_packet['request'].get('artifact_trial_ids'):
        raise ValueError('Selected trials differ from the frozen fact review request')
    if not selected:
        return None
    base = engine.store.get('experiment_evidence', record['evidence']['id']) or {}
    if base.get('run_id') != run_id or sha(base.get('summary')) != record['evidence']['sha256']:
        raise ValueError('Selected validation runs lost their original evidence')
    proposal = record.get('proposal')
    if proposal:
        supplement = handoff.bound_evidence(engine, run_id, record, proposal)
    else:
        from .artifact_fact_review import reference
        from .independent_full_review import authenticate_complete
        digest = reference(saved)['sha256']
        authenticate_complete(engine.store, run_id, review['results'])
        if {r.get('artifact_evidence_sha256') for r in review['results']} != {digest}:
            raise ValueError('Selected trials do not match the independently reviewed artifact evidence')
        supplement = handoff.archived_evidence(engine, run_id, record, digest)
    if not supplement:
        return None
    trial_ids = {t['summary']['trial_id'] for t in supplement.get('trials', [])}
    if (not set(selected).issubset(trial_ids) or ('artifact_trial_ids' in supplement
            and sorted(selected) != supplement['artifact_trial_ids'])):
        raise ValueError('Selected trials differ from the archived validation selection')
    identity = collect(engine, run_id, base['summary'], supplement, selected, '', include_identity=True)
    if identity:
        from .experiment_input_sources import cited_pages, field_sources
        result = {'evidence': record['evidence'], 'artifact_evidence': handoff.packet(saved, for_review=True)['evidence'],
            'result': identity, 'scope': '按运行、尝试和冻结快照关联已有回执。划分指纹来自该运行回报及其宿主记录；历史终评实载权重另需实测留痕。'}
        pages = cited_pages(engine, run_id, record)
        if pages:
            result['previous_proposal_reads'] = pages
        questions = [i['text'] for i in issues(record.get('votes', []))]
        sources = field_sources(engine, run_id, base['summary'], record['evidence'], identity, questions) if questions else None
        if sources:
            result['field_sources'] = sources
        return result


def vote_request(engine, run_id, decision, vote):
    """Return this reviewer's actual frozen and authenticated proposal request."""
    from . import independent_full_review as full
    if full.KEY in vote:
        return full.authenticate(engine.store, run_id, vote)
    from . import experiment_evidence_handoff as handoff
    from .diagnostic_planning_recovery import authenticate
    proof = next(r for r in decision[handoff.KEY]['review_reads'] if r['role'] == vote['role'])
    hid = proof.get('vote_handoff_id') or proof['handoff_id']
    frozen = engine.store.get('handoff_inputs', hid)
    purpose = engine.store.get('handoffs', hid)['purpose']
    if authenticate(engine, run_id, vote['role'], purpose,
                    {k: frozen[k] for k in ('instruction', 'context', 'schema')}, full.answer(vote)) != hid:
        raise ValueError('Proposal vote refers to a different handoff')
    return frozen


def reviewed_material(engine, run_id, parent):
    """Carry facts into a new revision while giving the author and voters fresh assignments."""
    from . import experiment_evidence_handoff as handoff, experiment_artifact_review as review
    from . import independent_full_review as full
    from .saved_validation_configuration import reviewed_configurations
    saved = parent.get(handoff.KEY) or {}
    facts = saved.get('independent_review') or {}
    if not facts.get('results'):
        return {}
    proposal = parent['proposal']
    if (parent['run_id'] != run_id or parent['status'] != 'disputed'
            or sha(proposal) != parent['proposal_sha256'] or not review.require_binding(parent)
            or not handoff.bound_evidence(engine, run_id, parent, proposal)):
        raise ValueError('Consensus revision lost its reviewed supplemental evidence')
    if not full.members(parent['votes']):
        raise ValueError('Consensus revision requires the original reviewers')
    full.authenticate_complete(engine.store, run_id, facts['results'])
    snapshots = []
    for vote in parent['votes']:
        frozen = vote_request(engine, run_id, parent, vote)
        ctx = frozen['context']
        if (vote['proposal_sha256'] != parent['proposal_sha256'] or ctx['decision_proposal'] != proposal
                or ctx['validation_evidence'] != parent['evidence']
                or ctx['artifact_review_results']['results'] != facts['results']
                or ctx['experiment_decision'][handoff.KEY] != handoff.packet(saved, for_review=True)):
            raise ValueError('Original vote did not assess this proposal and supplemental material')
        packet = ctx['experiment_decision']
        audits = reviewed_configurations(packet)
        if 'saved_weight_audit' in packet:
            audits['saved_weight_audit'] = packet['saved_weight_audit']
        snapshots.append(audits)
    if any(audit != snapshots[0] for audit in snapshots):
        raise ValueError('Original reviewers received different validation evidence')
    # Fresh proposal/vote requests use the normal synthesis and review paths.
    # Prior votes, correction requests and page continuations stay on the parent.
    retained = {k: v for k, v in saved.items() if k not in
                ('independent_review', 'blocker', 'review_reads', 'author_read')}
    retained['independent_review'] = {k: facts[k] for k in
        ('assessment', 'author_assignment', 'author_read', 'assignment', 'full_assignments',
         'page_assignments', 'results', 'reads') if k in facts}
    material = {'previous_proposal': proposal}
    selected = validation_runs(engine, run_id, parent)
    if selected:
        material['selected_validation_runs'] = selected
    from .experiment_review_reads import inspections
    links = inspections(engine, run_id, parent)
    if links:
        material['prior_inspections'] = links
    for name, audit in snapshots[0].items():
        if sha(audit['result']) != audit['sha256']:
            raise ValueError('Saved validation evidence changed before consensus revision')
        material[name] = audit
    return {handoff.KEY: {**retained, 'status': 'synthesizing', 'owner': 'engineer'},
            'reviewed_revision_material': material}


async def decide(engine, harness, run_id, transport, impl):
    from .experiment_decisions import snapshot, _archive, packet, DECISIONS
    from .proposal_request_contract import VERSION
    evidence = await engine._read_consensus_check(run_id, snapshot, engine, run_id, transport)
    from .combination_settings import ensure as ensure_combination_settings
    if await ensure_combination_settings(engine, harness, run_id, evidence):
        raise ConsensusPending('组合验证实验设置正在由agent制定与独立评审，随后继续当前实验。')
    from .combination_validation import prepare as prepare_combinations
    await engine._read_consensus_check(run_id, prepare_combinations, engine, run_id, evidence)
    ref = _archive(engine, run_id, evidence)
    from .artifact_pair_recovery import route as artifact_successor
    root_id = artifact_successor(engine, run_id, ref)
    from .architecture_prompt_recovery import route as architecture_successor
    root_id = architecture_successor(engine, run_id, root_id, ref)
    from .yaml_review_recovery import route as yaml_successor
    root_id = yaml_successor(engine, run_id, root_id, ref)
    key = root_id + ':consensus'
    task = engine.store.get(COLLECTION, key)
    current_id = task['current_id'] if task else root_id
    if task:
        if task['evidence'] != ref or current_id != (task['rounds'][-1]['next_id'] if task['rounds'] else root_id):
            raise ValueError('Consensus successor pointer changed')
        for round_ in task['rounds']:
            parent = engine.store.get(DECISIONS, round_['parent_id']) or {}
            if sha(source(parent)) != round_['source_sha256']:
                raise ValueError('Original proposal or independent dissent changed')
        current = engine.store.get(DECISIONS, current_id) or {}
        if task['rounds']:
            previous = task['rounds'][-1]
            if current.get('consensus_parent') != previous['parent_id'] or current.get('consensus_resolution') != previous['resolution']:
                raise ValueError('Consensus child lost its reviewed source binding')
        if current.get('status') == 'disputed':
            if 'pending' not in task:
                task['pending'] = {'parent_id': current_id, 'source_sha256': sha(source(current))}
            pending = task['pending']
            if pending['parent_id'] != current_id or pending['source_sha256'] != sha(source(current)):
                raise ValueError('Consensus source changed during negotiation')
            def save():
                engine.check_active(run_id)
                task['updated_at'] = now()
                engine.store.put(COLLECTION, key, task)
            context = packet(evidence, ref)
            if 'mediation' not in pending:
                from .experiment_review_reads import inspections
                links = await engine._read_consensus_check(run_id, inspections, engine, run_id, current)
                if links:
                    context['experiment_decision']['prior_inspections'] = links
                from . import experiment_evidence_handoff as handoff
                if current.get(handoff.KEY):
                    context['experiment_decision'][handoff.KEY] = handoff.packet(current[handoff.KEY], for_review=True)
                    selected = await engine._read_consensus_check(run_id, validation_runs, engine, run_id, current)
                    if selected:
                        context['experiment_decision']['selected_validation_runs'] = selected
            from .proposal_request_contract import architecture_iteration, focus_architecture_context
            if 'mediation' not in pending and architecture_iteration(evidence, VERSION):
                focus_architecture_context(context)
            elif 'mediation' not in pending and (current['proposal'].get('training_response') or {}).get('disposition') == 'instrument':
                from .proposal_request_contract import scope_diagnostic_routes
                from .scoped_work_order_contract import describe_cpu_check
                scope_diagnostic_routes(context, evidence)
                describe_cpu_check(engine, run_id, context, work_order_identity=3)
            resolution = await mediate(engine, harness, run_id, pending, context, source(current),
                                       issues(current['votes']), save, assign_readers=True)
            next_id = ref['id'] + ':consensus:' + sha([current_id, resolution])
            child = {'id': next_id, 'run_id': run_id, 'cycle': engine.cycle(run_id), 'evidence': ref,
                'status': 'proposing', 'analyses': copy.deepcopy(current['analyses']),
                'analysis_reads': copy.deepcopy(current['analysis_reads']), 'votes': [],
                'created_at': now(), 'scientific_approval': False, 'provenance': 'live',
                'parameter_path_contract_version': 1, 'proposal_request_contract_version': VERSION,
                'proposal_reference_contract_version': 2, 'cpu_check_work_order_identity': 3,
                'consensus_parent': current_id, 'consensus_resolution': resolution}
            if not engine.store.get(DECISIONS, next_id):
                child.update(await engine._read_consensus_check(run_id, reviewed_material, engine, run_id, current))
            if 'tuning_point_reads' in current:
                child['tuning_point_reads'] = copy.deepcopy(current['tuning_point_reads'])
                child['initial_point_reads'] = copy.deepcopy(current['initial_point_reads'])
            # The original rejected proposal, votes and paid requests remain untouched.
            engine.store.mutate(DECISIONS, next_id, lambda old: old or child)
            task['rounds'].append({**task.pop('pending'), 'resolution': resolution, 'next_id': next_id})
            task['current_id'] = current_id = next_id
            save()
    try:
        result = await impl(engine, harness, run_id, transport, evidence=evidence, record_key=current_id)
        if task:
            task.update(status='resolved', resolved_decision_id=result[0]['id'], updated_at=now())
            engine.check_active(run_id)
            engine.store.put(COLLECTION, key, task)
        return result
    except DecisionDissent as exc:
        current = engine.store.get(DECISIONS, current_id) or {}
        from .independent_full_review import members
        if current.get('status') != 'disputed' or not current.get('proposal') or not members(current.get('votes', [])):
            raise
        if not task:
            task = {'id': key, 'run_id': run_id, 'evidence': ref, 'current_id': current_id,
                    'rounds': [], 'created_at': now()}
        task.update(status='reconciling', updated_at=now())
        engine.check_active(run_id)
        engine.store.put(COLLECTION, key, task)
        engine.store.event(run_id, 'experiment.consensus_requested',
            '已保留评审异议，交 director 协调、工程师修订，再由本轮指定的评审者独立复审。',
            {'decision_id': current_id, 'consensus_id': key, 'execution_authorized': False})
        raise ConsensusPending('异议已进入共识修订，尚未授权执行。') from exc
