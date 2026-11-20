"""Durable engineer revision and dual independent whole-report acceptance."""
from __future__ import annotations
import copy

from . import independent_full_review as full
from .storage import now

COLLECTION = 'scientific_report_reviews'


async def draft(engine,harness,run_id,instruction,context,schema):
    """Old completed Director drafts are historical input, never a new author route."""
    from .engine import Harness
    from .diagnostic_planning_recovery import authenticate
    hid=Harness.decision_key(run_id,'director','report',instruction,context,schema)
    saved=engine.store.get('decisions',hid)
    if saved:
        authenticate(engine,run_id,'director','report',{'instruction':instruction,'context':context,'schema':schema},saved['result'])
        return copy.deepcopy(saved['result'])
    from .scientific_author_materials import prepare,ask as ask_author
    legacy_hid=Harness.decision_key(run_id,'engineer','report:author-full-v1',instruction,context,schema)
    # A request frozen before index-first authoring retains its original packet.
    material=context if engine.store.get('handoff_inputs',legacy_hid) else prepare(engine,harness,run_id,context)
    request={'purpose':'report:author-full-v1','instruction':instruction,'context':material,'schema':schema}
    return (await ask_author(engine,harness,run_id,request))['answer']


async def review(engine, harness, run_id, context, draft, schema):
    from .engine import Harness, ResearchBlocked
    from .diagnostic_planning_recovery import authenticate
    source = {'context_sha256': full.sha(context), 'original_draft_sha256': full.sha(draft)}
    identifier = run_id + ':scientific-report:' + full.sha(source)
    record = engine.store.get(COLLECTION, identifier) or {'id': identifier, 'run_id': run_id,
        'source': source, 'rounds': [], 'created_at': now()}
    for index in range(4):
        engine.check_active(run_id)
        if len(record['rounds']) <= index:
            from .scientific_author_materials import prepare
            previous = record['rounds'][-1] if record['rounds'] else None
            author_context = {**copy.deepcopy(context), 'report_draft': copy.deepcopy(previous['author']['answer'] if previous else draft),
                'whole_report_feedback': copy.deepcopy(previous.get('votes', [])) if previous else []}
            record['rounds'].append({'author': {'purpose': 'report:engineer-full-v1:' + full.sha(source) + ':' + str(index),
                'instruction': '工程师统一整理或修订这份完整科研报告。保留真实测量、原判据、失败、未知与来源；'
                    '逐项处理全文审核意见，给出完整报告。不能发明结果、重跑实验或把局部检查当科学通过。',
                'context': prepare(engine,harness,run_id,author_context), 'schema': copy.deepcopy(schema)}})
            engine.store.put(COLLECTION, identifier, record)
        slot = record['rounds'][index]
        author = slot['author']
        if 'answer' not in author:
            from .scientific_author_materials import ask as ask_author
            slot['author'] = author = await ask_author(engine,harness,run_id,author)
            engine.check_active(run_id)
            engine.store.put(COLLECTION, identifier, record)
        authenticate(engine, run_id, 'engineer', author['purpose'], {k:author[k] for k in ('instruction','context','schema')}, author['answer'])
        from .scientific_author_materials import validate as validate_author
        validate_author(engine,run_id,author)
        report_sha = full.sha(author['answer'])
        if 'votes' not in slot:
            report_context = {**copy.deepcopy(context), 'complete_report': copy.deepcopy(author['answer']), 'report_sha256': report_sha}
            vote_schema = {'type':'object','properties': {'report_sha256':{'const':report_sha}, 'approve':{'type':'boolean'},
                'issues':{'type':'array','items':{'type':'string','minLength':1}}, 'rationale':{'type':'string','minLength':1}},
                'required':['report_sha256','approve','issues','rationale'],'additionalProperties':False}
            slot['votes'] = await full.group(harness, run_id, 'report_review:full-v1:' + report_sha,
                '各自独立审核完整报告与全部冻结证据：逐条核对数值、协议、来源、失败与未知、科学结论边界。'
                '必须指出具体位置及修改要求；两位对同一版本均无阻断问题才允许交付报告。不得分担章节或职责。',
                report_context, vote_schema)
            engine.check_active(run_id)
            engine.store.put(COLLECTION, identifier, record)
        full.authenticate_complete(engine.store, run_id, slot['votes'])
        if all(v['approve'] is True and not v['issues'] and v['report_sha256']==report_sha for v in slot['votes']):
            for vote in slot['votes']: full.validate_delivery(engine.store,run_id,vote,engine=engine)
            return copy.deepcopy(author['answer'])
        signature = full.sha(sorted(issue for v in slot['votes'] for issue in v['issues']))
        if index and signature == record['rounds'][index-1].get('issue_signature'):
            raise ResearchBlocked('科研报告全文复核的相同意见尚未解决；原作者交付及两位意见保留。')
        slot['issue_signature'] = signature
        engine.store.put(COLLECTION, identifier, record)
    raise ResearchBlocked('科研报告已保留多轮修改与独立全文复核，尚未取得同版本两位批准。')
