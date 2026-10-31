import hashlib
import json

from ...storage import now

STRS = {'type': 'array', 'items': {'type': 'string', 'minLength': 1, 'maxLength': 160}, 'maxItems': 8}
SCHEMA = {'type': 'object', 'additionalProperties': False,
    'properties': {'concepts': STRS, 'mechanisms': STRS, 'outcomes': STRS,
        'queries': {'type': 'array', 'minItems': 1, 'maxItems': 3, 'items': {'type': 'object',
            'additionalProperties': False, 'properties': {'query': {'type': 'string', 'minLength': 1, 'maxLength': 400},
                'purpose': {'type': 'string', 'maxLength': 400}}, 'required': ['query', 'purpose']}},
        'limitations': STRS}, 'required': ['concepts', 'mechanisms', 'outcomes', 'queries', 'limitations']}
INSTRUCTION = ('制定学术API检索计划，不回答科研结论。保留研究领域与约束，提炼concepts、mechanisms、outcomes及术语同义表达。'
    '输出1–3条互补英文查询：直接证据、机制/方法同义词、反证或相近研究；不要把所有术语塞入一个AND查询。'
    '优先沿用initial_queries中的有效方向，计划只涉及检索和读文献；不得生成idea、长期记忆或证据图。')


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def compile_queries(expansion):
    """Keep planned directions, then fill unused slots with short term variants."""
    queries = [row['query'].strip() for row in expansion['queries']]
    concepts, mechanisms, outcomes = (expansion.get(k) or [] for k in ('concepts', 'mechanisms', 'outcomes'))
    combinations = [(concept, outcome) for concept in concepts for outcome in outcomes]
    combinations += [(concept, mechanism) for concept in concepts for mechanism in mechanisms]
    for first, second in combinations:
        value = ' '.join('"' + term.replace('"', '').strip() + '"' for term in (first, second))
        if len(queries) >= 3:
            break
        if len(value) <= 400 and value.casefold() not in {q.casefold() for q in queries}:
            queries.append(value)
    return queries


async def plan(engine, harness, run_id, question, queries, context):
    inputs = {'question': question, 'initial_queries': queries, 'phase': engine.store.get('runs', run_id)['stage'],
              'date_policy': context.get('literature_date_policy'), 'selected_idea_hash': context.get('selected_idea_hash'),
              'literature_reasoning_effort': context.get('literature_reasoning_effort')}
    key = f'{run_id}:literature-query-plan:{fingerprint(inputs)}'
    saved = engine.store.get('literature_inputs', key)
    if saved:
        if saved.get('inputs') != inputs or saved.get('result_sha256') != fingerprint(saved.get('result')):
            raise ValueError('Query planning checkpoint is inconsistent')
        return saved['result']
    result = await harness.ask(run_id, 'scout', 'literature_query_plan:' + fingerprint(inputs)[:20],
        INSTRUCTION, {**context, **inputs, 'codex_literature_protocol': 1}, SCHEMA)
    engine.check_active(run_id)
    engine.store.put('literature_inputs', key, {'id': key, 'run_id': run_id, 'kind': 'literature_query_plan',
        'inputs': inputs, 'result': result, 'result_sha256': fingerprint(result), 'completed_at': now()})
    return result
