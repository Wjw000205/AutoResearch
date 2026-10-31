"""Versioned configuration review inputs; never rewrite dispatched requests."""
import copy

from .experiment_workflow import sha

GUIDANCE = '''
本轮审批对象是候选配置、适配代码和执行策略。按 approval_order 完成配置评审、资源准入、CPU 接口检查和训练；有限额度时同时核对预算与任务匹配，unlimited_attempts 时核对完整实验覆盖。proposed/not_frozen 及尚无运行回执是本阶段正常状态；不以未来审批/运行回执作为本轮审批的前提。
snapshot="candidate" 与 snapshot="current" 在本次配置复核中都指向 source_binding 所列的同一不可变候选源码；这不是已执行版本。每批最多读取三个 6000 字符窗口，随后必须用 record_notes 归纳再读取下一批。使用八个稳定笔记 key 合并观察并保留支撑回执。
experiment_review_evidence.source_reads 是本角色已经实际读取的源码回执目录，不代替内容核验；最终 read_receipts 应包含覆盖所有 changed_ranges 的回执。历史拒绝记录保留，本轮按更新后的宿主证据独立判断，不要求重写无缺陷代码。'''
GUIDANCE += '''
存在 prior_reading 时，只复用本角色实际读过且当前文件 SHA 完全不变的源码页及其笔记；它不是批准。required_read_ranges 列出尚需读取的区间，已有观察不足时仍可定向重读。对发生变化的文件重新核验，结合当前配置和接口证据自行给出结论。'''
GUIDANCE += '\nissues 只列阻止本次配置审批的具体缺陷；有阻断项则 approve=false，无阻断项则 approve=true 且 issues=[]。比较解释边界、尚无运行结果及后续门禁写入 rationale。'


def enabled(context):
    return context.get('dataset_configuration', {}).get('review_protocol') == 2


def prepare(engine, run_id, saved):
    if enabled(saved['review_context']):
        return
    from .code_bundle import validate
    from .experiment_analysis import freeze_sources
    from .experiment_decisions import _archive
    from .dataset_experiments import REVIEW, COLLECTION
    from .rapid_experiment_revision import VOTE
    candidate = saved['candidate']
    validate(candidate['bundle'], candidate['program']['code'])
    sources = freeze_sources(engine, run_id, candidate['program'], candidate['bundle'])
    if sources != candidate['code_sources']:
        raise ValueError('Candidate review source binding changed')
    previous = copy.deepcopy(saved['review_context'])
    ref = previous['validation_evidence']
    evidence = engine.store.get('experiment_evidence', ref['id'])['summary']
    if sha(evidence) != ref['sha256']:
        raise ValueError('Configuration review evidence changed')
    evidence = copy.deepcopy(evidence)
    binding = {'candidate_sha256': candidate['bundle']['sha256'], 'code_sources': sources,
        'amendment_sha256': candidate['amendment_sha256'], 'execution_claim': False}
    evidence.update(candidate_code_sources=sources, source_binding=binding,
        approval_order=['proposal_review', 'budget_freeze_and_resource_admission', 'cpu_check', 'paired_training'])
    unbounded = previous.get('dataset_configuration', {}).get('attempt_policy', {}).get('status') == 'unlimited'
    if unbounded:
        evidence['approval_order'] = ['proposal_review', 'resource_admission', 'cpu_check', 'paired_training']
    ref = _archive(engine, run_id, evidence)
    context = copy.deepcopy(previous)
    context.update(validation_evidence=ref, evidence_sha256=ref['sha256'])
    context['dataset_configuration'].update(review_protocol=2, source_binding=binding,
        budget_phase='unlimited_attempts' if unbounded else 'proposal_review_before_budget_freeze', approval_order=evidence['approval_order'])
    schema = copy.deepcopy(VOTE)
    # Source pagination can require more than the small architecture patch limit.
    schema['properties']['read_receipts']['maxItems'] = 128
    schema['allOf'] = [{'if':{'properties':{'approve':{'const':True}}},
        'then':{'properties':{'issues':{'maxItems':0}}}}]
    if saved.get('votes'):
        saved.setdefault('review_history', []).append({'context':previous,
            'instruction': saved.get('review_instruction', REVIEW),
            'schema': saved.get('review_schema', VOTE), 'votes':copy.deepcopy(saved['votes'])})
    saved.update(review_context=context, review_instruction=REVIEW + GUIDANCE,
        review_schema=schema, votes=[], status='reviewing')
    engine.store.put(COLLECTION, saved['id'], saved)


def read_state(context, rows):
    """Only exact receipts from this evidence; compact metadata is not reading."""
    ref = context.get('validation_evidence', {})
    notes = [r['result'] for r in rows if r.get('call',{}).get('name') == 'experiments.record_notes'
        and r.get('result',{}).get('saved')]
    noted = {s for r in notes for n in r['notes'] for s in n['receipt_sha256s']}
    noted.update(s for n in context.get('experiment_notes',{}).get('notes',[]) for s in n['receipt_sha256s'])
    config = context.get('dataset_configuration',{})
    if config.get('review_clarification'):
        noted.update(config.get('prior_vote',{}).get('read_receipts',[]))
    pending = []
    sources = {r['receipt_sha256']:r for r in context.get('experiment_review_evidence',{}).get('source_reads',[])
        if not context.get('dataset_configuration',{}).get('prior_reading') or r['receipt_sha256'] in noted}
    for index, row in enumerate(rows):
        call, result = row.get('call',{}), row.get('result',{})
        args = call.get('arguments',{})
        if (call.get('name') not in {'experiments.compare','experiments.inspect_module'}
                or args.get('evidence_id') != ref.get('id') or args.get('expected_sha256') != ref.get('sha256')
                or not result.get('text') or result.get('error')
                or result.get('receipt_sha256') != sha({'call':call,'result':{k:v for k,v in result.items() if k!='receipt_sha256'}})):
            continue
        receipt = result['receipt_sha256']
        if receipt not in noted:
            pending.append(index)
        if result.get('source_read'):
            sources[receipt] = {k:result[k] for k in ('receipt_sha256','path','source_sha256','offset','end')}
    return pending, list(sources.values())


def inherited_rows(engine, run_id, role, context, _depth=0):
    """Validate same-role original receipts against unchanged current source bytes."""
    prior = context.get('dataset_configuration',{}).get('prior_reading')
    if not prior:
        return []
    if _depth>=8:
        raise ValueError('Configuration review ancestry exceeds its bounded chain')
    key = prior['handoff_id']
    handoff = engine.store.get('handoffs',key) or {}
    old = (engine.store.get('handoff_inputs',key) or {}).get('context',{})
    if (handoff.get('run_id') != run_id or handoff.get('to_role') != role
            or not handoff.get('purpose','').startswith('dataset_config_review:')
            or old.get('validation_evidence') != prior['validation_evidence']
            or old.get('dataset_configuration',{}).get('candidate_sha256') != prior['candidate_sha256']):
        raise ValueError('Inherited configuration review ownership or version changed')
    sources = context['dataset_configuration']['source_binding']['code_sources']
    archive = engine.store.get('experiment_sources',sources['id']) or {}
    if archive.get('run_id') != run_id or sha(archive.get('files')) != sources['sha256']:
        raise ValueError('Inherited review target source changed')
    result = []
    ancestors = inherited_rows(engine,run_id,role,old,_depth+1)
    ancestor_refs = {r['result']['receipt_sha256'] for r in ancestors}
    for row in [*ancestors,*(engine.store.get('handoff_progress',key) or {}).get('tool_results',[])]:
        call, value = row.get('call',{}),row.get('result',{})
        args = call.get('arguments',{})
        current_file = archive['files'].get(value.get('path'),{})
        if (call.get('name') == 'experiments.inspect_module' and value.get('source_read')
                and (value.get('receipt_sha256') in ancestor_refs or (
                    args.get('evidence_id') == prior['validation_evidence']['id']
                    and args.get('expected_sha256') == prior['validation_evidence']['sha256']
                    and value.get('evidence_sha256') == prior['validation_evidence']['sha256']))
                and value.get('source_sha256') == current_file.get('sha256')
                and value.get('text') == current_file.get('text','')[value.get('offset',0):value.get('end',0)]
                and value.get('receipt_sha256') == sha({'call':call,'result':{k:v for k,v in value.items() if k!='receipt_sha256'}})):
            result.append(row)
    # A page committed just before interruption may never have been seen in a
    # subsequent model request. Reuse only pages consolidated into saved notes.
    progress = engine.store.get('handoff_progress',key) or {}
    notes = [r['result']['notes'] for r in progress.get('tool_results',[]) if r.get('call',{}).get('name')=='experiments.record_notes' and r.get('result',{}).get('saved')]
    valid = {r['result']['receipt_sha256'] for r in result}
    noted = {ref for note in (notes[-1] if notes else []) if set(note['receipt_sha256s'])<=valid for ref in note['receipt_sha256s']}
    if prior.get('final_decision_sha256'):
        decision = (engine.store.get('decisions',key) or {}).get('result')
        if not decision or sha(decision) != prior['final_decision_sha256']:
            raise ValueError('Prior reviewer final decision changed')
        noted.update(set(decision.get('read_receipts',[])) & valid)
    return [r for r in result if r['result']['receipt_sha256'] in noted]


def clarification(engine, run_id, saved, role, result):
    """The same reviewer resolves contradictory approval fields, never the host."""
    from .engine import Harness
    from .dataset_experiments import REVIEW
    from .rapid_experiment_revision import VOTE
    base = saved.get('review_contexts',{}).get(role,saved['review_context'])
    purpose = 'dataset_config_review:'+saved['candidate']['amendment_sha256']
    instruction, schema = saved.get('review_instruction',REVIEW),saved.get('review_schema',VOTE)
    owner = Harness.decision_key(run_id,role,purpose,instruction,base,schema)
    decision = (engine.store.get('decisions',owner) or {}).get('result')
    if decision != result:
        raise ValueError('Approval clarification requires the exact original reviewer decision')
    from .rapid_experiment_revision import review_receipts
    review_receipts(engine,run_id,saved,{'role':role,**result},base,purpose=purpose,instruction=instruction,schema=schema)
    context = copy.deepcopy(base)
    context['dataset_configuration'].update(review_clarification=True, prior_vote=copy.deepcopy(result),
        prior_reading={'handoff_id':owner,'validation_evidence':base['validation_evidence'],
            'candidate_sha256':saved['candidate']['bundle']['sha256'],'approval_inherited':False,
            'final_decision_sha256':sha(result)})
    rows = inherited_rows(engine,run_id,role,context)
    refs = {r['result']['receipt_sha256'] for r in rows}
    progress = engine.store.get('handoff_progress',owner) or {}
    notes = [r['result']['notes'] for r in progress.get('tool_results',[]) if r.get('call',{}).get('name')=='experiments.record_notes' and r.get('result',{}).get('saved')]
    context['experiment_notes'] = {'notes':[n for n in (notes[-1] if notes else []) if set(n['receipt_sha256s'])<=refs],
        'provenance':'same_reviewer_saved_observations'}
    context['experiment_review_evidence'] = {'source_reads':[{k:r['result'][k] for k in
        ('receipt_sha256','path','source_sha256','offset','end')} for r in rows], 'scientific_approval':False}
    schema = copy.deepcopy(schema)
    schema.setdefault('allOf',[]).append({'if':{'properties':{'approve':{'const':True}}},'then':{'properties':{'issues':{'maxItems':0}}}})
    return {'purpose':purpose+':approval-clarification', 'context':context,'schema':schema,
        'instruction':instruction+'\n你上一答复 approve=true 却把说明写入 issues，语义矛盾。请依据自己已核验的同一份源码和笔记最终裁定：issues 只列阻止本次配置审批的具体缺陷；有阻断项则 approve=false；没有则 approve=true、issues=[]。已知比较解释边界、未运行状态及后续执行门禁写入 rationale，不是本轮待修改代码。保持三个版本哈希与覆盖源码的真实 read_receipts，不因格式修正改变实验范围或授权。不要继承宿主审批，也无需重新读取已归纳且未变的源码。'}


def remaining_ranges(changes, indexes):
    missing = []
    for changed in changes:
        cursor = changed['start']
        for r in sorted((r for r in indexes if r['path']==changed['path'] and r['source_sha256']==changed['sha256']),key=lambda r:r['offset']):
            # Credit only the intersection with this changed range. Pages for
            # another edit in the same file must not create gaps outside it.
            lo, hi = max(changed['start'], r['offset']), min(changed['end'], r['end'])
            if hi <= lo or hi <= cursor:
                continue
            if lo > cursor:
                missing.append({**changed,'start':cursor,'end':lo})
            cursor=max(cursor,hi)
            if cursor >= changed['end']:
                break
        if cursor<changed['end']:
            missing.append({**changed,'start':cursor})
    return missing


def role_context(engine, run_id, saved, role):
    from .engine import Harness
    from .dataset_experiments import REVIEW
    from .rapid_experiment_revision import VOTE
    context = copy.deepcopy(saved['review_context'])
    if not enabled(context) or not saved.get('previous_setup'):
        return context
    ref = saved['previous_setup']
    archive = engine.store.get('experiment_evidence',ref['id']) or {}
    if archive.get('run_id') != run_id or sha(archive.get('summary')) != ref['sha256']:
        raise ValueError('Previous configuration review archive changed')
    previous = archive['summary'].get('setup',{})
    old = previous.get('review_contexts',{}).get(role,previous.get('review_context',{}))
    if not enabled(old):
        return context
    purpose = 'dataset_config_review:'+previous['candidate']['amendment_sha256']
    key = Harness.decision_key(run_id,role,purpose,previous.get('review_instruction',REVIEW),old,previous.get('review_schema',VOTE))
    if not engine.store.get('handoffs',key):
        return context
    context['dataset_configuration']['prior_reading'] = {'handoff_id':key,
        'validation_evidence':old['validation_evidence'],
        'candidate_sha256':previous['candidate']['bundle']['sha256'], 'approval_inherited':False}
    rows = inherited_rows(engine,run_id,role,context)
    if not rows:
        context['dataset_configuration'].pop('prior_reading')
        return context
    indexes = [{k:r['result'][k] for k in ('receipt_sha256','path','source_sha256','offset','end')} for r in rows]
    refs = {r['receipt_sha256'] for r in indexes}
    progress = engine.store.get('handoff_progress',key) or {}
    notes = [r['result']['notes'] for r in progress.get('tool_results',[]) if r.get('call',{}).get('name')=='experiments.record_notes' and r.get('result',{}).get('saved')]
    notes = [n for n in (notes[-1] if notes else []) if set(n['receipt_sha256s']) <= refs]
    context['experiment_notes'] = {'notes':notes,'provenance':'same_role_prior_observations_unchanged_source_only'}
    context['experiment_review_evidence'] = {'source_reads':indexes,'scientific_approval':False}
    context['dataset_configuration']['required_read_ranges'] = remaining_ranges(saved['candidate']['changed_ranges'],indexes)
    return context
