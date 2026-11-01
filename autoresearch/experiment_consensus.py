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


async def decide(engine, harness, run_id, transport, impl):
    from .experiment_decisions import snapshot, _archive, packet, DECISIONS
    from .proposal_request_contract import VERSION
    evidence = await engine._read_consensus_check(run_id, snapshot, engine, run_id, transport)
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
            resolution = await mediate(engine, harness, run_id, pending, context, source(current),
                                       issues(current['votes']), save)
            next_id = ref['id'] + ':consensus:' + sha([current_id, resolution])
            child = {'id': next_id, 'run_id': run_id, 'cycle': engine.cycle(run_id), 'evidence': ref,
                'status': 'proposing', 'analyses': copy.deepcopy(current['analyses']),
                'analysis_reads': copy.deepcopy(current['analysis_reads']), 'votes': [],
                'created_at': now(), 'scientific_approval': False, 'provenance': 'live',
                'parameter_path_contract_version': 1, 'proposal_request_contract_version': VERSION,
                'proposal_reference_contract_version': 2,
                'consensus_parent': current_id, 'consensus_resolution': resolution}
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
        if current.get('status') != 'disputed' or not current.get('proposal') or len(current.get('votes', [])) != 3:
            raise
        if not task:
            task = {'id': key, 'run_id': run_id, 'evidence': ref, 'current_id': current_id,
                    'rounds': [], 'created_at': now()}
        task.update(status='reconciling', updated_at=now())
        engine.check_active(run_id)
        engine.store.put(COLLECTION, key, task)
        engine.store.event(run_id, 'experiment.consensus_requested',
            '已保留三方具体异议，交 director 协调、工程师修订，再由三方独立复审。',
            {'decision_id': current_id, 'consensus_id': key, 'execution_authorized': False})
        raise ConsensusPending('异议已进入共识修订，尚未授权执行。') from exc
