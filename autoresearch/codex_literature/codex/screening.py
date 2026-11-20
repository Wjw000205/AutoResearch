from .query_planner import fingerprint
from ...storage import now

CANDIDATE_FIELDS = ('id', 'title', 'abstract_excerpt', 'year', 'publication_date',
    'doi', 'pmid', 'citation_count', 'sources', 'relevance_score', 'oa_pdf_url')
VERIFICATION_FIELDS = ('verified', 'verification', 'venue', 'ccf', 'checked_at',
    'provenance', 'date_granularity', 'abstract_truncated')

IDS = {'type': 'array', 'items': {'type': 'string', 'maxLength': 500}, 'uniqueItems': True}
SCHEMA = {'type': 'object', 'additionalProperties': False,
    'properties': {'selected_ids': {**IDS, 'maxItems': 40}, 'seed_ids': {**IDS, 'maxItems': 3},
        'rationale': {'type': 'string', 'maxLength': 2400},
        'limitations': {'type': 'array', 'maxItems': 10, 'items': {'type': 'string', 'maxLength': 500}}},
    'required': ['selected_ids', 'seed_ids', 'rationale', 'limitations']}
INSTRUCTION = ('只根据候选论文的题目、摘要和元数据筛选，不能宣称阅读全文或实验结论已核验。'
    '选最多40篇相关论文selected_ids，从中选最多3篇seed_ids用于前向/后向引用扩展。'
    '优先研究问题和机制匹配、因果实验、相近方法、反证；引用次数只作辅助，保留新论文与矛盾证据。'
    '只使用提供的已核验ID，宁缺勿滥；解释选择理由和摘要/覆盖缺口。后续再选5–15篇获取全文。'
    'candidates中的verified、verification、venue和ccf保留本批检索的身份与出处核验证据；'
    'evidence是阶段开始时的快照，verified_papers为空不表示本批候选未经核验。'
    '按候选自带的核验字段判断资格，仍须筛选相关性；元数据核验不代表已读原文或科学结论成立。')


async def screen(engine, harness, run_id, question, papers, context):
    # Compact originals are retained in the survey, never destroyed by screening.
    candidates = [{k: p.get(k) for k in (*CANDIDATE_FIELDS, *VERIFICATION_FIELDS)}
        for p in papers if p.get('verified') is True][:120]
    inputs = {'question': question, 'candidates': candidates, 'phase': engine.store.get('runs', run_id)['stage'],
              'date_policy': context.get('literature_date_policy'), 'selected_idea_hash': context.get('selected_idea_hash'),
              'literature_reasoning_effort': context.get('literature_reasoning_effort')}
    from ...literature_discovery import enabled, POLICY, GUIDANCE
    discovery = enabled(context)
    if discovery:
        inputs['literature_discovery_policy'] = POLICY
    key = f'{run_id}:literature-screen:{fingerprint(inputs)}'
    saved = engine.store.get('literature_inputs', key)
    if not saved:
        # Reuse accepted historical screenings without rewriting their frozen
        # inputs. A rejected, evidence-poor decision has no accepted checkpoint
        # and receives a distinct assignment bound to the complete evidence.
        legacy_inputs = {**inputs, 'candidates': [
            {k: p.get(k) for k in CANDIDATE_FIELDS} for p in candidates]}
        legacy_key = f'{run_id}:literature-screen:{fingerprint(legacy_inputs)}'
        legacy = engine.store.get('literature_inputs', legacy_key)
        if legacy:
            inputs, key, saved = legacy_inputs, legacy_key, legacy
    if saved:
        if saved.get('inputs') != inputs or saved.get('result_sha256') != fingerprint(saved.get('result')):
            raise ValueError('Screening checkpoint is inconsistent')
        result = saved['result']
    else:
        result = await harness.ask(run_id, 'scout', 'literature_screen:' + fingerprint(inputs)[:20],
            INSTRUCTION + (GUIDANCE if discovery else ''), {**context, **inputs, 'codex_literature_protocol': 1}, SCHEMA)
    allowed = {p['id'] for p in candidates}
    if not set(result['selected_ids']) <= allowed or not set(result['seed_ids']) <= set(result['selected_ids']):
        raise ValueError('Screening must use supplied verified IDs and seeds must be selected')
    if allowed and not result['selected_ids']:
        raise ValueError('Screening rejected every candidate; revise the research query before proceeding')
    engine.check_active(run_id)
    if not saved:
        engine.store.put('literature_inputs', key, {'id': key, 'run_id': run_id, 'kind': 'literature_screening',
            'inputs': inputs, 'result': result, 'result_sha256': fingerprint(result), 'completed_at': now()})
    return result
