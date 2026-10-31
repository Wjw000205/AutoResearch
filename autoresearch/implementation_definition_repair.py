"""Post-approval definition completion, with immutable ancestry and scoped review.

The original SPEC and its approval stay untouched. Only an appended engineering
definition is editable; three authenticated readers must approve that addition
against the original scientific sources before a new code instruction exists.
"""
from __future__ import annotations

import asyncio
import copy

from . import code_repair_contract as repair
from .storage import now

FIELD = 'implementation_definition_repair'
PROTOCOL = 'implementation_definition_repair_protocol'
MAX_ADDITION = 6000
AUTHORIZATIONS = 'implementation_timing_authorizations'

# This is a host/operator control, deliberately not an agent tool. The human
# may authorize a scoped exception without approving the resulting definition.
def authorize_timing(engine, run_id, *, workflow_id, proposal, user_messages, wall_seconds=None):
    import math
    run = engine.store.get('runs', run_id)
    state = engine.store.get('codex_workflows', workflow_id)
    if (not run or run['status'] not in {'paused', 'blocked'} or run['stage'] != 'implementation'
            or not state or state['run_id'] != run_id):
        _fail('计时授权只能写入已停止的同一实现任务。')
    record = state[FIELD]
    base = base_row(state, record)
    if (proposal.get('run_id') != run_id or proposal.get('original_document_sha256') != base['document']['sha256']
            or not user_messages or not all(isinstance(m,str) and m.strip() for m in user_messages)):
        _fail('计时授权缺少原文档绑定或直接用户指令。')
    if wall_seconds is not None and (type(wall_seconds) not in (int,float)
            or not math.isfinite(wall_seconds) or wall_seconds <= 0):
        _fail('计时上限必须为正数，或明确为 null（不限时）。')
    value = {'version':1, 'run_id':run_id, 'workflow_id':workflow_id,
        'original_document_sha256':base['document']['sha256'], 'proposal_sha256':repair.sha(proposal),
        'user_messages':list(user_messages), 'source':'direct_user_authorization',
        'operation_families':copy.deepcopy(proposal['proposed_change']['operation_families']),
        'additional_wall_seconds':wall_seconds, 'maximum_executions':1,
        'training_authorized':False, 'repeat_original_probe_authorized':False,
        'reset_original_allowance':False, 'scientific_approval':False,
        'scope':'仅新增一次非训练计时；null 表示不设本次计时的时间上限。保留原算法、原额度和回执、已批准的实验预算及硬件限制。',
        'ordering':['author_save','independent_definition_review','codex_implementation','code_review','host_timing']}
    key = run_id + ':' + repair.sha(value)
    if record.get('timing_authorization_id') == key:
        timing_authorization(engine, state)
        return engine.store.get(AUTHORIZATIONS,key)
    _source(engine,run_id,state,record)
    previous = state['specifications'][-1]
    if record['status'] != 'authoring' or previous.get('approved') is not False:
        _fail('授权续接必须从已完成审核的退回版本开始，不能覆盖正在审核或批准的版本。')
    # Preserve all old requests, drafts and reviews, even an exhausted author.
    # A distinct repair ID gives the new authorization a fresh immutable packet.
    entry = {**value, 'id':key, 'created_at':now()}
    entry['sha256'] = repair.sha(entry)
    existing = engine.store.get(AUTHORIZATIONS,key)
    if existing:
        if {k:v for k,v in existing.items() if k not in {'created_at','sha256'}} != {k:v for k,v in entry.items() if k not in {'created_at','sha256'}}:
            _fail('已有授权记录不一致。')
        entry = existing
    engine.store.put(AUTHORIZATIONS,key,entry)
    state.setdefault('definition_repair_history',[]).append(copy.deepcopy(record))
    next_record = {k:copy.deepcopy(record[k]) for k in
        ('base_index','base_row_sha256','source_sha256','attempt_index','request_ids','source_kind') if k in record}
    next_record.update(previous_repair_id=record['id'], predecessor_index=previous['index'],
        timing_authorization_id=key, timing_authorization_sha256=entry['sha256'])
    next_record = {'id':repair.sha(next_record), **next_record, 'status':'authoring',
        'revision_indices':[], 'created_at':now()}
    state[FIELD] = next_record
    state.update(status='specification',current_revision=previous['index']+1,blocked_kind=None,reason=None)
    engine.store.put('codex_workflows',workflow_id,state)
    engine.store.event(run_id,'implementation.timing_scope_authorized',
        '用户已授权额外一次非训练计时；新条款仍须原作者保存并独立审核，既有进度与额度保留。',
        {'workflow_id':workflow_id,'authorization_id':key,'wall_seconds':wall_seconds,'repair_id':next_record['id']})
    return entry


def timing_authorization(engine, state):
    record = state[FIELD]
    key = record.get('timing_authorization_id')
    if not key: return None
    value = engine.store.get(AUTHORIZATIONS,key)
    if (not value or value.get('sha256') != record.get('timing_authorization_sha256')
            or value['sha256'] != repair.sha({k:v for k,v in value.items() if k != 'sha256'})
            or value.get('run_id') != state['run_id'] or value.get('workflow_id') != state['id']
            or value.get('original_document_sha256') != base_row(state,record)['document']['sha256']):
        _fail('新增计时授权的身份或范围发生变化。')
    return value


AUTHORIZED_TIMING = '''
本轮新增的 timing_authorization 是宿主记录的直接用户授权，优先于旧探针范围限制：
允许对列出的缺项额外执行一次非训练计时；additional_wall_seconds=null 表示此次计时没有时间上限，
新增计时遵守该授权的时间范围；原历史额度和实验预算保持批准值。
不得重复原已执行探针或进行模型拟合、优化器更新及训练，不重跑已完成阶段。
补充段必须明确该授权身份、覆盖的操作、输入形状、次数、计时边界、外推公式、结果文件/入口及故障处理。
纯算子调用可以抽取既有实现，不能通过运行完整训练或改变科学算法取得耗时。
先审核定义的可实施性与取证方法，审核通过后才交 Codex 实现，再由宿主取得实际计时。
定义审核不能要求尚未允许实现/执行的新计时先产生数值；但缺操作/形状/次数/换算或非法取证方法仍应退回。
定义通过仅关闭“缺定义”，不关闭代码缺陷或缺执行证据；实际计时、完整覆盖和资源门禁结果仍须后续真实验收。
已有评审中“原许可不能新增计时”的依据已被本次明确范围授权更新；其他问题继续逐项处理。
'''

AUTHOR_TASK = '''你是实现文档作者。本轮补齐 definition_repair 中原代码审查指出的必要工程定义。
已有批准文档保持原样；只在 algorithm 末尾增加一个独立的“资源估算定义补充”小节，正文不设字符上限。
后续修订只修改该补充段。不得改动原算法、参数、验收ID、阈值、预算、允许的探针数量或已完成阶段。
这不是复述缺口或添加 limitations：须给出能交给 Codex 的明确工程步骤、输入输出、计算依据、
覆盖项、代码位置及如何依原验收核对；需要耗时上界时说明有效依据和适用条件，不能将工作量当耗时。
新定义只能是已批准方案允许的实现细化；不可自创科学方法、增加训练/探针、把缺少证据写成通过，
也不可因停止门禁存在就宣称资源估算已经完整。真正与批准来源矛盾的定义不得提交为解决方案。
按 issue.references 定向读取原文，文档与代码均不用全文加入上下文；代码事实已由原评审冻结。
使用 implementation.locate_document 定位 algorithm 末尾，再用 implementation.edit_document 做最小文本修改。
保存后必须按 definition_addition 的精确版本/范围调用 documents.read，完整读取新增段核对，
未完成读取不能提交；最终只返回工具实际给出的 submission。保存后自动进入三方独立审核，
未通过的具体意见会交回你修订；审核通过前不派发 Codex、不执行训练。'''

REVIEW_TASK = '''独立审核同一新实现文档的资源估算定义补充。宿主已逐字验证原批准 SPEC 未变化，
不要重审未改部分或追加需求。读取 definition_repair.original_references 指向的批准约束及
definition_addition 指向的新段，检查原缺口是否闭合、步骤是否可执行、所有必要耗时是否有有效依据，
是否遵守原探针/预算/失败停止规则及科学方案。计数不是耗时，没有依据的常数不能当保守上界。
不要求本轮训练结果，不以新测试取代原验收；已有同源宿主回执保留。不能提出文档未定义的代码指令。
如不通过，issues/fixes 必须仅针对补充段给出具体问题、改法和验证；交回原作者继续修订。
批准时 issues/fixes 为空。每个原 issue_id 给出 resolution；证据必须为本交接实际读取的当前补充段
和批准科学来源。approve=true 仅表示该定义可以交给 Codex 实现，不代表代码/资源/科学验收通过。'''


def _fail(message):
    repair.fail('实现文档定向补定义：' + message)


def base_row(state, record):
    rows = [r for r in state['specifications'] if r['index'] == record['base_index']]
    if len(rows) != 1 or repair.sha(rows[0]) != record['base_row_sha256'] or not rows[0].get('approved'):
        _fail('原批准文档或审批记录发生变化。')
    return rows[0]


def validate_candidate(spec, state, binding):
    """Enforce preservation before a real author save, not just in a prompt."""
    from .implementation_authoring_preservation import SaveConflict
    record = state.get(FIELD)
    if not record or binding.get('id') != record['id']:
        raise SaveConflict('definition_scope_changed', '补定义请求与当前调度不一致。')
    base = base_row(state, record)['spec']
    if ({k:v for k,v in spec.items() if k != 'algorithm'} != {k:v for k,v in base.items() if k != 'algorithm'}
            or not isinstance(spec.get('algorithm'), str)
            or not spec['algorithm'].startswith(base['algorithm'])
            or len(spec['algorithm']) <= len(base['algorithm'])
            or not spec['algorithm'][len(base['algorithm']):].strip()):
        raise SaveConflict('definition_scope_changed',
            '只允许在原 algorithm 末尾补齐必要资源定义；原文及其他字段必须逐字保留。')


def _source(engine, run_id, state, record):
    from .code_repair_clarification import verify_saved
    base_row(state, record)
    timing_authorization(engine, state)
    attempt = next(a for a in state['attempts'] if a['index'] == record['attempt_index'])
    source = (verify_saved(engine, run_id, state, attempt, attempt['verification_followup'])
        if record.get('source_kind') == 'verification_followup' else
        repair.build_source(engine, run_id, state, base_row(state,record), attempt))
    if repair.sha(source) != record['source_sha256']:
        _fail('原始缺口证据发生变化。')
    for key in record['request_ids']:
        request = engine.store.get('implementation_definition_requests', key)
        if (not request or request['workflow_id'] != state['id']
                or request['source_sha256'] != record['source_sha256']):
            _fail('缺少同一交付的真实定义请求。')
    return source


def begin(engine, run_id, state, specification, attempt, source, requests, save):
    if not requests:
        _fail('没有待补定义。')
    value = {'base_index':specification['index'], 'base_row_sha256':repair.sha(specification),
        'source_sha256':repair.sha(source), 'attempt_index':attempt['index'], 'request_ids':requests,
        'source_kind':'verification_followup' if attempt.get('verification_followup',{}).get('status')=='completed' else 'review_votes'}
    key = repair.sha(value)
    previous = state.get(FIELD)
    if previous:
        if previous['id'] == key:
            return
        if previous['status'] != 'approved':
            _fail('已有另一条文档补定义链，不能覆盖。')
        state.setdefault('definition_repair_history',[]).append(copy.deepcopy(previous))
    state[FIELD] = {'id':key, **value, 'status':'authoring', 'revision_indices':[], 'created_at':now()}
    attempt['execution_progress'] = {'step':'definition_authoring',
        'label':'实现文档作者补齐原缺口，独立审核后交回 Codex；保留已完成测试', 'updated_at':now(), 'started_at':now()}
    save(status='specification', blocked_kind=None, reason=None)
    engine.store.event(run_id, 'implementation.definition_authoring',
        '原代码审查的定义缺口已派回实现文档作者；通过审核后自动继续代码修订。', {'workflow_id':state['id'], 'repair_id':key})


def binding(state):
    record = state[FIELD]
    return {k:copy.deepcopy(record[k]) for k in ('id','base_index','base_row_sha256','source_sha256','request_ids')}


def author_capacity(context, state):
    """Prepare a new packet; never rewrite a frozen predecessor's schema."""
    result = copy.deepcopy(context)
    length = len(base_row(state, state[FIELD])['spec']['algorithm'])
    schema = result['implementation_document_authoring']['specification_schema']
    schema['properties']['algorithm']['maxLength'] = length + MAX_ADDITION
    return result


def validate_previous(engine, run_id, state, context, revision):
    """Authenticate the approved base or the last scoped rejection for a save."""
    from . import implementation_documents as documents
    record = state.get(FIELD, {})
    if (context.get(PROTOCOL) != 1 or context.get('definition_repair') != issue_context(engine, state)
            or record.get('status') != 'authoring'):
        _fail('保存续接没有绑定当前补定义请求。')
    _source(engine, run_id, state, record)
    base = base_row(state, record)
    documents.validate_review(engine, run_id, state, base)
    indices = record['revision_indices']
    index = indices[-1] if indices else record.get('predecessor_index',base['index'])
    rows = [r for r in state['specifications'] if r['index'] == index]
    if len(rows) != 1 or revision != index + 1:
        _fail('保存续接不是当前补定义版本。')
    previous = rows[0]
    if indices or record.get('previous_repair_id'):
        scope = state
        if not indices:
            ancestors = [r for r in state.get('definition_repair_history',[]) if r['id']==record['previous_repair_id']]
            if len(ancestors)!=1 or index not in ancestors[0]['revision_indices']:
                _fail('新授权续接缺少原版本审核链。')
            scope = {**state,FIELD:ancestors[0]}
        packets = previous.get('definition_reviews', [])
        if (previous.get('approved') is not False or previous.get('definition_repair_id') != scope[FIELD]['id']
                or len(packets) != 3 or {p['role'] for p in packets} != set(repair.ROLES)):
            _fail('补定义修订缺少同版本三方真实审核。')
        for packet in packets:
            validate_vote(engine, run_id, scope, previous, packet)
        if all(p['result']['approve'] for p in packets):
            _fail('已通过的补充定义不能作为否决修订。')
    return previous


def issue_context(engine, state):
    record = state[FIELD]
    requests = [engine.store.get('implementation_definition_requests', key) for key in record['request_ids']]
    result = {**binding(state), 'issues':[{'issue_id':r['issue']['issue_id'], 'issue':r['issue']['issue'],
        'requirement_id':r['issue']['binding']['requirement_id'], 'references':r['references']} for r in requests],
        'original_references':[ref for r in requests for e in r['references'] for ref in e.get('implementation_refs',[])],
        'scope':'只补 algorithm 末尾的资源估算定义；已批准正文、科学方案和验收要求保持原样。',
        'training_authorized':False}
    authorization = timing_authorization(engine,state)
    if authorization:
        result['timing_authorization'] = copy.deepcopy(authorization)
    return result


def addition_ref(engine, run_id, state, row):
    from . import document_index
    base = base_row(state, state[FIELD])
    # Inter-section whitespace may belong to the preceding indexed section.
    # Bind all added content, without requiring a read across that separator.
    suffix = row['spec']['algorithm'][len(base['spec']['algorithm']):].strip()
    doc, text = document_index.load(engine, run_id, row['document']['document_id'], row['document']['sha256'])
    # Readable rendering preserves strings. Refuse ambiguous anchors.
    if text.count(suffix) != 1:
        _fail('补充段无法在保存的 Markdown 中唯一定位。')
    start = text.index(suffix)
    for section in row['document']['sections']:
        if section['start'] <= start and start + len(suffix) <= section['end']:
            return {'document_id':row['document']['document_id'], 'document_sha256':row['document']['sha256'],
                'section_id':section['id'], 'offset':start-section['start'], 'limit':len(suffix)}
    _fail('补充段跨越多个索引章节。')


def review_schema(authorized=False):
    from .implementation_alignment import SOURCE_REFS
    fields = {'approve':{'type':'boolean'}, 'proposal_hash':repair.TEXT, 'spec_hash':repair.TEXT,
        'document_sha256':repair.TEXT, 'rationale':repair.TEXT,
        'issues':{'type':'array','items':repair.TEXT,'maxItems':12},
        'fixes':{'type':'array','maxItems':12,'items':repair.obj({'issue_index':{'type':'integer','minimum':0},
            'change':repair.TEXT,'verification':repair.TEXT})},
        'resolutions':{'type':'array','items':repair.obj({'issue_id':repair.TEXT,
            'resolved':{'type':'boolean'},'reason':repair.TEXT})},
        'definition_refs':copy.deepcopy(SOURCE_REFS),'source_refs':copy.deepcopy(SOURCE_REFS)}
    if authorized:
        fields['timing_authorization_sha256'] = repair.TEXT
    return repair.obj(fields)


def validate_vote(engine, run_id, state, row, packet):
    from .implementation_acceptance_flow import check_packet, validate_refs
    context = check_packet(engine, run_id, packet)
    result = packet['result']
    authorization = timing_authorization(engine,state)
    if authorization and result.get('timing_authorization_sha256') != authorization['sha256']:
        _fail('审核没有确认本轮用户计时授权，不能复用旧范围结论。')
    if (context.get(PROTOCOL) != 1 or context.get('definition_repair') != issue_context(engine, state)
            or context.get('definition_addition') != addition_ref(engine, run_id, state, row)
            or result['spec_hash'] != row['spec_hash'] or result['proposal_hash'] != state['proposal_hash']
            or result['document_sha256'] != row['document']['sha256']):
        _fail('审核没有绑定当前定义和原问题。')
    ids = {i['issue_id'] for i in context['definition_repair']['issues']}
    if (len(result['resolutions']) != len(ids) or {i['issue_id'] for i in result['resolutions']} != ids
            or {f['issue_index'] for f in result['fixes']} != set(range(len(result['issues'])))
            or result['approve'] and (result['issues'] or not all(i['resolved'] for i in result['resolutions']))
            or not result['approve'] and not result['issues']):
        _fail('审核必须逐项闭合原缺口或给出可修订的具体意见。')
    validate_refs(engine, run_id, packet['handoff_id'], context, result['definition_refs'], current=row['document'])
    # The *whole addition* is new; its review cannot be based on reading only its title.
    validate_refs(engine, run_id, packet['handoff_id'], context, [context['definition_addition']], current=row['document'])
    scientific = context['architecture_document']
    validate_refs(engine, run_id, packet['handoff_id'], context, result['source_refs'], current=scientific)
    return result


def validate_author_read(engine,run_id,state,row):
    from .implementation_acceptance_flow import validate_refs
    hid=row['authoring_handoff_id']
    context=copy.deepcopy(engine.store.get('handoff_inputs',hid)['context'])
    context['indexed_documents']=[*context['indexed_documents'],row['document']]
    validate_refs(engine,run_id,hid,context,[addition_ref(engine,run_id,state,row)],current=row['document'])


async def advance(engine, h, run_id, state, common, save):
    """One durable author/review round; rejection feeds back without stopping the run."""
    from . import implementation_documents as documents, implementation_authoring as author
    from . import implementation_alignment as alignment
    from .codex_workflow import SPEC, _frozen_spec_author
    from .implementation_authoring_completion import ask as ask_author
    from .implementation_failures import ImplementationSpecificationPending
    from .implementation_acceptance_flow import authenticate
    record = state.get(FIELD)
    if not record or record['status'] == 'approved':
        return
    await engine._read_consensus_check(run_id, _source, engine, run_id, state, record)
    base = base_row(state, record)
    indices = record['revision_indices']
    row = next((r for r in state['specifications'] if indices and r['index'] == indices[-1]), None)
    if not row or row.get('approved') is False:
        previous = state['specifications'][-1]
        index = previous['index'] + 1
        purpose = f'implementation_spec:definition-{record["id"]}:{index}'
        frozen = _frozen_spec_author(engine, run_id, purpose)
        if frozen:
            task, context = frozen['instruction'], frozen['context']
        else:
            context = alignment.attach(copy.deepcopy(common))
            context.update({PROTOCOL:1, 'definition_repair':issue_context(engine, state),
                'implementation_indexed_review_protocol':1, 'implementation_document_authoring_protocol':1,
                'implementation_document_edit_protocol':1,'implementation_document_locate_protocol':1,
                'implementation_document_readability_protocol':base.get('implementation_document_readability_protocol',1),
                'implementation_document_authoring':{'workflow_id':state['id'],'revision':index,
                    'specification_schema':alignment.author_schema(SPEC)},
                'previous_specifications':[{k:copy.deepcopy(previous[k]) for k in ('index','spec_hash','document')}],
                'definition_review_feedback':[{k:copy.deepcopy(p['result'][k]) for k in ('issues','fixes','resolutions')}
                    for p in (row or previous).get('definition_reviews',[])],
                'objective':engine.store.get('runs',run_id)['objective']})
            context = author_capacity(context, state)
            if timing_authorization(engine,state):
                from .codex_workflow import _snapshot_artifacts
                attempt = next(a for a in state['attempts'] if a['index']==record['attempt_index'])
                context['definition_code_sources'] = await engine._read_consensus_check(
                    run_id,_snapshot_artifacts,engine,run_id,attempt['snapshot'])
            context['indexed_documents'] += [previous['document']]
            if previous['document'] != base['document']:
                context['indexed_documents'].append(base['document'])
            task = AUTHOR_TASK + (AUTHORIZED_TIMING if timing_authorization(engine,state) else '')
        record['status'] = 'authoring'
        save(status='specification',current_revision=index,blocked_kind=None,reason=None)
        result, hid = await ask_author(engine,h,run_id,state,purpose,task,context)
        row = await author.submit_async(engine,run_id,state,hid,result)
        validate_candidate(row['spec'],state,binding(state))
        await engine._read_consensus_check(run_id,validate_author_read,engine,run_id,state,row)
        row.update({PROTOCOL:1,'definition_repair_id':record['id'],'implementation_indexed_review_protocol':1,
                    'definition_base_algorithm_length':len(base['spec']['algorithm'])})
        state['specifications'].append(row)
        indices.append(index)
        save()
    record['status'] = 'reviewing'
    save(status='spec_review',current_revision=row['index'],blocked_kind=None,reason=None)
    context = {**copy.deepcopy(common),PROTOCOL:1,'definition_repair':issue_context(engine,state),
        'definition_addition':addition_ref(engine,run_id,state,row),'spec_hash':row['spec_hash'],
        'document_sha256':row['document']['sha256'],
        'indexed_documents':[*copy.deepcopy(common['indexed_documents']),base['document'],row['document']]}
    context['indexed_documents'] = list({d['document_id']:d for d in context['indexed_documents']}.values())
    if timing_authorization(engine,state):
        from .codex_workflow import _snapshot_artifacts
        attempt = next(a for a in state['attempts'] if a['index']==record['attempt_index'])
        context['definition_code_sources'] = await engine._read_consensus_check(
            run_id,_snapshot_artifacts,engine,run_id,attempt['snapshot'])
    purpose = f'implementation_definition_review:{record["id"]}:{row["index"]}'
    row['definition_review_purpose'] = purpose
    save()
    async def review(role):
        from .codex_workflow import _frozen_spec_assignment
        frozen = _frozen_spec_assignment(engine,run_id,purpose,role)
        task, ctx, schema = ((frozen[k] for k in ('instruction','context','schema')) if frozen else
                            (REVIEW_TASK + (AUTHORIZED_TIMING if timing_authorization(engine,state) else ''),
                             context,review_schema(bool(timing_authorization(engine,state)))))
        hid = h.decision_key(run_id,role,purpose,task,ctx,schema)
        result = await h.ask(run_id,role,purpose,task,ctx,schema)
        await engine._read_consensus_check(run_id,authenticate,engine,run_id,role,hid,result)
        packet = {'role':role,'handoff_id':hid,'result':result,'context':copy.deepcopy(ctx)}
        await engine._read_consensus_check(run_id,validate_vote,engine,run_id,state,row,packet)
        return packet
    tasks = [asyncio.create_task(review(role)) for role in repair.ROLES]
    try:
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    except BaseException:
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        raise
    for outcome in outcomes:
        if isinstance(outcome,BaseException): raise outcome
    engine.check_active(run_id)
    row['definition_reviews'] = outcomes
    row['review_votes'] = [{'role':p['role'],'handoff_id':p['handoff_id'],**p['result']} for p in outcomes]
    row['approved'] = all(p['result']['approve'] for p in outcomes)
    if not row['approved']:
        record['status'] = 'authoring'
        save(status='specification',current_revision=row['index']+1)
        raise ImplementationSpecificationPending()
    record.update(status='approved',approved_index=row['index'],approved_at=now())
    await engine._read_consensus_check(run_id,validate_release,engine,run_id,state,row)
    record['previous_input_bundle'] = copy.deepcopy(state.get('input_bundle'))
    state['input_bundle'] = documents.export_bundle(engine,run_id,state,row)
    save(status='implementing',spec_hash=row['spec_hash'],blocked_kind=None,reason=None)
    engine.store.event(run_id,'implementation.definition_approved',
        '资源估算补充定义已获同版本三方审核，继续定向修订代码；不重跑已完成阶段。',
        {'workflow_id':state['id'],'document_sha256':row['document']['sha256']})


def validate_release(engine, run_id, state, row):
    # All entry points, including scoped dispatch and recovery, need the same
    # request-local reading/manifest caches as validate_review_snapshot.
    if hasattr(engine.store,'read_snapshot') and not getattr(engine.store,'is_read_snapshot',False):
        from .architecture_validation import operation
        with engine.store.read_snapshot() as store:
            reader=copy.copy(engine)
            reader.store=store
            if getattr(getattr(engine,'sources',None),'store',None) is engine.store:
                reader.sources=copy.copy(engine.sources)
                reader.sources.store=store
            return operation(validate_release)(reader,run_id,state,row)
    from . import implementation_documents as documents
    from .implementation_authoring import validate_submission
    record = state.get(FIELD,{})
    if row.get('definition_repair_id') != record.get('id'):
        ancestors=[r for r in state.get('definition_repair_history',[]) if r['id']==row.get('definition_repair_id')]
        if len(ancestors)!=1: _fail('缺少原定义批准链。')
        state={**state,FIELD:ancestors[0]}
        record=ancestors[0]
    if (row.get(PROTOCOL) != 1 or row.get('definition_repair_id') != record.get('id')
            or not row.get('approved') or record.get('approved_index') != row['index']):
        _fail('缺少已通过的同版本定义审核。')
    base = base_row(state,record)
    if row.get('definition_base_algorithm_length') != len(base['spec']['algorithm']):
        _fail('补定义容量没有绑定原批准正文。')
    _source(engine,run_id,state,record)
    documents.validate_review(engine,run_id,state,base)
    validate_submission(engine,run_id,state,row)
    validate_candidate(row['spec'],state,binding(state))
    validate_author_read(engine,run_id,state,row)
    packets = row.get('definition_reviews',[])
    if len(packets) != 3 or {p['role'] for p in packets} != set(repair.ROLES):
        _fail('需要同一文档的三方独立审核。')
    for packet in packets:
        if not validate_vote(engine,run_id,state,row,packet)['approve']:
            _fail('仍有未通过的定义。')
    expected = [{'role':p['role'],'handoff_id':p['handoff_id'],**p['result']} for p in packets]
    if row.get('review_votes') != expected:
        _fail('审核票据发生变化。')
    return row


def pending_dispatch(state, index):
    record = state.get(FIELD,{})
    return (record.get('status') == 'approved' and index == record['attempt_index'] + 1)


def validate_recovery_extension(engine, run_id, state, original_count):
    """Permit authenticated appended drafts without changing a recovery's past."""
    from .implementation_authoring import validate_submission
    records = [*state.get('definition_repair_history', []), state.get(FIELD, {})]
    expected = [index for record in records for index in record.get('revision_indices', [])]
    appended = state['specifications'][original_count:]
    if not appended or [row.get('index') for row in appended] != expected:
        _fail('恢复记录之后只能追加本轮补定义的真实版本。')
    for record in records:
        scoped = {**state, FIELD: record}
        _source(engine, run_id, scoped, record)
        for index in record['revision_indices']:
            row = next(r for r in appended if r['index'] == index)
            if row.get(PROTOCOL) != 1 or row.get('definition_repair_id') != record['id']:
                _fail('新增文档没有绑定补定义链。')
            validate_candidate(row['spec'], scoped, binding(scoped))
            validate_submission(engine, run_id, scoped, row)
            validate_author_read(engine, run_id, scoped, row)
            if row.get('approved') is True:
                validate_release(engine, run_id, scoped, row)
            elif row.get('approved') is False:
                packets = row.get('definition_reviews', [])
                if len(packets) != 3 or {p['role'] for p in packets} != set(repair.ROLES):
                    _fail('退回版本缺少真实三方审核。')
                for packet in packets:
                    validate_vote(engine, run_id, scoped, row, packet)
                if all(p['result']['approve'] for p in packets):
                    _fail('退回状态与真实审核不一致。')


def instruction(state, row):
    from pathlib import Path
    bundle = state['input_bundle']
    doc = Path(bundle['manifest']).parent / bundle['documents'][0]['file']
    from .code_bundle import INSTRUCTION
    return ('仅实现已经通过三方审核的“资源估算定义补充”，依原代码审查对应的资源估算缺口做最小修改。'
        '保留当前工作目录及全部已完成实现；不要重新实现其他阶段或追加文档未定义的要求。'
        f'批准实现文档："{doc}"；索引："{bundle["manifest"]}"。'
        '使用索引按需读取 algorithm 末尾的新定义及相关代码，不加载完整文档/历史。'
        '已通过项目以保存的宿主回执为准，不重跑训练/资源探针，不自行执行测试来消耗原共同预算；'
        '更新代码及 implementation.json 的实现文档绑定，按原验收要求如实记录待宿主执行的项目。'
        '缺执行证据保留 not_run，不能伪造 passed 或通过放松资源门禁来补缺口。\n' + INSTRUCTION)


def validate_dispatch(engine, run_id, state, attempt):
    record = state.get(FIELD,{})
    if (not pending_dispatch(state,attempt['index'])
            or attempt.get('definition_repair_id') != record.get('id')):
        _fail('代码交付没有绑定批准的定义续接。')
    row = next(r for r in state['specifications'] if r['index'] == record['approved_index'])
    validate_release(engine,run_id,state,row)
    if attempt.get('instruction') != instruction(state,row):
        _fail('代码指令与批准的定义续接不一致。')


def admits_document(engine, run_id, admission, document_sha256):
    """An approved child document inherits the original quota, never resets it."""
    state = engine.store.get('codex_workflows',admission['workflow_id'],{})
    record = state.get(FIELD,{})
    if record.get('status') != 'approved': return False
    row = next(r for r in state['specifications'] if r['index'] == record['approved_index'])
    original = (state.get('definition_repair_history') or [record])[0]
    base = base_row(state,original)
    if (base['document']['sha256'] != admission['implementation_document_sha256']
            or row['document']['sha256'] != document_sha256): return False
    validate_release(engine,run_id,state,row)
    return True
