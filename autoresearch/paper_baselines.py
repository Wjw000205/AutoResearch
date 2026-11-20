"""Cited external scores are comparison evidence, never local training receipts."""

from .async_ownership import gather as gather_owned
import copy
import asyncio
import math

from .experiment_workflow import sha

COLLECTION = 'paper_baseline_references'
MODE = 'paper_reported'


def extract_table(text, table):
    """Extract metric cells of one cited method from a grouped, whitespace-delimited table."""
    lines = text.splitlines()
    def line(number):
        if type(number) is not int or not 1 <= number <= len(lines):
            raise ValueError('Paper table locator outside retained source')
        return lines[number - 1]
    methods = line(table['method_header']).split()[1:]
    metric_headers = line(table['metric_header']).split()[1:]
    metrics = table['metrics']
    if (len(methods) != len(set(methods)) or metric_headers != metrics * len(methods)
            or table['method'] not in methods or line(table['dataset_header']).strip() != table['dataset']
            or len(set(metrics)) != len(metrics) or not metrics
            or not table['caption'] < table['method_header'] < table['metric_header'] < table['dataset_header']):
        raise ValueError('Paper row, method or metric header does not match the cited table')
    offset = 1 + methods.index(table['method']) * len(metrics)
    rows = []
    for index, condition in enumerate(table['conditions'], table['dataset_header'] + 1):
        raw = line(index)
        cells = raw.split()
        if len(cells) != 1 + len(metric_headers) or cells[0] != str(condition):
            raise ValueError('Paper table row or number of metric columns changed')
        for i, metric in enumerate(metrics):
            token = cells[offset+i]
            value = float(token)
            if not math.isfinite(value):
                raise ValueError('Paper score must be finite')
            rows.append({'condition':condition, 'metric':metric, 'value':value, 'reported_text':token,
                         'line':index, 'column':offset+i+1, 'row_text':raw, 'provenance':MODE})
    return rows


def register(engine, rid, note_id, table, protocol, passages):
    from .experiment_protocols import _notes
    note = _notes(engine, rid)[note_id]
    if table['dataset'] not in note['datasets']:
        raise ValueError('Paper note belongs to another dataset')
    doc = engine.sources.document(rid, note['source_id'])
    if doc['text_sha256'] != note['text_sha256']:
        raise ValueError('Paper source differs from the bound reading note')
    lines = doc['text'].splitlines()
    quotes = []
    for label, start, end in passages:
        if not 1 <= start <= end <= len(lines) or end-start > 35:
            raise ValueError('Invalid cited passage')
        quotes.append({'field':label, 'first_line':start, 'last_line':end, 'text':'\n'.join(lines[start-1:end])})
    body = {'run_id':rid, 'note_id':note_id, 'paper_id':note['paper_id'], 'source_id':note['source_id'],
            'text_sha256':doc['text_sha256'], 'table':table, 'protocol':protocol, 'passages':quotes,
            'scores':extract_table(doc['text'], table), 'provenance':MODE,
            'measured_locally':False, 'alignment_approved':False}
    key = rid + ':paper-baseline:' + sha(body)
    row = {'id':key, 'run_id':rid, 'sha256':sha(body), 'body':body}
    actual = engine.store.mutate(COLLECTION, key, lambda previous: previous or row)
    if actual != row:
        raise ValueError('Immutable paper baseline changed')
    return {'id':key, 'sha256':row['sha256']}


def read(store, rid, ref):
    row = store.get(COLLECTION, ref['id']) or {}
    body = row.get('body', {})
    if (row.get('run_id') != rid or body.get('run_id') != rid or sha(body) != ref['sha256']
            or row.get('sha256') != ref['sha256'] or ref['id'] != rid + ':paper-baseline:' + ref['sha256']):
        raise ValueError('Paper baseline reference no longer matches the frozen source')
    return copy.deepcopy(body)


def assessment_results(store, run_id, evidence):
    """Show actual validation scores beside the registered paper cells, preserving their stated scope."""
    measurements = []
    for index, row in enumerate(evidence['records']):
        params = row.get('params', {})
        if row.get('status') == 'completed' and not row.get('superseded') and params.get('phase') == 'validation':
            measurements.append({'trial_id': row['id'], 'task': params.get('benchmark_task_id'), 'variant': params.get('variant'),
                                 'seed': params.get('seed'), 'metrics': row['metrics'], 'path': f'/records/{index}/metrics'})
    result = {'validation_measurements': measurements, 'published_cells': []}
    if evidence.get('paper_reference'):
        paper = read(store, run_id, evidence['paper_reference'])
        cells = paper['cells'] if paper.get('kind') == 'protocol_results' else paper['scores']
        groups = {}
        for row in cells:
            cell = {key: row[key] for key in ('paper_id', 'dataset', 'method', 'metric', 'reported_text', 'condition',
                                             'locator', 'protocol', 'line', 'column') if key in row}
            if 'table' in paper:
                cell.update(paper_id=paper['paper_id'], dataset=paper['table']['dataset'], method=paper['table']['method'],
                            protocol=paper['protocol'])
            group = groups.setdefault(sha(cell), {**cell, 'linked_tasks': []})
            if row.get('task_id') and row['task_id'] not in group['linked_tasks']:
                group['linked_tasks'].append(row['task_id'])
        result.update(published_cells=list(groups.values()), paper_reference=evidence['paper_reference'],
                      interpretation='linked_tasks 是来源关联。表中聚合数值只计一次；跨划分的数值差距仅供诊断。')
    return result


def bind_design(engine, rid, design, protocols):
    """Bind arbitrary task modalities to the result cards from their own source protocols."""
    from .experiment_protocols import _notes
    notes = _notes(engine,rid)
    comparisons = []
    for task in design.get('benchmark_tasks', []):
        source = protocols[task['protocol_id']]
        note = notes[source['note_id']]
        for item in source.get('reported_baselines', []):
            cell = note['results'][item['result_index']]
            header = note['excerpts'][item['header_excerpt_index']]
            protocol = {p['name']:(p['values'] if len(p['values'])>1 else p['values'][0])
                        if p['status']=='reported' else None for p in source['parameters']}
            from .paper_protocol_fields import COMMON
            for name in COMMON:
                protocol.setdefault(name,None)
            comparisons.append({'task_id':task['task_id'], 'paper_id':source['paper_id'], 'source_id':source['source_id'],
                'text_sha256':source['text_sha256'], 'protocol_sha256':source['protocol_sha256'],
                'dataset':source['dataset'],'method':item['method'],'metric':cell['metric'],'reported_text':str(cell['value']),
                'condition':cell['setting'],'locator':cell['locator'],'quote':cell['quote'],'header':header,'protocol':protocol})
        if not source.get('reported_baselines'):
            from .engine import ResearchBlocked
            raise ResearchBlocked('基线原表尚未绑定到 '+task['task_id']+' 的论文协议；用 literature.record_notes 和 literature.record_protocol 定向补齐对应结果与表头。')
    if not comparisons:
        raise ValueError('Paper-referenced experiments require source-bound benchmark tasks')
    body={'kind':'protocol_results','run_id':rid,'provenance':MODE,'measured_locally':False,'cells':comparisons}
    digest=sha(body)
    key=rid+':paper-baseline:'+digest
    row={'id':key,'run_id':rid,'sha256':digest,'body':body}
    if engine.store.mutate(COLLECTION,key,lambda old:old or row)!=row:
        raise ValueError('Paper result collection changed')
    return {'id':key,'sha256':digest}


def alignment(reference, measured_protocol):
    from .fair_comparison import strict_equal
    conditions = reference['protocol']
    unresolved = [key for key, value in conditions.items() if value is None or measured_protocol.get(key) is None]
    mismatched = [key for key, value in conditions.items() if key not in unresolved and not strict_equal(value, measured_protocol[key])]
    return {'comparable': bool(conditions) and not (unresolved or mismatched),
            'unresolved':unresolved, 'mismatched':mismatched, 'provenance':MODE}


def own_validation(summary, trials, seeds):
    """Select a local validation control without constructing a baseline score or a seed pair."""
    groups = [g for g in summary['groups'] if g['variant'] == 'proposed' and set(g['seeds']) == set(seeds)]
    if not groups or any(i.get('code') == 'comparison_task_mismatch' for i in summary['issues']):
        return {}
    best = min(groups, key=lambda g: ((1 if summary['direction']=='minimize' else -1)*g['mean'], g['configuration']))
    by_id = {t['id']:t for t in trials}
    observations = [{'seed':by_id[k]['params']['seed'], 'proposed':by_id[k]['metrics'][summary['metric']],
                     'proposed_trial_id':k} for k in best['trial_ids']]
    return {'best_proposed':best, 'proposed_score':best['mean'], 'own_control_eligible':True,
            'own_observations':observations, 'baseline_source':MODE}


def control_available(comparison):
    return comparison.get('comparable') or comparison.get('own_control_eligible')


def datasets(reference):
    if reference.get('kind') == 'protocol_results':
        return sorted({row['dataset'] for row in reference['cells']})
    return [reference['table']['dataset']]


def compare_results(plan, trials, reference):
    """Compare final local scores only under a matching evaluation protocol."""
    from .benchmark_matrix import dimensions
    tasks = {t['task_id']:t for t in plan['task_plans']}
    plan_hash = sha(plan)
    results = []
    grouped = reference.get('kind') != 'protocol_results'
    for cell in reference['scores'] if grouped else reference['cells']:
        meta = reference['table'] if grouped else cell
        expected = reference if grouped else cell
        potentials = []
        for t in trials:
            params = t.get('params', {})
            task = tasks.get(params.get('benchmark_task_id'))
            if (not task or t.get('status') != 'completed' or t.get('superseded') or t.get('evaluation_errors')
                    or t.get('experiment_plan_sha256') != plan_hash or params.get('seed') not in task['plan']['final_test_seeds']
                    or params.get('variant') != 'proposed' or params.get('phase') != 'test'
                    or params.get('dataset') != meta['dataset']):
                continue
            if ((grouped and dimensions(task).get(meta['condition_name']) != cell['condition'])
                    or (not grouped and task['task_id'] != cell['task_id'])):
                continue
            metric = 'test_' + cell['metric'].lower()
            score = t.get('metrics', {}).get(metric)
            if type(score) not in (int,float) or not math.isfinite(score):
                continue
            checked = alignment(expected, (t.get('evaluation') or {}).get('paper_protocol', {}))
            try:
                paper_score = float(cell['reported_text'])
            except ValueError:
                paper_score = None  # Preserve intervals and units verbatim, without a spurious numeric delta.
            potentials.append({'trial_id':t['id'], 'task_id':task['task_id'], 'seed':params['seed'],
                               'score':score, 'protocol_match':checked,
                               'difference':score-paper_score if checked['comparable'] and paper_score is not None and math.isfinite(paper_score) else None})
        results.append({**({'dataset':meta['dataset'],'method':meta['method'],'paper_id':reference['paper_id'],
                            'source_id':reference['source_id'],'text_sha256':reference['text_sha256'],
                            'table':meta['label']} if grouped else {}), **cell,'local_tests':potentials})
    return results


def outcome(plan, trials, reference):
    """Evaluate only aligned published results on the predeclared held-out metrics."""
    import statistics
    rows = compare_results(plan,trials,reference)
    criteria = plan['acceptance']
    checks = []
    for bound in criteria['criteria']:
        metric = bound['metric'].removeprefix('val_').lower()
        matches = [row for row in rows if row['metric'].lower() == metric]
        if not matches:
            checks.append({'metric':metric,'passed':None,'required':bound.get('required',True)})
        for row in matches:
            local = row['local_tests']
            ids = {t['task_id'] for t in local}
            expected = next((t['plan']['final_test_seeds'] for t in plan['task_plans'] if {t['task_id']}==ids), [])
            test_seeds = sorted(t['seed'] for t in local)
            passed = relative = None
            if expected and test_seeds==sorted(expected) and all(t['difference'] is not None for t in local):
                base = float(row['reported_text'])
                if base:
                    sign = -1 if bound['direction']=='minimize' else 1
                    relative = sign * statistics.mean(t['difference'] for t in local) / abs(base)
                    threshold = bound['minimum_relative_improvement']
                    passed = relative > threshold if bound.get('comparison')=='greater_than' else relative >= threshold
            checks.append({'dataset':row['dataset'],'condition':row['condition'],'method':row['method'],'metric':metric,
                           'required':bound.get('required',True),'passed':passed,'relative_improvement':relative})
    required = [c for c in checks if c['required']]
    complete = bool(required) and criteria.get('complete') and all(c['passed'] is not None for c in required)
    return {'research_outcome': ('positive' if all(c['passed'] for c in required) else 'negative') if complete else 'inconclusive',
            'phase':'test','criteria_complete':bool(complete),'checks':checks,'paper_comparisons':rows,
            'test_reuse_for_tuning_allowed':False,'scope':'Source-attributed external comparison; no paired-seed statistics.'}


def markdown(rows):
    lines = ['','## 论文报告的外部基线','',
             '| 数据集 | 方法 | 条件 | 指标 | 论文原值 | 论文与原文位置 | 自有测试减论文值 |',
             '|---|---|---|---|---:|---|---|']
    for row in rows:
        delta = ', '.join(f"{t['difference']:.6g}" for t in row['local_tests'] if t['difference'] is not None)
        locator = row['locator'] if 'locator' in row else f"{row['table']} L{row['line']} C{row['column']}"
        lines.append(f"| {row['dataset']} | {row['method']} | {row['condition']} | {row['metric']} | "
                     f"{row['reported_text']} | {row['paper_id']} {locator} | "
                     f"{delta or '待同协议测试'} |")
    return '\n'.join(lines)+'\n'


def guard(run, params):
    if run['config'].get('baseline_mode') == MODE and params.get('variant') == 'baseline':
        from .engine import ResearchBlocked
        raise ResearchBlocked('本任务的外部基线仅引用论文原表，不能派发本地基线训练或测试。')


async def review_adaptation(engine, harness, rid, packet):
    """Review the scope of the replacement design before any implementation."""
    from .engine import Harness, ResearchBlocked
    from . import dataset_planning as planning
    from .prose_schema import validate
    ref = packet['paper_reference']
    body = read(engine.store, rid, ref)
    if 'table' in body:
        from .paper_result_reading import ensure as read_original
        await read_original(engine,harness,rid,ref,body)
    scope = {'reference':ref, 'dataset':body['table']['dataset'] if 'table' in body else datasets(body), 'benchmark':body,
             'operator_instruction':packet['operator_instruction'],
             'previous_implementation':packet['existing_implementation'],
             'asset_bindings': [d['dataset_assets'] for d in packet['datasets']],
             'cycle':engine.cycle(rid)}
    key = rid + ':paper-adaptation:' + sha(scope)
    row = engine.store.get('paper_adaptation_reviews', key)
    if not row:
        ctx = planning._context(engine,rid,key,{'proposal':scope},'paper_model_adaptation')
        ctx['dataset_planning'].update(proposal=scope, read_paths=['/proposal'])
        row = {'id':key,'run_id':rid,'scope':scope,'context':ctx,'votes':{},'status':'reviewing'}
        engine.store.put('paper_adaptation_reviews',key,row)
        engine.store.event(rid,'dataset.paper_adaptation_review','已派发论文输入输出与自有模型适配范围的独立评审。',{'id':key})
    instruction = ('根据所附论文原表、行列和原文摘录，独立核验本次模型适配设计任务能否开始。'
        '只运行自有模型和任务批准的自有消融。逐项核对数据集、输入候选、输出、选优与汇总、基线名称和各条件的原值。'
        '在 rationale 中列出需要适配的形状、选择协议、CPU 检查和比较边界；论文未报告的条件写未知。'
        '将设计/实现可开展与最终结果可比性分别判断。此表决不批准训练、CPU 检查结果或跨论文性能主张。'
        '只把妨碍发出适配设计任务的具体问题列入 issues。')
    purpose = 'dataset_plan_review:paper-adaptation:' + sha(scope)
    if row['scope'] != scope or row['context']['dataset_planning']['proposal'] != scope:
        raise ValueError('Frozen paper adaptation scope changed')
    async def ask(role):
        result = await harness.ask(rid,role,purpose,instruction,row['context'],planning.VOTE)
        validate(result,planning.VOTE)
        engine.check_active(rid)
        owner = Harness.decision_key(rid,role,purpose,instruction,row['context'],planning.VOTE)
        row['votes'][role] = {'handoff_id':owner,'result':result}
        engine.store.put('paper_adaptation_reviews',key,row)
    roles = ('director','engineer','scout')
    outcomes = await gather_owned(*(ask(r) for r in roles if r not in row['votes']), return_exceptions=True)
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            raise outcome
    for role in roles:
        vote = row['votes'][role]
        owner = Harness.decision_key(rid,role,purpose,instruction,row['context'],planning.VOTE)
        decision = engine.store.get('decisions',owner) or {}
        if owner != vote['handoff_id'] or decision.get('run_id') != rid or decision.get('result') != vote['result']:
            raise ValueError('Paper adaptation requires each independent decision on this exact scope')
    objections = [v['result']['issues'] or [v['result']['rationale']] for v in row['votes'].values()
                  if not v['result']['approve'] or v['result']['issues']]
    row['status'] = 'changes_requested' if objections else 'scope_approved'
    engine.store.put('paper_adaptation_reviews',key,row)
    if objections:
        raise ResearchBlocked('论文协议适配设计需补证：' + '；'.join(i for group in objections for i in group))
    return {'id':key,'scope_sha256':sha(scope),'votes':row['votes'],'execution_authorized':False}
