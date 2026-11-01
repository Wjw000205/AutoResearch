"""Small, immutable research commitments and counterevidence for later decisions."""
import copy

from .experiment_workflow import sha

KEY = 'research_anchor'
COLLECTION = 'research_decision_anchors'
PHASES = {'analysis': 0, 'proposal': 1, 'vote': 2}
GUIDANCE = '''
research_anchor 是原研究问题、机制主张、关键对照及未关闭反证的宿主摘要，有精确来源引用。
在 hypothesis/rationale/verification 中说明本步解决的是哪项科学问题或工程阻塞，区分普通预测分数与机制归因。
历史负结果保留为科学结论的边界；其中历史停止条件不替代当前已授权的人工变更和执行协议，不能自动阻塞获批工程维修。
原始目标文字中的预算、次数及资源上限是历史记录；当前限制以本次请求绑定的预算和执行协议为准，不恢复旧默认上限。
完成配置迁移、静态检查、CPU检查或调参覆盖不等于科学主张通过；也不要为补写主张而重跑已完成实验。
工程修复只关闭工程问题。后续结论仍须真实可比实验、独立评议及既有执行门禁。
'''


def freeze(engine, run_id):
    consensus = engine.store.get('consensus', run_id, {})
    proposal = consensus.get('proposal')
    if not isinstance(proposal, dict) or not proposal:
        return None
    proposal_sha = sha(proposal)
    if consensus.get('proposal_hash', proposal_sha) != proposal_sha:
        raise ValueError('Research anchor proposal identity changed')
    agenda = proposal.get('research_agenda') or {}
    run = engine.store.get('runs', run_id, {})
    transition_id = run.get('state', {}).get('dataset_transition')
    transition = engine.store.get('dataset_transitions', transition_id or '', {})
    request = transition.get('request') or {}
    counterevidence, sources = [], [{'collection': 'consensus', 'key': run_id,
                                     'proposal_sha256': proposal_sha}]
    for row in engine.store.list('research_cycles', run_id):
        summary = row.get('validation_summary') or {}
        checks = (summary.get('acceptance_checks') or {}).get('checks', [])
        failed = [{k: copy.deepcopy(c[k]) for k in ('metric', 'passed', 'relative_improvement',
            'minimum_relative_improvement', 'comparison', 'issues') if k in c}
                  for c in checks if c.get('passed') is False]
        if failed:
            counterevidence.append({'cycle': row.get('cycle'), 'record_id': row['id'],
                'validation_summary_sha256': sha(summary), 'reported_failed_checks': failed,
                'status': 'retained_historical_counterevidence_not_automatically_resolved'})
            sources.append({'collection': 'research_cycles', 'key': row['id'],
                            'validation_summary_sha256': sha(summary)})
    override = None
    if request:
        override = {k: copy.deepcopy(request[k]) for k in ('operator_instruction', 'performance_policy', 'targets') if k in request}
        override.update(source_id=transition_id, request_sha256=sha(request),
            meaning='Current authorized scope; does not rewrite historical results or certify the original mechanism claim')
        sources.append({'collection': 'dataset_transitions', 'key': transition_id, 'request_sha256': sha(request)})
    body = {'version': 1, 'objective': run.get('objective'), 'origin': {
        'proposal_sha256': proposal_sha, 'title': proposal.get('title'), 'hypothesis': proposal.get('hypothesis'),
        **{k: copy.deepcopy(agenda[k]) for k in ('research_question', 'mechanism_claim', 'competing_explanation',
            'decisive_experiment', 'falsification', 'scope_and_significance') if k in agenda}},
        'historical_counterevidence': counterevidence, 'current_operator_scope': override,
        'sources': sources, 'scientific_approval': False, 'execution_authorized': False,
        'scope': 'Reported commitments and evidence boundaries, not new measurements, read credit or a new execution policy'}
    ref = {'id': run_id + ':research-anchor:' + sha(body), 'sha256': sha(body)}
    engine.store.mutate(COLLECTION, ref['id'], lambda old: old or {'id': ref['id'], 'run_id': run_id, 'body': body})
    return ref


def read(engine, run_id, ref):
    saved = engine.store.get(COLLECTION, ref['id'], {})
    if saved.get('run_id') != run_id or sha(saved.get('body')) != ref['sha256']:
        raise ValueError('Research anchor snapshot changed')
    return {**ref, **copy.deepcopy(saved['body'])}


def has_assignment(engine, run_id, prefix):
    return bool(engine.store.record_keys('handoffs', run_id,
        predicate="json_extract(data,'$.purpose') LIKE ?", parameters=(prefix + '%',), limit=1))


async def for_phase(engine, run_id, record, context, instruction, checkpoint, phase):
    saved = record.get(KEY)
    if not saved:
        ev = record['evidence']['sha256']
        prefix = {'analysis': 'experiment_diagnose:' + ev, 'proposal': 'experiment_decide:' + ev,
                  'vote': 'experiment_vote:' + str(record.get('proposal_sha256', ''))}[phase]
        eligible = (not record.get('analyses') if phase == 'analysis' else
                    not record.get('proposal') and not record.get('proposal_corrections')
                    and not record.get('recovery_answer') if phase == 'proposal' else
                    bool(record.get('proposal')) and not record.get('votes'))
        if not eligible or has_assignment(engine, run_id, prefix):
            return context, instruction
        ref = freeze(engine, run_id)
        if not ref:
            return context, instruction
        saved = {'reference': ref, 'introduced_for': phase}
        await checkpoint(**{KEY: saved})
    if PHASES[phase] < PHASES[saved['introduced_for']]:
        return context, instruction
    result = copy.deepcopy(context)
    result[KEY] = read(engine, run_id, saved['reference'])
    return result, instruction if GUIDANCE in instruction else instruction + GUIDANCE
