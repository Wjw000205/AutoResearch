"""One same-reviewer recheck after ambiguous parameter/evidence path guidance."""
import copy

from .experiment_workflow import sha

KEY = 'parameter_path_reviews'
AMBIGUOUS = '路径来自 /job_parameters。'
GUIDANCE = ('parameter_search.factor_path 与 training_response.factor_paths 都是单个 job 参数对象内的相对 JSON pointer，'
            '例如 /common/lr；不能添加 /job_parameters/<job> 前缀，也不按 baseline/proposed 重复同一个因素。'
            '只有 evidence_paths 使用冻结证据根下的完整路径，例如 /job_parameters/<job>/common/lr。'
            '参数相对路径同时映射到所选种子的 baseline/proposed 控制；必须逐侧核对实际差异。')


def guidance(instruction, *, method_local=False):
    contract = GUIDANCE if not method_local else GUIDANCE.replace(
        '参数相对路径同时映射到所选种子的 baseline/proposed 控制；必须逐侧核对实际差异。',
        'shared 路径映射到双方控制；baseline/proposed 专有路径只改变对应方法，另一侧必须复用精确已完成对照。')
    return instruction.replace(AMBIGUOUS, '参数位置从 /job_parameters 查得，但因素字段使用单个参数对象内的相对路径。').replace(
        'factor_paths 是 job_parameters 中实际变化参数的 JSON pointer',
        'factor_paths 是单个 job 参数对象内实际变化参数的相对 JSON pointer') + '\n' + contract


def audit(record, evidence):
    from .experiment_decisions import validate_proposal
    from .parameter_search import propose, control_records
    from .experiment_analysis import _param_diff
    proposal = record['proposal']
    if sha(evidence) != record['evidence']['sha256'] or sha(proposal) != record['proposal_sha256']:
        raise ValueError('Parameter path review evidence/proposal binding changed')
    validate_proposal(proposal, evidence, record['evidence']['sha256'])
    unit = propose(proposal['parameter_search'], evidence)
    controls = control_records(proposal['parameter_search'], evidence)
    return {'version': 1, 'factor_path': proposal['parameter_search']['factor_path'],
            'pairs': [{'control_trial_id': control['id'], 'candidate_job': name,
                       'control_params_sha256': sha(control['params']), 'candidate_params_sha256': sha(params),
                       'seed': params['seed'], 'variant': params['variant'],
                       'differences': _param_diff(control['params'], params)}
                      for control, (name, params) in zip(controls, unit['jobs'])],
            'scope': 'Host reconstruction of proposed configurations, not execution or scientific evidence.',
            'execution_authorized': False, 'approval_inherited': False}


def original(engine, run_id, record, vote, key=None):
    from .experiment_vote_clarification import _original
    from .engine import digest
    key, context = _original(engine, run_id, record, vote, key)
    owner = engine.store.get('handoffs', key)
    frozen = engine.store.get('handoff_inputs', key)
    payload = {k: frozen[k] for k in ('instruction', 'context', 'schema')}
    answer = {k: v for k, v in vote.items() if k != 'role'}
    if (owner.get('status') != 'completed' or owner.get('input_hash') != digest(payload)
            or owner.get('output_hash') != digest(answer)):
        raise ValueError('Parameter path review requires its completed original reviewer receipt')
    return key, context, frozen['instruction']


async def reconsider(engine, harness, run_id, record, evidence, checkpoint):
    from .experiment_decisions import VOTE
    from .experiment_vote_clarification import verify
    if record.get('proposal', {}).get('action') != 'search_parameters':
        return record['votes']
    assignments = copy.deepcopy(record.get(KEY, {}))
    votes = copy.deepcopy(record['votes'])
    material = None
    for i, vote in enumerate(votes):
        role = vote['role']
        saved = assignments.get(role)
        if saved:
            prior = {'role': role, **saved['original_vote']}
            original(engine, run_id, record, prior, saved['original_handoff'])
            material = material or audit(record, evidence)
            if saved['context']['experiment_decision']['parameter_path_contract']['audit'] != material:
                raise ValueError('Parameter path review audit changed')
            if saved.get('resolved_vote'):
                if vote != saved['resolved_vote']:
                    raise ValueError('Parameter path review result changed')
                verify(engine, run_id, record, vote, saved)
                continue
        else:
            # Wording identifies a request for clarification only, never approval.
            if vote.get('approve') is not False or not any(
                    'factor_path' in issue and '/job_parameters/' in issue for issue in vote.get('issues', [])):
                continue
            key, original_context, old_instruction = original(engine, run_id, record, vote)
            if AMBIGUOUS not in old_instruction or GUIDANCE in old_instruction:
                continue  # No retry for dissent under the already-explicit contract.
            material = material or audit(record, evidence)
            context = {k: copy.deepcopy(original_context[k]) for k in (
                'training_context_protocol', 'experiment_workflow_protocol', 'research_cycle',
                'validation_evidence', 'evidence_sha256', 'decision_proposal', 'proposal_sha256')}
            context['experiment_decision'] = {k: copy.deepcopy(v) for k, v in
                original_context['experiment_decision'].items() if k in (
                    'binding', 'counts', 'coverage_complete', 'budget', 'configuration_queue',
                    'iteration_policy', 'paths', 'scientific_approval', 'dataset_optimization')}
            prior = {k: v for k, v in vote.items() if k != 'role'}
            context['experiment_decision']['parameter_path_contract'] = {
                'prior_vote': prior, 'previous_handoff_id': key, 'audit': material,
                'approval_inherited': False, 'contract': GUIDANCE}
            schema = copy.deepcopy(VOTE)
            for field in ('evidence_sha256', 'proposal_sha256'):
                schema['properties'][field] = {'const': prior[field]}
            schema['allOf'] = [{'if': {'properties': {'approve': {'const': True}}},
                'then': {'properties': {'issues': {'maxItems': 0}}},
                'else': {'properties': {'issues': {'minItems': 1}}}}]
            saved = {'purpose': 'experiment_vote:' + record['proposal_sha256'] + ':parameter-path-contract-v1',
                     'context': context, 'schema': schema, 'original_vote': prior, 'original_handoff': key,
                     'instruction': '对同一提案作一次独立复核。旧提示“路径来自 /job_parameters”没有区分参数位置与证据引用，'
                         '宿主现明确实际校验契约：' + GUIDANCE +
                         '宿主提供逐侧配置哈希与差异，只是只读重建，不是实验已执行或批准。原否决和提案保持不变。'
                         '核对你的原问题及全部实质门禁；仍有阻塞请 approve=false 并具体列明，不能因提示澄清而自动批准。'
                         '同意时 approve=true、issues=[]，局限留在 rationale。必要时按原 validation_evidence 定向读取，'
                         '不扩大网格、不重跑控制、不激活候选补丁、不开放留出集。返回严格 JSON。'}
            assignments[role] = saved
            await checkpoint(**{KEY: assignments}, status='reviewing')
        response = await harness.ask(run_id, role, saved['purpose'], saved['instruction'], saved['context'], saved['schema'])
        resolved = {'role': role, **response}
        verify(engine, run_id, record, resolved, saved)
        saved['resolved_vote'] = resolved
        votes[i] = resolved
        await checkpoint(**{KEY: assignments}, votes=votes)
    return votes
