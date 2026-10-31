"""Resolve bounded host projections; context contracts are never measurements."""
from __future__ import annotations

from .experiment_workflow import sha

SCOPE_PATHS = {'/tuning_scope/' + name for name in
               ('strategy', 'stopping_rule', 'next_factors', 'pending_factor_count')}
CONTEXT_PATHS = {'/training_investigation/response_contract',
                 '/parameter_gate_followup/host_gate_requirements',
                 '/parameter_gate_followup/source_proposal'}


def host_gate_requirements():
    return {'frozen_criteria_must_match_exactly': True,
            'completion_with_alerts_requires': ['continue', 'resolved'],
            'continue_requires': 'supported batch_noise or telemetry, with actual epoch/validation evidence',
            'resolved_requires': 'new completed paired verification, supported outcome, no unresolved focus alerts',
            'unknown_cause_is_not_resolved': True,
            'no_new_thresholds_or_training_permission': True}


def resolve(path, evidence, director):
    """The caller authenticates the director's exact paid input and output first."""
    from .experiment_decisions import _pointer

    if path not in SCOPE_PATHS | CONTEXT_PATHS:
        return None
    context, answer = director['context'], director['answer']
    if path in SCOPE_PATHS:
        from .tuning_scope import compact
        scope = evidence.get('tuning_scope') or {}
        if (scope.get('status') != 'accepted' or not scope.get('plan')
                or sha(scope['plan']) != scope.get('plan_sha256')):
            raise ValueError('Tuning summary requires its exact accepted plan')
        field = path.rsplit('/', 1)[1]
        value = compact(scope)[field]
        source = ('/tuning_scope/plan/' + field
                  if field in {'strategy', 'stopping_rule'} else '/tuning_scope')
        result = {'evidence_path': source,
                  'operation': 'identity' if '/plan/' in source else 'tuning_scope_' + field + '_v1'}
    else:
        result = {'context_path': '/experiment_decision' + path,
                  'evidence_kind': 'host_contract', 'measured_evidence': False,
                  'proposal_evidence_path_allowed': False}
        if path == '/training_investigation/response_contract':
            from .training_investigation import response_contract
            value = response_contract(evidence['training_investigation'])
            result.update(source_evidence_path='/training_investigation',
                          operation='training_response_contract_v' + str(value['version']))
        elif path == '/parameter_gate_followup/host_gate_requirements':
            value = host_gate_requirements()
            result['operation'] = 'parameter_host_gate_contract_v1'
        else:
            gate = context['experiment_decision']['parameter_gate_followup']
            value = gate['source_proposal']
            if (sha(value) != gate['source_proposal_sha256']
                    or sha(value) != answer.get('source_proposal_sha256')
                    or value.get('evidence_sha256') != answer['evidence_sha256']):
                raise ValueError('Source proposal does not match the authenticated director assignment')
            result.update(evidence_kind='prior_proposal', operation='authenticated_source_proposal',
                          source_proposal_sha256=sha(value))
    supplied = _pointer(context['experiment_decision'], path)
    if sha(supplied) != sha(value):
        raise ValueError('Supplied host projection differs from its bound source')
    try:
        original = _pointer(evidence, path)
    except (KeyError, IndexError, TypeError):
        pass
    else:
        if sha(original) != sha(value):
            raise ValueError('Original evidence conflicts with the host projection')
    return {**result, 'cited_path': path, 'value_sha256': sha(value)}


def verify_context(saved):
    """Bind context-only citations back to the already authenticated predecessor."""
    director = saved['director']
    gate = director['context']['experiment_decision']['parameter_gate_followup']
    if (gate.get('source_proposal') != saved['source_proposal']
            or gate.get('source_proposal_sha256') != saved['source_proposal_sha256']
            or gate.get('frozen_assessment') != saved['assessment']
            or gate.get('assessment_sha256') != sha(saved['assessment'])
            or director['answer'].get('source_proposal_sha256') != saved['source_proposal_sha256']
            or director['answer'].get('assessment_sha256') != sha(saved['assessment'])):
        raise ValueError('Parameter gate director context lost its original proposal/criteria binding')


GUIDANCE = ('\n引用契约：原始实测 evidence_paths 指向 validation_evidence 的真实路径。'
            '调参策略和停止规则原路径为 /tuning_scope/plan/strategy、/tuning_scope/plan/stopping_rule；'
            'next_factors、pending_factor_count 是 /tuning_scope 的计划/覆盖摘要，不能证明已实测。'
            'training_investigation.response_contract、parameter_gate_followup.host_gate_requirements '
            '属于请求中提供的宿主规则，source_proposal 属于已绑定历史提案；它们不是实测证据。'
            '这些上下文引用只在条款说明中使用，不代替真实参数搜索、训练复验或稳定性证据。')
