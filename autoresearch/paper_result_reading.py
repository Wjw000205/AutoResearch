"""Source-only Scout extraction, checked against native read receipts and the cited cells."""
import hashlib

from . import dataset_planning as planning
from .experiment_workflow import sha
from .prose_schema import validate

COLLECTION = 'paper_baseline_readbacks'
SCHEMA = planning.obj({'method':planning.TEXT, 'dataset':planning.TEXT,
    'cells':{'type':'array','minItems':1,'maxItems':128,'items':planning.obj({
        'condition':{'type':['string','number']},'metric':planning.TEXT,'reported_text':planning.TEXT})},
    'selection_rule':planning.TEXT, 'unknowns':planning.TEXTS})
INSTRUCTION = '''独立从原论文读出指定方法和数据集的结果。调用 sources.read 打开所给原文窗口，核对表注、方法和指标列、数据集名及逐条件结果行。
自行填写 cells；condition 使用行的条件值，数值条件填数值，reported_text 保留原始小数位。只填逐条件数据，均值和误差条不能代替分项。
写明论文的选优或汇总规则，未披露条件写入 unknowns。只依据这次实际看到的原表，不根据记忆猜数。'''


def verify_reads(source, task, results, progress, expected):
    target = task['context']['dataset_planning']['protocol_task']
    if results['dataset'] != target['dataset'] or results['method'] != target['method']:
        raise ValueError('Scout extracted a different dataset or method')
    expected_cells = [(r['condition'],r['metric'],r['reported_text']) for r in expected['scores']]
    actual_cells = [(r['condition'],r['metric'],r['reported_text']) for r in results['cells']]
    if sorted(expected_cells,key=repr) != sorted(actual_cells,key=repr):
        raise ValueError('Extracted cells differ from the cited original row/column values')
    spans = []
    for row in progress.get('tool_results',[]):
        call, result = row.get('call',{}),row.get('result',{})
        args = call.get('arguments',{})
        if (call.get('name') != 'sources.read' or args.get('source_id') != target['source_id']
                or args.get('expected_sha256') != target['text_sha256'] or result.get('text_sha256') != target['text_sha256']):
            continue
        text,offset=result.get('text'),result.get('offset')
        if (isinstance(text,str) and type(offset) is int and offset>=0 and text and source[offset:offset+len(text)]==text
                and hashlib.sha256(text.encode()).hexdigest()==result.get('page_sha256')):
            spans.append((offset,offset+len(text),result['page_sha256']))
    for window in target['source_windows']:
        cursor = window['offset']
        for start,end,_ in sorted(spans):
            if start <= cursor < end:
                cursor=end
        if cursor < window['offset'] + window['limit']:
            raise ValueError('Scout decision lacks its own source read for part of the table header or result rows')
    return [{'offset':a,'end':b,'page_sha256':h} for a,b,h in spans]


async def ensure(engine,harness,rid,ref,body):
    from .engine import Harness, ResearchBlocked
    doc=engine.sources.document(rid,body['source_id'])
    if doc['text_sha256'] != body['text_sha256']:
        raise ValueError('Paper changed before Scout extraction')
    lines=doc['text'].splitlines(keepends=True)
    table=body['table']
    windows=[]
    for start,end in ((table['caption'],table['metric_header']),
                      (table['dataset_header'],table['dataset_header']+len(table['conditions']))):
        windows.append({'offset':sum(map(len,lines[:start-1])), 'limit':len(''.join(lines[start-1:end]).rstrip('\r\n'))})
    target={k:body[k] for k in ('paper_id','source_id','text_sha256')}
    target.update(dataset=table['dataset'],method=table['method'],table=table['label'],source_windows=windows)
    key=rid+':paper-readback:'+sha(target)
    row=engine.store.get(COLLECTION,key)
    if not row:
        ctx=planning._context(engine,rid,key,{'reading_assignment':target},'protocol_evidence')
        ctx['dataset_planning'].update(protocol_task=target,read_paths=['/reading_assignment'])
        task={'purpose':'dataset_select:paper-table:'+sha(target),'context':ctx,'instruction':INSTRUCTION,'schema':SCHEMA}
        row={'id':key,'run_id':rid,'reference':ref,'task':task,'status':'reading'}
        engine.store.put(COLLECTION,key,row)
    if row['reference'] != ref or row['task']['context']['dataset_planning']['protocol_task'] != target:
        raise ValueError('Published table readback is bound to another source or reference')
    for attempt in range(2):
        task=row['correction'] if attempt else row['task']
        owner=Harness.decision_key(rid,'scout',**task)
        old=engine.store.get('decisions',owner) or {}
        result=old['result'] if old else await harness.ask(rid,'scout',**task)
        validate(result,SCHEMA)
        accepted=engine.store.get('decisions',owner) or {}
        if accepted.get('result')!=result or accepted.get('run_id')!=rid:
            raise ValueError('Scout answer is detached from the saved handoff')
        try:
            receipts=verify_reads(doc['text'],task,result,engine.store.get('handoff_progress',owner) or {},body)
        except ValueError as exc:
            if attempt:
                raise ResearchBlocked('论文结果定向复读仍未核实：'+str(exc)) from exc
            correction={**task,'purpose':task['purpose']+':correct:'+sha(result),
                        'instruction':INSTRUCTION+'\n上次核验问题：'+str(exc)+'。请在本次交接重新读取所给两个原文窗口并逐格核对。'}
            row.update(correction=correction,status='rereading')
            engine.store.put(COLLECTION,key,row)
            continue
        was_verified=row.get('status')=='verified'
        row.update(status='verified',scout_handoff_id=owner,result=result,source_reads=receipts)
        engine.store.put(COLLECTION,key,row)
        if not was_verified:
            engine.store.event(rid,'literature.paper_baseline_read','Scout 已独立读取原表并核对全部所引结果。',
                               {'id':key,'source_reads':receipts,'cells':len(result['cells'])})
        return row
