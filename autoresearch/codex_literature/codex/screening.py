from .query_planner import fingerprint
from ...storage import now

IDS = {'type': 'array', 'items': {'type': 'string', 'maxLength': 500}, 'uniqueItems': True}
SCHEMA = {'type': 'object', 'additionalProperties': False,
    'properties': {'selected_ids': {**IDS, 'maxItems': 40}, 'seed_ids': {**IDS, 'maxItems': 3},
        'rationale': {'type': 'string', 'maxLength': 2400},
        'limitations': {'type': 'array', 'maxItems': 10, 'items': {'type': 'string', 'maxLength': 500}}},
    'required': ['selected_ids', 'seed_ids', 'rationale', 'limitations']}
INSTRUCTION = ('只根据候选论文的题目、摘要和元数据筛选，不能宣称阅读全文或实验结论已核验。'
    '选最多40篇相关论文selected_ids，从中选最多3篇seed_ids用于前向/后向引用扩展。'
    '优先研究问题和机制匹配、因果实验、相近方法、反证；引用次数只作辅助，保留新论文与矛盾证据。'
    '只使用提供的已核验ID，宁缺勿滥；解释选择理由和摘要/覆盖缺口。后续再选5–15篇获取全文。')


async def screen(engine, harness, run_id, question, papers, context):
    # Compact originals are retained in the survey, never destroyed by screening.
    candidates = [{k: p.get(k) for k in ('id', 'title', 'abstract_excerpt', 'year', 'publication_date',
        'doi', 'pmid', 'citation_count', 'sources', 'relevance_score', 'oa_pdf_url')} for p in papers if p.get('verified') is True][:120]
    inputs = {'question': question, 'candidates': candidates, 'phase': engine.store.get('runs', run_id)['stage'],
              'date_policy': context.get('literature_date_policy'), 'selected_idea_hash': context.get('selected_idea_hash'),
              'literature_reasoning_effort': context.get('literature_reasoning_effort')}
    key = f'{run_id}:literature-screen:{fingerprint(inputs)}'
    saved = engine.store.get('literature_inputs', key)
    if saved:
        if saved.get('inputs') != inputs or saved.get('result_sha256') != fingerprint(saved.get('result')):
            raise ValueError('Screening checkpoint is inconsistent')
        result = saved['result']
    else:
        result = await harness.ask(run_id, 'scout', 'literature_screen:' + fingerprint(inputs)[:20],
            INSTRUCTION, {**context, **inputs, 'codex_literature_protocol': 1}, SCHEMA)
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
