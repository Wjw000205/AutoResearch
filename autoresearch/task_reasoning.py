"""Harness-owned task routing.

Design references: RouteLLM's separation of routing from inference
(https://github.com/lm-sys/RouteLLM/blob/main/routellm/routers/routers.py) and
vLLM Semantic Router's ordered decisions and eligible model pools
(https://github.com/vllm-project/semantic-router/tree/main/config/recipes/agent).
These rules use the host assignment and its output contract.
"""

MODEL = 'gpt-6.1-sol'
EFFORTS = {
    'experiment_train': 'low',  # Invoke the exact frozen job and return its host receipt.
    'literature_query_plan': 'low',
    'literature_note_extract': 'low',
    'literature_screen': 'medium',
    'related_work_queries': 'medium',
}


# V1 above is used to replay old packets. V2 freezes the matched rule at handoff creation.
_DECISION_FIELDS = {'approve', 'approved', 'required_changes', 'verdict', 'disposition',
                    'suggested_action', 'acceptance', 'selection_script'}
_DEEP_WORK = {'design', 'implementation', 'repair', 'rapid_implement', 'deep_paper', 'protocol_amendment',
              'innovation_candidate', 'innovation_compare', 'experiment_decide', 'loss_propose'}
_BOUNDED_WORK = {
    'literature_query_plan': ('simple', '根据已给定的研究问题整理初步检索词。'),
    'literature_note_extract': ('simple', '从已经读过的材料提取结构化笔记。'),
    'literature_screen': ('moderate', '比较候选论文的标题、摘要与研究问题的相关性。'),
    'related_work_queries': ('moderate', '围绕已有研究方案规划多条相关工作检索式。'),
}


def _decision_output(schema):
    pending = [schema]
    while pending:
        node = pending.pop()
        if not isinstance(node, dict):
            continue
        fields = node.get('properties', {})
        if _DECISION_FIELDS.intersection(fields):
            return True
        pending.extend(fields.values())
        pending.append(node.get('items'))
        for key in ('$defs', 'definitions', 'patternProperties'):
            pending.extend(node.get(key, {}).values())
        for key in ('if', 'then', 'else', 'additionalProperties'):
            if isinstance(node.get(key), dict):
                pending.append(node[key])
        for key in ('oneOf', 'anyOf', 'allOf'):
            pending.extend(node.get(key, []))
    return False


def classify(purpose, schema, context=None):
    kind = purpose.partition(':')[0]
    if _decision_output(schema):
        return 'complex', 'decision_contract', '交付内容包含审批、裁决或执行计划。'
    task=(context or {}).get('implementation_evidence_task',{})
    fields=schema.get('properties',{}) if isinstance(schema,dict) else {}
    if (task.get('kind')=='needs_execution' and task.get('invocation_contract_version')==2
            and task.get('approved_checks') and set(fields)=={'ready','entrypoint','requirement_ids',
                'report_paths','timeout_seconds','allowance_reference','summary','remaining','invocation'}):
        return 'simple','frozen_check_dispatch','从已批准检查和已读入口提取固定调用参数；执行授权由宿主验证，不作科学裁决。'
    if (task.get('kind')=='needs_review' and task.get('evidence_domain')=='host_execution'
            and set(fields)=={'status','evidence_ids','summary','remaining'}):
        return 'simple','host_evidence_extract','从冻结宿主记录提取技术事实和读取回执，不作科学裁决或执行计划。'
    if kind in _DEEP_WORK or set(kind.split('_')) & {'review', 'diagnose', 'vote', 'adjudication'}:
        return 'complex', 'research_judgment', '任务需要科学判断、设计、诊断或独立复核。'
    if kind in _BOUNDED_WORK:
        tier, reason = _BOUNDED_WORK[kind]
        return tier, kind, reason
    if context and context.get('training_context_protocol') == 1 and context.get('plan_sha256') and context.get('trial'):
        from .python_training import FINAL
        if schema == FINAL:
            return 'simple', 'frozen_receipt', '冻结作业只需按计划派发并返回试验编号和完成状态。'
    # New query-only task types can adopt the same route without registering a name.
    fields = schema.get('properties', {}) if isinstance(schema, dict) else {}
    queries = fields.get('queries')
    if (set(fields) == {'queries'} and schema.get('required') == ['queries']
            and schema.get('additionalProperties') is False and isinstance(queries, dict)
            and queries.get('type') == 'array' and isinstance(queries.get('items'), dict)
            and queries['items'].get('type') == 'string'):
        return 'moderate', 'query_contract', '交付只包含检索式列表。'
    return 'complex', 'unclassified', '任务尚无轻量规则，使用角色的复杂推理配置。'


def assignment_route(intelligence, role, purpose, schema, context):
    from .intelligence import ProviderError
    if role not in {'director', 'scout', 'engineer'} or not hasattr(intelligence, 'provider_for'):
        return {}
    try:
        provider = intelligence.provider_for(role)
    except ProviderError:
        # A handoff may be drafted before the operator configures its backend.
        # Normal admission still requires the actual provider at dispatch.
        return {}
    if provider.get('protocol') != 'local_codex':
        return {}
    tier, rule, reason = classify(purpose, schema, context)
    route = {'tier': tier, 'rule': rule, 'reason': reason}
    if provider.get('bounded_model'):
        route['model'] = provider['model'] if tier == 'complex' else provider['bounded_model']
    return {'task_reasoning_version': 2, 'task_route': route}


def routed_provider(provider, context):
    purpose = str(context.get('_purpose', '')).partition(':')[0]
    if context.get('_task_reasoning_version') == 2 and provider.get('protocol') == 'local_codex':
        route = context['_task_route']
        tier = route['tier']
        if tier == 'complex':
            deep = route['rule'] != 'unclassified'
            effort = provider.get('deep_reasoning_effort', 'max') if deep else provider.get('reasoning_effort', 'high')
            return {**provider, 'model': route.get('model', provider['model']), 'task_route': route, 'task_reasoning_effort': 'high' if effort in ('low', 'medium') else effort,
                    'task_timeout': provider.get('deep_timeout_seconds', 3600) if deep else provider.get('timeout_seconds', 1800)}
        return {**provider, 'model': route.get('model', MODEL), 'task_reasoning_effort': {'simple': 'low', 'moderate': 'medium'}[tier],
                'task_route': route}
    if (context.get('_task_reasoning_version') == 1 and purpose in EFFORTS
            and provider.get('protocol') == 'local_codex'):
        return {**provider, 'model': MODEL, 'task_reasoning_effort': EFFORTS[purpose]}
    return provider


def request_provider(intelligence, prepared):
    return routed_provider(intelligence.provider_for(prepared['role']), prepared['context'])
