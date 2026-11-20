"""Post-approval definition completion, with immutable ancestry and scoped review.

The original SPEC and its approval stay untouched. Only an appended engineering
definition is editable; three authenticated readers must approve that addition
against the original scientific sources before a new code instruction exists.
"""
from __future__ import annotations

from .async_ownership import gather as gather_owned, cancel as cancel_owned

import asyncio
import copy

from . import code_repair_contract as repair
from .storage import now

FIELD = 'implementation_definition_repair'
PROTOCOL = 'implementation_definition_repair_protocol'
MAX_ADDITION = 6000
AUTHORIZATIONS = 'implementation_timing_authorizations'


def _review_votes(packets):
    from . import independent_full_review as full
    values = []
    for packet in packets:
        binding = packet.get('context', {}).get(full.KEY)
        values.append(full.stamp_vote(packet['role'], packet['result'], packet['handoff_id'], binding)
            if binding else {'role': packet['role'], 'handoff_id': packet['handoff_id'], **packet['result']})
    return values


def _review_members(packets):
    from .independent_full_review import members
    return members(_review_votes(packets))

ENGINEERING_AUTHOR_TASK = '''你是实现文档作者。本轮补齐原代码审查明确指出的工程定义缺口。
只在原 algorithm 末尾追加独立的“工程定义补充”小节；原正文和其他字段逐字保留。
definition_repair 中包含原问题、批准条款及原作者暂缓理由。实际读取相关批准来源和
definition_code_sources 中的同版本代码/配置，明确缺失概念的来源、实际消费和校验绑定。
不能把实现中的猜测当批准依据。已有科学来源足以决定的工程语义由你明确；与批准来源真正矛盾、
需要改变模型/数据/参数空间/验收标准的项目必须保留未解决并给出具体原因，不得悄悄改科学方案。
不新增实验、种子、预算或执行授权；不把缺执行回执写成通过。逐项回应原缺口，保留已完成工作。
用 implementation.edit_document 保存追加条款并读回新增正文，再提交真实保存回执。'''

ENGINEERING_REVIEW_TASK = '''独立审核同版本新实现文档的工程定义补充。原 SPEC 正文及其他字段已逐字保留。
只评审 definition_repair 的原缺口和新增条款。实际读取完整 definition_addition、相关批准科学来源，
并按需读取 definition_code_sources 中实际配置/实现，检查来源、消费、校验是否明确且一致。
resolutions 只列 definition_repair.issues 中的 issue_id，每项恰好一次。deferred_findings 供查阅；
其他暂缓项保留在原台账中。不能用原实现常量或他人意见代替批准来源。真正冲突、定义不完整时给出具体修订意见。
保留旧修改的暂缓理由、未运行检查和实验回执，不重复扩大验收或要求重跑已完成训练。
approve=true 仅批准新增工程定义交给 Codex 实现，不代表代码已修复、测试通过或新增执行许可。'''

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
        ('base_index','base_row_sha256','source_sha256','attempt_index','request_ids','source_kind',
         'kind','source_record_key','deferred_packet_sha256') if k in record}
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
            '只允许在原 algorithm 末尾补齐必要工程定义；原文及其他字段必须逐字保留。')


def _source(engine, run_id, state, record):
    from .code_repair_clarification import verify_saved
    base_row(state, record)
    timing_authorization(engine, state)
    attempt = next(a for a in state['attempts'] if a['index'] == record['attempt_index'])
    source_key = record.get('source_record_key', record.get('source_kind'))
    source = (verify_saved(engine, run_id, state, attempt, attempt[source_key])
        if source_key in {'verification_followup', 'deferred_repair_clarification', 'deferred_repair_execution', 'host_constraint_followup', 'deferred_evidence_clarification'} else
        repair.build_source(engine, run_id, state, base_row(state,record), attempt))
    if repair.sha(source) != record['source_sha256']:
        _fail('原始缺口证据发生变化。')
    for key in record['request_ids']:
        request = engine.store.get('implementation_definition_requests', key)
        if (not request or request['workflow_id'] != state['id']
                or request['source_sha256'] != record['source_sha256']):
            _fail('缺少同一交付的真实定义请求。')
        if record.get('kind') == 'engineering_definition':
            issue = next((i for i in source['issues'] if i['issue_id'] == request['issue']['issue_id']),None)
            expected = {k:v for k,v in request.items() if k not in {'id','created_at'}}
            if (key != run_id+':'+repair.sha(expected) or request.get('run_id') != run_id
                    or issue != request['issue']
                    or (issue.get('binding') or {}).get('disposition') not in {'needs_definition','spec_conflict'}
                    or request['references'] != [source['role_evidence'][issue['role']][i]
                        for i in issue['binding']['evidence_indices']]):
                _fail('工程定义请求与原审查证据不一致。')
    if record.get('deferred_packet_sha256'):
        from .code_repair_deferred import validate_scope, scope_for
        packet = engine.store.get('code_repair_packets',record['deferred_packet_sha256'],{}).get('packet')
        if not packet:
            _fail('暂缓修改原清单缺失。')
        original = repair.build_source(engine,run_id,state,base_row(state,record),attempt)
        validate_scope(engine,run_id,original,scope_for(packet))
    return source


def begin(engine, run_id, state, specification, attempt, source, requests, save,
          *, kind=None, source_key=None, deferred_packet_sha256=None):
    if not requests:
        _fail('没有待补定义。')
    value = {'base_index':specification['index'], 'base_row_sha256':repair.sha(specification),
        'source_sha256':repair.sha(source), 'attempt_index':attempt['index'], 'request_ids':requests,
        'source_kind':'verification_followup' if attempt.get('verification_followup',{}).get('status')=='completed' else 'review_votes'}
    if kind is not None:
        if kind != 'engineering_definition':
            _fail('未知补定义范围。')
        value['kind'] = kind
    if source_key is not None:
        if source_key not in {'review_votes','verification_followup','deferred_repair_clarification','deferred_repair_execution','host_constraint_followup','deferred_evidence_clarification'}:
            _fail('未知缺口来源。')
        value['source_record_key'] = source_key
    if deferred_packet_sha256 is not None:
        value['deferred_packet_sha256'] = deferred_packet_sha256
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


def _next_revision(state, record, previous_index):
    archived = state.get('host_constraint_definition_routes', {})
    skipped = {index for old in state.get('definition_repair_history', [])
        if old['id'] in archived and old['id'] != record['id'] for index in old['revision_indices']}
    index = previous_index + 1
    while index in skipped:
        index += 1
    return index


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
    if len(rows) != 1 or revision != _next_revision(state, record, index):
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
                or not _review_members(packets)):
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
    if record.get('kind') == 'engineering_definition':
        result['scope'] = '只在 algorithm 末尾补齐原审查缺失的工程定义；批准正文、科学方案和验收要求逐字保留。'
        if record.get('deferred_packet_sha256'):
            from .code_repair_deferred import findings
            packet = engine.store.get('code_repair_packets',record['deferred_packet_sha256'],{})['packet']
            result['deferred_packet_sha256'] = record['deferred_packet_sha256']
            result['deferred_findings'] = findings(packet)
    return result


def addition_refs(engine, run_id, state, row):
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
    from .document_reference_spans import references
    try:
        return references({**row['document'],'sections':doc['sections']},start,start+len(suffix))
    except ValueError as exc:
        _fail('补充段无法按当前文档索引分页：'+str(exc))


def addition_ref(engine, run_id, state, row):
    """Legacy short-addition locator; full coverage always uses addition_refs."""
    return addition_refs(engine,run_id,state,row)[0]


def addition_context(engine, run_id, state, row):
    refs = addition_refs(engine,run_id,state,row)
    return {'definition_addition':refs[0], **({'definition_additions':refs,
        'definition_addition_instruction':'definition_additions 是完整补充段的全部分页，必须逐页读完；'
            'definition_addition 仅是第一页，不能代表整段。每页 limit 不超过6000，不裁剪尾部或跳过章节。'}
        if len(refs)>1 else {})}


def review_schema(authorized=False, *, issue_ids=None, whole=False):
    from .implementation_alignment import SOURCE_REFS
    fields = {'approve':{'type':'boolean'}, 'proposal_hash':repair.TEXT, 'spec_hash':repair.TEXT,
        'document_sha256':repair.TEXT, 'rationale':repair.TEXT,
        'issues':{'type':'array','items':repair.TEXT,'maxItems':12},
        'fixes':{'type':'array','maxItems':12,'items':repair.obj({'issue_index':{'type':'integer','minimum':0},
            'change':repair.TEXT,'verification':repair.TEXT})},
        'resolutions':{'type':'array','items':repair.obj({'issue_id':repair.TEXT,
            'resolved':{'type':'boolean'},'reason':repair.TEXT})},
        'definition_refs':copy.deepcopy(SOURCE_REFS),'source_refs':copy.deepcopy(SOURCE_REFS)}
    if issue_ids is not None:
        resolutions = fields['resolutions']
        resolutions.update(minItems=len(issue_ids), maxItems=len(issue_ids))
        resolutions['items']['properties']['issue_id'] = {'type':'string', 'enum':list(issue_ids)}
    if authorized:
        fields['timing_authorization_sha256'] = repair.TEXT
    if whole:
        fields['fixes']['items']['properties']['location'] = copy.deepcopy(SOURCE_REFS['items'])
        fields['fixes']['items']['required'].append('location')
    return repair.obj(fields)


def validate_resolutions(scope, result):
    required = {issue['issue_id'] for issue in scope['issues']}
    historical = {issue['issue_id'] for issue in scope.get('deferred_findings', [])} - required
    seen = set()
    for entry in result['resolutions']:
        issue_id = entry['issue_id']
        if issue_id in seen:
            _fail('定义审核重复回答同一问题：' + issue_id)
        seen.add(issue_id)
        if issue_id in required:
            if result['approve'] and not entry['resolved']:
                _fail('同意补定义时仍有本轮缺口标记为未闭合：' + issue_id)
        elif issue_id not in historical or entry['resolved']:
            _fail('定义审核引用未知问题或试图关闭本轮以外的待办：' + issue_id)
        # Older frozen assignments invited readers to retain deferred findings.
        # An explicit unresolved entry for one is informational in that ballot.
    if missing := required - seen:
        _fail('定义审核漏答本轮缺口：' + '、'.join(sorted(missing)))
    if {f['issue_index'] for f in result['fixes']} != set(range(len(result['issues']))):
        _fail('定义审核的修订建议必须对应本次提出的每一条问题。')
    if result['approve'] == bool(result['issues']):
        _fail('定义审核同意时须闭合当前缺口，退回时须列出具体修订问题。')


def _definition_sources(engine, run_id, state, issues):
    """Resolve issue citations against the reviewed delivery and its exact diff base."""
    from . import code_revision_review as revision
    from .code_whole_policy import _specification
    from .codex_workflow import _snapshot_artifacts
    record = state[FIELD]
    attempt = next(a for a in state['attempts'] if a['index'] == record['attempt_index'])
    marker = attempt.get(revision.KEY)
    if marker is None:
        return _snapshot_artifacts(engine,run_id,attempt['snapshot'])
    current = list(revision._directory(engine,run_id,attempt).values())
    current_ids = {r['artifact_id']:r['sha256'] for r in current}
    prior_refs = [ref for issue in issues for ref in issue['references'] if 'artifact_id' in ref
                  and current_ids.get(ref['artifact_id']) != ref['sha256']]
    if not prior_refs:
        return current
    spec = _specification(state,attempt,base_row(state,record))
    compiled = revision.compile_context(engine,run_id,state,spec,attempt)
    if compiled[revision.KEY] != marker:
        _fail('定义引用的原代码差异版本与冻结审核不一致。')
    changes = compiled['revision_material']['changes']
    before = {(src['artifact_id'],src['sha256']):change for change in changes if (src := change['before'])}
    historical = {}
    for ref in prior_refs:
        change = before.get((ref['artifact_id'],ref['sha256']))
        if ref['artifact_id'] in current_ids or not change or not revision._supported_evidence(ref,[],[change]):
            _fail('定义引用缺少原审核差异中的精确基线范围。')
        historical[ref['artifact_id']] = {**change['before'],'base_attempt':marker['base_attempt']}
    return [*current,*historical.values()]


def validate_vote(engine, run_id, state, row, packet):
    from .implementation_acceptance_flow import check_packet, validate_refs
    from . import implementation_definition_review_scope as scope
    from . import host_asset_definition_recovery as asset_recovery
    context = check_packet(engine, run_id, packet)
    if scope.REVISION in context or (row.get('definition_review_purpose') or '').endswith(':scoped-v4'):
        if context.get(scope.REVISION) != scope.revision_comparison(engine, run_id, state, row):
            _fail('工程定义修订复审未绑定上一版本的差异和原评审意见。')
    if scope.enabled(context):
        original_issues = issue_context(engine,state)['issues']
        attempt = next(a for a in state['attempts'] if a['index'] == state[FIELD]['attempt_index'])
        current = {(r['artifact_id'],r['sha256']) for r in attempt['delivery_artifacts']}
        cited = {(r['artifact_id'],r['sha256']) for i in original_issues for r in i['references'] if 'artifact_id' in r}
        version = context.get('definition_code_source_version')
        if 'definition_code_source_version' in context or cited - current or any(
                'base_attempt' in r for r in context['definition_code_sources']):
            if type(version) is not int or version != 2:
                _fail('混合版本的工程定义代码目录缺少已知版本。')
            catalog = _definition_sources(engine,run_id,state,original_issues)
            addition = row['spec']['algorithm'][row['definition_base_algorithm_length']:]
            expected = scope.attach({**context,'definition_code_sources':catalog},row['document'],addition)
            if any(context.get(k) != expected.get(k) for k in (
                    'definition_code_source_version','definition_code_sources','definition_required_code_ranges')):
                _fail('工程定义审核的来源版本或必读范围发生变化。')
    if context.get(asset_recovery.KEY) != asset_recovery.author_evidence(engine, run_id, state, row):
        _fail('工程定义复审未绑定作者实际收到的同一宿主资产证据。')
    from . import independent_full_review as full
    if full.KEY in context:
        full.validate_delivery(engine.store, run_id, _review_votes([packet])[0], engine=engine)
    result = packet['result']
    scope.validate_citation(context, result)
    authorization = timing_authorization(engine,state)
    if authorization and result.get('timing_authorization_sha256') != authorization['sha256']:
        _fail('审核没有确认本轮用户计时授权，不能复用旧范围结论。')
    if (context.get(PROTOCOL) != 1 or context.get('definition_repair') != issue_context(engine, state)
            or context.get('definition_addition') != addition_ref(engine, run_id, state, row)
            or context.get('definition_additions',[context.get('definition_addition')]) != addition_refs(engine,run_id,state,row)
            or result['spec_hash'] != row['spec_hash'] or result['proposal_hash'] != state['proposal_hash']
            or result['document_sha256'] != row['document']['sha256']):
        _fail('审核没有绑定当前定义和原问题。')
    validate_resolutions(context['definition_repair'], result)
    if asset_recovery.KEY in context:
        asset_recovery.validate_review(context[asset_recovery.KEY], result)
    if context.get('whole_definition_review_protocol') == 2:
        for fix in result['fixes']:
            validate_refs(engine,run_id,packet['handoff_id'],context,[fix['location']],
                current=row['document'],field='fixes.location')
    validate_refs(engine, run_id, packet['handoff_id'], context, result['definition_refs'],
        current=row['document'], field='definition_refs')
    # The *whole addition* is new; its review cannot be based on reading only its title.
    validate_refs(engine, run_id, packet['handoff_id'], context, addition_refs(engine,run_id,state,row), current=row['document'])
    scientific = context['architecture_document']
    if full.KEY in context and not scope.enabled(context) and not any(
            (ref['document_id'], ref['document_sha256']) == (scientific['document_id'], scientific['sha256'])
            for ref in result['source_refs']):
        scope.request_citation(engine, run_id, packet)
    validate_refs(engine, run_id, packet['handoff_id'], context, result['source_refs'],
        current=None if scope.enabled(context) else scientific, field='source_refs')
    return result


def validate_author_read(engine,run_id,state,row):
    from .definition_readback import coverage
    if not coverage(engine,run_id,state,row)['complete']:
        _fail('已保存定义仍有未实际读取的正文；继续定向补读，不重新生成文档。')


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
    if not _review_members(packets):
        _fail('需要同一文档的三方独立审核。')
    for packet in packets:
        if not validate_vote(engine,run_id,state,row,packet)['approve']:
            _fail('仍有未通过的定义。')
    expected = _review_votes(packets)
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
                if not _review_members(packets):
                    _fail('退回版本缺少真实三方审核。')
                for packet in packets:
                    validate_vote(engine, run_id, scoped, row, packet)
                if all(p['result']['approve'] for p in packets):
                    _fail('退回状态与真实审核不一致。')


def instruction(state, row, *, detailed=False, work_order_version=1):
    from pathlib import Path
    bundle = state['input_bundle']
    doc = Path(bundle['manifest']).parent / bundle['documents'][0]['file']
    from .code_bundle import INSTRUCTION
    prefix = ('仅实现已经通过三方审核的“工程定义补充”，依原代码审查对应的工程定义缺口做最小修改。'
              if state.get(FIELD,{}).get('kind') == 'engineering_definition' else
              '仅实现已经通过三方审核的“资源估算定义补充”，依原代码审查对应的资源估算缺口做最小修改。')
    task = (prefix +
        '保留当前工作目录及全部已完成实现；不要重新实现其他阶段或追加文档未定义的要求。'
        f'批准实现文档："{doc}"；索引："{bundle["manifest"]}"。'
        '使用索引按需读取 algorithm 末尾的新定义及相关代码，不加载完整文档/历史。'
        '已通过项目以保存的宿主回执为准，不重跑训练/资源探针，不自行执行测试来消耗原共同预算；'
        '更新代码及 implementation.json 的实现文档绑定，按原验收要求如实记录待宿主执行的项目。'
        '缺执行证据保留 not_run，不能伪造 passed 或通过放松资源门禁来补缺口。\n' + INSTRUCTION)
    if state.get('hyperparameter_yaml_protocol') == 1:
        from .yaml_migration import instructions as yaml_instructions
        yaml_contract = yaml_instructions(state.get('yaml_configuration_scope', 'hyperparameters'))
        task += '\n' + yaml_contract
    if detailed:
        review = row['definition_reviews'][0]
        issues = review['context']['definition_repair']['issues']
        reasons = {r['issue_id']:r['reason'] for r in review['result']['resolutions'] if r['resolved']}
        lines = ['本次定向修改依据']
        for issue in issues:
            lines.extend([f"{issue['requirement_id']} 原问题：{issue['issue']}",
                          f"本轮已核准的处理：{reasons[issue['issue_id']]}"])
        if work_order_version == 2:
            approved = row['document']
            lines.append(f'当前批准文档 {approved["title"]}，document_id={approved["document_id"]}，'
                         f'SHA256={approved["sha256"]}。')
            requirements = {issue['requirement_id'] for issue in issues}
            lines.extend(f'{item["requirement_id"]} 实现位置：{item["location"]}'
                for item in row['spec']['implementation_map'] if item['requirement_id'] in requirements)
            sources = {s['artifact_id']: s for s in review['context'].get('definition_code_sources', [])}
            for issue in issues:
                for ref in issue['references']:
                    source = sources.get(ref.get('artifact_id'))
                    if source and source['sha256'] == ref['sha256']:
                        version = f"（基线交付 {source['base_attempt']}）" if 'base_attempt' in source else ''
                        lines.append(f'{issue["requirement_id"]} 已审核源码范围{version}：{source["path"]}，'
                            f'SHA256={ref["sha256"]}，offset={ref["offset"]}，limit={ref["limit"]}。')
            previous = max((item for item in state['specifications'] if item['index'] < row['index']),
                key=lambda item: item['index'])
            algorithm = row['spec']['algorithm']
            prefix = previous['spec']['algorithm']
            latest = algorithm[len(prefix):].strip() if algorithm.startswith(prefix) else ''
            references = [ref for packet in row['definition_reviews'] for ref in packet['result']['definition_refs']]
            inline_latest = bool(latest) and len(latest) <= 12000
            if not inline_latest:
                references.extend(review['context'].get('definition_additions', []))
            ranges = {(ref['section_id'], ref['offset'], ref['limit']) for ref in references
                if ref['document_id'] == approved['document_id'] and ref['document_sha256'] == approved['sha256']}
            lines.extend(f'同版审核对应的文档位置：{section}，offset={offset}，limit={limit}。'
                for section, offset, limit in sorted(ranges))
            lines.append('按同版实现位置及工程定义增补落实修改；架构按需核对。')
            if inline_latest:
                lines.extend(['当前文档本次追加的原文：', latest])
            task = task.replace('通过三方审核的', '通过独立审核的', 1)
        else:
            appendix = row['spec']['algorithm'][row['definition_base_algorithm_length']:].strip()
            lines.extend(['实现位置、修改步骤与验收条件以本次三方批准的增补原文为准。', appendix])
        task += '\n' + '\n'.join(lines)
    return task


def validate_dispatch(engine, run_id, state, attempt):
    record = state.get(FIELD,{})
    if (not pending_dispatch(state,attempt['index'])
            or attempt.get('definition_repair_id') != record.get('id')):
        _fail('代码交付没有绑定批准的定义续接。')
    row = next(r for r in state['specifications'] if r['index'] == record['approved_index'])
    validate_release(engine,run_id,state,row)
    version = attempt.get('definition_work_order_version')
    if version is not None and (type(version) is not int or version not in (1, 2)):
        _fail('未知的工程定义工作单版本。')
    expected = instruction(state, row, detailed=version in (1, 2), work_order_version=version)
    if (version == 2 or attempt.get('directory_layout_version') == 2) and not attempt.get('delivery_manifest_schema'):
        _fail('代码指令缺少绑定批准文档的交付结构。')
    if attempt.get('delivery_manifest_schema') is not None:
        import hashlib
        import json
        from pathlib import Path
        from . import code_review_partition, code_whole_review, delivery_manifest_contract
        from .codex_workflow import writer_instruction

        bundle = state['input_bundle']
        frozen = Path(bundle['manifest']).read_bytes()
        manifest = json.loads(frozen)
        primary = bundle['documents'][0]
        document_sha256 = row['document']['sha256']
        if (hashlib.sha256(frozen).hexdigest() != bundle['manifest_sha256']
                or manifest['run_id'] != run_id or manifest['proposal_hash'] != state['proposal_hash']
                or manifest['spec_hash'] != row['spec_hash'] or state['spec_hash'] != row['spec_hash']
                or manifest['documents'] != bundle['documents']
                or {key: value for key, value in primary.items() if key != 'file'} != row['document']
                or attempt['delivery_manifest_schema'] != delivery_manifest_contract.schema(document_sha256=document_sha256)):
            _fail('代码指令的输入目录或交付结构未绑定最新批准的实现文档。')
        expected = writer_instruction(expected, document_sha256,
            whole_delivery=version != 2 and code_whole_review.enabled(state),
            source_plan=attempt.get(code_review_partition.PLAN_FIELD))
    if attempt.get('instruction') != expected:
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
