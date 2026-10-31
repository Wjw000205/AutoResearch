"""One precise same-role page continuation after an incomplete read repair."""
import copy

from .experiment_workflow import sha

KEY = 'runtime_audit_read_repairs'


def audit(engine, run_id, role, repair, answer):
    from .diagnostic_planning_recovery import authenticate
    from .experiment_decisions import analysis_schema
    from .training_read_continuation import retained_rows, _valid_rows
    from .saved_runtime_audit import missing_pages
    context = repair['context']
    payload = {k: repair[k] for k in ('instruction', 'context')}
    payload['schema'] = analysis_schema(context)
    hid = authenticate(engine, run_id, role, repair['purpose'], payload, answer)
    rows = retained_rows(engine, run_id, role, context) + _valid_rows(engine, hid,
        context['evidence_sha256'], run_id=run_id, context=context)
    pages = missing_pages(engine, run_id, context, rows)
    if not pages:
        return None
    return {'version': 1, 'source_handoff_id': hid, 'source_answer_sha256': sha(answer),
        'source_input_sha256': sha(engine.store.get('handoff_inputs', hid)),
        'evidence': copy.deepcopy(context['validation_evidence']), 'missing_pages': pages,
        'retained_reads': [{'call': r['call'], 'receipt_sha256': r['result']['receipt_sha256'],
            **{k: r['result'][k] for k in ('offset', 'end', 'total_chars') if k in r['result']}} for r in rows],
        'execution_authorized': False, 'scientific_approval': False}


async def recover(engine, harness, run_id, role, repair, answer, record, checkpoint):
    from .diagnostic_planning_recovery import authenticate
    from .experiment_decisions import analysis_receipts, analysis_schema
    from .engine import ResearchBlocked
    proof = audit(engine, run_id, role, repair, answer)
    if proof is None:
        return None
    saved = copy.deepcopy(record.get(KEY, {}).get(role))
    if saved is not None and saved['audit'] != proof:
        raise ResearchBlocked('Runtime audit page continuation source changed')
    if saved is None:
        context = copy.deepcopy(repair['context'])
        context['experiment_decision']['runtime_audit_read_repair'] = {
            **proof, 'original_answer': copy.deepcopy(answer)}
        saved = {'audit': proof, 'status': 'reading', 'assignment': {
            'purpose': repair['purpose'] + ':runtime-audit-pages-1:' + sha(proof),
            'instruction': ('仅补齐本角色运行审计的缺页。missing_pages给出了精确path、offset、limit和end；'
                '对当前validation_evidence逐项调用experiments.compare，读取全部列出的区间。'
                '已有retained_reads由同角色原交接及冻结SHA核验，可引用并复用；不要重读完整报告或重做方案讨论。'
                '补读后返回更新后的分析JSON，引用真实回执；保留原训练根因未知及科学门禁。'
                '如果新页与原分析冲突，应如实修正。未读页不能声称已读，不授权训练、YAML修改或执行。'
                '这是一次有界补读；仍缺页时保存明确缺口，不自动反复调用。'),
            'context': context, 'schema': analysis_schema(context)}, 'execution_authorized': False}
    async def save():
        await checkpoint(**{KEY: {**record.get(KEY, {}), role: copy.deepcopy(saved)}})
    if role not in record.get(KEY, {}):
        await save()
    assignment = saved['assignment']
    if 'answer' not in saved:
        saved['answer'] = await harness.ask(run_id, role, **assignment)
        await save()
    value = saved['answer']
    hid = authenticate(engine, run_id, role, assignment['purpose'],
        {k: assignment[k] for k in ('instruction', 'context', 'schema')}, value)
    reads = analysis_receipts(engine, run_id, {'role': role, **value}, assignment['context'],
        purpose=assignment['purpose'], instruction=assignment['instruction'])
    reads.update(runtime_audit_page_handoff_id=hid, retained_source_handoff_id=proof['source_handoff_id'])
    if saved['status'] != 'complete':
        saved.update(status='complete', reads=reads)
        await save()
    elif saved.get('reads') != reads:
        raise ResearchBlocked('Runtime audit page continuation read evidence changed')
    return {'role': role, **value}, reads
