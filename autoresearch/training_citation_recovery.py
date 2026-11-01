"""One same-role confirmation of an omitted, already-delivered training receipt."""
from __future__ import annotations

import copy

from .experiment_workflow import sha

KEY = 'training_citation_repairs'


def audit(engine, run_id, role, repair, answer, required):
    from .diagnostic_planning_recovery import authenticate
    from .experiment_decisions import analysis_receipts, analysis_schema
    from .experiment_analysis import analyze
    from .training_read_continuation import retained_rows

    context = repair['context']
    if context.get('experiment_decision', {}).get('training_investigation', {}).get('version', 1) < 2:
        return None
    payload = {k: repair[k] for k in ('instruction', 'context')}
    payload['schema'] = analysis_schema(context)
    owner = authenticate(engine, run_id, role, repair['purpose'], payload, answer)
    original = analysis_receipts(engine, run_id, {'role': role, **answer}, context,
                                purpose=repair['purpose'], instruction=repair['instruction'])
    missing = set(required) - set(original['training_trials_read'])
    if not missing or not original['source_paths']:
        return None  # This recovery cannot manufacture source reads or unread pages.
    ref = context['validation_evidence']
    progress = engine.store.get('handoff_progress', owner) or {}
    inherited = retained_rows(engine, run_id, role, context)
    additions = {}
    for row in inherited + progress.get('tool_results', []):
        call, result = row.get('call') or {}, row.get('result') or {}
        args = call.get('arguments') or {}
        receipt = result.get('receipt_sha256')
        body = {k: v for k, v in result.items() if k != 'receipt_sha256'}
        if (call.get('name') != 'experiments.analyze' or args.get('kind') != 'training'
                or args.get('trial_id') not in missing or not result.get('text')
                or receipt in answer['read_receipts'] or result.get('error')
                or args.get('evidence_id') != ref['id'] or args.get('expected_sha256') != ref['sha256']
                or receipt != sha({'call': call, 'result': body})):
            continue
        if analyze(engine, run_id, context, args, persist=False) != body:
            raise ValueError('Omitted training receipt differs from its frozen report')
        additions[receipt] = copy.deepcopy(row)
    expected = sorted(set(answer['read_receipts']) | additions.keys())
    if (not additions or len(expected) > 16 or len(additions) > 4
            or sum(len(r['result']['text']) for r in additions.values()) > 24000):
        return None
    prospective = {**answer, 'read_receipts': expected}
    checked = analysis_receipts(engine, run_id, {'role': role, **prospective}, context,
                               purpose=repair['purpose'], instruction=repair['instruction'])
    if not set(required).issubset(checked['training_trials_read']) or not checked['source_paths']:
        return None
    return {'version': 1, 'source_handoff_id': owner, 'source_answer_sha256': sha(answer),
            'source_input_sha256': sha(engine.store.get('handoff_inputs', owner)),
            'evidence': copy.deepcopy(ref), 'required_training_trials': sorted(required),
            'original_cited_reads': copy.deepcopy(original), 'expected_receipts': expected,
            'omitted_receipts': sorted(additions), 'redisplayed_original_pages': list(additions.values()),
            'execution_authorized': False, 'scientific_approval': False,
            'scope': 'Actual delivered reads only; the author must confirm citations. No answer or reading credit is synthesized.'}


async def recover(engine, harness, run_id, role, repair, answer, required, record, checkpoint):
    from .diagnostic_planning_recovery import authenticate
    from .experiment_decisions import analysis_receipts, analysis_schema
    from .prose_schema import validate
    from .engine import ResearchBlocked

    proof = audit(engine, run_id, role, repair, answer, required)
    if proof is None:
        return None
    saved = copy.deepcopy(record.get(KEY, {}).get(role))
    if saved is not None and saved['audit'] != proof:
        raise ResearchBlocked('Training citation correction source changed')
    if saved is None:
        context = copy.deepcopy(repair['context'])
        context['experiment_decision'].pop('training_read_repair', None)
        context['experiment_decision']['training_citation_correction'] = {
            'original_answer': copy.deepcopy(answer), **proof}
        schema = copy.deepcopy(analysis_schema(context))
        schema['properties']['evidence_sha256'] = {'const': proof['evidence']['sha256']}
        schema['properties']['read_receipts'] = {'const': proof['expected_receipts']}
        saved = {'audit': proof, 'status': 'confirming', 'assignment': {
            'purpose': repair['purpose'] + ':citation-confirmation-1:' + sha(proof),
            'instruction': ('本次仅确认你上一份训练分析中漏列的实际读取回执。原答复、请求、笔记失败记录全部保留。'
                '宿主已逐页核验 source_handoff_id 的归属、原始内容与冻结SHA；omitted_receipts 对应报告此前已经完整交付，'
                'redisplayed_original_pages 是这些原始页面的重显，不是新增工具调用或训练。'
                '请依据原页纠正“未展开/未读”的描述，read_receipts 使用 expected_receipts；其他真实科研未知和门禁继续保留。'
                '本轮直接返回分析JSON，无需调用工具、重读已完成页面或重新讨论方案。'
                '读取通过不代表根因已查明、诊断代码已通过、参数稳定或执行获批。不得改写历史或重跑训练。'),
            'context': context, 'schema': schema}, 'execution_authorized': False}
    async def save():
        await checkpoint(**{KEY: {**record.get(KEY, {}), role: copy.deepcopy(saved)}})
    if role not in record.get(KEY, {}):
        await save()
    assignment = saved['assignment']
    if 'answer' not in saved:
        saved['answer'] = await harness.ask(run_id, role, **assignment)
        await save()  # Keep a delivered correction even if a later check fails.
    value = saved['answer']
    validate(value, assignment['schema'])
    new_owner = authenticate(engine, run_id, role, assignment['purpose'],
        {k: assignment[k] for k in ('instruction', 'context', 'schema')}, value)
    # Reads keep their original ownership; the new answer has its own paid owner.
    reads = analysis_receipts(engine, run_id, {'role': role, **value}, repair['context'],
                             purpose=repair['purpose'], instruction=repair['instruction'])
    if not set(required).issubset(reads['training_trials_read']) or not reads['source_paths']:
        raise ResearchBlocked('Training citation confirmation still lacks actual required reads')
    reads = {**reads, 'citation_confirmation_handoff_id': new_owner,
             'citation_confirmation_sha256': sha(value), 'new_reading_credit': False}
    if saved['status'] != 'complete':
        saved.update(status='complete', reads=reads)
        await save()
    elif saved.get('reads') != reads:
        raise ResearchBlocked('Training citation confirmation read binding changed')
    return {'role': role, **value}, reads
